from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

import agent_runtime_platform.infrastructure.tenant_migration as migration
from agent_runtime_platform.infrastructure.database import Database
from agent_runtime_platform.domain.models import (
    Agent, AgentCapability, Conversation, ConversationMember, HumanChatMessage,
    HumanChatRun, HumanChatRunEvent, HumanChatSession, Message, QueueJob, Room,
    RoomParticipant, RoomRun, RoomRunEvent, RoomRunTurn, Run, RunEvent, Task,
)
from agent_runtime_platform.infrastructure.tenant_migration import MigrationError, dry_run, migrate, snapshot


ISSUER = "https://login.example.test/"
SUBJECT = "stable-subject-7"
TENANT = "legacy"


def _database(path: Path, *, corrupt_queue: str | None = None) -> Database:
    db = Database(f"sqlite:///{path}")
    with db.session() as session:
        first = Agent(name="first", description="", instructions="test", model_provider="openai",
                      model_name="test-model", enabled=True, tool_ids=[], version=1)
        second = Agent(name="second", description="", instructions="test", model_provider="openai",
                       model_name="test-model", enabled=True, tool_ids=[], version=1)
        conversation = Conversation(status="open", next_message_sequence=1)
        room = Room(name="fixture room")
        session.add_all([first, second, conversation, room])
        session.flush()
        session.add_all([
            AgentCapability(agent_id=first.id, capability="review"),
            AgentCapability(agent_id=second.id, capability="code"),
            ConversationMember(conversation_id=conversation.id, agent_id=first.id),
            ConversationMember(conversation_id=conversation.id, agent_id=second.id),
            HumanChatSession(conversation_id=conversation.id, agent_id=first.id),
            RoomParticipant(room_id=room.id, agent_id=first.id, position=0,
                            is_moderator=True, config_snapshot={"id": first.id}),
            RoomParticipant(room_id=room.id, agent_id=second.id, position=1,
                            is_moderator=False, config_snapshot={"id": second.id}),
        ])
        run = Run(conversation_id=conversation.id, source_agent_id=first.id, target_agent_id=second.id,
                  source_config_snapshot={"id": first.id}, target_config_snapshot={"id": second.id})
        human_run = HumanChatRun(conversation_id=conversation.id, target_agent_id=first.id,
                                 target_config_snapshot={"id": first.id})
        room_run = RoomRun(room_id=room.id, content="question", agent_snapshots=[{"id": first.id}])
        session.add_all([run, human_run, room_run])
        session.flush()
        session.add_all([
            Message(run_id=run.id, conversation_id=conversation.id, sequence=1,
                    sender_agent_id=first.id, recipient_agent_id=second.id,
                    content="hello", kind="message"),
            RunEvent(run_id=run.id, sequence=1, event_type="started", payload={}),
            HumanChatMessage(run_id=human_run.id, conversation_id=conversation.id, sequence=1,
                             sender_type="user", agent_id=None, content="hello", kind="message"),
            HumanChatRunEvent(run_id=human_run.id, sequence=1, event_type="started", payload={}),
            Task(root_run_id=human_run.id, parent_task_id=None, conversation_id=conversation.id,
                 agent_id=first.id, capability=None, objective="fixture", config_snapshot={}),
            RoomRunTurn(run_id=room_run.id, agent_id=first.id, position=0,
                        is_moderator=True, status="completed", content="done"),
            RoomRunEvent(run_id=room_run.id, sequence=1, event_type="started", payload={}),
            QueueJob(run_id=run.id, status="pending", attempts=0, max_attempts=3),
            QueueJob(run_id=human_run.id, status="pending", attempts=0, max_attempts=3),
            QueueJob(run_id=room_run.id, status="pending", attempts=0, max_attempts=3),
        ])
        session.flush()
        if corrupt_queue == "orphan":
            session.add(QueueJob(run_id="not-a-run", status="pending", attempts=0, max_attempts=3))
        elif corrupt_queue == "ambiguous":
            ambiguous = HumanChatRun(id=run.id, conversation_id=conversation.id, target_agent_id=first.id,
                                     target_config_snapshot={"id": first.id})
            session.add(ambiguous)
            session.flush()
        session.commit()
    return db


def _scalar(db: Database, query: str, params: dict | None = None):
    with db.engine.connect() as connection:
        return connection.execute(text(query), params or {}).scalar_one()


