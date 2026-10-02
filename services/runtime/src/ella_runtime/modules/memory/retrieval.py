"""Optional OpenAI-compatible embeddings and hybrid memory recall."""

from __future__ import annotations

import asyncio
import math
import os
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from ella_runtime.modules.memory.contracts import MemoryRecord
from ella_runtime.modules.memory.store import MemoryStore


class EmbeddingError(RuntimeError):
    """An embedding service is unavailable or returned invalid vectors."""


@dataclass(frozen=True)
class EmbeddingSettings:
    base_url: str
    model: str
    api_key: str | None = None

    @classmethod
    def from_env(cls) -> EmbeddingSettings:
        return cls(
            base_url=os.getenv("ELLA_EMBEDDING_BASE_URL", "").strip(),
            model=os.getenv("ELLA_EMBEDDING_MODEL", "").strip(),
            api_key=os.getenv("ELLA_EMBEDDING_API_KEY") or None,
        )

    @property
    def enabled(self) -> bool:
        parsed = urlparse(self.base_url)
        return bool(
            self.model
            and parsed.scheme in {"http", "https"}
            and parsed.hostname
            and (parsed.hostname in {"127.0.0.1", "localhost", "::1"} or self.api_key)
        )


class EmbeddingProvider:
    def __init__(self, settings: EmbeddingSettings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.settings.enabled or not texts:
            raise EmbeddingError("语义向量模型尚未配置")
        headers = (
            {"Authorization": f"Bearer {self.settings.api_key}"}
            if self.settings.api_key else {}
        )
        try:
            if self.client is None:
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.post(
                        f"{self.settings.base_url.rstrip('/')}/embeddings",
                        headers=headers,
                        json={"model": self.settings.model, "input": texts},
                    )
            else:
                response = await self.client.post(
                    f"{self.settings.base_url.rstrip('/')}/embeddings",
                    headers=headers,
                    json={"model": self.settings.model, "input": texts},
                )
            response.raise_for_status()
            data = response.json()["data"]
            ordered = sorted(data, key=lambda item: item["index"])
            vectors = [item["embedding"] for item in ordered]
            if len(vectors) != len(texts):
                raise ValueError("向量数与输入不一致")
            if not all(
                isinstance(vector, list)
                and vector
                and len(vector) <= 8192
                and all(type(value) in {int, float} and math.isfinite(value) for value in vector)
                for vector in vectors
            ):
                raise ValueError("向量格式无效")
            return vectors
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            raise EmbeddingError("语义向量服务不可用或返回格式无效") from exc


class HybridMemoryRetriever:
    def __init__(
        self, store: MemoryStore, provider: EmbeddingProvider, *,
        query_timeout: float = 0.75, index_timeout: float = 30.0,
    ) -> None:
        self.store = store
        self.provider = provider
        self.query_timeout = query_timeout
        self.index_timeout = index_timeout
        self._retry_after = 0.0
        self._index_lock = asyncio.Lock()
        self._index_task: asyncio.Task[None] | None = None
        self._closed = False

    async def index_missing(self) -> None:
        """Explicit indexing is still available; conversation recall never waits on it."""
        if self._closed or not self.provider.settings.enabled:
            return
        async with self._index_lock:
            if self._closed or time.monotonic() < self._retry_after:
                return
            records = self.store.missing_embeddings(self.provider.settings.model, limit=20)
            if not records:
                return
            try:
                async with asyncio.timeout(self.index_timeout):
                    vectors = await self.provider.embed([record.content for record in records])
            except (EmbeddingError, TimeoutError):
                self._retry_after = time.monotonic() + 60
                return
            for record, vector in zip(records, vectors):
                if self._closed:
                    return
                try:
                    current = self.store.get(record.id)
                    if current is None or not current.active or current.content != record.content:
                        continue
                    self.store.set_embedding(
                        record.id, model=self.provider.settings.model, vector=vector
                    )
                except KeyError:
                    continue

    def _schedule_index(self) -> None:
        if self._closed or time.monotonic() < self._retry_after:
            return
        if self._index_task is not None and not self._index_task.done():
            return
        self._index_task = asyncio.create_task(self.index_missing(), name="ella-memory-index")
        self._index_task.add_done_callback(self._index_finished)

    def _index_finished(self, task: asyncio.Task[None]) -> None:
        # Retrieve exceptions so background indexing never leaks task warnings.
        if not task.cancelled() and task.exception() is not None:
            self._retry_after = time.monotonic() + 60
        if self._index_task is task:
            self._index_task = None

    async def search_async(self, query: str, *, limit: int = 5) -> list[MemoryRecord]:
        if (
            self._closed or not self.provider.settings.enabled
            or time.monotonic() < self._retry_after
        ):
            return self.store.search(query, limit=limit)
        try:
            async with asyncio.timeout(self.query_timeout):
                vector = (await self.provider.embed([query]))[0]
        except (EmbeddingError, TimeoutError):
            self._retry_after = time.monotonic() + 60
            return self.store.search(query, limit=limit)
        # Missing records are indexed separately for subsequent turns. Existing
        # vectors and text search remain usable while that job is in flight.
        self._schedule_index()
        return self.store.search_hybrid(
            query,
            limit=limit,
            query_vector=vector,
            embedding_model=self.provider.settings.model,
        )

    async def aclose(self) -> None:
        self._closed = True
        task = self._index_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._index_task = None
