"""OpenAI-compatible chat transport for local and third-party providers."""

import base64
import json
import re
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from ella_runtime.modules.models.contracts import ModelRequest, ModelResponse, TokenUsage
from ella_runtime.modules.models.persona import system_prompt
from ella_runtime.modules.models.settings import ProviderConfig


class ModelProviderError(RuntimeError):
    """A configured provider failed without exposing credentials in the error."""


def _error_detail(config: ProviderConfig, body: Any) -> str:
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else error
    if not isinstance(message, str):
        return ""
    # Redact the complete message before clipping; clipping first can reveal a key prefix.
    if config.api_key:
        message = message.replace(config.api_key, "[已隐藏密钥]")
    message = re.sub(r"AIza[0-9A-Za-z_-]{20,}", "[已隐藏密钥]", message)
    lines = message.strip().splitlines()
    return lines[0][:240] if lines else ""


def _http_failure(config: ProviderConfig, response: httpx.Response, action: str) -> ModelProviderError:
    detail = ""
    try:
        detail = _error_detail(config, response.json())
    except (ValueError, httpx.ResponseNotRead):
        pass
    suffix = f"：{detail}" if detail else ""
    return ModelProviderError(f"{config.name} {action}：HTTP {response.status_code}{suffix}")


def _check_response_error(config: ProviderConfig, body: dict[str, Any], action: str) -> None:
    if body.get("error") is not None:
        detail = _error_detail(config, body)
        suffix = f"：{detail}" if detail else ""
        raise ModelProviderError(f"{config.name} {action}{suffix}")


def _check_finish_reason(config: ProviderConfig, choice: dict[str, Any]) -> None:
    reason = choice.get("finish_reason")
    if reason is None:
        return
    if not isinstance(reason, str) or not reason:
        raise ModelProviderError(f"{config.name} 模型结束标记无效")
    if reason == "length":
        raise ModelProviderError(f"{config.name} 输出达到模型长度限制，回复可能不完整")
    if reason == "content_filter":
        raise ModelProviderError(f"{config.name} 内容过滤阻止了完整回复")
    if reason in {"tool_calls", "function_call"}:
        raise ModelProviderError(f"{config.name} 返回了未支持的模型工具调用")


def _output_limit(config: ProviderConfig, request: ModelRequest) -> dict[str, int]:
    field = (
        "max_completion_tokens"
        if urlsplit(config.base_url).hostname == "api.openai.com"
        else "max_tokens"
    )
    return {field: request.max_output_tokens}


