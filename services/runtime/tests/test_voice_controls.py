import asyncio
from datetime import UTC, datetime

import pytest

from ella_runtime.modules.models.contracts import ModelPurpose, TokenUsage
from ella_runtime.modules.models.usage_store import UsageStore
from ella_runtime.modules.voice.commands import pauses_listening
from ella_runtime.modules.voice.provider import Speech, Transcription
from ella_runtime.modules.voice.session import VoiceSession
from ella_runtime.modules.voice.streaming import StreamingVoiceTurn


@pytest.mark.parametrize("text", ["暂停监听。", "停止监听", "先别聊了", "我现在不想聊天"])
def test_explicit_pause_commands(text):
    assert pauses_listening(text)


@pytest.mark.parametrize("text", ["他说暂停监听", "怎么暂停监听？", "如果我说暂停监听", "我不想暂停监听", "这份文档包含暂停监听"])
def test_pause_does_not_infer_from_questions_or_quotes(text):
    assert not pauses_listening(text)


def test_streaming_pause_is_delivered_before_speech_without_model_or_action(tmp_path):
    def usage(model):
        return TokenUsage(provider="test", model=model, purpose=ModelPurpose.VOICE, occurred_at=datetime.now(UTC))
    class Provider:
        async def transcribe(self, *args, **kwargs):
            return Transcription("暂停监听", usage("asr"))
        async def synthesize(self, text, **kwargs):
            return Speech(b"audio", "audio/mpeg", usage("tts"))
    class Gateway:
        async def stream_voice(self, request):
            raise AssertionError("Pause must not call a model")
            yield ""
    class Actions:
        async def process(self, *args, **kwargs):
            raise AssertionError("Pause must not create or approve work")
    async def run():
        events=[]
        async def send(event): events.append(event)
        session=VoiceSession(Provider(), Gateway(), UsageStore(tmp_path/'usage.sqlite3'), tts=True, dialogue_actions=Actions())
        turn=StreamingVoiceTurn(session, send)
        await turn.feed(b"\x00\x00"*1600)
        await turn.finish_input()
        await turn._reply_task
        kinds=[event['type'] for event in events]
        assert kinds.index('listening_pause') < kinds.index('audio_segment')
        assert session.pause_requested
        assert not session._history
        for index in range(len(turn.ledger.segments)):
            turn.acknowledge(index)
        assert session._history[-1].content.startswith('好，暂停监听')
        assert session.usage_store.summary()['totals']['requests']==3
    asyncio.run(run())
