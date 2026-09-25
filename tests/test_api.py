from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from agent_runtime_platform.api import create_app
from agent_runtime_platform.models import Run
from agent_runtime_platform.providers import ProviderError, ProviderRegistry


class FakeProvider:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = False

    def generate(self, agent: dict[str, Any], history: list[dict[str, str]]) -> str:
        if self.fail:
            raise ProviderError("simulated provider failure")
        self.calls.append({"agent": agent, "history": history})
        return f"Reply from {agent['name']}"


@pytest.fixture
def client_and_provider():
    provider = FakeProvider()
    app = create_app(
        database_url="sqlite:///:memory:",
        providers=ProviderRegistry({"openai": provider}),
    )
    with TestClient(app) as client:
        yield client, provider
    app.state.database.dispose()


def create_agent(
    client: TestClient,
    name: str,
    model_name: str,
    capabilities: list[str] | None = None,
) -> dict[str, Any]:
    response = client.post(
        "/agents",
        json={
            "name": name,
            "description": f"{name} test agent",
            "instructions": f"You are {name}.",
            "model_provider": "openai",
            "model_name": model_name,
            "capabilities": capabilities or [],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def count_runs(client: TestClient) -> int:
    with client.app.state.database.session() as session:
        return session.scalar(select(func.count()).select_from(Run)) or 0


def create_conversation(client: TestClient, first_id: str, second_id: str) -> str:
    response = client.post("/conversations", json={"agent_ids": [first_id, second_id]})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_two_registered_agents_exchange_a_persisted_message_and_trace(client_and_provider):
    client, provider = client_and_provider
    sender = create_agent(client, "Analyst", "model-a")
    recipient = create_agent(client, "Architect", "model-b")
    conversation_id = create_conversation(client, sender["id"], recipient["id"])

    response = client.post(
        f"/conversations/{conversation_id}/messages",
        json={
            "sender_agent_id": sender["id"],
            "recipient_agent_id": recipient["id"],
            "content": "Review the proposed API boundary.",
        },
    )

    assert response.status_code == 201, response.text
    run = response.json()
    assert run["status"] == "completed"
    assert run["messages"][0]["kind"] == "agent_message"
    assert run["messages"][1]["kind"] == "agent_response"
    assert run["messages"][1]["content"] == "Reply from Architect"
    assert run["agent_snapshots"]["target"]["model_name"] == "model-b"
    assert [event["sequence"] for event in run["events"]] == list(range(1, 8))
    assert [event["type"] for event in run["events"]] == [
        "run_started",
        "message_sent",
        "agent_invocation_started",
        "model_call_started",
        "model_call_completed",
        "agent_response_saved",
        "run_completed",
    ]
    assert provider.calls[0]["agent"]["id"] == recipient["id"]
    assert provider.calls[0]["history"] == [
        {"role": "user", "content": "Review the proposed API boundary."}
    ]

    conversation = client.get(f"/conversations/{conversation_id}").json()
    assert [message["sequence"] for message in conversation["messages"]] == [1, 2]
    assert conversation["messages"][1]["content"] == "Reply from Architect"
    assert client.get(f"/runs/{run['id']}").json()["status"] == "completed"


def test_run_keeps_the_agent_configuration_snapshot_after_agent_is_updated(client_and_provider):
    client, _provider = client_and_provider
    sender = create_agent(client, "Analyst", "model-a")
    recipient = create_agent(client, "Architect", "model-b-v1", capabilities=[" Backend "])
    conversation_id = create_conversation(client, sender["id"], recipient["id"])
    run_response = client.post(
        f"/conversations/{conversation_id}/messages",
        json={
            "sender_agent_id": sender["id"],
            "recipient_agent_id": recipient["id"],
            "content": "Check the interface.",
        },
    )
    assert run_response.status_code == 201, run_response.text
    run_id = run_response.json()["id"]

    update = client.patch(
        f"/agents/{recipient['id']}",
        json={
            "model_name": "model-b-v2",
            "instructions": "Use the new instructions.",
            "capabilities": [" API ", "BACKEND", "api"],
        },
    )

    assert update.status_code == 200, update.text
    assert update.json()["version"] == 2
    stored_run = client.get(f"/runs/{run_id}").json()
    assert stored_run["agent_snapshots"]["target"]["version"] == 1
    assert stored_run["agent_snapshots"]["target"]["model_name"] == "model-b-v1"
    assert stored_run["agent_snapshots"]["target"]["capabilities"] == ["backend"]
    assert update.json()["capabilities"] == ["api", "backend"]


def test_capability_search_is_normalized_and_excludes_disabled_agents(client_and_provider):
    client, _provider = client_and_provider
    active = create_agent(client, "Active Backend", "model-a", capabilities=[" Backend "])
    disabled = create_agent(client, "Disabled Backend", "model-b", capabilities=["backend"])
    assert client.patch(f"/agents/{disabled['id']}", json={"enabled": False}).status_code == 200

    response = client.get("/agents", params={"capability": " BACKEND "})

    assert response.status_code == 200
    assert [agent["id"] for agent in response.json()] == [active["id"]]
    assert response.json()[0]["capabilities"] == ["backend"]


def test_capability_message_resolves_a_unique_agent_and_adds_it_to_conversation(client_and_provider):
    client, provider = client_and_provider
    sender = create_agent(client, "Analyst", "model-a")
    initial_member = create_agent(client, "Observer", "model-b", capabilities=["observability"])
    recipient = create_agent(client, "Architect", "model-c", capabilities=["backend", "api"])
    conversation_id = create_conversation(client, sender["id"], initial_member["id"])

    response = client.post(
        f"/conversations/{conversation_id}/messages",
        json={
            "sender_agent_id": sender["id"],
            "recipient_capability": " BACKEND ",
            "content": "Review the service boundary.",
        },
    )

    assert response.status_code == 201, response.text
    run = response.json()
    assert run["target_agent_id"] == recipient["id"]
    assert run["agent_snapshots"]["target"]["capabilities"] == ["api", "backend"]
    assert provider.calls[-1]["agent"]["id"] == recipient["id"]
    assert recipient["id"] in client.get(f"/conversations/{conversation_id}").json()["agent_ids"]


def test_capability_message_with_no_enabled_match_creates_no_run_or_message(client_and_provider):
    client, _provider = client_and_provider
    sender = create_agent(client, "Analyst", "model-a")
    initial_member = create_agent(client, "Observer", "model-b")
    disabled = create_agent(client, "Disabled Backend", "model-c", capabilities=["backend"])
    conversation_id = create_conversation(client, sender["id"], initial_member["id"])
    assert client.patch(f"/agents/{disabled['id']}", json={"enabled": False}).status_code == 200

    response = client.post(
        f"/conversations/{conversation_id}/messages",
        json={
            "sender_agent_id": sender["id"],
            "recipient_capability": "backend",
            "content": "Find a backend agent.",
        },
    )

    assert response.status_code == 404
    assert client.get(f"/conversations/{conversation_id}").json()["messages"] == []
    assert count_runs(client) == 0


def test_capability_message_rejects_ambiguous_matches_without_creating_a_run(client_and_provider):
    client, _provider = client_and_provider
    sender = create_agent(client, "Analyst", "model-a")
    initial_member = create_agent(client, "Observer", "model-b")
    create_agent(client, "Architect A", "model-c", capabilities=["backend"])
    create_agent(client, "Architect B", "model-d", capabilities=["backend"])
    conversation_id = create_conversation(client, sender["id"], initial_member["id"])

    response = client.post(
        f"/conversations/{conversation_id}/messages",
        json={
            "sender_agent_id": sender["id"],
            "recipient_capability": "backend",
            "content": "Find a backend agent.",
        },
    )

    assert response.status_code == 409
    assert client.get(f"/conversations/{conversation_id}").json()["messages"] == []
    assert count_runs(client) == 0


def test_disabled_agents_are_rejected_before_a_run_is_created(client_and_provider):
    client, _provider = client_and_provider
    sender = create_agent(client, "Analyst", "model-a")
    recipient = create_agent(client, "Architect", "model-b")
    conversation_id = create_conversation(client, sender["id"], recipient["id"])

    disabled = client.patch(f"/agents/{recipient['id']}", json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["version"] == 2

    response = client.post(
        f"/conversations/{conversation_id}/messages",
        json={
            "sender_agent_id": sender["id"],
            "recipient_agent_id": recipient["id"],
            "content": "This should not run.",
        },
    )
    assert response.status_code == 409
    assert client.get(f"/conversations/{conversation_id}").json()["messages"] == []


def test_provider_errors_leave_a_failed_run_and_trace(client_and_provider):
    client, provider = client_and_provider
    sender = create_agent(client, "Analyst", "model-a")
    recipient = create_agent(client, "Architect", "model-b")
    conversation_id = create_conversation(client, sender["id"], recipient["id"])
    provider.fail = True

    response = client.post(
        f"/conversations/{conversation_id}/messages",
        json={
            "sender_agent_id": sender["id"],
            "recipient_agent_id": recipient["id"],
            "content": "Run this through the provider.",
        },
    )

    assert response.status_code == 502
    run_id = response.json()["detail"]["run_id"]
    run = client.get(f"/runs/{run_id}").json()
    assert run["status"] == "failed"
    assert run["error_code"] == "provider_error"
    assert run["messages"][0]["kind"] == "agent_message"
    assert [event["type"] for event in run["events"]][-1] == "run_failed"
