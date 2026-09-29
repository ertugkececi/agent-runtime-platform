from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import openai_codex
from fastapi.testclient import TestClient
from sqlalchemy import text

from agent_runtime_platform.api.app import create_app
from agent_runtime_platform.infrastructure.database import Database


def test_codex_model_catalog_exposes_supported_efforts(monkeypatch):
    class StubCodex:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def models(self):
            option = lambda effort: SimpleNamespace(reasoning_effort=SimpleNamespace(value=effort))
            return SimpleNamespace(data=[
                SimpleNamespace(model="gpt-6-sol", display_name="GPT-6-Sol",
                                is_default=True, default_reasoning_effort=SimpleNamespace(value="medium"),
                                supported_reasoning_efforts=[option("low"), option("medium"), option("high")])
            ])

    monkeypatch.setattr(openai_codex, "Codex", StubCodex)
    app = create_app(database_url="sqlite:///:memory:")
    with TestClient(app) as client:
        response = client.get("/codex/models")
    app.state.database.dispose()

    assert response.status_code == 200
    assert response.json() == [{
        "id": "gpt-6-sol", "label": "GPT-6-Sol", "is_default": True,
        "default_effort": "medium", "efforts": ["low", "medium", "high"],
    }]


def test_agent_effort_can_be_created_and_updated():
    app = create_app(database_url="sqlite:///:memory:")
    with TestClient(app) as client:
        response = client.post("/agents", json={
            "name": "Assistant", "instructions": "Answer.",
            "model_provider": "codex", "model_name": "gpt-6-sol",
            "model_reasoning_effort": "high",
        })
        assert response.status_code == 201, response.text
        agent = response.json()
        assert agent["model_reasoning_effort"] == "high"
        assert client.get("/agents").json()[0]["model_reasoning_effort"] == "high"
        changed = client.patch(
            f"/agents/{agent['id']}", json={"model_reasoning_effort": "xhigh"}
        )
        assert changed.status_code == 200
        assert changed.json()["model_reasoning_effort"] == "xhigh"
        assert client.patch(
            f"/agents/{agent['id']}", json={"model_reasoning_effort": "invalid"}
        ).status_code == 422
    app.state.database.dispose()


def test_existing_database_adds_effort_column_without_losing_agents(tmp_path):
    path = tmp_path / "existing.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE agents (id TEXT PRIMARY KEY, name TEXT)")
        connection.execute("INSERT INTO agents (id, name) VALUES ('legacy', 'Earlier agent')")

    for expected in [None, "high"]:
        database = Database(f"sqlite:///{path}")
        with database.engine.begin() as connection:
            row = connection.execute(
                text("SELECT name, model_reasoning_effort FROM agents WHERE id='legacy'")
            ).one()
            assert row == ("Earlier agent", expected)
            connection.execute(
                text("UPDATE agents SET model_reasoning_effort='high' WHERE id='legacy'")
            )
        database.dispose()
