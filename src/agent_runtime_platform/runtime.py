from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_runtime_platform.database import Database
from agent_runtime_platform.models import (
    Agent,
    Conversation,
    ConversationMember,
    Message,
    Run,
    RunEvent,
    new_id,
    utc_now,
)
from agent_runtime_platform.providers import ProviderError, ProviderRegistry


class AgentNotFoundError(Exception):
    pass


class AgentDisabledError(Exception):
    pass


class ConversationNotFoundError(Exception):
    pass


class ConversationConflictError(Exception):
    pass


class InvalidMessageError(Exception):
    pass


class RunExecutionFailed(Exception):
    def __init__(self, run_id: str) -> None:
        super().__init__("Agent run failed")
        self.run_id = run_id


class MessagingState(TypedDict):
    run_id: str
    target_config: NotRequired[dict[str, Any]]
    history: NotRequired[list[dict[str, str]]]
    reply: NotRequired[str]


def _agent_snapshot(agent: Agent) -> dict[str, Any]:
    return {
        "id": agent.id,
        "name": agent.name,
        "instructions": agent.instructions,
        "model_provider": agent.model_provider,
        "model_name": agent.model_name,
        "version": agent.version,
    }


def _public_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": snapshot["id"],
        "name": snapshot["name"],
        "model_provider": snapshot["model_provider"],
        "model_name": snapshot["model_name"],
        "version": snapshot["version"],
    }


def _agent_payload(agent: Agent) -> dict[str, Any]:
    return {
        "id": agent.id,
        "name": agent.name,
        "description": agent.description,
        "instructions": agent.instructions,
        "model_provider": agent.model_provider,
        "model_name": agent.model_name,
        "enabled": agent.enabled,
        "version": agent.version,
        "created_at": agent.created_at.isoformat(),
        "updated_at": agent.updated_at.isoformat(),
    }


def _message_payload(message: Message) -> dict[str, Any]:
    return {
        "id": message.id,
        "run_id": message.run_id,
        "conversation_id": message.conversation_id,
        "sequence": message.sequence,
        "sender_agent_id": message.sender_agent_id,
        "recipient_agent_id": message.recipient_agent_id,
        "content": message.content,
        "kind": message.kind,
        "created_at": message.created_at.isoformat(),
    }


def _append_event(session: Session, run: Run, event_type: str, payload: dict[str, Any]) -> None:
    run.next_event_sequence += 1
    session.add(
        RunEvent(
            run_id=run.id,
            sequence=run.next_event_sequence,
            event_type=event_type,
            payload=payload,
        )
    )


