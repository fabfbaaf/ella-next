import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from ella_runtime import api
from ella_runtime.modules.companion.store import CompanionStore
from ella_runtime.modules.models.contracts import ModelPurpose, ModelRequest, TokenUsage
from ella_runtime.modules.models.provider import OpenAICompatibleProvider
from ella_runtime.modules.models.settings import ProviderConfig
from ella_runtime.modules.models.usage_store import UsageStore
from ella_runtime.modules.voice.provider import Speech, Transcription
from ella_runtime.modules.voice.session import VoiceSession
from ella_runtime.modules.voice.streaming import PlaybackLedger, StreamingVoiceTurn, wav_audio
from tests.api_client import authorized_client


def usage(model):
    return TokenUsage(
        provider="test", model=model, purpose=ModelPurpose.VOICE, occurred_at=datetime.now(UTC)
    )


def test_streaming_provider_preserves_spaces_and_reads_usage():
    def respond(request):
        assert json.loads(request.content)["stream"] is True
        body = (
            'data: {"choices":[{"delta":{"content":"你好"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":" 世界"}}]}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":4}}\n\n'
            'data: [DONE]\n\n'
        )
        return httpx.Response(200, text=body)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = OpenAICompatibleProvider(client)
            config = ProviderConfig("test", "model", "http://127.0.0.1:9000/v1", None)
            request = ModelRequest(
                purpose=ModelPurpose.VOICE, messages=[{"role": "user", "content": "你好"}]
            )
            events = [event async for event in provider.stream(config, request, "艾拉")]
            assert "".join(item for kind, item in events if kind == "text") == "你好 世界"
            assert events[-1][1].output_tokens == 4

    asyncio.run(run())


def test_playback_ledger_rejects_out_of_order_acknowledgement():
    ledger = PlaybackLedger("你好", segments=["第一句。", "第二句。"])
    with pytest.raises(ValueError):
        ledger.acknowledge(1)
    ledger.acknowledge(0)
    assert ledger.heard == "第一句。"


def test_streaming_interrupt_remembers_only_completed_segments(tmp_path):
    class Provider:
        async def transcribe(self, audio, *, filename, media_type, task_id=None):
            assert audio.startswith(b"RIFF")
            return Transcription("你好", usage("asr"))

        async def synthesize(self, text, *, task_id=None):
            return Speech(text.encode(), "audio/mpeg", usage("tts"))

    class Gateway:
        async def stream_voice(self, request):
            yield "第一句。"
            yield "第二句。"

    async def run():
        events = []

        async def send(event):
            events.append(event)

        session = VoiceSession(
            Provider(), Gateway(), UsageStore(tmp_path / "usage.sqlite3"), tts=True
        )
        turn = StreamingVoiceTurn(session, send)
        await turn.feed(b"\x00\x00" * 1600)
        await turn.finish_input()
        await turn._reply_task
        assert [item["type"] for item in events].count("audio_segment") == 2
        turn.acknowledge(0)
        await turn.interrupt()
        assert session._history[-1].content == "第一句。"
        assert session._remaining_reply == "第二句。"
        assert session.usage_store.summary()["totals"]["requests"] == 3

    asyncio.run(run())


def test_wav_audio_contains_pcm_payload():
    assert wav_audio(b"\x01\x00").endswith(b"\x01\x00")


