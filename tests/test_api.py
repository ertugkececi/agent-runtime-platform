from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from agent_runtime_platform.api.app import create_app
from agent_runtime_platform.domain.models import HumanChatRun, Run, Task
from agent_runtime_platform.infrastructure.providers import (
    HandoffRequest,
    ProviderError,
    ProviderRegistry,
)


class FakeProvider:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.outputs: list[str | HandoffRequest] = []
        self.fail_for_agents: set[str] = set()
        self.fail = False

    def generate(
        self,
        agent: dict[str, Any],
        history: list[dict[str, str]],
        *,
        allow_handoff: bool = False,
    ) -> str | HandoffRequest:
        self.calls.append({"agent": agent, "history": history, "allow_handoff": allow_handoff})
        if self.fail or agent["id"] in self.fail_for_agents:
            raise ProviderError("simulated provider failure")
        if self.outputs:
            return self.outputs.pop(0)
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


def count_human_chat_runs(client: TestClient) -> int:
    with client.app.state.database.session() as session:
        return session.scalar(select(func.count()).select_from(HumanChatRun)) or 0


def count_tasks(client: TestClient) -> int:
    with client.app.state.database.session() as session:
        return session.scalar(select(func.count()).select_from(Task)) or 0


def create_conversation(client: TestClient, first_id: str, second_id: str) -> str:
    response = client.post("/conversations", json={"agent_ids": [first_id, second_id]})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_agent_config_catalog_returns_safe_provider_metadata_only():
    provider = FakeProvider()
    app = create_app(
        database_url="sqlite:///:memory:",
        providers=ProviderRegistry({"opencode": provider}),
    )
    with TestClient(app) as client:
        response = client.get("/agent-config/catalog")
        assert response.status_code == 200
        assert response.json() == {
            "providers": [
                {"id": "opencode", "supports_tool_ids": True, "model_catalog_url": "/opencode/models"},
            ]
        }
        assert "command" not in response.text
        assert "OPENAI_API_KEY" not in response.text
        assert "env_vars" not in response.text
    app.state.database.dispose()