class AgentRuntimeService:
    """Product services and one reusable, data-driven LangGraph workflow."""

    def __init__(self, database: Database, providers: ProviderRegistry) -> None:
        self.database = database
        self.providers = providers
        builder = StateGraph(MessagingState)
        builder.add_node("prepare_context", self._prepare_context)
        builder.add_node("invoke_model", self._invoke_model)
        builder.add_node("persist_response", self._persist_response)
        builder.add_edge(START, "prepare_context")
        builder.add_edge("prepare_context", "invoke_model")
        builder.add_edge("invoke_model", "persist_response")
        builder.add_edge("persist_response", END)
        self.graph = builder.compile()

    def create_agent(self, data: dict[str, Any]) -> dict[str, Any]:
        if not self.providers.supports(data["model_provider"]):
            raise InvalidMessageError("The requested model provider is not configured.")
        with self.database.session() as session:
            agent = Agent(**data)
            session.add(agent)
            session.commit()
            return _agent_payload(agent)

    def list_agents(self) -> list[dict[str, Any]]:
        with self.database.session() as session:
            agents = session.scalars(select(Agent).order_by(Agent.created_at, Agent.id)).all()
            return [_agent_payload(agent) for agent in agents]

    def update_agent(self, agent_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        with self.database.session() as session:
            agent = session.get(Agent, agent_id)
            if agent is None:
                raise AgentNotFoundError(agent_id)
            if "model_provider" in changes and not self.providers.supports(changes["model_provider"]):
                raise InvalidMessageError("The requested model provider is not configured.")
            if changes:
                for key, value in changes.items():
                    setattr(agent, key, value)
                agent.version += 1
                agent.updated_at = utc_now()
                session.commit()
            return _agent_payload(agent)

    def create_conversation(self, agent_ids: list[str]) -> dict[str, Any]:
        if len(set(agent_ids)) != len(agent_ids):
            raise InvalidMessageError("Agent IDs in a conversation must be unique.")
        with self.database.session() as session:
            agents = session.scalars(select(Agent).where(Agent.id.in_(agent_ids))).all()
            agents_by_id = {agent.id: agent for agent in agents}
            if len(agents_by_id) != len(agent_ids):
                missing = next(agent_id for agent_id in agent_ids if agent_id not in agents_by_id)
                raise AgentNotFoundError(missing)
            if any(not agent.enabled for agent in agents):
                raise AgentDisabledError("Disabled agents cannot join a new conversation.")

            conversation = Conversation(status="open")
            session.add(conversation)
            session.flush()
            session.add_all(
                [ConversationMember(conversation_id=conversation.id, agent_id=agent_id) for agent_id in agent_ids]
            )
            session.commit()
            return self._conversation_payload(session, conversation)

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        with self.database.session() as session:
            conversation = session.get(Conversation, conversation_id)
            if conversation is None:
                return None
            return self._conversation_payload(session, conversation)

    def _conversation_payload(self, session: Session, conversation: Conversation) -> dict[str, Any]:
        agent_ids = session.scalars(
            select(ConversationMember.agent_id)
            .where(ConversationMember.conversation_id == conversation.id)
            .order_by(ConversationMember.agent_id)
        ).all()
        messages = session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.sequence)
        ).all()
        return {
            "id": conversation.id,
            "status": conversation.status,
            "agent_ids": list(agent_ids),
            "messages": [_message_payload(message) for message in messages],
            "created_at": conversation.created_at.isoformat(),
        }

    def send_message(
        self,
        conversation_id: str,
        sender_agent_id: str,
        recipient_agent_id: str,
        content: str,
    ) -> dict[str, Any]:
        if sender_agent_id == recipient_agent_id:
            raise InvalidMessageError("Sender and recipient must be different agents.")

        run_id = new_id()
        with self.database.session() as session:
            conversation = session.get(Conversation, conversation_id)
            if conversation is None:
                raise ConversationNotFoundError(conversation_id)
            if conversation.status != "open":
                raise ConversationConflictError("The conversation is not open.")

            agents = {
                agent.id: agent
                for agent in session.scalars(
                    select(Agent).where(Agent.id.in_([sender_agent_id, recipient_agent_id]))
                ).all()
            }
            if sender_agent_id not in agents:
                raise AgentNotFoundError(sender_agent_id)
            if recipient_agent_id not in agents:
                raise AgentNotFoundError(recipient_agent_id)
            source = agents[sender_agent_id]
            target = agents[recipient_agent_id]
            if not source.enabled or not target.enabled:
                raise AgentDisabledError("Disabled agents cannot send or receive new messages.")

            members = set(
                session.scalars(
                    select(ConversationMember.agent_id).where(
                        ConversationMember.conversation_id == conversation_id
                    )
                ).all()
            )
            if sender_agent_id not in members or recipient_agent_id not in members:
                raise ConversationConflictError("Both agents must belong to the conversation.")

            run = Run(
                id=run_id,
                conversation_id=conversation_id,
                source_agent_id=source.id,
                target_agent_id=target.id,
                source_config_snapshot=_agent_snapshot(source),
                target_config_snapshot=_agent_snapshot(target),
                status="running",
            )
            session.add(run)
            session.flush()

            conversation.next_message_sequence += 1
            session.add(
                Message(
                    run_id=run_id,
                    conversation_id=conversation_id,
                    sequence=conversation.next_message_sequence,
                    sender_agent_id=sender_agent_id,
                    recipient_agent_id=recipient_agent_id,
                    content=content,
                    kind="agent_message",
                )
            )
            _append_event(session, run, "run_started", {"conversation_id": conversation_id})
            _append_event(
                session,
                run,
                "message_sent",
                {"sender_agent_id": sender_agent_id, "recipient_agent_id": recipient_agent_id},
            )
            session.commit()

        try:
            self.graph.invoke({"run_id": run_id})
        except Exception as exc:
            self._mark_run_failed(run_id, exc)
            raise RunExecutionFailed(run_id) from exc

        result = self.get_run(run_id)
        if result is None:
            raise RunExecutionFailed(run_id)
        return result

    def _prepare_context(self, state: MessagingState) -> dict[str, Any]:
        with self.database.session() as session:
            run = session.get(Run, state["run_id"])
            if run is None:
                raise RuntimeError("Run disappeared before model execution.")
            target_id = run.target_agent_id
            messages = session.scalars(
                select(Message)
                .where(Message.conversation_id == run.conversation_id)
                .order_by(Message.sequence)
            ).all()
            history: list[dict[str, str]] = []
            for message in messages:
                if message.sender_agent_id == target_id:
                    history.append({"role": "assistant", "content": message.content})
                elif message.recipient_agent_id == target_id:
                    history.append({"role": "user", "content": message.content})
            _append_event(
                session,
                run,
                "agent_invocation_started",
                {"agent_id": target_id, "agent_version": run.target_config_snapshot["version"]},
            )
            target_config = run.target_config_snapshot
            session.commit()
            return {"target_config": target_config, "history": history}

    def _invoke_model(self, state: MessagingState) -> dict[str, str]:
        target_config = state["target_config"]
        history = state["history"]
        with self.database.session() as session:
            run = session.get(Run, state["run_id"])
            if run is None:
                raise RuntimeError("Run disappeared before model invocation.")
            _append_event(
                session,
                run,
                "model_call_started",
                {
                    "agent_id": target_config["id"],
                    "provider": target_config["model_provider"],
                    "model": target_config["model_name"],
                },
            )
            session.commit()

        reply = self.providers.generate(target_config, history)
        if not reply.strip():
            raise ProviderError("The model returned an empty response.")

        with self.database.session() as session:
            run = session.get(Run, state["run_id"])
            if run is None:
                raise RuntimeError("Run disappeared after model invocation.")
            _append_event(session, run, "model_call_completed", {"agent_id": target_config["id"]})
            session.commit()
        return {"reply": reply.strip()}

    def _persist_response(self, state: MessagingState) -> dict[str, str]:
        with self.database.session() as session:
            run = session.get(Run, state["run_id"])
            if run is None:
                raise RuntimeError("Run disappeared before response persistence.")
            conversation = session.get(Conversation, run.conversation_id)
            if conversation is None:
                raise RuntimeError("Conversation disappeared before response persistence.")
            conversation.next_message_sequence += 1
            response = Message(
                id=new_id(),
                run_id=run.id,
                conversation_id=run.conversation_id,
                sequence=conversation.next_message_sequence,
                sender_agent_id=run.target_agent_id,
                recipient_agent_id=run.source_agent_id,
                content=state["reply"],
                kind="agent_response",
            )
            session.add(response)
            run.status = "completed"
            run.completed_at = datetime.now(timezone.utc)
            _append_event(session, run, "agent_response_saved", {"message_id": response.id})
            _append_event(session, run, "run_completed", {"status": run.status})
            session.commit()
            return {}

    def _mark_run_failed(self, run_id: str, error: Exception) -> None:
        with self.database.session() as session:
            run = session.get(Run, run_id)
            if run is None or run.status != "running":
                return
            run.status = "failed"
            run.completed_at = datetime.now(timezone.utc)
            run.error_code = "provider_error" if isinstance(error, ProviderError) else "runtime_error"
            _append_event(
                session,
                run,
                "run_failed",
                {"error_code": run.error_code},
            )
            session.commit()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.database.session() as session:
            run = session.get(Run, run_id)
            if run is None:
                return None
            events = session.scalars(
                select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.sequence)
            ).all()
            messages = session.scalars(
                select(Message).where(Message.run_id == run_id).order_by(Message.sequence)
            ).all()
            return {
                "id": run.id,
                "conversation_id": run.conversation_id,
                "source_agent_id": run.source_agent_id,
                "target_agent_id": run.target_agent_id,
                "agent_snapshots": {
                    "source": _public_snapshot(run.source_config_snapshot),
                    "target": _public_snapshot(run.target_config_snapshot),
                },
                "status": run.status,
                "error_code": run.error_code,
                "messages": [_message_payload(message) for message in messages],
                "events": [
                    {
                        "sequence": event.sequence,
                        "type": event.event_type,
                        "payload": event.payload,
                        "created_at": event.created_at.isoformat(),
                    }
                    for event in events
                ],
                "started_at": run.started_at.isoformat(),
                "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            }
