import asyncio
from datetime import UTC, datetime

import pytest

from ella_runtime.modules.agent.contracts import (
    AgentTask,
    TaskState,
    TaskStep,
    ToolEvidence,
)
from ella_runtime.modules.agent.engine import AgentEngine
from ella_runtime.modules.agent.store import TaskStore
from ella_runtime.modules.companion.dialogue import DialogueActions
from ella_runtime.modules.companion.store import CompanionStore
from ella_runtime.modules.models.contracts import ModelMessage, ModelPurpose, TokenUsage
from ella_runtime.modules.models.conversations import ConversationStore
from ella_runtime.modules.models.usage_store import UsageStore
from ella_runtime.modules.voice.context import VoiceContext
from ella_runtime.modules.voice.history import VoiceHistory
from ella_runtime.modules.voice.provider import Speech, Transcription
from ella_runtime.modules.voice.session import VoiceSession


class NoModel:
    async def generate(self, _request):
        raise AssertionError("Bound task commands must not ask the model to guess a plan")


class RecordingTool:
    name = "office.create_document"
    description = "Isolated test document tool"

    def __init__(self):
        self.calls = []

    async def execute(self, arguments, *, action_id):
        self.calls.append(arguments.copy())
        return ToolEvidence(verified=True, details={"path": "fixture/result.docx"})


def fixture(tmp_path):
    tool = RecordingTool()
    agent = AgentEngine(NoModel(), TaskStore(tmp_path / "tasks.sqlite3"), [tool])
    actions = DialogueActions(
        NoModel(), CompanionStore(tmp_path / "companion.sqlite3"), agent,
        path=tmp_path / "dialogue.sqlite3",
    )
    return actions, agent, tool


def bind_task(actions, agent, identity, *, task_id="source-task", state=TaskState.WAITING_APPROVAL):
    now = datetime.now(UTC)
    task = AgentTask(
        id=task_id, goal=f"创建 {task_id}.docx", created_at=now, updated_at=now,
        state=state, steps=[TaskStep(
            id=f"step-{task_id}", tool="office.create_document",
            arguments={"path": f"{task_id}.docx", "title": "安排", "paragraphs": ["周一学习英语"]},
        )],
    )
    agent.store.save(task)
    preview = actions._show(identity, task)
    return task, preview


def voice_session(tmp_path, actions, *, tts=True):
    usage = TokenUsage(provider="test", model="fixture", purpose=ModelPurpose.VOICE, occurred_at=datetime.now(UTC))

    class Provider:
        async def transcribe(self, _audio, **_kwargs):
            return Transcription(text="确认执行", usage=usage)

        async def synthesize(self, _text, **_kwargs):
            return Speech(audio=b"fixture", media_type="audio/mpeg", usage=usage)

    store = ConversationStore(tmp_path / "chats.sqlite3")
    context = VoiceContext(store)
    session = VoiceSession(
        Provider(), NoModel(), UsageStore(tmp_path / "usage.sqlite3"), tts=tts,
        history=VoiceHistory(store), dialogue_actions=actions, context=context,
    )
    return session, store, context


@pytest.mark.parametrize("approved", [False, True])
def test_source_delivery_or_approval_is_not_inherited_and_interruption_requires_redelivery(tmp_path, approved):
    async def run():
        actions, agent, tool = fixture(tmp_path)
        task, source_preview = bind_task(actions, agent, "chat-source")
        if approved:
            agent.approve(task.id, task.plan_hash)
        source_binding = actions._binding("chat-source")
        session, store, context = voice_session(tmp_path, actions)
        store.ensure("chat-source")
        store.append_pair("chat-source", "确认执行", source_preview)
        context.set_context_source("chat-source")
        actions.handoff_reference("chat-source")
        binding = actions._binding("voice-main")
        assert binding["preview_text"] == "" and binding["full_preview"] == 0
        first = await session.turn(b"fixture", filename="fixture.wav", media_type="audio/wav")
        assert "具体内容" in first.reply and "周一学习英语" in first.reply
        assert not tool.calls and not actions.jobs
        session.acknowledge(first.turn_id, played_ratio=0.25)
        actions.handoff_reference("chat-source")
        second = await session.turn(b"fixture", filename="fixture.wav", media_type="audio/wav")
        assert "具体内容" in second.reply and not tool.calls and not actions.jobs
        session.acknowledge(second.turn_id, played_ratio=1)
        actions.handoff_reference("chat-source")
        third = await session.turn(b"fixture", filename="fixture.wav", media_type="audio/wav")
        assert "开始执行" in third.reply
        session.acknowledge(third.turn_id, played_ratio=1)
        await asyncio.gather(*tuple(actions.jobs.values()))
        assert len(tool.calls) == 1 and agent.store.get(task.id).state == TaskState.COMPLETE
        assert actions._binding("chat-source") == source_binding
        assert context.reference_messages()[0].role == "user"
        await actions.aclose()

    asyncio.run(run())


