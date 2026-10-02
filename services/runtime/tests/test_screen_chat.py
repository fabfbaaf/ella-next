import asyncio
import base64
import json
from datetime import UTC, datetime

import httpx

from ella_runtime.api import app, get_screen_chat
from ella_runtime.modules.games.screen_chat import GameScreenChat, ScreenCaptureError, WindowImage
from ella_runtime.modules.models.contracts import ModelPurpose, ModelResponse, TokenUsage
from ella_runtime.modules.models.gateway import ModelGateway
from ella_runtime.modules.models.provider import OpenAICompatibleProvider
from ella_runtime.modules.models.settings import ModelSettings, ProviderConfig
from ella_runtime.modules.models.usage_store import UsageStore
from tests.api_client import authorized_client


def test_vision_request_includes_persona_image_and_records_usage(tmp_path):
    seen = []

    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "画面里有一片农场"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 7},
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            gateway = ModelGateway(
                ModelSettings(
                    chat=ProviderConfig("ollama", "text", "http://127.0.0.1:11434/v1"),
                    vision=ProviderConfig("gemini", "vision", "https://example.com/v1", "test-key"),
                ),
                UsageStore(tmp_path / "usage.sqlite3"),
                OpenAICompatibleProvider(client),
            )

            class Capture:
                def capture(self, expected_title):
                    assert expected_title == "Stardew"
                    return WindowImage(title="Stardew Valley", png=b"pngdata")

            title, answer = await GameScreenChat(Capture(), gateway).ask(
                game_id="stardew_valley", window_title="Stardew", message="我现在在哪里？"
            )
            assert title == "Stardew Valley"
            assert answer == "画面里有一片农场"

    asyncio.run(run())
    payload = seen[0]
    assert payload["model"] == "vision"
    assert "你是艾拉" in payload["messages"][0]["content"]
    assert payload["messages"][1]["content"][1]["image_url"]["url"] == (
        "data:image/png;base64," + base64.b64encode(b"pngdata").decode()
    )
    summary = UsageStore(tmp_path / "usage.sqlite3").summary()
    assert summary["totals"]["input_tokens"] == 12
    assert summary["groups"][0]["purpose"] == "game"


def test_screen_chat_api_rejects_wrong_foreground_window():
    class Capture:
        def capture(self, expected_title):
            raise ScreenCaptureError("当前前台窗口与指定游戏不匹配")

    class Gateway:
        async def generate_vision(self, request, image):
            return ModelResponse(
                text="不应调用",
                provider="test",
                model="vision",
                usage=TokenUsage(
                    provider="test",
                    model="vision",
                    purpose=ModelPurpose.GAME,
                    occurred_at=datetime.now(UTC),
                ),
            )

    app.dependency_overrides[get_screen_chat] = lambda: GameScreenChat(Capture(), Gateway())
    try:
        response = authorized_client(app).post(
            "/api/games/screen-chat",
            json={"game_id": "minecraft", "window_title": "Minecraft", "message": "看到什么？"},
        )
        assert response.status_code == 409
    finally:
        app.dependency_overrides.clear()
