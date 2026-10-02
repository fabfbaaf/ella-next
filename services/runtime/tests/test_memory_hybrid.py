import asyncio
import json
from datetime import UTC, datetime

import httpx

from ella_runtime.modules.memory.contracts import (
    MemoryCorrection,
    MemoryCreate,
    MemoryKind,
    MemorySource,
)
from ella_runtime.modules.memory.retrieval import (
    EmbeddingProvider,
    EmbeddingSettings,
    HybridMemoryRetriever,
)
from ella_runtime.modules.memory.store import MemoryStore
from ella_runtime.modules.memory.summary import MemorySummaryExtractor
from ella_runtime.modules.models.contracts import (
    ModelMessage,
    ModelPurpose,
    ModelResponse,
    TokenUsage,
)


def test_fts_and_vectors_follow_correction_and_deletion(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    record = store.create(MemoryCreate(kind=MemoryKind.FACT, content="用户在星露谷养鸡"))
    assert store.search("星露谷养鸡")[0].id == record.id
    store.set_embedding(record.id, model="small", vector=[1.0, 0.0])
    assert store.search_hybrid(
        "农场动物", query_vector=[1.0, 0.0], embedding_model="small"
    )[0].id == record.id

    store.correct(record.id, MemoryCorrection(content="用户在我的世界养狼", reason="用户更正"))
    assert store.search("星露谷养鸡") == []
    assert store.missing_embeddings("small")[0].id == record.id
    assert store.search("我的世界养狼")[0].id == record.id

    store.delete(record.id)
    assert store.search("我的世界养狼") == []
    assert store.missing_embeddings("small") == []


def test_embedding_provider_indexes_and_recalls_semantically(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    record = store.create(MemoryCreate(kind=MemoryKind.PREFERENCE, content="用户喜欢蓝色"))

    def respond(request: httpx.Request) -> httpx.Response:
        texts = json.loads(request.content)["input"]
        vectors = [[1.0, 0.0] for _ in texts]
        return httpx.Response(
            200,
            json={"data": [
                {"index": index, "embedding": vector}
                for index, vector in enumerate(vectors)
            ]},
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            retriever = HybridMemoryRetriever(
                store,
                EmbeddingProvider(EmbeddingSettings("http://127.0.0.1:1234/v1", "small"), client),
            )
            await retriever.index_missing()
            matches = await retriever.search_async("最喜欢什么颜色")
            assert matches[0].id == record.id
            await retriever.aclose()

    asyncio.run(run())


def test_summary_keeps_event_and_independent_fact_with_source(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")

    class Model:
        async def generate(self, request):
            assert request.purpose == ModelPurpose.ACTION
            return ModelResponse(
                text=json.dumps({
                    "topic": "周末游戏",
                    "event_summary": "周末开始玩星露谷，建了一个农场",
                    "facts": ["用户建了一个农场"],
                    "preferences": ["用户喜欢种田游戏"],
                }),
                provider="test",
                model="extractor",
                usage=TokenUsage(
                    provider="test", model="extractor", purpose=ModelPurpose.ACTION,
                    occurred_at=datetime.now(UTC),
                ),
            )

    extractor = MemorySummaryExtractor(Model(), store)
    messages = [ModelMessage(role="user", content="周末我玩了星露谷，建了农场")]

    async def run():
        assert await extractor.extract(messages, source_ref="chat:one:messages:1-16") == 3
        assert await extractor.extract(messages, source_ref="chat:one:messages:1-16") == 0

    asyncio.run(run())
    records = store.list()
    assert {item.kind for item in records} == {
        MemoryKind.EVENT, MemoryKind.FACT, MemoryKind.PREFERENCE,
    }
    assert all(item.source_type == MemorySource.CONVERSATION for item in records)
    assert all(item.source_ref == "chat:one:messages:1-16" for item in records)


def test_embedding_outage_falls_back_to_text_without_repeated_calls(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.create(MemoryCreate(kind=MemoryKind.FACT, content="用户喜欢蓝色"))
    calls = 0

    def fail(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
            retriever = HybridMemoryRetriever(
                store,
                EmbeddingProvider(EmbeddingSettings("http://127.0.0.1:1234/v1", "small"), client),
            )
            assert (await retriever.search_async("喜欢蓝色"))[0].content == "用户喜欢蓝色"
            assert (await retriever.search_async("喜欢蓝色"))[0].content == "用户喜欢蓝色"

    asyncio.run(run())
    assert calls == 1
