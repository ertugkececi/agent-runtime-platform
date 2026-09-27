from __future__ import annotations

import base64
import json
import re
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from agent_runtime_platform.api import create_app
from agent_runtime_platform.providers import ProviderRegistry
from agent_runtime_platform.models import AuthSession


def b64int(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class FakeOIDCIssuer:
    def __init__(self):
        self.good_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.bad_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.variant = "valid"
        self.used_codes: set[str] = set()
        issuer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def send_json(self, value, status=200):
                body = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/.well-known/openid-configuration":
                    self.send_json({
                        "issuer": issuer.url,
                        "authorization_endpoint": issuer.url + "/authorize",
                        "token_endpoint": issuer.url + "/token",
                        "jwks_uri": issuer.url + "/jwks",
                        "token_endpoint_auth_methods_supported": ["none"],
                    })
                elif self.path == "/jwks":
                    numbers = issuer.good_key.public_key().public_numbers()
                    self.send_json({"keys": [{
                        "kty": "RSA", "kid": "test-key", "use": "sig", "alg": "RS256",
                        "n": b64int(numbers.n), "e": b64int(numbers.e),
                    }]})
                else:
                    self.send_json({"error": "not_found"}, 404)

            def do_POST(self):
                if self.path != "/token":
                    self.send_json({"error": "not_found"}, 404)
                    return
                size = int(self.headers.get("Content-Length", "0"))
                form = parse_qs(self.rfile.read(size).decode())
                code = form.get("code", [""])[0]
                verifier = form.get("code_verifier", [""])[0]
                challenge = base64.urlsafe_b64encode(__import__("hashlib").sha256(verifier.encode()).digest()).rstrip(b"=").decode() if verifier else ""
                if code in issuer.used_codes or not verifier or len(verifier) < 43 or challenge != issuer.expected_challenge:
                    self.send_json({"error": "invalid_grant"}, 400)
                    return
                issuer.used_codes.add(code)
                now = int(time.time())
                claims = {
                    "iss": issuer.url, "sub": "legacy-operator", "aud": "runtime-client",
                    "iat": now, "exp": now + 300, "nonce": "",
                }
                if issuer.variant == "issuer": claims["iss"] = "https://attacker.example"
                if issuer.variant == "audience": claims["aud"] = "other-client"
                if issuer.variant == "expired": claims["exp"] = now - 300
                if issuer.variant == "subject": claims["sub"] = "another-person"
                if issuer.variant == "azp_wrong": claims["azp"] = "other-client"
                if issuer.variant == "multi_no_azp": claims["aud"] = ["runtime-client", "other-client"]
                if issuer.variant == "multi_wrong": claims["aud"] = ["runtime-client", "other-client"]; claims["azp"] = "other-client"
                if issuer.variant == "multi_valid": claims["aud"] = ["runtime-client", "other-client"]; claims["azp"] = "runtime-client"
                state = form.get("state", [None])[0]
                claims["nonce"] = issuer.expected_nonce
                if issuer.variant == "nonce": claims["nonce"] = "wrong-nonce"
                signing_key = issuer.bad_key if issuer.variant == "signature" else issuer.good_key
                token = jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "test-key"})
                self.send_json({"access_token": "fixture-access-token", "token_type": "Bearer", "id_token": token})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.expected_nonce = ""
        self.expected_challenge = ""
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class FakeAuthProvider:
    def generate(self, agent, history, *, allow_handoff=False):
        return "Authenticated response"


@pytest.fixture
def oidc_runtime(monkeypatch):
    issuer = FakeOIDCIssuer()
    monkeypatch.setenv("AGENT_RUNTIME_AUTH_MODE", "oidc")
    monkeypatch.setenv("AGENT_RUNTIME_OIDC_ISSUER", issuer.url)
    monkeypatch.setenv("AGENT_RUNTIME_OIDC_CLIENT_ID", "runtime-client")
    monkeypatch.setenv("AGENT_RUNTIME_OIDC_REDIRECT_URI", "https://testserver/auth/callback")
    monkeypatch.setenv("AGENT_RUNTIME_OIDC_LEGACY_SUB", "legacy-operator")
    app = create_app(database_url="sqlite:///:memory:", providers=ProviderRegistry({"openai": FakeAuthProvider()}))
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
        yield client, issuer
    app.state.database.dispose()
    issuer.close()


def start_login(client):
    response = client.get("/auth/login")
    assert response.status_code == 302
    flow_cookie = response.headers["set-cookie"]
    assert "__Host-agent_runtime_oidc_flow=" in flow_cookie
    assert "Secure" in flow_cookie and "HttpOnly" in flow_cookie and "SameSite=lax" in flow_cookie
    params = parse_qs(urlsplit(response.headers["location"]).query)
    assert params["code_challenge_method"] == ["S256"]
    assert len(params["code_challenge"][0]) == 43
    return params


def finish_login(client, issuer, params, *, code="single-use-code"):
    issuer.expected_nonce = params["nonce"][0]
    issuer.expected_challenge = params["code_challenge"][0]
    return client.get("/auth/callback", params={"code": code, "state": params["state"][0]})


