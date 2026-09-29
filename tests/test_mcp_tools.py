from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_runtime_platform.api.app import create_app
from agent_runtime_platform.infrastructure.mcp_tools import bridge_mcp_servers


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


def test_agent_tool_ids_are_validated_and_the_bridge_config_is_narrow(mcp_client):
    allowed = mcp_client.post(
        "/agents",
        json={"name": "Reader", "instructions": "Read carefully.", "model_name": "opencode/big-model", "tool_ids": ["fixture/lookup"]},
    )
    assert allowed.status_code == 201, allowed.text
    assert allowed.json()["tool_ids"] == ["fixture/lookup"]

    for bad_id in ("fixture/hinted_but_untrusted", "unknown/tool", "https://example.com/tool"):
        rejected = mcp_client.post(
            "/agents",
            json={"name": "Bad", "instructions": "No.", "model_name": "opencode/big-model", "tool_ids": [bad_id]},
        )
        assert rejected.status_code == 422

    arbitrary_server = mcp_client.post(
        "/agents",
        json={
            "name": "Invalid config",
            "instructions": "No.",
            "model_name": "opencode/big-model",
            "mcp_servers": {"unsafe": {"command": "sh"}},
        },
    )
    assert arbitrary_server.status_code == 422

    unknown_provider = mcp_client.post(
        "/agents",
        json={"name": "Unknown", "instructions": "No.", "model_provider": "made_up", "model_name": "x", "tool_ids": ["fixture/lookup"]},
    )
    assert unknown_provider.status_code == 422

    servers = bridge_mcp_servers(["fixture/lookup"])
    assert servers == [
        {
            "name": "fixture",
            "command": sys.executable,
            "args": [str(FIXTURE_SERVER)],
            "env_vars": [],
            "tools": ["lookup"],
        }
    ]
    # Names, never values: the environment carries what the host needs.
    assert "must-not-reach-server" not in json.dumps(servers)


def test_bridge_mcp_servers_groups_and_refuses_withdrawn_tools(monkeypatch):
    config = {
        "fixture": {
            "command": "run-fixture",
            "args": ["--stdio"],
            "cwd": "/srv/fixture",
            "env_vars": ["FIXTURE_TOKEN"],
            "read_only_tools": ["lookup", "search"],
        }
    }
    monkeypatch.setenv("AGENT_RUNTIME_MCP_SERVERS", json.dumps(config))
    assert bridge_mcp_servers(["fixture/lookup", "fixture/search"]) == [
        {
            "name": "fixture",
            "command": "run-fixture",
            "args": ["--stdio"],
            "cwd": "/srv/fixture",
            "env_vars": ["FIXTURE_TOKEN"],
            "tools": ["lookup", "search"],
        }
    ]
    with pytest.raises(ValueError, match="no longer administrator-approved"):
        bridge_mcp_servers(["fixture/delete"])


def test_default_agent_has_no_tools_and_updates_revoke_grants(mcp_client):
    agent = mcp_client.post(
        "/agents", json={"name": "Default", "instructions": "No tools.", "model_name": "opencode/big-model"}
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
