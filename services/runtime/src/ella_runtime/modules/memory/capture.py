"""Capture directly stated memories from conversation text with provenance."""

import re

from ella_runtime.modules.memory.contracts import (
    MemoryCreate,
    MemoryKind,
    MemorySource,
    preference_claim,
)
from ella_runtime.modules.memory.store import MemoryStore

EXPLICIT = re.compile(r"^(?:请|帮我)?记住(?:一下)?[：:，,\s]*(.+)$", re.DOTALL)
PREFERENCE = re.compile(r"^(?:我喜欢|我不喜欢|我偏好|我习惯|我更喜欢|我的生日是|我住在|我工作于).+")


class MemoryCapture:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    @staticmethod
    def matchable(text: str) -> bool:
        raw = text.strip()
        return bool(
            EXPLICIT.fullmatch(raw)
            or preference_claim(raw) is not None
            or (PREFERENCE.fullmatch(raw) and "?" not in raw and "？" not in raw)
        )

    def capture(self, text: str, *, source_ref: str) -> bool:
        raw = text.strip()
        if not raw or len(raw) > 4000:
            return False
        explicit = EXPLICIT.fullmatch(raw)
        if explicit is not None:
            content = explicit.group(1).strip()
            confidence = 1.0
            tag = "明确记忆"
        elif (PREFERENCE.fullmatch(raw) or preference_claim(raw)) and not any(mark in raw for mark in "？?"):
            content = raw
            confidence = 0.75
            tag = "对话提取"
        else:
            return False
        if not content or len(content) > 4000:
            return False
        normalized = content.casefold()
        if any(item.content.casefold() == normalized for item in self.store.list(limit=10000, active_only=True)):
            return False
        claim = preference_claim(content)
        kind = (
            MemoryKind.PREFERENCE
            if claim is not None or any(word in content for word in ("喜欢", "偏好", "习惯", "讨厌"))
            else MemoryKind.EVENT
            if any(word in content for word in ("昨天", "上周", "发生"))
            else MemoryKind.FACT
        )
        self.store.create(
            MemoryCreate(
                kind=kind,
                content=content,
                source_type=MemorySource.CONVERSATION,
                source_ref=source_ref,
                confidence=confidence,
                tags=[tag],
                topic=claim.topic if claim else None,
                preference_value=claim.value if claim else None,
                preference_negative=claim.negative if claim else False,
            ),
            supersede_topic=claim.topic if claim and claim.replace_existing else None,
            supersede_value=claim.replaces_value if claim else None,
            supersede_reason="用户明确更新偏好：" + content[:200],
        )
        return True
