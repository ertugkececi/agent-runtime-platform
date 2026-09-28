"""Model providers.

Every provider is one directory with the same shape:

- ``provider.py`` implements the shared contract declared in ``_base``;
- ``manifest.toml`` declares the provider's capabilities.

The contract itself is deliberately small. Everything a provider may or may not
support is declared in its manifest rather than implied by the shared interface.
See ``docs/architecture/repo-and-package-boundaries.md``.
"""

from agent_runtime_platform.providers._base import (
    HandoffRequest,
    ModelOutput,
    ModelProvider,
    ProviderError,
)
from agent_runtime_platform.providers._registry import ProviderRegistry
from agent_runtime_platform.providers.codex.provider import CodexChatProvider, list_codex_models
from agent_runtime_platform.providers.openai.provider import OpenAIChatProvider

__all__ = [
    "CodexChatProvider",
    "HandoffRequest",
    "ModelOutput",
    "ModelProvider",
    "OpenAIChatProvider",
    "ProviderError",
    "ProviderRegistry",
    "list_codex_models",
]
