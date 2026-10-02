"""Offline regression checks for model transport failures and streamed usage."""

import asyncio
import json

import httpx
import pytest

from ella_runtime.modules.models.contracts import ModelPurpose, ModelRequest
from ella_runtime.modules.models.provider import ModelProviderError, OpenAICompatibleProvider
from ella_runtime.modules.models.settings import ProviderConfig


def config(key=None):
    return ProviderConfig("test", "test-model", "http://127.0.0.1:9000/v1", key)


def request():
    return ModelRequest(
        purpose=ModelPurpose.VOICE, messages=[{"role": "user", "content": "hello"}]
    )


def event(data):
    return f"data: {json.dumps(data)}\n\n"


def text_event(text="hello", **fields):
    return event({"choices": [{"delta": {"content": text}, **fields}]})


async def collect_stream(body, *, key=None, status=200):
    calls = []

    def respond(http_request):
        calls.append(http_request)
        return httpx.Response(status, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = [item async for item in OpenAICompatibleProvider(client).stream(
            config(key), request(), "persona"
        )]
    assert len(calls) == 1
    return result


@pytest.mark.parametrize("message", ["", " ", " \n\t"])
@pytest.mark.parametrize("mode", ["generate", "stream", "vision"])
def test_empty_http_error_message_remains_provider_error(message, mode):
    async def run():
        def respond(http_request):
            return httpx.Response(400, json={"error": {"message": message}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = OpenAICompatibleProvider(client)
            with pytest.raises(ModelProviderError, match="HTTP 400"):
                if mode == "stream":
                    _ = [item async for item in provider.stream(config(), request(), "persona")]
                elif mode == "vision":
                    await provider.generate_vision(config(), request(), "persona", b"image")
                else:
                    await provider.generate(config(), request(), "persona")

    asyncio.run(run())


@pytest.mark.parametrize("stream_error", [False, True])
def test_errors_redact_entire_key_before_clipping(stream_error):
    secret = "fake-secret-" + "z" * 50
    message = "x" * 220 + secret

    async def run():
        def respond(http_request):
            error = {"error": {"message": message}}
            if stream_error:
                return httpx.Response(200, text=event(error))
            return httpx.Response(400, json=error)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = OpenAICompatibleProvider(client)
            with pytest.raises(ModelProviderError) as failure:
                if stream_error:
                    _ = [item async for item in provider.stream(config(secret), request(), "persona")]
                else:
                    await provider.generate(config(secret), request(), "persona")
            assert secret not in str(failure.value)
            assert secret[:20] not in str(failure.value)
            assert "已隐藏密钥" in str(failure.value)

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["generate", "vision"])
def test_http_200_error_body_reports_upstream_failure(mode):
    async def run():
        def respond(http_request):
            return httpx.Response(200, json={"error": {"message": "upstream quota exceeded"}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = OpenAICompatibleProvider(client)
            with pytest.raises(ModelProviderError, match="upstream quota exceeded"):
                if mode == "vision":
                    await provider.generate_vision(config(), request(), "persona", b"image")
                else:
                    await provider.generate(config(), request(), "persona")

    asyncio.run(run())


@pytest.mark.parametrize("prefix", ["", text_event("partial")])
def test_sse_error_fails_without_replaying_already_started_stream(prefix):
    async def run():
        calls = []
        received = []

        def respond(http_request):
            calls.append(http_request)
            return httpx.Response(200, text=prefix + event({"error": {"message": "quota exceeded"}}))

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(ModelProviderError, match="quota exceeded"):
                async for kind, item in OpenAICompatibleProvider(client).stream(
                    config(), request(), "persona"
                ):
                    if kind == "text":
                        received.append(item)
        assert received == (["partial"] if prefix else [])
        assert len(calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("body", [
    "", ": keepalive\n\n", "data: [DONE]\n\n",
    event({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
    text_event("  ") + "data: [DONE]\n\n",
    '{"error":{"message":"gateway returned JSON"}}',
])
def test_empty_stream_or_json_response_is_not_success(body):
    async def run():
        with pytest.raises(ModelProviderError, match="未返回文本"):
            await collect_stream(body)

    asyncio.run(run())


def test_early_eof_after_text_is_not_success_or_retried():
    async def run():
        calls = []
        received = []

        def respond(http_request):
            calls.append(http_request)
            return httpx.Response(200, text=text_event("partial"))

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(ModelProviderError, match="提前结束"):
                async for kind, item in OpenAICompatibleProvider(client).stream(
                    config(), request(), "persona"
                ):
                    if kind == "text":
                        received.append(item)
        assert received == ["partial"]
        assert len(calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("body", [
    text_event("hello") + "data: [DONE]\n\n",
    text_event("hello", finish_reason="stop"),
    text_event("hello") + event({"choices": [{"finish_reason": "stop"}]}),
])
def test_done_or_finish_reason_completes_stream(body):
    result = asyncio.run(collect_stream(body))
    assert [item for kind, item in result if kind == "text"] == ["hello"]


def test_sse_comments_metadata_and_multiline_data_preserve_spaces():
    body = (
        ': ping\n\nevent: completion\nid: 1\n'
        'data: {"choices":\n'
        'data: [{"delta":{"content":"hello"}}]}\n\n'
        + text_event(" world", finish_reason="stop")
        + "data: [DONE]\n\n"
    )
    result = asyncio.run(collect_stream(body))
    assert "".join(item for kind, item in result if kind == "text") == "hello world"


@pytest.mark.parametrize("usage", [
    {"prompt_tokens": 3, "completion_tokens": 4, "prompt_cache_hit_tokens": 2},
    {"prompt_tokens": 3, "completion_tokens": 4, "prompt_tokens_details": {"cached_tokens": 2}},
])
def test_usage_after_finish_reason_preserves_deepseek_and_openai_cache_tokens(usage):
    body = text_event("hello", finish_reason="stop") + event({"choices": [], "usage": usage})
    result = asyncio.run(collect_stream(body))
    reported = next(item for kind, item in result if kind == "usage")
    assert (reported.input_tokens, reported.output_tokens, reported.cache_read_tokens) == (3, 4, 2)


@pytest.mark.parametrize("mode", ["generate", "vision"])
def test_nonstream_usage_preserves_deepseek_cache_tokens(mode):
    async def run():
        def respond(http_request):
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "hello"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4, "prompt_cache_hit_tokens": 2},
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = OpenAICompatibleProvider(client)
            if mode == "vision":
                result = await provider.generate_vision(config(), request(), "persona", b"image")
            else:
                result = await provider.generate(config(), request(), "persona")
            assert result.usage.cache_read_tokens == 2

    asyncio.run(run())


@pytest.mark.parametrize("chunk", [
    [], {}, {"choices": "invalid"}, {"choices": ["invalid"]},
    {"choices": [{"delta": "invalid"}]},
    {"choices": [{"delta": {"content": 17}}]},
    {"choices": [{"delta": {}, "finish_reason": 17}]},
    {"choices": [{"delta": {}, "finish_reason": []}]},
    {"choices": [{"delta": {}, "finish_reason": {}}]},
    {"choices": [], "usage": []},
])
def test_malformed_stream_fields_raise_provider_error(chunk):
    async def run():
        with pytest.raises(ModelProviderError, match="格式无效|标记无效"):
            await collect_stream(event(chunk))

    asyncio.run(run())


@pytest.mark.parametrize("reason,message", [
    ("length", "长度限制"), ("content_filter", "内容过滤"),
    ("tool_calls", "工具调用"), ("function_call", "工具调用"),
])
@pytest.mark.parametrize("mode", ["generate", "stream", "vision"])
def test_failed_completion_reason_is_not_success(reason, message, mode):
    async def run():
        def respond(http_request):
            if mode == "stream":
                return httpx.Response(200, text=text_event("partial", finish_reason=reason))
            return httpx.Response(200, json={"choices": [{
                "message": {"content": "partial"}, "finish_reason": reason,
            }]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = OpenAICompatibleProvider(client)
            with pytest.raises(ModelProviderError, match=message):
                if mode == "stream":
                    _ = [item async for item in provider.stream(config(), request(), "persona")]
                elif mode == "vision":
                    await provider.generate_vision(config(), request(), "persona", b"image")
                else:
                    await provider.generate(config(), request(), "persona")

    asyncio.run(run())


@pytest.mark.parametrize("base_url,field", [
    ("https://api.openai.com/v1", "max_completion_tokens"),
    ("https://generativelanguage.googleapis.com/v1beta/openai", "max_tokens"),
    ("https://api.openai.com.example/v1", "max_tokens"),
])
@pytest.mark.parametrize("mode", ["generate", "stream", "vision"])
def test_output_limit_field_matches_official_endpoint(base_url, field, mode):
    async def run():
        def respond(http_request):
            payload = json.loads(http_request.content)
            assert payload[field] == 2048
            assert ({"max_tokens", "max_completion_tokens"} - {field}).isdisjoint(payload)
            if mode == "stream":
                return httpx.Response(200, text=text_event("hello", finish_reason="stop"))
            return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = OpenAICompatibleProvider(client)
            selected = ProviderConfig("test", "test-model", base_url, "fake-secret")
            if mode == "stream":
                result = [item async for item in provider.stream(selected, request(), "persona")]
                assert result[0] == ("text", "hello")
            elif mode == "vision":
                assert (await provider.generate_vision(selected, request(), "persona", b"image")).text == "hello"
            else:
                assert (await provider.generate(selected, request(), "persona")).text == "hello"

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["generate", "stream", "vision"])
def test_injected_client_cannot_follow_provider_redirects(mode):
    async def run():
        hosts = []

        def respond(http_request):
            hosts.append(http_request.url.host)
            return httpx.Response(302, headers={"location": "https://other.example/chat/completions"})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), follow_redirects=True
        ) as client:
            provider = OpenAICompatibleProvider(client)
            selected = ProviderConfig("test", "test-model", "https://first.example/v1", "fake-secret")
            with pytest.raises(ModelProviderError, match="HTTP 302"):
                if mode == "stream":
                    _ = [item async for item in provider.stream(selected, request(), "persona")]
                elif mode == "vision":
                    await provider.generate_vision(selected, request(), "persona", b"image")
                else:
                    await provider.generate(selected, request(), "persona")
        assert hosts == ["first.example"]

    asyncio.run(run())