def _apply(db: Database, backup: Path):
    return migrate(db.engine, ISSUER, SUBJECT, TENANT, backup)


def test_dry_run_is_read_only_and_reports_counts_checksum(tmp_path):
    db = _database(tmp_path / "before.db")
    before = snapshot(db.engine)
    report = dry_run(db.engine, ISSUER, SUBJECT, TENANT)
    assert report["mutated"] is False
    assert report["snapshot"] == before
    readonly = migration._read_only_engine(db.engine)
    try:
        with pytest.raises(Exception, match="readonly|read-only"):
            with readonly.begin() as connection:
                connection.execute(text("UPDATE agents SET name='should-not-write'"))
    finally:
        readonly.dispose()
    assert report["legacy_rows_to_backfill"]["agents"] == 2
    assert report["legacy_rows_to_backfill"]["conversations"] == 1
    assert report["legacy_rows_to_backfill"]["rooms"] == 1
    assert snapshot(db.engine) == before
    db.dispose()


def test_reserved_rebuild_table_collision_fails_before_backup_or_mutation(tmp_path):
    db = _database(tmp_path / "collision.db")
    sentinel_name = "__tenant_v1_agents"
    with db.engine.begin() as connection:
        connection.execute(text(f'CREATE TABLE "{sentinel_name}" (payload TEXT NOT NULL)'))
        connection.execute(text(f'INSERT INTO "{sentinel_name}" VALUES (:value)'), {"value": "keep-me"})
    before = snapshot(db.engine)
    backup = tmp_path / "must-not-exist.db"

    with pytest.raises(MigrationError, match="Reserved SQLite rebuild name"):
        _apply(db, backup)

    assert not backup.exists()
    assert snapshot(db.engine) == before
    with db.engine.connect() as connection:
        assert connection.execute(text(f'SELECT payload FROM "{sentinel_name}"')).scalar_one() == "keep-me"
    db.dispose()


