"""The OpenCode model catalog and its provider registration.

The catalog itself is the adapter's job (``tests/test_opencode_provider.py``
drives it with a fake host). Here the HTTP surface and the registry gate are
pinned: the flag turns the provider on and off, and an unsupported
``model_provider`` is refused with an explicit ``422``.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agent_runtime_platform.api.app import create_app
from agent_runtime_platform.infrastructure.providers import ProviderError, ProviderRegistry

FLAG = "AGENT_RUNTIME_FEATURE_PROVIDER_OPENCODE"

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


def test_the_flag_registers_the_provider_and_its_catalog(monkeypatch):
    monkeypatch.setenv(FLAG, "on")
    registry = ProviderRegistry()
    assert registry.supports("opencode") is True
    assert "opencode" in registry.configured_provider_ids()


def test_without_the_flag_the_provider_and_catalog_are_absent(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    registry = ProviderRegistry()
    assert registry.supports("opencode") is False

    app = create_app(database_url="sqlite:///:memory:")
    with TestClient(app) as client:
        assert client.get("/opencode/models").status_code == 404
        refused = client.post("/agents", json=_agent())
        assert refused.status_code == 422
        assert refused.json()["detail"] == "The requested model provider is not configured."
        assert client.get("/agent-config/catalog").json() == {
            "providers": [
                {"id": "codex", "supports_tool_ids": True, "model_catalog_url": "/codex/models"},
                {"id": "openai", "supports_tool_ids": False, "model_catalog_url": None},
            ]
        }
    app.state.database.dispose()


def test_the_catalog_serves_the_shared_model_shape(monkeypatch):
    monkeypatch.setenv(FLAG, "on")
    monkeypatch.setattr("agent_runtime_platform.api.app.list_opencode_models", lambda: CATALOG)
    app = create_app(database_url="sqlite:///:memory:")
    with TestClient(app) as client:
        response = client.get("/opencode/models")
        assert response.status_code == 200
        assert response.json() == CATALOG
        catalog = client.get("/agent-config/catalog").json()
        assert {"id": "opencode", "supports_tool_ids": False, "model_catalog_url": "/opencode/models"} in catalog["providers"]
        created = client.post("/agents", json=_agent(model_reasoning_effort="high"))
        assert created.status_code == 201, created.text
        assert created.json()["model_provider"] == "opencode"
    app.state.database.dispose()


def test_an_unsupported_model_provider_is_refused_on_create_and_update(monkeypatch):
    monkeypatch.setenv(FLAG, "on")
    app = create_app(database_url="sqlite:///:memory:")
    with TestClient(app) as client:
        refused = client.post("/agents", json=_agent(model_provider="made_up"))
        assert refused.status_code == 422
        assert refused.json()["detail"] == "The requested model provider is not configured."

        created = client.post("/agents", json=_agent(model_provider="openai"))
        assert created.status_code == 201, created.text
        changed = client.patch(
            f"/agents/{created.json()['id']}", json={"model_provider": "made_up"}
        )
        assert changed.status_code == 422
        assert changed.json()["detail"] == "The requested model provider is not configured."
    app.state.database.dispose()


def test_an_unavailable_catalog_is_reported_as_unavailable(monkeypatch):
    monkeypatch.setenv(FLAG, "on")

    def unavailable() -> list[dict]:
        raise ProviderError("The OpenCode bridge host could not be started.")

    monkeypatch.setattr("agent_runtime_platform.api.app.list_opencode_models", unavailable)
    app = create_app(database_url="sqlite:///:memory:")
    with TestClient(app) as client:
        response = client.get("/opencode/models")
        assert response.status_code == 503
        assert response.json()["detail"] == "OpenCode model list is unavailable."
    app.state.database.dispose()
