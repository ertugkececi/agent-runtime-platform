from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    instructions: str = Field(min_length=1, max_length=20_000)
    model_provider: Literal["openai"] = "openai"
    model_name: str = Field(min_length=1, max_length=160)
    enabled: bool = True

    @field_validator("name", "model_name")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Value cannot be blank")
        return value

    @field_validator("instructions")
    @classmethod
    def strip_instructions(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Instructions cannot be blank")
        return value


class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    instructions: str | None = Field(default=None, min_length=1, max_length=20_000)
    model_provider: Literal["openai"] | None = None
    model_name: str | None = Field(default=None, min_length=1, max_length=160)
    enabled: bool | None = None

    @field_validator("name", "model_name")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("Value cannot be blank")
        return value

    @field_validator("instructions")
    @classmethod
    def strip_optional_instructions(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("Instructions cannot be blank")
        return value


class ConversationCreate(BaseModel):
    agent_ids: list[str] = Field(min_length=2, max_length=20)


class MessageCreate(BaseModel):
    sender_agent_id: str
    recipient_agent_id: str
    content: str = Field(min_length=1, max_length=20_000)

    @field_validator("content")
    @classmethod
    def strip_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Message content cannot be blank")
        return value
