from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class ProviderError(Exception):
    """A safe, user-facing error from a configured model provider."""


@dataclass(frozen=True)
class HandoffRequest:
    """An explicit request to delegate one bounded task to another agent."""

    capability: str
    task: str


ModelOutput = str | HandoffRequest


class ModelProvider(Protocol):
    def generate(
        self,
        agent: dict[str, Any],
        history: list[dict[str, str]],
        *,
        allow_handoff: bool = False,
    ) -> ModelOutput:
        """Generate a response using the agent snapshot and its conversation view."""
