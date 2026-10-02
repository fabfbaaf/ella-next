from datetime import datetime

import pytest

from ella_runtime.api import app, get_model_config_store, get_usage_store
from ella_runtime.modules.models import diagnostics
from ella_runtime.modules.models.contracts import TokenUsage
from ella_runtime.modules.models.provider import ModelProviderError
from ella_runtime.modules.models.settings import ModelSettings, ProviderConfig
from ella_runtime.modules.models.usage_store import UsageStore
from tests.api_client import authorized_client


@pytest.fixture
def model_api(tmp_path):
    chat = ProviderConfig("local", "fixture", "http://localhost:11434/v1")
    class Store:
        def settings(self):
            return ModelSettings(chat=chat)
    usage = UsageStore(tmp_path / "usage.sqlite3")
    app.dependency_overrides[get_model_config_store] = Store
    app.dependency_overrides[get_usage_store] = lambda: usage
    try:
        yield authorized_client(app), chat, usage
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("slot", ["action", "vision"])
def test_probe_uses_same_inherited_route_as_gateway(model_api, monkeypatch, slot):
    client, chat, usage = model_api
    async def probe(config):
        assert config is chat
        return {"reachable": True, "model_ids": ["fixture"]}
    monkeypatch.setattr(diagnostics, "probe_model", probe)
    response = client.post(f"/api/models/{slot}/probe")
    assert response.status_code == 200
    assert response.json()["effective_slot"] == "chat"
    assert usage.summary()["totals"]["requests"] == 0


@pytest.mark.parametrize("mode", ["text", "stream"])
@pytest.mark.parametrize("slot", ["chat", "vision", "action"])
def test_explicit_generation_records_usage_and_uses_saved_route(model_api, monkeypatch, slot, mode):
    client, chat, usage = model_api
    async def run(config, *, mode):
        assert config is chat
        return {"preview": "连接成功", "provider": config.name, "model": config.model, "latency_ms": 1,
                "usage": TokenUsage(provider=config.name, model=config.model, purpose="chat", occurred_at=datetime.now().astimezone(), input_tokens=10, output_tokens=3).model_dump(mode="json")}
    monkeypatch.setattr(diagnostics, "run_model_test", run)
    response = client.post(f"/api/models/{slot}/test?mode={mode}")
    assert response.status_code == 200
    result = response.json()
    assert result["mode"] == mode and result["effective_slot"] == "chat"
    assert result["usage"]["purpose"] == ("action" if slot == "action" else "chat")
    assert usage.summary()["totals"]["reported_requests"] == 1


def test_generation_failure_is_actionable_and_not_success(model_api, monkeypatch):
    client, _, usage = model_api
    async def run(config, *, mode):
        raise ModelProviderError("gemini 请求失败：HTTP 400：模型不可用")
    monkeypatch.setattr(diagnostics, "run_model_test", run)
    response = client.post("/api/models/chat/test?mode=text")
    assert response.status_code == 502
    assert "HTTP 400" in response.json()["detail"]
    assert usage.summary()["totals"]["requests"] == 0


def test_unset_persona_and_invalid_modes_do_not_call_provider(model_api, monkeypatch):
    client, _, usage = model_api
    async def run(*args, **kwargs):
        raise AssertionError("Provider must not be called")
    monkeypatch.setattr(diagnostics, "run_model_test", run)
    assert client.post("/api/models/persona/test").status_code == 502
    assert client.post("/api/models/chat/test?mode=vision").status_code == 422
    assert usage.summary()["totals"]["requests"] == 0


def test_generation_test_timeout_returns_clear_failure(model_api, monkeypatch):
    client, _, usage = model_api
    async def run(config, *, mode):
        raise TimeoutError
    monkeypatch.setattr(diagnostics, "run_model_test", run)
    response = client.post("/api/models/chat/test")
    assert response.status_code == 502 and "30 秒" in response.json()["detail"]
    assert usage.summary()["totals"]["requests"] == 0
