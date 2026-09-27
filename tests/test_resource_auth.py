from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from agent_runtime_platform.api import create_app
from agent_runtime_platform.auth import COOKIE_NAME, OIDCConfig
from agent_runtime_platform.models import (
    Agent, AgentCapability, AuthSession, Conversation, ConversationMember, HumanChatRun, HumanChatMessage,
    HumanChatRunEvent, HumanChatSession, Message, QueueJob, Room, RoomParticipant, RoomRun,
    RoomRunEvent, RoomRunTurn, Run, RunEvent, Task, new_id,
)
from agent_runtime_platform.providers import HandoffRequest, ProviderRegistry
from agent_runtime_platform.tenant_migration import _ids, migrate

ISSUER = "https://issuer.example.test"
SUBJECT = "legacy-owner"
TENANT = "legacy"
REDIRECT = "https://testserver/auth/callback"


class StubProvider:
    def __init__(self):
        self.calls = []

    def generate(self, agent, history, *, allow_handoff=False):
        self.calls.append(agent["id"])
        return "fixture reply"


def _auth_env(monkeypatch, *, issuer=ISSUER, subject=SUBJECT, tenant=TENANT, mode="legacy_owner"):
    monkeypatch.setenv("AGENT_RUNTIME_AUTH_MODE", "oidc")
    monkeypatch.setenv("AGENT_RUNTIME_OIDC_ISSUER", issuer)
    monkeypatch.setenv("AGENT_RUNTIME_OIDC_CLIENT_ID", "resource-test-client")
    monkeypatch.setenv("AGENT_RUNTIME_OIDC_REDIRECT_URI", REDIRECT)
    monkeypatch.setenv("AGENT_RUNTIME_OIDC_LEGACY_SUB", subject)
    monkeypatch.setenv("AGENT_RUNTIME_OIDC_LEGACY_TENANT", tenant)
    monkeypatch.setenv("AGENT_RUNTIME_RESOURCE_AUTH_MODE", mode)


def _create_agent(client, name):
    response = client.post("/agents", json={
        "name": name, "instructions": "fixture", "model_provider": "openai",
        "model_name": "fixture-model", "capabilities": [name.casefold()],
    })
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _create_room(client, name, agent_ids):
    response = client.post("/rooms", json={
        "name": name, "participant_agent_ids": agent_ids,
        "moderator_agent_id": agent_ids[0],
    })
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _insert_authenticated_session(app, *, scopes=None):
    secret = "synthetic-oidc-session-secret"
    config = app.state.auth.config
    with app.state.database.session() as db:
        db.add(AuthSession(
            session_hash=hashlib.sha256(secret.encode()).hexdigest(),
            issuer=config.issuer, subject=config.legacy_subject, tenant_id=config.tenant_id,
            scopes=list(scopes if scopes is not None else ["legacy:operator"]),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        ))
        db.commit()
    token = base64.urlsafe_b64encode(hmac.new(
        secret.encode(), b"agent-runtime-csrf-v1", hashlib.sha256
    ).digest()).rstrip(b"=").decode()
    client = TestClient(app, base_url="https://testserver")
    client.cookies.set(COOKIE_NAME, secret)
    headers = {"Origin": "https://testserver", "X-CSRF-Token": token}
    return client, headers, secret


def _count(db, model):
    with db.session() as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def test_authz_default_off_keeps_anonymous_legacy_routes(monkeypatch):
    monkeypatch.delenv("AGENT_RUNTIME_AUTH_MODE", raising=False)
    monkeypatch.delenv("AGENT_RUNTIME_RESOURCE_AUTH_MODE", raising=False)
    app = create_app("sqlite:///:memory:", ProviderRegistry({"openai": StubProvider()}))
    with TestClient(app) as client:
        assert client.get("/agents").status_code == 200
    app.state.database.dispose()


def test_legacy_owner_mode_requires_oidc_migration_and_exact_mapping(tmp_path, monkeypatch):
    path = tmp_path / "unmigrated.db"
    _auth_env(monkeypatch)
    with pytest.raises(RuntimeError, match="Ownership migration"):
        create_app(f"sqlite:///{path}", ProviderRegistry({"openai": StubProvider()}))

    # A valid migration for a different OIDC owner must not open resource mode.
    monkeypatch.setenv("AGENT_RUNTIME_AUTH_MODE", "off")
    monkeypatch.setenv("AGENT_RUNTIME_RESOURCE_AUTH_MODE", "off")
    base = create_app(f"sqlite:///{path}", ProviderRegistry({"openai": StubProvider()}))
    migrate(base.state.database.engine, ISSUER, "someone-else", TENANT, tmp_path / "backup.db")
    base.state.database.dispose()
    _auth_env(monkeypatch)
    with pytest.raises(RuntimeError, match="mapping"):
        create_app(f"sqlite:///{path}", ProviderRegistry({"openai": StubProvider()}))


