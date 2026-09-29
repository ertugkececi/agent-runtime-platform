"""Model providers.

Every provider is one directory with the same shape:

- ``provider.py`` implements the shared contract declared in ``_base``;
- ``manifest.toml`` declares the provider's capabilities.

The contract itself is deliberately small. Everything a provider may or may not
support is declared in its manifest rather than implied by the shared interface.
See ``docs/architecture/repo-and-package-boundaries.md``.
"""

from agent_runtime_platform.infrastructure.providers._base import (
    HandoffRequest,
    ModelOutput,
    ModelProvider,
    ProviderError,
)
from agent_runtime_platform.infrastructure.providers._manifest import (
    ProviderManifest,
    load_manifest,
    manifest_exists,
    supports_tool_ids,
)
from agent_runtime_platform.infrastructure.providers._registry import ProviderRegistry
from agent_runtime_platform.infrastructure.providers.opencode.connections import OpenCodeConnections
from agent_runtime_platform.infrastructure.providers.opencode.provider import (
    OpenCodeChatProvider,
    list_opencode_integrations,
    list_opencode_models,
)

__all__ = [
    "HandoffRequest",
    "ModelOutput",
    "ModelProvider",
    "OpenCodeChatProvider",
    "OpenCodeConnections",
    "ProviderError",
    "ProviderManifest",
    "ProviderRegistry",
    "list_opencode_integrations",
    "list_opencode_models",
    "load_manifest",
    "manifest_exists",
    "supports_tool_ids",
]