def test_agent_ui_fields_round_trip_and_disable_preserves_conversation_history(client_and_provider):
    client, _provider = client_and_provider
    first = create_agent(client, "Analyst", "model-a")
    response = client.post(
        "/agents",
        json={
            "name": "Managed helper",
            "description": "Initial description",
            "instructions": "Initial instructions",
            "model_provider": "openai",
            "model_name": "fixture-model",
            "capabilities": ["research"],
            "tool_ids": [],
            "enabled": True,
        },
    )
    assert response.status_code == 201, response.text
    managed = response.json()
    conversation_id = create_conversation(client, first["id"], managed["id"])
    message = client.post(
        f"/conversations/{conversation_id}/messages",
        json={
            "sender_agent_id": first["id"],
            "recipient_agent_id": managed["id"],
            "content": "Keep this conversation.",
        },
    )
    assert message.status_code == 201, message.text
    original_message_count = len(client.get(f"/conversations/{conversation_id}").json()["messages"])

    update = client.patch(
        f"/agents/{managed['id']}",
        json={
            "name": "Managed research helper",
            "description": "Updated description",
            "instructions": "Updated instructions",
            "capabilities": ["research", "analysis"],
            "enabled": False,
        },
    )
    assert update.status_code == 200, update.text
    assert update.json()["name"] == "Managed research helper"
    assert update.json()["description"] == "Updated description"
    assert update.json()["capabilities"] == ["analysis", "research"]
    assert update.json()["enabled"] is False
    assert len(client.get(f"/conversations/{conversation_id}").json()["messages"]) == original_message_count
    assert managed["id"] not in [agent["id"] for agent in client.get("/agents").json() if agent["enabled"]]

    reenabled = client.patch(f"/agents/{managed['id']}", json={"enabled": True})
    assert reenabled.status_code == 200
    assert reenabled.json()["enabled"] is True
    assert len(client.get(f"/conversations/{conversation_id}").json()["messages"]) == original_message_count


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
    assert [event["sequence"] for event in run["events"]] == list(range(1, 9))
    assert [event["type"] for event in run["events"]] == [
        "run_started",
        "message_sent",
        "agent_invocation_started",
        "mcp_tool_permissions_checked",
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


def test_human_can_chat_with_an_agent_and_reload_the_persisted_trace(client_and_provider):
    client, provider = client_and_provider
    agent = create_agent(client, "Assistant", "model-human", capabilities=["research"])
    conversation_response = client.post("/chat/conversations", json={"agent_id": agent["id"]})
    assert conversation_response.status_code == 201, conversation_response.text
    conversation_id = conversation_response.json()["id"]

    response = client.post(
        f"/chat/conversations/{conversation_id}/messages",
        json={"content": "Summarize the proposal."},
    )

    assert response.status_code == 201, response.text
    run = response.json()
    assert run["status"] == "completed"
    assert run["source_type"] == "user"
    assert run["source_agent_id"] is None
    assert run["target_agent_id"] == agent["id"]
    assert run["agent_snapshots"]["target"]["capabilities"] == ["research"]
    assert [message["sender_type"] for message in run["messages"]] == ["user", "agent"]
    assert [event["type"] for event in run["events"]] == [
        "run_started",
        "user_message_received",
        "agent_invocation_started",
        "mcp_tool_permissions_checked",
        "model_call_started",
        "model_call_completed",
        "agent_response_saved",
        "run_completed",
    ]
    assert provider.calls[-1]["history"] == [
        {"role": "user", "content": "Summarize the proposal."}
    ]

    conversation = client.get(f"/conversations/{conversation_id}").json()
    assert [message["sequence"] for message in conversation["messages"]] == [1, 2]
    assert [message["sender_type"] for message in conversation["messages"]] == ["user", "agent"]
    assert client.get(f"/runs/{run['id']}").json()["status"] == "completed"


def test_human_chat_sends_prior_turns_as_conversation_context(client_and_provider):
    client, provider = client_and_provider
    agent = create_agent(client, "Assistant", "model-human")
    conversation_id = client.post("/chat/conversations", json={"agent_id": agent["id"]}).json()["id"]

    first = client.post(
        f"/chat/conversations/{conversation_id}/messages",
        json={"content": "My project is called Atlas."},
    )
    second = client.post(
        f"/chat/conversations/{conversation_id}/messages",
        json={"content": "What is the project called?"},
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert provider.calls[-1]["history"] == [
        {"role": "user", "content": "My project is called Atlas."},
        {"role": "assistant", "content": "Reply from Assistant"},
        {"role": "user", "content": "What is the project called?"},
    ]
    conversation = client.get(f"/conversations/{conversation_id}").json()
    assert [message["sequence"] for message in conversation["messages"]] == [1, 2, 3, 4]


def test_human_chat_uses_current_agent_version_at_each_run(client_and_provider):
    client, provider = client_and_provider
    agent = create_agent(client, "Assistant", "model-v1")
    conversation_id = client.post("/chat/conversations", json={"agent_id": agent["id"]}).json()["id"]
    first = client.post(
        f"/chat/conversations/{conversation_id}/messages",
        json={"content": "First turn."},
    )
    assert first.status_code == 201, first.text

    update = client.patch(
        f"/agents/{agent['id']}",
        json={"model_name": "model-v2", "instructions": "Use updated instructions."},
    )
    assert update.status_code == 200

    second = client.post(
        f"/chat/conversations/{conversation_id}/messages",
        json={"content": "Second turn."},
    )

    assert second.status_code == 201, second.text
    assert first.json()["agent_snapshots"]["target"]["version"] == 1
    assert second.json()["agent_snapshots"]["target"]["version"] == 2
    assert provider.calls[-1]["agent"]["model_name"] == "model-v2"
    assert provider.calls[-1]["agent"]["instructions"] == "Use updated instructions."


def test_disabled_agent_cannot_start_or_continue_a_human_chat(client_and_provider):
    client, _provider = client_and_provider
    agent = create_agent(client, "Assistant", "model-human")
    conversation_id = client.post("/chat/conversations", json={"agent_id": agent["id"]}).json()["id"]
    assert client.patch(f"/agents/{agent['id']}", json={"enabled": False}).status_code == 200

    response = client.post(
        f"/chat/conversations/{conversation_id}/messages",
        json={"content": "This should not run."},
    )
    create_disabled = client.post("/chat/conversations", json={"agent_id": agent["id"]})

    assert response.status_code == 409
    assert create_disabled.status_code == 409
    assert client.get(f"/conversations/{conversation_id}").json()["messages"] == []
    assert count_human_chat_runs(client) == 0


def test_human_chat_provider_error_persists_a_failed_run_and_trace(client_and_provider):
    client, provider = client_and_provider
    agent = create_agent(client, "Assistant", "model-human")
    conversation_id = client.post("/chat/conversations", json={"agent_id": agent["id"]}).json()["id"]
    provider.fail = True

    response = client.post(
        f"/chat/conversations/{conversation_id}/messages",
        json={"content": "Please respond."},
    )

    assert response.status_code == 502
    run_id = response.json()["detail"]["run_id"]
    run = client.get(f"/runs/{run_id}").json()
    assert run["status"] == "failed"
    assert run["error_code"] == "provider_error"
    assert run["messages"][0]["sender_type"] == "user"
    assert [event["type"] for event in run["events"]][-1] == "run_failed"


def test_browser_chat_page_is_served_by_the_app(client_and_provider):
    client, _provider = client_and_provider

    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Yeni ajan oluştur" in response.text
    assert "Gönder" in response.text


def test_human_chat_can_delegate_one_task_and_return_the_result_to_the_parent(client_and_provider):
    client, provider = client_and_provider
    parent = create_agent(client, "Coordinator", "model-parent")
    helper = create_agent(client, "Researcher", "model-helper", capabilities=["research"])
    conversation_id = client.post("/chat/conversations", json={"agent_id": parent["id"]}).json()["id"]
    provider.outputs = [
        HandoffRequest(capability=" Research ", task="Find the two key risks."),
        "Two risks: data quality and access control.",
        "The main risks are data quality and access control.",
    ]

    response = client.post(
        f"/chat/conversations/{conversation_id}/messages",
        json={"content": "Assess the rollout risks."},
    )

    assert response.status_code == 201, response.text
    run = response.json()
    assert run["status"] == "completed"
    assert [message["sender_type"] for message in run["messages"]] == ["user", "agent"]
    assert run["messages"][-1]["content"] == "The main risks are data quality and access control."
    tasks = run["tasks"]
    assert len(tasks) == 2
    root_task, child_task = tasks
    assert root_task["parent_task_id"] is None
    assert root_task["objective"] == "Assess the rollout risks."
    assert root_task["status"] == "completed"
    assert root_task["result"] == run["messages"][-1]["content"]
    assert child_task["parent_task_id"] == root_task["id"]
    assert child_task["agent_id"] == helper["id"]
    assert child_task["capability"] == "research"
    assert child_task["objective"] == "Find the two key risks."
    assert child_task["agent_snapshot"]["version"] == 1
    assert child_task["status"] == "completed"
    assert child_task["result"] == "Two risks: data quality and access control."
    assert [call["agent"]["id"] for call in provider.calls] == [parent["id"], helper["id"], parent["id"]]
    assert [call["allow_handoff"] for call in provider.calls] == [True, False, False]
    assert provider.calls[1]["history"] == [
        {"role": "user", "content": "Find the two key risks."}
    ]
    assert "Two risks: data quality and access control." in provider.calls[2]["history"][-1]["content"]
    event_types = [event["type"] for event in run["events"]]
    assert event_types.index("handoff_requested") < event_types.index("handoff_target_resolved")
    assert event_types.index("delegated_task_started") < event_types.index("delegated_task_completed")
    assert event_types.index("delegated_task_completed") < event_types.index("handoff_result_returned")
    assert event_types[-2:] == ["agent_response_saved", "run_completed"]
    assert helper["id"] in client.get(f"/conversations/{conversation_id}").json()["agent_ids"]


def test_handoff_with_only_disabled_matches_is_inspectable_and_creates_no_child_task(client_and_provider):
    client, provider = client_and_provider
    parent = create_agent(client, "Coordinator", "model-parent")
    helper = create_agent(client, "Researcher", "model-helper", capabilities=["research"])
    assert client.patch(f"/agents/{helper['id']}", json={"enabled": False}).status_code == 200
    conversation_id = client.post("/chat/conversations", json={"agent_id": parent["id"]}).json()["id"]
    provider.outputs = [
        HandoffRequest(capability="research", task="Find supporting evidence."),
        "I could not find an enabled researcher, so here is a limited answer.",
    ]

    response = client.post(
        f"/chat/conversations/{conversation_id}/messages",
        json={"content": "Review the evidence."},
    )

    assert response.status_code == 201, response.text
    run = response.json()
    assert run["status"] == "completed"
    assert len(run["tasks"]) == 1
    rejected = next(event for event in run["events"] if event["type"] == "handoff_rejected")
    assert rejected["payload"]["reason"] == "no_enabled_match"
    assert rejected["payload"]["disabled_match_count"] == 1
    assert "no child task was started" in provider.calls[-1]["history"][-1]["content"]


def test_ambiguous_handoff_creates_no_child_task_and_parent_can_finish(client_and_provider):
    client, provider = client_and_provider
    parent = create_agent(client, "Coordinator", "model-parent")
    create_agent(client, "Researcher A", "model-helper-a", capabilities=["research"])
    create_agent(client, "Researcher B", "model-helper-b", capabilities=["research"])
    conversation_id = client.post("/chat/conversations", json={"agent_id": parent["id"]}).json()["id"]
    provider.outputs = [
        HandoffRequest(capability="research", task="Find supporting evidence."),
        "The request is answerable without delegation.",
    ]

    response = client.post(
        f"/chat/conversations/{conversation_id}/messages",
        json={"content": "Review the evidence."},
    )

    assert response.status_code == 201, response.text
    run = response.json()
    assert run["status"] == "completed"
    assert len(run["tasks"]) == 1
    rejected = next(event for event in run["events"] if event["type"] == "handoff_rejected")
    assert rejected["payload"]["reason"] == "ambiguous_match"
    assert rejected["payload"]["candidate_count"] == 2


def test_delegated_provider_error_is_recorded_and_parent_returns_a_safe_answer(client_and_provider):
    client, provider = client_and_provider
    parent = create_agent(client, "Coordinator", "model-parent")
    helper = create_agent(client, "Researcher", "model-helper", capabilities=["research"])
    conversation_id = client.post("/chat/conversations", json={"agent_id": parent["id"]}).json()["id"]
    provider.outputs = [
        HandoffRequest(capability="research", task="Find supporting evidence."),
        "I could not complete the delegated lookup, so I cannot verify the source.",
    ]
    provider.fail_for_agents.add(helper["id"])

    response = client.post(
        f"/chat/conversations/{conversation_id}/messages",
        json={"content": "Review the evidence."},
    )

    assert response.status_code == 201, response.text
    run = response.json()
    assert run["status"] == "completed"
    child_task = next(task for task in run["tasks"] if task["parent_task_id"] is not None)
    assert child_task["status"] == "failed"
    assert child_task["error_code"] == "provider_error"
    assert child_task["result"] is None
    failure = next(event for event in run["events"] if event["type"] == "delegated_task_failed")
    assert failure["payload"] == {"task_id": child_task["id"], "error_code": "provider_error"}
    assert run["messages"][-1]["content"] == "I could not complete the delegated lookup, so I cannot verify the source."


def test_delegated_agent_cannot_start_a_recursive_handoff(client_and_provider):
    client, provider = client_and_provider
    parent = create_agent(client, "Coordinator", "model-parent")
    helper = create_agent(client, "Researcher", "model-helper", capabilities=["research"])
    specialist = create_agent(client, "Specialist", "model-specialist", capabilities=["security"])
    conversation_id = client.post("/chat/conversations", json={"agent_id": parent["id"]}).json()["id"]
    provider.outputs = [
        HandoffRequest(capability="research", task="Review the design."),
        HandoffRequest(capability="security", task="Review security."),
        "I could not complete the delegated review.",
    ]

    response = client.post(
        f"/chat/conversations/{conversation_id}/messages",
        json={"content": "Review the design."},
    )

    assert response.status_code == 201, response.text
    run = response.json()
    assert run["status"] == "completed"
    assert len(run["tasks"]) == 2
    child_task = next(task for task in run["tasks"] if task["parent_task_id"] is not None)
    assert child_task["agent_id"] == helper["id"]
    assert child_task["status"] == "failed"
    assert child_task["error_code"] == "provider_error"
    assert [call["allow_handoff"] for call in provider.calls] == [True, False, False]
    assert all(task["agent_id"] != specialist["id"] for task in run["tasks"])


def test_malformed_handoff_is_rejected_without_creating_a_child_task(client_and_provider):
    client, provider = client_and_provider
    parent = create_agent(client, "Coordinator", "model-parent")
    create_agent(client, "Researcher", "model-helper", capabilities=["research"])
    conversation_id = client.post("/chat/conversations", json={"agent_id": parent["id"]}).json()["id"]
    provider.outputs = [HandoffRequest(capability="  ", task="Find supporting evidence.")]

    response = client.post(
        f"/chat/conversations/{conversation_id}/messages",
        json={"content": "Review the evidence."},
    )

    assert response.status_code == 502
    run_id = response.json()["detail"]["run_id"]
    run = client.get(f"/runs/{run_id}").json()
    assert run["status"] == "failed"
    assert run["error_code"] == "provider_error"
    assert len(run["tasks"]) == 1
    assert run["tasks"][0]["status"] == "failed"
    assert count_tasks(client) == 1


class _FakeA2AServer:
    def __init__(self, mode="task", auth=False, bad_origin=False, tenant=None):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        import json

        self.mode = mode
        self.auth = auth
        self.bad_origin = bad_origin
        self.tenant = tenant
        self.posts = 0
        self.last_get_query = None
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def reply(self, status, data):
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/a2a+json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def do_GET(self):
                from urllib.parse import parse_qs, urlsplit
                parsed = urlsplit(self.path)
                if parsed.path == "/.well-known/agent-card.json":
                    owner.reply_card(self)
                elif parsed.path == "/a2a/tasks/remote-1":
                    owner.last_get_query = parse_qs(parsed.query)
                    if owner.mode == "slow":
                        owner.reply_slow(self)
                    elif owner.mode == "terminal_message":
                        self.reply(200, {"id":"remote-1", "status":{"state":"TASK_STATE_COMPLETED",
                                         "message":{"role":"ROLE_AGENT", "parts":[{"text":"status message result"}]}}})
                    else:
                        self.reply(200, {"id":"remote-1", "status":{"state":"TASK_STATE_COMPLETED"},
                                         "artifacts":[{"parts":[{"text":"remote result"}]}]})
                else:
                    self.reply(404, {})
            def do_POST(self):
                owner.posts += 1
                owner.last_headers = dict(self.headers)
                owner.last_message = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                if owner.mode == "ambiguous":
                    self.reply(500, {"error":"simulated lost response"})
                elif owner.mode == "direct":
                    self.reply(200, {"message":{"messageId":"reply-1", "role":"ROLE_AGENT",
                                                "parts":[{"text":"direct result"}]}})
                else:
                    self.reply(200, {"task":{"id":"remote-1", "contextId":"ctx-1",
                                              "status":{"state":"TASK_STATE_WORKING"}}})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
    def reply_card(self, handler):
        import json
        endpoint = self.url + "/a2a"
        if self.bad_origin:
            endpoint = endpoint.replace(f":{self.server.server_port}", ":1")
        interface = {"url":endpoint, "protocolBinding":"HTTP+JSON", "protocolVersion":"1.0"}
        if self.tenant is not None:
            interface["tenant"] = self.tenant
        card = {"name":"Local A2A test", "version":"1.0", "supportedInterfaces":[interface],
            "skills":[{"id":"research", "name":"Research", "description":"Research tasks", "tags":["research"]}]}
        if self.auth:
            card["securitySchemes"] = {"bearerAuth":{"httpAuthSecurityScheme":{"scheme":"Bearer", "bearerFormat":"JWT"}}}
        body = json.dumps(card).encode()
        handler.send_response(200); handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body))); handler.end_headers(); handler.wfile.write(body)
    def reply_slow(self, handler):
        """Trickle fewer bytes than the declared body, so the response can never finish."""
        import json
        import time
        body = json.dumps({"id":"remote-1", "status":{"state":"TASK_STATE_WORKING"}, "padding":"x" * 4096}).encode()
        chunks = 60
        handler.send_response(200); handler.send_header("Content-Type", "application/a2a+json")
        handler.send_header("Content-Length", str(len(body) * (chunks + 1))); handler.end_headers()
        try:
            for _ in range(chunks):
                handler.wfile.write(body); handler.wfile.flush(); time.sleep(0.1)
        except OSError:
            # The client stopped reading at its own deadline, which is the expected outcome.
            pass
    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)


