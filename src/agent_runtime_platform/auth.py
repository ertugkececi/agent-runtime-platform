from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete, or_, select, text
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp

from agent_runtime_platform.database import Database
from agent_runtime_platform.models import AuthSession

COOKIE_NAME = "__Host-agent_runtime_session"
FLOW_COOKIE_NAME = "__Host-agent_runtime_oidc_flow"
SESSION_TTL = timedelta(hours=8)
FLOW_TTL_SECONDS = 300
_ALLOWED_ALGORITHMS = {"RS256"}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LoginFlow:
    nonce: str
    verifier: str
    browser_binding_hash: str
    expires_at: float


@dataclass(frozen=True)
class Principal:
    kind: str
    issuer: str
    subject: str
    tenant_id: str
    scopes: tuple[str, ...]
    session_hash: str


@dataclass(frozen=True)
class OIDCConfig:
    issuer: str
    client_id: str
    redirect_uri: str
    legacy_subject: str
    tenant_id: str = "legacy"
    session_ttl_seconds: int = int(SESSION_TTL.total_seconds())
    resource_auth_mode: str = "off"

    @classmethod
    def from_environment(cls) -> OIDCConfig | None:
        mode = os.getenv("AGENT_RUNTIME_AUTH_MODE", "off").strip().lower()
        resource_mode = os.getenv("AGENT_RUNTIME_RESOURCE_AUTH_MODE", "off").strip().lower()
        if resource_mode not in {"off", "legacy_owner", "tenant_roles"}:
            raise RuntimeError("AGENT_RUNTIME_RESOURCE_AUTH_MODE must be 'off', 'legacy_owner', or 'tenant_roles'.")
        if mode == "off":
            if resource_mode != "off":
                raise RuntimeError("Resource authorization requires OIDC authentication.")
            return None
        if mode != "oidc":
            raise RuntimeError("AGENT_RUNTIME_AUTH_MODE must be 'off' or 'oidc'.")
        required = {
            "AGENT_RUNTIME_OIDC_ISSUER": os.getenv("AGENT_RUNTIME_OIDC_ISSUER"),
            "AGENT_RUNTIME_OIDC_CLIENT_ID": os.getenv("AGENT_RUNTIME_OIDC_CLIENT_ID"),
            "AGENT_RUNTIME_OIDC_REDIRECT_URI": os.getenv("AGENT_RUNTIME_OIDC_REDIRECT_URI"),
            "AGENT_RUNTIME_OIDC_LEGACY_SUB": os.getenv("AGENT_RUNTIME_OIDC_LEGACY_SUB"),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError("OIDC mode requires: " + ", ".join(missing))
        issuer = required["AGENT_RUNTIME_OIDC_ISSUER"]
        redirect_uri = required["AGENT_RUNTIME_OIDC_REDIRECT_URI"]
        _validate_https_or_loopback(issuer)
        _validate_https_or_loopback(redirect_uri)
        issuer_parts = urlsplit(issuer)
        redirect_parts = urlsplit(redirect_uri)
        if issuer_parts.query or issuer_parts.fragment or redirect_parts.path != "/auth/callback" or redirect_parts.query or redirect_parts.fragment:
            raise RuntimeError("OIDC issuer must not contain query/fragment and redirect URI must be the exact callback path.")
        return cls(
            issuer=issuer,
            client_id=required["AGENT_RUNTIME_OIDC_CLIENT_ID"],
            redirect_uri=redirect_uri,
            legacy_subject=required["AGENT_RUNTIME_OIDC_LEGACY_SUB"],
            tenant_id=os.getenv("AGENT_RUNTIME_OIDC_LEGACY_TENANT", "legacy"),
            session_ttl_seconds=_session_ttl_from_environment(),
            resource_auth_mode=resource_mode,
        )


def _session_ttl_from_environment() -> int:
    raw = os.getenv("AGENT_RUNTIME_OIDC_SESSION_TTL_SECONDS", str(int(SESSION_TTL.total_seconds())))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("AGENT_RUNTIME_OIDC_SESSION_TTL_SECONDS must be an integer.") from exc
    if not 300 <= value <= 86400:
        raise RuntimeError("OIDC session lifetime must be between 300 and 86400 seconds.")
    return value


def _validate_https_or_loopback(url: str) -> None:
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    if parsed.username or parsed.password or not parsed.hostname:
        raise RuntimeError("OIDC URLs must not contain user information and must include a host.")
    try:
        parsed.port
    except ValueError as exc:
        raise RuntimeError("OIDC URL has an invalid port.") from exc
    if parsed.scheme == "https" and parsed.hostname:
        return
    if parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}:
        return
    raise RuntimeError("OIDC URLs must use HTTPS (HTTP is allowed only on loopback for tests).")


