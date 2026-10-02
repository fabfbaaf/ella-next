import asyncio
import json
from datetime import UTC, date, datetime

import httpx

from ella_runtime.api import app, get_model_config_store, get_usage_store
from ella_runtime.modules.models.config_store import ModelConfigStore
from ella_runtime.modules.models.contracts import (
    ModelMessage,
    ModelPurpose,
    ModelRequest,
    TokenUsage,
)
from ella_runtime.modules.models.gateway import ModelGateway
from ella_runtime.modules.models.provider import OpenAICompatibleProvider
from ella_runtime.modules.models.settings import ModelSettings, ProviderConfig
from ella_runtime.modules.models.usage_store import UsageStore
from tests.api_client import authorized_client


def test_chat_and_action_share_persona_and_record_only_reported_tokens(tmp_path):
    sent: list[tuple[str, dict]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        sent.append((str(request.url), payload))
        if request.url.host == "127.0.0.1":
            return httpx.Response(
                200,
                json={
                    "model": "local-model",
                    "choices": [{"message": {"content": "你好"}}],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "prompt_tokens_details": {"cached_tokens": 2},
                    },
                },
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "任务已规划"}}]},
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            gateway = ModelGateway(
                ModelSettings(
                    chat=ProviderConfig("ollama", "local-model", "http://127.0.0.1:11434/v1"),
                    action=ProviderConfig(
                        "deepseek", "deepseek-flash", "https://api.deepseek.com", "test"
                    ),
                ),
                UsageStore(tmp_path / "usage.sqlite3"),
                OpenAICompatibleProvider(client),
            )
            chat = await gateway.generate(
                ModelRequest(
                    purpose=ModelPurpose.CHAT, messages=[ModelMessage(role="user", content="你好")]
                )
            )
            action = await gateway.generate(
                ModelRequest(
                    purpose=ModelPurpose.ACTION,
                    messages=[ModelMessage(role="user", content="整理文档")],
                    task_id="task-1",
                    instructions="先核对文件",
                )
            )
            assert chat.text == "你好"
            assert chat.usage.cache_read_tokens == 2
            assert action.usage.is_reported is False

    asyncio.run(run())
    assert sent[0][0].endswith("/v1/chat/completions")
    assert sent[1][0].endswith("/chat/completions")
    assert "你是艾拉" in sent[0][1]["messages"][0]["content"]
    assert "你是艾拉" in sent[1][1]["messages"][0]["content"]
    assert "先核对文件" in sent[1][1]["messages"][0]["content"]

    summary = UsageStore(tmp_path / "usage.sqlite3").summary()
    assert summary["totals"] == {
        "requests": 2,
        "reported_requests": 1,
        "unreported_requests": 1,
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 2,
    }
    assert (
        UsageStore(tmp_path / "usage.sqlite3").summary(task_id="task-1")["totals"]["requests"] == 1
    )


def test_action_uses_chat_model_when_optional_action_model_is_missing():
    chat = ProviderConfig("hf-local", "local-hf", "http://localhost:8000/v1")
    settings = ModelSettings(chat=chat)
    assert chat.configured
    assert settings.for_purpose("action") is chat
    assert settings.public_status()["action_uses_chat"] is True


def test_usage_api_filters_dates_and_does_not_expose_keys(tmp_path, monkeypatch):
    store = UsageStore(tmp_path / "usage.sqlite3")
    store.record(
        TokenUsage(
            provider="ollama",
            model="local-model",
            purpose=ModelPurpose.CHAT,
            task_id="one",
            occurred_at=datetime(2026, 9, 23, tzinfo=UTC),
            input_tokens=8,
            output_tokens=3,
        )
    )
    store.record(
        TokenUsage(
            provider="ollama",
            model="local-model",
            purpose=ModelPurpose.CHAT,
            task_id="two",
            occurred_at=datetime(2026, 9, 24, tzinfo=UTC),
        )
    )
    app.dependency_overrides[get_usage_store] = lambda: store
    app.dependency_overrides[get_model_config_store] = lambda: ModelConfigStore(
        tmp_path / "models.sqlite3"
    )
    try:
        client = authorized_client(app)
        response = client.get(
            "/api/usage/summary",
            params={"from_date": date(2026, 9, 24).isoformat()},
            headers={"Origin": "http://127.0.0.1:1421"},
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:1421"
        assert response.json()["totals"]["requests"] == 1
        assert response.json()["totals"]["unreported_requests"] == 1
        monkeypatch.setenv("ELLA_CHAT_MODEL", "local-model")
        monkeypatch.setenv("ELLA_CHAT_API_KEY", "private-test-key")
        status = client.get("/api/models/status")
        assert status.status_code == 200
        assert "private-test-key" not in status.text
    finally:
        app.dependency_overrides.clear()


def test_optional_persona_model_polishes_chat_but_not_action_plan(tmp_path):
    sent = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        sent.append((request.url.host, payload))
        if request.url.host == "gemini.example":
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "我陪你把这件事做好。"}}],
                    "usage": {"prompt_tokens": 6, "completion_tokens": 8},
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "初稿"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            gateway = ModelGateway(
                ModelSettings(
                    chat=ProviderConfig("ollama", "local", "http://127.0.0.1:11434/v1"),
                    action=ProviderConfig("deepseek", "action", "https://deepseek.example", "key"),
                    persona=ProviderConfig("gemini", "persona", "https://gemini.example", "key"),
                ),
                UsageStore(tmp_path / "usage.sqlite3"),
                OpenAICompatibleProvider(client),
            )
            chat = await gateway.generate(
                ModelRequest(
                    purpose=ModelPurpose.CHAT,
                    messages=[ModelMessage(role="user", content="帮我安排今天")],
                )
            )
            action = await gateway.generate(
                ModelRequest(
                    purpose=ModelPurpose.ACTION,
                    messages=[ModelMessage(role="user", content="写文件")],
                )
            )
            assert chat.text == "我陪你把这件事做好。"
            assert action.text == "初稿"

    asyncio.run(run())
    assert [host for host, _ in sent] == ["127.0.0.1", "gemini.example", "deepseek.example"]
    persona_input = json.loads(sent[1][1]["messages"][1]["content"])
    assert persona_input["draft_answer"] == "初稿"
    summary = UsageStore(tmp_path / "usage.sqlite3").summary()["totals"]
    assert summary["requests"] == 3
    assert summary["input_tokens"] == 12
