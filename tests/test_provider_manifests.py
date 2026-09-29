"""Provider manifests declare capabilities instead of the platform inferring them."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_runtime_platform.infrastructure import providers
from agent_runtime_platform.infrastructure.mcp_tools import validate_tool_ids
from agent_runtime_platform.infrastructure.providers import (
    ProviderError,
    ProviderRegistry,
    load_manifest,
    manifest_exists,
    supports_tool_ids,
)

SRC = Path(__file__).resolve().parents[1] / "src"


def test_every_registered_provider_declares_a_manifest():
    for provider_id in ProviderRegistry()._providers:
        assert manifest_exists(provider_id), provider_id
        assert load_manifest(provider_id).provider_id == provider_id


@pytest.mark.parametrize(
    ("provider_id", "tool_ids_supported"),
    [("codex", True), ("openai", False)],
)
def test_declared_tool_id_support(provider_id, tool_ids_supported):
    manifest = load_manifest(provider_id)
    assert manifest.supports_tool_ids is tool_ids_supported
    assert supports_tool_ids(provider_id) is tool_ids_supported
    assert manifest.runtime == "python"
    assert manifest.sdk


def test_opencode_declares_the_typescript_sdk():
    """The host runs in its native stack; its SDK version is declared here."""
    manifest = load_manifest("opencode")
    assert manifest.runtime == "node"
    assert manifest.supports_tool_ids is False
    assert supports_tool_ids("opencode") is False
    package = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "src"
            / "agent_runtime_platform"
            / "infrastructure"
            / "providers"
            / "opencode"
            / "host"
            / "package.json"
        ).read_text(encoding="utf-8")
    )
    assert package["dependencies"]["@opencode/sdk"] in manifest.sdk


def test_undeclared_provider_has_no_manifest():
    assert manifest_exists("made_up") is False
    with pytest.raises(ProviderError, match="does not declare a manifest"):
        load_manifest("made_up")


def test_undeclared_provider_supports_no_tools():
    # Fail closed: an unknown provider is treated as supporting nothing.
    assert supports_tool_ids("made_up") is False


def test_manifests_are_read_without_importing_provider_sdks():
    code = (
        "import sys;"
        "from agent_runtime_platform.infrastructure.providers import load_manifest;"
        "m = load_manifest('codex');"
        "print(m.provider_id, int(m.supports_tool_ids),"
        " int('openai_codex' in sys.modules), int('langchain_openai' in sys.modules))"
    )
    env = dict(os.environ, PYTHONPATH=str(SRC))
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env
    )
    assert result.stdout.strip() == "codex 1 0 0"


def test_openai_agents_reject_tool_grants():
    with pytest.raises(ValueError, match="not supported by provider 'openai'"):
        validate_tool_ids(["fixture/lookup"], "openai")


def test_undeclared_providers_reject_tool_grants():
    with pytest.raises(ValueError, match="not supported by provider 'made_up'"):
        validate_tool_ids(["fixture/lookup"], "made_up")


def test_empty_tool_grants_are_accepted_for_any_provider():
    assert validate_tool_ids([], "openai") == []
    assert validate_tool_ids([], "made_up") == []


def test_codex_passes_the_provider_gate_and_fails_the_approval_check():
    # The error must come from the administrator-approval check, not the
    # provider gate, which proves Codex cleared the manifest check.
    with pytest.raises(ValueError, match="not administrator-approved"):
        validate_tool_ids(["unknown/tool"], "codex")


def test_manifest_is_required_to_match_its_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(providers._manifest, "MANIFEST_ROOT", tmp_path)
    load_manifest.cache_clear()
    try:
        directory = tmp_path / "sample"
        directory.mkdir()
        (directory / "manifest.toml").write_text(
            'id = "other"\nruntime = "python"\nsdk = "x"\nsupports_tool_ids = false\n',
            encoding="utf-8",
        )
        with pytest.raises(ProviderError, match="declares id 'other'"):
            load_manifest("sample")
    finally:
        load_manifest.cache_clear()