def _enable_a2a(monkeypatch, server, auth=False, max_wait_seconds=2):
    target = {"id":"local-research", "url":server.url, "allow_private":True,
        "capabilities":["research"], "max_wait_seconds":max_wait_seconds, "poll_interval_seconds":0.01}
    if auth:
        monkeypatch.setenv("A2A_TEST_TOKEN", "secret-a2a-token")
        target.update({"token_env":"A2A_TEST_TOKEN", "security_scheme":"bearerAuth"})
    monkeypatch.setenv("AGENT_RUNTIME_A2A_TARGETS", json.dumps([target]))


@pytest.mark.parametrize(("mode", "expected"), [("direct", "direct result"), ("task", "remote result"), ("terminal_message", "status message result")])
def test_human_chat_a2a_delegation_handles_direct_and_task_responses(client_and_provider, monkeypatch, mode, expected):
    client, provider = client_and_provider
    server = _FakeA2AServer(mode)
    try:
        _enable_a2a(monkeypatch, server)
        parent = create_agent(client, "Coordinator", "model-parent")
        conversation = client.post("/chat/conversations", json={"agent_id":parent["id"]}).json()["id"]
        provider.outputs = [HandoffRequest(capability="research", task="Find a concise answer."), "Here is the answer."]
        run = client.post(f"/chat/conversations/{conversation}/messages", json={"content":"Research this."}).json()
        assert run["status"] == "completed"
        child = run["tasks"][1]
        assert child["remote_target_id"] == "local-research"
        assert child["remote_status"] == "completed"
        assert child["result"] == expected
        assert child["remote_task_id"] == ("remote-1" if mode in {"task", "terminal_message"} else None)
        events = [event["type"] for event in run["events"]]
        assert "a2a_remote_task_status" in events
        assert server.posts == 1
        assert server.last_headers["A2A-Version"] == "1.0"
        assert server.last_headers["Content-Type"] == "application/a2a+json"
        assert server.last_message["message"]["role"] == "ROLE_USER"
        assert server.last_message["configuration"]["returnImmediately"] is True
        assert "research" in provider.calls[0]["agent"]["remote_a2a_capabilities"]
        discovered = client.get("/a2a/targets").json()
        assert discovered == [{"id":"local-research", "kind":"a2a", "capabilities":["research"]}]
        assert server.url not in json.dumps(discovered)
    finally:
        server.close()




