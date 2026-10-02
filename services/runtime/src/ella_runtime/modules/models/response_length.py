"""Select daily response length with explicit user requests taking priority."""

from __future__ import annotations

import secrets


def choose_length(user_text: str, *, draw: int | None = None) -> str:
    if any(word in user_text for word in ("详细说", "展开讲", "讲细点", "多说点", "详细解释")):
        return "long"
    if any(word in user_text for word in ("简单点", "简短点", "一句话", "别啰嗦", "直接说")):
        return "short"
    sample = secrets.randbelow(100) if draw is None else draw
    if not 0 <= sample < 100:
        raise ValueError("随机值必须在 0 到 99 之间")
    return "short" if sample < 40 else "medium" if sample < 70 else "long"


LENGTH_INSTRUCTIONS = {
    "short": "这次日常回应尽量简短，通常 1–2 句；必要的关键信息不能省略。",
    "medium": "这次日常回应保持中等长度，通常 3–5 句。",
    "long": "这次可以多聊一些，解释想法或延伸用户感兴趣的细节，避免空话。",
}
