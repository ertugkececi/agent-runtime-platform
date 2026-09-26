from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_runtime_platform.api import create_app
from agent_runtime_platform.codex_home import prepare_codex_home
from agent_runtime_platform.mcp_tools import codex_mcp_config


FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "readonly_mcp_server.py"


@pytest.fixture
def mcp_client(monkeypatch):
    config = {
        "fixture": {
            "command": sys.executable,
            "args": [str(FIXTURE_SERVER)],
            "read_only_tools": ["lookup"],
        }
    }
    monkeypatch.setenv("AGENT_RUNTIME_MCP_SERVERS", json.dumps(config))
    monkeypatch.setenv("MCP_TEST_SECRET", "must-not-reach-server")
    with TestClient(create_app("sqlite:///:memory:")) as client:
        yield client


def test_catalog_discovers_tools_and_does_not_trust_read_only_hint_alone(mcp_client):
    response = mcp_client.get("/mcp/tools")
    assert response.status_code == 200
    tools = {tool["name"]: tool for tool in response.json()}
    assert tools["lookup"]["id"] == "fixture/lookup"
    assert tools["lookup"]["trusted_read_only"] is True
    assert tools["lookup"]["read_only_hint"] is True
    assert tools["hinted_but_untrusted"]["read_only_hint"] is True
    assert tools["hinted_but_untrusted"]["trusted_read_only"] is False
    assert "test parent env passed: False" in tools["hinted_but_untrusted"]["description"]


def test_agent_tool_ids_are_validated_and_codex_config_is_narrow(mcp_client):
    allowed = mcp_client.post(
        "/agents",
        json={"name": "Reader", "instructions": "Read carefully.", "model_name": "gpt-6-sol", "tool_ids": ["fixture/lookup"]},
    )
    assert allowed.status_code == 201, allowed.text
    assert allowed.json()["tool_ids"] == ["fixture/lookup"]

    for bad_id in ("fixture/hinted_but_untrusted", "unknown/tool", "https://example.com/tool"):
        rejected = mcp_client.post(
            "/agents",
            json={"name": "Bad", "instructions": "No.", "model_name": "gpt-6-sol", "tool_ids": [bad_id]},
        )
        assert rejected.status_code == 422

    arbitrary_server = mcp_client.post(
        "/agents",
        json={
            "name": "Invalid config",
            "instructions": "No.",
            "model_name": "gpt-6-sol",
            "mcp_servers": {"unsafe": {"command": "sh"}},
        },
    )
    assert arbitrary_server.status_code == 422

    openai = mcp_client.post(
        "/agents",
        json={"name": "OpenAI", "instructions": "No.", "model_provider": "openai", "model_name": "gpt-6-sol", "tool_ids": ["fixture/lookup"]},
    )
    assert openai.status_code == 422

    config = codex_mcp_config(["fixture/lookup"])
    assert config["fixture"]["enabled_tools"] == ["lookup"]
    assert config["fixture"]["default_tools_approval_mode"] == "approve"


def test_default_agent_has_no_tools_and_updates_revoke_grants(mcp_client):
    agent = mcp_client.post(
        "/agents", json={"name": "Default", "instructions": "No tools.", "model_name": "gpt-6-sol"}
    ).json()
    assert agent["tool_ids"] == []
    updated = mcp_client.patch(f"/agents/{agent['id']}", json={"tool_ids": ["fixture/lookup"]})
    assert updated.status_code == 200
    assert updated.json()["version"] == agent["version"] + 1
    revoked = mcp_client.patch(f"/agents/{agent['id']}", json={"tool_ids": []})
    assert revoked.status_code == 200
    assert revoked.json()["tool_ids"] == []


def test_disable_and_noop_patch_work_when_mcp_catalog_is_unavailable(mcp_client, monkeypatch):
    agent = mcp_client.post(
        "/agents", json={"name": "Disable me", "instructions": "Read.", "model_name": "test", "tool_ids": ["fixture/lookup"]}
    ).json()
    monkeypatch.setenv("AGENT_RUNTIME_MCP_SERVERS", "{}")
    disabled = mcp_client.patch(f"/agents/{agent['id']}", json={"enabled": False})
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["enabled"] is False
    version = disabled.json()["version"]
    noop = mcp_client.patch(f"/agents/{agent['id']}", json={})
    assert noop.status_code == 200
    assert noop.json()["version"] == version


