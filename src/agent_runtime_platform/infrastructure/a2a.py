"""Small, fail-closed A2A v1 HTTP+JSON client for admin-trusted targets."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import math
import os
import socket
import time
import re
from urllib.parse import quote, urlparse

import httpx


class A2AError(RuntimeError):
    def __init__(self, code: str, *, ambiguous: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.ambiguous = ambiguous


def target_fingerprint(target: dict) -> str:
    stable = {key: target.get(key) for key in ("id", "url", "card_url", "token_env", "security_scheme", "capabilities", "allow_private")}
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def configured_targets() -> list[dict]:
    raw = os.getenv("AGENT_RUNTIME_A2A_TARGETS", "[]")
    try:
        values = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise A2AError("invalid_admin_configuration") from exc
    if not isinstance(values, list) or len(values) > 50:
        raise A2AError("invalid_admin_configuration")
    targets = []
    target_ids: set[str] = set()
    for item in values:
        if not isinstance(item, dict):
            raise A2AError("invalid_admin_configuration")
        target_id, url = item.get("id"), item.get("url")
        caps = item.get("capabilities", [])
        if not isinstance(target_id, str) or not target_id.strip() or target_id != target_id.strip() or len(target_id) > 80 or target_id in target_ids:
            raise A2AError("invalid_admin_configuration")
        target_ids.add(target_id)
        if not isinstance(caps, list) or not caps or len(caps) > 30 or any(not isinstance(x, str) or not x.strip() or len(x.strip()) > 80 for x in caps):
            raise A2AError("invalid_admin_configuration")
        if not isinstance(url, str):
            raise A2AError("invalid_admin_configuration")
        allow_private = item.get("allow_private", False) is True
        _validate_url(url, allow_private=allow_private)
        token_env = item.get("token_env")
        if token_env is not None and (not isinstance(token_env, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", token_env)):
            raise A2AError("invalid_admin_configuration")
        normalized_caps = [x.strip().casefold() for x in caps]
        if len(set(normalized_caps)) != len(normalized_caps):
            raise A2AError("invalid_admin_configuration")
        target = {**item, "id": target_id, "url": url.rstrip("/"),
                  "capabilities": normalized_caps, "allow_private": allow_private}
        for key, default, minimum, maximum in (("request_timeout_seconds", 10.0, 0.1, 30.0),
                                                ("max_wait_seconds", 60.0, 1.0, 300.0),
                                                ("poll_interval_seconds", 1.0, 0.05, 10.0)):
            try:
                value = float(item.get(key, default))
                if not math.isfinite(value):
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise A2AError("invalid_admin_configuration") from exc
            target[key] = min(max(value, minimum), maximum)
        targets.append(target)
    return targets


def _origin(parsed) -> tuple[str, str, int | None]:
    return parsed.scheme, (parsed.hostname or "").casefold(), parsed.port


def _validate_url(url: str, *, allow_private: bool, expected_origin: tuple[str, str, int | None] | None = None) -> str:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ({"https", "http"} if allow_private else {"https"}):
            raise ValueError
        if not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query:
            raise ValueError
        host = parsed.hostname.casefold()
        if expected_origin and _origin(parsed) != expected_origin:
            raise ValueError
        addresses = {ipaddress.ip_address(x[4][0]) for x in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))}
        if not addresses:
            raise ValueError
        if allow_private:
            if any(x.is_global for x in addresses):
                raise ValueError
        elif any(not x.is_global for x in addresses):
            raise ValueError
    except Exception as exc:
        raise A2AError("untrusted_target_url") from exc
    return host


def _extract_text(obj: dict) -> str:
    parts = obj.get("parts", [])
    values = [part.get("text", "") for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str)]
    return "\n".join(x for x in values if x)[:100_000]


def _status(task: dict) -> str:
    value = task.get("status")
    state = value.get("state") if isinstance(value, dict) else None
    if state in {"TASK_STATE_COMPLETED", "completed"}:
        return "completed"
    if state in {"TASK_STATE_FAILED", "failed"}:
        return "failed"
    if state in {"TASK_STATE_CANCELED", "canceled"}:
        return "canceled"
    if state in {"TASK_STATE_REJECTED", "rejected"}:
        return "rejected"
    if state in {"TASK_STATE_INPUT_REQUIRED", "input-required"}:
        return "input-required"
    if state in {"TASK_STATE_AUTH_REQUIRED", "auth-required"}:
        return "auth-required"
    return "working"


class A2AClient:
    def __init__(self, target: dict) -> None:
        self.target = target
        request_timeout = float(target.get("request_timeout_seconds", 10))
        if not math.isfinite(request_timeout):
            raise A2AError("invalid_admin_configuration")
        self.request_timeout = min(max(request_timeout, 0.1), 30.0)
        self.origin = _origin(urlparse(target["url"]))
        self.host = _validate_url(target["url"], allow_private=target["allow_private"], expected_origin=self.origin)
        self._card: dict | None = None
        self._endpoint: str | None = None
        self._tenant: str | None = None

    def close(self) -> None:
        # Each bounded async request owns and closes its HTTPX client.
        return None

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/a2a+json, application/json", "A2A-Version": "1.0"}
        token_env = self.target.get("token_env")
        if token_env:
            token = os.getenv(token_env)
            if not token:
                raise A2AError("credential_unavailable")
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _json(
        self, method: str, url: str, *, headers: dict,
        json_body: dict | None = None, params: dict[str, str] | None = None,
        deadline: float | None = None,
    ) -> dict:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise A2AError("sync_call_required")
        absolute_deadline = min(deadline or float("inf"), time.monotonic() + self.request_timeout)
        try:
            return asyncio.run(self._json_async(
                method, url, headers=headers, json_body=json_body, params=params,
                deadline=absolute_deadline,
            ))
        except TimeoutError as exc:
            raise A2AError("operation_deadline_exceeded") from exc
        except A2AError:
            raise
        except Exception as exc:
            raise A2AError("remote_request_failed") from exc

    async def _json_async(
        self, method: str, url: str, *, headers: dict,
        json_body: dict | None, params: dict[str, str] | None, deadline: float,
    ) -> dict:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise A2AError("operation_deadline_exceeded")
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(remaining), follow_redirects=False, trust_env=False,
        ) as client:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise A2AError("operation_deadline_exceeded")
            async with asyncio.timeout(remaining):
                async with client.stream(
                    method, url, headers=headers, json=json_body, params=params,
                ) as response:
                    response.raise_for_status()
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 1_048_576:
                            raise A2AError("remote_response_too_large")
                    result = json.loads(data)
        if not isinstance(result, dict):
            raise A2AError("invalid_remote_response")
        return result

    def validate_card(self, capability: str) -> None:
        parsed_origin = urlparse(self.target["url"])
        origin = f"{parsed_origin.scheme}://{parsed_origin.netloc}"
        card_url = self.target.get("card_url") or f"{origin}/.well-known/agent-card.json"
        _validate_url(card_url, allow_private=self.target["allow_private"], expected_origin=self.origin)
        try:
            card = self._json("GET", card_url, headers=self._headers())
        except Exception as exc:
            raise A2AError("agent_card_unavailable") from exc
        if not isinstance(card, dict) or not isinstance(card.get("name"), str) or not isinstance(card.get("version"), str):
            raise A2AError("invalid_agent_card")
        interfaces = card.get("supportedInterfaces")
        if not isinstance(interfaces, list):
            raise A2AError("unsupported_agent_card")
        interface = next((x for x in interfaces if isinstance(x, dict)
                          and x.get("protocolBinding") == "HTTP+JSON" and x.get("protocolVersion") == "1.0"), None)
        if not interface or not isinstance(interface.get("url"), str):
            raise A2AError("unsupported_agent_card")
        endpoint = interface["url"]
        _validate_url(endpoint, allow_private=self.target["allow_private"], expected_origin=self.origin)
        tenant = interface.get("tenant")
        if tenant is not None and (not isinstance(tenant, str) or len(tenant) > 1024):
            raise A2AError("unsupported_agent_card")
        skills = card.get("skills", [])
        card_caps = set()
        if isinstance(skills, list):
            for skill in skills:
                if isinstance(skill, dict):
                    card_caps.add(str(skill.get("id", "")).strip().casefold())
                    card_caps.update(str(x).strip().casefold() for x in skill.get("tags", []) if isinstance(x, str))
        if capability.casefold() not in card_caps:
            raise A2AError("capability_not_advertised")
        # This slice supports no auth or configured Bearer auth only.
        security = card.get("securitySchemes", {})
        if security:
            selected = self.target.get("security_scheme")
            definition = security.get(selected) if isinstance(security, dict) and selected else None
            scheme = definition.get("httpAuthSecurityScheme") if isinstance(definition, dict) else None
            if not isinstance(scheme, dict) or str(scheme.get("scheme", "")).casefold() != "bearer" or not self.target.get("token_env"):
                raise A2AError("unsupported_authentication")
        requirements = card.get("securityRequirements", [])
        if not isinstance(requirements, list):
            raise A2AError("unsupported_authentication")
        if requirements:
            selected = self.target.get("security_scheme")
            if not selected or not any(isinstance(req, dict) and isinstance(req.get("schemes"), dict)
                                       and selected in req["schemes"] and len(req["schemes"]) == 1
                                       for req in requirements):
                raise A2AError("unsupported_authentication")
        self._card, self._endpoint, self._tenant = card, endpoint.rstrip("/"), tenant

    def send_or_poll(self, capability: str, objective: str, message_id: str,
                     remote_task_id: str | None, *, on_remote_task, on_status) -> str:
        if self._endpoint is None:
            self.validate_card(capability)
        endpoint = self._endpoint
        if endpoint is None:
            raise A2AError("remote_target_unavailable")
        headers = self._headers()
        if remote_task_id:
            return self._poll(remote_task_id, headers, on_status)
        # POST errors are ambiguous: never retry this message automatically.
        try:
            body = self._json("POST", endpoint + "/message:send",
                headers={**headers, "Content-Type": "application/a2a+json"}, json_body={
                    "message": {"messageId": message_id, "role": "ROLE_USER", "parts": [{"text": objective}]},
                    **({"tenant": self._tenant} if self._tenant is not None else {}),
                    "configuration": {"acceptedOutputModes": ["text/plain"], "historyLength": 0, "returnImmediately": True},
                })
        except Exception as exc:
            raise A2AError("submission_unknown", ambiguous=True) from exc
        if not isinstance(body, dict):
            raise A2AError("invalid_remote_response", ambiguous=True)
        if isinstance(body.get("message"), dict):
            message = body["message"]
            if message.get("role") != "ROLE_AGENT" or not isinstance(message.get("parts"), list):
                raise A2AError("invalid_remote_response", ambiguous=True)
            on_status("completed", None)
            return _extract_text(message)
        task = body.get("task")
        if not isinstance(task, dict) or not isinstance(task.get("id"), str) or not task["id"] or len(task["id"]) > 256:
            raise A2AError("invalid_remote_response", ambiguous=True)
        on_remote_task(task["id"])
        return self._consume_task(task, headers, on_status)

    def _consume_task(self, task: dict, headers: dict, on_status) -> str:
        state = _status(task)
        on_status(state, task.get("id"))
        if state in {"completed", "failed", "canceled", "rejected", "input-required", "auth-required"}:
            if state != "completed":
                raise A2AError("remote_task_" + state)
            return "\n".join(filter(None, [_extract_text(x) for x in task.get("artifacts", []) if isinstance(x, dict)])) or _extract_text(task.get("status", {}).get("message", {}))
        return self._poll(task["id"], headers, on_status)

    def _poll(self, task_id: str, headers: dict, on_status) -> str:
        endpoint = self._endpoint
        if endpoint is None:
            raise A2AError("remote_target_unavailable")
        max_wait = max(1, min(float(self.target.get("max_wait_seconds", 60)), 300))
        interval = max(0.05, min(float(self.target.get("poll_interval_seconds", 1)), 10))
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            try:
                task = self._json(
                    "GET", endpoint + "/tasks/" + quote(task_id, safe=""),
                    headers=headers,
                    params={"tenant": self._tenant} if self._tenant is not None else None,
                    deadline=deadline,
                )
            except A2AError as exc:
                if exc.code == "operation_deadline_exceeded" and time.monotonic() >= deadline:
                    on_status("timeout", task_id)
                    raise A2AError("task_timeout") from exc
                raise A2AError("task_poll_failed") from exc
            if time.monotonic() >= deadline:
                on_status("timeout", task_id)
                raise A2AError("task_timeout")
            if not isinstance(task, dict) or task.get("id") != task_id:
                raise A2AError("invalid_remote_response")
            state = _status(task)
            on_status(state, task_id)
            if state in {"completed", "failed", "canceled", "rejected", "input-required", "auth-required"}:
                if state != "completed":
                    raise A2AError("remote_task_" + state)
                artifacts = task.get("artifacts", [])
                artifact_text = "\n".join(_extract_text(x) for x in artifacts if isinstance(x, dict))[:100_000]
                return artifact_text or _extract_text(task.get("status", {}).get("message", {}))
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
        on_status("timeout", task_id)
        raise A2AError("task_timeout")
