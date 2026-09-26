from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent_runtime_platform.api import create_app
from agent_runtime_platform.models import QueueJob, RoomRunTurn
from agent_runtime_platform.providers import ProviderRegistry
from agent_runtime_platform.queue_worker import claim_one, recover_interrupted_jobs


class RoomProvider:
    def __init__(self, outputs: list[str] | None = None, crash_on_call: int | None = None) -> None:
        self.outputs = outputs or []
        self.crash_on_call = crash_on_call
        self.calls: list[dict[str, Any]] = []

    def generate(self, agent, history, *, allow_handoff=False):
        self.calls.append({"agent": agent, "history": history, "allow_handoff": allow_handoff})
        if self.crash_on_call == len(self.calls):
            self.crash_on_call = None
            raise SystemExit("simulated worker process interruption")
        if self.outputs:
            return self.outputs.pop(0)
        return f"Contribution by {agent['name']}"


def setup_room(tmp_path: Path, provider: RoomProvider, count: int = 3):
    url = f"sqlite:///{tmp_path / 'rooms.db'}"
    app = create_app(url, ProviderRegistry({"openai": provider}))
    client = TestClient(app)
    agents = []
    for index in range(count):
        response = client.post("/agents", json={
            "name": f"Agent {index + 1}", "instructions": f"Instructions {index + 1}",
            "model_provider": "openai", "model_name": f"test-{index + 1}",
        })
        assert response.status_code == 201, response.text
        agents.append(response.json())
    return url, app, client, agents


def test_room_run_is_queued_then_runs_in_fixed_order_with_shared_context(tmp_path):
    provider = RoomProvider(["first view", "moderator view", "third view", "final summary"])
    _url, app, client, agents = setup_room(tmp_path, provider)
    created = client.post("/rooms", json={
        "name": "Design review",
        "participant_agent_ids": [item["id"] for item in agents],
        "moderator_agent_id": agents[1]["id"],
    })
    assert created.status_code == 201, created.text
    room = created.json()
    assert [item["position"] for item in room["participants"]] == [1, 2, 3]
    assert [item["agent_id"] for item in room["participants"]] == [item["id"] for item in agents]
    assert [item["is_moderator"] for item in room["participants"]] == [False, True, False]

    accepted = client.post(f"/rooms/{room['id']}/runs", json={"content": "Review this design"})
    assert accepted.status_code == 202, accepted.text
    run_id = accepted.json()["id"]
    assert accepted.json()["status_url"] == f"/runs/{run_id}"
    queued = client.get(f"/runs/{run_id}").json()
    assert queued["status"] == "queued"
    assert len(queued["turns"]) == 4
    assert claim_one(app.state.runtime) == run_id
    app.state.runtime.execute_queued_run(run_id)

    run = client.get(f"/runs/{run_id}").json()
    assert run["status"] == "completed"
    assert run["run_type"] == "room"
    assert [turn["content"] for turn in run["turns"]] == [
        "first view", "moderator view", "third view", "final summary"
    ]
    assert run["turns"][-1]["is_moderator"] is True
    assert run["turns"][-1]["is_summary"] is True
    assert run["turns"][-1]["phase"] == "moderator_summary"
    assert run["turns"][1]["phase"] == "participant"
    assert run["final_answer"] == "final summary"
    assert len(provider.calls) == 4
    assert all(call["allow_handoff"] is False for call in provider.calls)
    assert "Review this design" in provider.calls[0]["history"][0]["content"]
    assert "first view" in str(provider.calls[1]["history"])
    assert "first view" in str(provider.calls[2]["history"])
    assert "moderator view" in str(provider.calls[2]["history"])
    assert all(value in str(provider.calls[3]["history"]) for value in [
        "first view", "moderator view", "third view"
    ])
    assert [event["type"] for event in run["events"]].count("room_turn_completed") == 4
    assert len(client.get(f"/rooms/{room['id']}/runs").json()) == 1
    with app.state.database.session() as session:
        job = session.get(QueueJob, run_id)
        turns = session.scalars(select(RoomRunTurn).where(RoomRunTurn.run_id == run_id)).all()
        assert job.status == "completed"
        assert len(turns) == 4
    client.close()
    app.state.database.dispose()


