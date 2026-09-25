from __future__ import annotations

import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response, status

from agent_runtime_platform.database import Database
from agent_runtime_platform.providers import ProviderRegistry
from agent_runtime_platform.runtime import (
    AgentDisabledError,
    AgentNotFoundError,
    AgentRuntimeService,
    ConversationConflictError,
    ConversationNotFoundError,
    InvalidMessageError,
    RunExecutionFailed,
)
from agent_runtime_platform.schemas import AgentCreate, AgentUpdate, ConversationCreate, MessageCreate


def create_app(
    database_url: str | None = None,
    providers: ProviderRegistry | None = None,
) -> FastAPI:
    load_dotenv(override=False)
    database = Database(database_url or os.getenv("AGENT_RUNTIME_DATABASE_URL", "sqlite:///./data/agent_runtime.db"))
    runtime = AgentRuntimeService(database, providers or ProviderRegistry())
    app = FastAPI(
        title="Agent Runtime Platform API",
        version="0.1.0",
        description="Register agents and run a traceable direct message between them.",
    )
    app.state.database = database
    app.state.runtime = runtime

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/agents", status_code=status.HTTP_201_CREATED)
    def create_agent(request: AgentCreate) -> dict:
        try:
            return runtime.create_agent(request.model_dump())
        except InvalidMessageError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/agents")
    def list_agents() -> list[dict]:
        return runtime.list_agents()

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

    @app.post("/conversations/{conversation_id}/messages", status_code=status.HTTP_201_CREATED)
    def send_message(conversation_id: str, request: MessageCreate, response: Response) -> dict:
        try:
            result = runtime.send_message(
                conversation_id=conversation_id,
                sender_agent_id=request.sender_agent_id,
                recipient_agent_id=request.recipient_agent_id,
                content=request.content,
            )
            response.headers["Location"] = f"/runs/{result['id']}"
            return result
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Conversation not found.") from exc
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

    @app.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        run = runtime.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found.")
        return run

    return app