def test_handoff_confirmation_is_rejected_without_tts_even_with_old_source_preview(tmp_path):
    async def run():
        actions, agent, tool = fixture(tmp_path)
        task, preview = bind_task(actions, agent, "chat-source")
        agent.approve(task.id, task.plan_hash)
        actions.handoff_reference("chat-source")
        reply = await actions.process(
            "确认执行", [ModelMessage(role="assistant", content=preview)], "voice-main",
            allow_confirmation=False,
        )
        assert "语音合成尚未就绪" in reply
        assert actions._binding("voice-main")["preview_text"] == ""
        assert agent.store.get(task.id).state == TaskState.READY
        assert not tool.calls and not actions.jobs
        await actions.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("state", [
    TaskState.WAITING_APPROVAL, TaskState.READY, TaskState.RUNNING, TaskState.NEEDS_RECONCILIATION,
])
def test_different_unfinished_voice_task_is_not_overwritten(tmp_path, state):
    actions, agent, tool = fixture(tmp_path)
    source, _ = bind_task(actions, agent, "chat-source")
    target, _ = bind_task(actions, agent, "voice-main", task_id="voice-owned", state=state)
    target_binding = actions._binding("voice-main")
    with pytest.raises(ValueError, match="不同的未完成任务"):
        actions.handoff_reference("chat-source")
    assert actions._binding("voice-main") == target_binding
    assert agent.store.get(target.id).state == state
    assert agent.store.get(source.id).state == TaskState.WAITING_APPROVAL
    actions.handoff_reference(None)
    assert actions._binding("voice-main") == target_binding
    assert not tool.calls


@pytest.mark.parametrize("source", [None, "chat-without-plan"])
def test_clearing_handoff_only_detaches_reference_and_does_not_cancel_source(tmp_path, source):
    actions, agent, tool = fixture(tmp_path)
    task, _ = bind_task(actions, agent, "chat-source")
    source_binding = actions._binding("chat-source")
    actions.handoff_reference("chat-source")
    actions.handoff_reference(source)
    assert actions._binding("voice-main") is None
    assert actions._binding("chat-source") == source_binding
    assert agent.store.get(task.id).state == TaskState.WAITING_APPROVAL
    assert not tool.calls


def test_new_voice_owned_binding_survives_unset_and_empty_source(tmp_path):
    actions, agent, _ = fixture(tmp_path)
    source, _ = bind_task(actions, agent, "chat-source")
    actions.handoff_reference("chat-source")
    source.state = TaskState.FAILED
    agent.store.save(source)
    own, _ = bind_task(actions, agent, "voice-main", task_id="voice-owned")
    binding = actions._binding("voice-main")
    actions.handoff_reference(None)
    actions.handoff_reference("chat-without-plan")
    assert actions._binding("voice-main") == binding
    assert agent.store.get(own.id).state == TaskState.WAITING_APPROVAL


def test_existing_voice_owned_same_task_is_not_converted_to_reference(tmp_path):
    actions, agent, _ = fixture(tmp_path)
    task, _ = bind_task(actions, agent, "voice-main", task_id="voice-owned")
    actions._show("chat-source", task)
    binding = actions._binding("voice-main")
    actions.handoff_reference("chat-source")
    actions.handoff_reference(None)
    assert actions._binding("voice-main") == binding


@pytest.mark.parametrize("state", [TaskState.COMPLETE, TaskState.FAILED])
def test_terminal_source_task_can_be_read_without_starting_or_reapproving(tmp_path, state):
    async def run():
        actions, agent, tool = fixture(tmp_path)
        task, _ = bind_task(actions, agent, "chat-source", state=state)
        actions.handoff_reference("chat-source")
        progress = await actions.process("任务进度", [], "voice-main")
        confirmation = await actions.process("确认执行", [], "voice-main")
        assert progress == confirmation
        assert agent.store.get(task.id).state == state
        assert actions._binding("voice-main")["preview_text"] == ""
        assert not tool.calls and not actions.jobs
        await actions.aclose()

    asyncio.run(run())


def test_missing_source_task_record_drops_only_old_reference(tmp_path, monkeypatch):
    actions, agent, _ = fixture(tmp_path)
    task, _ = bind_task(actions, agent, "chat-source")
    actions.handoff_reference("chat-source")
    original_get = agent.store.get

    def get_without_source(identity):
        if identity == task.id:
            raise KeyError(identity)
        return original_get(identity)

    monkeypatch.setattr(agent.store, "get", get_without_source)
    actions.handoff_reference("chat-source")
    assert actions._binding("voice-main") is None


def test_handoff_origin_survives_restart_without_importing_source_preview(tmp_path):
    actions, agent, _ = fixture(tmp_path)
    task, _ = bind_task(actions, agent, "chat-source")
    actions.handoff_reference("chat-source")
    restarted = DialogueActions(NoModel(), actions.companion, agent, path=actions.path)
    restarted.handoff_reference("chat-source")
    assert restarted._binding("voice-main")["preview_text"] == ""
    assert restarted._binding("voice-main")["task_id"] == task.id
    restarted.handoff_reference(None)
    assert restarted._binding("voice-main") is None
    assert restarted._binding("chat-source")["task_id"] == task.id
