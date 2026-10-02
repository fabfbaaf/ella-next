"""Contracts for game observations and requested actions.

Adapters must verify real game outcomes before reporting an action as done.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field


class GameMode(StrEnum):
    API_CONTROL = "api_control"
    SCRIPT_CONTROL = "script_control"
    SCREEN_CHAT = "screen_chat"


class ActionStatus(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    UNKNOWN = "unknown"


class GameSnapshot(BaseModel):
    game_id: str
    captured_at: datetime
    observation_seq: int = Field(default=0, ge=0)
    state: dict[str, Any] = Field(default_factory=dict)


class GameAction(BaseModel):
    action_id: str
    name: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    client_id: str | None = None
    session_id: str | None = None
    save_id: str | None = None


class ActionResult(BaseModel):
    action_id: str
    status: ActionStatus
    before: GameSnapshot | None = None
    after: GameSnapshot | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class GameAdapter(Protocol):
    game_id: str
    mode: GameMode

    async def observe(self) -> GameSnapshot: ...

    async def apply_action(self, action: GameAction) -> None: ...

    def verify_action(
        self, action: GameAction, before: GameSnapshot, after: GameSnapshot
    ) -> tuple[bool, dict[str, Any]]: ...
