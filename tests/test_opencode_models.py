"""The OpenCode model catalog and its provider registration.

The catalog itself is the adapter's job (``tests/test_opencode_provider.py``
drives it with a fake host). Here the HTTP surface and the registry are
pinned: OpenCode is the one registered provider, its catalog is served at
``/opencode/models``, and an unsupported ``model_provider`` is refused with an
explicit ``422``.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agent_runtime_platform.api.app import create_app
from agent_runtime_platform.infrastructure.providers import ProviderError, ProviderRegistry

CATALOG = [
    {
        "id": "opencode/big-model",
        "label": "Big Model",
        "is_default": True,
        "default_effort": "",
        "efforts": ["low", "high"],
    },
    {
        "id": "opencode/plain-model",
        "label": "Plain Model",
        "is_default": False,
        "default_effort": "",
        "efforts": [],
    },
]


def _agent(**overrides) -> dict:
    agent = {
        "name": "OpenCode",
        "instructions": "Answer.",
        "model_provider": "opencode",
        "model_name": "opencode/big-model",
    }
    agent.update(overrides)
    return agent


def test_the_registry_registers_the_opencode_provider_and_catalog():
    registry = ProviderRegistry()
    assert registry.supports("opencode") is True
    assert "opencode" in registry.configured_provider_ids()


def test_the_agent_config_catalog_declares_opencode_capabilities():
    app = create_app(database_url="sqlite:///:memory:")
    with TestClient(app) as client:
        assert client.get("/agent-config/catalog").json() == {
            "providers": [
                {
                    "id": "opencode",
                    "supports_tool_ids": True,
                    "model_catalog_url": "/opencode/models",
                }
            ]
        }
    app.state.database.dispose()


def test_the_catalog_serves_the_shared_model_shape(monkeypatch):
    monkeypatch.setattr("agent_runtime_platform.api.app.list_opencode_models", lambda: CATALOG)
    app = create_app(database_url="sqlite:///:memory:")
    with TestClient(app) as client:
        response = client.get("/opencode/models")
        assert response.status_code == 200
        assert response.json() == CATALOG
        catalog = client.get("/agent-config/catalog").json()
        assert catalog["providers"][0]["supports_tool_ids"] is True
        created = client.post("/agents", json=_agent(model_reasoning_effort="high"))
        assert created.status_code == 201, created.text
        assert created.json()["model_provider"] == "opencode"
    app.state.database.dispose()


def test_an_unsupported_model_provider_is_refused_on_create_and_update():
    app = create_app(database_url="sqlite:///:memory:")
    with TestClient(app) as client:
        refused = client.post("/agents", json=_agent(model_provider="made_up"))
        assert refused.status_code == 422
        assert refused.json()["detail"] == "The requested model provider is not configured."

        created = client.post("/agents", json=_agent())
        assert created.status_code == 201, created.text
        changed = client.patch(
            f"/agents/{created.json()['id']}", json={"model_provider": "made_up"}
        )
        assert changed.status_code == 422
        assert changed.json()["detail"] == "The requested model provider is not configured."
    app.state.database.dispose()


def test_an_unavailable_catalog_is_reported_as_unavailable(monkeypatch):
    def unavailable() -> list[dict]:
        raise ProviderError("The OpenCode bridge host could not be started.")

    monkeypatch.setattr("agent_runtime_platform.api.app.list_opencode_models", unavailable)
    app = create_app(database_url="sqlite:///:memory:")
    with TestClient(app) as client:
        response = client.get("/opencode/models")
        assert response.status_code == 503
        assert response.json()["detail"] == "OpenCode model list is unavailable."
    app.state.database.dispose()


class _StubConnections:
    """A connection service with no subprocess; the API surface is what is pinned."""

    def __init__(self) -> None:
        self.attempts: dict[str, dict] = {}

    def list_integrations(self) -> list[dict]:
        return [
            {
                "id": "openai",
                "name": "OpenAI",
                "connected": False,
                "methods": [
                    {"id": "chatgpt-headless", "type": "oauth", "label": "ChatGPT Pro/Plus (headless)"}
                ],
            }
        ]

    def start(self, integration: str, method: str | None = None, label: str | None = None) -> dict:
        if integration == "missing":
            raise ProviderError("The integration 'missing' is not available.")
        attempt = {
            "attempt_id": "attempt-1",
            "integration": integration,
            "method": method,
            "status": "waiting",
            "url": "https://auth.example.test/codex/device",
            "instructions": "Enter code: ABCD-EFGH",
            "mode": "code",
            "message": None,
        }
        self.attempts[attempt["attempt_id"]] = attempt
        return attempt

    def status(self, attempt_id: str) -> dict:
        if attempt_id not in self.attempts:
            raise KeyError(attempt_id)
        return self.attempts[attempt_id]

    def submit_code(self, attempt_id: str, code: str) -> dict:
        if attempt_id not in self.attempts:
            raise KeyError(attempt_id)
        if code == "bad":
            raise ValueError("The connection does not accept a code.")
        self.attempts[attempt_id]["status"] = "complete"
        return self.attempts[attempt_id]

    def cancel(self, attempt_id: str) -> dict:
        if attempt_id not in self.attempts:
            raise KeyError(attempt_id)
        self.attempts[attempt_id]["status"] = "failed"
        self.attempts[attempt_id]["message"] = "The provider connection was cancelled."
        return self.attempts[attempt_id]


def test_the_connection_api_surfaces_integrations_and_attempts():
    service = _StubConnections()
    app = create_app(database_url="sqlite:///:memory:", connections=service)
    with TestClient(app) as client:
        integrations = client.get("/opencode/integrations")
        assert integrations.status_code == 200
        assert integrations.json()[0]["methods"][0]["id"] == "chatgpt-headless"

        started = client.post(
            "/opencode/connections", json={"integration": "openai", "method": "chatgpt-headless"}
        )
        assert started.status_code == 202, started.text
        attempt = started.json()
        assert attempt["status"] == "waiting"
        assert attempt["mode"] == "code"
        assert attempt["attempt_id"] == "attempt-1"

        assert client.get("/opencode/connections/attempt-1").json()["status"] == "waiting"
        assert client.get("/opencode/connections/missing").status_code == 404

        refused = client.post("/opencode/connections/attempt-1/code", json={"code": "bad"})
        assert refused.status_code == 422
        assert refused.json()["detail"] == "The connection does not accept a code."

        completed = client.post("/opencode/connections/attempt-1/code", json={"code": "ABCD"})
        assert completed.status_code == 202
        assert completed.json()["status"] == "complete"

        cancelled = client.delete("/opencode/connections/attempt-1")
        assert cancelled.status_code == 204
        assert client.delete("/opencode/connections/attempt-1").status_code == 204

        unavailable = client.post("/opencode/connections", json={"integration": "missing"})
        assert unavailable.status_code == 503
        assert unavailable.json()["detail"] == "The integration 'missing' is not available."

        invalid = client.post("/opencode/connections", json={"integration": ""})
        assert invalid.status_code == 422
    app.state.database.dispose()
