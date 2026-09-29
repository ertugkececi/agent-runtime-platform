from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

from sqlalchemy import func, select, text

from agent_runtime_platform.infrastructure.auth import OIDCConfig, Principal
from agent_runtime_platform.infrastructure.database import Database
from agent_runtime_platform.infrastructure.tenant_migration import REVISION, ROOT_TABLES, _validate_owned_roots
from agent_runtime_platform.domain.models import HumanChatRun, RoomRun, Run


@dataclass(frozen=True)
class OwnershipScope:
    owner_id: str
    tenant_id: str
    role: str = "legacy_owner"


class ResourceAuthorization:
    """Fail-closed gate for the initial single legacy-owner policy."""

    def __init__(self, database: Database, auth_config: OIDCConfig | None) -> None:
        self.database = database
        self.auth_config = auth_config
        self.mode = os.getenv("AGENT_RUNTIME_RESOURCE_AUTH_MODE", "off").strip().lower()
        if self.mode not in {"off", "legacy_owner", "tenant_roles"}:
            raise RuntimeError("AGENT_RUNTIME_RESOURCE_AUTH_MODE must be 'off', 'legacy_owner', or 'tenant_roles'.")
        if self.mode == "tenant_roles":
            if auth_config is None or auth_config.resource_auth_mode != "tenant_roles":
                raise RuntimeError("tenant_roles requires OIDC auth mode and matching resource mode.")
            if database.engine.dialect.name != "sqlite":
                raise RuntimeError("tenant_roles is validated for SQLite only; PostgreSQL is fail-closed.")
            try:
                from agent_runtime_platform.infrastructure.tenant_roles_migration import validate_role_migration
                validate_role_migration(database.engine, auth_config.issuer, auth_config.legacy_subject, auth_config.tenant_id)
            except Exception as exc:
                raise RuntimeError("Tenant role migration invariants are incomplete or inconsistent.") from exc
        if self.mode == "legacy_owner":
            if auth_config is None:
                raise RuntimeError("legacy_owner resource authorization requires OIDC auth mode.")
            if database.engine.dialect.name != "sqlite":
                raise RuntimeError("legacy_owner resource authorization is unavailable until PostgreSQL migration is validated.")
            owner_id = self._resolve_mapping(auth_config.issuer, auth_config.legacy_subject, auth_config.tenant_id)
            try:
                with database.engine.connect() as connection:
                    role_revision = connection.execute(text(
                        "SELECT mapping_sha256 FROM tenant_migration_versions WHERE revision='tenant_roles_v1'"
                    )).scalar_one_or_none()
                    if role_revision is None:
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
                role_digest = connection.execute(text(
                    "SELECT mapping_sha256 FROM tenant_migration_versions WHERE revision='tenant_roles_v1'"
                )).scalar_one_or_none()
                if role_digest is not None:
                    from agent_runtime_platform.infrastructure.tenant_roles_migration import validate_role_migration
                    validate_role_migration(self.database.engine, issuer, subject, tenant_id)
                    membership = connection.execute(text("""
                        SELECT id FROM tenant_memberships
                        WHERE oidc_issuer=:issuer AND oidc_subject=:subject AND tenant_id=:tenant
                          AND role='admin' AND active=1
                    """), {"issuer":issuer,"subject":subject,"tenant":tenant_id}).scalar_one_or_none()
                    if membership is None:
                        raise RuntimeError("Configured legacy owner is not an active tenant admin.")
                    return membership
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
            # Normalize lexical whitespace/case only outside quoted tokens. Literal bytes
            # and quoted identifiers remain exact because SQLite compares string literals
            # case-sensitively and their contents are part of the guard semantics.
            out: list[str] = []
            quote: str | None = None
            pending_space = False
            index = 0
            while index < len(ddl):
                char = ddl[index]
                if quote:
                    out.append(char)
                    if char == quote:
                        if index + 1 < len(ddl) and ddl[index + 1] == quote:
                            out.append(ddl[index + 1])
                            index += 1
                        else:
                            quote = None
                    index += 1
                    continue
                if char in ("'", '\"', '`'):
                    if pending_space and out:
                        out.append(" ")
                    pending_space = False
                    quote = char
                    out.append(char)
                elif char.isspace():
                    pending_space = True
                else:
                    if pending_space and out:
                        out.append(" ")
                    pending_space = False
                    out.append(char.lower())
                index += 1
            return "".join(out).rstrip(" ;")

        for item in triggers:
            table, action = item.name.removeprefix("trg_").removesuffix("_legacy_owner_insert"), "insert"
            if item.name.endswith("_legacy_owner_update"):
                table, action = item.name.removeprefix("trg_").removesuffix("_legacy_owner_update"), "update"
            event = "INSERT" if action == "insert" else "UPDATE OF tenant_id, owner_id"
            expected_ddl = (
                f'CREATE TRIGGER "trg_{table}_legacy_owner_{action}" BEFORE {event} ON "{table}" '
                f"WHEN NEW.tenant_id <> '{tenant_id}' OR NEW.owner_id <> '{owner_id}' "
                "BEGIN SELECT RAISE(ABORT, 'tenant owner context missing or inconsistent'); END"
            )
            if normalized_trigger_sql(item.sql or "") != normalized_trigger_sql(expected_ddl):
                raise RuntimeError("Ownership root write guard differs from the canonical migration trigger.")

    def scope_for_owner(self, owner_id: str, tenant_id: str) -> OwnershipScope:
        if self.mode != "tenant_roles":
            return OwnershipScope(owner_id, tenant_id)
        with self.database.engine.connect() as connection:
            rows = connection.execute(text("""
                SELECT id,tenant_id,role FROM tenant_memberships
                WHERE oidc_issuer=(SELECT oidc_issuer FROM tenant_memberships WHERE id=:owner)
                  AND oidc_subject=(SELECT oidc_subject FROM tenant_memberships WHERE id=:owner)
                  AND active=1 ORDER BY tenant_id,id
            """), {"owner":owner_id}).all()
        if len(rows) != 1 or rows[0].id != owner_id or rows[0].tenant_id != tenant_id or rows[0].role not in {"admin", "member"}:
            raise RuntimeError("Queued job owner must have exactly one active tenant membership.")
        return OwnershipScope(owner_id, tenant_id, rows[0].role)

    def assign_created_root(self, session, model, root_id: str, scope: OwnershipScope | None) -> None:
        if self.mode != "tenant_roles" or scope is None:
            return
        table = model.__tablename__
        if table not in {"agents", "conversations", "rooms"}:
            raise RuntimeError("Only root resources can receive tenant ownership.")
        # The UPDATE acquires SQLite's write reservation before re-checking active membership.
        # Any failure below aborts the enclosing transaction, so the temporary DB default is never visible.
        result = session.execute(text(
            f'UPDATE "{table}" SET tenant_id=:tenant,owner_id=:owner WHERE id=:id'
        ), {"tenant":scope.tenant_id,"owner":scope.owner_id,"id":root_id})
        if result.rowcount != 1:
            raise RuntimeError("Root ownership assignment did not affect exactly one row.")
        membership = session.execute(text("""
            SELECT role FROM tenant_memberships
            WHERE id=:owner AND tenant_id=:tenant AND active=1
        """), {"owner":scope.owner_id,"tenant":scope.tenant_id}).one_or_none()
        if membership is None or membership._mapping["role"] != scope.role:
            raise PermissionError("Tenant membership changed during root creation.")
        if table == "agents" and scope.role != "admin":
            raise PermissionError("Only tenant admins can create agents.")
        row = session.execute(text(
            f'SELECT tenant_id,owner_id FROM "{table}" WHERE id=:id'
        ), {"id":root_id}).one_or_none()
        if row is None or row._mapping["tenant_id"] != scope.tenant_id or row._mapping["owner_id"] != scope.owner_id:
            raise RuntimeError("Root owner/tenant binding failed before commit.")

    def scope_for(self, principal: Principal | None) -> OwnershipScope | None:
        if self.mode == "off":
            return None
        if principal is None or self.auth_config is None:
            return None
        if self.mode == "tenant_roles":
            if principal.issuer != self.auth_config.issuer:
                raise PermissionError("Authenticated principal issuer is not configured.")
            try:
                with self.database.engine.connect() as connection:
                    matches = connection.execute(text("""
                        SELECT id,tenant_id,role FROM tenant_memberships
                        WHERE oidc_issuer=:issuer AND oidc_subject=:subject AND active=1
                        ORDER BY tenant_id,id
                    """), {"issuer":principal.issuer,"subject":principal.subject}).all()
            except Exception as exc:
                raise RuntimeError("Tenant membership lookup failed.") from exc
            if len(matches) != 1:
                raise PermissionError("Principal does not have exactly one active tenant membership.")
            membership = matches[0]
            if membership.tenant_id != principal.tenant_id or ("tenant:" + membership.role) not in principal.scopes:
                raise PermissionError("Principal membership changed during request authorization.")
            return OwnershipScope(membership.id, membership.tenant_id, membership.role)
        if (principal.issuer != self.auth_config.issuer or principal.subject != self.auth_config.legacy_subject
                or principal.tenant_id != self.auth_config.tenant_id or "legacy:operator" not in principal.scopes):
            raise PermissionError("Authenticated principal lacks the legacy owner authorization.")
        owner_id = self._resolve_mapping(principal.issuer, principal.subject, principal.tenant_id)
        return OwnershipScope(owner_id, principal.tenant_id)


def scoped_root(model, scope: OwnershipScope):
    # Table names come only from internal ORM classes, never request input.
    table = model.__tablename__
    if table == "agents" and scope.role in {"admin", "member"}:
        predicate = f"{table}.tenant_id=:tenant_id"
        params = {"tenant_id": scope.tenant_id}
        if scope.role == "member":
            predicate += f" AND {table}.published=1 AND {table}.enabled=1"
        return text(predicate).bindparams(**params)
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
