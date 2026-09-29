"""Interactive provider connections through the OpenCode bridge host.

One connection owns one long-lived bridge host process: it starts the OAuth
attempt, reports the sign-in details, waits until the provider credential is
stored, and exits. The host's data root is the persistent one, so the
credential survives the call. A ``code``-mode attempt additionally accepts one
line on the host's stdin, written when the human submits the code.

The service is deliberately in-process: it holds subprocess handles and waits
for a human, so it lives and dies with the server process that started it. The
API documents that a multi-process deployment must pin a connection to one
worker.
"""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import IO, Any, Sequence

from agent_runtime_platform.infrastructure.providers._base import ProviderError
from agent_runtime_platform.infrastructure.providers.opencode.provider import (
    BRIDGE_PROTOCOL,
    CONNECT_STOP_MESSAGE,
    EXIT_GRACE_SECONDS,
    FAILED_REQUEST,
    INVALID_RESPONSE,
    TIMEOUT_MESSAGE,
    OpenCodeChatProvider,
    _create_private_home,
    _decode,
    _kill,
    _pump_lines,
    _spawn,
)

# How long `start` waits for the host to report the sign-in details.
START_TIMEOUT_SECONDS = 120.0
# How many finished attempts stay addressable after they end.
FINISHED_ATTEMPTS = 20

_GENERIC_FAILURE = "The OpenCode bridge host failed the connection request."


@dataclass
class _Attempt:
    """One live connection attempt and the process that serves it."""

    attempt_id: str
    integration: str
    method: str | None = None
    label: str | None = None
    status: str = "starting"
    url: str = ""
    instructions: str = ""
    mode: str = ""
    message: str | None = None
    code_submitted: bool = False
    process: "subprocess.Popen[bytes] | None" = None
    stdin: IO[Any] | None = None
    condition: threading.Condition = field(default_factory=threading.Condition)


