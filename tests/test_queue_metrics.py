from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_runtime_platform.api.app import create_app
from agent_runtime_platform.infrastructure.providers import ProviderRegistry
from agent_runtime_platform.infrastructure.queue_metrics import collect_queue_metrics, main
from agent_runtime_platform.queue_worker import claim_one, recover_interrupted_jobs


class QueueProvider:
    """Fake provider that can fail or die on chosen call numbers."""

    def __init__(
        self,
        *,
        reply: str = "queued answer",
        fail_calls: set[int] | None = None,
        crash_calls: set[int] | None = None,
    ) -> None:
        self.calls = 0
        self.reply = reply
        self.fail_calls = set(fail_calls or ())
        self.crash_calls = set(crash_calls or ())

    def generate(
        self, agent: dict[str, Any], history: list[dict[str, str]], *, allow_handoff: bool = False
    ) -> str:
        self.calls += 1
        if self.calls in self.crash_calls:
            raise SystemExit("simulated hard termination during the model call")
        if self.calls in self.fail_calls:
            raise RuntimeError("ERROR-SENTINEL-DO-NOT-REPORT")
        return self.reply


def setup_chat(tmp_path: Path, provider: QueueProvider, name: str = "metrics.db"):
    url = f"sqlite:///{tmp_path / name}"
    app = create_app(url, ProviderRegistry({"openai": provider}))
    client = TestClient(app)
    agent = client.post("/agents", json={
        "name": "Metrics agent", "instructions": "Answer clearly.",
        "model_provider": "openai", "model_name": "test-model",
    }).json()
    conversation = client.post("/chat/conversations", json={"agent_id": agent["id"]}).json()
    return url, app, client, conversation["id"]


def enqueue_chat(client: TestClient, conversation_id: str, content: str) -> str:
    response = client.post(f"/chat/conversations/{conversation_id}/messages/async", json={"content": content})
    assert response.status_code == 202
    return response.json()["id"]


def test_report_summarizes_queue_and_model_call_workload(tmp_path):
    provider = QueueProvider(fail_calls={2})
    _url, app, client, conversation_id = setup_chat(tmp_path, provider)
    first = enqueue_chat(client, conversation_id, "First")
    second = enqueue_chat(client, conversation_id, "Second")

    assert claim_one(app.state.runtime) == first
    app.state.runtime.execute_queued_run(first)
    assert claim_one(app.state.runtime) == second
    app.state.runtime.execute_queued_run(second)  # Attempt 1 fails and requeues.
    assert app.state.runtime.get_run(second)["status"] == "queued"
    assert claim_one(app.state.runtime) == second
    app.state.runtime.execute_queued_run(second)

    report = collect_queue_metrics(app.state.database, window_hours=24)
    queue = report["queue"]

    assert queue["current"] == {"pending": 0, "running": 0, "oldest_pending_age_seconds": None}
    assert queue["workload"] == {
        "enqueued": 2,
        "completed": 2,
        "failed": 0,
        "terminal_failure_rate": 0.0,
        "by_type": {"human_chat": {"enqueued": 2, "completed": 2, "failed": 0}},
    }
    assert queue["wait_seconds"]["count"] == 2
    assert queue["processing_seconds"]["count"] == 2
    assert queue["end_to_end_seconds"]["count"] == 2
    assert queue["attempts"] == {"attempted_jobs": 2, "total_attempts": 3, "retried_jobs": 1}
    assert queue["recovery"] == {
        "recovered_jobs": 0,
        "resume_wait_seconds": {"count": 0, "p50": None, "p90": None, "p95": None, "p99": None, "max": None},
        "exhausted_jobs": 0,
    }
    assert queue["failures"] == {"by_error": {}}
    assert queue["backlog"]["seconds"] > 0
    assert queue["backlog"]["peak_pending"] >= 1
    assert queue["throughput"]["enqueued_per_hour"] > 0
    assert queue["worker"]["concurrency"] == 1
    assert len(queue["depth_series"]) >= 2

    model = report["model_calls"]
    assert model["completed"] == 2
    assert model["errors"] == 1
    assert model["interrupted"] == 0
    assert model["in_flight"] == 0
    assert model["duration_seconds"]["count"] == 2
    assert model["by_provider"]["openai"]["completed"] == 2
    assert model["by_provider"]["openai"]["errors"] == 1
    client.close()
    app.state.database.dispose()


def test_failed_run_counts_as_a_terminal_failure(tmp_path):
    provider = QueueProvider(fail_calls={1, 2, 3})
    _url, app, client, conversation_id = setup_chat(tmp_path, provider, name="failure.db")
    run_id = enqueue_chat(client, conversation_id, "Never finishes")

    for attempt in range(3):
        assert claim_one(app.state.runtime) == run_id
        app.state.runtime.execute_queued_run(run_id)
        assert app.state.runtime.get_run(run_id)["status"] == ("failed" if attempt == 2 else "queued")

    report = collect_queue_metrics(app.state.database, window_hours=24)
    assert report["queue"]["workload"]["failed"] == 1
    assert report["queue"]["workload"]["terminal_failure_rate"] == 1.0
    assert report["queue"]["failures"] == {"by_error": {"runtime_error": 1}}
    assert report["queue"]["attempts"] == {"attempted_jobs": 1, "total_attempts": 3, "retried_jobs": 1}
    assert report["model_calls"]["errors"] == 3
    assert report["model_calls"]["completed"] == 0
    client.close()
    app.state.database.dispose()


