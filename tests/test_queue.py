from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from agent_runtime_platform.api import create_app
from agent_runtime_platform.models import HumanChatMessage, HumanChatRun, QueueJob, Task
from agent_runtime_platform.providers import HandoffRequest, ProviderRegistry
from agent_runtime_platform.queue_worker import claim_one, recover_interrupted_jobs


class QueueProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.fail_once = False
        self.fail_always = False

    def generate(self, agent: dict[str, Any], history: list[dict[str, str]], *, allow_handoff: bool = False) -> str:
        self.calls += 1
        if self.fail_always or self.fail_once:
            self.fail_once = False
            raise RuntimeError("temporary failure")
        return "queued answer"


def setup_chat(tmp_path: Path, provider: QueueProvider):
    url = f"sqlite:///{tmp_path / 'queue.db'}"
    app = create_app(url, ProviderRegistry({"openai": provider}))
    client = TestClient(app)
    agent = client.post("/agents", json={
        "name": "Queue agent", "instructions": "Answer clearly.",
        "model_provider": "openai", "model_name": "test-model",
    }).json()
    conversation = client.post("/chat/conversations", json={"agent_id": agent["id"]}).json()
    return url, app, client, conversation["id"]


def test_async_acceptance_survives_worker_restart_and_has_one_visible_reply(tmp_path):
    provider = QueueProvider()
    url, app, client, conversation_id = setup_chat(tmp_path, provider)
    accepted = client.post(
        f"/chat/conversations/{conversation_id}/messages/async", json={"content": "Hello"}
    )
    assert accepted.status_code == 202
    payload = accepted.json()
    assert payload["status"] == "queued"
    assert payload["status_url"] == f"/runs/{payload['id']}"
    assert client.get(payload["status_url"]).json()["status"] == "queued"

    # Simulate a worker dying after it claimed the durable job.
    assert claim_one(app.state.runtime) == payload["id"]
    app.state.database.dispose()
    restarted = create_app(url, ProviderRegistry({"openai": provider}))
    recover_interrupted_jobs(restarted.state.runtime)
    assert restarted.state.runtime.get_run(payload["id"])["status"] == "queued"
    assert claim_one(restarted.state.runtime) == payload["id"]
    restarted.state.runtime.execute_queued_run(payload["id"])
    restarted.state.runtime.execute_queued_run(payload["id"])

    result = restarted.state.runtime.get_run(payload["id"])
    assert result["status"] == "completed"
    assert result["queue"] == {"status": "completed", "attempts": 2, "max_attempts": 3}
    with restarted.state.database.session() as session:
        replies = session.scalars(select(HumanChatMessage).where(
            HumanChatMessage.run_id == payload["id"], HumanChatMessage.kind == "agent_response"
        )).all()
        assert len(replies) == 1
    restarted.state.database.dispose()
    client.close()


def test_failed_attempt_is_retried_with_same_run_and_one_response(tmp_path):
    provider = QueueProvider()
    provider.fail_once = True
    _url, app, client, conversation_id = setup_chat(tmp_path, provider)
    accepted = client.post(
        f"/chat/conversations/{conversation_id}/messages/async", json={"content": "Try this"}
    )
    run_id = accepted.json()["id"]

    assert claim_one(app.state.runtime) == run_id
    app.state.runtime.execute_queued_run(run_id)
    assert app.state.runtime.get_run(run_id)["status"] == "queued"
    assert claim_one(app.state.runtime) == run_id
    app.state.runtime.execute_queued_run(run_id)
    result = app.state.runtime.get_run(run_id)
    assert result["status"] == "completed"
    assert [event["type"] for event in result["events"]].count("retry_scheduled") == 1
    with app.state.database.session() as session:
        replies = session.scalars(select(HumanChatMessage).where(
            HumanChatMessage.run_id == run_id, HumanChatMessage.kind == "agent_response"
        )).all()
        job = session.get(QueueJob, run_id)
        run = session.get(HumanChatRun, run_id)
        assert len(replies) == 1
        assert job.status == run.status == "completed"
    app.state.database.dispose()
    client.close()


