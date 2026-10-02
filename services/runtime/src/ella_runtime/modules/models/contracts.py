"""Stable model usage contract for the future token dashboard."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class ModelPurpose(StrEnum):
    CHAT = "chat"
    ACTION = "action"
    GAME = "game"
    VOICE = "voice"


class TokenUsage(BaseModel):
    provider: str
    model: str
    purpose: ModelPurpose
    task_id: str | None = None
    occurred_at: datetime
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)

    @property
    def is_reported(self) -> bool:
        return self.input_tokens is not None and self.output_tokens is not None


class ModelMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class ModelRequest(BaseModel):
    purpose: ModelPurpose
    messages: list[ModelMessage] = Field(min_length=1)
    task_id: str | None = None
    instructions: str = ""
    max_output_tokens: int = Field(default=2048, ge=1, le=32768)


class ModelResponse(BaseModel):
    text: str
    provider: str
    model: str
    usage: TokenUsage