def test_oidc_pkce_login_issues_opaque_session_and_logout_revokes_it(oidc_runtime):
    client, issuer = oidc_runtime
    params = start_login(client)
    response = finish_login(client, issuer, params)
    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    raw_session = client.cookies.get("__Host-agent_runtime_session")
    assert raw_session
    with client.app.state.database.session() as db:
        record = db.query(AuthSession).one()
        assert record.session_hash != raw_session
        assert "legacy-operator" == record.subject
        persisted = " ".join(str(getattr(record, column)) for column in ("session_hash", "subject", "issuer", "tenant_id", "scopes"))
        assert raw_session not in persisted and "single-use-code" not in persisted and "fixture-access-token" not in persisted
    session = client.get("/auth/session").json()
    assert session["authenticated"] and session["csrf_token"]
    assert client.get("/auth/session").json()["csrf_token"] == session["csrf_token"]
    with client.app.state.database.session() as db:
        assert session["csrf_token"] not in str(db.query(AuthSession).one().session_hash)
    assert client.get("/agents").status_code == 200
    assert client.get("/agents", headers={"Authorization": "Bearer opaque-machine-token"}).status_code == 401

    denied = client.post("/agents", json={"name": "No CSRF"})
    assert denied.status_code == 403
    wrong_origin = client.post("/agents", headers={"Origin": "https://attacker.example", "X-CSRF-Token": session["csrf_token"]}, json={"name": "No"})
    assert wrong_origin.status_code == 403
    created = client.post("/agents", headers={"Origin": "https://testserver", "X-CSRF-Token": session["csrf_token"]}, json={
        "name": "Test", "instructions": "test", "model_provider": "openai", "model_name": "test-model",
    })
    assert created.status_code == 201, created.text
    logged_out = client.post("/auth/logout", headers={"Origin": "https://testserver", "X-CSRF-Token": session["csrf_token"]})
    assert logged_out.status_code == 204
    assert client.get("/agents").status_code == 401
    with client.app.state.database.session() as db:
        assert db.query(AuthSession).one().revoked_at is not None


def test_open_protected_route_denial_docs_hidden_and_public_health(oidc_runtime):
    client, _issuer = oidc_runtime
    assert client.get("/").status_code == 200
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/agents").status_code == 401
    assert client.get("/runs/nonexistent").status_code == 401
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


@pytest.mark.parametrize("variant", ["issuer", "audience", "signature", "nonce", "expired", "subject", "azp_wrong", "multi_no_azp", "multi_wrong"])
def test_oidc_rejects_invalid_claims_and_signatures(oidc_runtime, variant):
    client, issuer = oidc_runtime
    issuer.variant = variant
    params = start_login(client)
    response = finish_login(client, issuer, params, code=f"{variant}-code")
    assert response.status_code == (403 if variant == "subject" else 400)
    assert "set-cookie" not in response.headers
    assert client.get("/agents").status_code == 401


def test_state_is_single_use_and_code_replay_is_rejected(oidc_runtime):
    client, issuer = oidc_runtime
    params = start_login(client)
    bad_state = client.get("/auth/callback", params={"code": "unused", "state": "attacker-state"})
    assert bad_state.status_code == 400
    valid = finish_login(client, issuer, params, code="replay-code")
    assert valid.status_code == 303
    replay_state = client.get("/auth/callback", params={"code": "replay-code", "state": params["state"][0]})
    assert replay_state.status_code == 400
    params = start_login(client)
    first = finish_login(client, issuer, params, code="same-code")
    assert first.status_code == 303
    second_params = start_login(client)
    issuer.expected_nonce = second_params["nonce"][0]
    replay_code = client.get("/auth/callback", params={"code": "same-code", "state": second_params["state"][0]})
    assert replay_code.status_code == 400