def test_websocket_voice_turn_delivers_segments_and_acknowledges_playback(tmp_path, monkeypatch):
    class Provider:
        async def transcribe(self, audio, *, filename, media_type, task_id=None):
            return Transcription("你好", usage("asr"))

        async def synthesize(self, text, *, task_id=None):
            return Speech(b"audio", "audio/mpeg", usage("tts"))

    class Gateway:
        async def stream_voice(self, request):
            yield "你好呀。"

    session = VoiceSession(
        Provider(), Gateway(), UsageStore(tmp_path / "usage.sqlite3"), tts=True
    )
    monkeypatch.setattr(api, "get_voice_session", lambda: session)
    monkeypatch.setattr(
        api, "get_companion_store", lambda: CompanionStore(tmp_path / "companion.sqlite3")
    )
    monkeypatch.setenv("ELLA_VOICE_BASE_URL", "http://127.0.0.1:9000/v1")
    monkeypatch.setenv("ELLA_STT_MODEL", "asr")
    with authorized_client(api.app).websocket_connect(
        "/api/voice/stream", headers={"origin": "http://localhost:1421", "host": "127.0.0.1:8766"},
        subprotocols=["ella-auth", api.get_runtime_session().token],
    ) as websocket:
        websocket.send_bytes(b"\x00\x00" * 1600)
        websocket.send_json({"type": "finish"})
        events = []
        while True:
            event = websocket.receive_json()
            events.append(event)
            if event["type"] == "reply_done":
                break
        assert [event["type"] for event in events] == [
            "transcript", "reply_delta", "audio_segment", "reply_done"
        ]
        websocket.send_json({"type": "ack", "index": 0})
        assert websocket.receive_json()["type"] == "acknowledged"
    assert session._history[-1].content == "你好呀。"


def test_streaming_http_error_reads_body_and_redacts_credentials():
    class ErrorBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"error":{"message":"Unknown model; key private-test-key"}}'

    def respond(request):
        return httpx.Response(400, stream=ErrorBody())

    async def run():
        from ella_runtime.modules.models.provider import ModelProviderError

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = OpenAICompatibleProvider(client)
            config = ProviderConfig("test", "invalid", "http://127.0.0.1:9000/v1", "private-test-key")
            request = ModelRequest(purpose=ModelPurpose.VOICE, messages=[{"role": "user", "content": "hello"}])
            with pytest.raises(ModelProviderError) as failure:
                _ = [item async for item in provider.stream(config, request, "艾拉")]
            assert "HTTP 400" in str(failure.value)
            assert "Unknown model" in str(failure.value)
            assert "private-test-key" not in str(failure.value)

    asyncio.run(run())


def test_empty_old_stream_does_not_interrupt_active_session(tmp_path):
    from ella_runtime.modules.voice.session import VoiceState

    async def run():
        async def send(event):
            pass

        session = VoiceSession(None, None, UsageStore(tmp_path / "usage.sqlite3"), tts=False)
        session._generation = 9
        session.state = VoiceState.THINKING
        idle_connection = StreamingVoiceTurn(session, send)
        await idle_connection.interrupt()
        assert session._generation == 9
        assert session.state == VoiceState.THINKING

    asyncio.run(run())


def test_old_interrupt_summary_cannot_cancel_restarted_voice_turn(tmp_path):
    class History:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.pairs = []

        def recent(self):
            return []

        def record(self, user, assistant):
            self.pairs.append((user, assistant))

        async def flush(self):
            self.started.set()
            await self.release.wait()

    class Provider:
        def __init__(self):
            self.calls = 0
            self.new_started = asyncio.Event()
            self.release_new = asyncio.Event()

        async def transcribe(self, audio, *, filename, media_type, task_id=None):
            self.calls += 1
            if self.calls == 2:
                self.new_started.set()
                await self.release_new.wait()
            return Transcription(f"问题{self.calls}", usage("asr"))

        async def synthesize(self, text, *, task_id=None):
            return Speech(b"audio", "audio/mpeg", usage("tts"))

    class Gateway:
        async def stream_voice(self, request):
            yield "回复。"

    async def run():
        events = []

        async def send(event):
            events.append(event)

        history = History()
        provider = Provider()
        session = VoiceSession(provider, Gateway(), UsageStore(tmp_path / "usage.sqlite3"), tts=True, history=history)
        old = StreamingVoiceTurn(session, send)
        await old.feed(b"\x00\x00" * 1600)
        await old.finish_input()
        await old._reply_task
        stopping = asyncio.create_task(old.interrupt())
        await asyncio.wait_for(history.started.wait(), 1)
        events.clear()
        new = StreamingVoiceTurn(session, send)
        await new.feed(b"\x00\x00" * 1600)
        await new.finish_input()
        await asyncio.wait_for(provider.new_started.wait(), 1)
        history.release.set()
        await asyncio.wait_for(stopping, 1)
        provider.release_new.set()
        await asyncio.wait_for(new._reply_task, 1)
        assert any(event["type"] == "reply_done" for event in events)
        assert not any(event["type"] == "interrupted" for event in events)
        new.acknowledge(0)
        if session._memory_task is not None:
            await session._memory_task
        assert history.pairs[-1] == ("问题2", "回复。")

    asyncio.run(run())


