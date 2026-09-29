"""Feature flags.

Environment-driven behaviour falls into three categories that are deliberately
kept apart:

1. **Configuration** - values the deployment must supply, such as
   ``AGENT_RUNTIME_DATABASE_URL`` or the ``AGENT_RUNTIME_OIDC_*`` pair of issuer
   and client settings. Configuration is not a flag; it is a required input.
2. **Deployment modes** - explicit postures such as ``AGENT_RUNTIME_AUTH_MODE``
   and ``AGENT_RUNTIME_RESOURCE_AUTH_MODE``. A mode selects one documented
   behaviour, and its accepted values are validated where the mode is read.
3. **Feature flags** - new behaviour that ships behind a gate and defaults to
   off. Flags are declared here and read through :func:`is_enabled`.

Two rules apply to this module:

- **Flags default to off.** A flag that is absent or blank means "use the
  declared default", and every declared default is ``False``.
- **A flag is not a policy.** Security behaviour - tool grants, permission
  checks, authorization decisions - is policy and must stay unconditional. A
  flag may introduce new behaviour, but it may not weaken or disable a policy
  that already holds.

Declare a flag here, document it in ``.env.example``, and read it through
:func:`is_enabled` rather than calling ``os.getenv`` directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


PREFIX = "AGENT_RUNTIME_FEATURE_"

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
_ACCEPTED = "one of " + ", ".join(sorted(_TRUE | _FALSE))


@dataclass(frozen=True)
class FeatureFlag:
    """One declared feature flag."""

    name: str
    default: bool
    description: str
    owner: str
    requires: tuple[str, ...] = ()

    @property
    def env_var(self) -> str:
        """The environment variable that overrides this flag."""
        return PREFIX + self.name.upper()


FLAGS: dict[str, FeatureFlag] = {}


def _flag(name: str) -> FeatureFlag:
    flag = FLAGS.get(name)
    if flag is None:
        raise RuntimeError(f"Unknown feature flag '{name}'.")
    return flag


def _parse(flag: FeatureFlag, value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in _TRUE:
        return True
    if normalized in _FALSE:
        return False
    raise RuntimeError(f"{flag.env_var} must be {_ACCEPTED}.")


def is_enabled(name: str) -> bool:
    """Return whether a declared feature flag is enabled.

    An unset or blank variable yields the declared default. A flag whose
    ``requires`` dependency is not enabled raises rather than silently
    activating an incomplete feature.
    """
    flag = _flag(name)
    raw = os.getenv(flag.env_var)
    enabled = flag.default if raw is None or not raw.strip() else _parse(flag, raw)
    if enabled:
        for dependency in flag.requires:
            if not is_enabled(dependency):
                raise RuntimeError(
                    f"{flag.env_var} requires {_flag(dependency).env_var}."
                )
    return enabled


def enabled_flags() -> list[str]:
    """Return the names of the flags that are currently enabled."""
    return sorted(name for name in FLAGS if is_enabled(name))


def describe() -> list[dict[str, object]]:
    """Return the declared flags as plain data, for documentation and tests."""
    return [
        {
            "name": flag.name,
            "env_var": flag.env_var,
            "default": flag.default,
            "description": flag.description,
            "owner": flag.owner,
            "requires": list(flag.requires),
        }
        for flag in sorted(FLAGS.values(), key=lambda flag: flag.name)
    ]


def validate() -> None:
    """Raise if the registry is internally inconsistent.

    Enforced by the test suite so a declaration mistake fails a check rather
    than the running application.
    """
    for flag in FLAGS.values():
        if not flag.description.strip():
            raise RuntimeError(f"{flag.env_var} has no description.")
        if not flag.owner.strip():
            raise RuntimeError(f"{flag.env_var} has no owner.")
        for dependency in flag.requires:
            if dependency == flag.name:
                raise RuntimeError(f"{flag.env_var} requires itself.")
            if dependency not in FLAGS:
                raise RuntimeError(
                    f"{flag.env_var} requires undeclared flag '{dependency}'."
                )
