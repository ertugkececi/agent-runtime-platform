"""The Python side of the OpenCode bridge.

One model turn runs one short-lived TypeScript host process (``host/``): one
JSON request on stdin, NDJSON records on stdout, process exit as the end of the
turn. The wire contract is ``docs/opencode-bridge-contract.md``. There is no
daemon and no state shared between calls.

The adapter owns the process: a wall-clock budget from provider configuration
kills the host on expiry, and every host failure becomes one
:class:`ProviderError`, so a run fails instead of hanging.
"""

from __future__ import annotations

import json
import math
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from agent_runtime_platform.providers._base import HandoffRequest, ModelOutput, ProviderError

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

# The host fills in a private root for every XDG variable that is left unset,
# and rejects the invoking user's OpenCode configuration. #82 pins the concrete
# persistent mechanism; until then the adapter must not hand the child the
# user's roots in the first place.
XDG_ROOTS = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME")

ToolEvent = Callable[[dict[str, str]], None]


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
        try:
            process = subprocess.Popen(
                command,
                cwd=str(HOST_ROOT),
                env=_child_environment(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                start_new_session=os.name == "posix",
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
        return _result_value(result, allow_handoff=allow_handoff)


def _child_environment() -> dict[str, str]:
    """The server environment without the invoking user's XDG roots."""
    return {key: value for key, value in os.environ.items() if key not in XDG_ROOTS}


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
