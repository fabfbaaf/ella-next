import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

from ella_runtime import api
from ella_runtime.modules.memory.contracts import MemoryCreate, MemoryKind, MemorySource
from ella_runtime.modules.memory.store import MemoryStore
from ella_runtime.modules.memory.summary import MemorySummaryExtractor
from ella_runtime.modules.models.contracts import ModelPurpose, ModelResponse, TokenUsage
from ella_runtime.modules.models.conversations import ConversationStore
from ella_runtime.modules.models.provider import ModelProviderError
from ella_runtime.modules.models.usage_store import UsageStore
from ella_runtime.modules.voice.history import VoiceHistory
from ella_runtime.modules.voice.provider import Transcription
from ella_runtime.modules.voice.session import VoiceSession
from tests.api_client import authorized_client


def _usage() -> TokenUsage:
    return TokenUsage(
        provider="test", model="voice", purpose=ModelPurpose.VOICE,
        occurred_at=datetime.now(UTC),
    )


def test_completed_voice_turns_survive_restart_and_get_topic_summary(tmp_path):
    class Recognizer:
        async def transcribe(self, audio, *, filename, media_type, task_id=None):
            return Transcription("我喜欢种田游戏", _usage())

    class ChatModel:
        def __init__(self):
            self.requests = []

        async def generate(self, request):
            self.requests.append(request)
            return ModelResponse(text="知道啦", provider="test", model="voice", usage=_usage())

    class SummaryModel:
        async def generate(self, request):
            return ModelResponse(text=json.dumps({
                "topic": "游戏偏好",
                "event_summary": "用户在语音中聊到种田游戏",
                "facts": [],
                "preferences": ["用户喜欢种田游戏"],
            }), provider="test", model="summary", usage=_usage())

    conversations = ConversationStore(tmp_path / "conversations.sqlite3")
    memories = MemoryStore(tmp_path / "memory.sqlite3")
    recorder = VoiceHistory(conversations, MemorySummaryExtractor(SummaryModel(), memories))
    model = ChatModel()
    session = VoiceSession(
        Recognizer(), model, UsageStore(tmp_path / "usage.sqlite3"),
        tts=False, history=recorder,
    )

    async def run():
        for _ in range(8):
            await session.turn(b"audio", filename="voice.wav", media_type="audio/wav")
            await session.flush_memory()

    asyncio.run(run())
    assert conversations.message_count("voice-main") == 16
    assert {record.source_ref for record in memories.list()} == {
        "voice:voice-main:messages:1-16"
    }

    restarted_model = ChatModel()
    restarted = VoiceSession(
        Recognizer(), restarted_model, UsageStore(tmp_path / "usage.sqlite3"),
        tts=False, history=VoiceHistory(conversations),
    )
    asyncio.run(restarted.turn(b"audio", filename="voice.wav", media_type="audio/wav"))
    assert len(restarted_model.requests[0].messages) == 13
    assert restarted_model.requests[0].messages[0].content == "我喜欢种田游戏"
    for record in memories.list():
        memories.delete(record.id)
    reopened = VoiceHistory(conversations, MemorySummaryExtractor(SummaryModel(), memories))
    asyncio.run(reopened.flush())
    assert memories.list() == []  # Deleting a memory cannot trigger regeneration on restart.
    restarted.clear_history()
    assert conversations.message_count("voice-main") == 0
    assert restarted._history == []


def test_memory_source_reads_exact_voice_window_after_long_history(tmp_path):
    conversations = ConversationStore(tmp_path / "conversations.sqlite3")
    conversations.ensure("voice-main")
    for index in range(60):
        conversations.append_pair("voice-main", f"语音第 {index} 轮", f"回复第 {index} 轮")
    memories = MemoryStore(tmp_path / "memory.sqlite3")
    record = memories.create(MemoryCreate(
        kind=MemoryKind.EVENT,
        content="用户起初聊到种田游戏",
        source_type=MemorySource.CONVERSATION,
        source_ref="voice:voice-main:messages:1-16",
    ))
    api.app.dependency_overrides[api.get_memory_store] = lambda: memories
    api.app.dependency_overrides[api.get_conversation_service] = lambda: SimpleNamespace(
        store=conversations
    )
    try:
        response = authorized_client(api.app).get(f"/api/memory/{record.id}/source")
        assert response.status_code == 200
        messages = response.json()["messages"]
        assert len(messages) == 16
        assert messages[0]["content"] == "语音第 0 轮"
        assert messages[-1]["content"] == "回复第 7 轮"
    finally:
        api.app.dependency_overrides.clear()


def test_failed_voice_summary_retries_after_restart(tmp_path):
    class FlakyModel:
        def __init__(self, fail):
            self.fail = fail

        async def generate(self, request):
            if self.fail:
                raise ModelProviderError("暂时不可用")
            return ModelResponse(
                text=json.dumps({
                    "topic": "游戏", "event_summary": "用户聊到游戏", "facts": [],
                    "preferences": [],
                }), provider="test", model="summary", usage=_usage(),
            )

    conversations = ConversationStore(tmp_path / "conversations.sqlite3")
    memories = MemoryStore(tmp_path / "memory.sqlite3")
    failed = VoiceHistory(conversations, MemorySummaryExtractor(FlakyModel(True), memories))
    for index in range(8):
        failed.record(f"问题 {index}", f"回复 {index}")
    asyncio.run(failed.flush())
    assert not conversations.summary_window_processed("voice-main", 16)
    assert memories.list() == []

    restored = VoiceHistory(conversations, MemorySummaryExtractor(FlakyModel(False), memories))
    asyncio.run(restored.flush())
    assert conversations.summary_window_processed("voice-main", 16)
    assert [item.content for item in memories.list()] == ["用户聊到游戏"]
