from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent_runtime_platform.api import create_app
from agent_runtime_platform.models import Agent, HumanChatMessage, HumanChatRun, QueueJob, Task
from agent_runtime_platform.providers import HandoffRequest, ProviderRegistry
from agent_runtime_platform.runtime import _agent_snapshot
from agent_runtime_platform.queue_worker import (
    WorkerAlreadyRunning, acquire_worker_lock, claim_one, recover_interrupted_jobs,
)


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
    with app.state.database.session() as session:
        root_task = session.scalar(select(Task).where(
            Task.root_run_id == payload["id"], Task.parent_task_id.is_(None)
        ))
        assert root_task.status == "queued"

    # Simulate a worker dying after it claimed the durable job.
    assert claim_one(app.state.runtime) == payload["id"]
    with app.state.database.session() as session:
        root_task = session.scalar(select(Task).where(
            Task.root_run_id == payload["id"], Task.parent_task_id.is_(None)
        ))
        assert root_task.status == "running"
    app.state.database.dispose()
    restarted = create_app(url, ProviderRegistry({"openai": provider}))
    recover_interrupted_jobs(restarted.state.runtime)
    assert restarted.state.runtime.get_run(payload["id"])["status"] == "queued"
    with restarted.state.database.session() as session:
        root_task = session.scalar(select(Task).where(
            Task.root_run_id == payload["id"], Task.parent_task_id.is_(None)
        ))
        assert root_task.status == "queued"
    assert claim_one(restarted.state.runtime) == payload["id"]
    with restarted.state.database.session() as session:
        root_task = session.scalar(select(Task).where(
            Task.root_run_id == payload["id"], Task.parent_task_id.is_(None)
        ))
        assert root_task.status == "running"
    restarted.state.runtime.execute_queued_run(payload["id"])
    restarted.state.runtime.execute_queued_run(payload["id"])

    result = restarted.state.runtime.get_run(payload["id"])
    assert result["status"] == "completed"
    assert result["queue"] == {"status": "completed", "attempts": 2, "max_attempts": 3}
    with restarted.state.database.session() as session:
        root_task = session.scalar(select(Task).where(
            Task.root_run_id == payload["id"], Task.parent_task_id.is_(None)
        ))
        assert root_task.status == "completed"
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
    with app.state.database.session() as session:
        root_task = session.scalar(select(Task).where(
            Task.root_run_id == run_id, Task.parent_task_id.is_(None)
        ))
        assert root_task.status == "queued"
    assert claim_one(app.state.runtime) == run_id
    with app.state.database.session() as session:
        root_task = session.scalar(select(Task).where(
            Task.root_run_id == run_id, Task.parent_task_id.is_(None)
        ))
        assert root_task.status == "running"
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
        run = session.get(HumanChatRun, run_id)
        root_task = session.scalar(select(Task).where(
            Task.root_run_id == run_id, Task.parent_task_id.is_(None)
        ))
        assert job.status == run.status == "failed"
        assert root_task.status == "failed"
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
    changed_state = {
        **state,
        "handoff_request": HandoffRequest("frontend", "A different retry objective"),
    }
    second = app.state.runtime._execute_handoff(changed_state)
    assert "queued answer" in first["history"][-1]["content"]
    assert "queued answer" in second["history"][-1]["content"]
    assert "Review the backend" in second["history"][-1]["content"]
    assert "A different retry objective" not in second["history"][-1]["content"]
    assert provider.calls == 1
    with app.state.database.session() as session:
        children = session.scalars(select(Task).where(
            Task.root_run_id == run_id, Task.parent_task_id.is_not(None)
        )).all()
        assert len(children) == 1
        assert children[0].agent_id == delegated["id"]
    client.close()
    app.state.database.dispose()


def test_worker_process_lock_blocks_second_owner_and_releases_cleanly(tmp_path):
    provider = QueueProvider()
    _url, app, client, _conversation_id = setup_chat(tmp_path, provider)
    first_lock = acquire_worker_lock(app.state.runtime)
    try:
        try:
            acquire_worker_lock(app.state.runtime)
        except WorkerAlreadyRunning:
            pass
        else:
            raise AssertionError("A second worker acquired the same database lock")
    finally:
        import os
        os.close(first_lock)
    second_lock = acquire_worker_lock(app.state.runtime)
    import os
    os.close(second_lock)
    client.close()
    app.state.database.dispose()


