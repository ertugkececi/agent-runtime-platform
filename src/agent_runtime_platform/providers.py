from __future__ import annotations

import os
from typing import Any, Protocol


class ProviderError(Exception):
    """A safe, user-facing error from a configured model provider."""


class ModelProvider(Protocol):
    def generate(self, agent: dict[str, Any], history: list[dict[str, str]]) -> str:
        """Generate a response using the agent snapshot and its conversation view."""


class OpenAIChatProvider:
    def generate(self, agent: dict[str, Any], history: list[dict[str, str]]) -> str:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ProviderError("OPENAI_API_KEY is not configured.")

        try:
            from langchain_openai import ChatOpenAI

            model = ChatOpenAI(model=agent["model_name"], api_key=api_key)
            messages = [("system", agent["instructions"])] + [
                (item["role"], item["content"]) for item in history
            ]
            response = model.invoke(messages)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("The OpenAI model request failed.") from exc

        content = response.content
        if isinstance(content, list):
            content = "".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
            )
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("The OpenAI model returned an empty response.")
        return content.strip()


class ProviderRegistry:
    def __init__(self, providers: dict[str, ModelProvider] | None = None) -> None:
        self._providers = providers or {"openai": OpenAIChatProvider()}

    def supports(self, provider_name: str) -> bool:
        return provider_name in self._providers

    def generate(self, agent: dict[str, Any], history: list[dict[str, str]]) -> str:
        provider_name = agent["model_provider"]
        provider = self._providers.get(provider_name)
        if provider is None:
            raise ProviderError(f"Provider '{provider_name}' is not configured.")
        return provider.generate(agent, history)
