"""Metadata and opt-in generation diagnostics, tested without network access."""

import asyncio
import json
from datetime import datetime

import httpx
import pytest

from ella_runtime.modules.models.diagnostics import probe_model
from ella_runtime.modules.models.diagnostics import run_model_test as run_generation
from ella_runtime.modules.models.provider import ModelProviderError
from ella_runtime.modules.models.settings import ProviderConfig


def config(model="chat-model", base_url="https://service.example/v1"):
    return ProviderConfig("test", model, base_url, "fake-secret")


def sse(data):
    return f"data: {json.dumps(data)}\n\n"


def assert_timing(result):
    assert isinstance(result["latency_ms"], int)
    assert result["latency_ms"] >= 0


def test_metadata_returns_sorted_unique_nonempty_model_ids_and_latency():
    def respond(http_request):
        assert http_request.method == "GET"
        assert http_request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [
            {"id": " z-model "}, {"id": "chat-model"}, {"id": "chat-model "},
            {"id": ""}, {"id": " "}, {"id": 17}, None, {"id": "a-model"},
        ]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await probe_model(config(), client=client)
        assert result["model_ids"] == ["a-model", "chat-model", "z-model"]
        assert result["model_count"] == 3
        assert result["model_found"] is True
        assert_timing(result)

    asyncio.run(run())


def test_metadata_limits_visible_ids_without_losing_full_count_or_selected_model():
    def respond(http_request):
        return httpx.Response(200, json={"data": [
            {"id": f"model-{index:04}"} for index in reversed(range(1600))
        ]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await probe_model(config("model-1599"), client=client)
        assert result["model_count"] == 1600
        assert result["model_found"] is True
        assert result["model_ids"] == [f"model-{index:04}" for index in range(1000)]

    asyncio.run(run())


def test_metadata_accepts_prefixed_gemini_ids():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda http_request: httpx.Response(200, json={"data": [{"id": "models/chat-model"}]})
        )) as client:
            result = await probe_model(config(), client=client)
        assert result["model_found"] is True
        assert result["model_ids"] == ["models/chat-model"]

    asyncio.run(run())


def test_metadata_does_not_follow_redirects_even_when_client_allows_them():
    hosts = []

    def respond(http_request):
        hosts.append(http_request.url.host)
        return httpx.Response(302, headers={"location": "https://other.example/models"})

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), follow_redirects=True
        ) as client:
            with pytest.raises(ValueError, match="HTTP 302"):
                await probe_model(config(), client=client)
        assert hosts == ["service.example"]

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["text", "stream"])
@pytest.mark.parametrize("base_url,field", [
    ("https://service.example/v1", "max_tokens"),
    ("https://api.openai.com/v1", "max_completion_tokens"),
])
def test_generation_calls_only_completions_with_short_prompt_and_bounded_output(mode, base_url, field):
    calls = []

    def respond(http_request):
        calls.append(http_request)
        assert http_request.method == "POST"
        assert http_request.url.path == "/v1/chat/completions"
        payload = json.loads(http_request.content)
        assert payload[field] == 128
        assert payload["messages"][-1] == {"role": "user", "content": "请只回复：连接成功"}
        assert len(payload["messages"]) == 2
        if mode == "stream":
            assert payload["stream"] is True
            body = sse({"choices": [{"delta": {"content": "连接"}}]})
            body += sse({"choices": [{"delta": {"content": "成功"}, "finish_reason": "stop"}]})
            body += sse({"model": "returned-model", "choices": [], "usage": {
                "prompt_tokens": 3, "completion_tokens": 4, "prompt_cache_hit_tokens": 2,
            }})
            return httpx.Response(200, text=body)
        assert payload["stream"] is False
        return httpx.Response(200, json={
            "model": "returned-model",
            "choices": [{"message": {"content": "连接成功"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "prompt_cache_hit_tokens": 2},
        })

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await run_generation(config(base_url=base_url), mode=mode, client=client)
        assert result["preview"] == "连接成功"
        assert result["provider"] == "test"
        assert result["model"] == "returned-model"
        assert result["usage"]["input_tokens"] == 3
        assert result["usage"]["output_tokens"] == 4
        assert result["usage"]["cache_read_tokens"] == 2
        assert result["usage"]["purpose"] == "chat"
        datetime.fromisoformat(result["usage"]["occurred_at"])
        assert_timing(result)
        assert len(calls) == 1

    asyncio.run(run())


def test_stream_generation_preserves_unreported_usage():
    async def run():
        body = sse({"choices": [{"delta": {"content": "hello"}}]}) + "data: [DONE]\n\n"
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda http_request: httpx.Response(200, text=body)
        )) as client:
            result = await run_generation(config(), mode="stream", client=client)
        assert result["preview"] == "hello"
        assert result["model"] == "chat-model"
        assert result["usage"]["input_tokens"] is None
        assert result["usage"]["output_tokens"] is None
        assert result["usage"]["cache_read_tokens"] is None

    asyncio.run(run())


def test_generation_preview_is_bounded():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda http_request: httpx.Response(200, json={
                "choices": [{"message": {"content": "x" * 700}}],
            })
        )) as client:
            result = await run_generation(config(), client=client)
        assert result["preview"] == "x" * 500

    asyncio.run(run())


@pytest.mark.parametrize("body", [
    sse({"choices": [{"delta": {"content": "partial"}}]}),
    sse({"error": {"message": "quota exceeded"}}),
    "data: [DONE]\n\n",
])
def test_stream_generation_uses_provider_failure_checks(body):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda http_request: httpx.Response(200, text=body)
        )) as client:
            with pytest.raises(ModelProviderError):
                await run_generation(config(), mode="stream", client=client)

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["text", "stream"])
def test_generation_error_is_sanitized_by_provider(mode):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda http_request: httpx.Response(401, json={
                "error": {"message": "rejected fake-secret"},
            })
        )) as client:
            with pytest.raises(ModelProviderError) as failure:
                await run_generation(config(), mode=mode, client=client)
        assert "fake-secret" not in str(failure.value)
        assert "HTTP 401" in str(failure.value)

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["text", "stream"])
def test_generation_redirects_fail_without_following_or_replaying(mode):
    calls = []

    def respond(http_request):
        calls.append(http_request.url.host)
        return httpx.Response(302, headers={"location": "https://other.example/completions"})

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), follow_redirects=True
        ) as client:
            with pytest.raises(ModelProviderError, match="HTTP 302"):
                await run_generation(config(), mode=mode, client=client)
        assert calls == ["service.example"]

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["invalid", ""])
def test_invalid_generation_mode_does_not_contact_provider(mode):
    async def run():
        def fail_if_called(http_request):
            raise AssertionError("Invalid mode must not issue a request")

        async with httpx.AsyncClient(transport=httpx.MockTransport(fail_if_called)) as client:
            with pytest.raises(ValueError, match="测试模式"):
                await run_generation(config(), mode=mode, client=client)

    asyncio.run(run())


def test_unconfigured_generation_does_not_contact_provider():
    async def run():
        def fail_if_called(http_request):
            raise AssertionError("Incomplete config must not issue a request")

        async with httpx.AsyncClient(transport=httpx.MockTransport(fail_if_called)) as client:
            with pytest.raises(ValueError, match="未配齐"):
                await run_generation(ProviderConfig("test", "", ""), client=client)

    asyncio.run(run())
