from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from agent_runtime_platform.domain.models import Base


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        options: dict = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            options["connect_args"] = {"check_same_thread": False}
            if ":memory:" in url:
                options["poolclass"] = StaticPool
            else:
                database_path = url.partition("///")[2].split("?", maxsplit=1)[0]
                if database_path and database_path != ":memory:":
                    Path(database_path).expanduser().parent.mkdir(parents=True, exist_ok=True)

        self.engine: Engine = create_engine(url, **options)
        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._enable_sqlite_foreign_keys)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False, class_=Session)
        Base.metadata.create_all(self.engine)
        # Existing installations were created before agents had effort and MCP grants.
        agent_columns = {column["name"] for column in inspect(self.engine).get_columns("agents")}
        with self.engine.begin() as connection:
            if "model_reasoning_effort" not in agent_columns:
                connection.execute(text("ALTER TABLE agents ADD COLUMN model_reasoning_effort VARCHAR(16)"))
            if "tool_ids" not in agent_columns:
                connection.execute(text("ALTER TABLE agents ADD COLUMN tool_ids JSON NOT NULL DEFAULT '[]'"))
            task_columns = {column["name"] for column in inspect(self.engine).get_columns("tasks")}
            for name, ddl in (
                ("remote_target_id", "VARCHAR(80)"),
                ("remote_message_id", "VARCHAR(36)"),
                ("remote_task_id", "VARCHAR(256)"),
                ("remote_status", "VARCHAR(32)"),
            ):
                if name not in task_columns:
                    connection.execute(text(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}"))

    @staticmethod
    def _enable_sqlite_foreign_keys(connection, _record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    def session(self) -> Session:
        return self.session_factory()

    def dispose(self) -> None:
        self.engine.dispose()
