"""Explicit local listening controls, never inferred from arbitrary dialogue."""

PAUSE_COMMANDS = frozenset({
    "暂停监听", "停止监听", "关闭监听", "暂停语音", "先别聊了", "先不聊了",
    "这会儿不想聊天", "我现在不想聊天", "先安静一会儿",
})


def pauses_listening(text: str) -> bool:
    return text.strip().strip("，。！？!? ") in PAUSE_COMMANDS
