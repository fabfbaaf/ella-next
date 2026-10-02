import asyncio
import json

import httpx

from ella_runtime.modules.voice.fish import FishSpeechProvider, FishSpeechSettings


def test_fish_cloud_tts_sends_selected_voice_and_model_without_reporting_fake_tokens():
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"mp3-audio")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = FishSpeechProvider(
                FishSpeechSettings("test-key", "voice-reference", base_url="https://fish.example"),
                client,
            )
            speech = await provider.synthesize("你好，今天一起玩游戏吧", task_id="voice-1")
            assert speech.audio == b"mp3-audio"
            assert speech.usage.input_tokens is None
            assert speech.usage.output_tokens is None

    asyncio.run(run())
    assert requests[0].url.path == "/v1/tts"
    assert requests[0].headers["model"] == "s2.1-pro-free"
    assert json.loads(requests[0].content)["reference_id"] == "voice-reference"