def test_english_and_unpunctuated_replies_use_bounded_speech_segments(tmp_path):
    from ella_runtime.modules.voice.streaming import MAX_SEGMENT_CHARS

    class Provider:
        async def transcribe(self, audio, *, filename, media_type, task_id=None):
            return Transcription("hello", usage("asr"))

        async def synthesize(self, text, *, task_id=None):
            return Speech(b"audio", "audio/mpeg", usage("tts"))

    class Gateway:
        async def stream_voice(self, request):
            yield "Hello. "
            yield "x" * (MAX_SEGMENT_CHARS * 2 + 9)

    async def run():
        async def send(event):
            pass

        session = VoiceSession(Provider(), Gateway(), UsageStore(tmp_path / "usage.sqlite3"), tts=True)
        turn = StreamingVoiceTurn(session, send)
        await turn.feed(b"\x00\x00" * 1600)
        await turn.finish_input()
        await turn._reply_task
        assert turn.ledger.segments[0] == "Hello."
        assert all(len(segment) <= MAX_SEGMENT_CHARS for segment in turn.ledger.segments)
        assert "".join(turn.ledger.segments) == turn.ledger.reply

    asyncio.run(run())


def test_streaming_model_reception_continues_while_tts_is_waiting(tmp_path):
    class Provider:
        async def transcribe(self, audio, **kwargs):
            return Transcription("你好", usage("asr"))

        async def synthesize(self, text, **kwargs):
            synthesis_started.set()
            await release_synthesis.wait()
            return Speech(text.encode(), "audio/mpeg", usage("tts"))

    class Gateway:
        async def stream_voice(self, request):
            yield "第一句。"
            await synthesis_started.wait()
            yield "第二句。"
            model_received.set()

    async def run():
        nonlocal synthesis_started, release_synthesis, model_received
        synthesis_started, release_synthesis, model_received = asyncio.Event(), asyncio.Event(), asyncio.Event()
        events = []
        async def send(event):
            events.append(event)
        session = VoiceSession(Provider(), Gateway(), UsageStore(tmp_path / "usage.sqlite3"), tts=True)
        turn = StreamingVoiceTurn(session, send)
        await turn.feed(b"\0\0" * 1600)
        await turn.finish_input()
        await asyncio.wait_for(model_received.wait(), 1)
        assert not [event for event in events if event["type"] == "audio_segment"]
        release_synthesis.set()
        await turn._reply_task
        assert len([event for event in events if event["type"] == "audio_segment"]) == 2
        await turn.interrupt()
    synthesis_started = release_synthesis = model_received = None
    asyncio.run(run())


def test_pcm_chunks_do_not_repeatedly_transcribe_full_prefix(tmp_path):
    class Provider:
        calls = 0
        async def transcribe(self, audio, **kwargs):
            self.calls += 1
            return Transcription("这是测试的一句话", usage("asr"))
    class Gateway:
        async def stream_voice(self, request):
            yield "收到。"
    async def run():
        provider = Provider()
        async def send(event):
            pass
        session = VoiceSession(provider, Gateway(), UsageStore(tmp_path / "usage.sqlite3"), tts=False)
        turn = StreamingVoiceTurn(session, send)
        for _ in range(30):
            await turn.feed(b"\0\0" * 1600)
        assert provider.calls == 0
        await turn.finish_input()
        await turn._reply_task
        assert provider.calls == 1
    asyncio.run(run())
