from __future__ import annotations

from typing import Any

from agent_runtime_platform.providers._base import (
    HandoffRequest,
    ModelOutput,
    ModelProvider,
    ProviderError,
)
from agent_runtime_platform.providers.codex.provider import CodexChatProvider
from agent_runtime_platform.providers.openai.provider import OpenAIChatProvider


class ProviderRegistry:
    def __init__(self, providers: dict[str, ModelProvider] | None = None) -> None:
        self._providers = providers if providers is not None else {
            "codex": CodexChatProvider(),
            "openai": OpenAIChatProvider(),
        }

    def supports(self, provider_name: str) -> bool:
        return provider_name in self._providers

    def generate(
        self,
        agent: dict[str, Any],
        history: list[dict[str, str]],
        *,
        allow_handoff: bool = False,
    ) -> ModelOutput:
        provider_name = agent["model_provider"]
        provider = self._providers.get(provider_name)
        if provider is None:
            raise ProviderError(f"Provider '{provider_name}' is not configured.")
        output = (
            provider.generate(agent, history, allow_handoff=True)
            if allow_handoff
            else provider.generate(agent, history)
        )
        if isinstance(output, HandoffRequest):
            if not allow_handoff:
                raise ProviderError("The model requested a handoff when handoff was disabled.")
            if not isinstance(output.capability, str) or not isinstance(output.task, str):
                raise ProviderError("The model returned an invalid handoff request.")
            capability = output.capability.strip().casefold()
            task = output.task.strip()
            if not capability or len(capability) > 80 or not task or len(task) > 20_000:
                raise ProviderError("The model returned an invalid handoff request.")
            return HandoffRequest(capability=capability, task=task)
        if not isinstance(output, str):
            raise ProviderError("The model returned an unsupported response.")
        return output