def test_startup_recovery_exhaustion_fails_run_job_and_root_task_together(tmp_path):
    provider = QueueProvider()
    _url, app, client, conversation_id = setup_chat(tmp_path, provider)
    run_id = client.post(
        f"/chat/conversations/{conversation_id}/messages/async", json={"content": "Interrupted"}
    ).json()["id"]
    with app.state.database.session() as session:
        job = session.get(QueueJob, run_id)
        run = session.get(HumanChatRun, run_id)
        root_task = session.scalar(select(Task).where(
            Task.root_run_id == run_id, Task.parent_task_id.is_(None)
        ))
        job.status = run.status = root_task.status = "running"
        job.attempts = job.max_attempts
        session.commit()

    recover_interrupted_jobs(app.state.runtime)
    with app.state.database.session() as session:
        job = session.get(QueueJob, run_id)
        run = session.get(HumanChatRun, run_id)
        root_task = session.scalar(select(Task).where(
            Task.root_run_id == run_id, Task.parent_task_id.is_(None)
        ))
        assert job.status == run.status == root_task.status == "failed"
        assert job.last_error == run.error_code == root_task.error_code == "worker_interrupted"
    client.close()
    app.state.database.dispose()


def test_graph_retry_resumes_persisted_child_when_parent_model_returns_plain_text(tmp_path):
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

    # This persisted child represents a prior attempt interrupted during delegation.
    with app.state.database.session() as session:
        root_task = session.scalar(select(Task).where(
            Task.root_run_id == run_id, Task.parent_task_id.is_(None)
        ))
        delegate_agent = session.get(Agent, delegated["id"])
        child = Task(
            root_run_id=run_id, parent_task_id=root_task.id, conversation_id=conversation_id,
            agent_id=delegate_agent.id, capability="backend", objective="Persisted original work",
            config_snapshot=_agent_snapshot(delegate_agent), status="failed",
            error_code="provider_error", completed_at=datetime.now(timezone.utc), result="stale failure",
        )
        session.add(child)
        session.commit()

    # The fake provider returns plain text for every call. The graph must skip
    # parent planning, resume the saved child intent, then finalize with its result.
    app.state.runtime.execute_queued_run(run_id)
    result = app.state.runtime.get_run(run_id)
    assert result["status"] == "completed"
    phases = [event["payload"].get("phase") for event in result["events"] if event["type"] == "model_call_started"]
    assert phases == ["delegated_task", "handoff_finalization"]
    assert provider.calls == 2
    with app.state.database.session() as session:
        persisted_child = session.get(Task, child.id)
        root_task = session.scalar(select(Task).where(
            Task.root_run_id == run_id, Task.parent_task_id.is_(None)
        ))
        assert persisted_child.status == "completed"
        assert persisted_child.error_code is None
        assert persisted_child.completed_at is not None
        assert persisted_child.result == "queued answer"
        assert root_task.status == "completed"
    client.close()
    app.state.database.dispose()


def test_queued_run_does_not_use_mcp_grants_after_agent_is_disabled(tmp_path, monkeypatch):
    import json
    import sys

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

    class ToolAwareProvider:
        def __init__(self):
            self.tool_ids_at_call = None

        def generate(self, agent, history, *, allow_handoff=False):
            self.tool_ids_at_call = agent.get("tool_ids", [])
            return "answer without revoked tool"

    provider = ToolAwareProvider()
    app = create_app(
        f"sqlite:///{tmp_path / 'mcp-revocation.db'}",
        ProviderRegistry({"codex": provider}),
    )
    client = TestClient(app)
    agent = client.post("/agents", json={
        "name": "Queued reader", "instructions": "Read safely.",
        "model_name": "test", "tool_ids": ["fixture/lookup"],
    }).json()
    conversation = client.post("/chat/conversations", json={"agent_id": agent["id"]}).json()
    queued = client.post(
        f"/chat/conversations/{conversation['id']}/messages/async",
        json={"content": "Read this."},
    ).json()

    assert claim_one(app.state.runtime) == queued["id"]
    revoked = client.patch(f"/agents/{agent['id']}", json={"enabled": False})
    assert revoked.status_code == 200
    assert revoked.json()["tool_ids"] == ["fixture/lookup"]
    app.state.runtime.execute_queued_run(queued["id"])

    result = app.state.runtime.get_run(queued["id"])
    assert result["status"] == "completed"
    assert result["agent_snapshots"]["target"]["tool_ids"] == ["fixture/lookup"]
    checked = next(event for event in result["events"] if event["type"] == "mcp_tool_permissions_checked")
    assert checked["payload"]["snapshot_tool_ids"] == ["fixture/lookup"]
    assert checked["payload"]["effective_tool_ids"] == []
    assert provider.tool_ids_at_call == []
    client.close()
    app.state.database.dispose()


