from __future__ import annotations

import base64
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from agent_runtime_platform.api import create_app
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


@pytest.fixture
def oidc_runtime(monkeypatch):
    issuer = FakeOIDCIssuer()
    monkeypatch.setenv("AGENT_RUNTIME_AUTH_MODE", "oidc")
    monkeypatch.setenv("AGENT_RUNTIME_OIDC_ISSUER", issuer.url)
    monkeypatch.setenv("AGENT_RUNTIME_OIDC_CLIENT_ID", "runtime-client")
    monkeypatch.setenv("AGENT_RUNTIME_OIDC_REDIRECT_URI", "https://testserver/auth/callback")
    monkeypatch.setenv("AGENT_RUNTIME_OIDC_LEGACY_SUB", "legacy-operator")
    app = create_app(database_url="sqlite:///:memory:")
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


@pytest.mark.parametrize("variant", ["issuer", "audience", "signature", "nonce", "expired", "subject"])
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