def test_report_carries_no_content_or_error_messages(tmp_path):
    provider = QueueProvider(reply="REPLY-SENTINEL", fail_calls={1})
    url = f"sqlite:///{tmp_path / 'privacy.db'}"
    app = create_app(url, ProviderRegistry({"openai": provider}))
    client = TestClient(app)
    agent = client.post("/agents", json={
        "name": "NAME-SENTINEL",
        "description": "DESCRIPTION-SENTINEL",
        "instructions": "INSTRUCTIONS-SENTINEL",
        "model_provider": "openai",
        "model_name": "MODEL-NAME-SENTINEL",
    }).json()
    conversation = client.post("/chat/conversations", json={"agent_id": agent["id"]}).json()
    run_id = enqueue_chat(client, conversation["id"], "REQUEST-SENTINEL")
    assert claim_one(app.state.runtime) == run_id
    app.state.runtime.execute_queued_run(run_id)  # Fails once, then retries.
    assert claim_one(app.state.runtime) == run_id
    app.state.runtime.execute_queued_run(run_id)

    report = collect_queue_metrics(app.state.database, window_hours=24)
    text = json.dumps(report, sort_keys=True)
    for sentinel in (
        "NAME-SENTINEL",
        "DESCRIPTION-SENTINEL",
        "INSTRUCTIONS-SENTINEL",
        "MODEL-NAME-SENTINEL",
        "REQUEST-SENTINEL",
        "REPLY-SENTINEL",
        "ERROR-SENTINEL",
    ):
        assert sentinel not in text
    # Raw event payloads (tool ids, agent ids, task ids) never enter the report.
    assert '"payload"' not in text
    client.close()
    app.state.database.dispose()


def test_recovered_interruption_is_reported_as_interrupted_not_error(tmp_path):
    provider = QueueProvider(crash_calls={1})
    url, app, client, conversation_id = setup_chat(tmp_path, provider, name="recovery.db")
    run_id = enqueue_chat(client, conversation_id, "Survive a kill")

    assert claim_one(app.state.runtime) == run_id
    with pytest.raises(SystemExit):
        app.state.runtime.execute_queued_run(run_id)
    app.state.database.dispose()

    restarted = create_app(url, ProviderRegistry({"openai": provider}))
    recover_interrupted_jobs(restarted.state.runtime)
    assert claim_one(restarted.state.runtime) == run_id
    restarted.state.runtime.execute_queued_run(run_id)

    report = collect_queue_metrics(restarted.state.database, window_hours=24)
    assert report["queue"]["workload"]["completed"] == 1
    assert report["queue"]["recovery"]["recovered_jobs"] == 1
    assert report["queue"]["recovery"]["resume_wait_seconds"]["count"] == 1
    assert report["queue"]["attempts"] == {"attempted_jobs": 1, "total_attempts": 2, "retried_jobs": 1}
    assert report["model_calls"]["completed"] == 1
    assert report["model_calls"]["interrupted"] == 1
    assert report["model_calls"]["errors"] == 0
    client.close()
    restarted.state.database.dispose()


def test_room_turns_are_reported_as_model_calls(tmp_path):
    provider = QueueProvider(reply="room answer")
    url = f"sqlite:///{tmp_path / 'room-metrics.db'}"
    app = create_app(url, ProviderRegistry({"openai": provider}))
    client = TestClient(app)
    agent_ids = [
        client.post("/agents", json={
            "name": f"Room agent {index}", "instructions": "Contribute.",
            "model_provider": "openai", "model_name": "test-model",
        }).json()["id"]
        for index in (1, 2)
    ]
    room = client.post("/rooms", json={
        "name": "Metrics room",
        "participant_agent_ids": agent_ids,
        "moderator_agent_id": agent_ids[0],
    }).json()
    queued = client.post(f"/rooms/{room['id']}/runs", json={"content": "Produce a summary"}).json()

    assert claim_one(app.state.runtime) == queued["id"]
    app.state.runtime.execute_queued_run(queued["id"])

    report = collect_queue_metrics(app.state.database, window_hours=24)
    assert report["queue"]["workload"]["by_type"]["room"] == {
        "enqueued": 1, "completed": 1, "failed": 0,
    }
    model = report["model_calls"]
    assert model["completed"] == 3
    assert model["errors"] == 0
    assert model["duration_seconds"]["count"] == 3
    assert model["by_phase"]["participant"]["completed"] == 2
    assert model["by_phase"]["moderator_summary"]["completed"] == 1
    client.close()
    app.state.database.dispose()


def test_current_depth_is_reported_outside_the_window(tmp_path):
    provider = QueueProvider()
    _url, app, client, conversation_id = setup_chat(tmp_path, provider, name="window.db")
    enqueue_chat(client, conversation_id, "Waiting")

    report = collect_queue_metrics(
        app.state.database, window_hours=0.001, now=datetime.now(timezone.utc) + timedelta(hours=1)
    )
    assert report["queue"]["current"]["pending"] == 1
    assert report["queue"]["current"]["oldest_pending_age_seconds"] is not None
    assert report["queue"]["workload"]["enqueued"] == 0
    assert report["model_calls"]["completed"] == 0

    with pytest.raises(ValueError):
        collect_queue_metrics(app.state.database, window_hours=0)
    client.close()
    app.state.database.dispose()


def test_cli_prints_the_report_as_json(tmp_path, capsys):
    provider = QueueProvider()
    url, app, client, conversation_id = setup_chat(tmp_path, provider, name="cli.db")
    run_id = enqueue_chat(client, conversation_id, "Report me")
    assert claim_one(app.state.runtime) == run_id
    app.state.runtime.execute_queued_run(run_id)

    assert main(["--database-url", url, "--window-hours", "24"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["queue"]["workload"]["enqueued"] == 1
    assert output["model_calls"]["completed"] == 1
    client.close()
    app.state.database.dispose()
