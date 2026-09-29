from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _normalize_capabilities(values: list[str]) -> list[str]:
    normalized: set[str] = set()
    if len(values) > 30:
        raise ValueError("An agent can have at most 30 capabilities")
    for value in values:
        capability = value.strip().casefold()
        if not capability:
            raise ValueError("Capabilities cannot be blank")
        if len(capability) > 80:
            raise ValueError("Capabilities cannot exceed 80 characters")
        normalized.add(capability)
    return sorted(normalized)


class AgentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    instructions: str = Field(min_length=1, max_length=20_000)
    model_provider: str = Field(default="codex", min_length=1, max_length=40)
    model_name: str = Field(min_length=1, max_length=160)
    model_reasoning_effort: Literal["low", "medium", "high", "xhigh", "max", "ultra"] | None = None
    enabled: bool = True
    published: bool | None = None
    capabilities: list[str] = Field(default_factory=list, max_length=30)
    tool_ids: list[str] = Field(default_factory=list, max_length=50)

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

    @field_validator("capabilities")
    @classmethod
    def normalize_capabilities(cls, value: list[str]) -> list[str]:
        return _normalize_capabilities(value)


class AgentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    instructions: str | None = Field(default=None, min_length=1, max_length=20_000)
    model_provider: str | None = Field(default=None, min_length=1, max_length=40)
    model_name: str | None = Field(default=None, min_length=1, max_length=160)
    model_reasoning_effort: Literal["low", "medium", "high", "xhigh", "max", "ultra"] | None = None
    enabled: bool | None = None
    published: bool | None = None
    capabilities: list[str] | None = Field(default=None, max_length=30)
    tool_ids: list[str] | None = Field(default=None, max_length=50)

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

    @field_validator("capabilities")
    @classmethod
    def normalize_optional_capabilities(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        return _normalize_capabilities(value)


class AgentConfigProvider(BaseModel):
    """One configured provider and the capabilities its manifest declares.

    ``model_catalog_url`` is the contract path that lists the provider's models
    and their supported reasoning efforts, or ``None`` when it has no catalog.
    """

    id: str
    supports_tool_ids: bool
    model_catalog_url: str | None


class AgentConfigCatalog(BaseModel):
    """What an agent configuration screen may rely on, and nothing more."""

    providers: list[AgentConfigProvider]


class RoomCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    participant_agent_ids: list[str] = Field(min_length=2, max_length=5)
    moderator_agent_id: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def strip_room_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Room name cannot be blank")
        return value


class RoomRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=20_000)

    @field_validator("content")
    @classmethod
    def strip_room_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Room task cannot be blank")
        return value


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_ids: list[str] = Field(min_length=2, max_length=20)


class MessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sender_agent_id: str = Field(min_length=1)
    recipient_agent_id: str | None = Field(default=None, min_length=1)
    recipient_capability: str | None = Field(default=None, min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=20_000)

    @field_validator("recipient_capability")
    @classmethod
    def normalize_recipient_capability(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip().casefold()
        if not value:
            raise ValueError("Recipient capability cannot be blank")
        return value

    @model_validator(mode="after")
    def require_one_recipient_selector(self) -> "MessageCreate":
        if (self.recipient_agent_id is None) == (self.recipient_capability is None):
            raise ValueError("Provide exactly one of recipient_agent_id or recipient_capability")
        return self

    @field_validator("content")
    @classmethod
    def strip_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Message content cannot be blank")
        return value


class HumanChatCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str = Field(min_length=1)


class HumanChatMessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=20_000)

    @field_validator("content")
    @classmethod
    def strip_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Message content cannot be blank")
        return value
