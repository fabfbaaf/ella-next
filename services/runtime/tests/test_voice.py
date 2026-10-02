import asyncio
import json
from datetime import UTC, datetime

import httpx

from ella_runtime.api import app, get_companion_store, get_voice_session
from ella_runtime.modules.companion.store import CompanionStore
from ella_runtime.modules.models.contracts import ModelPurpose, ModelResponse, TokenUsage
from ella_runtime.modules.models.usage_store import UsageStore
from ella_runtime.modules.voice.provider import (
    OpenAICompatibleVoiceProvider,
    Speech,
    Transcription,
    VoiceProviderError,
    VoiceSettings,
)
from ella_runtime.modules.voice.session import VoiceInterrupted, VoiceSession, VoiceState
from tests.api_client import authorized_client


def _usage(model: str) -> TokenUsage:
    return TokenUsage(
        provider="test",
        model=model,
        purpose=ModelPurpose.VOICE,
        occurred_at=datetime.now(UTC),
    )


def test_voice_provider_sends_audio_and_records_only_reported_usage():
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/audio/transcriptions"):
            return httpx.Response(
                200, json={"text": "早上好", "usage": {"input_tokens": 7, "output_tokens": 2}}
            )
        return httpx.Response(200, content=b"mp3data", headers={"content-type": "audio/mpeg"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = OpenAICompatibleVoiceProvider(
                VoiceSettings("test", "http://127.0.0.1:9000/v1", None, "asr", "tts", "voice-a"),
                client,
            )
            transcript = await provider.transcribe(
                b"webmdata", filename="recording.webm", media_type="audio/webm", task_id="one"
            )
            speech = await provider.synthesize("你好", task_id="one")
            assert transcript.text == "早上好"
            assert transcript.usage.input_tokens == 7
            assert transcript.usage.output_tokens == 2
            assert speech.audio == b"mp3data"
            assert speech.usage.input_tokens is None

    asyncio.run(run())
    assert len(requests) == 2
    assert b"webmdata" in requests[0].content
    assert b'name="model"' in requests[0].content
    assert requests[0].url.path == "/v1/audio/transcriptions"
    assert json.loads(requests[1].content)["voice"] == "voice-a"


def test_voice_turn_uses_voice_route_keeps_history_and_falls_back_to_text(tmp_path):
    class Provider:
        async def transcribe(self, audio, *, filename, media_type, task_id=None):
            return Transcription("你好", _usage("asr"))

        async def synthesize(self, text, *, task_id=None):
            raise VoiceProviderError("语音合成失败")

    class Gateway:
        def __init__(self):
            self.requests = []

        async def generate(self, request):
            self.requests.append(request)
            return ModelResponse(text="你好呀", provider="test", model="chat", usage=_usage("chat"))

    gateway = Gateway()
    store = UsageStore(tmp_path / "usage.sqlite3")
    session = VoiceSession(Provider(), gateway, store, tts=True)

    async def run():
        first = await session.turn(b"audio", filename="a.webm", media_type="audio/webm")
        second = await session.turn(b"audio", filename="b.webm", media_type="audio/webm")
        assert first.audio is None
        assert first.speech_error == "语音合成失败"
        assert second.reply == "你好呀"

    asyncio.run(run())
    assert session.state == VoiceState.IDLE
    assert gateway.requests[0].purpose == ModelPurpose.VOICE
    assert [m.content for m in gateway.requests[1].messages] == ["你好", "（艾拉的语音播报失败）", "你好"]
    assert store.summary()["totals"]["requests"] == 2


def test_interrupt_discards_stale_response_and_recover_allows_new_turn(tmp_path):
    gate = asyncio.Event()

    class Provider:
        async def transcribe(self, audio, *, filename, media_type, task_id=None):
            await gate.wait()
            return Transcription("过时内容", _usage("asr"))

        async def synthesize(self, text, *, task_id=None):
            return Speech(b"mp3", "audio/mpeg", _usage("tts"))

    class Gateway:
        async def generate(self, request):
            raise AssertionError("打断后不能调用模型")

    store = UsageStore(tmp_path / "usage.sqlite3")
    session = VoiceSession(Provider(), Gateway(), store, tts=True)

    async def run():
        task = asyncio.create_task(
            session.turn(b"audio", filename="a.webm", media_type="audio/webm")
        )
        await asyncio.sleep(0)
        assert session.state == VoiceState.TRANSCRIBING
        session.interrupt()
        gate.set()
        try:
            await task
        except VoiceInterrupted:
            pass
        else:
            raise AssertionError("应丢弃过时语音")

    asyncio.run(run())
    assert session.state == VoiceState.INTERRUPTED
    assert store.summary()["totals"]["requests"] == 0
    session.recover()
    assert session.state == VoiceState.IDLE


def test_voice_api_checks_format_size_and_returns_text_fallback(tmp_path):
    class Provider:
        async def transcribe(self, audio, *, filename, media_type, task_id=None):
            assert audio == b"recording"
            assert filename == "recording.webm"
            return Transcription("测试", _usage("asr"))

        async def synthesize(self, text, *, task_id=None):
            raise AssertionError("未配置 TTS 时不应调用")

    class Gateway:
        async def generate(self, request):
            return ModelResponse(text="收到", provider="test", model="chat", usage=_usage("chat"))

    session = VoiceSession(Provider(), Gateway(), UsageStore(tmp_path / "usage.sqlite3"), tts=False)
    app.dependency_overrides[get_voice_session] = lambda: session
    app.dependency_overrides[get_companion_store] = lambda: CompanionStore(
        tmp_path / "companion.sqlite3"
    )
    try:
        client = authorized_client(app)
        assert (
            client.post(
                "/api/voice/turn", content=b"a", headers={"content-type": "text/plain"}
            ).status_code
            == 415
        )
        assert (
            client.post(
                "/api/voice/turn",
                content=b"a" * (10 * 1024 * 1024 + 1),
                headers={"content-type": "audio/webm"},
            ).status_code
            == 413
        )
        response = client.post(
            "/api/voice/turn", content=b"recording", headers={"content-type": "audio/webm"}
        )
        assert response.status_code == 200
        assert response.json()["reply"] == "收到"
        assert response.json()["audio_base64"] is None
        assert client.post("/api/voice/interrupt").json()["state"] == "interrupted"
        assert client.post("/api/voice/recover").json()["state"] == "idle"
    finally:
        app.dependency_overrides.clear()


def test_interrupted_playback_remembers_only_heard_sentence_and_can_continue(tmp_path):
    class Provider:
        def __init__(self):
            self.transcripts = iter(["讲个故事", "继续刚才的"])

        async def transcribe(self, audio, *, filename, media_type, task_id=None):
            return Transcription(next(self.transcripts), _usage("asr"))

        async def synthesize(self, text, *, task_id=None):
            return Speech(b"mp3", "audio/mpeg", _usage("tts"))

    class Gateway:
        def __init__(self):
            self.calls = 0

        async def generate(self, request):
            self.calls += 1
            return ModelResponse(
                text="第一句。第二句。", provider="test", model="chat", usage=_usage("chat")
            )

    gateway = Gateway()
    session = VoiceSession(Provider(), gateway, UsageStore(tmp_path / "usage.sqlite3"), tts=True)

    async def run():
        first = await session.turn(b"audio", filename="a.webm", media_type="audio/webm")
        assert first.turn_id and session.state == VoiceState.SPEAKING
        session.acknowledge(first.turn_id, played_ratio=0.6)
        assert [item.content for item in session._history] == ["讲个故事", "第一句。"]
        second = await session.turn(b"audio", filename="b.webm", media_type="audio/webm")
        assert second.reply == "第二句。"
        assert gateway.calls == 1
        session.acknowledge(second.turn_id, played_ratio=1)
        assert session.state == VoiceState.IDLE

    asyncio.run(run())
