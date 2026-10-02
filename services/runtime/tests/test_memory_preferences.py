import asyncio
import json
import sqlite3
from datetime import UTC, datetime

from ella_runtime.modules.memory.capture import MemoryCapture
from ella_runtime.modules.memory.contracts import MemoryCorrection
from ella_runtime.modules.memory.retrieval import EmbeddingSettings, HybridMemoryRetriever
from ella_runtime.modules.memory.store import MemoryStore
from ella_runtime.modules.memory.summary import MemorySummaryExtractor
from ella_runtime.modules.models.contracts import (
    ModelMessage,
    ModelPurpose,
    ModelResponse,
    TokenUsage,
)


def test_negation_replaces_only_the_same_object_and_retains_provenance(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    capture = MemoryCapture(store)
    assert capture.capture("我喜欢蓝色", source_ref="chat:first")
    assert capture.capture("我喜欢红色", source_ref="chat:other")
    blue = next(item for item in store.list() if item.content == "我喜欢蓝色")
    red = next(item for item in store.list() if item.content == "我喜欢红色")
    store.set_embedding(blue.id, model="small", vector=[1.0, 0.0])
    assert capture.capture("我现在不喜欢蓝色了", source_ref="voice:changed")
    negative = next(item for item in store.list() if item.preference_negative)
    old = store.get(blue.id)
    assert old.active is False and old.superseded_by == negative.id
    assert old.content == "我喜欢蓝色" and old.source_ref == "chat:first"
    assert "明确更新偏好" in old.inactive_reason
    assert store.get(red.id).active is True
    assert blue.id not in {item.id for item in store.search("蓝色")}
    assert blue.id not in {item.id for item in store.search_hybrid(
        "最喜欢什么", query_vector=[1.0, 0.0], embedding_model="small",
    )}
    assert blue.id not in {item.id for item in store.missing_embeddings("other")}
    assert not capture.capture("我现在不喜欢蓝色了", source_ref="chat:duplicate")


def test_new_nickname_and_explicit_favorite_change_replace_their_own_topics(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    capture = MemoryCapture(store)
    capture.capture("以后叫我小王", source_ref="chat:name")
    name = store.list()[0]
    capture.capture("我最喜欢的颜色是蓝色", source_ref="chat:color")
    color = next(item for item in store.list() if item.topic == "最喜欢的颜色")
    capture.capture("别再叫我小王，叫我小李", source_ref="voice:name-change")
    assert store.get(name.id).active is False
    assert store.get(color.id).active is True
    capture.capture("我最喜欢的颜色改为红色", source_ref="chat:color-change")
    assert store.get(color.id).active is False
    active = store.list(active_only=True)
    assert {(item.topic, item.preference_value) for item in active} == {
        ("称呼", "小李"), ("最喜欢的颜色", "红色"),
    }
    assert all(item.source_ref for item in active)


def test_preferences_are_not_guessed_from_questions_or_new_unrelated_likes(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    capture = MemoryCapture(store)
    capture.capture("我喜欢星露谷", source_ref="chat:first")
    capture.capture("我喜欢我的世界", source_ref="chat:next")
    assert len(store.list(active_only=True)) == 2
    assert not capture.capture("我不喜欢星露谷吗？", source_ref="chat:question")
    assert not capture.capture("如果我不喜欢星露谷的话", source_ref="chat:hypothetical")
    assert all(item.active for item in store.list())


def test_inactive_memory_can_be_manually_corrected_exported_and_deleted(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    capture = MemoryCapture(store)
    capture.capture("我喜欢蓝色", source_ref="chat:first")
    old = store.list()[0]
    capture.capture("我不再喜欢蓝色", source_ref="chat:change")
    exported = store.export()
    exported_old = next(item for item in exported["items"] if item["id"] == old.id)
    assert exported_old["active"] is False and exported_old["source_ref"] == "chat:first"
    reopened = MemoryStore(store.path)
    assert reopened.get(old.id).active is False
    corrected = reopened.correct(old.id, MemoryCorrection(content="我喜欢浅蓝色", reason="用户修正"))
    assert corrected.active and corrected.superseded_by is None
    assert corrected.topic == "喜好:浅蓝色"
    assert reopened.export()["revisions"][0]["old_source_ref"] == "chat:first"
    assert reopened.delete(old.id)
    assert reopened.get(old.id) is None


def test_legacy_schema_migrates_without_guessing_which_preference_is_current(tmp_path):
    path = tmp_path / "memory.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TABLE memory_items (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL,
            source_type TEXT NOT NULL, source_ref TEXT, confidence REAL NOT NULL,
            tags_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        for identity, text in [("old", "用户喜欢蓝色"), ("new", "用户不喜欢蓝色")]:
            connection.execute("INSERT INTO memory_items VALUES (?,?,?,?,?,?,?,?,?)", (
                identity, "preference", text, "conversation", "chat:legacy", 0.8, "[]",
                "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00",
            ))
    store = MemoryStore(path)
    assert len(store.list()) == 2 and all(item.active for item in store.list())
    assert {item.topic for item in store.list()} == {"喜好:蓝色"}
    assert MemoryCapture(store).capture("我现在不喜欢蓝色了", source_ref="chat:explicit")
    assert store.get("old").active is False and store.get("new").active is False


def test_summary_does_not_resurrect_replaced_preference_or_use_assistant_change(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    capture = MemoryCapture(store)
    capture.capture("我喜欢蓝色", source_ref="chat:one")
    capture.capture("我现在不喜欢蓝色了", source_ref="voice:two")

    class Model:
        async def generate(self, request):
            return ModelResponse(
                text=json.dumps({"preferences": ["用户喜欢蓝色"]}, ensure_ascii=False),
                provider="mock", model="summary",
                usage=TokenUsage(provider="mock", model="summary", purpose=ModelPurpose.ACTION,
                                 occurred_at=datetime.now(UTC)),
            )

    async def run():
        summary = MemorySummaryExtractor(Model(), store)
        assert await summary.extract([
            ModelMessage(role="user", content="聊聊颜色"),
            ModelMessage(role="assistant", content="我喜欢蓝色"),
        ], source_ref="chat:summary:messages:1-16") == 0
        assert not any(item.content == "用户喜欢蓝色" for item in store.list())
        assert next(item for item in store.list(active_only=True)).preference_negative is True

    asyncio.run(run())


def test_raw_user_change_is_resolved_even_if_summary_model_is_unavailable(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    capture = MemoryCapture(store)
    capture.capture("我喜欢蓝色", source_ref="chat:one")
    old = store.list()[0]

    class Model:
        async def generate(self, request):
            # Invalid JSON is an ordinary extraction failure, not a real call.
            return ModelResponse(
                text="unavailable", provider="mock", model="summary",
                usage=TokenUsage(provider="mock", model="summary", purpose=ModelPurpose.ACTION,
                                 occurred_at=datetime.now(UTC)),
            )

    async def run():
        result = await MemorySummaryExtractor(Model(), store).extract([
            ModelMessage(role="user", content="我不再喜欢蓝色"),
        ], source_ref="chat:summary:messages:1-16")
        assert result == 1
        assert store.get(old.id).active is False

    asyncio.run(run())


def test_inflight_index_cannot_embed_a_preference_that_became_inactive(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    capture = MemoryCapture(store)
    capture.capture("我喜欢蓝色", source_ref="chat:first")
    old = store.list()[0]

    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        class Provider:
            settings = EmbeddingSettings("http://127.0.0.1:1234/v1", "small")

            async def embed(self, texts):
                started.set()
                await release.wait()
                return [[1.0, 0.0] for _ in texts]

        retriever = HybridMemoryRetriever(store, Provider())
        indexing = asyncio.create_task(retriever.index_missing())
        await started.wait()
        capture.capture("我不喜欢蓝色", source_ref="voice:changed")
        release.set()
        await indexing
        with sqlite3.connect(store.path) as connection:
            assert connection.execute(
                "SELECT 1 FROM memory_vectors WHERE memory_id=?", (old.id,),
            ).fetchone() is None
        await retriever.aclose()

    asyncio.run(run())


def test_exact_object_matching_normalizes_case_without_overwriting_other_preferences(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    capture = MemoryCapture(store)
    capture.capture("我喜欢Minecraft", source_ref="chat:old")
    old = store.list()[0]
    capture.capture("我喜欢Stardew", source_ref="chat:other")
    capture.capture("我不再喜欢MINECRAFT", source_ref="chat:change")
    assert store.get(old.id).active is False
    assert any(item.content == "我喜欢Stardew" for item in store.list(active_only=True))
