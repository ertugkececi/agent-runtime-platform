"""Golden tests for the provider module boundary.

These guard the contract that the split into ``providers/`` must not change:
the public import surface, the registry defaults, and the registry's handoff
validation.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from agent_runtime_platform.infrastructure.providers import (
    HandoffRequest,
    ModelOutput,
    ModelProvider,
    OpenCodeChatProvider,
    OpenCodeConnections,
    ProviderError,
    ProviderRegistry,
    list_opencode_integrations,
    list_opencode_models,
)

SRC = Path(__file__).resolve().parents[1] / "src"
PROJECT = Path(__file__).resolve().parents[1]


class _FakeProvider:
    def __init__(self, output: object) -> None:
        self.output = output

    def generate(self, agent, history, *, allow_handoff=False):
        return self.output


def _agent(name: str = "fixture") -> dict:
    return {"model_provider": name}


def test_public_import_surface_is_preserved():
    assert callable(OpenCodeChatProvider)
    assert callable(OpenCodeConnections)
    assert callable(ProviderRegistry)
    assert callable(list_opencode_models)
    assert callable(list_opencode_integrations)
    assert issubclass(ProviderError, Exception)
    assert HandoffRequest("a", "b").capability == "a"
    assert ModelOutput == str | HandoffRequest
    assert ModelProvider is not None


def test_default_registry_registers_exactly_opencode():
    registry = ProviderRegistry()
    assert sorted(registry._providers) == ["opencode"]
    assert registry.supports("opencode") is True
    assert registry.supports("codex") is False
    assert registry.supports("openai") is False


def test_injected_registry_replaces_the_defaults():
    registry = ProviderRegistry({"fixture": _FakeProvider("hello")})
    assert sorted(registry._providers) == ["fixture"]
    assert registry.supports("opencode") is False


def test_the_removed_provider_sdks_are_no_longer_dependencies():
    project = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = " ".join(project["project"]["dependencies"])
    assert "openai-codex" not in dependencies
    assert "langchain-openai" not in dependencies


def test_registry_returns_plain_text():
    registry = ProviderRegistry({"fixture": _FakeProvider("  hello  ")})
    assert registry.generate(_agent(), []) == "  hello  "


def test_registry_rejects_an_unconfigured_provider():
    registry = ProviderRegistry({"fixture": _FakeProvider("hello")})
    with pytest.raises(ProviderError, match="is not configured"):
        registry.generate(_agent("opencode"), [])


def test_registry_rejects_a_non_string_response():
    registry = ProviderRegistry({"fixture": _FakeProvider(42)})
    with pytest.raises(ProviderError, match="unsupported response"):
        registry.generate(_agent(), [])


def test_registry_rejects_handoff_when_handoff_is_disabled():
    registry = ProviderRegistry({"fixture": _FakeProvider(HandoffRequest("research", "task"))})
    with pytest.raises(ProviderError, match="handoff when handoff was disabled"):
        registry.generate(_agent(), [])


def test_registry_normalises_the_handoff_capability():
    registry = ProviderRegistry({"fixture": _FakeProvider(HandoffRequest("  Research ", " task "))})
    result = registry.generate(_agent(), [], allow_handoff=True)
    assert result == HandoffRequest("research", "task")


@pytest.mark.parametrize(
    "handoff",
    [
        HandoffRequest("", "task"),
        HandoffRequest("x" * 81, "task"),
        HandoffRequest("research", ""),
        HandoffRequest("research", "y" * 20_001),
    ],
)
def test_registry_rejects_an_invalid_handoff(handoff):
    registry = ProviderRegistry({"fixture": _FakeProvider(handoff)})
    with pytest.raises(ProviderError, match="invalid handoff"):
        registry.generate(_agent(), [], allow_handoff=True)


def test_providers_live_in_one_directory_each():
    package = SRC / "agent_runtime_platform" / "infrastructure" / "providers"
    assert (package / "_base.py").is_file()
    assert (package / "_registry.py").is_file()
    assert (package / "opencode" / "provider.py").is_file()
    assert not (package / "codex" / "provider.py").exists()
    assert not (package / "openai" / "provider.py").exists()
    assert not (SRC / "agent_runtime_platform" / "infrastructure" / "providers.py").exists()