def test_a2a_v1_propagates_interface_tenant_on_send_and_get_task(client_and_provider, monkeypatch):
    client, provider = client_and_provider
    server = _FakeA2AServer("task", tenant="tenant / west")
    try:
        _enable_a2a(monkeypatch, server)
        parent = create_agent(client, "Coordinator", "model-parent")
        conversation = client.post("/chat/conversations", json={"agent_id":parent["id"]}).json()["id"]
        provider.outputs = [HandoffRequest(capability="research", task="Use the tenant route."), "Done."]
        run = client.post(f"/chat/conversations/{conversation}/messages", json={"content":"Research."}).json()
        assert run["status"] == "completed"
        assert server.last_message["tenant"] == "tenant / west"
        assert server.last_get_query == {"tenant":["tenant / west"]}
    finally:
        server.close()


def test_a2a_poll_deadline_interrupts_a_slow_trickling_response(client_and_provider, monkeypatch):
    client, provider = client_and_provider
    server = _FakeA2AServer("slow")
    try:
        _enable_a2a(monkeypatch, server, max_wait_seconds=1)
        parent = create_agent(client, "Coordinator", "model-parent")
        conversation = client.post("/chat/conversations", json={"agent_id":parent["id"]}).json()["id"]
        provider.outputs = [HandoffRequest(capability="research", task="Wait for the remote task."), "The remote task timed out."]
        run = client.post(f"/chat/conversations/{conversation}/messages", json={"content":"Research."}).json()
        assert run["status"] == "completed"
        # The remote body never finishes arriving, so the 1 s deadline is the only thing that can
        # end the read; a machine that is merely slow cannot turn this assertion around.
        assert run["tasks"][1]["remote_status"] == "timeout"
    finally:
        server.close()


