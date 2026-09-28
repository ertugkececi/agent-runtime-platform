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
