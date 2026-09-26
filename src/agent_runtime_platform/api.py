from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Response, status
from fastapi.responses import FileResponse

from agent_runtime_platform.database import Database
from agent_runtime_platform.providers import ProviderRegistry, list_codex_models
from agent_runtime_platform.mcp_tools import MCPConfigurationError, public_tool_catalog
from agent_runtime_platform.rooms import RoomRuntimeService
from agent_runtime_platform.runtime import (
    AgentDisabledError,
    AgentAmbiguousError,
    AgentCapabilityNotFoundError,
    AgentNotFoundError,
    AgentRuntimeService,
    ConversationConflictError,
    ConversationNotFoundError,
    InvalidMessageError,
    RunExecutionFailed,
)
from agent_runtime_platform.schemas import (
    AgentCreate,
    AgentUpdate,
    ConversationCreate,
    HumanChatCreate,
    HumanChatMessageCreate,
    MessageCreate,
    RoomCreate,
    RoomRunCreate,
)


def create_app(
    database_url: str | None = None,
    providers: ProviderRegistry | None = None,
) -> FastAPI:
    load_dotenv(override=False)
    database = Database(database_url or os.getenv("AGENT_RUNTIME_DATABASE_URL", "sqlite:///./data/agent_runtime.db"))
    provider_registry = providers or ProviderRegistry()
    runtime = AgentRuntimeService(database, provider_registry)
    room_runtime = RoomRuntimeService(database, provider_registry)
    runtime.room_runtime = room_runtime
    app = FastAPI(
        title="Agent Runtime Platform API",
        version="0.1.0",
        description="Register agents, discover them by capability, and run traceable human or agent conversations.",
    )
    app.state.database = database
    app.state.runtime = runtime

    @app.get("/", include_in_schema=False)
    def chat_ui() -> FileResponse:
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/mcp/tools")
    def list_mcp_tools() -> list[dict]:
        try:
            return public_tool_catalog()
        except MCPConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/codex/models")
    def codex_models() -> list[dict]:
        try:
            return list_codex_models()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Codex model list is unavailable.") from exc

    @app.post("/agents", status_code=status.HTTP_201_CREATED)
    def create_agent(request: AgentCreate) -> dict:
        try:
            return runtime.create_agent(request.model_dump())
        except InvalidMessageError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/agents")
    def list_agents(capability: str | None = Query(default=None, min_length=1, max_length=80)) -> list[dict]:
        try:
            return runtime.list_agents(capability)
        except InvalidMessageError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.patch("/agents/{agent_id}")
    def update_agent(agent_id: str, request: AgentUpdate) -> dict:
        try:
            return runtime.update_agent(agent_id, request.model_dump(exclude_unset=True, exclude_none=True))
        except AgentNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Agent not found.") from exc
        except InvalidMessageError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/conversations", status_code=status.HTTP_201_CREATED)
    def create_conversation(request: ConversationCreate) -> dict:
        try:
            return runtime.create_conversation(request.agent_ids)
        except AgentNotFoundError as exc:
            raise HTTPException(status_code=404, detail="An agent was not found.") from exc
        except AgentDisabledError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except InvalidMessageError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/conversations/{conversation_id}")
    def get_conversation(conversation_id: str) -> dict:
        conversation = runtime.get_conversation(conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        return conversation

    @app.post("/rooms", status_code=status.HTTP_201_CREATED)
    def create_room(request: RoomCreate) -> dict:
        try:
            return room_runtime.create_room(
                request.name, request.participant_agent_ids, request.moderator_agent_id
            )
        except AgentNotFoundError as exc:
            raise HTTPException(status_code=404, detail="An agent was not found.") from exc
        except AgentDisabledError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except InvalidMessageError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/rooms/{room_id}")
    def get_room(room_id: str) -> dict:
        room = room_runtime.get_room(room_id)
        if room is None:
            raise HTTPException(status_code=404, detail="Room not found.")
        return room

    @app.post("/rooms/{room_id}/runs", status_code=status.HTTP_202_ACCEPTED)
    def enqueue_room_run(room_id: str, request: RoomRunCreate) -> dict:
        try:
            return room_runtime.enqueue_run(room_id, request.content)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="Room not found.") from exc
        except AgentNotFoundError as exc:
            raise HTTPException(status_code=404, detail="A room participant was not found.") from exc
        except AgentDisabledError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except InvalidMessageError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/rooms/{room_id}/runs")
    def list_room_runs(room_id: str) -> list[dict]:
        runs = room_runtime.list_room_runs(room_id)
        if runs is None:
            raise HTTPException(status_code=404, detail="Room not found.")
        return runs

    @app.post("/chat/conversations", status_code=status.HTTP_201_CREATED)
    def create_human_chat(request: HumanChatCreate) -> dict:
        try:
            return runtime.create_human_chat_conversation(request.agent_id)
        except AgentNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Agent not found.") from exc
        except AgentDisabledError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/chat/conversations/{conversation_id}/messages", status_code=status.HTTP_201_CREATED)
    def send_human_chat_message(
        conversation_id: str,
        request: HumanChatMessageCreate,
        response: Response,
    ) -> dict:
        try:
            result = runtime.send_human_message(conversation_id, request.content)
            response.headers["Location"] = f"/runs/{result['id']}"
            return result
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Conversation not found.") from exc
        except ConversationConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AgentNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Agent not found.") from exc
        except AgentDisabledError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RunExecutionFailed as exc:
            raise HTTPException(
                status_code=502,
                detail={"error": "run_failed", "run_id": exc.run_id},
            ) from exc

    @app.post("/conversations/{conversation_id}/messages", status_code=status.HTTP_201_CREATED)
    def send_message(conversation_id: str, request: MessageCreate, response: Response) -> dict:
        try:
            result = runtime.send_message(
                conversation_id=conversation_id,
                sender_agent_id=request.sender_agent_id,
                recipient_agent_id=request.recipient_agent_id,
                recipient_capability=request.recipient_capability,
                content=request.content,
            )
            response.headers["Location"] = f"/runs/{result['id']}"
            return result
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Conversation not found.") from exc
        except AgentCapabilityNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail="No enabled agent matches the requested capability.",
            ) from exc
        except AgentAmbiguousError as exc:
            raise HTTPException(
                status_code=409,
                detail="Multiple enabled agents match the requested capability.",
            ) from exc
        except AgentNotFoundError as exc:
            raise HTTPException(status_code=404, detail="An agent was not found.") from exc
        except AgentDisabledError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ConversationConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except InvalidMessageError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RunExecutionFailed as exc:
            raise HTTPException(
                status_code=502,
                detail={"error": "run_failed", "run_id": exc.run_id},
            ) from exc

    @app.post("/chat/conversations/{conversation_id}/messages/async", status_code=status.HTTP_202_ACCEPTED)
    def enqueue_human_chat_message(conversation_id: str, request: HumanChatMessageCreate) -> dict:
        try:
            result = runtime.send_human_message(conversation_id, request.content, asynchronous=True)
            return {"id": result["id"], "status": result["status"], "status_url": f"/runs/{result['id']}"}
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Conversation not found.") from exc
        except ConversationConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AgentNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Agent not found.") from exc
        except AgentDisabledError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/conversations/{conversation_id}/messages/async", status_code=status.HTTP_202_ACCEPTED)
    def enqueue_message(conversation_id: str, request: MessageCreate) -> dict:
        try:
            result = runtime.send_message(
                conversation_id=conversation_id,
                sender_agent_id=request.sender_agent_id,
                recipient_agent_id=request.recipient_agent_id,
                recipient_capability=request.recipient_capability,
                content=request.content,
                asynchronous=True,
            )
            return {"id": result["id"], "status": result["status"], "status_url": f"/runs/{result['id']}"}
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Conversation not found.") from exc
        except AgentCapabilityNotFoundError as exc:
            raise HTTPException(status_code=404, detail="No enabled agent matches the requested capability.") from exc
        except AgentAmbiguousError as exc:
            raise HTTPException(status_code=409, detail="Multiple enabled agents match the requested capability.") from exc
        except AgentNotFoundError as exc:
            raise HTTPException(status_code=404, detail="An agent was not found.") from exc
        except (AgentDisabledError, ConversationConflictError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except InvalidMessageError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        run = runtime.get_run(run_id)
        if run is None:
            run = room_runtime.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found.")
        return run

    return app