def test_migrated_two_owner_route_matrix_and_no_side_effects(tmp_path, monkeypatch):
    path = tmp_path / "synthetic-migrated.db"
    database_url = f"sqlite:///{path}"
    monkeypatch.setenv("AGENT_RUNTIME_AUTH_MODE", "off")
    monkeypatch.setenv("AGENT_RUNTIME_RESOURCE_AUTH_MODE", "off")
    legacy_app = create_app(database_url, ProviderRegistry({"openai": StubProvider()}))
    with TestClient(legacy_app) as legacy:
        agent_a = _create_agent(legacy, "Owner A one")
        agent_b = _create_agent(legacy, "Owner A two")
        agent_c = _create_agent(legacy, "Owner B one")
        agent_d = _create_agent(legacy, "Owner B two")
        own_conversation = legacy.post("/conversations", json={"agent_ids": [agent_a, agent_b]}).json()["id"]
        foreign_conversation = legacy.post("/conversations", json={"agent_ids": [agent_c, agent_d]}).json()["id"]
        foreign_room = _create_room(legacy, "Owner B room", [agent_c, agent_d])
        foreign_chat = legacy.post("/chat/conversations", json={"agent_id": agent_c}).json()["id"]
        foreign_run = legacy.post(
            f"/chat/conversations/{foreign_chat}/messages/async", json={"content": "queued for fixture"}
        ).json()["id"]
        foreign_room_run = legacy.post(f"/rooms/{foreign_room}/runs", json={"content": "room fixture"}).json()["id"]
        own_room = _create_room(legacy, "Owner A room", [agent_a, agent_b])
    migrate(legacy_app.state.database.engine, ISSUER, SUBJECT, TENANT, tmp_path / "migration-backup.db")
    legacy_app.state.database.dispose()

    _auth_env(monkeypatch)
    app = create_app(database_url, ProviderRegistry({"openai": StubProvider()}))
    _, owner_id = _ids(ISSUER, SUBJECT, TENANT)
    other_tenant, other_owner = "tenant-b", str(uuid4())
    # Add a second mapped owner only to this disposable synthetic fixture. Remove
    # the fixed legacy write guards briefly to backfill test rows, then restore them.
    with app.state.database.engine.begin() as connection:
        connection.execute(text("INSERT INTO tenants(id,created_at) VALUES (:id,CURRENT_TIMESTAMP)"), {"id": other_tenant})
        connection.execute(text("INSERT INTO tenant_owners(id,tenant_id,oidc_issuer,oidc_subject) VALUES (:id,:tenant,:issuer,:subject)"), {
            "id": other_owner, "tenant": other_tenant, "issuer": "https://other.example.test", "subject": "owner-b",
        })
        for table in ("agents", "conversations", "rooms"):
            for action in ("insert", "update"):
                connection.execute(text(f'DROP TRIGGER "trg_{table}_legacy_owner_{action}"'))
        for root_id, table in ((agent_c, "agents"), (agent_d, "agents"),
                               (foreign_conversation, "conversations"), (foreign_chat, "conversations"),
                               (foreign_room, "rooms")):
            connection.execute(text(f"UPDATE {table} SET tenant_id=:tenant, owner_id=:owner WHERE id=:id"), {
                "tenant": other_tenant, "owner": other_owner, "id": root_id,
            })
        from agent_runtime_platform.tenant_migration import _create_write_guard
        _create_write_guard(connection, "sqlite", TENANT, owner_id)

    client, headers, session_secret = _insert_authenticated_session(app)
    try:
        assert client.get("/agents").status_code == 200
        assert {agent["id"] for agent in client.get("/agents").json()} == {agent_a, agent_b}
        assert client.patch(f"/agents/{agent_a}", json={"description": "owner update"}, headers=headers).status_code == 200
        own_created_agent = client.post("/agents", json={
            "name": "owner-created", "instructions": "fixture", "model_provider": "openai", "model_name": "fixture",
        }, headers=headers)
        assert own_created_agent.status_code == 201
        own_created_conversation = client.post("/conversations", json={"agent_ids": [agent_a, agent_b]}, headers=headers)
        assert own_created_conversation.status_code == 201
        assert client.get(f"/conversations/{own_created_conversation.json()['id']}").status_code == 200
        own_chat = client.post("/chat/conversations", json={"agent_id": agent_a}, headers=headers)
        assert own_chat.status_code == 201
        sync_chat_run = client.post(f"/chat/conversations/{own_chat.json()['id']}/messages", json={"content": "sync"}, headers=headers)
        async_chat_run = client.post(f"/chat/conversations/{own_chat.json()['id']}/messages/async", json={"content": "async"}, headers=headers)
        assert sync_chat_run.status_code == 201 and async_chat_run.status_code == 202
        assert client.get(f"/runs/{sync_chat_run.json()['id']}").status_code == 200
        assert client.get(f"/runs/{async_chat_run.json()['id']}").status_code == 200
        sync_agent_run = client.post(f"/conversations/{own_conversation}/messages", json={
            "sender_agent_id": agent_a, "recipient_agent_id": agent_b, "content": "sync",
        }, headers=headers)
        async_agent_run = client.post(f"/conversations/{own_conversation}/messages/async", json={
            "sender_agent_id": agent_a, "recipient_agent_id": agent_b, "content": "async",
        }, headers=headers)
        assert sync_agent_run.status_code == 201 and async_agent_run.status_code == 202
        assert client.get(f"/runs/{sync_agent_run.json()['id']}").status_code == 200
        assert client.get(f"/runs/{async_agent_run.json()['id']}").status_code == 200
        own_room_response = client.post("/rooms", json={
            "name": "new owner room", "participant_agent_ids": [agent_a, agent_b], "moderator_agent_id": agent_a,
        }, headers=headers)
        assert own_room_response.status_code == 201
        own_room_id = own_room_response.json()["id"]
        assert client.get(f"/rooms/{own_room_id}").status_code == 200
        own_room_run = client.post(f"/rooms/{own_room_id}/runs", json={"content": "room"}, headers=headers)
        assert own_room_run.status_code == 202
        assert client.get(f"/rooms/{own_room_id}/runs").status_code == 200
        assert client.get(f"/runs/{own_room_run.json()['id']}").status_code == 200
        assert client.get(f"/conversations/{foreign_conversation}").status_code == 404
        assert client.get(f"/rooms/{foreign_room}").status_code == 404
        assert client.get(f"/rooms/{foreign_room}/runs", headers=headers).status_code == 404
        assert client.post(f"/rooms/{foreign_room}/runs", json={"content": "no"}, headers=headers).status_code == 404
        assert client.get(f"/runs/{foreign_run}").status_code == 404
        assert client.get(f"/runs/{foreign_room_run}").status_code == 404

        mutation_models = (Agent, Conversation, ConversationMember, Message, Run, RunEvent, HumanChatSession,
                           HumanChatMessage, HumanChatRun, HumanChatRunEvent, Task, Room, RoomParticipant,
                           RoomRun, RoomRunTurn, RoomRunEvent, QueueJob)
        before = tuple(_count(app.state.database, model) for model in mutation_models)
        assert client.patch(f"/agents/{agent_c}", json={"name": "stolen"}, headers=headers).status_code == 404
        assert client.post("/conversations", json={"agent_ids": [agent_a, agent_c]}, headers=headers).status_code == 404
        assert client.post("/chat/conversations", json={"agent_id": agent_c}, headers=headers).status_code == 404
        assert client.post("/rooms", json={
            "name": "cross owner", "participant_agent_ids": [agent_a, agent_c], "moderator_agent_id": agent_a,
        }, headers=headers).status_code == 404
        for suffix, body in (
            ("", {"sender_agent_id": agent_a, "recipient_agent_id": agent_b, "content": "x"}),
            ("/async", {"sender_agent_id": agent_a, "recipient_agent_id": agent_b, "content": "x"}),
        ):
            assert client.post(f"/conversations/{foreign_conversation}/messages{suffix}", json=body, headers=headers).status_code == 404
        assert client.post(f"/conversations/{own_conversation}/messages", json={
            "sender_agent_id": agent_c, "recipient_agent_id": agent_b, "content": "x",
        }, headers=headers).status_code == 404
        assert client.post(f"/conversations/{own_conversation}/messages/async", json={
            "sender_agent_id": agent_a, "recipient_agent_id": agent_c, "content": "x",
        }, headers=headers).status_code == 404
        for suffix in ("", "/async"):
            assert client.post(f"/chat/conversations/{foreign_chat}/messages{suffix}", json={"content": "x"}, headers=headers).status_code == 404
            assert client.post(f"/conversations/{foreign_chat}/messages{suffix}", json={
                "sender_agent_id": agent_c, "recipient_agent_id": agent_d, "content": "x",
            }, headers=headers).status_code == 404
        assert tuple(_count(app.state.database, model) for model in mutation_models) == before

        # Worker handoff candidate lookup and retry bind to the chat run's tenant owner.
        import agent_runtime_platform.runtime as runtime_module
        monkeypatch.setattr(runtime_module, "configured_targets", lambda: [])
        with app.state.database.session() as db:
            db.add_all([
                AgentCapability(agent_id=agent_b, capability="shared-handoff-test"),
                AgentCapability(agent_id=agent_d, capability="shared-handoff-test"),
            ])
            db.commit()
        handoff_state = {
            "run_id": foreign_run, "history": [], "handoff_request": HandoffRequest(
                capability="shared-handoff-test", task="Use only this owner's agent"
            ),
        }
        app.state.runtime._execute_handoff(handoff_state)
        with app.state.database.session() as db:
            child = db.scalar(select(Task).where(Task.root_run_id == foreign_run, Task.parent_task_id.is_not(None)))
            assert child is not None and child.agent_id == agent_d
        app.state.runtime._execute_handoff(handoff_state)
        with app.state.database.session() as db:
            assert db.scalar(select(Task).where(Task.root_run_id == foreign_run, Task.parent_task_id.is_not(None))).agent_id == agent_d

        provider = app.state.runtime.providers._providers["openai"]
        calls_before = len(provider.calls)
        with pytest.raises(RuntimeError, match="outside its parent"):
            app.state.runtime._call_provider(
                sync_agent_run.json()["id"], {"id": agent_c}, [], phase="fixture", allow_handoff=False
            )
        assert len(provider.calls) == calls_before

        # A colliding polymorphic run ID cannot choose one of two typed parents.
        with app.state.database.session() as db:
            db.add(RoomRun(id=foreign_run, room_id=own_room, content="collision", agent_snapshots=[], status="queued"))
            db.commit()
        assert client.get(f"/runs/{foreign_run}").status_code == 404
        app.state.runtime.execute_queued_run(foreign_run)
        with app.state.database.session() as db:
            assert db.get(QueueJob, foreign_run).last_error == "ambiguous_run_parent"

        # New root writes use the migration's validated fixed owner defaults.
        created = client.post("/agents", json={
            "name": "created by owner", "instructions": "fixture", "model_provider": "openai", "model_name": "fixture",
        }, headers=headers)
        assert created.status_code == 201, created.text
        with app.state.database.engine.connect() as connection:
            row = connection.execute(text("SELECT tenant_id,owner_id FROM agents WHERE id=:id"), {"id": created.json()["id"]}).one()
        assert row == (TENANT, owner_id)

        # Caller-supplied identity is rejected, and no session is 401 while an
        # authenticated principal without the legacy operator scope is 403.
        assert client.post("/agents", json={
            "name": "claimed", "instructions": "x", "model_provider": "openai", "model_name": "m",
            "tenant_id": TENANT, "owner_id": owner_id,
        }, headers=headers).status_code == 422
        assert client.post("/conversations", json={"agent_ids": [agent_a, agent_b], "tenant_id": TENANT}, headers=headers).status_code == 422
        assert client.post("/rooms", json={"name": "claimed", "participant_agent_ids": [agent_a, agent_b],
            "moderator_agent_id": agent_a, "owner_id": owner_id}, headers=headers).status_code == 422

        # Process-global catalog routes retain legacy operator access and expose only their safe summaries.
        import agent_runtime_platform.api as api_module
        monkeypatch.setattr(api_module, "configured_targets", lambda: [{"id": "target-safe", "capabilities": ["work"], "url": "secret", "token": "secret"}])
        monkeypatch.setattr(api_module, "public_tool_catalog", lambda: [{"id": "tool-safe", "name": "safe"}])
        monkeypatch.setattr(api_module, "list_codex_models", lambda: [{"id": "model-safe"}])
        assert client.get("/a2a/targets").json() == [{"id": "target-safe", "kind": "a2a", "capabilities": ["work"]}]
        assert client.get("/mcp/tools").json() == [{"id": "tool-safe", "name": "safe"}]
        assert client.get("/codex/models").json() == [{"id": "model-safe"}]

        anonymous = TestClient(app, base_url="https://testserver")
        route_matrix = [
            ("GET", "/agents", None), ("POST", "/agents", {"name":"anon","instructions":"x","model_provider":"openai","model_name":"m"}),
            ("PATCH", f"/agents/{agent_a}", {"name":"anon"}),
            ("POST", "/conversations", {"agent_ids":[agent_a,agent_b]}), ("GET", f"/conversations/{own_conversation}", None),
            ("POST", "/chat/conversations", {"agent_id":agent_a}),
            ("POST", f"/chat/conversations/{own_chat.json()['id']}/messages", {"content":"x"}),
            ("POST", f"/chat/conversations/{own_chat.json()['id']}/messages/async", {"content":"x"}),
            ("POST", f"/conversations/{own_conversation}/messages", {"sender_agent_id":agent_a,"recipient_agent_id":agent_b,"content":"x"}),
            ("POST", f"/conversations/{own_conversation}/messages/async", {"sender_agent_id":agent_a,"recipient_agent_id":agent_b,"content":"x"}),
            ("POST", "/rooms", {"name":"anon","participant_agent_ids":[agent_a,agent_b],"moderator_agent_id":agent_a}),
            ("GET", f"/rooms/{own_room}", None), ("POST", f"/rooms/{own_room}/runs", {"content":"x"}),
            ("GET", f"/rooms/{own_room}/runs", None), ("GET", f"/runs/{sync_agent_run.json()['id']}", None),
            ("GET", "/a2a/targets", None), ("GET", "/mcp/tools", None), ("GET", "/codex/models", None),
        ]
        for method, route, body in route_matrix:
            response = anonymous.request(method, route, json=body) if body is not None else anonymous.request(method, route)
            assert response.status_code == 401, f"{method} {route}: {response.status_code} {response.text}"
        with app.state.database.session() as db:
            stored = db.get(AuthSession, hashlib.sha256(session_secret.encode()).hexdigest())
            stored.scopes = []
            db.commit()
        assert client.get("/agents").status_code == 403
        with app.state.database.engine.begin() as connection:
            connection.execute(text("UPDATE tenant_migration_versions SET mapping_sha256='wrong' WHERE revision='tenant_ownership_v1'"))
        # An authenticated but unauthorized principal is 403 before any owner mapping lookup.
        assert client.get("/agents").status_code == 403
        with app.state.database.session() as db:
            stored = db.get(AuthSession, hashlib.sha256(session_secret.encode()).hexdigest())
            stored.scopes = ["legacy:operator"]
            db.commit()
        assert client.get("/agents").status_code == 503
    finally:
        client.close()
        app.state.database.dispose()