def test_a2a_v1_bearer_auth_uses_server_environment_and_is_not_exposed(client_and_provider, monkeypatch):
    client, provider = client_and_provider
    server = _FakeA2AServer("direct", auth=True)
    try:
        _enable_a2a(monkeypatch, server, auth=True)
        parent = create_agent(client, "Coordinator", "model-parent")
        conversation = client.post("/chat/conversations", json={"agent_id":parent["id"]}).json()["id"]
        provider.outputs = [HandoffRequest(capability="research", task="A small research task."), "Done."]
        run = client.post(f"/chat/conversations/{conversation}/messages", json={"content":"Research."}).json()
        assert run["status"] == "completed"
        assert server.last_headers["Authorization"] == "Bearer secret-a2a-token"
        assert "secret-a2a-token" not in json.dumps(run)
        assert "secret-a2a-token" not in json.dumps(client.get("/a2a/targets").json())
    finally:
        server.close()


def test_a2a_card_cannot_redirect_credentials_to_another_origin(client_and_provider, monkeypatch):
    client, provider = client_and_provider
    server = _FakeA2AServer("direct", bad_origin=True)
    try:
        _enable_a2a(monkeypatch, server)
        parent = create_agent(client, "Coordinator", "model-parent")
        conversation = client.post("/chat/conversations", json={"agent_id":parent["id"]}).json()["id"]
        provider.outputs = [HandoffRequest(capability="research", task="Research."), "Cannot delegate safely."]
        run = client.post(f"/chat/conversations/{conversation}/messages", json={"content":"Research."}).json()
        assert run["tasks"][1]["error_code"] == "remote_validation_failed"
        assert server.posts == 0
    finally:
        server.close()