def test_apply_backfills_all_parent_families_backup_restore_and_is_idempotent(tmp_path):
    db = _database(tmp_path / "source.db")
    backup_path = tmp_path / "backup.db"
    pre_migration = snapshot(db.engine)
    report = _apply(db, backup_path)
    assert report["status"] == "applied"
    assert backup_path.stat().st_mode & 0o777 == 0o600
    restored = sqlite3.connect(backup_path)
    try:
        assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert restored.execute("SELECT COUNT(*) FROM agents").fetchone() == (2,)
        assert restored.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='tenants'"
        ).fetchone() == (0,)
    finally:
        restored.close()
    restored_engine = create_engine(f"sqlite:///{backup_path}")
    try:
        assert snapshot(restored_engine) == pre_migration
    finally:
        restored_engine.dispose()
    # Restore drill with the old application model: service schema remains usable and
    # the untouched backup artifact itself is not modified.
    restored_copy = tmp_path / "restored-old-db.db"
    shutil.copyfile(backup_path, restored_copy)
    old_app_db = Database(f"sqlite:///{restored_copy}")
    with old_app_db.session() as session:
        session.add(Agent(name="restore-check", description="", instructions="test",
                          model_provider="openai", model_name="test", enabled=True,
                          tool_ids=[], version=1))
        session.commit()
    assert "tenant_migration_versions" not in inspect(old_app_db.engine).get_table_names()
    assert "tenant_id" not in {c["name"] for c in inspect(old_app_db.engine).get_columns("agents")}
    old_app_db.dispose()

    # Physical NOT NULL, per-root composite FK and exact single-owner row checks.
    for table in ("agents", "conversations", "rooms"):
        columns = {column["name"]: column for column in inspect(db.engine).get_columns(table)}
        assert columns["tenant_id"]["nullable"] is False
        assert columns["owner_id"]["nullable"] is False
        assert any(fk["constrained_columns"] == ["owner_id", "tenant_id"]
                   for fk in inspect(db.engine).get_foreign_keys(table))
        assert _scalar(db, f"SELECT COUNT(*) FROM {table} WHERE tenant_id IS NULL OR owner_id IS NULL") == 0

    # Every persistent child family resolves scope through its documented parent path.
    checks = [
        ("agent_capabilities", "agent_capabilities x JOIN agents p ON p.id=x.agent_id"),
        ("conversation_members", "conversation_members x JOIN conversations p ON p.id=x.conversation_id"),
        ("messages", "messages x JOIN conversations p ON p.id=x.conversation_id"),
        ("runs", "runs x JOIN conversations p ON p.id=x.conversation_id"),
        ("run_events", "run_events x JOIN runs r ON r.id=x.run_id JOIN conversations p ON p.id=r.conversation_id"),
        ("human_chat_sessions", "human_chat_sessions x JOIN conversations p ON p.id=x.conversation_id"),
        ("human_chat_runs", "human_chat_runs x JOIN conversations p ON p.id=x.conversation_id"),
        ("human_chat_messages", "human_chat_messages x JOIN conversations p ON p.id=x.conversation_id"),
        ("human_chat_run_events", "human_chat_run_events x JOIN human_chat_runs r ON r.id=x.run_id JOIN conversations p ON p.id=r.conversation_id"),
        ("tasks", "tasks x JOIN conversations p ON p.id=x.conversation_id"),
        ("room_participants", "room_participants x JOIN rooms p ON p.id=x.room_id"),
        ("room_runs", "room_runs x JOIN rooms p ON p.id=x.room_id"),
        ("room_run_turns", "room_run_turns x JOIN room_runs r ON r.id=x.run_id JOIN rooms p ON p.id=r.room_id"),
        ("room_run_events", "room_run_events x JOIN room_runs r ON r.id=x.run_id JOIN rooms p ON p.id=r.room_id"),
    ]
    for table, join in checks:
        expected = 2 if table in {"agent_capabilities", "conversation_members", "room_participants"} else 1
        assert _scalar(db, f"SELECT COUNT(*) FROM {join} WHERE p.tenant_id=:tenant AND p.owner_id IS NOT NULL",
                       {"tenant": TENANT}) == expected, table
    # The queue has a polymorphic run_id and no FK; all three queue kinds resolve uniquely.
    assert _scalar(db, """
        SELECT COUNT(*) FROM queue_jobs q JOIN (
          SELECT r.id run_id,c.tenant_id,c.owner_id FROM runs r JOIN conversations c ON c.id=r.conversation_id
          UNION ALL SELECT r.id,c.tenant_id,c.owner_id FROM human_chat_runs r JOIN conversations c ON c.id=r.conversation_id
          UNION ALL SELECT r.id,rm.tenant_id,rm.owner_id FROM room_runs r JOIN rooms rm ON rm.id=r.room_id
        ) p ON p.run_id=q.run_id WHERE p.tenant_id=:tenant AND p.owner_id IS NOT NULL
    """, {"tenant": TENANT}) == 3

    # Current single-user ORM inserts omit ownership columns; DB defaults bind only this exact owner.
    with db.session() as session:
        new_agent = Agent(name="new", description="", instructions="test", model_provider="openai",
                          model_name="test-model", enabled=True, tool_ids=[], version=1)
        new_conversation = Conversation(status="open", next_message_sequence=0)
        new_room = Room(name="new room")
        session.add_all([new_agent, new_conversation, new_room])
        session.commit()
        for table, identity in (("agents", new_agent.id), ("conversations", new_conversation.id), ("rooms", new_room.id)):
            assert _scalar(db, f"SELECT tenant_id=:tenant AND owner_id IS NOT NULL FROM {table} WHERE id=:id",
                           {"id": identity, "tenant": TENANT}) == 1

    second = _apply(db, tmp_path / "must-not-be-created.db")
    assert second["status"] == "already-applied"
    assert not (tmp_path / "must-not-be-created.db").exists()
    assert snapshot(db.engine)["checksum_sha256"] != pre_migration["checksum_sha256"]
    db.dispose()


@pytest.mark.parametrize("corrupt_queue", ["orphan", "ambiguous"])
def test_dry_run_and_apply_reject_bad_queue_graph_without_mutation(tmp_path, corrupt_queue):
    db = _database(tmp_path / f"{corrupt_queue}.db", corrupt_queue=corrupt_queue)
    before = snapshot(db.engine)
    with pytest.raises(MigrationError, match="queue jobs"):
        dry_run(db.engine, ISSUER, SUBJECT, TENANT)
    with pytest.raises(MigrationError, match="queue jobs"):
        _apply(db, tmp_path / f"{corrupt_queue}-backup.db")
    assert snapshot(db.engine) == before
    assert not (tmp_path / f"{corrupt_queue}-backup.db").exists()
    db.dispose()