def test_room_validation_rejects_duplicate_participants_and_nonparticipant_moderator(tmp_path):
    _url, app, client, agents = setup_room(tmp_path, RoomProvider())
    ids = [item["id"] for item in agents]
    duplicate = client.post("/rooms", json={
        "name": "Invalid", "participant_agent_ids": [ids[0], ids[0]],
        "moderator_agent_id": ids[0],
    })
    assert duplicate.status_code == 422
    bad_moderator = client.post("/rooms", json={
        "name": "Invalid", "participant_agent_ids": ids[:2],
        "moderator_agent_id": ids[2],
    })
    assert bad_moderator.status_code == 422
    too_few = client.post("/rooms", json={
        "name": "Invalid", "participant_agent_ids": [ids[0]],
        "moderator_agent_id": ids[0],
    })
    assert too_few.status_code == 422
    missing_agent = client.post("/rooms", json={
        "name": "Invalid", "participant_agent_ids": [ids[0], "not-a-registered-agent"],
        "moderator_agent_id": ids[0],
    })
    assert missing_agent.status_code == 404
    too_many = client.post("/rooms", json={
        "name": "Invalid", "participant_agent_ids": ids + ["x", "y", "z"],
        "moderator_agent_id": ids[0],
    })
    assert too_many.status_code == 422
    disabled = client.patch(f"/agents/{ids[0]}", json={"enabled": False})
    assert disabled.status_code == 200
    disabled_participant = client.post("/rooms", json={
        "name": "Invalid", "participant_agent_ids": ids[:2],
        "moderator_agent_id": ids[0],
    })
    assert disabled_participant.status_code == 409
    client.close()
    app.state.database.dispose()


def test_interrupted_room_run_resumes_without_duplicating_completed_turn(tmp_path):
    provider = RoomProvider(crash_on_call=3)
    url, app, client, agents = setup_room(tmp_path, provider, count=2)
    room = client.post("/rooms", json={
        "name": "Resume room", "participant_agent_ids": [item["id"] for item in agents],
        "moderator_agent_id": agents[0]["id"],
    }).json()
    accepted = client.post(f"/rooms/{room['id']}/runs", json={"content": "Continue safely"})
    run_id = accepted.json()["id"]
    assert claim_one(app.state.runtime) == run_id
    with pytest.raises(SystemExit):
        app.state.runtime.execute_queued_run(run_id)
    partial = client.get(f"/runs/{run_id}").json()
    assert partial["status"] == "running"
    assert [turn["status"] for turn in partial["turns"]] == ["completed", "completed", "running"]

    original_snapshot = room["participants"][1]["agent_snapshot"]
    original_moderator_snapshot = room["participants"][0]["agent_snapshot"]
    changed = client.patch(f"/agents/{agents[0]['id']}", json={
        "name": "Updated after room creation", "instructions": "New instructions", "model_name": "new-model"
    })
    assert changed.status_code == 200
    app.state.database.dispose()
    restarted = create_app(url, ProviderRegistry({"openai": provider}))
    recover_interrupted_jobs(restarted.state.runtime)
    assert restarted.state.runtime.get_run(run_id)["status"] == "queued"
    assert claim_one(restarted.state.runtime) == run_id
    restarted.state.runtime.execute_queued_run(run_id)
    finished = restarted.state.runtime.get_run(run_id)
    assert finished["status"] == "completed"
    assert [turn["status"] for turn in finished["turns"]] == ["completed"] * 3
    assert len(provider.calls) == 4  # only the interrupted moderator call repeats
    assert provider.calls[3]["agent"]["name"] == original_moderator_snapshot["name"]
    assert provider.calls[3]["agent"]["model_name"] == original_moderator_snapshot["model_name"]
    assert provider.calls[3]["agent"]["instructions"] == "Instructions 1"
    assert [turn["phase"] for turn in finished["turns"]] == [
        "participant", "participant", "moderator_summary"
    ]
    assert sum(turn["phase"] == "moderator_summary" for turn in finished["turns"]) == 1
    assert finished["final_answer"] == finished["turns"][-1]["content"]
    assert [event["type"] for event in finished["events"]].count("room_turn_completed") == 3
    restarted.state.database.dispose()
    client.close()


def test_startup_recovery_fails_terminal_room_job_and_marks_unfinished_turns(tmp_path):
    from agent_runtime_platform.models import QueueJob

    provider = RoomProvider(crash_on_call=2)
    url, app, client, agents = setup_room(tmp_path, provider, count=2)
    room = client.post("/rooms", json={
        "name": "Terminal recovery", "participant_agent_ids": [item["id"] for item in agents],
        "moderator_agent_id": agents[0]["id"],
    }).json()
    run_id = client.post(f"/rooms/{room['id']}/runs", json={"content": "Stop after budget"}).json()["id"]
    assert claim_one(app.state.runtime) == run_id
    with pytest.raises(SystemExit):
        app.state.runtime.execute_queued_run(run_id)
    with app.state.database.session() as session:
        job = session.get(QueueJob, run_id)
        job.attempts = job.max_attempts
        session.commit()
    app.state.database.dispose()

    restarted = create_app(url, ProviderRegistry({"openai": provider}))
    recover_interrupted_jobs(restarted.state.runtime)
    failed = restarted.state.runtime.get_run(run_id)
    assert failed["status"] == "failed"
    assert failed["queue"]["status"] == "failed"
    assert [turn["status"] for turn in failed["turns"]] == ["completed", "failed", "failed"]
    restarted.state.database.dispose()
    client.close()
