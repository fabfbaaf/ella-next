"""Read model metadata and run explicit, bounded model generation diagnostics."""

import json
from datetime import datetime
from time import perf_counter
from typing import Literal

import httpx

from ella_runtime.modules.models.contracts import (
    ModelPurpose,
    ModelRequest,
    ModelResponse,
    TokenUsage,
)
from ella_runtime.modules.models.provider import OpenAICompatibleProvider
from ella_runtime.modules.models.settings import ProviderConfig


async def probe_model(config: ProviderConfig, *, client: httpx.AsyncClient | None = None) -> dict:
    if not config.configured:
        raise ValueError("模型字段或凭据未配齐，请先保存配置")
    if client is None:
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as owned:
            return await probe_model(config, client=owned)
    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    started = perf_counter()
    try:
        async with client.stream(
            "GET", config.base_url.rstrip("/") + "/models", headers=headers,
            follow_redirects=False,
        ) as response:
            if response.status_code != 200:
                raise ValueError(f"模型列表请求失败：HTTP {response.status_code}；请核对服务地址、权限和密钥")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 1024 * 1024:
                    raise ValueError("模型列表响应过大，已停止读取")
        payload = json.loads(data)
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise ValueError("服务返回的模型列表格式不兼容")  # noqa: TRY004 - malformed service response, not caller type
        ids = {
            model_id.strip()
            for item in items
            if isinstance(item, dict)
            and isinstance(model_id := item.get("id"), str)
            and model_id.strip()
        }
        found = config.model in ids or f"models/{config.model}" in ids
        return {
            "reachable": True,
            "model_found": found,
            "model_count": len(ids),
            "model_ids": sorted(ids)[:1000],
            "latency_ms": round((perf_counter() - started) * 1000),
            "detail": (
                "服务可连接，列表中包含当前模型；生成能力仍需聊天测试。"
                if found else
                "服务可连接，但列表未包含当前模型；部分服务会隐藏模型，请继续核对实际模型 ID。"
            ),
        }
    except httpx.HTTPError as exc:
        raise ValueError("模型服务连接超时或网络不可用，请检查地址与网络") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("服务没有返回有效模型列表") from exc


async def run_model_test(
    config: ProviderConfig,
    *,
    mode: Literal["text", "stream"] = "text",
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Use the saved route for one explicitly requested, short generation check."""
    if mode not in {"text", "stream"}:
        raise ValueError("生成测试模式必须为 text 或 stream")
    if not config.configured:
        raise ValueError("模型字段或凭据未配齐，请先保存配置")
    request = ModelRequest(
        purpose=ModelPurpose.CHAT,
        messages=[{"role": "user", "content": "请只回复：连接成功"}],
        max_output_tokens=128,
    )
    provider = OpenAICompatibleProvider(client)
    persona = "你是模型连接测试助手，请简短回复。"
    started = perf_counter()
    if mode == "text":
        response = await provider.generate(config, request, persona)
    else:
        parts: list[str] = []
        reported: TokenUsage | None = None
        async for kind, item in provider.stream(config, request, persona):
            if kind == "text":
                parts.append(item)
            elif kind == "usage":
                reported = item
        usage = reported or TokenUsage(
            provider=config.name, model=config.model, purpose=request.purpose,
            occurred_at=datetime.now().astimezone(),
        )
        response = ModelResponse(
            text="".join(parts).strip(), provider=usage.provider, model=usage.model, usage=usage,
        )
    return {
        "preview": response.text[:500],
        "provider": response.provider,
        "model": response.model,
        "latency_ms": round((perf_counter() - started) * 1000),
        "usage": response.usage.model_dump(mode="json"),
    }
