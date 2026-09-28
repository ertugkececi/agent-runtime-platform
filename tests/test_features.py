from __future__ import annotations

from pathlib import Path

import pytest

from agent_runtime_platform import features

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def test_declared_flags_are_consistent():
    features.validate()


def test_flags_default_to_off():
    for flag in features.FLAGS.values():
        assert flag.default is False


def test_env_var_follows_prefix_and_name():
    for flag in features.FLAGS.values():
        assert flag.env_var == features.PREFIX + flag.name.upper()


def test_describe_covers_every_declared_flag():
    described = {entry["name"] for entry in features.describe()}
    assert described == set(features.FLAGS)


def test_every_declared_flag_is_documented_in_env_example():
    example = ENV_EXAMPLE.read_text(encoding="utf-8")
    for flag in features.FLAGS.values():
        assert flag.env_var in example


def test_unset_flag_uses_declared_default(monkeypatch):
    monkeypatch.delenv("AGENT_RUNTIME_FEATURE_PROVIDER_OPENCODE", raising=False)
    assert features.is_enabled("provider_opencode") is False


def test_blank_flag_uses_declared_default(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_FEATURE_PROVIDER_OPENCODE", "   ")
    assert features.is_enabled("provider_opencode") is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "On"])
def test_truthy_values_enable_flag(monkeypatch, value):
    monkeypatch.setenv("AGENT_RUNTIME_FEATURE_PROVIDER_OPENCODE", value)
    assert features.is_enabled("provider_opencode") is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", " no ", "Off"])
def test_falsy_values_disable_flag(monkeypatch, value):
    monkeypatch.setenv("AGENT_RUNTIME_FEATURE_PROVIDER_OPENCODE", value)
    assert features.is_enabled("provider_opencode") is False


def test_invalid_value_is_rejected(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_FEATURE_PROVIDER_OPENCODE", "maybe")
    with pytest.raises(RuntimeError, match="AGENT_RUNTIME_FEATURE_PROVIDER_OPENCODE"):
        features.is_enabled("provider_opencode")


def test_unknown_flag_is_rejected():
    with pytest.raises(RuntimeError, match="Unknown feature flag"):
        features.is_enabled("does_not_exist")


def test_enabled_flags_reflects_environment(monkeypatch):
    monkeypatch.delenv("AGENT_RUNTIME_FEATURE_PROVIDER_OPENCODE", raising=False)
    assert features.enabled_flags() == []
    monkeypatch.setenv("AGENT_RUNTIME_FEATURE_PROVIDER_OPENCODE", "on")
    assert features.enabled_flags() == ["provider_opencode"]


def test_dependency_is_checked_only_when_flag_is_enabled(monkeypatch):
    probe = features.FeatureFlag(
        name="probe",
        default=False,
        description="test flag",
        owner="tests",
        requires=("provider_opencode",),
    )
    monkeypatch.setitem(features.FLAGS, "probe", probe)
    monkeypatch.delenv("AGENT_RUNTIME_FEATURE_PROVIDER_OPENCODE", raising=False)

    assert features.is_enabled("probe") is False

    monkeypatch.setenv("AGENT_RUNTIME_FEATURE_PROBE", "on")
    with pytest.raises(RuntimeError, match="AGENT_RUNTIME_FEATURE_PROVIDER_OPENCODE"):
        features.is_enabled("probe")

    monkeypatch.setenv("AGENT_RUNTIME_FEATURE_PROVIDER_OPENCODE", "on")
    assert features.is_enabled("probe") is True


def test_validate_rejects_undeclared_dependency(monkeypatch):
    broken = features.FeatureFlag(
        name="broken",
        default=False,
        description="test flag",
        owner="tests",
        requires=("not_declared",),
    )
    monkeypatch.setitem(features.FLAGS, "broken", broken)
    with pytest.raises(RuntimeError, match="not_declared"):
        features.validate()


def test_validate_rejects_self_dependency(monkeypatch):
    broken = features.FeatureFlag(
        name="looping",
        default=False,
        description="test flag",
        owner="tests",
        requires=("looping",),
    )
    monkeypatch.setitem(features.FLAGS, "looping", broken)
    with pytest.raises(RuntimeError, match="requires itself"):
        features.validate()
