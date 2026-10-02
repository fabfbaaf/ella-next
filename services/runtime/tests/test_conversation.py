import asyncio
import json
import sqlite3
from datetime import UTC, datetime

import pytest

import ella_runtime.api as runtime_api
from ella_runtime.api import app, get_companion_store, get_conversation_service
from ella_runtime.modules.companion.store import CompanionStore
from ella_runtime.modules.memory.store import MemoryStore
from ella_runtime.modules.memory.summary import MemorySummaryExtractor
from ella_runtime.modules.models.contracts import ModelPurpose, ModelResponse, TokenUsage
from ella_runtime.modules.models.conversations import ConversationService, ConversationStore
from tests.api_client import authorized_client


class Gateway:
    def __init__(self):
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        return ModelResponse(
            text=f"回复 {len(self.requests)}",
            provider="test",
            model="chat",
            usage=TokenUsage(
                provider="test",
                model="chat",
                purpose=ModelPurpose.CHAT,
                occurred_at=datetime.now(UTC),
            ),
        )


def test_conversation_persists_context_and_can_be_deleted(tmp_path):
    path = tmp_path / "conversations.sqlite3"
    gateway = Gateway()
    service = ConversationService(gateway, ConversationStore(path))

    async def run():
        identity, first = await service.reply("你好")
        assert first == "回复 1"
        same, second = await service.reply("继续", identity)
        assert same == identity
        assert second == "回复 2"
        return identity

    identity = asyncio.run(run())
    assert gateway.requests[0].purpose == ModelPurpose.CHAT
    assert [m.content for m in gateway.requests[1].messages] == ["你好", "回复 1", "继续"]
    reopened = ConversationStore(path)
    assert [m.content for m in reopened.history(identity)] == ["你好", "回复 1", "继续", "回复 2"]
    assert reopened.delete(identity)
    assert not reopened.exists(identity)