def _origin(url: str) -> str:
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    port = parsed.port
    default_port = (parsed.scheme == "https" and port in (None, 443)) or (parsed.scheme == "http" and port in (None, 80))
    hostname = parsed.hostname or ""
    authority = f"[{hostname}]" if ":" in hostname else hostname
    if not default_port and port is not None:
        authority += f":{port}"
    return f"{parsed.scheme}://{authority}"


class OIDCAuth:
    def __init__(self, config: OIDCConfig, database: Database) -> None:
        self.config = config
        self.database = database
        self._flows: dict[str, LoginFlow] = {}
        self._flow_lock = threading.Lock()
        self._metadata: dict[str, Any] | None = None

    def _discovery(self) -> dict[str, Any]:
        if self._metadata is not None:
            return self._metadata
        url = self.config.issuer + "/.well-known/openid-configuration"
        with httpx.Client(timeout=5, follow_redirects=False) as client:
            response = client.get(url)
            response.raise_for_status()
            metadata = response.json()
        if metadata.get("issuer") != self.config.issuer:
            raise ValueError("OIDC discovery issuer did not match configured issuer.")
        # This implementation is a public client: it authenticates the code exchange with PKCE, not a client secret.
        supported_auth = metadata.get("token_endpoint_auth_methods_supported", ["client_secret_basic"])
        if not isinstance(supported_auth, list) or "none" not in supported_auth:
            raise ValueError("OIDC provider does not advertise public-client token exchange.")
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            endpoint = metadata.get(key)
            if not isinstance(endpoint, str) or _origin(endpoint) != _origin(self.config.issuer):
                raise ValueError("OIDC endpoint must remain on the configured issuer origin.")
            _validate_https_or_loopback(endpoint)
        self._metadata = metadata
        return metadata

    def begin_login(self, existing_browser_binding: str | None = None) -> tuple[str, str]:
        metadata = self._discovery()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        browser_binding = existing_browser_binding if existing_browser_binding and len(existing_browser_binding) >= 43 else secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        with self._flow_lock:
            now = time.time()
            self._flows = {key: flow for key, flow in self._flows.items() if flow.expires_at > now}
            if len(self._flows) >= 1024:
                raise RuntimeError("OIDC login flow capacity reached.")
            self._flows[_hash(state)] = LoginFlow(nonce, verifier, _hash(browser_binding), now + FLOW_TTL_SECONDS)
        authorization_url = metadata["authorization_endpoint"] + "?" + urlencode({
            "response_type": "code", "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri, "scope": "openid profile",
            "state": state, "nonce": nonce, "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        return authorization_url, browser_binding

    def consume_state(self, state: str, browser_binding: str | None) -> tuple[str, str]:
        key = _hash(state)
        with self._flow_lock:
            flow = self._flows.get(key)
            if flow is None or flow.expires_at <= time.time() or not browser_binding or not hmac.compare_digest(flow.browser_binding_hash, _hash(browser_binding)):
                raise ValueError("Invalid, expired, or browser-unbound OIDC state.")
            del self._flows[key]
        return flow.nonce, flow.verifier

    def complete_login(self, code: str, state: str, browser_binding: str | None, prior_session_id: str | None = None) -> str:
        nonce, verifier = self.consume_state(state, browser_binding)
        metadata = self._discovery()
        with httpx.Client(timeout=8, follow_redirects=False) as client:
            token_response = client.post(metadata["token_endpoint"], data={
                "grant_type": "authorization_code", "code": code,
                "redirect_uri": self.config.redirect_uri,
                "client_id": self.config.client_id,
                "code_verifier": verifier,
            }, headers={"Accept": "application/json"})
            token_response.raise_for_status()
            token_data = token_response.json()
            if not isinstance(token_data.get("id_token"), str):
                raise ValueError("OIDC token response did not include an ID token.")
            jwks_response = client.get(metadata["jwks_uri"])
            jwks_response.raise_for_status()
            jwks = jwks_response.json()
        header = jwt.get_unverified_header(token_data["id_token"])
        if header.get("alg") not in _ALLOWED_ALGORITHMS or not isinstance(header.get("kid"), str):
            raise ValueError("Unsupported OIDC signing algorithm or key id.")
        candidates = [key for key in jwks.get("keys", []) if key.get("kid") == header["kid"]]
        if len(candidates) != 1 or candidates[0].get("kty") != "RSA":
            raise ValueError("OIDC signing key was not found.")
        signing_key = jwt.PyJWK.from_dict(candidates[0], algorithm="RS256").key
        claims = jwt.decode(
            token_data["id_token"], signing_key, algorithms=["RS256"],
            issuer=self.config.issuer, audience=self.config.client_id, leeway=30,
            options={"require": ["iss", "sub", "aud", "exp", "iat", "nonce"]},
        )
        audience = claims.get("aud", [])
        if isinstance(audience, str):
            audience = [audience]
        authorized_party = claims.get("azp")
        if authorized_party is not None and authorized_party != self.config.client_id:
            raise ValueError("OIDC authorized party mismatch.")
        if len(audience) > 1 and authorized_party != self.config.client_id:
            raise ValueError("OIDC authorized party is required for a multi-audience token.")
        if claims.get("nonce") != nonce:
            raise ValueError("OIDC nonce mismatch.")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject or len(subject) > 500:
            raise PermissionError("The OIDC subject is invalid.")
        if self.config.resource_auth_mode == "tenant_roles":
            with self.database.session() as db:
                membership = self._active_membership(db, self.config.issuer, subject)
            if membership is None:
                raise PermissionError("No unique active tenant membership exists for this identity.")
            tenant_id, scopes = membership.tenant_id, [f"tenant:{membership.role}"]
        else:
            if not hmac.compare_digest(subject, self.config.legacy_subject):
                raise PermissionError("This identity is not the configured legacy operator.")
            tenant_id, scopes = self.config.tenant_id, ["legacy:operator"]
        session_id = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        row = AuthSession(
            session_hash=_hash(session_id), issuer=self.config.issuer, subject=subject,
            tenant_id=tenant_id, scopes=scopes,
            expires_at=now + timedelta(seconds=self.config.session_ttl_seconds),
        )
        with self.database.session() as db:
            db.execute(delete(AuthSession).where(or_(AuthSession.expires_at <= now, AuthSession.revoked_at.is_not(None))))
            if prior_session_id:
                previous = db.get(AuthSession, _hash(prior_session_id))
                if previous is not None and previous.revoked_at is None:
                    previous.revoked_at = now
            db.add(row)
            db.commit()
        return session_id

    @staticmethod
    def _active_membership(db, issuer: str, subject: str):
        try:
            rows = db.execute(text("""
                SELECT id,tenant_id,role FROM tenant_memberships
                WHERE oidc_issuer=:issuer AND oidc_subject=:subject AND active=1
                ORDER BY tenant_id,id
            """), {"issuer": issuer, "subject": subject}).all()
        except SQLAlchemyError as exc:
            raise RuntimeError("Tenant membership lookup failed.") from exc
        if len(rows) != 1 or rows[0]._mapping["role"] not in {"admin", "member"}:
            return None
        return rows[0]

    def load_principal(self, session_id: str | None) -> Principal | None:
        if not session_id:
            return None
        digest = _hash(session_id)
        now = datetime.now(timezone.utc)
        with self.database.session() as db:
            row = db.scalar(select(AuthSession).where(AuthSession.session_hash == digest))
            expires_at = row.expires_at.replace(tzinfo=timezone.utc) if row and row.expires_at.tzinfo is None else (row.expires_at if row else now)
            if row is None or row.revoked_at is not None or expires_at <= now or row.issuer != self.config.issuer:
                return None
            if self.config.resource_auth_mode == "tenant_roles":
                membership = self._active_membership(db, row.issuer, row.subject)
                if membership is None or membership.tenant_id != row.tenant_id:
                    return None
                scopes = (f"tenant:{membership.role}",)
            else:
                if row.subject != self.config.legacy_subject or row.tenant_id != self.config.tenant_id:
                    return None
                scopes = tuple(row.scopes)
            return Principal("human", row.issuer, row.subject, row.tenant_id, scopes, digest)

    def revoke(self, session_id: str | None) -> None:
        if not session_id:
            return
        with self.database.session() as db:
            row = db.get(AuthSession, _hash(session_id))
            if row is not None and row.revoked_at is None:
                row.revoked_at = datetime.now(timezone.utc)
                db.commit()

    def csrf_matches(self, session_id: str, token: str) -> bool:
        if self.load_principal(session_id) is None:
            return False
        expected = base64.urlsafe_b64encode(
            hmac.new(session_id.encode("utf-8"), b"agent-runtime-csrf-v1", hashlib.sha256).digest()
        ).rstrip(b"=").decode("ascii")
        return hmac.compare_digest(expected, token)


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, auth: OIDCAuth | None) -> None:
        super().__init__(app)
        self.auth = auth

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if path == "/auth/callback":
            # Authorization codes and state are credentials: keep them out of access logs.
            raw_query = request.scope.get("query_string", b"").decode("ascii", errors="ignore")
            request.state.oidc_callback_params = parse_qs(raw_query, keep_blank_values=True)
            request.scope["query_string"] = b""
        if self.auth is None:
            response = await call_next(request)
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("Referrer-Policy", "no-referrer")
            return response
        if path in {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}:
            return JSONResponse({"detail": "Not found"}, status_code=404, headers={"Cache-Control": "no-store"})
        if path not in {"/", "/health", "/auth/login", "/auth/callback", "/auth/session"} and request.headers.get("authorization", "").lower().startswith("bearer "):
            return JSONResponse({"detail": "Bearer credentials are not accepted."}, status_code=401, headers={"Cache-Control": "no-store"})
        if path in {"/", "/health", "/auth/login", "/auth/callback", "/auth/session"} and request.method in {"GET", "HEAD"}:
            response = await call_next(request)
        else:
            try:
                principal = self.auth.load_principal(request.cookies.get(COOKIE_NAME))
            except (RuntimeError, SQLAlchemyError):
                return JSONResponse({"detail": "Tenant membership authorization is unavailable."}, status_code=503, headers={"Cache-Control": "no-store"})
            if principal is None:
                return JSONResponse({"detail": "Authentication required."}, status_code=401, headers={"Cache-Control": "no-store"})
            request.state.principal = principal
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                expected_origin = _origin(self.auth.config.redirect_uri)
                origin = request.headers.get("origin")
                if origin != expected_origin:
                    return JSONResponse({"detail": "Invalid request origin."}, status_code=403, headers={"Cache-Control": "no-store"})
                fetch_site = request.headers.get("sec-fetch-site")
                if fetch_site is not None and fetch_site not in {"same-origin", "none"}:
                    return JSONResponse({"detail": "Cross-site request denied."}, status_code=403, headers={"Cache-Control": "no-store"})
                csrf = request.headers.get("x-csrf-token", "")
                if not csrf or not self.auth.csrf_matches(request.cookies.get(COOKIE_NAME, ""), csrf):
                    return JSONResponse({"detail": "CSRF validation failed."}, status_code=403, headers={"Cache-Control": "no-store"})
            response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.url.scheme == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        response.headers.setdefault("Cache-Control", "no-store")
        return response


def install_auth_routes(app: FastAPI, auth: OIDCAuth | None) -> None:
    @app.get("/auth/login", include_in_schema=False)
    def login(request: Request):
        if auth is None:
            raise HTTPException(status_code=404, detail="Not found")
        try:
            url, flow_cookie = auth.begin_login(request.cookies.get(FLOW_COOKIE_NAME))
        except Exception as exc:
            raise HTTPException(status_code=503, detail="OIDC provider is unavailable.") from exc
        response = RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        response.set_cookie(FLOW_COOKIE_NAME, flow_cookie, max_age=FLOW_TTL_SECONDS, secure=True, httponly=True, samesite="lax", path="/")
        return response

    @app.get("/auth/callback", include_in_schema=False)
    def callback(request: Request):
        if auth is None:
            raise HTTPException(status_code=404, detail="Not found")
        params = request.state.oidc_callback_params
        if any(len(params.get(name, [])) > 1 for name in ("code", "state", "error")):
            raise HTTPException(status_code=400, detail="OIDC authorization failed.")
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]
        error = params.get("error", [None])[0]
        if error or not code or not state:
            raise HTTPException(status_code=400, detail="OIDC authorization failed.")
        try:
            session_id = auth.complete_login(code, state, request.cookies.get(FLOW_COOKIE_NAME), request.cookies.get(COOKIE_NAME))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="This identity is not allowed.") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail="Tenant membership authorization is unavailable.") from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail="OIDC callback validation failed.") from exc
        response = RedirectResponse("/", status_code=303, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        response.set_cookie(COOKIE_NAME, session_id, max_age=auth.config.session_ttl_seconds, secure=True, httponly=True, samesite="lax", path="/")
        return response

    @app.get("/auth/session", include_in_schema=False)
    def session_status(request: Request):
        if auth is None:
            return {"authenticated": False, "auth_enabled": False}
        session_id = request.cookies.get(COOKIE_NAME)
        try:
            principal = auth.load_principal(session_id)
        except (RuntimeError, SQLAlchemyError) as exc:
            raise HTTPException(status_code=503, detail="Tenant membership authorization is unavailable.") from exc
        if principal is None:
            return {"authenticated": False, "auth_enabled": True}
        # Derive a stable synchronizer token from the opaque HttpOnly session secret.
        csrf_token = base64.urlsafe_b64encode(
            hmac.new(session_id.encode("utf-8"), b"agent-runtime-csrf-v1", hashlib.sha256).digest()
        ).rstrip(b"=").decode("ascii")
        return {"authenticated": True, "auth_enabled": True, "csrf_token": csrf_token}

    @app.post("/auth/logout", include_in_schema=False)
    def logout(request: Request, response: Response):
        if auth is not None:
            auth.revoke(request.cookies.get(COOKIE_NAME))
        result = Response(status_code=204, headers={"Cache-Control": "no-store"})
        result.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax")
        return result
