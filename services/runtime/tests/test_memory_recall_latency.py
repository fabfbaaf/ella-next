import asyncio

from ella_runtime.modules.memory.contracts import MemoryCreate, MemoryKind
from ella_runtime.modules.memory.retrieval import EmbeddingSettings, HybridMemoryRetriever
from ella_runtime.modules.memory.store import MemoryStore


class Provider:
    settings = EmbeddingSettings("http://127.0.0.1:1234/v1", "small")


def test_slow_query_falls_back_quickly_and_cancels_request(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    record = store.create(MemoryCreate(kind=MemoryKind.FACT, content="用户喜欢蓝色"))

    async def run():
        calls, cancelled = [], []

        class Slow(Provider):
            async def embed(self, texts):
                calls.append(texts)
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.append(True)

        retriever = HybridMemoryRetriever(store, Slow(), query_timeout=0.02)
        matches = await asyncio.wait_for(retriever.search_async("喜欢蓝色"), 0.5)
        assert matches[0].id == record.id
        assert (await retriever.search_async("喜欢蓝色"))[0].id == record.id
        assert len(calls) == 1 and cancelled == [True]
        assert retriever._index_task is None
        await retriever.aclose()

    asyncio.run(run())


def test_background_index_does_not_block_next_turn_and_shutdown_cancels_it(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    record = store.create(MemoryCreate(kind=MemoryKind.FACT, content="用户喜欢蓝色"))

    async def run():
        started, cancelled = asyncio.Event(), []
        indexes = []

        class SlowIndex(Provider):
            async def embed(self, texts):
                if texts == [record.content]:
                    indexes.append(texts)
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.append(True)
                return [[1.0, 0.0] for _ in texts]

        retriever = HybridMemoryRetriever(store, SlowIndex())
        assert (await retriever.search_async("喜欢蓝色"))[0].id == record.id
        await asyncio.wait_for(started.wait(), 0.5)
        for _ in range(3):
            matches = await asyncio.wait_for(retriever.search_async("喜欢蓝色"), 0.5)
            assert matches[0].id == record.id
        assert len(indexes) == 1
        task = retriever._index_task
        await asyncio.wait_for(retriever.aclose(), 0.5)
        assert task.done() and cancelled == [True] and retriever._index_task is None
        assert store.missing_embeddings("small")[0].id == record.id
        await retriever.search_async("喜欢蓝色")
        assert retriever._index_task is None

    asyncio.run(run())


def test_completed_background_index_is_available_on_following_turn(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    record = store.create(MemoryCreate(kind=MemoryKind.PREFERENCE, content="用户喜欢蓝色"))

    async def run():
        class Fast(Provider):
            async def embed(self, texts):
                return [[1.0, 0.0] for _ in texts]

        retriever = HybridMemoryRetriever(store, Fast())
        await retriever.search_async("喜欢蓝色")
        task = retriever._index_task
        await task
        matches = await retriever.search_async("最喜欢什么颜色")
        assert matches[0].id == record.id
        await retriever.aclose()

    asyncio.run(run())
