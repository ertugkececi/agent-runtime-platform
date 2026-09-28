"""Golden tests for the provider module boundary.

These guard the contract that the split into ``providers/`` must not change:
the public import surface, the registry defaults, and the registry's handoff
validation.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_runtime_platform.providers import (
    CodexChatProvider,
    HandoffRequest,
    ModelOutput,
    ModelProvider,
    OpenAIChatProvider,
    ProviderError,
    ProviderRegistry,
    list_codex_models,
)

SRC = Path(__file__).resolve().parents[1] / "src"


class _FakeProvider:
    def __init__(self, output: object) -> None:
        self.output = output

    def generate(self, agent, history, *, allow_handoff=False):
        return self.output


def _agent(name: str = "fake") -> dict:
    return {"model_provider": name}


def test_public_import_surface_is_preserved():
    assert callable(CodexChatProvider)
    assert callable(OpenAIChatProvider)
    assert callable(ProviderRegistry)
    assert callable(list_codex_models)
    assert issubclass(ProviderError, Exception)
    assert HandoffRequest("a", "b").capability == "a"
    assert ModelOutput == str | HandoffRequest
    assert ModelProvider is not None


def test_default_registry_registers_exactly_codex_and_openai():
    registry = ProviderRegistry()
    assert sorted(registry._providers) == ["codex", "openai"]
    assert registry.supports("codex") is True
    assert registry.supports("openai") is True
    assert registry.supports("opencode") is False


def test_the_opencode_flag_adds_the_provider(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_FEATURE_PROVIDER_OPENCODE", "on")
    registry = ProviderRegistry()
    assert sorted(registry._providers) == ["codex", "openai", "opencode"]
    assert registry.supports("opencode") is True


def test_injected_registry_replaces_the_defaults():
    registry = ProviderRegistry({"openai": _FakeProvider("hello")})
    assert sorted(registry._providers) == ["openai"]
    assert registry.supports("codex") is False


def test_provider_sdks_are_still_imported_lazily():
    code = (
        "import sys;"
        "import agent_runtime_platform.providers as p;"
        "p.ProviderRegistry();"
        "print(int('openai_codex' in sys.modules), int('langchain_openai' in sys.modules))"
    )
    env = dict(os.environ, PYTHONPATH=str(SRC))
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env
    )
    assert result.stdout.strip() == "0 0"


def test_registry_returns_plain_text():
    registry = ProviderRegistry({"openai": _FakeProvider("  hello  ")})
    assert registry.generate(_agent("openai"), []) == "  hello  "


def test_registry_rejects_an_unconfigured_provider():
    registry = ProviderRegistry({"openai": _FakeProvider("hello")})
    with pytest.raises(ProviderError, match="is not configured"):
        registry.generate(_agent("codex"), [])


def test_registry_rejects_a_non_string_response():
    registry = ProviderRegistry({"openai": _FakeProvider(42)})
    with pytest.raises(ProviderError, match="unsupported response"):
        registry.generate(_agent("openai"), [])


def test_registry_rejects_handoff_when_handoff_is_disabled():
    registry = ProviderRegistry({"openai": _FakeProvider(HandoffRequest("research", "task"))})
    with pytest.raises(ProviderError, match="handoff when handoff was disabled"):
        registry.generate(_agent("openai"), [])


def test_registry_normalises_the_handoff_capability():
    registry = ProviderRegistry({"openai": _FakeProvider(HandoffRequest("  Research ", " task "))})
    result = registry.generate(_agent("openai"), [], allow_handoff=True)
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
    registry = ProviderRegistry({"openai": _FakeProvider(handoff)})
    with pytest.raises(ProviderError, match="invalid handoff"):
        registry.generate(_agent("openai"), [], allow_handoff=True)


def test_providers_live_in_one_directory_each():
    package = SRC / "agent_runtime_platform" / "providers"
    assert (package / "_base.py").is_file()
    assert (package / "_registry.py").is_file()
    assert (package / "codex" / "provider.py").is_file()
    assert (package / "openai" / "provider.py").is_file()
    assert not (SRC / "agent_runtime_platform" / "providers.py").exists()