@pytest.mark.parametrize("damage", ["guard", "root_owner"])
def test_inconsistent_migrated_schema_fails_closed(tmp_path, monkeypatch, damage):
    path = tmp_path / f"bad-{damage}.db"
    database_url = f"sqlite:///{path}"
    monkeypatch.setenv("AGENT_RUNTIME_AUTH_MODE", "off")
    monkeypatch.setenv("AGENT_RUNTIME_RESOURCE_AUTH_MODE", "off")
    app = create_app(database_url, ProviderRegistry({"openai": StubProvider()}))
    with TestClient(app) as client:
        agent_id = _create_agent(client, "migration root")
    migrate(app.state.database.engine, ISSUER, SUBJECT, TENANT, tmp_path / f"{damage}-backup.db")
    app.state.database.dispose()
    if damage == "guard":
        with app.state.database.engine.begin() as connection:
            connection.execute(text('DROP TRIGGER "trg_agents_legacy_owner_update"'))
    else:
        _, owner_id = _ids(ISSUER, SUBJECT, TENANT)
        other_owner = str(uuid4())
        with app.state.database.engine.begin() as connection:
            connection.execute(text("INSERT INTO tenants(id,created_at) VALUES ('tenant-b',CURRENT_TIMESTAMP)"))
            connection.execute(text("INSERT INTO tenant_owners(id,tenant_id,oidc_issuer,oidc_subject) VALUES (:id,'tenant-b','https://other.example.test','owner-b')"), {"id": other_owner})
            connection.execute(text('DROP TRIGGER "trg_agents_legacy_owner_update"'))
            connection.execute(text("UPDATE agents SET tenant_id='tenant-b',owner_id=:owner WHERE id=:id"), {"owner": other_owner, "id": agent_id})
            from agent_runtime_platform.tenant_migration import _create_write_guard
            _create_write_guard(connection, "sqlite", TENANT, owner_id)
    _auth_env(monkeypatch)
    with pytest.raises(RuntimeError):
        create_app(database_url, ProviderRegistry({"openai": StubProvider()}))
