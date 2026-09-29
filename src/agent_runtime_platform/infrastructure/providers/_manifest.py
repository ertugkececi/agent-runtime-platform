"""Provider capability manifests.

A provider declares what it supports, instead of the platform inferring it from
the provider's name. Capabilities live in a ``manifest.toml`` next to the
provider module, so they can be read without importing the provider's SDK.

Only facts that a caller actually reads are declared here. A new field is added
when something consumes it, not in advance.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from agent_runtime_platform.infrastructure.providers._base import ProviderError

MANIFEST_ROOT = Path(__file__).resolve().parent

_RUNTIMES = frozenset({"python", "node"})


@dataclass(frozen=True)
class ProviderManifest:
    """The capabilities a provider declares."""

    provider_id: str
    runtime: str
    sdk: str
    supports_tool_ids: bool


def manifest_path(provider_id: str) -> Path:
    """Return the expected manifest path for a provider id."""
    return MANIFEST_ROOT / provider_id / "manifest.toml"


def manifest_exists(provider_id: str) -> bool:
    """Return whether a provider declares a manifest."""
    return manifest_path(provider_id).is_file()


@lru_cache(maxsize=None)
def load_manifest(provider_id: str) -> ProviderManifest:
    """Read and validate one provider manifest.

    Raises ``ProviderError`` for an undeclared provider or an incomplete or
    inconsistent manifest.
    """
    path = manifest_path(provider_id)
    if not path.is_file():
        raise ProviderError(f"Provider '{provider_id}' does not declare a manifest.")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ProviderError(f"The manifest for provider '{provider_id}' is not valid TOML.") from exc
    return _build(provider_id, data)


def supports_tool_ids(provider_id: str) -> bool:
    """Return whether a provider accepts administrator-approved tool grants.

    Fails closed: a provider with no manifest supports no tools.
    """
    return manifest_exists(provider_id) and load_manifest(provider_id).supports_tool_ids


def _build(provider_id: str, data: dict[str, object]) -> ProviderManifest:
    manifest = ProviderManifest(
        provider_id=_require(provider_id, data, "id", str),
        runtime=_require(provider_id, data, "runtime", str),
        sdk=_require(provider_id, data, "sdk", str),
        supports_tool_ids=_require(provider_id, data, "supports_tool_ids", bool),
    )
    if manifest.provider_id != provider_id:
        raise ProviderError(
            f"The manifest for provider '{provider_id}' declares id '{manifest.provider_id}'."
        )
    if manifest.runtime not in _RUNTIMES:
        raise ProviderError(
            f"The manifest for provider '{provider_id}' declares runtime '{manifest.runtime}'."
        )
    return manifest


def _require(provider_id: str, data: dict[str, object], field: str, kind: type) -> object:
    if field not in data:
        raise ProviderError(f"The manifest for provider '{provider_id}' is missing '{field}'.")
    value = data[field]
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ProviderError(
            f"The manifest for provider '{provider_id}' declares an invalid '{field}'."
        )
    return value
