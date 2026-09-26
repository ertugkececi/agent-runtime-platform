from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

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
