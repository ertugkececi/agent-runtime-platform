from __future__ import annotations

import os
from typing import Any

from agent_runtime_platform.infrastructure.providers._base import HandoffRequest, ModelOutput, ProviderError


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
                remote_caps = sorted(set(agent.get("remote_a2a_capabilities") or []))
                if remote_caps:
                    instructions += " Available administrator-configured remote A2A capabilities: " + ", ".join(remote_caps) + ". Use an exact listed capability only when useful."

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
