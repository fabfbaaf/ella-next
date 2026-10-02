"""Persisted task, plan and tool contracts."""

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field, computed_field


class TaskState(StrEnum):
    WAITING_APPROVAL = "waiting_approval"
    READY = "ready"
    RUNNING = "running"
    NEEDS_RECONCILIATION = "needs_reconciliation"
    COMPLETE = "complete"
    FAILED = "failed"


class ToolPreconditionError(ValueError):
    """The tool explicitly guarantees no target action or write has started.

    Only validation and read-only preflight may raise this error. Errors during
    or after a write/launch must keep the outcome uncertain, regardless of type.
    """


class StepState(StrEnum):
    FAILED = "failed"
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    NEEDS_RECONCILIATION = "needs_reconciliation"


class TaskStep(BaseModel):
    id: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    risk_level: str = "review"
    risk_reason: str = "需要确认计划"
    destination: str | None = None
    state: StepState = StepState.PENDING
    evidence: dict[str, Any] = Field(default_factory=dict)


class AgentTask(BaseModel):
    id: str
    goal: str
    created_at: datetime
    updated_at: datetime
    state: TaskState
    steps: list[TaskStep] = Field(min_length=1, max_length=12)
    error: str | None = None
    approved_plan_hash: str | None = None

    @computed_field
    @property
    def plan_hash(self) -> str:
        visible_plan = {
            "goal": self.goal,
            "steps": [
                {
                    "id": step.id,
                    "tool": step.tool,
                    "arguments": step.arguments,
                    "reason": step.reason,
                    "risk_level": step.risk_level,
                    "risk_reason": step.risk_reason,
                    "destination": step.destination,
                }
                for step in self.steps
            ],
        }
        encoded = json.dumps(
            visible_plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class ToolEvidence(BaseModel):
    verified: bool
    details: dict[str, Any] = Field(default_factory=dict)


class AgentTool(Protocol):
    name: str
    description: str

    async def execute(self, arguments: dict[str, Any], *, action_id: str) -> ToolEvidence: ...

    async def reconcile(
        self, arguments: dict[str, Any], *, action_id: str
    ) -> ToolEvidence | None: ...
