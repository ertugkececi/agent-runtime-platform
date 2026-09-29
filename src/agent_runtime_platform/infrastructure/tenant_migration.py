"""Explicit offline, versioned migration for legacy tenant ownership."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.engine import Engine

from agent_runtime_platform.domain.models import Base

REVISION = "tenant_ownership_v1"
ROOT_TABLES = ("agents", "conversations", "rooms")
RELATED_TABLES = (
    "agent_capabilities", "conversation_members", "messages", "runs", "run_events",
    "human_chat_sessions", "human_chat_runs", "human_chat_messages", "human_chat_run_events",
    "tasks", "rooms", "room_participants", "room_runs", "room_run_turns", "room_run_events",
    "queue_jobs",
)
TENANT_NAMESPACE = uuid.UUID("73a84b75-236a-4df8-8de4-890030e28d1a")


class MigrationError(RuntimeError):
    pass


def _json_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return value


def snapshot(engine: Engine) -> dict:
    inspector = inspect(engine)
    counts: dict[str, int] = {}
    digest = hashlib.sha256()
    with engine.connect() as connection:
        if engine.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN")
        elif engine.dialect.name == "postgresql":
            connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        for table in sorted(inspector.get_table_names()):
            column_info = inspector.get_columns(table)
            columns = [c["name"] for c in column_info]
            schema_state = {
                "table": table,
                "columns": [(c["name"], str(c["type"]), c["nullable"], c.get("default")) for c in column_info],
                "primary_key": inspector.get_pk_constraint(table),
                "foreign_keys": inspector.get_foreign_keys(table),
                "indexes": inspector.get_indexes(table),
                "unique": inspector.get_unique_constraints(table),
                "checks": inspector.get_check_constraints(table),
            }
            if engine.dialect.name == "sqlite":
                schema_state["sqlite_ddl"] = connection.execute(text(
                    "SELECT type,name,sql FROM sqlite_master WHERE tbl_name=:table "
                    "AND type IN ('table','index','trigger') ORDER BY type,name"
                ), {"table": table}).all()
            schema = json.dumps(schema_state, sort_keys=True, separators=(",", ":"), default=str).encode()
            digest.update(b"schema\0" + schema + b"\n")
            pk = inspector.get_pk_constraint(table).get("constrained_columns") or columns
            query = text(f'SELECT * FROM "{table}" ORDER BY ' + ", ".join(f'"{c}"' for c in pk))
            result = connection.execute(query)
            count = 0
            for row in result:
                payload = json.dumps(
                    [_json_value(v) for v in row], sort_keys=True, separators=(",", ":"), default=str
                ).encode()
                digest.update(table.encode() + b"\0" + payload + b"\n")
                count += 1
            counts[table] = count
        connection.rollback()
    return {"row_counts": counts, "checksum_sha256": digest.hexdigest()}


def _read_only_engine(engine: Engine) -> Engine:
    if engine.dialect.name == "sqlite":
        # SQLAlchemy's URI form requires an explicit file: URI for SQLite read-only mode.
        database = engine.url.database
        if not database or database == ":memory:":
            raise MigrationError("Read-only dry-run requires an existing on-disk SQLite database.")
        url = engine.url.set(database="file:" + str(Path(database).expanduser().resolve()),
                             query={**dict(engine.url.query), "mode": "ro", "uri": "true"})
        return create_engine(url, pool_pre_ping=True)
    if engine.dialect.name == "postgresql":
        raise MigrationError("PostgreSQL dry-run is disabled until a service-backed read-only integration test passes.")
    raise MigrationError(f"Unsupported database dialect: {engine.dialect.name}")


def _ids(issuer: str, subject: str, tenant_id: str) -> tuple[str, str]:
    if not issuer.strip() or not subject.strip() or len(issuer) > 500 or len(subject) > 500:
        raise MigrationError("OIDC issuer and subject must be exact non-empty values (max 500 characters each).")
    parsed_issuer = urlparse(issuer)
    if parsed_issuer.scheme != "https" or not parsed_issuer.netloc:
        raise MigrationError("OIDC issuer must be an absolute HTTPS issuer URL.")
    if issuer != issuer.strip() or subject != subject.strip():
        raise MigrationError("Issuer and subject must not contain leading/trailing whitespace.")
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", tenant_id):
        raise MigrationError("Tenant ID must use only letters, digits, dot, underscore, colon, or hyphen (max 80).")
    configured_tenant = os.environ.get("AGENT_RUNTIME_OIDC_LEGACY_TENANT", "legacy")
    if tenant_id != configured_tenant:
        raise MigrationError("Tenant ID must exactly match AGENT_RUNTIME_OIDC_LEGACY_TENANT (default: legacy).")
    key = issuer + "\0" + subject
    return tenant_id, str(uuid.uuid5(TENANT_NAMESPACE, "owner:" + key))


def _backup_sqlite(engine: Engine, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL prevents accidental replacement; mode is private even under a permissive umask.
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    raw = engine.raw_connection()
    try:
        source = raw.driver_connection
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
            status = target.execute("PRAGMA integrity_check").fetchone()[0]
            if status != "ok":
                raise MigrationError("SQLite backup integrity_check failed.")
        finally:
            target.close()
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        raw.close()
    os.chmod(destination, 0o600)


def _backup_postgres(engine: Engine, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    parsed = urlparse(engine.url.render_as_string(hide_password=False))
    # Keep secrets out of process arguments. pg_dump obtains password from its environment.
    env = os.environ.copy()
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)
    args = ["pg_dump", "--format=custom", "--no-owner", "--no-privileges"]
    if parsed.hostname:
        args += ["--host", parsed.hostname]
    if parsed.port:
        args += ["--port", str(parsed.port)]
    if parsed.username:
        args += ["--username", unquote(parsed.username)]
    args += ["--dbname", unquote(parsed.path.lstrip("/")), "--file", str(destination)]
    try:
        subprocess.run(args, env=env, check=True, capture_output=True, text=True)
        os.chmod(destination, 0o600)
    except FileNotFoundError as exc:
        destination.unlink(missing_ok=True)
        raise MigrationError("pg_dump is required to apply PostgreSQL migrations.") from exc
    except subprocess.CalledProcessError as exc:
        destination.unlink(missing_ok=True)
        raise MigrationError("pg_dump failed: " + exc.stderr[-1000:]) from exc
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _backup(engine: Engine, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if engine.dialect.name == "sqlite":
        _backup_sqlite(engine, path)
    elif engine.dialect.name == "postgresql":
        _backup_postgres(engine, path)
    else:
        raise MigrationError(f"Unsupported database dialect: {engine.dialect.name}")


def _ensure_unambiguous_legacy_graph(engine: Engine) -> None:
    tables = set(inspect(engine).get_table_names())
    required = set(ROOT_TABLES)
    if not required.issubset(tables):
        raise MigrationError("Database is missing expected root tables: " + ", ".join(sorted(required - tables)))
    # Verify the deployed child-FK graph matches the ORM's declared graph. queue_jobs.run_id is
    # intentionally polymorphic and is validated separately because it has no physical FK.
    actual_tables = set(inspect(engine).get_table_names())
    for model_table in Base.metadata.sorted_tables:
        if model_table.name not in actual_tables:
            continue
        expected = {(fk.parent.name, fk.column.table.name, fk.column.name) for fk in model_table.foreign_keys}
        actual = set()
        for fk in inspect(engine).get_foreign_keys(model_table.name):
            actual.update((local, fk["referred_table"], remote)
                          for local, remote in zip(fk["constrained_columns"], fk["referred_columns"]))
        if expected - actual:
            raise MigrationError(f"Database is missing declared foreign keys for {model_table.name}: {sorted(expected - actual)}")
    # Every record that owns data is attached to one of these roots; queue_jobs is checked via its run graph.
    with engine.connect() as connection:
        for table in ("human_chat_sessions",):
            if table in tables:
                orphan = connection.execute(text(
                    "SELECT COUNT(*) FROM human_chat_sessions h "
                    "LEFT JOIN conversations c ON c.id=h.conversation_id WHERE c.id IS NULL"
                )).scalar_one()
                if orphan:
                    raise MigrationError(f"Found {orphan} human chat sessions without a conversation.")
        if engine.dialect.name == "sqlite":
            triggers = connection.execute(text(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name IN ('agents','conversations','rooms')"
            )).scalars().all()
            expected_triggers = {f"trg_{table}_legacy_owner_{action}" for table in ROOT_TABLES for action in ("insert", "update")}
            unexpected = set(triggers) - expected_triggers
            if unexpected or (triggers and set(triggers) != expected_triggers):
                raise MigrationError("Unexpected or incomplete triggers exist on ownership roots; refusing to rebuild: " + ", ".join(sorted(set(triggers) ^ expected_triggers)))
            violations = connection.execute(text("PRAGMA foreign_key_check")).all()
            if violations:
                raise MigrationError(f"Foreign-key check found {len(violations)} violations.")
        _validate_queue_graph(connection, ownership=False)


def _create_schema(connection, dialect: str) -> None:
    connection.execute(text("""
        CREATE TABLE IF NOT EXISTS tenants (
            id VARCHAR(80) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL
        )
    """))
    connection.execute(text("""
        CREATE TABLE IF NOT EXISTS tenant_owners (
            id VARCHAR(36) PRIMARY KEY,
            tenant_id VARCHAR(80) NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            oidc_issuer VARCHAR(2048) NOT NULL,
            oidc_subject VARCHAR(1024) NOT NULL,
            UNIQUE (oidc_issuer, oidc_subject),
            UNIQUE (id, tenant_id)
        )
    """))
    connection.execute(text("""
        CREATE TABLE IF NOT EXISTS tenant_migration_versions (
            revision VARCHAR(80) PRIMARY KEY,
            applied_at TIMESTAMP NOT NULL,
            mapping_sha256 VARCHAR(64) NOT NULL
        )
    """))
    if dialect == "postgresql":
        inspector = inspect(connection)
        for table in ROOT_TABLES:
            existing = {c["name"] for c in inspector.get_columns(table)}
            if "tenant_id" not in existing:
                connection.execute(text(f'ALTER TABLE "{table}" ADD COLUMN tenant_id VARCHAR(80) REFERENCES tenants(id)'))
            if "owner_id" not in existing:
                connection.execute(text(f'ALTER TABLE "{table}" ADD COLUMN owner_id VARCHAR(36) REFERENCES tenant_owners(id)'))


def _rebuild_sqlite_root(connection, table: str, tenant_id: str, owner_id: str) -> None:
    inspector = inspect(connection)
    columns = inspector.get_columns(table)
    if {c["name"] for c in columns} & {"tenant_id", "owner_id"}:
        raise MigrationError(f"Unexpected partial SQLite ownership schema in {table}; manual review required.")
    pk = inspector.get_pk_constraint(table).get("constrained_columns") or []
    if not pk:
        raise MigrationError(f"Cannot rebuild {table} without a primary key.")
    # Preserve current root column types/nullability/server defaults and named indexes.
    trigger_count = connection.execute(text(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND tbl_name=:table"
    ), {"table": table}).scalar_one()
    if trigger_count:
        raise MigrationError(f"Unsupported existing triggers on {table}; refusing to discard them.")
    if (inspector.get_unique_constraints(table) or inspector.get_foreign_keys(table)
            or inspector.get_check_constraints(table)):
        raise MigrationError(f"Unsupported root constraints in {table}; refusing to discard them during SQLite rebuild.")
    indexes = connection.execute(text(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=:table AND sql IS NOT NULL"
    ), {"table": table}).scalars().all()
    definitions = []
    for column in columns:
        part = f'"{column["name"]}" {column["type"]}'
        if not column["nullable"]:
            part += " NOT NULL"
        if column.get("default") is not None:
            part += " DEFAULT " + str(column["default"])
        definitions.append(part)
    definitions.extend([
        f"\"tenant_id\" VARCHAR(80) NOT NULL DEFAULT '{tenant_id}' REFERENCES tenants(id)",
        f"\"owner_id\" VARCHAR(36) NOT NULL DEFAULT '{owner_id}' REFERENCES tenant_owners(id)",
        "FOREIGN KEY (owner_id, tenant_id) REFERENCES tenant_owners(id, tenant_id)",
        "PRIMARY KEY (" + ", ".join(f'"{name}"' for name in pk) + ")",
    ])
    temporary = f"__tenant_v1_{table}"
    if connection.execute(text(
        "SELECT 1 FROM sqlite_master WHERE name=:name LIMIT 1"
    ), {"name": temporary}).first():
        raise MigrationError(f"Reserved SQLite rebuild name {temporary} already exists; refusing to overwrite it.")
    connection.execute(text(f'CREATE TABLE "{temporary}" (' + ", ".join(definitions) + ")"))
    names = [c["name"] for c in columns]
    quoted = ", ".join(f'"{name}"' for name in names)
    connection.execute(text(
        f'INSERT INTO "{temporary}" ({quoted}, tenant_id, owner_id) '
        f'SELECT {quoted}, :tenant, :owner FROM "{table}"'
    ), {"tenant": tenant_id, "owner": owner_id})
    connection.execute(text(f'DROP TABLE "{table}"'))
    connection.execute(text(f'ALTER TABLE "{temporary}" RENAME TO "{table}"'))
    for statement in indexes:
        connection.exec_driver_sql(statement)
    connection.execute(text(f'CREATE INDEX IF NOT EXISTS "ix_{table}_tenant_owner" ON "{table}" (tenant_id, owner_id)'))


def _ensure_no_rebuild_name_collisions(connection) -> None:
    for table in ROOT_TABLES:
        temporary = f"__tenant_v1_{table}"
        if connection.execute(text(
            "SELECT type FROM sqlite_master WHERE name=:name LIMIT 1"
        ), {"name": temporary}).first():
            raise MigrationError(
                f"Reserved SQLite rebuild name {temporary} already exists; refusing to migrate."
            )


def _validate_queue_graph(connection, ownership: bool) -> None:
    tables = set(inspect(connection).get_table_names())
    if "queue_jobs" not in tables:
        return
    if not {"runs", "human_chat_runs", "room_runs", "conversations", "rooms"}.issubset(tables):
        raise MigrationError("Queue graph tables are incomplete; refusing tenant migration.")
    if ownership:
        target = """
          SELECT r.id AS run_id, c.tenant_id, c.owner_id FROM runs r JOIN conversations c ON c.id=r.conversation_id
          UNION ALL SELECT r.id, c.tenant_id, c.owner_id FROM human_chat_runs r JOIN conversations c ON c.id=r.conversation_id
          UNION ALL SELECT r.id, rm.tenant_id, rm.owner_id FROM room_runs r JOIN rooms rm ON rm.id=r.room_id
        """
        bad = connection.execute(text(f"""
          SELECT COUNT(*) FROM (
            SELECT q.run_id FROM queue_jobs q LEFT JOIN ({target}) resolved ON resolved.run_id=q.run_id
            GROUP BY q.run_id
            HAVING COUNT(resolved.run_id) <> 1 OR MAX(resolved.tenant_id) IS NULL OR MAX(resolved.owner_id) IS NULL
          ) invalid
        """)).scalar_one()
    else:
        bad = connection.execute(text("""
          SELECT COUNT(*) FROM queue_jobs q WHERE
            ((SELECT COUNT(*) FROM runs r JOIN conversations c ON c.id=r.conversation_id WHERE r.id=q.run_id) +
             (SELECT COUNT(*) FROM human_chat_runs r JOIN conversations c ON c.id=r.conversation_id WHERE r.id=q.run_id) +
             (SELECT COUNT(*) FROM room_runs r JOIN rooms rm ON rm.id=r.room_id WHERE r.id=q.run_id)) <> 1
        """)).scalar_one()
    if bad:
        raise MigrationError(f"{bad} queue jobs are orphaned or ambiguously linked to run roots.")


def _validate_owned_roots(connection, tenant_id: str, owner_id: str) -> None:
    _validate_queue_graph(connection, ownership=True)
    inspector = inspect(connection)
    for table in ROOT_TABLES:
        columns = {item["name"]: item for item in inspector.get_columns(table)}
        if columns["tenant_id"]["nullable"] or columns["owner_id"]["nullable"]:
            raise MigrationError(f"{table} ownership columns must be physically NOT NULL.")
        if not any(fk["constrained_columns"] == ["owner_id", "tenant_id"]
                   for fk in inspector.get_foreign_keys(table)):
            raise MigrationError(f"{table} is missing its composite tenant/owner foreign key.")
        bad = connection.execute(text(
            f'SELECT COUNT(*) FROM "{table}" WHERE tenant_id IS NULL OR owner_id IS NULL '
            'OR tenant_id<>:tenant OR owner_id<>:owner'
        ), {"tenant": tenant_id, "owner": owner_id}).scalar_one()
        if bad:
            raise MigrationError(f"{table} contains null or conflicting ownership rows.")


def _create_write_guard(connection, dialect: str, tenant_id: str, owner_id: str) -> None:
    for table in ROOT_TABLES:
        if dialect == "sqlite":
            connection.execute(text(f'DROP TRIGGER IF EXISTS "trg_{table}_legacy_owner_insert"'))
            connection.execute(text(f'DROP TRIGGER IF EXISTS "trg_{table}_legacy_owner_update"'))
            for action in ("insert", "update"):
                event = "INSERT" if action == "insert" else "UPDATE OF tenant_id, owner_id"
                connection.execute(text(f"""
                    CREATE TRIGGER "trg_{table}_legacy_owner_{action}"
                    BEFORE {event} ON "{table}"
                    WHEN NEW.tenant_id <> '{tenant_id}' OR NEW.owner_id <> '{owner_id}'
                    BEGIN SELECT RAISE(ABORT, 'tenant owner context missing or inconsistent'); END
                """))
        else:
            function = f"assign_{table}_legacy_owner_v1"
            connection.execute(text(f'DROP TRIGGER IF EXISTS "trg_{table}_legacy_owner_insert" ON "{table}"'))
            connection.execute(text(f'DROP TRIGGER IF EXISTS "trg_{table}_legacy_owner_update" ON "{table}"'))
            connection.execute(text(f'DROP FUNCTION IF EXISTS "{function}"()'))
            connection.execute(text(f"""
                CREATE FUNCTION "{function}"() RETURNS trigger AS $$
                BEGIN
                  IF NEW.tenant_id IS NULL OR NEW.owner_id IS NULL OR NOT EXISTS (
                    SELECT 1 FROM tenant_owners o
                    WHERE o.id=NEW.owner_id AND o.tenant_id=NEW.tenant_id
                      AND o.id='{owner_id}' AND o.tenant_id='{tenant_id}'
                  ) THEN
                    RAISE EXCEPTION 'tenant owner context missing or inconsistent';
                  END IF;
                  RETURN NEW;
                END; $$ LANGUAGE plpgsql
            """))
            connection.execute(text(
                f'CREATE TRIGGER "trg_{table}_legacy_owner_insert" BEFORE INSERT ON "{table}" '
                f'FOR EACH ROW EXECUTE FUNCTION "{function}"()'
            ))
            connection.execute(text(
                f'CREATE TRIGGER "trg_{table}_legacy_owner_update" BEFORE UPDATE OF tenant_id, owner_id '
                f'ON "{table}" FOR EACH ROW EXECUTE FUNCTION "{function}"()'
            ))


def migrate(engine: Engine, issuer: str, subject: str, tenant_id: str, backup_path: Path) -> dict:
    tenant_id, owner_id = _ids(issuer, subject, tenant_id)
    dialect = engine.dialect.name
    if dialect == "postgresql":
        raise MigrationError("PostgreSQL apply is disabled until its migration and restore integration gate passes.")
    if dialect != "sqlite":
        raise MigrationError(f"Unsupported database dialect: {dialect}")
    if backup_path is None:
        raise MigrationError("A consistent backup path is mandatory for every apply.")
    with engine.connect() as connection:
        _ensure_no_rebuild_name_collisions(connection)
    _ensure_unambiguous_legacy_graph(engine)
    with engine.connect() as connection:
        _validate_queue_graph(connection, ownership=False)
    before = snapshot(engine)
    inspector = inspect(engine)
    if "tenant_migration_versions" in inspector.get_table_names():
        with engine.connect() as connection:
            applied = connection.execute(text(
                "SELECT mapping_sha256 FROM tenant_migration_versions WHERE revision=:revision"
            ), {"revision": REVISION}).scalar_one_or_none()
            if applied:
                expected = hashlib.sha256((issuer + "\0" + subject + "\0" + tenant_id).encode()).hexdigest()
                if applied != expected:
                    raise MigrationError("This database is already migrated with a different owner mapping.")
                _validate_owned_roots(connection, tenant_id, owner_id)
                return {"status": "already-applied", "before": before, "after": before, "backup": None}
    _backup(engine, backup_path)
    if dialect == "sqlite":
        restored_engine = create_engine(f"sqlite:///{backup_path.resolve()}")
        try:
            if snapshot(restored_engine)["checksum_sha256"] != before["checksum_sha256"]:
                raise MigrationError("Consistent backup checksum differs from the dry-run snapshot; rerun after stopping writers.")
        finally:
            restored_engine.dispose()
    mapping_digest = hashlib.sha256((issuer + "\0" + subject + "\0" + tenant_id).encode()).hexdigest()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with engine.connect() as connection:
        # SQLite's legacy driver will otherwise autocommit DDL before its first DML.
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            if snapshot(engine)["checksum_sha256"] != before["checksum_sha256"]:
                raise MigrationError("Database changed after the snapshot/backup; rerun after stopping writers.")
            _create_schema(connection, dialect)
            existing = connection.execute(text(
                "SELECT id, tenant_id FROM tenant_owners WHERE oidc_issuer=:issuer AND oidc_subject=:subject"
            ), {"issuer": issuer, "subject": subject}).first()
            if existing and (existing.id != owner_id or existing.tenant_id != tenant_id):
                raise MigrationError("Stored owner mapping conflicts with deterministic migration identity.")
            other = connection.execute(text("SELECT COUNT(*) FROM tenant_owners")).scalar_one()
            if other and not existing:
                raise MigrationError("Database has a different tenant owner mapping; refusing to reassign legacy data.")
            connection.execute(text(
                "INSERT INTO tenants (id, created_at) VALUES (:id, :created) ON CONFLICT (id) DO NOTHING"
            ), {"id": tenant_id, "created": now})
            connection.execute(text(
                "INSERT INTO tenant_owners (id, tenant_id, oidc_issuer, oidc_subject) "
                "VALUES (:id,:tenant,:issuer,:subject) ON CONFLICT (id) DO NOTHING"
            ), {"id": owner_id, "tenant": tenant_id, "issuer": issuer, "subject": subject})
            if dialect == "sqlite":
                for table in ROOT_TABLES:
                    _rebuild_sqlite_root(connection, table, tenant_id, owner_id)
            else:
                # PostgreSQL is deliberately fail-closed above until a service-backed gate exists.
                pass
            _create_schema(connection, dialect)
            _create_write_guard(connection, dialect, tenant_id, owner_id)
            _validate_owned_roots(connection, tenant_id, owner_id)
            violations = connection.execute(text("PRAGMA foreign_key_check")).all()
            if violations:
                raise MigrationError(f"Foreign-key check found {len(violations)} violations after rebuild.")
            connection.execute(text(
                "INSERT INTO tenant_migration_versions (revision, applied_at, mapping_sha256) "
                "VALUES (:revision,:applied,:digest)"
            ), {"revision": REVISION, "applied": now, "digest": mapping_digest})
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    return {"status": "applied", "before": before, "after": snapshot(engine), "backup": str(backup_path)}


def dry_run(engine: Engine, issuer: str, subject: str, tenant_id: str) -> dict:
    _ids(issuer, subject, tenant_id)
    readonly = _read_only_engine(engine)
    try:
        _ensure_unambiguous_legacy_graph(readonly)
        with readonly.connect() as connection:
            _validate_queue_graph(connection, ownership=False)
        state = snapshot(readonly)
        owner_cols = {}
        for table in ROOT_TABLES:
            owner_cols[table] = {
                c["name"] for c in inspect(readonly).get_columns(table)
            } & {"tenant_id", "owner_id"}
    finally:
        readonly.dispose()
    return {
        "status": "dry-run",
        "revision": REVISION,
        "owner_mapping_sha256": hashlib.sha256((issuer + "\0" + subject + "\0" + tenant_id).encode()).hexdigest(),
        "snapshot": state,
        "legacy_rows_to_backfill": {
            table: state["row_counts"].get(table, 0) for table in ROOT_TABLES
        },
        "existing_ownership_columns": {k: sorted(v) for k, v in owner_cols.items()},
        "mutated": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"), required=os.getenv("DATABASE_URL") is None)
    parser.add_argument("--issuer", required=True, help="Exact OIDC issuer URL from the operator.")
    parser.add_argument("--subject", required=True, help="Exact OIDC subject from the operator; never an email.")
    parser.add_argument("--tenant-id", required=True, help="Exact configured tenant ID (AGENT_RUNTIME_OIDC_LEGACY_TENANT; default: legacy).")
    parser.add_argument("--backup-dir", type=Path, default=Path("data/backups"))
    parser.add_argument("--apply", action="store_true", help="Create backup and apply; default is read-only dry-run. PostgreSQL dry-run and apply are fail-closed until service-backed tests pass.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configured = {
        "AGENT_RUNTIME_OIDC_ISSUER": args.issuer,
        "AGENT_RUNTIME_OIDC_LEGACY_SUB": args.subject,
        "AGENT_RUNTIME_OIDC_LEGACY_TENANT": args.tenant_id,
    }
    for key, supplied in configured.items():
        expected = os.environ.get(key, "legacy" if key == "AGENT_RUNTIME_OIDC_LEGACY_TENANT" else supplied)
        if expected != supplied:
            print(f"{key} does not match the operator-supplied migration mapping", file=sys.stderr)
            return 2
    url = make_url(args.database_url)
    if url.get_backend_name() == "sqlite":
        database = url.database
        if not database or database == ":memory:":
            print("tenant migration requires an existing on-disk SQLite database", file=sys.stderr)
            return 2
        if not Path(database).expanduser().exists():
            print("SQLite database does not exist; refusing to create a file during dry-run/apply", file=sys.stderr)
            return 2
    engine = create_engine(args.database_url, pool_pre_ping=True)
    if engine.dialect.name == "sqlite":
        from sqlalchemy import event
        event.listen(engine, "connect", lambda dbapi, _record: dbapi.execute("PRAGMA foreign_keys=ON"))
    try:
        if not args.apply:
            report = dry_run(engine, args.issuer, args.subject, args.tenant_id)
        else:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            suffix = ".dump" if engine.dialect.name == "postgresql" else ".db"
            backup_path = args.backup_dir / f"agent-runtime-before-{REVISION}-{stamp}{suffix}"
            report = migrate(engine, args.issuer, args.subject, args.tenant_id, backup_path)
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0
    except Exception as exc:
        print(f"tenant migration failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
