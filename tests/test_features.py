from __future__ import annotations

from pathlib import Path

import pytest

from agent_runtime_platform import features

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"

PROBE = features.FeatureFlag(
    name="probe",
    default=False,
    description="A test flag.",
    owner="tests",
)


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.setitem(features.FLAGS, "probe", PROBE)
    monkeypatch.delenv("AGENT_RUNTIME_FEATURE_PROBE", raising=False)
    return PROBE


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


def test_unset_flag_uses_declared_default(probe):
    assert features.is_enabled("probe") is False


def test_blank_flag_uses_declared_default(probe, monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_FEATURE_PROBE", "   ")
    assert features.is_enabled("probe") is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "On"])
def test_truthy_values_enable_flag(probe, monkeypatch, value):
    monkeypatch.setenv("AGENT_RUNTIME_FEATURE_PROBE", value)
    assert features.is_enabled("probe") is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", " no ", "Off"])
def test_falsy_values_disable_flag(probe, monkeypatch, value):
    monkeypatch.setenv("AGENT_RUNTIME_FEATURE_PROBE", value)
    assert features.is_enabled("probe") is False


def test_invalid_value_is_rejected(probe, monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_FEATURE_PROBE", "maybe")
    with pytest.raises(RuntimeError, match="AGENT_RUNTIME_FEATURE_PROBE"):
        features.is_enabled("probe")


def test_unknown_flag_is_rejected():
    with pytest.raises(RuntimeError, match="Unknown feature flag"):
        features.is_enabled("does_not_exist")


def test_enabled_flags_reflects_environment(probe, monkeypatch):
    assert features.enabled_flags() == []
    monkeypatch.setenv("AGENT_RUNTIME_FEATURE_PROBE", "on")
    assert features.enabled_flags() == ["probe"]


def test_dependency_is_checked_only_when_flag_is_enabled(monkeypatch):
    dependency = features.FeatureFlag(
        name="base",
        default=False,
        description="test flag",
        owner="tests",
    )
    dependent = features.FeatureFlag(
        name="dependent",
        default=False,
        description="test flag",
        owner="tests",
        requires=("base",),
    )
    monkeypatch.setitem(features.FLAGS, "base", dependency)
    monkeypatch.setitem(features.FLAGS, "dependent", dependent)
    monkeypatch.delenv("AGENT_RUNTIME_FEATURE_BASE", raising=False)

    assert features.is_enabled("dependent") is False

    monkeypatch.setenv("AGENT_RUNTIME_FEATURE_DEPENDENT", "on")
    with pytest.raises(RuntimeError, match="AGENT_RUNTIME_FEATURE_BASE"):
        features.is_enabled("dependent")

    monkeypatch.setenv("AGENT_RUNTIME_FEATURE_BASE", "on")
    assert features.is_enabled("dependent") is True


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
