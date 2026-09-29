"""Explicit offline migration from legacy ownership to tenant roles.

This migration is never invoked at application startup.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from agent_runtime_platform.tenant_migration import (
    REVISION as OWNERSHIP_REVISION,
    MigrationError,
    _backup,
    _read_only_engine,
    _validate_owned_roots,
    snapshot,
)

ROLE_REVISION = "tenant_roles_v1"
ROOTS = ("agents", "conversations", "rooms")


def _digest(issuer: str, subject: str, tenant_id: str) -> str:
    return hashlib.sha256((ROLE_REVISION + "\0" + issuer + "\0" + subject + "\0" + tenant_id).encode()).hexdigest()


def _require_v1(connection, issuer: str, subject: str, tenant_id: str) -> str:
    if not {"tenant_owners", "tenants", "tenant_migration_versions"} <= set(inspect(connection).get_table_names()):
        raise MigrationError("A verified tenant_ownership_v1 migration is required.")
    rows = connection.execute(text(
        "SELECT id,tenant_id FROM tenant_owners WHERE oidc_issuer=:issuer AND oidc_subject=:subject"
    ), {"issuer": issuer, "subject": subject}).all()
    if len(rows) != 1 or rows[0].tenant_id != tenant_id:
        raise MigrationError("Role migration requires the unique configured legacy owner mapping.")
    expected = hashlib.sha256((issuer + "\0" + subject + "\0" + tenant_id).encode()).hexdigest()
    applied = connection.execute(text(
        "SELECT mapping_sha256 FROM tenant_migration_versions WHERE revision=:revision"
    ), {"revision": OWNERSHIP_REVISION}).scalar_one_or_none()
    if applied != expected:
        raise MigrationError("A verified tenant_ownership_v1 mapping is required.")
    owner_id = rows[0].id
    role_digest = connection.execute(text(
        "SELECT mapping_sha256 FROM tenant_migration_versions WHERE revision=:revision"
    ), {"revision": ROLE_REVISION}).scalar_one_or_none()
    if role_digest is None:
        _validate_owned_roots(connection, tenant_id, owner_id)
    elif role_digest != _digest(issuer, subject, tenant_id):
        raise MigrationError("Stored tenant_roles_v1 marker does not match the configured owner mapping.")
    return owner_id


def _assert_legacy_guards(connection, tenant_id: str, owner_id: str) -> None:
    # Reuse the exact validator that gates legacy_owner resource authorization.
    from agent_runtime_platform.resource_auth import ResourceAuthorization
    ResourceAuthorization._validate_schema(connection, tenant_id, owner_id)


def _create_memberships(connection, owner_id: str, tenant_id: str, issuer: str, subject: str) -> None:
    connection.exec_driver_sql("""
      CREATE TABLE tenant_memberships (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        tenant_id VARCHAR(80) NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        oidc_issuer VARCHAR(2048) NOT NULL,
        oidc_subject VARCHAR(1024) NOT NULL,
        role VARCHAR(16) NOT NULL CHECK (role IN ('admin','member')),
        active BOOLEAN NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
        created_at TIMESTAMP NOT NULL,
        UNIQUE (id, tenant_id),
        UNIQUE (tenant_id, oidc_issuer, oidc_subject)
      )
    """)
    connection.execute(text("""
      INSERT INTO tenant_memberships
        (id,tenant_id,oidc_issuer,oidc_subject,role,active,created_at)
      VALUES (:id,:tenant,:issuer,:subject,'admin',1,:created)
    """), {"id": owner_id, "tenant": tenant_id, "issuer": issuer, "subject": subject,
           "created": datetime.now(timezone.utc).replace(tzinfo=None)})
    connection.exec_driver_sql(
      "CREATE INDEX ix_tenant_memberships_principal ON tenant_memberships(oidc_issuer,oidc_subject,active)"
    )
    connection.exec_driver_sql(
      "CREATE INDEX ix_tenant_memberships_tenant_role ON tenant_memberships(tenant_id,role,active)"
    )
def _rebuild_root(connection, table: str) -> None:
    inspector = inspect(connection)
    info = inspector.get_columns(table)
    names = [item["name"] for item in info]
    if not {"tenant_id", "owner_id"} <= set(names):
        raise MigrationError(f"{table} does not have the complete v1 ownership schema.")
    indexes = connection.execute(text(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=:table AND sql IS NOT NULL"
    ), {"table": table}).scalars().all()
    pk = inspector.get_pk_constraint(table).get("constrained_columns") or []
    if len(pk) != 1:
        raise MigrationError(f"{table} has an unexpected primary key.")
    temporary = "__tenant_roles_v1_" + table
    if connection.execute(text("SELECT 1 FROM sqlite_master WHERE name=:n"), {"n": temporary}).first():
        raise MigrationError(f"Reserved table {temporary} already exists.")
    definitions = []
    for column in info:
        part = f'"{column["name"]}" {column["type"]}'
        if not column["nullable"]:
            part += " NOT NULL"
        if column.get("default") is not None:
            part += " DEFAULT " + str(column["default"])
        definitions.append(part)
    if table == "agents" and "published" in names:
        definitions.append("CHECK (published IN (0,1))")
    definitions.append("FOREIGN KEY (owner_id,tenant_id) REFERENCES tenant_memberships(id,tenant_id)")
    definitions.append("PRIMARY KEY (" + ",".join(f'"{item}"' for item in pk) + ")")
    connection.exec_driver_sql(f'CREATE TABLE "{temporary}" (' + ",".join(definitions) + ")")
    quoted = ",".join(f'"{name}"' for name in names)
    connection.exec_driver_sql(
        f'INSERT INTO "{temporary}" ({quoted}) SELECT {quoted} FROM "{table}"'
    )
    connection.exec_driver_sql(f'DROP TABLE "{table}"')
    connection.exec_driver_sql(f'ALTER TABLE "{temporary}" RENAME TO "{table}"')
    for statement in indexes:
        connection.exec_driver_sql(statement)
    connection.exec_driver_sql(
        f'CREATE INDEX IF NOT EXISTS "ix_{table}_tenant_owner" ON "{table}"(tenant_id,owner_id)'
    )


def _install_membership_guards(connection, legacy_owner_id: str, tenant_id: str) -> None:
    # ORM INSERTs can transiently use the legacy physical default. This exception is
    # limited to that exact owner+tenant and INSERT only; app code must rebind and
    # verify an active membership before commit. Every ownership UPDATE is active-only.
    for table in ROOTS:
        for suffix, event in (
            ("insert", "INSERT"),
            ("update", "UPDATE OF tenant_id,owner_id"),
        ):
            name = f"trg_{table}_tenant_membership_{suffix}"
            connection.exec_driver_sql(f'DROP TRIGGER IF EXISTS "{name}"')
            active_or_legacy = (
                "m.active=1 OR (NEW.owner_id='" + legacy_owner_id.replace("'", "''") +
                "' AND NEW.tenant_id='" + tenant_id.replace("'", "''") + "')"
                if suffix == "insert" else "m.active=1"
            )
            connection.exec_driver_sql(f"""
              CREATE TRIGGER "{name}" BEFORE {event} ON "{table}"
              WHEN NEW.tenant_id IS NULL OR NEW.owner_id IS NULL OR NOT EXISTS (
                SELECT 1 FROM tenant_memberships m
                WHERE m.id=NEW.owner_id AND m.tenant_id=NEW.tenant_id AND ({active_or_legacy})
              )
              BEGIN SELECT RAISE(ABORT,'tenant membership required'); END
            """)


def _validate_role_schema(connection, issuer: str, subject: str, tenant_id: str, legacy_owner_id: str) -> None:
    inspector = inspect(connection)
    if not {"tenant_memberships", "tenant_migration_versions"} <= set(inspector.get_table_names()):
        raise MigrationError("Tenant role schema is incomplete.")
    columns = {column["name"]: column for column in inspector.get_columns("agents")}
    published = columns.get("published")
    if published is None or published["nullable"] or str(published.get("default")).strip("()'") != "1":
        raise MigrationError("agents.published must be NOT NULL with physical default true.")
    table_sql = connection.execute(text(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='tenant_memberships'"
    )).scalar_one_or_none() or ""
    compact = " ".join(table_sql.lower().split())
    if "check (role in ('admin','member'))" not in compact or "check (active in (0,1))" not in compact:
        raise MigrationError("Tenant membership role/active CHECK constraints are missing.")
    unique = {tuple(item.get("column_names") or ()) for item in inspector.get_unique_constraints("tenant_memberships")}
    if {("id","tenant_id"),("tenant_id","oidc_issuer","oidc_subject")} - unique:
        raise MigrationError("Tenant membership composite uniqueness constraints are missing.")
    if not any(fk["constrained_columns"] == ["tenant_id"] and fk["referred_table"] == "tenants"
               for fk in inspector.get_foreign_keys("tenant_memberships")):
        raise MigrationError("Tenant membership tenant foreign key is missing.")
    membership_columns = {column["name"]: column for column in inspector.get_columns("tenant_memberships")}
    required_membership = {"tenant_id", "oidc_issuer", "oidc_subject", "role", "active", "created_at"}
    if (not required_membership <= set(membership_columns)
            or inspector.get_pk_constraint("tenant_memberships").get("constrained_columns") != ["id"]
            or any(membership_columns[name]["nullable"] for name in required_membership)
            or str(membership_columns["active"].get("default")).strip("()'") != "1"):
        raise MigrationError("Tenant membership required columns/defaults drifted.")
    membership_indexes = {index.get("name"): tuple(index.get("column_names") or ())
                          for index in inspector.get_indexes("tenant_memberships")}
    if membership_indexes.get("ix_tenant_memberships_principal") != ("oidc_issuer", "oidc_subject", "active") or membership_indexes.get(
        "ix_tenant_memberships_tenant_role"
    ) != ("tenant_id", "role", "active"):
        raise MigrationError("Tenant membership index columns/order drifted.")
    agents_sql = connection.execute(text(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='agents'"
    )).scalar_one_or_none() or ""
    if "check (published in (0,1))" not in " ".join(agents_sql.lower().split()):
        raise MigrationError("agents.published CHECK constraint is missing.")
    for table in ROOTS:
        root_columns = {column["name"]: column for column in inspector.get_columns(table)}
        if any(root_columns.get(name) is None or root_columns[name]["nullable"] for name in ("tenant_id", "owner_id")):
            raise MigrationError(f"{table} tenant_id/owner_id must remain NOT NULL.")
        expected_defaults = {"tenant_id": tenant_id, "owner_id": legacy_owner_id}
        for name, expected_default in expected_defaults.items():
            actual = str(root_columns[name].get("default") or "").strip("()'\\\"")
            if actual != expected_default:
                raise MigrationError(f"{table}.{name} physical compatibility default drifted.")
        indexes = {index.get("name"): tuple(index.get("column_names") or ())
                   for index in inspector.get_indexes(table)}
        if indexes.get(f"ix_{table}_tenant_owner") != ("tenant_id", "owner_id"):
            raise MigrationError(f"{table} tenant-owner index columns/order drifted.")
        invalid_roots = connection.execute(text(
            f"SELECT COUNT(*) FROM \"{table}\" r LEFT JOIN tenant_memberships m "
            "ON m.id=r.owner_id AND m.tenant_id=r.tenant_id "
            "WHERE r.owner_id IS NULL OR r.tenant_id IS NULL OR m.id IS NULL"
        )).scalar_one()
        if invalid_roots:
            raise MigrationError(f"{table} contains roots without a valid tenant owner mapping.")
        if not any(fk["constrained_columns"] == ["owner_id","tenant_id"]
                   and fk["referred_table"] == "tenant_memberships"
                   and fk["referred_columns"] == ["id","tenant_id"]
                   for fk in inspector.get_foreign_keys(table)):
            raise MigrationError(f"{table} lacks its tenant-membership composite foreign key.")
        triggers = connection.execute(text(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=:table"
        ), {"table": table}).all()
        expected = {f"trg_{table}_tenant_membership_{kind}" for kind in ("insert","update")}
        if {item.name for item in triggers} != expected:
            raise MigrationError(f"{table} membership guards are missing or unexpected.")
        for item in triggers:
            suffix = "insert" if item.name.endswith("_insert") else "update"
            event = "INSERT" if suffix == "insert" else "UPDATE OF tenant_id,owner_id"
            active_or_legacy = (
                "m.active=1 OR (NEW.owner_id='" + legacy_owner_id.replace("'", "''") +
                "' AND NEW.tenant_id='" + tenant_id.replace("'", "''") + "')"
                if suffix == "insert" else "m.active=1"
            )
            expected_ddl = f"""CREATE TRIGGER "{item.name}" BEFORE {event} ON "{table}"
              WHEN NEW.tenant_id IS NULL OR NEW.owner_id IS NULL OR NOT EXISTS (
                SELECT 1 FROM tenant_memberships m
                WHERE m.id=NEW.owner_id AND m.tenant_id=NEW.tenant_id AND ({active_or_legacy})
              )
              BEGIN SELECT RAISE(ABORT,'tenant membership required'); END"""
            normalized_actual = " ".join((item.sql or "").lower().split())
            normalized_expected = " ".join(expected_ddl.lower().split())
            if normalized_actual != normalized_expected:
                raise MigrationError(f"{item.name} does not match the canonical membership guard.")
    violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    if violations:
        raise MigrationError(f"Tenant role schema has {len(violations)} foreign-key/data violations.")
    expected_digest = _digest(issuer, subject, tenant_id)
    actual_digest = connection.execute(text(
        "SELECT mapping_sha256 FROM tenant_migration_versions WHERE revision=:r"
    ), {"r": ROLE_REVISION}).scalar_one_or_none()
    if actual_digest != expected_digest:
        raise MigrationError("Role migration marker does not match its configured principal.")


def dry_run(engine: Engine, issuer: str, subject: str, tenant_id: str) -> dict:
    if engine.dialect.name != "sqlite":
        raise MigrationError("Tenant role migration is offline SQLite only.")
    readonly = _read_only_engine(engine)
    try:
        with readonly.connect() as connection:
            owner_id = _require_v1(connection, issuer, subject, tenant_id)
            if connection.execute(text("SELECT 1 FROM tenant_migration_versions WHERE revision=:r"),
                                  {"r": ROLE_REVISION}).first():
                _validate_role_schema(connection, issuer, subject, tenant_id, owner_id)
                return {"status":"already-applied","revision":ROLE_REVISION,"mutated":False}
            if "tenant_memberships" in inspect(connection).get_table_names():
                raise MigrationError("Unversioned tenant_memberships table exists.")
            if "published" in {c["name"] for c in inspect(connection).get_columns("agents")}:
                raise MigrationError("Unversioned agents.published column exists.")
            _assert_legacy_guards(connection, tenant_id, owner_id)
            state = snapshot(readonly)
    finally:
        readonly.dispose()
    return {"status":"dry-run","revision":ROLE_REVISION,"snapshot":state,"mutated":False}


def migrate(engine: Engine, issuer: str, subject: str, tenant_id: str, backup_path: Path) -> dict:
    if engine.dialect.name != "sqlite":
        raise MigrationError("Tenant role migration is offline SQLite only; PostgreSQL apply is disabled.")
    report = dry_run(engine, issuer, subject, tenant_id)
    if report["status"] == "already-applied":
        return report
    before = report["snapshot"]
    _backup(engine, backup_path)
    backup_engine = create_engine(f"sqlite:///{backup_path.resolve()}")
    try:
        if snapshot(backup_engine)["checksum_sha256"] != before["checksum_sha256"]:
            raise MigrationError("Consistent role-migration backup checksum differs from snapshot.")
    finally:
        backup_engine.dispose()
    owner_id = None
    with engine.connect() as connection:
        owner_id = _require_v1(connection, issuer, subject, tenant_id)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            if snapshot(engine)["checksum_sha256"] != before["checksum_sha256"]:
                raise MigrationError("Database changed after role migration snapshot/backup.")
            _create_memberships(connection, owner_id, tenant_id, issuer, subject)
            connection.exec_driver_sql(
                "ALTER TABLE agents ADD COLUMN published BOOLEAN NOT NULL DEFAULT 1 CHECK (published IN (0,1))"
            )
            for table in ROOTS:
                _rebuild_root(connection, table)
            _install_membership_guards(connection, owner_id, tenant_id)
            connection.execute(text(
                "INSERT INTO tenant_migration_versions(revision,applied_at,mapping_sha256) "
                "VALUES(:r,:now,:digest)"
            ), {"r":ROLE_REVISION,"now":datetime.now(timezone.utc).replace(tzinfo=None),
                "digest":_digest(issuer,subject,tenant_id)})
            _validate_role_schema(connection, issuer, subject, tenant_id, owner_id)
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
            if violations:
                raise MigrationError(f"Role migration produced {len(violations)} foreign-key violations.")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    return {"status":"applied","revision":ROLE_REVISION,"before":before,"after":snapshot(engine),
            "backup":str(backup_path)}


def validate_role_migration(engine: Engine, issuer: str, subject: str, tenant_id: str) -> None:
    if engine.dialect.name != "sqlite":
        raise MigrationError("Tenant role authorization is validated for SQLite only.")
    with engine.connect() as connection:
        owner_id = _require_v1(connection, issuer, subject, tenant_id)
        _validate_role_schema(connection, issuer, subject, tenant_id, owner_id)

def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    from sqlalchemy.engine import make_url

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--backup-dir", type=Path, default=Path("data/backups"))
    parser.add_argument("--apply", action="store_true", help="Back up and apply; default is read-only dry-run.")
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")
    for key, supplied in (
        ("AGENT_RUNTIME_OIDC_ISSUER", args.issuer),
        ("AGENT_RUNTIME_OIDC_LEGACY_SUB", args.subject),
        ("AGENT_RUNTIME_OIDC_LEGACY_TENANT", args.tenant_id),
    ):
        configured = os.getenv(key)
        if configured is not None and configured != supplied:
            print(f"{key} does not match the supplied role migration mapping", file=sys.stderr)
            return 2
    url = make_url(args.database_url)
    if url.get_backend_name() != "sqlite":
        print("Tenant role migration is offline SQLite only.", file=sys.stderr)
        return 2
    database_path = url.database
    if not database_path or database_path == ":memory:" or not Path(database_path).expanduser().exists():
        print("Role migration requires an existing on-disk SQLite database.", file=sys.stderr)
        return 2
    engine = create_engine(args.database_url, pool_pre_ping=True)
    try:
        if not args.apply:
            report = dry_run(engine, args.issuer, args.subject, args.tenant_id)
        else:
            import time
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = args.backup_dir / f"agent-runtime-before-{ROLE_REVISION}-{stamp}-{time.time_ns()}.db"
            report = migrate(engine, args.issuer, args.subject, args.tenant_id, backup)
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0
    except Exception as exc:
        print(f"tenant role migration failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()
