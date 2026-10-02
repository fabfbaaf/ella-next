import asyncio
import json

import httpx

from ella_runtime.api import app, get_memory_store
from ella_runtime.modules.memory.contracts import (
    MemoryCorrection,
    MemoryCreate,
    MemoryKind,
    MemorySource,
)
from ella_runtime.modules.memory.store import MemoryStore
from ella_runtime.modules.models.contracts import ModelMessage, ModelPurpose, ModelRequest
from ella_runtime.modules.models.gateway import ModelGateway
from ella_runtime.modules.models.provider import OpenAICompatibleProvider
from ella_runtime.modules.models.settings import ModelSettings, ProviderConfig
from ella_runtime.modules.models.usage_store import UsageStore
from tests.api_client import authorized_client


def test_memory_survives_restart_and_correction_preserves_provenance(tmp_path):
    path = tmp_path / "memory.sqlite3"
    store = MemoryStore(path)
    record = store.create(
        MemoryCreate(
            kind=MemoryKind.PREFERENCE,
            content="用户喜欢星露谷",
            source_type=MemorySource.CONVERSATION,
            source_ref="message-42",
            confidence=0.8,
            tags=["星露谷", "游戏"],
        )
    )
    reopened = MemoryStore(path)
    assert reopened.get(record.id) is not None
    assert reopened.search("还记得我喜欢星露谷吗")[0].id == record.id

    corrected = reopened.correct(
        record.id,
        MemoryCorrection(content="用户最近更喜欢我的世界", reason="用户亲自更正"),
    )
    assert corrected.content == "用户最近更喜欢我的世界"
    assert corrected.source_type == MemorySource.MANUAL
    exported = reopened.export()
    assert exported["revisions"][0]["old_content"] == "用户喜欢星露谷"
    assert exported["revisions"][0]["old_source_ref"] == "message-42"

    assert reopened.delete(record.id)
    assert MemoryStore(path).get(record.id) is None
    assert MemoryStore(path).search("我的世界") == []
    assert MemoryStore(path).export() == {"version": 1, "items": [], "revisions": []}


def test_memory_api_create_search_export_and_delete(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    app.dependency_overrides[get_memory_store] = lambda: store
    try:
        client = authorized_client(app)
        invalid = client.post(
            "/api/memory",
            json={"kind": "fact", "content": "住在上海", "source_type": "conversation"},
        )
        assert invalid.status_code == 422

        created = client.post(
            "/api/memory",
            json={"kind": "fact", "content": "用户喜欢做表格", "tags": ["Office"]},
            headers={"Origin": "http://127.0.0.1:1421"},
        )
        assert created.status_code == 201
        assert created.headers["access-control-allow-origin"] == "http://127.0.0.1:1421"
        identity = created.json()["id"]
        assert client.get("/api/memory", params={"query": "表格"}).json()[0]["id"] == identity

        corrected = client.patch(
            f"/api/memory/{identity}",
            json={"content": "用户常用 Excel 做表格", "reason": "明确了应用"},
        )
        assert corrected.status_code == 200
        assert len(client.get("/api/memory/export").json()["revisions"]) == 1

        assert client.delete(f"/api/memory/{identity}").status_code == 204
        assert client.get("/api/memory/export").json()["items"] == []
        assert client.get("/api/memory/export").json()["revisions"] == []
        assert client.delete(f"/api/memory/{identity}").status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_model_recall_stops_using_deleted_memory(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    item = store.create(
        MemoryCreate(kind=MemoryKind.PREFERENCE, content="用户喜欢星露谷", tags=["星露谷"])
    )
    sent: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "收到"}}]})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            gateway = ModelGateway(
                ModelSettings(chat=ProviderConfig("ollama", "local", "http://127.0.0.1:11434/v1")),
                UsageStore(tmp_path / "usage.sqlite3"),
                OpenAICompatibleProvider(client),
                memory_retriever=store,
            )
            request = ModelRequest(
                purpose=ModelPurpose.CHAT,
                messages=[ModelMessage(role="user", content="还记得我喜欢星露谷吗？")],
            )
            await gateway.generate(request)
            assert store.delete(item.id)
            await gateway.generate(request)

    asyncio.run(run())
    assert "用户喜欢星露谷" in sent[0]["messages"][0]["content"]
    assert "用户喜欢星露谷" not in sent[1]["messages"][0]["content"]
