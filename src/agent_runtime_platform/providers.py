from __future__ import annotations

import json
import os
import tempfile
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


class OpenAIChatProvider:
    _HANDOFF_TOOL = {
        "name": "handoff_to_agent",
        "description": "Delegate one specific subtask to an enabled agent with the requested capability.",
        "parameters": {
            "type": "object",
            "properties": {
                "capability": {
                    "type": "string",
                    "description": "The exact capability required from the receiving agent.",
                },
                "task": {
                    "type": "string",
                    "description": "A bounded subtask with enough context for the receiving agent.",
                },
            },
            "required": ["capability", "task"],
            "additionalProperties": False,
        },
    }

    def generate(
        self,
        agent: dict[str, Any],
        history: list[dict[str, str]],
        *,
        allow_handoff: bool = False,
    ) -> ModelOutput:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ProviderError("OPENAI_API_KEY is not configured.")

        try:
            from langchain_openai import ChatOpenAI

            model = ChatOpenAI(model=agent["model_name"], api_key=api_key)
            instructions = agent["instructions"]
            if allow_handoff:
                instructions += (
                    "\n\nYou may use the handoff_to_agent tool once when a specific subtask "
                    "requires another agent. Do not delegate the entire user request."
                )
                model = model.bind_tools([self._HANDOFF_TOOL])
            messages = [("system", instructions)] + [
                (item["role"], item["content"]) for item in history
            ]
            response = model.invoke(messages)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("The OpenAI model request failed.") from exc

        tool_calls = getattr(response, "tool_calls", None) or []
        if tool_calls:
            if not allow_handoff:
                raise ProviderError("The model requested a handoff when handoff was disabled.")
            if len(tool_calls) != 1:
                raise ProviderError("The model requested more than one agent handoff.")
            call = tool_calls[0]
            if not isinstance(call, dict):
                raise ProviderError("The model returned an invalid handoff request.")
            if call.get("name") != "handoff_to_agent":
                raise ProviderError("The model requested an unsupported tool.")
            args = call.get("args")
            if not isinstance(args, dict):
                raise ProviderError("The model returned an invalid handoff request.")
            return HandoffRequest(
                capability=args.get("capability", ""),
                task=args.get("task", ""),
            )

        content = response.content
        if isinstance(content, list):
            content = "".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
            )
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("The OpenAI model returned an empty response.")
        return content.strip()


def list_codex_models() -> list[dict[str, Any]]:
    """Expose the model and effort choices available to the current Codex account."""
    from openai_codex import Codex

    with Codex() as codex:
        return [
            {
                "id": model.model,
                "label": model.display_name,
                "is_default": model.is_default,
                "default_effort": model.default_reasoning_effort.value,
                "efforts": [option.reasoning_effort.value for option in model.supported_reasoning_efforts],
            }
            for model in codex.models().data
        ]


class CodexChatProvider:
    """Run local Codex using the server user's existing ChatGPT login."""

    _HANDOFF_SCHEMA = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["reply", "handoff"]},
            "content": {"type": "string"},
            "capability": {"type": "string"},
            "task": {"type": "string"},
        },
        "required": ["type", "content", "capability", "task"],
        "additionalProperties": False,
    }

    def generate(
        self,
        agent: dict[str, Any],
        history: list[dict[str, str]],
        *,
        allow_handoff: bool = False,
    ) -> ModelOutput:
        from openai_codex import ApprovalMode, Codex, Sandbox

        instructions = (
            "You are the agent in the conversation below. Follow the agent instructions. "
            "Answer in the user's language. Do not use tools, inspect files, or run commands.\n\n"
            f"Agent instructions:\n{agent['instructions']}"
        )
        if allow_handoff:
            instructions += (
                "\n\nIf one bounded subtask genuinely requires another agent, return a "
                "handoff with its exact capability and a specific task. Otherwise return "
                "a reply. Use empty strings for fields that do not apply. A handoff "
                "result will be supplied in a later invocation."
            )
        prompt = (
            "Here is the conversation history in chronological order as JSON. "
            "Respond to the latest user message; earlier messages are context.\n"
            + json.dumps(history, ensure_ascii=False)
        )
        try:
            with tempfile.TemporaryDirectory(prefix="agent-runtime-codex-") as cwd:
                with Codex() as codex:
                    account = codex.account().account
                    if account is None or account.root.type != "chatgpt":
                        raise ProviderError(
                            "Codex needs a ChatGPT login. Run 'uv run agent-runtime-login' on the server."
                        )
                    thread = codex.thread_start(
                        model=agent["model_name"],
                        cwd=cwd,
                        ephemeral=True,
                        sandbox=Sandbox.read_only,
                        approval_mode=ApprovalMode.deny_all,
                        developer_instructions=instructions,
                        config={
                            **({"model_reasoning_effort": agent["model_reasoning_effort"]}
                               if agent.get("model_reasoning_effort") else {}),
                            "features": {
                                "shell_tool": False,
                                "unified_exec": False,
                                "multi_agent": False,
                                "remote_plugin": False,
                            },
                            "web_search": "disabled",
                        },
                    )
                    result = thread.run(
                        prompt,
                        **({"output_schema": self._HANDOFF_SCHEMA} if allow_handoff else {}),
                    )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("The Codex model request failed.") from exc

        if result.error is not None or not result.final_response:
            raise ProviderError("The Codex model request failed.")
        if not allow_handoff:
            return result.final_response.strip()

        try:
            action = json.loads(result.final_response)
            if action["type"] == "handoff":
                return HandoffRequest(
                    capability=action["capability"], task=action["task"]
                )
            if action["type"] == "reply":
                return action["content"].strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError("Codex returned an invalid response.") from exc
        raise ProviderError("Codex returned an unsupported response.")


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