def test_wrong_owner_mapping_is_rejected_without_database_mutation(tmp_path):
    db = _database(tmp_path / "wrong-owner.db")
    _apply(db, tmp_path / "first-backup.db")
    before = snapshot(db.engine)
    with pytest.raises(MigrationError, match="different owner mapping"):
        migrate(db.engine, ISSUER, "different-subject", TENANT, tmp_path / "wrong-backup.db")
    assert snapshot(db.engine) == before
    assert not (tmp_path / "wrong-backup.db").exists()
    db.dispose()


def test_ownerless_or_foreign_new_writes_are_rejected_and_legacy_defaults_are_fixed(tmp_path):
    db = _database(tmp_path / "claims.db")
    _apply(db, tmp_path / "claims-backup.db")
    # Explicit partial or foreign claim cannot bypass configured owner defaults.
    with db.engine.connect() as connection:
        real_owner = connection.execute(text("SELECT id FROM tenant_owners")).scalar_one()
    invalid_pairs = [("'legacy'", "NULL"), ("NULL", f"'{real_owner}'"),
                     ("'foreign-tenant'", "'foreign-owner'")]
    for tenant_value, owner_value in invalid_pairs:
        with pytest.raises(Exception):
            with db.engine.begin() as connection:
                connection.execute(text(f"""
                    INSERT INTO conversations
                    (id,status,next_message_sequence,created_at,tenant_id,owner_id)
                    VALUES ('reject','open',0,CURRENT_TIMESTAMP,{tenant_value},{owner_value})
                """))
    assert _scalar(db, "SELECT COUNT(*) FROM conversations WHERE id='reject'") == 0
    db.dispose()


def test_sqlite_failure_rolls_back_schema_backfill_guards_and_version(tmp_path, monkeypatch):
    db = _database(tmp_path / "atomic.db")
    before = snapshot(db.engine)
    original = migration._create_write_guard
    def fail_after_schema_and_backfill(connection, dialect, tenant_id, owner_id):
        original(connection, dialect, tenant_id, owner_id)
        raise RuntimeError("injected failure before version commit")
    monkeypatch.setattr(migration, "_create_write_guard", fail_after_schema_and_backfill)
    with pytest.raises(RuntimeError, match="injected failure"):
        _apply(db, tmp_path / "atomic-backup.db")
    assert snapshot(db.engine) == before
    assert "tenants" not in set(inspect(db.engine).get_table_names())
    assert "tenant_migration_versions" not in set(inspect(db.engine).get_table_names())
    db.dispose()


def test_sqlite_backup_is_private_and_refuses_overwrite(tmp_path):
    db = _database(tmp_path / "source-backup.db")
    destination = tmp_path / "private.db"
    migration._backup_sqlite(db.engine, destination)
    assert destination.stat().st_mode & 0o777 == 0o600
    original_bytes = destination.read_bytes()
    with pytest.raises(FileExistsError):
        migration._backup_sqlite(db.engine, destination)
    assert destination.read_bytes() == original_bytes
    db.dispose()


def test_postgres_apply_is_fail_closed_without_service_test_gate(tmp_path):
    from sqlalchemy import create_engine
    engine = create_engine("postgresql+psycopg://user:secret@127.0.0.1:1/runtime")
    try:
        with pytest.raises(MigrationError, match="PostgreSQL apply is disabled"):
            migrate(engine, ISSUER, SUBJECT, TENANT, tmp_path / "must-not-exist.dump")
        assert not (tmp_path / "must-not-exist.dump").exists()
    finally:
        engine.dispose()


def test_postgres_dry_run_is_fail_closed_without_service_read_only_gate():
    engine = create_engine("postgresql+psycopg://user:secret@127.0.0.1:1/runtime")
    try:
        with pytest.raises(MigrationError, match="PostgreSQL dry-run is disabled"):
            dry_run(engine, ISSUER, SUBJECT, TENANT)
    finally:
        engine.dispose()


def test_tenant_identifier_rejects_sql_metacharacters_without_mutation(tmp_path):
    db = _database(tmp_path / "unsafe-tenant.db")
    before = snapshot(db.engine)
    with pytest.raises(MigrationError, match="Tenant ID"):
        dry_run(db.engine, ISSUER, SUBJECT, "legacy'; DROP TABLE agents;--")
    assert snapshot(db.engine) == before
    db.dispose()
