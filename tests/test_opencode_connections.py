"""The in-process connection service drives one host process per sign-in.

The host is the same fake as the provider tests: no Bun, no network, and a
scenario per case. The service owns a live subprocess, so these tests always
cancel or wait out their attempts.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from agent_runtime_platform.infrastructure.providers._base import ProviderError
from agent_runtime_platform.infrastructure.providers.opencode.connections import OpenCodeConnections

FAKE_HOST = Path(__file__).resolve().parent / "fixtures" / "fake_opencode_host.py"


def _service(monkeypatch, scenario: str, *, timeout_seconds: float = 10, **env: str) -> OpenCodeConnections:
    monkeypatch.setenv("FAKE_HOST_SCENARIO", scenario)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return OpenCodeConnections(
        command=[sys.executable, str(FAKE_HOST)], timeout_seconds=timeout_seconds
    )


def _wait_for_status(service, attempt_id: str, expected: str, timeout: float = 10) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = service.status(attempt_id)
        if current["status"] == expected:
            return current
        time.sleep(0.05)
    raise AssertionError(f"attempt never reached {expected}: {service.status(attempt_id)}")


def test_an_auto_attempt_reaches_complete(monkeypatch):
    service = _service(monkeypatch, "connect_auto")
    attempt = service.start("openai")
    assert attempt["status"] in {"waiting", "complete"}
    assert attempt["integration"] == "openai"
    assert attempt["url"] == "https://auth.example.test/codex/device"
    assert attempt["instructions"] == "Enter code: ABCD-EFGH"
    assert attempt["mode"] == "auto"
    assert attempt["message"] is None

    done = _wait_for_status(service, attempt["attempt_id"], "complete")
    assert done["method"] == "chatgpt-headless"


def test_a_code_attempt_accepts_the_code_once(monkeypatch, tmp_path):
    capture = tmp_path / "code.txt"
    service = _service(monkeypatch, "connect_code", FAKE_HOST_CODE_CAPTURE=str(capture))
    attempt = service.start("openai")
    assert attempt["mode"] == "code"
    assert attempt["status"] == "waiting"

    service.submit_code(attempt["attempt_id"], "ABCD-EFGH")
    done = _wait_for_status(service, attempt["attempt_id"], "complete")
    assert done["method"] == "chatgpt-headless"
    assert capture.read_text(encoding="utf-8") == "ABCD-EFGH"

    with pytest.raises(ValueError, match="not waiting for a code"):
        service.submit_code(attempt["attempt_id"], "AGAIN")


def test_a_code_is_refused_for_an_auto_attempt(monkeypatch):
    service = _service(monkeypatch, "connect_hang", timeout_seconds=60)
    attempt = service.start("openai")
    assert attempt["status"] == "waiting"
    with pytest.raises(ValueError, match="does not accept a code"):
        service.submit_code(attempt["attempt_id"], "ABCD")
    service.cancel(attempt["attempt_id"])


def test_a_host_error_fails_the_start(monkeypatch):
    service = _service(monkeypatch, "connect_error")
    with pytest.raises(ProviderError, match="The provider connection failed."):
        service.start("openai")


def test_a_host_that_stops_before_completion_fails_the_attempt(monkeypatch):
    service = _service(monkeypatch, "connect_stop")
    attempt = service.start("openai")
    failed = _wait_for_status(service, attempt["attempt_id"], "failed")
    assert "stopped before the connection completed" in (failed["message"] or "")


def test_a_malformed_oauth_record_is_refused(monkeypatch):
    service = _service(monkeypatch, "connect_bad_oauth")
    with pytest.raises(ProviderError, match="invalid response"):
        service.start("openai")


def test_a_silent_attempt_times_out(monkeypatch):
    service = _service(monkeypatch, "connect_hang", timeout_seconds=0.5)
    attempt = service.start("openai")
    failed = _wait_for_status(service, attempt["attempt_id"], "failed")
    assert "timed out" in (failed["message"] or "")


def test_cancel_stops_the_attempt(monkeypatch):
    service = _service(monkeypatch, "connect_hang", timeout_seconds=60)
    attempt = service.start("openai")
    cancelled = service.cancel(attempt["attempt_id"])
    assert cancelled["status"] == "failed"
    assert cancelled["message"] == "The provider connection was cancelled."


def test_an_unknown_attempt_is_a_key_error(monkeypatch):
    service = _service(monkeypatch, "connect_auto")
    with pytest.raises(KeyError):
        service.status("missing")
    with pytest.raises(KeyError):
        service.submit_code("missing", "ABCD")
    with pytest.raises(KeyError):
        service.cancel("missing")
