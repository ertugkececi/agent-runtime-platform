"""The Python OpenCode adapter manages one host process per call.

The host package is replaced by ``tests/fixtures/fake_opencode_host.py``, so
these tests need no Bun and never reach a model service. The wire contract is
``docs/opencode-bridge-contract.md``.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from agent_runtime_platform.infrastructure.mcp_tools import validate_tool_ids
from agent_runtime_platform.infrastructure.providers._base import HandoffRequest, ProviderError
from agent_runtime_platform.infrastructure.providers.opencode.provider import (
    DATA_HOME_ENV,
    DEFAULT_TIMEOUT_SECONDS,
    MAXIMUM_TIMEOUT_SECONDS,
    TIMEOUT_ENV,
    OpenCodeChatProvider,
    list_opencode_integrations,
    list_opencode_models,
)

FAKE_HOST = Path(__file__).resolve().parent / "fixtures" / "fake_opencode_host.py"
HISTORY = [{"role": "user", "content": "Summarize the queue module."}]
MCP_CONFIG = {
    "fixture": {
        "command": sys.executable,
        "args": [str(FAKE_HOST)],
        "env_vars": ["FIXTURE_TOKEN"],
        "read_only_tools": ["lookup"],
    }
}


def _agent(**overrides) -> dict:
    agent = {
        "model_provider": "opencode",
        "model_name": "opencode/big-model",
        "instructions": "Answer in the user's language.",
        "tool_ids": [],
    }
    agent.update(overrides)
    return agent


def _provider(
    monkeypatch, scenario: str, *, timeout_seconds: float = 10, **env: str
) -> OpenCodeChatProvider:
    monkeypatch.setenv("FAKE_HOST_SCENARIO", scenario)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return OpenCodeChatProvider(
        command=[sys.executable, str(FAKE_HOST)], timeout_seconds=timeout_seconds
    )


def test_a_reply_round_trips_through_the_host(monkeypatch, tmp_path):
    capture = tmp_path / "request.json"
    provider = _provider(monkeypatch, "reply", FAKE_HOST_CAPTURE=str(capture))
    assert provider.generate(_agent(), HISTORY) == "Fake reply."
    request = json.loads(capture.read_text(encoding="utf-8"))
    assert request == {
        "bridge_protocol": 2,
        "model": "opencode/big-model",
        "instructions": "Answer in the user's language.",
        "history": HISTORY,
        "allow_handoff": False,
        "tool_ids": [],
    }


def test_reasoning_effort_is_named_as_a_model_variant(monkeypatch, tmp_path):
    capture = tmp_path / "request.json"
    provider = _provider(monkeypatch, "reply", FAKE_HOST_CAPTURE=str(capture))
    provider.generate(_agent(model_reasoning_effort="high"), HISTORY)
    assert json.loads(capture.read_text(encoding="utf-8"))["model"] == "opencode/big-model#high"


def test_an_explicit_model_variant_is_not_overwritten(monkeypatch, tmp_path):
    capture = tmp_path / "request.json"
    provider = _provider(monkeypatch, "reply", FAKE_HOST_CAPTURE=str(capture))
    provider.generate(
        _agent(model_name="opencode/big-model#low", model_reasoning_effort="high"), HISTORY
    )
    assert json.loads(capture.read_text(encoding="utf-8"))["model"] == "opencode/big-model#low"


def test_handoff_instructions_travel_when_allowed(monkeypatch, tmp_path):
    capture = tmp_path / "request.json"
    provider = _provider(monkeypatch, "reply", FAKE_HOST_CAPTURE=str(capture))
    provider.generate(
        _agent(remote_a2a_capabilities=["research", "research"]),
        HISTORY,
        allow_handoff=True,
    )
    request = json.loads(capture.read_text(encoding="utf-8"))
    assert request["allow_handoff"] is True
    assert request["remote_capabilities"] == ["research"]


def test_a_handoff_is_returned_when_allowed(monkeypatch):
    provider = _provider(monkeypatch, "handoff")
    output = provider.generate(_agent(), HISTORY, allow_handoff=True)
    assert output == HandoffRequest("research", "Find the SQLite schema for queue_jobs.")


def test_a_handoff_is_refused_when_handoff_is_disabled(monkeypatch):
    provider = _provider(monkeypatch, "handoff")
    with pytest.raises(ProviderError, match="handoff when handoff was disabled"):
        provider.generate(_agent(), HISTORY)


def test_tool_grants_travel_as_mcp_servers_without_values(monkeypatch, tmp_path):
    capture = tmp_path / "request.json"
    monkeypatch.setenv("AGENT_RUNTIME_MCP_SERVERS", json.dumps(MCP_CONFIG))
    monkeypatch.setenv("FIXTURE_TOKEN", "sk-test-secret-value")
    provider = _provider(monkeypatch, "reply", FAKE_HOST_CAPTURE=str(capture))
    assert provider.generate(_agent(tool_ids=["fixture/lookup"]), HISTORY) == "Fake reply."

    request = json.loads(capture.read_text(encoding="utf-8"))
    assert request["tool_ids"] == ["fixture/lookup"]
    assert request["mcp_servers"] == [
        {
            "name": "fixture",
            "command": sys.executable,
            "args": [str(FAKE_HOST)],
            "env_vars": ["FIXTURE_TOKEN"],
            "tools": ["lookup"],
        }
    ]
    assert "sk-test-secret-value" not in capture.read_text(encoding="utf-8")


def test_a_withdrawn_tool_grant_fails_the_turn(monkeypatch, tmp_path):
    marker = tmp_path / "started"
    provider = OpenCodeChatProvider(
        command=[sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"],
        timeout_seconds=10,
    )
    monkeypatch.setenv("AGENT_RUNTIME_MCP_SERVERS", "{}")
    with pytest.raises(ProviderError, match="no longer administrator-approved"):
        provider.generate(_agent(tool_ids=["fixture/lookup"]), HISTORY)
    assert not marker.exists()


def test_the_api_gate_allows_opencode_past_the_manifest_check():
    # create_agent/update_agent run this; the approval catalog decides next.
    with pytest.raises(ValueError, match="not administrator-approved"):
        validate_tool_ids(["unknown/tool"], "opencode")


def test_tool_events_reach_the_tool_event_callback(monkeypatch):
    events = []
    provider = _provider(monkeypatch, "events")
    output = provider.generate(_agent(tool_event_callback=events.append), HISTORY)
    assert output == "Done."
    assert events == [
        {"server": "files", "tool": "search", "status": "running", "phase": "running"}
    ]
    # A host that tried to log arguments or results cannot leak them.
    assert "sk-test-secret-value" not in json.dumps(events)


def test_unknown_record_types_are_ignored(monkeypatch):
    provider = _provider(monkeypatch, "future")
    assert provider.generate(_agent(), HISTORY) == "Done."


def test_the_child_runs_in_a_private_home_and_a_persistent_data_root(monkeypatch, tmp_path):
    user_home = tmp_path / "user-home"
    user_config = user_home / ".config" / "opencode" / "opencode.json"
    user_config.parent.mkdir(parents=True)
    user_config.write_text('{"model": "user-choice"}', encoding="utf-8")
    modified_at = user_config.stat().st_mtime_ns
    data_root = tmp_path / "persistent-opencode"

    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(user_home / ".config"))
    monkeypatch.setenv(DATA_HOME_ENV, str(data_root))
    capture = tmp_path / "environment.json"
    provider = _provider(monkeypatch, "reply", FAKE_HOST_ENV_CAPTURE=str(capture))
    provider.generate(_agent(), HISTORY)

    report = json.loads(capture.read_text(encoding="utf-8"))
    home = Path(report["HOME"])
    assert home != user_home
    # Config, cache and state are per call; only data is app-owned and persistent.
    for name in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        assert Path(report[name]).parent == home
    assert Path(report["XDG_DATA_HOME"]) == data_root / "data"
    assert Path(report["XDG_DATA_HOME"]).is_dir()
    if os.name == "posix":
        assert (data_root / "data").stat().st_mode & 0o777 == 0o700
    # The user's configuration is neither read nor modified.
    assert user_config.read_text(encoding="utf-8") == '{"model": "user-choice"}'
    assert user_config.stat().st_mtime_ns == modified_at
    # The per-call home does not outlive the call; the data root does.
    assert not home.exists()
    assert data_root.exists()
    if os.name == "posix":
        assert report["home_mode"] == "0o700"
        assert report["probe_mode"] == "0o600"


def test_the_data_root_defaults_to_the_app_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(DATA_HOME_ENV, raising=False)
    capture = tmp_path / "environment.json"
    provider = _provider(monkeypatch, "reply", FAKE_HOST_ENV_CAPTURE=str(capture))
    provider.generate(_agent(), HISTORY)
    report = json.loads(capture.read_text(encoding="utf-8"))
    assert Path(report["XDG_DATA_HOME"]) == (
        tmp_path / ".agent-runtime-platform" / "opencode-home" / "data"
    )


def test_environment_secrets_never_reach_the_request(monkeypatch, tmp_path):
    capture = tmp_path / "request.json"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-secret-value")
    provider = _provider(monkeypatch, "reply", FAKE_HOST_CAPTURE=str(capture))
    provider.generate(_agent(), HISTORY)
    assert "sk-test-secret-value" not in capture.read_text(encoding="utf-8")


def test_a_host_error_record_becomes_a_provider_error(monkeypatch):
    provider = _provider(monkeypatch, "error")
    with pytest.raises(ProviderError, match="The fake host failed the model request."):
        provider.generate(_agent(), HISTORY)


def test_the_model_catalog_round_trips_through_the_host(monkeypatch, tmp_path):
    capture = tmp_path / "request.json"
    provider = _provider(monkeypatch, "models", FAKE_HOST_CAPTURE=str(capture))
    assert provider.list_models() == [
        {
            "id": "opencode/big-model",
            "label": "Big Model",
            "is_default": True,
            "default_effort": "",
            "efforts": ["low", "high"],
        },
        {
            "id": "opencode/plain-model",
            "label": "Plain Model",
            "is_default": False,
            "default_effort": "",
            "efforts": [],
        },
    ]
    assert json.loads(capture.read_text(encoding="utf-8")) == {
        "bridge_protocol": 2,
        "operation": "models",
    }


def test_the_integration_list_round_trips_through_the_host(monkeypatch, tmp_path):
    capture = tmp_path / "request.json"
    provider = _provider(monkeypatch, "integrations", FAKE_HOST_CAPTURE=str(capture))
    assert provider.list_integrations() == [
        {
            "id": "openai",
            "name": "OpenAI",
            "connected": False,
            "methods": [
                {
                    "id": "chatgpt-headless",
                    "type": "oauth",
                    "label": "ChatGPT Pro/Plus (headless)",
                }
            ],
        }
    ]
    assert json.loads(capture.read_text(encoding="utf-8")) == {
        "bridge_protocol": 2,
        "operation": "integrations",
    }


def test_the_module_functions_use_the_default_provider(monkeypatch):
    monkeypatch.setenv("FAKE_HOST_SCENARIO", "models")
    monkeypatch.setattr(
        OpenCodeChatProvider,
        "_default_command",
        staticmethod(lambda: [sys.executable, str(FAKE_HOST)]),
    )
    assert list_opencode_models()[0]["id"] == "opencode/big-model"
    monkeypatch.setenv("FAKE_HOST_SCENARIO", "integrations")
    assert list_opencode_integrations()[0]["id"] == "openai"


def test_an_incomplete_catalog_record_is_refused(monkeypatch):
    with pytest.raises(ProviderError, match="invalid response"):
        _provider(monkeypatch, "invalid_models").list_models()


def test_an_incomplete_integration_record_is_refused(monkeypatch):
    provider = _provider(monkeypatch, "models")
    with pytest.raises(ProviderError, match="invalid response"):
        provider.list_integrations()


def test_a_host_error_record_fails_the_catalog(monkeypatch):
    provider = _provider(monkeypatch, "error")
    with pytest.raises(ProviderError, match="The fake host failed the model request."):
        provider.list_models()


def test_a_catalog_without_hello_is_refused(monkeypatch):
    provider = _provider(monkeypatch, "no_hello")
    with pytest.raises(ProviderError, match="incompatible bridge protocol"):
        provider.list_models()


def test_a_host_that_skips_hello_is_refused(monkeypatch):
    provider = _provider(monkeypatch, "no_hello")
    with pytest.raises(ProviderError, match="incompatible bridge protocol"):
        provider.generate(_agent(), HISTORY)


def test_a_host_with_another_protocol_version_is_refused(monkeypatch):
    provider = _provider(monkeypatch, "bad_version", FAKE_HOST_PROTOCOL="1")
    with pytest.raises(ProviderError, match="incompatible bridge protocol"):
        provider.generate(_agent(), HISTORY)


def test_a_startup_failure_is_reported_verbatim(monkeypatch):
    provider = _provider(monkeypatch, "startup_error")
    with pytest.raises(ProviderError, match="failed to start"):
        provider.generate(_agent(), HISTORY)


def test_a_crash_without_a_result_fails_the_turn(monkeypatch):
    provider = _provider(monkeypatch, "crash")
    with pytest.raises(ProviderError, match="failed the model request"):
        provider.generate(_agent(), HISTORY)


def test_a_silent_host_fails_the_turn(monkeypatch):
    provider = _provider(monkeypatch, "silent")
    with pytest.raises(ProviderError, match="incompatible bridge protocol"):
        provider.generate(_agent(), HISTORY)


def test_an_invalid_stdout_line_fails_the_turn(monkeypatch):
    provider = _provider(monkeypatch, "garbage")
    with pytest.raises(ProviderError, match="invalid response"):
        provider.generate(_agent(), HISTORY)


def test_a_second_result_is_refused(monkeypatch):
    provider = _provider(monkeypatch, "two_results")
    with pytest.raises(ProviderError, match="invalid response"):
        provider.generate(_agent(), HISTORY)


def test_a_record_after_the_result_is_refused(monkeypatch):
    provider = _provider(monkeypatch, "result_then_error")
    with pytest.raises(ProviderError, match="invalid response"):
        provider.generate(_agent(), HISTORY)


def test_a_timeout_kills_the_host_process(monkeypatch, tmp_path):
    pid_file = tmp_path / "host.pid"
    provider = _provider(
        monkeypatch, "hang", timeout_seconds=0.5, FAKE_HOST_PID=str(pid_file)
    )
    started = time.monotonic()
    with pytest.raises(ProviderError, match="timed out"):
        provider.generate(_agent(), HISTORY)
    assert time.monotonic() - started < 10
    pid = int(pid_file.read_text(encoding="utf-8"))
    if os.name == "posix":
        deadline = time.monotonic() + 5
        while _pid_running(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _pid_running(pid), "the timed out host was left running"


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_a_missing_bun_is_reported(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    provider = OpenCodeChatProvider()
    with pytest.raises(ProviderError, match="'bun' on PATH"):
        provider.generate(_agent(), HISTORY)


def test_the_timeout_defaults_to_provider_configuration(monkeypatch):
    monkeypatch.delenv(TIMEOUT_ENV, raising=False)
    assert OpenCodeChatProvider(command=["unused"])._timeout() == DEFAULT_TIMEOUT_SECONDS


def test_the_timeout_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv(TIMEOUT_ENV, "120")
    assert OpenCodeChatProvider(command=["unused"])._timeout() == 120.0


def test_the_timeout_is_capped_at_the_provider_maximum(monkeypatch):
    monkeypatch.setenv(TIMEOUT_ENV, str(MAXIMUM_TIMEOUT_SECONDS * 10))
    assert OpenCodeChatProvider(command=["unused"])._timeout() == MAXIMUM_TIMEOUT_SECONDS


@pytest.mark.parametrize("value", ["soon", "0", "-1", "nan"])
def test_an_invalid_timeout_is_refused(monkeypatch, value):
    monkeypatch.setenv(TIMEOUT_ENV, value)
    with pytest.raises(ProviderError, match=TIMEOUT_ENV):
        OpenCodeChatProvider(command=["unused"])._timeout()