def _token(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _usage(
    config: ProviderConfig, request: ModelRequest, data: dict[str, Any]
) -> TokenUsage:
    raw_usage = data.get("usage")
    usage = raw_usage if isinstance(raw_usage, dict) else {}
    raw_details = usage.get("prompt_tokens_details")
    details = raw_details if isinstance(raw_details, dict) else {}
    cache_read = usage.get("prompt_cache_hit_tokens")
    if cache_read is None:
        cache_read = details.get("cached_tokens")
    return TokenUsage(
        provider=config.name,
        model=str(data.get("model") or config.model),
        purpose=request.purpose,
        task_id=request.task_id,
        occurred_at=datetime.now().astimezone(),
        input_tokens=_token(usage.get("prompt_tokens")),
        output_tokens=_token(usage.get("completion_tokens")),
        cache_read_tokens=_token(cache_read),
    )


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "".join(
            part.get("text", "")
            for part in value
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ).strip()
    return ""


async def _sse_data(response: httpx.Response) -> AsyncIterator[str]:
    """Join data lines within each event and ignore comments/metadata."""
    parts: list[str] = []
    async for line in response.aiter_lines():
        if not line:
            if parts:
                yield "\n".join(parts)
                parts.clear()
        elif line.startswith("data:"):
            part = line[5:]
            parts.append(part.removeprefix(" "))
    if parts:
        yield "\n".join(parts)


class OpenAICompatibleProvider:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def stream(
        self, config: ProviderConfig, request: ModelRequest, persona: str
    ) -> AsyncIterator[tuple[str, str | TokenUsage]]:
        """Yield text deltas and usage; never silently accept failed or cut-off streams."""
        if not config.configured:
            raise ModelProviderError(f"{config.name} 模型尚未配置完整")
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        messages = [{"role": "system", "content": system_prompt(persona, request.instructions)}]
        messages.extend(message.model_dump() for message in request.messages)
        payload = {
            "model": config.model, "messages": messages,
            **_output_limit(config, request), "stream": True,
            "stream_options": {"include_usage": True},
        }
        url = f"{config.base_url.rstrip('/')}/chat/completions"

        async def consume(client: httpx.AsyncClient) -> AsyncIterator[tuple[str, str | TokenUsage]]:
            has_text = False
            finished = False
            try:
                async with client.stream(
                    "POST", url, headers=headers, json=payload, follow_redirects=False
                ) as response:
                    if response.is_error:
                        await response.aread()
                        raise _http_failure(config, response, "流式请求失败")
                    response.raise_for_status()
                    async for data in _sse_data(response):
                        if data.strip() == "[DONE]":
                            finished = True
                            break
                        if not data.strip():
                            continue
                        try:
                            chunk = json.loads(data)
                        except ValueError as exc:
                            raise ModelProviderError(f"{config.name} 流式响应格式无效") from exc
                        if not isinstance(chunk, dict):
                            raise ModelProviderError(f"{config.name} 流式响应格式无效")
                        _check_response_error(config, chunk, "流式请求失败")
                        usage = chunk.get("usage")
                        if usage is not None and not isinstance(usage, dict):
                            raise ModelProviderError(f"{config.name} 流式用量格式无效")
                        if isinstance(usage, dict):
                            yield "usage", _usage(config, request, chunk)
                        choices = chunk.get("choices")
                        if choices is None and isinstance(usage, dict):
                            continue
                        if not isinstance(choices, list):
                            raise ModelProviderError(f"{config.name} 流式候选格式无效")
                        if not choices:
                            continue
                        first = choices[0]
                        if not isinstance(first, dict):
                            raise ModelProviderError(f"{config.name} 流式候选格式无效")
                        _check_finish_reason(config, first)
                        finish_reason = first.get("finish_reason")
                        if finish_reason is not None:
                            if not isinstance(finish_reason, str) or not finish_reason:
                                raise ModelProviderError(f"{config.name} 流式结束标记无效")
                            finished = True
                        delta = first.get("delta")
                        if delta is None and finish_reason is not None:
                            continue
                        if not isinstance(delta, dict):
                            raise ModelProviderError(f"{config.name} 流式文本格式无效")
                        part = delta.get("content")
                        if part is not None and not isinstance(part, str):
                            raise ModelProviderError(f"{config.name} 流式文本格式无效")
                        if part:
                            has_text = has_text or bool(part.strip())
                            yield "text", part
                    if not has_text:
                        raise ModelProviderError(f"{config.name} 流式请求未返回文本内容")
                    if not finished:
                        raise ModelProviderError(f"{config.name} 流式响应提前结束，回复可能不完整")
            except httpx.HTTPStatusError as exc:
                raise _http_failure(config, exc.response, "流式请求失败") from exc
            except httpx.HTTPError as exc:
                raise ModelProviderError(f"{config.name} 流式连接失败") from exc

        if self._client is None:
            async with httpx.AsyncClient(timeout=90.0) as client:
                async for item in consume(client):
                    yield item
        else:
            async for item in consume(self._client):
                yield item

    async def generate(
        self, config: ProviderConfig, request: ModelRequest, persona: str
    ) -> ModelResponse:
        if not config.configured:
            raise ModelProviderError(f"{config.name} 模型尚未配置完整")

        messages = [{"role": "system", "content": system_prompt(persona, request.instructions)}]
        messages.extend(message.model_dump() for message in request.messages)
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        payload = {
            "model": config.model,
            "messages": messages,
            **_output_limit(config, request),
            "stream": False,
        }
        url = f"{config.base_url.rstrip('/')}/chat/completions"

        async def send(client: httpx.AsyncClient) -> dict[str, Any]:
            try:
                response = await client.post(url, headers=headers, json=payload, follow_redirects=False)
                response.raise_for_status()
                data = response.json()
            except httpx.HTTPStatusError as exc:
                raise _http_failure(config, exc.response, "请求失败") from exc
            except (httpx.HTTPError, ValueError) as exc:
                raise ModelProviderError(f"{config.name} 请求失败或返回格式无效") from exc
            if not isinstance(data, dict):
                raise ModelProviderError(f"{config.name} 返回格式无效")
            _check_response_error(config, data, "请求失败")
            return data

        if self._client is None:
            async with httpx.AsyncClient(timeout=90.0) as client:
                data = await send(client)
        else:
            data = await send(self._client)

        choices = data.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else None
        if not isinstance(first, dict):
            raise ModelProviderError(f"{config.name} 未返回候选结果")
        _check_finish_reason(config, first)
        message = first.get("message")
        text = _message_text(message.get("content") if isinstance(message, dict) else None)
        if not text:
            raise ModelProviderError(f"{config.name} 未返回文本内容")

        return ModelResponse(
            text=text,
            provider=config.name,
            model=str(data.get("model") or config.model),
            usage=_usage(config, request, data),
        )

    async def generate_vision(
        self,
        config: ProviderConfig,
        request: ModelRequest,
        persona: str,
        image: bytes,
        *,
        media_type: str = "image/png",
    ) -> ModelResponse:
        if not config.configured:
            raise ModelProviderError(f"{config.name} 视觉模型尚未配置完整")
        if (
            media_type not in {"image/png", "image/jpeg"}
            or not image
            or len(image) > 5 * 1024 * 1024
        ):
            raise ModelProviderError("截图格式无效或超过 5 MB")
        prompt = request.messages[-1].content
        if not isinstance(prompt, str):
            raise ModelProviderError("视觉问题必须是文字")
        image_url = f"data:{media_type};base64,{base64.b64encode(image).decode()}"
        payload = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": system_prompt(persona, request.instructions)},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                },
            ],
            **_output_limit(config, request),
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"

        async def send(client: httpx.AsyncClient) -> dict[str, Any]:
            try:
                response = await client.post(
                    f"{config.base_url.rstrip('/')}/chat/completions", headers=headers,
                    json=payload, follow_redirects=False,
                )
                response.raise_for_status()
                data = response.json()
            except httpx.HTTPStatusError as exc:
                raise _http_failure(config, exc.response, "视觉请求失败") from exc
            except (httpx.HTTPError, ValueError) as exc:
                raise ModelProviderError(f"{config.name} 视觉请求失败或返回格式无效") from exc
            if not isinstance(data, dict):
                raise ModelProviderError(f"{config.name} 视觉请求返回格式无效")
            _check_response_error(config, data, "视觉请求失败")
            return data

        if self._client is None:
            async with httpx.AsyncClient(timeout=90.0) as client:
                data = await send(client)
        else:
            data = await send(self._client)

        choices = data.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else None
        if isinstance(first, dict):
            _check_finish_reason(config, first)
        message = first.get("message") if isinstance(first, dict) else None
        answer = _message_text(message.get("content") if isinstance(message, dict) else None)
        if not answer:
            raise ModelProviderError(f"{config.name} 视觉请求未返回文字")
        return ModelResponse(
            text=answer,
            provider=config.name,
            model=str(data.get("model") or config.model),
            usage=_usage(config, request, data),
        )