def test_ambiguous_a2a_submission_is_not_posted_again_on_retry(client_and_provider, monkeypatch):
    client, provider = client_and_provider
    server = _FakeA2AServer("ambiguous")
    try:
        _enable_a2a(monkeypatch, server)
        parent = create_agent(client, "Coordinator", "model-parent")
        conversation = client.post("/chat/conversations", json={"agent_id":parent["id"]}).json()["id"]
        request = HandoffRequest(capability="research", task="Create one remote item.")
        provider.outputs = [request, "The remote submission result is unknown."]
        run = client.post(f"/chat/conversations/{conversation}/messages", json={"content":"Do this."}).json()
        child = run["tasks"][1]
        assert child["remote_status"] == "submission_unknown"
        assert child["error_code"] == "remote_error"
        assert server.posts == 1
        state = {"run_id":run["id"], "history":[]}
        monkeypatch.setenv("AGENT_RUNTIME_A2A_TARGETS", "[]")
        replay = client.app.state.runtime._try_a2a_handoff(state, request)
        assert replay is not None
        assert client.get(f"/runs/{run['id']}").json()["tasks"][1]["remote_status"] == "submission_unknown"

        changed_server = _FakeA2AServer("direct")
        try:
            _enable_a2a(monkeypatch, changed_server)
            replay = client.app.state.runtime._try_a2a_handoff(state, request)
            assert replay is not None
            changed_run = client.get(f"/runs/{run['id']}").json()
            assert changed_run["tasks"][1]["remote_status"] == "submission_unknown"
            changed_events = [event for event in changed_run["events"] if event["type"] == "delegated_task_failed"]
            assert changed_events[-1]["payload"]["remote_status"] == "submission_unknown"
            assert changed_server.posts == 0
        finally:
            changed_server.close()

        _enable_a2a(monkeypatch, server)
        replay = client.app.state.runtime._try_a2a_handoff(state, request)
        assert replay is not None
        assert server.posts == 1
    finally:
        server.close()
