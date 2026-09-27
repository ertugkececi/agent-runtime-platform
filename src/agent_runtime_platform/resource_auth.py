from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

from sqlalchemy import func, select, text

from agent_runtime_platform.auth import OIDCConfig, Principal
from agent_runtime_platform.database import Database
from agent_runtime_platform.tenant_migration import REVISION, ROOT_TABLES, _validate_owned_roots
from agent_runtime_platform.models import HumanChatRun, RoomRun, Run


@dataclass(frozen=True)
class OwnershipScope:
    owner_id: str
    tenant_id: str


class ResourceAuthorization:
    """Fail-closed gate for the initial single legacy-owner policy."""

    def __init__(self, database: Database, auth_config: OIDCConfig | None) -> None:
        self.database = database
        self.auth_config = auth_config
        self.mode = os.getenv("AGENT_RUNTIME_RESOURCE_AUTH_MODE", "off").strip().lower()
        if self.mode not in {"off", "legacy_owner"}:
            raise RuntimeError("AGENT_RUNTIME_RESOURCE_AUTH_MODE must be 'off' or 'legacy_owner'.")
        if self.mode == "legacy_owner":
            if auth_config is None:
                raise RuntimeError("legacy_owner resource authorization requires OIDC auth mode.")
            if database.engine.dialect.name != "sqlite":
                raise RuntimeError("legacy_owner resource authorization is unavailable until PostgreSQL migration is validated.")
            owner_id = self._resolve_mapping(auth_config.issuer, auth_config.legacy_subject, auth_config.tenant_id)
            try:
                with database.engine.connect() as connection:
                    _validate_owned_roots(connection, auth_config.tenant_id, owner_id)
                    self._validate_schema(connection, auth_config.tenant_id, owner_id)
            except Exception as exc:
                raise RuntimeError("Ownership migration invariants are incomplete or inconsistent.") from exc

    def _resolve_mapping(self, issuer: str, subject: str, tenant_id: str) -> str:
        expected = hashlib.sha256((issuer + "\0" + subject + "\0" + tenant_id).encode()).hexdigest()
        try:
            with self.database.engine.connect() as connection:
                digest = connection.execute(text(
                    "SELECT mapping_sha256 FROM tenant_migration_versions WHERE revision=:revision"
                ), {"revision": REVISION}).scalar_one_or_none()
                row = connection.execute(text(
                    "SELECT id FROM tenant_owners WHERE oidc_issuer=:issuer AND oidc_subject=:subject AND tenant_id=:tenant"
                ), {"issuer": issuer, "subject": subject, "tenant": tenant_id}).scalar_one_or_none()
                for table in ("agents", "conversations", "rooms"):
                    columns = {item["name"] for item in __import__("sqlalchemy").inspect(connection).get_columns(table)}
                    if not {"tenant_id", "owner_id"} <= columns:
                        raise RuntimeError("Ownership migration is incomplete.")
                if digest != expected or row is None:
                    raise RuntimeError("Ownership migration or OIDC owner mapping is missing or mismatched.")
                self._validate_schema(connection, tenant_id, row)
                return row
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("Ownership migration gate could not be verified.") from exc

    @staticmethod
    def _validate_schema(connection, tenant_id: str, owner_id: str) -> None:
        inspector = __import__("sqlalchemy").inspect(connection)
        for table in ROOT_TABLES:
            columns = {item["name"]: item for item in inspector.get_columns(table)}
            tenant_column = columns.get("tenant_id")
            owner_column = columns.get("owner_id")
            if (tenant_column is None or owner_column is None or tenant_column["nullable"]
                    or owner_column["nullable"]):
                raise RuntimeError("Ownership columns must be physically non-null.")
            def clean_default(value):
                return str(value).strip().strip("()'") if value is not None else None
            if clean_default(tenant_column.get("default")) != tenant_id or clean_default(owner_column.get("default")) != owner_id:
                raise RuntimeError("Ownership root defaults do not match the configured legacy mapping.")
            if not any(fk["constrained_columns"] == ["owner_id", "tenant_id"]
                       and fk["referred_table"] == "tenant_owners"
                       and fk["referred_columns"] == ["id", "tenant_id"]
                       for fk in inspector.get_foreign_keys(table)):
                raise RuntimeError("Ownership root is missing its composite owner/tenant foreign key.")
        expected = {f"trg_{table}_legacy_owner_{action}" for table in ROOT_TABLES for action in ("insert", "update")}
        triggers = connection.execute(text(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name IN ('agents','conversations','rooms')"
        )).all()
        if {item.name for item in triggers} != expected:
            raise RuntimeError("Ownership root write guards are missing or unexpected.")
        def normalized_trigger_sql(ddl: str) -> str:
            # SQLite preserves the CREATE TRIGGER source in sqlite_master. Normalize only
            # whitespace and identifier quoting; every event, column, predicate, and action
            # must still match the migration's canonical guard exactly.
            return " ".join(ddl.lower().replace('"', "").split()).rstrip(";")

        for item in triggers:
            table, action = item.name.removeprefix("trg_").removesuffix("_legacy_owner_insert"), "insert"
            if item.name.endswith("_legacy_owner_update"):
                table, action = item.name.removeprefix("trg_").removesuffix("_legacy_owner_update"), "update"
            event = "insert" if action == "insert" else "update of tenant_id, owner_id"
            expected_sql = normalized_trigger_sql(
                f"CREATE TRIGGER trg_{table}_legacy_owner_{action} BEFORE {event} ON {table} "
                f"WHEN NEW.tenant_id <> '{tenant_id}' OR NEW.owner_id <> '{owner_id}' "
                "BEGIN SELECT RAISE(ABORT, 'tenant owner context missing or inconsistent'); END"
            )
            if normalized_trigger_sql(item.sql or "") != expected_sql:
                raise RuntimeError("Ownership root write guard differs from the canonical migration trigger.")

    def scope_for(self, principal: Principal | None) -> OwnershipScope | None:
        if self.mode == "off":
            return None
        if principal is None or self.auth_config is None:
            return None
        if (principal.issuer != self.auth_config.issuer or principal.subject != self.auth_config.legacy_subject
                or principal.tenant_id != self.auth_config.tenant_id or "legacy:operator" not in principal.scopes):
            raise PermissionError("Authenticated principal lacks the legacy owner authorization.")
        owner_id = self._resolve_mapping(principal.issuer, principal.subject, principal.tenant_id)
        return OwnershipScope(owner_id, principal.tenant_id)


def scoped_root(model, scope: OwnershipScope):
    # Table names come only from internal ORM classes, never request input.
    table = model.__tablename__
    return text(f"{table}.owner_id=:owner_id AND {table}.tenant_id=:tenant_id").bindparams(
        owner_id=scope.owner_id, tenant_id=scope.tenant_id
    )


def unique_run_parent(session, run_id: str) -> str | None:
    """Resolve polymorphic queue/run IDs only when exactly one typed parent exists."""
    parents = (("run", Run), ("human_chat", HumanChatRun), ("room", RoomRun))
    matches = [kind for kind, model in parents if session.scalar(
        select(func.count()).select_from(model).where(model.id == run_id)
    )]
    return matches[0] if len(matches) == 1 else None
