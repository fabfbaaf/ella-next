"""Run one model request from a PyCharm terminal without exposing an HTTP call endpoint."""

import argparse
import asyncio

from ella_runtime.modules.memory.store import MemoryStore
from ella_runtime.modules.models.contracts import ModelMessage, ModelPurpose, ModelRequest
from ella_runtime.modules.models.gateway import ModelGateway
from ella_runtime.modules.models.settings import ModelSettings
from ella_runtime.modules.models.usage_store import UsageStore


async def _run(message: str, purpose: ModelPurpose) -> None:
    gateway = ModelGateway(ModelSettings.from_env(), UsageStore(), memory_retriever=MemoryStore())
    response = await gateway.generate(
        ModelRequest(purpose=purpose, messages=[ModelMessage(role="user", content=message)])
    )
    print(response.text)
    usage = response.usage
    print(
        f"\n[{response.provider}/{response.model}] "
        f"input={usage.input_tokens if usage.input_tokens is not None else '未知'} "
        f"output={usage.output_tokens if usage.output_tokens is not None else '未知'}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="艾拉模型链路命令行检查")
    parser.add_argument("message", help="发送给模型的文本")
    parser.add_argument("--purpose", choices=["chat", "action", "game", "voice"], default="chat")
    args = parser.parse_args()
    asyncio.run(_run(args.message, ModelPurpose(args.purpose)))


if __name__ == "__main__":
    main()