def test_failed_child_retry_rechecks_revoked_codex_grant(tmp_path, monkeypatch):
    import json
    import sys

    fixture = Path(__file__).parent / "fixtures" / "readonly_mcp_server.py"
    monkeypatch.setenv("AGENT_RUNTIME_MCP_SERVERS", json.dumps({
        "fixture": {"command": sys.executable, "args": [str(fixture)], "read_only_tools": ["lookup"]}
    }))

    class ToolAware:
        def __init__(self):
            self.grants = []
        def generate(self, agent, history, **kwargs):
            self.grants.append(list(agent.get("tool_ids", [])))
            return "delegated response"

    parent_provider = QueueProvider()
    tool_provider = ToolAware()
    app = create_app(
        f"sqlite:///{tmp_path / 'child-revoke.db'}",
        ProviderRegistry({"openai": parent_provider, "codex": tool_provider}),
    )
    client = TestClient(app)
    parent = client.post("/agents", json={
        "name": "Parent", "instructions": "Delegate", "model_provider": "openai", "model_name": "test"
    }).json()
    conversation = client.post("/chat/conversations", json={"agent_id": parent["id"]}).json()
    run_id = client.post(
        f"/chat/conversations/{conversation['id']}/messages/async", json={"content": "Resume child"}
    ).json()["id"]
    child = client.post("/agents", json={
        "name": "Codex child", "instructions": "Read only", "model_provider": "codex",
        "model_name": "test", "capabilities": ["backend"], "tool_ids": ["fixture/lookup"],
    }).json()
    assert claim_one(app.state.runtime) == run_id
    with app.state.database.session() as session:
        root = session.scalar(select(Task).where(Task.root_run_id == run_id, Task.parent_task_id.is_(None)))
        child_row = session.get(Agent, child["id"])
        session.add(Task(
            root_run_id=run_id, parent_task_id=root.id, conversation_id=conversation["id"],
            agent_id=child_row.id, capability="backend", objective="Original child task",
            config_snapshot=_agent_snapshot(child_row), status="failed", error_code="provider_error",
            completed_at=datetime.now(timezone.utc), result="stale",
        ))
        session.commit()
    assert client.patch(f"/agents/{child['id']}", json={"tool_ids": []}).status_code == 200

    app.state.runtime.execute_queued_run(run_id)
    result = app.state.runtime.get_run(run_id)
    assert result["status"] == "completed"
    assert tool_provider.grants == [[]]
    checked = [event["payload"] for event in result["events"] if event["type"] == "mcp_tool_permissions_checked" and event["payload"].get("phase") == "delegated_task"]
    assert checked and checked[-1]["effective_tool_ids"] == []
    assert not [event for event in result["events"] if event["type"] == "mcp_tool_call"]
    client.close()
    app.state.database.dispose()


class HardTerminationProvider:
    """Dies mid-call on the configured call numbers, like a SIGKILLed worker."""

    def __init__(self) -> None:
        self.calls = 0
        self.crash_calls: set[int] = set()
        self.reply = "answer after recovery"

    def generate(self, agent: dict[str, Any], history: list[dict[str, str]], *, allow_handoff: bool = False) -> str:
        self.calls += 1
        if self.calls in self.crash_calls:
            raise SystemExit("simulated hard termination during the model call")
        return self.reply


def test_hard_termination_consumes_one_attempt_and_the_retry_replies_once(tmp_path):
    provider = HardTerminationProvider()
    provider.crash_calls = {1}
    url, app, client, conversation_id = setup_chat(tmp_path, provider)
    run_id = client.post(
        f"/chat/conversations/{conversation_id}/messages/async", json={"content": "Survive a kill"}
    ).json()["id"]

    assert claim_one(app.state.runtime) == run_id
    with pytest.raises(SystemExit):
        app.state.runtime.execute_queued_run(run_id)
    # The interrupted attempt is spent, and it left no half-written reply behind.
    with app.state.database.session() as session:
        job = session.get(QueueJob, run_id)
        replies = session.scalars(select(HumanChatMessage).where(
            HumanChatMessage.run_id == run_id, HumanChatMessage.kind == "agent_response"
        )).all()
        assert job.status == "running"
        assert job.attempts == 1
        assert replies == []
    app.state.database.dispose()

    restarted = create_app(url, ProviderRegistry({"openai": provider}))
    recover_interrupted_jobs(restarted.state.runtime)
    with restarted.state.database.session() as session:
        job = session.get(QueueJob, run_id)
        assert job.status == "pending"
        assert job.attempts == 1
    assert restarted.state.runtime.get_run(run_id)["status"] == "queued"
    assert claim_one(restarted.state.runtime) == run_id
    restarted.state.runtime.execute_queued_run(run_id)

    result = restarted.state.runtime.get_run(run_id)
    assert result["status"] == "completed"
    assert result["queue"] == {"status": "completed", "attempts": 2, "max_attempts": 3}
    with restarted.state.database.session() as session:
        replies = session.scalars(select(HumanChatMessage).where(
            HumanChatMessage.run_id == run_id, HumanChatMessage.kind == "agent_response"
        )).all()
        assert [reply.content for reply in replies] == ["answer after recovery"]
    restarted.state.database.dispose()
    client.close()


