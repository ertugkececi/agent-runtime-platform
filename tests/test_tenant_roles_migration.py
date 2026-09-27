from __future__ import annotations

from sqlalchemy import inspect, text

import pytest

from agent_runtime_platform.tenant_migration import migrate as migrate_ownership
from agent_runtime_platform.tenant_roles_migration import (
    MigrationError,
    ROLE_REVISION,
    dry_run,
    migrate,
    validate_role_migration,
)
from test_tenant_migration import ISSUER, SUBJECT, TENANT, _database


def test_role_migration_requires_v1_and_is_read_only_when_dry_run(tmp_path):
    db = _database(tmp_path / "roles-prereq.db")
    before = db.engine.connect()
    before.close()
    with pytest.raises(MigrationError, match="tenant_ownership_v1"):
        dry_run(db.engine, ISSUER, SUBJECT, TENANT)
    migrate_ownership(db.engine, ISSUER, SUBJECT, TENANT, tmp_path / "v1-backup.db")
    report = dry_run(db.engine, ISSUER, SUBJECT, TENANT)
    assert report["revision"] == ROLE_REVISION
    assert report["mutated"] is False
    assert "tenant_memberships" not in inspect(db.engine).get_table_names()
    db.dispose()


def test_role_migration_backfills_admin_published_and_is_idempotent(tmp_path):
    db = _database(tmp_path / "roles-apply.db")
    migrate_ownership(db.engine, ISSUER, SUBJECT, TENANT, tmp_path / "v1-backup.db")
    report = migrate(db.engine, ISSUER, SUBJECT, TENANT, tmp_path / "roles-backup.db")
    assert report["status"] == "applied"
    validate_role_migration(db.engine, ISSUER, SUBJECT, TENANT)
    with db.engine.connect() as connection:
        membership = connection.execute(text(
            "SELECT tenant_id,oidc_issuer,oidc_subject,role,active FROM tenant_memberships"
        )).one()
        assert membership == (TENANT, ISSUER, SUBJECT, "admin", 1)
        assert connection.execute(text("SELECT COUNT(*) FROM agents WHERE published=1")).scalar_one() == 2
        for table in ("agents", "conversations", "rooms"):
            assert any(fk["referred_table"] == "tenant_memberships"
                       for fk in inspect(connection).get_foreign_keys(table))
    repeated = migrate(db.engine, ISSUER, SUBJECT, TENANT, tmp_path / "unused.db")
    assert repeated["status"] == "already-applied"
    assert not (tmp_path / "unused.db").exists()
    db.dispose()


def test_role_root_guard_rejects_foreign_or_inactive_membership(tmp_path):
    db = _database(tmp_path / "roles-guards.db")
    migrate_ownership(db.engine, ISSUER, SUBJECT, TENANT, tmp_path / "v1-backup.db")
    migrate(db.engine, ISSUER, SUBJECT, TENANT, tmp_path / "roles-backup.db")
    validate_role_migration(db.engine, ISSUER, SUBJECT, TENANT)
    with db.engine.connect() as connection:
        owner_id = connection.execute(text(
            "SELECT id FROM tenant_memberships WHERE oidc_issuer=:issuer AND oidc_subject=:subject"
        ), {"issuer": ISSUER, "subject": SUBJECT}).scalar_one()
    with db.engine.begin() as connection:
        with pytest.raises(Exception, match="tenant membership required"):
            connection.execute(text(
                "INSERT INTO agents(id,name,description,instructions,model_provider,model_name,"
                "model_reasoning_effort,enabled,tool_ids,version,created_at,updated_at,published,"
                "tenant_id,owner_id) VALUES ('foreign','x','','x','openai','m',NULL,1,'[]',1,"
                "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,1,:tenant,:owner)"
            ), {"tenant": "tenant-b", "owner": owner_id})
    db.dispose()


def test_role_migration_transaction_rolls_back_rebuild_failure(tmp_path):
    db = _database(tmp_path / "roles-rollback.db")
    migrate_ownership(db.engine, ISSUER, SUBJECT, TENANT, tmp_path / "v1-backup.db")
    with db.engine.begin() as connection:
        connection.execute(text("CREATE TABLE __tenant_roles_v1_agents (payload TEXT)"))
        connection.execute(text("INSERT INTO __tenant_roles_v1_agents VALUES ('keep')"))
    before = __import__("agent_runtime_platform.tenant_migration", fromlist=["snapshot"]).snapshot(db.engine)
    backup = tmp_path / "roles-backup.db"
    with pytest.raises(MigrationError, match="Reserved table"):
        migrate(db.engine, ISSUER, SUBJECT, TENANT, backup)
    assert backup.exists()
    assert __import__("agent_runtime_platform.tenant_migration", fromlist=["snapshot"]).snapshot(db.engine) == before
    assert "tenant_memberships" not in inspect(db.engine).get_table_names()
    assert "published" not in {column["name"] for column in inspect(db.engine).get_columns("agents")}
    db.dispose()


def test_role_migration_rejects_schema_drift_before_backup(tmp_path):
    db = _database(tmp_path / "roles-drift.db")
    migrate_ownership(db.engine, ISSUER, SUBJECT, TENANT, tmp_path / "v1-backup.db")
    with db.engine.begin() as connection:
        connection.execute(text('DROP TRIGGER "trg_agents_legacy_owner_insert"'))
    before = __import__("agent_runtime_platform.tenant_migration", fromlist=["snapshot"]).snapshot(db.engine)
    backup = tmp_path / "should-not-exist.db"
    with pytest.raises(RuntimeError, match="write guards"):
        dry_run(db.engine, ISSUER, SUBJECT, TENANT)
    assert not backup.exists()
    assert __import__("agent_runtime_platform.tenant_migration", fromlist=["snapshot"]).snapshot(db.engine) == before
    db.dispose()