def test_wrong_http_methods_are_not_safe_and_mode_off_keeps_routes_available(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_AUTH_MODE", "off")
    app = create_app(database_url="sqlite:///:memory:")
    with TestClient(app) as client:
        assert client.get("/auth/session").json() == {"authenticated": False, "auth_enabled": False}
        assert client.get("/agents").status_code == 200
        assert client.get("/docs").status_code == 200
    app.state.database.dispose()


def test_expired_server_side_session_is_denied(oidc_runtime):
    client, issuer = oidc_runtime
    params = start_login(client)
    assert finish_login(client, issuer, params).status_code == 303
    with client.app.state.database.session() as db:
        record = db.query(AuthSession).one()
        record.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    assert client.get("/agents").status_code == 401
    assert client.get("/auth/session").json() == {"authenticated": False, "auth_enabled": True}


def test_callback_state_is_bound_to_the_browser_that_started_login(oidc_runtime):
    client, issuer = oidc_runtime
    params = start_login(client)
    with TestClient(client.app, base_url="https://testserver", follow_redirects=False) as other_browser:
        stolen_callback = other_browser.get("/auth/callback", params={"code": "cross-browser-code", "state": params["state"][0]})
    assert stolen_callback.status_code == 400
    # A failed cross-browser attempt does not burn the legitimate browser's state.
    legitimate = finish_login(client, issuer, params, code="cross-browser-code")
    assert legitimate.status_code == 303


def test_multiple_pending_login_flows_do_not_break_expiry_cleanup(oidc_runtime):
    client, issuer = oidc_runtime
    first = start_login(client)
    second = start_login(client)
    assert finish_login(client, issuer, first, code="first-pending-code").status_code == 303
    assert finish_login(client, issuer, second, code="second-pending-code").status_code == 303


def test_multi_audience_requires_and_accepts_matching_authorized_party(oidc_runtime):
    client, issuer = oidc_runtime
    issuer.variant = "multi_valid"
    params = start_login(client)
    assert finish_login(client, issuer, params, code="multi-audience-code").status_code == 303


def test_pending_flow_expiry_cleanup_and_two_pending_starts(oidc_runtime):
    from agent_runtime_platform.auth import LoginFlow

    client, issuer = oidc_runtime
    auth = client.app.state.auth
    auth._flows["expired-flow"] = LoginFlow("n", "v", "binding", time.time() - 1)
    first = start_login(client)
    second = start_login(client)
    assert "expired-flow" not in auth._flows
    assert finish_login(client, issuer, first, code="pending-one").status_code == 303
    assert finish_login(client, issuer, second, code="pending-two").status_code == 303


def test_origin_formats_dns_ports_and_ipv6():
    from agent_runtime_platform.auth import _origin

    assert _origin("https://app.example.test:8443/auth/callback") == "https://app.example.test:8443"
    assert _origin("https://[::1]:8443/auth/callback") == "https://[::1]:8443"
    assert _origin("https://app.example.test/auth/callback") == "https://app.example.test"


@pytest.mark.parametrize("field,value", [("legacy_subject", "changed-sub"), ("issuer", "https://other.example"), ("tenant_id", "changed-tenant")])
def test_saved_sessions_stop_authenticating_after_bootstrap_config_changes(oidc_runtime, field, value):
    client, issuer = oidc_runtime
    params = start_login(client)
    assert finish_login(client, issuer, params).status_code == 303
    client.app.state.auth.config = replace(client.app.state.auth.config, **{field: value})
    assert client.get("/agents").status_code == 401
    assert client.get("/auth/session").json()["authenticated"] is False


def test_successful_relogin_revokes_old_session_but_failed_relogin_preserves_it(oidc_runtime):
    client, issuer = oidc_runtime
    first = start_login(client)
    assert finish_login(client, issuer, first, code="initial-session").status_code == 303
    old_session = client.cookies.get("__Host-agent_runtime_session")
    assert client.get("/agents").status_code == 200

    issuer.variant = "subject"
    failed = start_login(client)
    assert finish_login(client, issuer, failed, code="failed-relogin").status_code == 403
    assert client.cookies.get("__Host-agent_runtime_session") == old_session
    assert client.get("/agents").status_code == 200

    issuer.variant = "valid"
    second = start_login(client)
    assert finish_login(client, issuer, second, code="successful-relogin").status_code == 303
    new_session = client.cookies.get("__Host-agent_runtime_session")
    assert new_session and new_session != old_session
    assert client.get("/agents").status_code == 200
    old_browser = TestClient(client.app, base_url="https://testserver", follow_redirects=False)
    old_browser.cookies.set("__Host-agent_runtime_session", old_session)
    assert old_browser.get("/agents").status_code == 401
    new_browser = TestClient(client.app, base_url="https://testserver", follow_redirects=False)
    new_browser.cookies.set("__Host-agent_runtime_session", new_session)
    assert new_browser.get("/agents").status_code == 200


def test_authenticated_browser_can_send_human_chat_message_and_ui_uses_csrf(oidc_runtime):
    client, issuer = oidc_runtime
    params = start_login(client)
    assert finish_login(client, issuer, params).status_code == 303
    csrf = client.get("/auth/session").json()["csrf_token"]
    headers = {"Origin": "https://testserver", "X-CSRF-Token": csrf}
    agent = client.post("/agents", headers=headers, json={
        "name": "Chat", "instructions": "Answer briefly", "model_provider": "openai", "model_name": "fake",
    })
    assert agent.status_code == 201, agent.text
    conversation = client.post("/chat/conversations", headers=headers, json={"agent_id": agent.json()["id"]})
    assert conversation.status_code == 201, conversation.text
    message = client.post(f"/chat/conversations/{conversation.json()['id']}/messages", headers=headers, json={"content": "Hello"})
    assert message.status_code == 201, message.text
    assert message.json()["status"] == "completed"
    assert message.json()["messages"][-1]["content"] == "Authenticated response"
    page = client.get("/").text
    assert "initializeAuth" in page and "X-CSRF-Token" in page and "/auth/login" in page


def test_unauthenticated_shell_computes_hidden_over_layout_grid_rule(oidc_runtime):
    client, _issuer = oidc_runtime
    html = client.get("/").text
    stylesheet = html.split("<style>", 1)[1].split("</style>", 1)[0]
    rules = []
    for selector_text, declaration_text in re.findall(r"([^{}]+)\{([^{}]*)\}", stylesheet):
        declarations = {}
        for declaration in declaration_text.split(";"):
            if ":" not in declaration:
                continue
            property_name, value = declaration.split(":", 1)
            property_name = property_name.strip()
            value = value.strip()
            important = value.endswith("!important")
            declarations[property_name] = (value.removesuffix("!important").strip(), important)
        rules.append((selector_text.strip(), declarations))

    display_candidates = []
    for selector, declarations in rules:
        if selector not in {".layout", "[hidden]"} or "display" not in declarations:
            continue
        value, important = declarations["display"]
        specificity = 1 if selector in {".layout", "[hidden]"} else 0
        display_candidates.append(((important, specificity), value))
    assert (False, "grid") in [(important, value) for (important, _specificity), value in display_candidates]
    assert max(display_candidates, key=lambda candidate: candidate[0])[1] == "none"

    # The logged-out branch hides the grid shell and reveals only the sign-in panel.
    assert 'document.querySelector("main.layout").hidden = true' in html
    assert 'document.getElementById("login-required").hidden = false' in html
    assert 'document.getElementById("login-link").hidden = false' in html


def test_client_id_only_change_keeps_current_session_until_logout(oidc_runtime):
    client, issuer = oidc_runtime
    params = start_login(client)
    assert finish_login(client, issuer, params).status_code == 303
    client.app.state.auth.config = replace(client.app.state.auth.config, client_id="rotated-client-id")
    assert client.get("/agents").status_code == 200

def _migrated_role_database(path, issuer_url, *, member_subject="another-person", duplicate=False):
    from uuid import uuid4
    from sqlalchemy import text
    from agent_runtime_platform.database import Database
    from agent_runtime_platform.tenant_migration import migrate as migrate_ownership
    from agent_runtime_platform.tenant_roles_migration import migrate as migrate_roles

    url = f"sqlite:///{path}"
    db = Database(url)
    migration_issuer = "https://login.example.test"
    migrate_ownership(db.engine, migration_issuer, "legacy-operator", "legacy", path.with_suffix(".v1-backup.db"))
    migrate_roles(db.engine, migration_issuer, "legacy-operator", "legacy", path.with_suffix(".roles-backup.db"))
    import hashlib
    from agent_runtime_platform.tenant_roles_migration import ROLE_REVISION
    with db.engine.begin() as connection:
        # Simulate an HTTPS production issuer while the local OIDC fixture serves loopback HTTP.
        connection.execute(text("UPDATE tenant_owners SET oidc_issuer=:issuer"), {"issuer":issuer_url})
        connection.execute(text("UPDATE tenant_memberships SET oidc_issuer=:issuer"), {"issuer":issuer_url})
        connection.execute(text("UPDATE tenant_migration_versions SET mapping_sha256=:digest WHERE revision='tenant_ownership_v1'"),
            {"digest":hashlib.sha256((issuer_url+"\0legacy-operator\0legacy").encode()).hexdigest()})
        connection.execute(text("UPDATE tenant_migration_versions SET mapping_sha256=:digest WHERE revision=:revision"),
            {"revision":ROLE_REVISION,"digest":hashlib.sha256((ROLE_REVISION+"\0"+issuer_url+"\0legacy-operator\0legacy").encode()).hexdigest()})
        connection.execute(text("INSERT INTO tenants(id,created_at) VALUES ('tenant-b',CURRENT_TIMESTAMP)"))
        connection.execute(text("""
          INSERT INTO tenant_memberships
            (id,tenant_id,oidc_issuer,oidc_subject,role,active,created_at)
          VALUES (:id,'legacy',:issuer,:subject,'member',1,CURRENT_TIMESTAMP)
        """), {"id": str(uuid4()), "issuer": issuer_url, "subject": member_subject})
        if duplicate:
            connection.execute(text("""
              INSERT INTO tenant_memberships
                (id,tenant_id,oidc_issuer,oidc_subject,role,active,created_at)
              VALUES (:id,'tenant-b',:issuer,:subject,'admin',1,CURRENT_TIMESTAMP)
            """), {"id": str(uuid4()), "issuer": issuer_url, "subject": member_subject})
    db.dispose()
    return url


def test_tenant_role_login_resolves_subject_and_refreshes_live_role(tmp_path, monkeypatch):
    from sqlalchemy import text

    issuer = FakeOIDCIssuer()
    db_url = _migrated_role_database(tmp_path / "tenant-role-auth.db", issuer.url)
    from sqlalchemy import create_engine
    check = create_engine(db_url)
    with check.connect() as connection:
        assert connection.execute(text("SELECT oidc_issuer,oidc_subject,tenant_id,role,active FROM tenant_memberships WHERE oidc_subject='another-person'")).one() == (issuer.url,"another-person","legacy","member",1)
    check.dispose()
    monkeypatch.setenv("AGENT_RUNTIME_AUTH_MODE", "oidc")
    monkeypatch.setenv("AGENT_RUNTIME_RESOURCE_AUTH_MODE", "tenant_roles")
    monkeypatch.setenv("AGENT_RUNTIME_OIDC_ISSUER", issuer.url)
    monkeypatch.setenv("AGENT_RUNTIME_OIDC_CLIENT_ID", "runtime-client")
    monkeypatch.setenv("AGENT_RUNTIME_OIDC_REDIRECT_URI", "https://testserver/auth/callback")
    monkeypatch.setenv("AGENT_RUNTIME_OIDC_LEGACY_SUB", "legacy-operator")
    issuer.variant = "subject"
    app = create_app(database_url=db_url, providers=ProviderRegistry({"openai": FakeAuthProvider()}))
    try:
        with app.state.database.session() as session:
            assert app.state.auth._active_membership(session, issuer.url, "another-person") is not None
        with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
            params = start_login(client)
            response = finish_login(client, issuer, params, code="member-login")
            assert response.status_code == 303, response.text
            raw = client.cookies.get("__Host-agent_runtime_session")
            principal = app.state.auth.load_principal(raw)
            assert principal.subject == "another-person"
            assert principal.scopes == ("tenant:member",)
            with app.state.database.engine.begin() as connection:
                connection.execute(text("""
                  UPDATE tenant_memberships SET role='admin'
                  WHERE oidc_subject='another-person' AND tenant_id='legacy'
                """))
            refreshed = app.state.auth.load_principal(raw)
            assert refreshed.scopes == ("tenant:admin",)
            with app.state.database.engine.begin() as connection:
                connection.execute(text("""
                  UPDATE tenant_memberships SET active=0 WHERE oidc_subject='another-person'
                """))
            assert app.state.auth.load_principal(raw) is None
            assert client.get("/agents").status_code == 401
    finally:
        app.state.database.dispose()
        issuer.close()


def test_tenant_role_login_rejects_unknown_and_ambiguous_memberships(tmp_path, monkeypatch):
    issuer = FakeOIDCIssuer()
    db_url = _migrated_role_database(tmp_path / "tenant-role-ambiguous.db", issuer.url, duplicate=True)
    monkeypatch.setenv("AGENT_RUNTIME_AUTH_MODE", "oidc")
    monkeypatch.setenv("AGENT_RUNTIME_RESOURCE_AUTH_MODE", "tenant_roles")
    monkeypatch.setenv("AGENT_RUNTIME_OIDC_ISSUER", issuer.url)
    monkeypatch.setenv("AGENT_RUNTIME_OIDC_CLIENT_ID", "runtime-client")
    monkeypatch.setenv("AGENT_RUNTIME_OIDC_REDIRECT_URI", "https://testserver/auth/callback")
    monkeypatch.setenv("AGENT_RUNTIME_OIDC_LEGACY_SUB", "legacy-operator")
    issuer.variant = "subject"
    app = create_app(database_url=db_url, providers=ProviderRegistry({"openai": FakeAuthProvider()}))
    try:
        with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
            params = start_login(client)
            denied = finish_login(client, issuer, params, code="ambiguous-login")
            assert denied.status_code == 403
            assert "__Host-agent_runtime_session" not in client.cookies
            assert client.get("/agents").status_code == 401
    finally:
        app.state.database.dispose()
        issuer.close()


def test_tenant_roles_flag_fails_closed_without_oidc_and_invalid_modes(monkeypatch):
    from agent_runtime_platform.auth import OIDCConfig

    monkeypatch.setenv("AGENT_RUNTIME_AUTH_MODE", "off")
    monkeypatch.setenv("AGENT_RUNTIME_RESOURCE_AUTH_MODE", "tenant_roles")
    with pytest.raises(RuntimeError, match="requires OIDC"):
        OIDCConfig.from_environment()
    monkeypatch.setenv("AGENT_RUNTIME_AUTH_MODE", "oidc")
    monkeypatch.setenv("AGENT_RUNTIME_RESOURCE_AUTH_MODE", "nonsense")
    with pytest.raises(RuntimeError, match="must be"):
        OIDCConfig.from_environment()


def test_tenant_roles_route_policy_and_transaction_owner_binding(tmp_path, monkeypatch):
    import hashlib
    import hmac
    from sqlalchemy import select, text
    from datetime import timedelta

    issuer = FakeOIDCIssuer()
    db_url = _migrated_role_database(tmp_path / "tenant-role-routes.db", issuer.url)
    monkeypatch.setenv("AGENT_RUNTIME_AUTH_MODE", "oidc")
    monkeypatch.setenv("AGENT_RUNTIME_RESOURCE_AUTH_MODE", "tenant_roles")
    monkeypatch.setenv("AGENT_RUNTIME_OIDC_ISSUER", issuer.url)
    monkeypatch.setenv("AGENT_RUNTIME_OIDC_CLIENT_ID", "runtime-client")
    monkeypatch.setenv("AGENT_RUNTIME_OIDC_REDIRECT_URI", "https://testserver/auth/callback")
    monkeypatch.setenv("AGENT_RUNTIME_OIDC_LEGACY_SUB", "legacy-operator")
    app = create_app(database_url=db_url, providers=ProviderRegistry({"openai": FakeAuthProvider()}))
    try:
        from agent_runtime_platform.resource_auth import OwnershipScope
        with app.state.database.engine.connect() as connection:
            admin_id = connection.execute(text("SELECT id FROM tenant_memberships WHERE oidc_subject='legacy-operator'")).scalar_one()
        published = app.state.runtime.create_agent({"name":"shared published", "instructions":"fixture",
            "model_provider":"openai", "model_name":"fixture", "published":True}, OwnershipScope(admin_id,"legacy","admin"))["id"]
        published_two = app.state.runtime.create_agent({"name":"shared published two", "instructions":"fixture",
            "model_provider":"openai", "model_name":"fixture", "published":True}, OwnershipScope(admin_id,"legacy","admin"))["id"]
        hidden = app.state.runtime.create_agent({"name":"admin private", "instructions":"fixture",
            "model_provider":"openai", "model_name":"fixture", "published":False}, OwnershipScope(admin_id,"legacy","admin"))["id"]
        with app.state.database.engine.begin() as connection:
            connection.execute(text("INSERT INTO tenant_memberships (id,tenant_id,oidc_issuer,oidc_subject,role,active,created_at) VALUES ('00000000-0000-0000-0000-000000000002','tenant-b',:issuer,'tenant-b-admin','admin',1,CURRENT_TIMESTAMP)"), {"issuer":issuer.url})
        from agent_runtime_platform.resource_auth import OwnershipScope
        scope_b = OwnershipScope("00000000-0000-0000-0000-000000000002","tenant-b","admin")
        b_agent_one = app.state.runtime.create_agent({"name":"tenant b one", "instructions":"fixture", "model_provider":"openai", "model_name":"fixture"}, scope_b)["id"]
        b_agent_two = app.state.runtime.create_agent({"name":"tenant b two", "instructions":"fixture", "model_provider":"openai", "model_name":"fixture"}, scope_b)["id"]
        b_conversation = app.state.runtime.create_conversation([b_agent_one,b_agent_two], scope_b)["id"]
        b_room = app.state.runtime.room_runtime.create_room("tenant b room", [b_agent_one,b_agent_two], b_agent_one, scope_b)["id"]
        b_room_run = app.state.runtime.room_runtime.enqueue_run(b_room, "tenant b queued run", scope_b)["id"]
        def authenticated(subject, role, tenant="legacy"):
            secret = "session-" + subject
            with app.state.database.session() as session:
                session.add(AuthSession(session_hash=hashlib.sha256(secret.encode()).hexdigest(),
                    issuer=issuer.url, subject=subject, tenant_id=tenant, scopes=["tenant:"+role],
                    expires_at=datetime.now(timezone.utc)+timedelta(hours=1)))
                session.commit()
            csrf = base64.urlsafe_b64encode(hmac.new(secret.encode(), b"agent-runtime-csrf-v1", hashlib.sha256).digest()).rstrip(b"=").decode()
            client = TestClient(app, base_url="https://testserver")
            client.cookies.set("__Host-agent_runtime_session", secret)
            return client, {"Origin":"https://testserver", "X-CSRF-Token":csrf}
        member, headers = authenticated("another-person", "member")
        with app.state.database.engine.connect() as connection:
            before_count = connection.execute(text("SELECT count(*) FROM conversations")).scalar_one()
            member_id = connection.execute(text("SELECT id FROM tenant_memberships WHERE oidc_subject='another-person' AND active=1")).scalar_one()
        assign_root = app.state.resource_auth.assign_created_root
        def fail_after_binding(session, model, root_id, scope):
            assign_root(session, model, root_id, scope)
            raise RuntimeError("simulated failure before commit")
        monkeypatch.setattr(app.state.resource_auth, "assign_created_root", fail_after_binding)
        with pytest.raises(RuntimeError, match="simulated failure"):
            app.state.runtime.create_conversation([published,published_two], OwnershipScope(member_id,"legacy","member"))
        monkeypatch.setattr(app.state.resource_auth, "assign_created_root", assign_root)
        with app.state.database.engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM conversations")).scalar_one() == before_count
        visible = member.get("/agents").json()
        assert {agent["id"] for agent in visible} == {published, published_two}
        for claim in ("tenant_id", "owner_id", "subject", "oidc_subject", "role", "published"):
            assert member.get("/agents", params={claim: "forged"}).status_code == 422
        assert member.post("/conversations", params={"owner_id": member_id},
                           json={"agent_ids":[published]}, headers=headers).status_code == 422
        assert member.post("/conversations", json={"agent_ids":[published], "tenant_id":"tenant-b"},
                           headers=headers).status_code == 422
        monkeypatch.setattr(app.state.resource_auth, "assign_created_root",
                            lambda *args: (_ for _ in ()).throw(PermissionError("membership changed")))
        assert member.post("/conversations", json={"agent_ids":[published,published_two]},
                           headers=headers).status_code == 403
        monkeypatch.setattr(app.state.resource_auth, "assign_created_root",
                            lambda *args: (_ for _ in ()).throw(RuntimeError("membership schema unavailable")))
        assert member.post("/conversations", json={"agent_ids":[published,published_two]},
                           headers=headers).status_code == 503
        monkeypatch.setattr(app.state.resource_auth, "assign_created_root", assign_root)
        with app.state.database.engine.begin() as connection:
            connection.execute(text("UPDATE tenant_memberships SET active=0 WHERE id=:id"), {"id":admin_id})
        legacy_default_exception = app.state.runtime.create_conversation(
            [published,published_two], OwnershipScope(member_id,"legacy","member"))
        with app.state.database.engine.begin() as connection:
            connection.execute(text("UPDATE tenant_memberships SET active=1 WHERE id=:id"), {"id":admin_id})
        with app.state.database.engine.connect() as connection:
            assert connection.execute(text("SELECT owner_id FROM conversations WHERE id=:id"),
                                      {"id":legacy_default_exception["id"]}).scalar_one() == member_id
        assert member.patch(f"/agents/{published}", json={"description":"denied"}, headers=headers).status_code == 403
        assert member.patch(f"/agents/{hidden}", json={"description":"hidden"}, headers=headers).status_code == 404
        assert member.patch(f"/agents/{b_agent_one}", json={"description":"foreign"}, headers=headers).status_code == 404
        assert member.get("/codex/models").status_code == 403
        assert member.get("/mcp/tools").status_code == 403
        assert member.get("/a2a/targets").status_code == 403
        assert member.post("/agents", json={"name":"denied","instructions":"x","model_provider":"openai","model_name":"fixture"}, headers=headers).status_code == 403
        assert member.post("/conversations", json={"agent_ids":[published,hidden]}, headers=headers).status_code == 404
        created = member.post("/conversations", json={"agent_ids":[published,published_two]}, headers=headers)
        assert created.status_code == 201, created.text
        conversation_id = created.json()["id"]
        created_room = member.post("/rooms", json={"name":"member room", "participant_agent_ids":[published,published_two], "moderator_agent_id":published}, headers=headers)
        assert created_room.status_code == 201, created_room.text
        room_id = created_room.json()["id"]
        sync_run = member.post(f"/conversations/{conversation_id}/messages", json={
            "sender_agent_id":published, "recipient_agent_id":published_two, "content":"sync"}, headers=headers)
        async_run = member.post(f"/conversations/{conversation_id}/messages/async", json={
            "sender_agent_id":published, "recipient_agent_id":published_two, "content":"async"}, headers=headers)
        assert sync_run.status_code == 201, sync_run.text
        assert async_run.status_code == 202, async_run.text
        assert member.get(f"/runs/{sync_run.json()['id']}").status_code == 200
        assert member.get(f"/runs/{async_run.json()['id']}").status_code == 200
        with app.state.database.engine.begin() as connection:
            connection.execute(text("UPDATE agents SET published=0 WHERE id=:id"), {"id":published_two})
        with app.state.database.session() as session:
            with pytest.raises(RuntimeError, match="no longer published"):
                app.state.runtime._assert_worker_agent_access(session, conversation_id, published_two)
        with app.state.database.engine.begin() as connection:
            connection.execute(text("UPDATE agents SET published=1 WHERE id=:id"), {"id":published_two})
        room_run = member.post(f"/rooms/{room_id}/runs", json={"content":"queued room"}, headers=headers)
        assert room_run.status_code == 202, room_run.text
        assert member.get(f"/rooms/{room_id}/runs").status_code == 200
        assert member.get(f"/runs/{room_run.json()['id']}").status_code == 200
        with app.state.database.engine.begin() as connection:
            connection.execute(text("UPDATE agents SET published=0 WHERE id=:id"), {"id":published_two})
        with app.state.database.session() as session:
            with pytest.raises(RuntimeError, match="no longer published"):
                app.state.runtime.room_runtime._assert_worker_agent_access(session, room_id, published_two)
        with app.state.database.engine.begin() as connection:
            connection.execute(text("UPDATE agents SET published=1 WHERE id=:id"), {"id":published_two})
        from agent_runtime_platform.models import AgentCapability, Task
        from agent_runtime_platform.providers import HandoffRequest
        import agent_runtime_platform.runtime as runtime_module
        with app.state.database.session() as session:
            session.add_all([AgentCapability(agent_id=published_two, capability="tenant-handoff"),
                             AgentCapability(agent_id=b_agent_one, capability="tenant-handoff")])
            session.commit()
        chat = member.post("/chat/conversations", json={"agent_id":published}, headers=headers)
        assert chat.status_code == 201, chat.text
        chat_run = member.post(f"/chat/conversations/{chat.json()['id']}/messages/async", json={"content":"delegate"}, headers=headers)
        assert chat_run.status_code == 202, chat_run.text
        monkeypatch.setattr(runtime_module, "configured_targets", lambda: [])
        handoff_state = {"run_id":chat_run.json()["id"], "history":[],
                         "handoff_request":HandoffRequest("tenant-handoff", "Use a tenant-local agent")}
        app.state.runtime._execute_handoff(handoff_state)
        app.state.runtime._execute_handoff(handoff_state)
        with app.state.database.session() as session:
            child = session.scalar(select(Task).where(Task.root_run_id==chat_run.json()["id"], Task.parent_task_id.is_not(None)))
            assert child is not None and child.agent_id == published_two
        global_chat = member.post("/chat/conversations", json={"agent_id":published}, headers=headers)
        assert global_chat.status_code == 201
        global_run = member.post(f"/chat/conversations/{global_chat.json()['id']}/messages/async",
                                 json={"content":"remote handoff"}, headers=headers)
        assert global_run.status_code == 202
        a2a_calls = {"targets": 0, "clients": 0}
        def fake_targets():
            a2a_calls["targets"] += 1
            return [{"id":"global-target","capabilities":["remote-only"]}]
        class FakeA2AClient:
            def __init__(self, target):
                a2a_calls["clients"] += 1
            def send_or_poll(self, *args, **kwargs):
                a2a_calls["clients"] += 100
                return "unexpected"
        monkeypatch.setattr(runtime_module, "configured_targets", fake_targets)
        monkeypatch.setattr(runtime_module, "A2AClient", FakeA2AClient)
        blocked = app.state.runtime._try_a2a_handoff(
            {"run_id":global_run.json()["id"], "history":[]},
            HandoffRequest("remote-only", "Do not contact global target"))
        assert blocked is None
        assert a2a_calls == {"targets": 0, "clients": 0}
        with app.state.database.session() as session:
            root = session.scalar(select(Task).where(Task.root_run_id==global_run.json()["id"], Task.parent_task_id.is_(None)))
            session.add(Task(id="remote-retry-child", root_run_id=global_run.json()["id"],
                parent_task_id=root.id, conversation_id=global_chat.json()["id"], agent_id=published,
                capability="remote-only", objective="retry", config_snapshot={"kind":"a2a"},
                remote_target_id="global-target", remote_message_id="remote-message", status="running"))
            session.commit()
        blocked_retry = app.state.runtime._try_a2a_handoff(
            {"run_id":global_run.json()["id"], "history":[]},
            HandoffRequest("remote-only", "Do not retry global target"))
        assert blocked_retry is not None and blocked_retry["allow_handoff"] is False
        assert a2a_calls == {"targets": 0, "clients": 0}
        with app.state.database.session() as session:
            retry_child = session.get(Task, "remote-retry-child")
            assert retry_child.status == "failed"
            assert retry_child.error_code == "tenant_remote_handoff_disabled"
        with app.state.database.engine.connect() as connection:
            owner = connection.execute(text("SELECT owner_id,tenant_id FROM conversations WHERE id=:id"), {"id":conversation_id}).one()
            room_owner = connection.execute(text("SELECT owner_id,tenant_id FROM rooms WHERE id=:id"), {"id":room_id}).one()
            membership = connection.execute(text("SELECT id FROM tenant_memberships WHERE oidc_subject='another-person' AND active=1")).scalar_one()
        assert owner == (membership, "legacy")
        assert room_owner == (membership, "legacy")
        admin, admin_headers = authenticated("legacy-operator", "admin")
        assert admin.get(f"/conversations/{conversation_id}").status_code == 404
        assert admin.get(f"/rooms/{room_id}").status_code == 404
        admin_chat = admin.post("/chat/conversations", json={"agent_id":hidden}, headers=admin_headers)
        assert admin_chat.status_code == 201
        admin_run = admin.post(f"/chat/conversations/{admin_chat.json()['id']}/messages/async",
                               json={"content":"admin private agent"}, headers=admin_headers)
        assert admin_run.status_code == 202
        provider_invocations = []
        provider = app.state.runtime.providers._providers["openai"]
        original_generate = provider.generate
        monkeypatch.setattr(provider, "generate", lambda *args, **kwargs: provider_invocations.append(args))
        with app.state.database.engine.begin() as connection:
            connection.execute(text("UPDATE agents SET enabled=0 WHERE id=:id"), {"id":hidden})
        with pytest.raises(RuntimeError, match="no longer enabled"):
            app.state.runtime._call_provider(admin_run.json()["id"],
                {"id":hidden,"tool_ids":[],"model_provider":"openai"}, [],
                phase="disabled-admin-retry", allow_handoff=False)
        with pytest.raises(RuntimeError, match="no longer enabled"):
            app.state.runtime._finalize_handoff({"run_id":admin_run.json()["id"],
                "target_config":{"id":hidden,"tool_ids":[],"model_provider":"openai"},
                "history":[]})
        assert provider_invocations == []
        with app.state.database.engine.begin() as connection:
            connection.execute(text("UPDATE agents SET enabled=1 WHERE id=:id"), {"id":hidden})
        monkeypatch.setattr(provider, "generate", original_generate)
        assert hidden not in {agent["id"] for agent in member.get("/agents").json()}
        assert b_agent_one not in {agent["id"] for agent in member.get("/agents").json()}
        other_admin, other_headers = authenticated("tenant-b-admin", "admin", "tenant-b")
        assert {agent["id"] for agent in other_admin.get("/agents").json()} == {b_agent_one,b_agent_two}
        b_sync_run = other_admin.post(f"/conversations/{b_conversation}/messages", json={
            "sender_agent_id":b_agent_one, "recipient_agent_id":b_agent_two, "content":"tenant b sync"}, headers=other_headers)
        b_async_run = other_admin.post(f"/conversations/{b_conversation}/messages/async", json={
            "sender_agent_id":b_agent_one, "recipient_agent_id":b_agent_two, "content":"tenant b async"}, headers=other_headers)
        assert b_sync_run.status_code == 201, b_sync_run.text
        assert b_async_run.status_code == 202, b_async_run.text
        assert member.get(f"/runs/{b_sync_run.json()['id']}").status_code == 404
        assert member.get(f"/runs/{b_async_run.json()['id']}").status_code == 404
        assert other_admin.get(f"/conversations/{conversation_id}").status_code == 404
        assert other_admin.get(f"/rooms/{room_id}").status_code == 404
        assert other_admin.get(f"/rooms/{b_room}").status_code == 200
        assert other_admin.get(f"/rooms/{b_room}/runs").status_code == 200
        assert other_admin.get(f"/runs/{b_room_run}").status_code == 200
        assert member.get(f"/conversations/{b_conversation}").status_code == 404
        assert member.get(f"/rooms/{b_room}").status_code == 404
        assert member.get(f"/runs/{b_room_run}").status_code == 404
        with app.state.database.engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO tenant_memberships
                  (id,tenant_id,oidc_issuer,oidc_subject,role,active,created_at)
                VALUES ('duplicate-member-membership','tenant-b',:issuer,'another-person','member',1,CURRENT_TIMESTAMP)
            """), {"issuer":issuer.url})
        with pytest.raises(RuntimeError, match="exactly one active tenant membership"):
            app.state.resource_auth.scope_for_owner(member_id, "legacy")
        with app.state.database.session() as session:
            with pytest.raises(RuntimeError, match="exactly one active tenant membership"):
                app.state.runtime.room_runtime._assert_worker_agent_access(session, room_id, published)
        provider_calls = []
        provider = app.state.runtime.providers._providers["openai"]
        monkeypatch.setattr(provider, "generate", lambda *args, **kwargs: provider_calls.append(args))
        with pytest.raises(RuntimeError, match="exactly one active tenant membership"):
            app.state.runtime._call_provider(sync_run.json()["id"],
                {"id":published, "tool_ids":[], "model_provider":"openai"}, [],
                phase="duplicate-owner-check", allow_handoff=False)
        before_a2a = dict(a2a_calls)
        with pytest.raises(RuntimeError, match="exactly one active tenant membership"):
            app.state.runtime._try_a2a_handoff(
                {"run_id":global_run.json()["id"], "history":[]},
                HandoffRequest("remote-only", "Do not retry after duplicate membership"))
        assert provider_calls == []
        assert a2a_calls == before_a2a
        membership_lookup = app.state.auth._active_membership
        def membership_database_failure(*args):
            raise RuntimeError("database unavailable")
        monkeypatch.setattr(app.state.auth, "_active_membership", membership_database_failure)
        assert member.get("/agents").status_code == 503
        assert member.get("/auth/session").status_code == 503
        monkeypatch.setattr(app.state.auth, "_active_membership", membership_lookup)
    finally:
        app.state.database.dispose()
        issuer.close()
