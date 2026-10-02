"""User-visible memory records and provenance contracts."""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class MemoryKind(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    EVENT = "event"


class MemorySource(StrEnum):
    MANUAL = "manual"
    CONVERSATION = "conversation"
    TASK = "task"
    GAME = "game"


class MemoryCreate(BaseModel):
    kind: MemoryKind
    content: str = Field(min_length=1, max_length=4000)
    source_type: MemorySource = MemorySource.MANUAL
    source_ref: str | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    tags: list[str] = Field(default_factory=list, max_length=20)
    topic: str | None = Field(default=None, max_length=120)
    preference_value: str | None = Field(default=None, max_length=120)
    preference_negative: bool = False

    @model_validator(mode="after")
    def require_source_reference(self) -> "MemoryCreate":
        self.content = self.content.strip()
        if not self.content:
            raise ValueError("记忆内容不能为空")
        if self.source_type != MemorySource.MANUAL and not self.source_ref:
            raise ValueError("自动记忆必须指明来源引用")
        self.tags = list(dict.fromkeys(tag.strip() for tag in self.tags if tag.strip()))
        return self


class MemoryCorrection(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    reason: str = Field(min_length=1, max_length=500)
    tags: list[str] | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def strip_fields(self) -> "MemoryCorrection":
        self.content = self.content.strip()
        self.reason = self.reason.strip()
        if not self.content or not self.reason:
            raise ValueError("修正内容和原因不能为空")
        if self.tags is not None:
            self.tags = list(dict.fromkeys(tag.strip() for tag in self.tags if tag.strip()))
        return self


class MemoryRecord(BaseModel):
    id: str
    kind: MemoryKind
    content: str
    source_type: MemorySource
    source_ref: str | None
    confidence: float
    tags: list[str]
    created_at: datetime
    updated_at: datetime

    topic: str | None = None
    preference_value: str | None = None
    preference_negative: bool = False
    active: bool = True
    superseded_by: str | None = None
    inactive_reason: str | None = None


@dataclass(frozen=True)
class PreferenceClaim:
    topic: str
    value: str
    negative: bool = False
    replace_existing: bool = False
    replaces_value: str | None = None


def preference_claim(content: str) -> PreferenceClaim | None:
    """Only explicit, single claims are comparable; unrelated likes coexist."""
    raw = content.strip().rstrip("。.!！")
    if not raw or len(raw) > 200 or any(mark in raw for mark in "?？;；"):
        return None
    changed_name = re.fullmatch(
        r"(?:别|不要)(?:再)?叫我([^，,。]{1,40}?)(?:了)?[，,](?:以后|现在)?叫我([^，,。]{1,40})", raw
    )
    if changed_name:
        return PreferenceClaim("称呼", changed_name[2].strip(), replace_existing=True,
                               replaces_value=changed_name[1].strip())
    name = re.fullmatch(r"(?:以后|现在)?(?:叫我|请叫我|我希望你叫我)([^，,。]{1,40})", raw)
    if name:
        return PreferenceClaim("称呼", name[1].strip(), replace_existing=True)
    rejected_name = re.fullmatch(r"(?:别|不要)(?:再)?叫我([^，,。]{1,40}?)(?:了)?", raw)
    if rejected_name:
        value = rejected_name[1].strip()
        return PreferenceClaim("称呼", value, True, True, value)
    favorite = re.fullmatch(
        r"(?:我|用户)(?:最喜欢的)(颜色|饮料|食物|游戏)(是|改为|改成|更正为)[：:\s]*([^，,。]{1,60})", raw
    )
    if favorite:
        return PreferenceClaim("最喜欢的" + favorite[1], favorite[3].strip(),
                               replace_existing=favorite[2] != "是")
    formerly = re.fullmatch(
        r"(?:我|用户)(?:以前|之前)喜欢([^，,。]{1,80})[，,](?:现在|如今)(?:已经)?不喜欢了", raw
    )
    if formerly:
        value = formerly[1].strip().casefold()
        return PreferenceClaim("喜好:" + value.casefold(), value, True, True, value)
    liked = re.fullmatch(
        r"(?:我|用户)(?:现在|目前)?(?:已经)?(不再喜欢|不喜欢|喜欢|偏好)[：:\s]*([^，,。]{1,80}?)(?:了)?", raw
    )
    if liked:
        value = liked[2].strip().casefold()
        if not value or any(word in value for word in ("但是", "不过", "如果", "的话", "还是")):
            return None
        return PreferenceClaim("喜好:" + value.casefold(), value,
                               liked[1] in {"不再喜欢", "不喜欢"}, True, value)
    return None