def test_hard_termination_never_grants_more_than_three_attempts(tmp_path):
    provider = HardTerminationProvider()
    provider.crash_calls = {1, 2, 3}
    url, app, client, conversation_id = setup_chat(tmp_path, provider)
    run_id = client.post(
        f"/chat/conversations/{conversation_id}/messages/async", json={"content": "Never finishes"}
    ).json()["id"]

    for attempt in (1, 2):
        assert claim_one(app.state.runtime) == run_id
        with pytest.raises(SystemExit):
            app.state.runtime.execute_queued_run(run_id)
        app.state.database.dispose()
        app = create_app(url, ProviderRegistry({"openai": provider}))
        recover_interrupted_jobs(app.state.runtime)
        with app.state.database.session() as session:
            job = session.get(QueueJob, run_id)
            assert job.status == "pending"
            assert job.attempts == attempt
        assert app.state.runtime.get_run(run_id)["status"] == "queued"

    assert claim_one(app.state.runtime) == run_id
    with pytest.raises(SystemExit):
        app.state.runtime.execute_queued_run(run_id)
    app.state.database.dispose()
    app = create_app(url, ProviderRegistry({"openai": provider}))
    recover_interrupted_jobs(app.state.runtime)

    result = app.state.runtime.get_run(run_id)
    assert result["status"] == "failed"
    assert result["queue"] == {"status": "failed", "attempts": 3, "max_attempts": 3}
    with app.state.database.session() as session:
        job = session.get(QueueJob, run_id)
        run = session.get(HumanChatRun, run_id)
        root_task = session.scalar(select(Task).where(
            Task.root_run_id == run_id, Task.parent_task_id.is_(None)
        ))
        replies = session.scalars(select(HumanChatMessage).where(
            HumanChatMessage.run_id == run_id, HumanChatMessage.kind == "agent_response"
        )).all()
        assert job.status == run.status == root_task.status == "failed"
        assert job.last_error == run.error_code == root_task.error_code == "worker_interrupted"
        assert replies == []
    assert claim_one(app.state.runtime) is None
    assert provider.calls == 3
    app.state.database.dispose()
    client.close()


def test_completed_run_is_not_answered_twice_when_a_stale_job_is_recovered(tmp_path):
    provider = QueueProvider()
    url, app, client, conversation_id = setup_chat(tmp_path, provider)
    run_id = client.post(
        f"/chat/conversations/{conversation_id}/messages/async", json={"content": "Answer once"}
    ).json()["id"]
    assert claim_one(app.state.runtime) == run_id
    app.state.runtime.execute_queued_run(run_id)
    assert app.state.runtime.get_run(run_id)["status"] == "completed"
    # A stale claim from a crashed worker must not earn the completed run a second reply.
    with app.state.database.session() as session:
        job = session.get(QueueJob, run_id)
        job.status = "running"
        session.commit()
    app.state.database.dispose()

    restarted = create_app(url, ProviderRegistry({"openai": provider}))
    recover_interrupted_jobs(restarted.state.runtime)
    assert restarted.state.runtime.get_run(run_id)["status"] == "completed"
    assert claim_one(restarted.state.runtime) == run_id
    assert provider.calls == 1
    restarted.state.runtime.execute_queued_run(run_id)
    with restarted.state.database.session() as session:
        job = session.get(QueueJob, run_id)
        replies = session.scalars(select(HumanChatMessage).where(
            HumanChatMessage.run_id == run_id, HumanChatMessage.kind == "agent_response"
        )).all()
        assert job.status == "completed"
        assert len(replies) == 1
    restarted.state.database.dispose()
    client.close()