def test_agent_message_async_endpoint_queues_and_worker_completes(tmp_path):
    provider = QueueProvider()
    url = f"sqlite:///{tmp_path / 'agent-queue.db'}"
    app = create_app(url, ProviderRegistry({"openai": provider}))
    client = TestClient(app)
    sender = client.post("/agents", json={
        "name": "Sender", "instructions": "Send.", "model_provider": "openai", "model_name": "test"
    }).json()
    target = client.post("/agents", json={
        "name": "Target", "instructions": "Answer.", "model_provider": "openai", "model_name": "test"
    }).json()
    conversation = client.post("/conversations", json={"agent_ids": [sender["id"], target["id"]]}).json()
    accepted = client.post(f"/conversations/{conversation['id']}/messages/async", json={
        "sender_agent_id": sender["id"], "recipient_agent_id": target["id"], "content": "Review this"
    })
    assert accepted.status_code == 202
    run_id = accepted.json()["id"]
    assert claim_one(app.state.runtime) == run_id
    app.state.runtime.execute_queued_run(run_id)
    assert app.state.runtime.get_run(run_id)["status"] == "completed"
    client.close()
    app.state.database.dispose()


def test_retry_limit_marks_run_failed_without_a_partial_response(tmp_path):
    provider = QueueProvider()
    provider.fail_always = True
    _url, app, client, conversation_id = setup_chat(tmp_path, provider)
    run_id = client.post(
        f"/chat/conversations/{conversation_id}/messages/async", json={"content": "Fail safely"}
    ).json()["id"]

    for attempt in range(3):
        assert claim_one(app.state.runtime) == run_id
        app.state.runtime.execute_queued_run(run_id)
        status = app.state.runtime.get_run(run_id)["status"]
        assert status == ("failed" if attempt == 2 else "queued")
    with app.state.database.session() as session:
        job = session.get(QueueJob, run_id)
        replies = session.scalars(select(HumanChatMessage).where(
            HumanChatMessage.run_id == run_id, HumanChatMessage.kind == "agent_response"
        )).all()
        assert job.status == "failed"
        assert job.attempts == job.max_attempts == 3
        assert replies == []
    client.close()
    app.state.database.dispose()


def test_two_workers_cannot_claim_the_same_pending_run(tmp_path):
    provider = QueueProvider()
    _url, app, client, conversation_id = setup_chat(tmp_path, provider)
    run_id = client.post(
        f"/chat/conversations/{conversation_id}/messages/async", json={"content": "Claim once"}
    ).json()["id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _index: claim_one(app.state.runtime), range(2)))
    assert claims.count(run_id) == 1
    assert claims.count(None) == 1
    client.close()
    app.state.database.dispose()


def test_handoff_retry_reuses_the_completed_child_task(tmp_path):
    provider = QueueProvider()
    _url, app, client, conversation_id = setup_chat(tmp_path, provider)
    delegated = client.post("/agents", json={
        "name": "Backend", "instructions": "Do backend work.", "model_provider": "openai",
        "model_name": "test-model", "capabilities": ["backend"],
    }).json()
    run_id = client.post(
        f"/chat/conversations/{conversation_id}/messages/async", json={"content": "Delegate"}
    ).json()["id"]
    assert claim_one(app.state.runtime) == run_id
    state = {
        "run_id": run_id,
        "target_config": {"id": "parent", "name": "Parent", "instructions": "",
                           "model_provider": "openai", "model_name": "test-model",
                           "capabilities": [], "version": 1},
        "history": [{"role": "user", "content": "Delegate"}],
        "handoff_request": HandoffRequest("backend", "Review the backend"),
    }

    first = app.state.runtime._execute_handoff(state)
    second = app.state.runtime._execute_handoff(state)
    assert first["history"][-1]["content"].find("queued answer") >= 0
    assert second["history"][-1]["content"].find("queued answer") >= 0
    assert provider.calls == 1
    with app.state.database.session() as session:
        children = session.scalars(select(Task).where(
            Task.root_run_id == run_id, Task.parent_task_id.is_not(None)
        )).all()
        assert len(children) == 1
        assert children[0].agent_id == delegated["id"]
    client.close()
    app.state.database.dispose()