def test_codex_home_is_persistent_private_and_does_not_inherit_global_config(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    auth = source / "auth.json"
    auth.write_text('{"token":"private"}')
    (source / "config.toml").write_text('[mcp_servers.unwanted]\nenabled = true\n')
    app_home = tmp_path / "app" / "codex-home"

    result = prepare_codex_home(app_home=app_home, source_home=source)
    assert result == app_home
    assert (app_home / "auth.json").read_text() == auth.read_text()
    assert not (app_home / "config.toml").exists()
    assert os.stat(app_home).st_mode & 0o777 == 0o700
    assert os.stat(app_home / "auth.json").st_mode & 0o777 == 0o600

    (app_home / "auth.json").write_text('{"token":"refreshed"}')
    newer = auth.stat().st_mtime_ns + 10**9
    os.utime(app_home / "auth.json", ns=(newer, newer))
    auth.write_text('{"token":"private"}')
    assert prepare_codex_home(app_home=app_home, source_home=source)
    assert (app_home / "auth.json").read_text() == '{"token":"refreshed"}'


def test_codex_mcp_items_emit_metadata_only_tool_events(monkeypatch, tmp_path):
    import openai_codex
    from types import SimpleNamespace
    from openai_codex.generated.v2_all import (
        AgentMessageThreadItem,
        ItemCompletedNotification,
        McpToolCallResult,
        McpToolCallStatus,
        McpToolCallThreadItem,
        MessagePhase,
        ThreadItem,
        Turn,
        TurnCompletedNotification,
        TurnStatus,
    )
    from openai_codex.models import Notification
    from agent_runtime_platform.providers import CodexChatProvider

    fixture = Path(__file__).parent / "fixtures" / "readonly_mcp_server.py"
    monkeypatch.setenv(
        "AGENT_RUNTIME_MCP_SERVERS",
        json.dumps({
            "fixture": {
                "command": sys.executable,
                "args": [str(fixture)],
                "read_only_tools": ["lookup"],
            }
        }),
    )
    monkeypatch.setattr("agent_runtime_platform.codex_home.prepare_codex_home", lambda: tmp_path)
    callbacks = []
    setup = {}

    class FakeCodex:
        def __init__(self, config):
            setup["config"] = config

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def account(self):
            return SimpleNamespace(account=SimpleNamespace(root=SimpleNamespace(type="chatgpt")))

        def thread_start(self, **options):
            setup["thread"] = options
            return self

        def turn(self, _prompt, **_options):
            tool = ThreadItem(root=McpToolCallThreadItem(
                id="tool-1",
                type="mcpToolCall",
                server="fixture",
                tool="lookup",
                status=McpToolCallStatus.completed,
                arguments={"secret_argument": "private"},
                result=McpToolCallResult(content=[{"type": "text", "text": "private result"}]),
            ))
            answer = ThreadItem(root=AgentMessageThreadItem(
                id="answer-1", type="agentMessage", phase=MessagePhase.final_answer, text="Done."
            ))
            tool_event = Notification("item/completed", ItemCompletedNotification(
                completedAtMs=1, item=tool, threadId="thread-1", turnId="turn-1"
            ))
            answer_event = Notification("item/completed", ItemCompletedNotification(
                completedAtMs=2, item=answer, threadId="thread-1", turnId="turn-1"
            ))
            turn_event = Notification("turn/completed", TurnCompletedNotification(
                threadId="thread-1",
                turn=Turn(id="turn-1", status=TurnStatus.completed, items=[tool, answer]),
            ))
            return SimpleNamespace(id="turn-1", stream=lambda: iter([tool_event, answer_event, turn_event]))

    monkeypatch.setattr(openai_codex, "Codex", FakeCodex)
    result = CodexChatProvider().generate(
        {
            "id": "agent",
            "instructions": "Answer.",
            "model_provider": "codex",
            "model_name": "gpt-6-luna",
            "tool_ids": ["fixture/lookup"],
            "tool_event_callback": callbacks.append,
        },
        [{"role": "user", "content": "Read."}],
    )

    assert result == "Done."
    assert callbacks == [{"server": "fixture", "tool": "lookup", "status": "completed"}]
    assert "private" not in json.dumps(callbacks)
    assert "mcp_servers.fixture.enabled_tools=[\"lookup\"]" in setup["config"].config_overrides
    assert "mcp_servers" not in setup["thread"]["config"]


def test_codex_home_concurrent_initialization_uses_distinct_atomic_temps(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    source = tmp_path / "source-concurrent"
    source.mkdir()
    (source / "auth.json").write_text('{"access_token":"safe"}')
    home = tmp_path / "persistent-home"
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _index: prepare_codex_home(app_home=home, source_home=source), range(16)))
    assert all(result == home for result in results)
    assert (home / "auth.json").read_text() == '{"access_token":"safe"}'
    assert not list(home.glob(".auth-*.tmp"))
