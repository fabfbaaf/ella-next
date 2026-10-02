"""Approval uses actually acknowledged speech, including interrupted and silent turns."""

import asyncio

import pytest

from ella_runtime.modules.agent.contracts import TaskState
from ella_runtime.modules.models.conversations import ConversationStore
from ella_runtime.modules.models.usage_store import UsageStore
from ella_runtime.modules.voice.history import VoiceHistory
from ella_runtime.modules.voice.provider import Speech, Transcription
from ella_runtime.modules.voice.session import VoiceSession
from ella_runtime.modules.voice.streaming import StreamingVoiceTurn
from tests.test_dialogue_actions import finish_jobs, setup
from tests.test_voice_streaming import usage


class FakeVoiceProvider:
    def __init__(self, *transcripts):
        self.transcripts = iter(transcripts)
        self.synthesized = []

    async def transcribe(self, audio, *, filename, media_type, task_id=None):
        assert audio.startswith(b"RIFF")
        return Transcription(next(self.transcripts), usage("fake-asr"))

    async def synthesize(self, text, *, task_id=None):
        self.synthesized.append(text)
        return Speech(b"fake-audio", "audio/mpeg", usage("fake-tts"))


class NoFreeFormGateway:
    async def stream_voice(self, request):
        raise AssertionError("Work and confirmation must use the shared dialogue router")
        yield ""


def voice_session(tmp_path, provider, actions, *, tts=True):
    history = VoiceHistory(
        ConversationStore(tmp_path / "voice.sqlite3"), conversation_id="streaming-review",
    )
    return VoiceSession(
        provider, NoFreeFormGateway(), UsageStore(tmp_path / "usage.sqlite3"),
        tts=tts, history=history, dialogue_actions=actions,
    )


async def completed_turn(session):
    events = []

    async def send(event):
        events.append(event.copy())
        if event["type"] == "audio_segment":
            turn.acknowledge(event["index"])

    turn = StreamingVoiceTurn(session, send)
    await turn.feed(b"\0\0" * 1600)
    await turn.finish_input()
    await asyncio.wait_for(turn._reply_task, 2)
    assert not any(event["type"] in {"error", "speech_error"} for event in events)
    assert any(event["type"] == "reply_done" for event in events)
    assert turn.ledger.complete and turn.ledger.saved
    if session._memory_task is not None:
        await session._memory_task
    return turn, events


def test_streaming_full_plan_acknowledgements_allow_confirmation(tmp_path):
    actions, agent, model, planner, tool = setup(
        tmp_path, {"kind": "task", "goal": "创建学习安排文档"},
    )
    provider = FakeVoiceProvider("帮我创建学习安排文档", "确认执行")
    session = voice_session(tmp_path, provider, actions)

    async def run():
        try:
            first, events = await completed_turn(session)
            assert len(first.ledger.segments) > 1
            assert first.ledger.acknowledged == len(first.ledger.segments)
            assert session._history[-1].content == first.ledger.reply
            assert any(event["type"] == "audio_segment" for event in events)
            assert agent.store.list()[0].state == TaskState.WAITING_APPROVAL
            assert not tool.calls and not actions.jobs
            second, _ = await completed_turn(session)
            assert "开始执行" in second.ledger.reply
            await finish_jobs(actions)
            assert agent.store.list()[0].state == TaskState.COMPLETE
            assert len(tool.calls) == len(model.requests) == len(planner.goals) == 1
        finally:
            await actions.aclose()

    asyncio.run(run())


def test_streaming_partial_ack_then_interrupt_does_not_approve_unheard_plan(tmp_path):
    actions, agent, _, _, tool = setup(tmp_path, {"kind": "task", "goal": "创建学习安排文档"})
    provider = FakeVoiceProvider("帮我创建学习安排文档", "确认执行", "确认执行")
    session = voice_session(tmp_path, provider, actions)

    async def run():
        reached_unplayed_segment = asyncio.Event()
        pause_send = asyncio.Event()

        async def send(event):
            if event["type"] == "audio_segment":
                if event["index"] == 0:
                    first.acknowledge(0)
                else:
                    reached_unplayed_segment.set()
                    await pause_send.wait()

        first = StreamingVoiceTurn(session, send)
        try:
            await first.feed(b"\0\0" * 1600)
            await first.finish_input()
            await asyncio.wait_for(reached_unplayed_segment.wait(), 2)
            assert first.ledger.acknowledged == 1
            assert len(first.ledger.segments) > first.ledger.acknowledged
            await first.interrupt()
            assert session._history[-1].content == first.ledger.heard
            assert session._history[-1].content != first.ledger.reply
            assert agent.store.list()[0].state == TaskState.WAITING_APPROVAL
            second, _ = await completed_turn(session)
            assert "具体内容" in second.ledger.reply
            assert not tool.calls and not actions.jobs
            assert agent.store.list()[0].state == TaskState.WAITING_APPROVAL
            third, _ = await completed_turn(session)
            assert "开始执行" in third.ledger.reply
            await finish_jobs(actions)
            assert len(tool.calls) == 1 and agent.store.list()[0].state == TaskState.COMPLETE
        finally:
            await first.interrupt()
            await actions.aclose()
            if session._memory_task is not None:
                await session._memory_task

    asyncio.run(run())


@pytest.mark.parametrize("reload_history", [False, True])
def test_enabling_tts_cannot_approve_a_previously_silent_plan(tmp_path, reload_history):
    actions, agent, _, _, tool = setup(tmp_path, {"kind": "task", "goal": "创建学习安排文档"})
    provider = FakeVoiceProvider("帮我创建学习安排文档", "确认执行", "确认执行")
    silent_session = voice_session(tmp_path, provider, actions, tts=False)

    async def run():
        try:
            first, events = await completed_turn(silent_session)
            assert "office.create_document" in first.ledger.reply
            assert first.ledger.segments == []
            assert not provider.synthesized
            assert not any(event["type"] == "audio_segment" for event in events)
            assert silent_session._history[-1].content == "（语音合成未启用，回复未播出）"
            assert silent_session.history.recent()[-1].content == silent_session._history[-1].content
            if reload_history:
                session = voice_session(tmp_path, provider, actions, tts=True)
            else:
                session = silent_session
                session.tts = True
            second, _ = await completed_turn(session)
            assert "具体内容" in second.ledger.reply
            assert not tool.calls and not actions.jobs
            assert agent.store.list()[0].state == TaskState.WAITING_APPROVAL
            third, _ = await completed_turn(session)
            assert "开始执行" in third.ledger.reply
            await finish_jobs(actions)
            assert len(tool.calls) == 1 and agent.store.list()[0].state == TaskState.COMPLETE
        finally:
            await actions.aclose()

    asyncio.run(run())