class OpenCodeConnections:
    """Start and observe provider sign-ins through the bridge host."""

    def __init__(
        self,
        *,
        command: Sequence[str] | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        """``command`` and ``timeout_seconds`` exist so tests can replace the host."""
        self._provider = OpenCodeChatProvider(
            command=command, timeout_seconds=timeout_seconds
        )
        self._lock = threading.Lock()
        self._attempts: dict[str, _Attempt] = {}

    def list_integrations(self) -> list[dict[str, Any]]:
        """List the integrations and their sign-in methods."""
        return self._provider.list_integrations()

    def start(
        self,
        integration: str,
        method: str | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        """Start one connection attempt and return it once the sign-in details exist."""
        attempt = _Attempt(
            attempt_id=uuid.uuid4().hex,
            integration=integration,
            method=method,
            label=label,
        )
        home = _create_private_home()
        try:
            process = _spawn(self._provider.host_command(), home)
        except ProviderError:
            shutil.rmtree(home, ignore_errors=True)
            raise
        attempt.process = process
        attempt.stdin = process.stdin
        with self._lock:
            self._prune_locked()
            self._attempts[attempt.attempt_id] = attempt
        request: dict[str, Any] = {
            "bridge_protocol": BRIDGE_PROTOCOL,
            "operation": "connect",
            "integration": integration,
        }
        if method:
            request["method"] = method
        if label:
            request["label"] = label
        payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
        try:
            stdin = process.stdin
            if stdin is None:
                raise ProviderError(FAILED_REQUEST)
            stdin.write(payload + b"\n")
            stdin.flush()
        except OSError as exc:
            self._fail(attempt, FAILED_REQUEST)
            shutil.rmtree(home, ignore_errors=True)
            self._discard(attempt.attempt_id)
            raise ProviderError(FAILED_REQUEST) from exc

        thread = threading.Thread(
            target=self._pump,
            args=(attempt, home, self._provider._timeout()),
            daemon=True,
        )
        thread.start()
        with attempt.condition:
            started = attempt.condition.wait_for(
                lambda: attempt.status != "starting",
                timeout=min(START_TIMEOUT_SECONDS, self._provider._timeout()),
            )
        if not started:
            message = "The provider connection did not start in time."
            self._fail(attempt, message)
            self._discard(attempt.attempt_id)
            raise ProviderError(message)
        if attempt.status == "failed":
            message = attempt.message or FAILED_REQUEST
            self._discard(attempt.attempt_id)
            raise ProviderError(message)
        return self._public(attempt)

    def status(self, attempt_id: str) -> dict[str, Any]:
        """Return one attempt; ``KeyError`` when it is not addressable."""
        return self._public(self._get(attempt_id))

    def submit_code(self, attempt_id: str, code: str) -> dict[str, Any]:
        """Send the human's code to a ``code``-mode attempt."""
        attempt = self._get(attempt_id)
        with attempt.condition:
            if attempt.status != "waiting":
                raise ValueError("The connection is not waiting for a code.")
            if attempt.mode != "code":
                raise ValueError("The connection does not accept a code.")
            if attempt.code_submitted:
                raise ValueError("A code was already submitted for this connection.")
            stdin = attempt.stdin
            if stdin is None:
                raise ValueError("The connection is not waiting for a code.")
            try:
                stdin.write(json.dumps({"code": code}).encode("utf-8") + b"\n")
                stdin.flush()
            except OSError as exc:
                raise ValueError("The connection is no longer accepting input.") from exc
            attempt.code_submitted = True
        return self._public(attempt)

    def cancel(self, attempt_id: str) -> dict[str, Any]:
        """Stop one attempt and release its process."""
        attempt = self._get(attempt_id)
        self._fail(attempt, "The provider connection was cancelled.")
        return self._public(attempt)

    def _get(self, attempt_id: str) -> _Attempt:
        with self._lock:
            attempt = self._attempts.get(attempt_id)
        if attempt is None:
            raise KeyError(attempt_id)
        return attempt

    def _discard(self, attempt_id: str) -> None:
        with self._lock:
            self._attempts.pop(attempt_id, None)

    def _pump(self, attempt: _Attempt, home: Any, timeout: float) -> None:
        """Read the host's records until the attempt ends; then clean up."""
        process = attempt.process
        assert process is not None
        try:
            lines: queue.Queue[bytes | None] = queue.Queue()
            threading.Thread(
                target=_pump_lines, args=(process.stdout, lines), daemon=True
            ).start()
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._fail(attempt, TIMEOUT_MESSAGE)
                    break
                try:
                    line = lines.get(timeout=remaining)
                except queue.Empty:
                    self._fail(attempt, TIMEOUT_MESSAGE)
                    break
                if line is None:
                    break
                try:
                    record = _decode(line)
                except ProviderError as exc:
                    self._fail(attempt, str(exc))
                    break
                try:
                    if self._handle(attempt, record):
                        break
                except ProviderError as exc:
                    self._fail(attempt, str(exc))
                    break
            self._settle(attempt)
        except Exception:
            self._fail(attempt, _GENERIC_FAILURE)
        finally:
            if process.poll() is None:
                _kill(process)
            else:
                try:
                    process.wait(timeout=EXIT_GRACE_SECONDS)
                except subprocess.TimeoutExpired:  # pragma: no cover - already exited
                    _kill(process)
            shutil.rmtree(home, ignore_errors=True)

    @staticmethod
    def _handle(attempt: _Attempt, record: dict[str, Any]) -> bool:
        """Apply one record; return True when the attempt is over."""
        kind = record["type"]
        if kind == "oauth":
            url = record.get("url")
            instructions = record.get("instructions")
            mode = record.get("mode")
            if (
                not isinstance(url, str)
                or not url.strip()
                or not isinstance(instructions, str)
                or mode not in {"auto", "code"}
            ):
                raise ProviderError(INVALID_RESPONSE)
            with attempt.condition:
                if attempt.status == "starting":
                    attempt.url = url
                    attempt.instructions = instructions
                    attempt.mode = mode
                    attempt.status = "waiting"
                    attempt.condition.notify_all()
            return False
        if kind == "result":
            method = record.get("method")
            if record.get("kind") != "connected" or not isinstance(method, str):
                raise ProviderError(INVALID_RESPONSE)
            with attempt.condition:
                attempt.method = method
                attempt.status = "complete"
                attempt.condition.notify_all()
            return True
        if kind == "error":
            message = record.get("message")
            if not isinstance(message, str) or not message.strip():
                raise ProviderError(INVALID_RESPONSE)
            with attempt.condition:
                _set_failed(attempt, message)
                attempt.condition.notify_all()
            return True
        # `hello` and `event` need no handling; unknown records are ignored
        # within the compatible window, as the contract says.
        return False

    def _settle(self, attempt: _Attempt) -> None:
        """A host that stopped before its verdict fails the attempt explicitly."""
        with attempt.condition:
            if attempt.status in {"starting", "waiting"}:
                _set_failed(attempt, CONNECT_STOP_MESSAGE)
                attempt.condition.notify_all()

    def _fail(self, attempt: _Attempt, message: str) -> None:
        with attempt.condition:
            _set_failed(attempt, message)
            attempt.condition.notify_all()
            process = attempt.process
            if process is not None and process.poll() is None:
                _kill(process)

    @staticmethod
    def _public(attempt: _Attempt) -> dict[str, Any]:
        with attempt.condition:
            return {
                "attempt_id": attempt.attempt_id,
                "integration": attempt.integration,
                "method": attempt.method,
                "status": attempt.status,
                "url": attempt.url,
                "instructions": attempt.instructions,
                "mode": attempt.mode,
                "message": attempt.message,
            }

    def _prune_locked(self) -> None:
        finished = [
            attempt_id
            for attempt_id, attempt in self._attempts.items()
            if attempt.status in {"complete", "failed"}
        ]
        for attempt_id in finished[: max(0, len(finished) - FINISHED_ATTEMPTS)]:
            self._attempts.pop(attempt_id, None)


def _set_failed(attempt: _Attempt, message: str) -> None:
    if attempt.status == "complete":
        return
    attempt.status = "failed"
    attempt.message = message
