"""Persist completed voice turns and summarize them into long-term memory."""

from __future__ import annotations

from ella_runtime.modules.models.contracts import ModelMessage
from ella_runtime.modules.models.conversations import ConversationStore, ConversationSummarizer

VOICE_CONVERSATION_ID = "voice-main"


class VoiceHistory:
    def __init__(
        self,
        store: ConversationStore,
        summarizer: ConversationSummarizer | None = None,
        *,
        conversation_id: str = VOICE_CONVERSATION_ID,
    ) -> None:
        self.store = store
        self.summarizer = summarizer
        self.conversation_id = store.ensure(conversation_id, kind="voice")
        self._pending: list[tuple[list[ModelMessage], str, int]] = []
        self._flushing = False
        for end in range(16, store.message_count(self.conversation_id) + 1, 16):
            self._queue_summary(end)

    def recent(self) -> list[ModelMessage]:
        return self.store.history(self.conversation_id, limit=12)

    def record(self, user: str, assistant: str) -> None:
        self.store.append_pair(self.conversation_id, user, assistant)
        self._queue_summary(self.store.message_count(self.conversation_id))

    def _queue_summary(self, count: int) -> None:
        if (
            self.summarizer is not None and count > 0 and count % 16 == 0
            and not self.store.summary_window_processed(self.conversation_id, count)
        ):
            self._pending.append((
                self.store.history_window(self.conversation_id, count - 15, count),
                f"voice:{self.conversation_id}:messages:{count - 15}-{count}",
                count,
            ))

    async def flush(self) -> None:
        if self._flushing:
            return
        self._flushing = True
        try:
            while self._pending:
                messages, source_ref, count = self._pending[0]
                if self.summarizer is not None:
                    saved = await self.summarizer.extract(messages, source_ref=source_ref)
                    if saved is None:
                        break
                self.store.mark_summary_window(self.conversation_id, count)
                self._pending.pop(0)
        finally:
            self._flushing = False

    def clear(self) -> None:
        if self._flushing:
            raise ValueError("语音记忆摘要正在处理，请稍后再清空")
        self.store.delete(self.conversation_id)
        self.store.ensure(self.conversation_id, kind="voice")
        self._pending.clear()
