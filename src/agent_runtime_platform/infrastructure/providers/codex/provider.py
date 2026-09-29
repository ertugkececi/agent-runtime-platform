from __future__ import annotations

import json
import tempfile
from typing import TYPE_CHECKING, Any

from agent_runtime_platform.infrastructure.providers._base import HandoffRequest, ModelOutput, ProviderError

if TYPE_CHECKING:
    # The SDK is imported lazily at call time (see test_provider_modules.py);
    # this import exists only for the handoff schema annotation.
    from openai_codex.models import JsonObject


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

    _HANDOFF_SCHEMA: JsonObject = {
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
        from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox

        tool_ids = sorted(set(agent.get("tool_ids") or []))
        tool_event_callback = agent.get("tool_event_callback")
        instructions = (
            "You are the agent in the conversation below. Follow the agent instructions. "
            "Answer in the user's language. "
            + ("Do not inspect files or run commands.\n\n" if tool_ids else
               "Do not use tools, inspect files, or run commands.\n\n")
            + f"Agent instructions:\n{agent['instructions']}"
        )
        if tool_ids:
            instructions += (
                "\n\nYou may use only the explicitly available administrator-approved, "
                "read-only MCP tools when they help answer the user. Do not claim a tool was "
                "used unless Codex reports that tool call."
            )
        if allow_handoff:
            instructions += (
                "\n\nIf one bounded subtask genuinely requires another agent, return a "
                "handoff with its exact capability and a specific task. Otherwise return "
                "a reply. Use empty strings for fields that do not apply. A handoff "
                "result will be supplied in a later invocation."
            )
            remote_caps = sorted(set(agent.get("remote_a2a_capabilities") or []))
            if remote_caps:
                instructions += " Administrator-configured remote A2A capabilities: " + ", ".join(remote_caps) + "."

        prompt = (
            "Here is the conversation history in chronological order as JSON. "
            "Respond to the latest user message; earlier messages are context.\n"
            + json.dumps(history, ensure_ascii=False)
        )
        try:
            from agent_runtime_platform.infrastructure.codex_home import prepare_codex_home
            from agent_runtime_platform.infrastructure.mcp_tools import codex_mcp_overrides

            codex_home = prepare_codex_home()
            mcp_overrides = codex_mcp_overrides(tool_ids) if tool_ids else ()
            with tempfile.TemporaryDirectory(prefix="agent-runtime-codex-") as cwd:
                with Codex(config=CodexConfig(
                    env={"CODEX_HOME": str(codex_home)},
                    config_overrides=mcp_overrides,
                )) as codex:
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
                    turn = (
                        thread.turn(prompt, output_schema=self._HANDOFF_SCHEMA)
                        if allow_handoff
                        else thread.turn(prompt)
                    )
                    from openai_codex import TurnResult
                    from openai_codex.models import (
                        ItemCompletedNotification,
                        TurnCompletedNotification,
                    )
                    from openai_codex.generated.v2_all import (
                        AgentMessageThreadItem,
                        McpToolCallThreadItem,
                        MessagePhase,
                    )

                    items = []
                    completed_turn = None
                    for notification in turn.stream():
                        payload = notification.payload
                        if isinstance(payload, ItemCompletedNotification) and payload.turn_id == turn.id:
                            items.append(payload.item)
                            item = payload.item.root
                            if isinstance(item, McpToolCallThreadItem) and callable(tool_event_callback):
                                tool_event_callback({
                                    "server": item.server,
                                    "tool": item.tool,
                                    "status": item.status.value,
                                })
                        elif isinstance(payload, TurnCompletedNotification) and payload.turn.id == turn.id:
                            completed_turn = payload.turn
                    if completed_turn is None:
                        raise ProviderError("Codex did not return a completed turn.")
                    final_response = None
                    fallback_response = None
                    for entry in reversed(items):
                        item = entry.root
                        if not isinstance(item, AgentMessageThreadItem):
                            continue
                        if fallback_response is None:
                            fallback_response = item.text
                        if item.phase == MessagePhase.final_answer:
                            final_response = item.text
                            break
                    result = TurnResult(
                        id=completed_turn.id,
                        status=completed_turn.status,
                        error=completed_turn.error,
                        started_at=completed_turn.started_at,
                        completed_at=completed_turn.completed_at,
                        duration_ms=completed_turn.duration_ms,
                        final_response=final_response or fallback_response,
                        items=items,
                        usage=None,
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
