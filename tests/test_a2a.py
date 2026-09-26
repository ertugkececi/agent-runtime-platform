from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, inspect, text

from agent_runtime_platform.a2a import A2AError, configured_targets
from agent_runtime_platform.database import Database


def test_existing_sqlite_tasks_table_gets_remote_delegation_columns(tmp_path):
    path = tmp_path / "existing.db"
    url = f"sqlite:///{path}"
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE tasks (id VARCHAR(36) PRIMARY KEY)"))
    engine.dispose()

    upgraded = Database(url)
    columns = {item["name"] for item in inspect(upgraded.engine).get_columns("tasks")}
    assert {"remote_target_id", "remote_message_id", "remote_task_id", "remote_status"} <= columns
    upgraded.dispose()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_timeout_seconds", "inf"),
        ("max_wait_seconds", "NaN"),
        ("poll_interval_seconds", "-inf"),
    ],
)
def test_non_finite_admin_timeouts_are_rejected(monkeypatch, field, value):
    monkeypatch.setenv("AGENT_RUNTIME_A2A_TARGETS", json.dumps([{
        "id": "target", "url": "http://127.0.0.1", "allow_private": True,
        "capabilities": ["research"], field: value,
    }]))
    with pytest.raises(A2AError, match="invalid_admin_configuration"):
        configured_targets()
