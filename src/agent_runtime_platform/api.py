from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse

from agent_runtime_platform.database import Database
from agent_runtime_platform.auth import AuthMiddleware, OIDCAuth, OIDCConfig, install_auth_routes
from agent_runtime_platform.a2a import A2AError, configured_targets
from agent_runtime_platform.providers import ProviderRegistry, list_codex_models
from agent_runtime_platform.mcp_tools import MCPConfigurationError, public_tool_catalog
from agent_runtime_platform.rooms import RoomRuntimeService
from agent_runtime_platform.resource_auth import ResourceAuthorization, OwnershipScope
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
    auth_config = OIDCConfig.from_environment()
    auth = OIDCAuth(auth_config, database) if auth_config else None
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
    app.state.auth = auth
    resource_auth = ResourceAuthorization(database, auth_config)
    app.state.resource_auth = resource_auth
    runtime.resource_auth = resource_auth
    app.add_middleware(AuthMiddleware, auth=auth)
    install_auth_routes(app, auth)

    def ownership_scope(request: Request) -> OwnershipScope | None:
        if resource_auth.mode == "off":
            return None
        principal = getattr(request.state, "principal", None)
        try:
            scope = resource_auth.scope_for(principal)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="This action is not allowed.") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail="Resource authorization mapping is unavailable.") from exc
        if scope is None:
            raise HTTPException(status_code=401, detail="Authentication required.")
        return scope

    @app.get("/", include_in_schema=False)
    def chat_ui() -> FileResponse:
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/a2a/targets")
    def list_a2a_targets(request: Request) -> list[dict]:
        """Return safe discovery metadata; configured URLs and credentials stay private."""
        ownership_scope(request)
        try:
            return [{"id": target["id"], "kind": "a2a", "capabilities": target["capabilities"]}
                    for target in configured_targets()]
        except A2AError as exc:
            raise HTTPException(status_code=503, detail="A2A target configuration is invalid.") from exc

    @app.get("/mcp/tools")
    def list_mcp_tools(request: Request) -> list[dict]:
        ownership_scope(request)
        try:
            return public_tool_catalog()
        except MCPConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/codex/models")
    def codex_models(request: Request) -> list[dict]:
        ownership_scope(request)
        try:
            return list_codex_models()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Codex model list is unavailable.") from exc

    @app.post("/agents", status_code=status.HTTP_201_CREATED)
    def create_agent(request: AgentCreate, http_request: Request) -> dict:
        try:
            return runtime.create_agent(request.model_dump(), ownership_scope(http_request))
        except InvalidMessageError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/agents")
    def list_agents(request: Request, capability: str | None = Query(default=None, min_length=1, max_length=80)) -> list[dict]:
        try:
            return runtime.list_agents(capability, ownership_scope(request))
        except InvalidMessageError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.patch("/agents/{agent_id}")
    def update_agent(agent_id: str, request: AgentUpdate, http_request: Request) -> dict:
        try:
            return runtime.update_agent(agent_id, request.model_dump(exclude_unset=True, exclude_none=True), ownership_scope(http_request))
        except AgentNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Agent not found.") from exc
        except InvalidMessageError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/conversations", status_code=status.HTTP_201_CREATED)
    def create_conversation(request: ConversationCreate, http_request: Request) -> dict:
        try:
            return runtime.create_conversation(request.agent_ids, ownership_scope(http_request))
        except AgentNotFoundError as exc:
            raise HTTPException(status_code=404, detail="An agent was not found.") from exc
        except AgentDisabledError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except InvalidMessageError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/conversations/{conversation_id}")
    def get_conversation(conversation_id: str, request: Request) -> dict:
        conversation = runtime.get_conversation(conversation_id, ownership_scope(request))
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        return conversation

    @app.post("/rooms", status_code=status.HTTP_201_CREATED)
    def create_room(request: RoomCreate, http_request: Request) -> dict:
        try:
            return room_runtime.create_room(
                request.name, request.participant_agent_ids, request.moderator_agent_id, ownership_scope(http_request)
            )
        except AgentNotFoundError as exc:
            raise HTTPException(status_code=404, detail="An agent was not found.") from exc
        except AgentDisabledError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except InvalidMessageError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/rooms/{room_id}")
    def get_room(room_id: str, request: Request) -> dict:
        room = room_runtime.get_room(room_id, ownership_scope(request))
        if room is None:
            raise HTTPException(status_code=404, detail="Room not found.")
        return room

    @app.post("/rooms/{room_id}/runs", status_code=status.HTTP_202_ACCEPTED)
    def enqueue_room_run(room_id: str, request: RoomRunCreate, http_request: Request) -> dict:
        try:
            return room_runtime.enqueue_run(room_id, request.content, ownership_scope(http_request))
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="Room not found.") from exc
        except AgentNotFoundError as exc:
            raise HTTPException(status_code=404, detail="A room participant was not found.") from exc
        except AgentDisabledError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except InvalidMessageError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/rooms/{room_id}/runs")
    def list_room_runs(room_id: str, request: Request) -> list[dict]:
        runs = room_runtime.list_room_runs(room_id, ownership_scope(request))
        if runs is None:
            raise HTTPException(status_code=404, detail="Room not found.")
        return runs

    @app.post("/chat/conversations", status_code=status.HTTP_201_CREATED)
    def create_human_chat(request: HumanChatCreate, http_request: Request) -> dict:
        try:
            return runtime.create_human_chat_conversation(request.agent_id, ownership_scope(http_request))
        except AgentNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Agent not found.") from exc
        except AgentDisabledError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/chat/conversations/{conversation_id}/messages", status_code=status.HTTP_201_CREATED)
    def send_human_chat_message(
        conversation_id: str,
        request: HumanChatMessageCreate,
        response: Response,
        http_request: Request,
    ) -> dict:
        try:
            result = runtime.send_human_message(conversation_id, request.content, owner_scope=ownership_scope(http_request))
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
    def send_message(conversation_id: str, request: MessageCreate, response: Response, http_request: Request) -> dict:
        try:
            result = runtime.send_message(
                conversation_id=conversation_id,
                sender_agent_id=request.sender_agent_id,
                recipient_agent_id=request.recipient_agent_id,
                recipient_capability=request.recipient_capability,
                content=request.content,
                owner_scope=ownership_scope(http_request),
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
    def enqueue_human_chat_message(conversation_id: str, request: HumanChatMessageCreate, http_request: Request) -> dict:
        try:
            result = runtime.send_human_message(conversation_id, request.content, asynchronous=True, owner_scope=ownership_scope(http_request))
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
    def enqueue_message(conversation_id: str, request: MessageCreate, http_request: Request) -> dict:
        try:
            result = runtime.send_message(
                conversation_id=conversation_id,
                sender_agent_id=request.sender_agent_id,
                recipient_agent_id=request.recipient_agent_id,
                recipient_capability=request.recipient_capability,
                content=request.content,
                asynchronous=True,
                owner_scope=ownership_scope(http_request),
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
    def get_run(run_id: str, request: Request) -> dict:
        scope = ownership_scope(request)
        run = runtime.get_run(run_id, scope)
        if run is None:
            run = room_runtime.get_run(run_id, scope)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found.")
        return run

    return app