def test_chat_api_reuses_conversation_and_exposes_delete(tmp_path, monkeypatch):
    service = ConversationService(Gateway(), ConversationStore(tmp_path / "chat.sqlite3"))
    memory = MemoryStore(tmp_path / "memory.sqlite3")
    monkeypatch.setattr(runtime_api, "get_memory_store", lambda: memory)
    app.dependency_overrides[get_conversation_service] = lambda: service
    app.dependency_overrides[get_companion_store] = lambda: CompanionStore(
        tmp_path / "companion.sqlite3"
    )
    try:
        client = authorized_client(app)
        first = client.post("/api/chat", json={"message": "你好"})
        assert first.status_code == 200
        identity = first.json()["conversation_id"]
        second = client.post("/api/chat", json={"message": "继续", "conversation_id": identity})
        assert second.status_code == 200
        assert second.json()["conversation_id"] == identity
        remembered = client.post(
            "/api/chat", json={"message": "记住：我的猫叫团子", "conversation_id": identity}
        )
        assert remembered.json()["memory_saved"] is True
        assert memory.list()[0].content == "我的猫叫团子"
        assert len(client.get(f"/api/chat/{identity}").json()["messages"]) == 6
        assert client.delete(f"/api/chat/{identity}").status_code == 204
        assert client.get(f"/api/chat/{identity}").status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_existing_conversations_migrate_and_voice_stays_out_of_chat_list(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE conversations (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO conversations VALUES ('old-chat', '2026-01-01', '2026-01-01')"
        )
        connection.execute(
            "INSERT INTO conversations VALUES ('voice-main', '2026-01-01', '2026-01-01')"
        )
    store = ConversationStore(path)
    assert [item["id"] for item in store.list_chats()] == ["old-chat"]
    assert store.chat_metadata("old-chat")["archived_at"] is None
    assert ConversationStore(path).exists("voice-main")


def test_conversation_list_new_archive_restore_and_missing_ids(tmp_path):
    store = ConversationStore(tmp_path / "chat.sqlite3")
    service = ConversationService(Gateway(), store)
    app.dependency_overrides[get_conversation_service] = lambda: service
    try:
        client = authorized_client(app)
        created = client.post("/api/conversations")
        assert created.status_code == 201
        identity = created.json()["id"]
        assert created.json()["preview"] == "新对话"
        store.append_pair(identity, "讨论项目进度", "好的")
        listed = client.get("/api/conversations").json()
        assert listed[0]["id"] == identity
        assert listed[0]["preview"] == "讨论项目进度"
        assert listed[0]["message_count"] == 2
        archived = client.patch(
            f"/api/conversations/{identity}/archive", json={"archived": True}
        )
        assert archived.status_code == 200
        assert archived.json()["archived_at"] is not None
        with pytest.raises(ValueError, match="已归档"):
            store.append_pair(identity, "不该写入", "不该写入")
        assert client.post(
            "/api/chat", json={"message": "继续", "conversation_id": identity}
        ).status_code == 422
        restored = client.patch(
            f"/api/conversations/{identity}/archive", json={"archived": False}
        )
        assert restored.json()["archived_at"] is None
        assert client.patch(
            "/api/conversations/missing/archive", json={"archived": True}
        ).status_code == 404
    finally:
        app.dependency_overrides.clear()



def _model_response(text: str) -> ModelResponse:
    return ModelResponse(
        text=text, provider="test", model="chat",
        usage=TokenUsage(
            provider="test", model="chat", purpose=ModelPurpose.CHAT,
            occurred_at=datetime.now(UTC),
        ),
    )


def test_different_conversations_reply_concurrently_without_retaining_locks(tmp_path):
    class PausedGateway:
        def __init__(self):
            self.requests = []
            self.both_started = asyncio.Event()
            self.release = asyncio.Event()

        async def generate(self, request):
            self.requests.append(request)
            if len(self.requests) == 2:
                self.both_started.set()
            await self.release.wait()
            return _model_response("done")

    async def run():
        store = ConversationStore(tmp_path / "chat.sqlite3")
        gateway = PausedGateway()
        service = ConversationService(gateway, store)
        first_id, second_id = store.create(), store.create()
        first = asyncio.create_task(service.reply("first", first_id))
        second = asyncio.create_task(service.reply("second", second_id))
        try:
            await asyncio.wait_for(gateway.both_started.wait(), timeout=1)
        finally:
            gateway.release.set()
        assert {item.task_id for item in gateway.requests} == {first_id, second_id}
        await asyncio.gather(first, second)
        assert len(service._locks) == 0
        await service.aclose()

    asyncio.run(run())


def test_same_conversation_still_serializes_replies(tmp_path):
    class FirstPausedGateway:
        def __init__(self):
            self.requests = []
            self.first_started = asyncio.Event()
            self.release = asyncio.Event()

        async def generate(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                self.first_started.set()
                await self.release.wait()
            return _model_response(f"answer {len(self.requests)}")

    async def run():
        store = ConversationStore(tmp_path / "chat.sqlite3")
        identity = store.create()
        gateway = FirstPausedGateway()
        service = ConversationService(gateway, store)
        first = asyncio.create_task(service.reply("first", identity))
        await asyncio.wait_for(gateway.first_started.wait(), timeout=1)
        second = asyncio.create_task(service.reply("second", identity))
        await asyncio.sleep(0)
        assert len(gateway.requests) == 1
        gateway.release.set()
        await asyncio.gather(first, second)
        assert [message.content for message in gateway.requests[1].messages] == [
            "first", "answer 1", "second",
        ]
        await service.aclose()

    asyncio.run(run())


def test_blocked_summary_does_not_delay_chat_or_later_reply(tmp_path):
    class BlockingSummarizer:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = []

        async def extract(self, messages, *, source_ref):
            self.calls.append((messages, source_ref))
            self.started.set()
            await self.release.wait()
            return 0

    async def run():
        store = ConversationStore(tmp_path / "chat.sqlite3")
        identity = store.create()
        summarizer = BlockingSummarizer()
        service = ConversationService(Gateway(), store, summarizer)
        for index in range(7):
            await service.reply(f"turn {index}", identity)
        _, eighth = await asyncio.wait_for(service.reply("turn 7", identity), timeout=1)
        assert eighth == "回复 8"
        await asyncio.wait_for(summarizer.started.wait(), timeout=1)
        assert not store.summary_window_processed(identity, 16)
        await asyncio.wait_for(service.reply("turn 8", identity), timeout=1)
        summarizer.release.set()
        await service.drain()
        assert store.summary_window_processed(identity, 16)
        messages, source_ref = summarizer.calls[0]
        assert source_ref == f"chat:{identity}:messages:1-16"
        assert [item.content for item in messages] == [
            item.content for item in store.history_window(identity, 1, 16)
        ]
        assert len(summarizer.calls) == 1
        await service.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["none", "exception"])
def test_failed_summary_retries_exact_window_after_restart(tmp_path, failure, caplog):
    class Summarizer:
        def __init__(self, should_fail):
            self.should_fail = should_fail
            self.calls = []

        async def extract(self, messages, *, source_ref):
            self.calls.append((messages, source_ref))
            if self.should_fail:
                if failure == "exception":
                    raise RuntimeError("summary unavailable")
                return None
            return 0

    async def run():
        store = ConversationStore(tmp_path / "chat.sqlite3")
        identity = store.create()
        failed = Summarizer(True)
        service = ConversationService(Gateway(), store, failed)
        for index in range(8):
            await service.reply(f"turn {index}", identity)
        await service.drain()
        assert not store.summary_window_processed(identity, 16)
        assert len(failed.calls) == 1
        await service.aclose()

        restored = Summarizer(False)
        restarted = ConversationService(Gateway(), ConversationStore(store.path), restored)
        await restarted.reply("turn 8", identity)
        await restarted.drain()
        assert store.summary_window_processed(identity, 16)
        assert len(restored.calls) == 1
        messages, source_ref = restored.calls[0]
        assert source_ref == f"chat:{identity}:messages:1-16"
        assert len(messages) == 16
        assert messages[0].content == "turn 0"
        assert messages[-1].role == "assistant"
        await restarted.aclose()

    asyncio.run(run())
    assert "will retry later" in caplog.text


@pytest.mark.parametrize("via_service", [True, False])
def test_deleting_chat_cancels_inflight_summary_before_memory_write(tmp_path, via_service):
    class BlockedSummaryModel:
        def __init__(self):
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.release = asyncio.Event()

        async def generate(self, request):
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
            return _model_response(json.dumps({
                "topic": "private", "event_summary": "should not be saved",
                "facts": [], "preferences": [],
            }))

    async def run():
        store = ConversationStore(tmp_path / "chat.sqlite3")
        memory = MemoryStore(tmp_path / "memory.sqlite3")
        identity = store.create()
        model = BlockedSummaryModel()
        service = ConversationService(
            Gateway(), store, MemorySummaryExtractor(model, memory)
        )
        for index in range(8):
            await service.reply(f"turn {index}", identity)
        await asyncio.wait_for(model.started.wait(), timeout=1)
        if via_service:
            assert await service.delete(identity)
        else:
            assert store.delete(identity)  # Current API still calls the store directly.
        await asyncio.wait_for(model.cancelled.wait(), timeout=1)
        model.release.set()
        await service.drain()
        assert not store.exists(identity)
        assert memory.list() == []
        await service.aclose()

    asyncio.run(run())
