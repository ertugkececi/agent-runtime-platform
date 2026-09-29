"""The Python side of the OpenCode bridge.

One model turn runs one short-lived TypeScript host process (``host/``): one
JSON request on stdin, NDJSON records on stdout, process exit as the end of the
turn. The wire contract is ``docs/opencode-bridge-contract.md``. There is no
daemon and no state shared between calls.

The adapter owns the process: a wall-clock budget from provider configuration
kills the host on expiry, and every host failure becomes one
:class:`ProviderError`, so a run fails instead of hanging.

Every call runs in a private home of its own (``0700``, files ``0600``) that
replaces ``HOME`` and the XDG roots for the child, so the invoking user's
``~/.config/opencode`` is never read or modified.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from agent_runtime_platform.infrastructure.providers._base import HandoffRequest, ModelOutput, ProviderError

BRIDGE_PROTOCOL = 1
COMPATIBLE_MIN = 1
COMPATIBLE_MAX_EXCLUSIVE = 2

HOST_ROOT = Path(__file__).resolve().parent / "host"
TIMEOUT_ENV = "AGENT_RUNTIME_OPENCODE_TIMEOUT_SECONDS"
DEFAULT_TIMEOUT_SECONDS = 600.0
MAXIMUM_TIMEOUT_SECONDS = 3600.0
EXIT_GRACE_SECONDS = 5.0

INVALID_RESPONSE = "The OpenCode bridge host returned an invalid response."
FAILED_REQUEST = "The OpenCode bridge host failed the model request."
TIMEOUT_MESSAGE = "The OpenCode model request timed out."
VERSION_MESSAGE = "The OpenCode bridge host reported an incompatible bridge protocol."

# The child never sees the invoking user's roots. HOME and every XDG variable
# point into one private home per call, so OpenCode resolves its configuration,
# data, cache and state inside a directory this process owns and removes.
HOME_VARIABLE = "HOME"
XDG_ROOTS: tuple[tuple[str, str], ...] = (
    ("XDG_CONFIG_HOME", "config"),
    ("XDG_DATA_HOME", "data"),
    ("XDG_CACHE_HOME", "cache"),
    ("XDG_STATE_HOME", "state"),
)

ToolEvent = Callable[[dict[str, str]], None]


def list_opencode_models() -> list[dict[str, Any]]:
    """Expose the OpenCode model and effort choices, in the shared catalog shape."""
    return OpenCodeChatProvider().list_models()


class OpenCodeChatProvider:
    """Run OpenCode through its bridge host, one process per call."""

    def __init__(
        self,
        *,
        command: Sequence[str] | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        """``command`` and ``timeout_seconds`` exist so tests can replace the host."""
        self._command = list(command) if command is not None else None
        self._timeout_seconds = timeout_seconds

    def generate(
        self,
        agent: dict[str, Any],
        history: list[dict[str, str]],
        *,
        allow_handoff: bool = False,
    ) -> ModelOutput:
        tool_ids = sorted(set(agent.get("tool_ids") or []))
        if tool_ids:
            raise ProviderError("tool_ids are not supported by this provider.")
        request = self._request(agent, history, allow_handoff=allow_handoff)
        callback = agent.get("tool_event_callback")
        exit_code, records = self._run(request, callback if callable(callback) else None)
        return self._interpret(records, exit_code, allow_handoff=allow_handoff)

    def list_models(self) -> list[dict[str, Any]]:
        """Run one host process and return its model catalog."""
        request = {"bridge_protocol": BRIDGE_PROTOCOL, "operation": "models"}
        exit_code, records = self._run(request, None)
        return _catalog_value(records, exit_code)

    @staticmethod
    def _request(
        agent: dict[str, Any],
        history: list[dict[str, str]],
        *,
        allow_handoff: bool,
    ) -> dict[str, Any]:
        model = agent["model_name"]
        effort = agent.get("model_reasoning_effort")
        if isinstance(model, str) and isinstance(effort, str) and effort and "#" not in model:
            # OpenCode selects effort through model variants, not a request field.
            model = f"{model}#{effort}"
        request: dict[str, Any] = {
            "bridge_protocol": BRIDGE_PROTOCOL,
            "model": model,
            "instructions": agent["instructions"],
            "history": [
                {"role": message["role"], "content": message["content"]}
                for message in history
            ],
            "allow_handoff": allow_handoff,
            "tool_ids": [],
        }
        if allow_handoff:
            capabilities = sorted(set(agent.get("remote_a2a_capabilities") or []))
            if capabilities:
                request["remote_capabilities"] = capabilities
        return request

    def _run(
        self,
        request: dict[str, Any],
        on_event: ToolEvent | None,
    ) -> tuple[int, list[dict[str, Any]]]:
        command = self._command or self._default_command()
        timeout = self._timeout()
        try:
            payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ProviderError(FAILED_REQUEST) from exc
        with _private_home() as home:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(HOST_ROOT),
                    env=_child_environment(home),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    start_new_session=os.name == "posix",
                    # Files the host creates in the private home are 0600 and
                    # its directories 0700, whatever the server's umask is.
                    umask=0o077 if os.name == "posix" else None,
                )
            except OSError as exc:
                raise ProviderError("The OpenCode bridge host could not be started.") from exc
            try:
                try:
                    process.stdin.write(payload)
                    process.stdin.close()
                except OSError as exc:
                    raise ProviderError(FAILED_REQUEST) from exc
                return _read(process, timeout, on_event)
            finally:
                # Every path out of this method leaves no host process behind.
                if process.poll() is None:
                    _kill(process)

    @staticmethod
    def _default_command() -> list[str]:
        bun = shutil.which("bun")
        if bun is None:
            raise ProviderError("The OpenCode bridge host requires 'bun' on PATH.")
        return [bun, "run", "start"]

    def _timeout(self) -> float:
        if self._timeout_seconds is not None:
            return self._timeout_seconds
        raw = os.getenv(TIMEOUT_ENV)
        if raw is None or not raw.strip():
            return DEFAULT_TIMEOUT_SECONDS
        try:
            value = float(raw)
        except ValueError as exc:
            raise ProviderError(f"{TIMEOUT_ENV} must be a number of seconds.") from exc
        if not math.isfinite(value) or value <= 0:
            raise ProviderError(f"{TIMEOUT_ENV} must be a positive number of seconds.")
        return min(value, MAXIMUM_TIMEOUT_SECONDS)

    @staticmethod
    def _interpret(
        records: list[dict[str, Any]],
        exit_code: int,
        *,
        allow_handoff: bool,
    ) -> ModelOutput:
        return _result_value(
            _final_record(records, exit_code), allow_handoff=allow_handoff
        )


def _final_record(records: list[dict[str, Any]], exit_code: int) -> dict[str, Any]:
    error: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    for index, record in enumerate(records):
        if result is not None:
            # The contract makes `result` the last and only result record.
            raise ProviderError(INVALID_RESPONSE)
        kind = record["type"]
        if kind == "error":
            if error is not None:
                raise ProviderError(INVALID_RESPONSE)
            error = record
        elif kind == "result":
            result = record
        elif kind == "hello" and index != 0:
            raise ProviderError(INVALID_RESPONSE)
        # Unknown record types are ignored within the compatible window.
    if error is not None:
        message = error.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ProviderError(INVALID_RESPONSE)
        raise ProviderError(message)
    if _hello_version(records) is None:
        raise ProviderError(VERSION_MESSAGE)
    if result is None or exit_code != 0:
        raise ProviderError(FAILED_REQUEST)
    return result


def _catalog_value(records: list[dict[str, Any]], exit_code: int) -> list[dict[str, Any]]:
    result = _final_record(records, exit_code)
    if result.get("kind") != "models":
        raise ProviderError(INVALID_RESPONSE)
    models = result.get("models")
    if not isinstance(models, list):
        raise ProviderError(INVALID_RESPONSE)
    return [_catalog_entry(entry) for entry in models]


def _catalog_entry(entry: Any) -> dict[str, Any]:
    """Keep only the catalog fields, with the types the HTTP response promises."""
    if not isinstance(entry, dict):
        raise ProviderError(INVALID_RESPONSE)
    model_id = entry.get("id")
    if not isinstance(model_id, str) or not model_id.strip():
        raise ProviderError(INVALID_RESPONSE)
    label = entry.get("label")
    if not isinstance(label, str):
        raise ProviderError(INVALID_RESPONSE)
    efforts = entry.get("efforts")
    if not isinstance(efforts, list) or any(not isinstance(effort, str) for effort in efforts):
        raise ProviderError(INVALID_RESPONSE)
    is_default = entry.get("is_default")
    if not isinstance(is_default, bool):
        raise ProviderError(INVALID_RESPONSE)
    default_effort = entry.get("default_effort")
    if not isinstance(default_effort, str):
        raise ProviderError(INVALID_RESPONSE)
    return {
        "id": model_id,
        "label": label,
        "is_default": is_default,
        "default_effort": default_effort,
        "efforts": list(efforts),
    }


def _child_environment(home: Path) -> dict[str, str]:
    """The server environment with every OpenCode root pointed at ``home``.

    The user's ``HOME`` and XDG values are replaced, never forwarded. Environment
    variables are how configuration and credentials reach the host, so the rest
    of the environment is passed through untouched.
    """
    environment = {
        key: value
        for key, value in os.environ.items()
        if key != HOME_VARIABLE and key not in dict(XDG_ROOTS)
    }
    environment[HOME_VARIABLE] = str(home)
    for name, directory in XDG_ROOTS:
        environment[name] = str(home / directory)
    return environment


@contextlib.contextmanager
def _private_home() -> Iterator[Path]:
    """A private home for one host call; every path in it is removed at the end."""
    root = Path(tempfile.mkdtemp(prefix="agent-runtime-opencode-"))
    root.chmod(0o700)
    try:
        for _, directory in XDG_ROOTS:
            (root / directory).mkdir(mode=0o700)
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _read(
    process: subprocess.Popen,
    timeout: float,
    on_event: ToolEvent | None,
) -> tuple[int, list[dict[str, Any]]]:
    deadline = time.monotonic() + timeout
    lines: queue.Queue[bytes | None] = queue.Queue()
    threading.Thread(target=_pump_lines, args=(process.stdout, lines), daemon=True).start()
    records: list[dict[str, Any]] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError(TIMEOUT_MESSAGE)
        try:
            line = lines.get(timeout=remaining)
        except queue.Empty:
            raise ProviderError(TIMEOUT_MESSAGE)
        if line is None:
            break
        record = _decode(line)
        if record["type"] == "event":
            event = _tool_event(record)
            if on_event is not None:
                on_event(event)
        records.append(record)
    try:
        exit_code = process.wait(timeout=EXIT_GRACE_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise ProviderError(TIMEOUT_MESSAGE) from exc
    return exit_code, records


def _pump_lines(stream: Any, lines: "queue.Queue[bytes | None]") -> None:
    """Read stdout in a thread, so a silent host cannot outlive its deadline."""
    try:
        for line in stream:
            lines.put(line)
    except (OSError, ValueError):
        pass
    finally:
        lines.put(None)


def _decode(line: bytes) -> dict[str, Any]:
    try:
        record = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError(INVALID_RESPONSE) from exc
    if not isinstance(record, dict) or not isinstance(record.get("type"), str):
        raise ProviderError(INVALID_RESPONSE)
    return record


def _tool_event(record: dict[str, Any]) -> dict[str, str]:
    fields = ("server", "tool", "status", "phase")
    if any(not isinstance(record.get(field), str) for field in fields):
        raise ProviderError(INVALID_RESPONSE)
    return {field: record[field] for field in fields}


def _hello_version(records: list[dict[str, Any]]) -> int | None:
    if not records or records[0].get("type") != "hello":
        return None
    version = records[0].get("bridge_protocol")
    if not isinstance(version, int) or isinstance(version, bool):
        return None
    if not COMPATIBLE_MIN <= version < COMPATIBLE_MAX_EXCLUSIVE:
        return None
    return version


def _result_value(record: dict[str, Any], *, allow_handoff: bool) -> ModelOutput:
    kind = record.get("kind")
    if kind == "reply":
        content = record.get("content")
        if not isinstance(content, str):
            raise ProviderError(INVALID_RESPONSE)
        if not content.strip():
            raise ProviderError("The OpenCode model returned an empty response.")
        return content.strip()
    if kind == "handoff":
        if not allow_handoff:
            raise ProviderError("The model requested a handoff when handoff was disabled.")
        capability = record.get("capability")
        task = record.get("task")
        if not isinstance(capability, str) or not isinstance(task, str):
            raise ProviderError(INVALID_RESPONSE)
        return HandoffRequest(capability=capability, task=task)
    raise ProviderError(INVALID_RESPONSE)


def _kill(process: subprocess.Popen) -> None:
    """Force the host and everything it started to stop; never leak a process."""
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:
            process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=EXIT_GRACE_SECONDS)
    except subprocess.TimeoutExpired:  # pragma: no cover - the kernel may lag
        pass