class DelegationCrashProvider:
    """Plans one handoff, then dies during the delegated model call."""

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, agent: dict[str, Any], history: list[dict[str, str]], *, allow_handoff: bool = False) -> str | HandoffRequest:
        self.calls += 1
        if self.calls == 1:
            return HandoffRequest("backend", "First recorded objective")
        raise SystemExit("simulated hard termination during the delegated call")


class ReplanningProvider:
    """Would propose a different handoff if a retry asked the parent to plan again."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(self, agent: dict[str, Any], history: list[dict[str, str]], *, allow_handoff: bool = False) -> str | HandoffRequest:
        self.calls.append({"agent_id": agent["id"], "history": history, "allow_handoff": allow_handoff})
        if allow_handoff:
            return HandoffRequest("frontend", "A different retry objective")
        return "child answer" if len(self.calls) == 1 else "final answer"


def test_retry_uses_the_first_recorded_handoff_target_and_objective(tmp_path):
    url = f"sqlite:///{tmp_path / 'handoff-retry.db'}"
    planning_provider = DelegationCrashProvider()
    app = create_app(url, ProviderRegistry({"openai": planning_provider}))
    client = TestClient(app)
    parent = client.post("/agents", json={
        "name": "Parent", "instructions": "Delegate once.", "model_provider": "openai", "model_name": "test",
    }).json()
    backend = client.post("/agents", json={
        "name": "Backend", "instructions": "Do backend work.", "model_provider": "openai",
        "model_name": "test", "capabilities": ["backend"],
    }).json()
    frontend = client.post("/agents", json={
        "name": "Frontend", "instructions": "Do frontend work.", "model_provider": "openai",
        "model_name": "test", "capabilities": ["frontend"],
    }).json()
    conversation = client.post("/chat/conversations", json={"agent_id": parent["id"]}).json()
    run_id = client.post(
        f"/chat/conversations/{conversation['id']}/messages/async", json={"content": "Delegate once"}
    ).json()["id"]

    assert claim_one(app.state.runtime) == run_id
    with pytest.raises(SystemExit):
        app.state.runtime.execute_queued_run(run_id)
    app.state.database.dispose()

    retry_provider = ReplanningProvider()
    restarted = create_app(url, ProviderRegistry({"openai": retry_provider}))
    recover_interrupted_jobs(restarted.state.runtime)
    with restarted.state.database.session() as session:
        child = session.scalar(select(Task).where(
            Task.root_run_id == run_id, Task.parent_task_id.is_not(None)
        ))
        assert child.agent_id == backend["id"]
        assert child.objective == "First recorded objective"
        assert child.status == "queued"
    assert claim_one(restarted.state.runtime) == run_id
    restarted.state.runtime.execute_queued_run(run_id)

    result = restarted.state.runtime.get_run(run_id)
    assert result["status"] == "completed"
    # The retry never re-planned: no handoff-allowed call, and the original objective
    # is what the persisted target received.
    assert [call["allow_handoff"] for call in retry_provider.calls] == [False, False]
    assert retry_provider.calls[0]["history"] == [{"role": "user", "content": "First recorded objective"}]
    assert retry_provider.calls[1]["agent_id"] == parent["id"]
    assert frontend["id"] not in {call["agent_id"] for call in retry_provider.calls}
    with restarted.state.database.session() as session:
        children = session.scalars(select(Task).where(
            Task.root_run_id == run_id, Task.parent_task_id.is_not(None)
        )).all()
        assert len(children) == 1
        assert children[0].agent_id == backend["id"]
        assert children[0].objective == "First recorded objective"
        assert children[0].status == "completed"
        assert children[0].result == "child answer"
        replies = session.scalars(select(HumanChatMessage).where(
            HumanChatMessage.run_id == run_id, HumanChatMessage.kind == "agent_response"
        )).all()
        assert [reply.content for reply in replies] == ["final answer"]
    event_types = [event["type"] for event in result["events"]]
    assert "handoff_resumed" in event_types
    assert "delegated_task_resumed" in event_types
    restarted.state.database.dispose()
    client.close()


def test_worker_service_unit_keeps_the_bounded_stop_contract():
    # README: on a systemd stop only the worker process is signalled, it stops
    # claiming new work, and the in-flight model call gets at most 300 seconds
    # before the process group is killed. Keep the shipped unit in sync.
    unit = (
        Path(__file__).resolve().parents[1]
        / "deploy" / "systemd" / "user" / "agent-runtime-worker.service"
    ).read_text(encoding="utf-8")
    assert "KillMode=mixed" in unit
    assert "TimeoutStopSec=300" in unit
    assert "Restart=on-failure" in unit
