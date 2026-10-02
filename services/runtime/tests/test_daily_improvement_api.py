"""HTTP contracts for daily improvements, with isolated databases and fake services."""

import asyncio
import base64
from datetime import UTC, datetime
from functools import lru_cache
from types import SimpleNamespace

import pytest

from ella_runtime import api
from ella_runtime.modules.agent.contracts import (
    AgentTask,
    StepState,
    TaskState,
    TaskStep,
    ToolEvidence,
)
from ella_runtime.modules.agent.engine import AgentEngine
from ella_runtime.modules.agent.store import TaskStore
from ella_runtime.modules.companion.dialogue import DialogueActions
from ella_runtime.modules.companion.store import CompanionStore
from ella_runtime.modules.models.contracts import ModelPurpose, TokenUsage
from ella_runtime.modules.models.conversations import ConversationStore
from ella_runtime.modules.models.settings import ModelSettings, ProviderConfig
from ella_runtime.modules.models.usage_store import UsageStore
from ella_runtime.modules.voice.context import VoiceContext
from ella_runtime.modules.voice.history import VoiceHistory
from ella_runtime.modules.voice.provider import Speech, VoiceProviderError
from ella_runtime.modules.voice.session import VoiceSession
from tests.api_client import authorized_client


class NoModel:
    async def generate(self, _request):
        raise AssertionError("This HTTP test must not issue generation requests")


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.error = None
        self.audio = b"isolated-tts-fixture"

    async def synthesize(self, text, *, task_id=None):
        self.calls.append((text, task_id))
        if self.error:
            raise VoiceProviderError(self.error)
        return Speech(self.audio, "audio/mpeg", TokenUsage(
            provider="test", model="fake-tts", purpose=ModelPurpose.VOICE,
            occurred_at=datetime.now(UTC), task_id=task_id,
        ))


class FakeTool:
    name = "office.create_document"
    description = "Isolated test tool"

    async def execute(self, _arguments, *, action_id):
        raise AssertionError("API revisions must not run a tool")

    async def reconcile(self, _arguments, *, action_id):
        return ToolEvidence(verified=False)


@pytest.fixture
def daily_api(tmp_path, monkeypatch):
    monkeypatch.setenv("ELLA_DATA_DIR", str(tmp_path))
    store = ConversationStore(tmp_path / "chats.sqlite3")
    context = VoiceContext(store)
    usage = UsageStore(tmp_path / "usage.sqlite3")
    companion = CompanionStore(tmp_path / "companion.sqlite3")
    agent = AgentEngine(NoModel(), TaskStore(tmp_path / "tasks.sqlite3"), [FakeTool()])
    actions = DialogueActions(NoModel(), companion, agent, path=tmp_path / "dialogue.sqlite3")
    provider = FakeProvider()
    session = VoiceSession(provider, NoModel(), usage, tts=True,
                           history=VoiceHistory(store), dialogue_actions=actions, context=context)

    @lru_cache(maxsize=1)
    def get_session():
        return session

    get_session()
    monkeypatch.setattr(api, "get_voice_session", get_session)
    monkeypatch.setattr(api, "get_dialogue_actions", lambda: actions)
    monkeypatch.setattr(api, "get_usage_store", lambda: usage)
    monkeypatch.setattr(api, "_active_voice_sockets", 0)
    monkeypatch.setattr(api, "_notification_lock", asyncio.Lock())
    dependency = api.get_agent_engine
    prior_override = api.app.dependency_overrides.get(dependency)
    api.app.dependency_overrides[dependency] = lambda: agent
    client = authorized_client(api.app)
    try:
        yield SimpleNamespace(client=client, store=store, context=context, session=session,
                              usage=usage, agent=agent, actions=actions, provider=provider)
    finally:
        client.close()
        if prior_override is None:
            api.app.dependency_overrides.pop(dependency, None)
        else:
            api.app.dependency_overrides[dependency] = prior_override


def bind_plan(fixture, conversation, *, task_id="source-task"):
    now = datetime.now(UTC)
    task = AgentTask(
        id=task_id, goal="创建学习安排.docx", created_at=now, updated_at=now,
        state=TaskState.WAITING_APPROVAL,
        steps=[TaskStep(id=f"step-{task_id}", tool="office.create_document", arguments={
            "path": "学习安排.docx", "title": "学习安排", "paragraphs": ["周一学习英语"],
        })],
    )
    fixture.agent.store.save(task)
    fixture.actions._show(conversation, task)
    return task


def test_context_http_save_restore_and_invalid_sources_do_not_replace_valid_selection(daily_api):
    chat = daily_api.store.create()
    response = daily_api.client.put("/api/voice/context", json={"conversation_id": chat})
    assert response.status_code == 200 and response.json() == {"conversation_id": chat}
    assert daily_api.client.get("/api/voice/context").json() == {"conversation_id": chat}
    assert VoiceContext(daily_api.store).get_context_source() == chat
    for invalid in ("missing", "voice-main"):
        rejected = daily_api.client.put("/api/voice/context", json={"conversation_id": invalid})
        assert rejected.status_code == 409
        assert daily_api.context.get_context_source() == chat
    daily_api.store.set_archived(chat, True)
    assert daily_api.client.get("/api/voice/context").json() == {"conversation_id": None}


def test_context_http_binds_actual_task_without_importing_source_approval_or_preview(daily_api):
    chat = daily_api.store.create()
    task = bind_plan(daily_api, chat)
    daily_api.agent.approve(task.id, task.plan_hash)
    source_binding = daily_api.actions._binding(chat)
    response = daily_api.client.put("/api/voice/context", json={"conversation_id": chat})
    assert response.status_code == 200
    binding = daily_api.actions._binding("voice-main")
    assert binding is not None and binding["task_id"] == task.id
    assert binding["preview_text"] == "" and binding["full_preview"] == 0
    assert daily_api.actions._binding(chat) == source_binding
    assert not daily_api.actions.jobs


def test_context_conflict_is_http_409_and_does_not_change_reference_or_voice_task(daily_api):
    old_chat, new_chat = daily_api.store.create(), daily_api.store.create()
    daily_api.context.set_context_source(old_chat)
    bind_plan(daily_api, new_chat)
    owned = bind_plan(daily_api, "voice-main", task_id="voice-owned")
    before = daily_api.actions._binding("voice-main")
    response = daily_api.client.put("/api/voice/context", json={"conversation_id": new_chat})
    assert response.status_code == 409
    assert daily_api.context.get_context_source() == old_chat
    assert daily_api.actions._binding("voice-main") == before
    assert daily_api.agent.store.get(owned.id).state == TaskState.WAITING_APPROVAL


def test_context_busy_does_not_save_and_unset_keeps_voice_owned_task(daily_api, monkeypatch):
    chat = daily_api.store.create()
    monkeypatch.setattr(api, "_active_voice_sockets", 1)
    assert daily_api.client.put("/api/voice/context", json={"conversation_id": chat}).status_code == 409
    assert daily_api.context.get_context_source() is None
    monkeypatch.setattr(api, "_active_voice_sockets", 0)
    task = bind_plan(daily_api, "voice-main", task_id="voice-owned")
    assert daily_api.client.put("/api/voice/context", json={"conversation_id": None}).status_code == 200
    assert daily_api.actions._binding("voice-main")["task_id"] == task.id


def test_voice_preview_returns_audio_and_usage_without_writing_voice_history(daily_api):
    before = daily_api.session.history.recent()
    response = daily_api.client.post("/api/voice/preview")
    assert response.status_code == 200
    assert base64.b64decode(response.json()["audio_base64"]) == daily_api.provider.audio
    assert daily_api.provider.calls[0][1] == "voice-preview"
    assert daily_api.usage.summary()["totals"]["requests"] == 1
    assert daily_api.session.history.recent() == before
    assert not api._notification_lock.locked()


@pytest.mark.parametrize("failure", ["provider", "empty"])
def test_voice_preview_failures_release_lock_without_fake_usage(daily_api, failure):
    if failure == "provider":
        daily_api.provider.error = "模拟合成失败"
    else:
        daily_api.provider.audio = b""
    response = daily_api.client.post("/api/voice/preview")
    assert response.status_code == 502
    assert not api._notification_lock.locked()
    assert daily_api.usage.summary()["totals"]["requests"] == 0
    assert daily_api.session.history.recent() == []


def test_voice_preview_disabled_or_busy_does_not_call_tts(daily_api, monkeypatch):
    daily_api.session.tts = False
    assert daily_api.client.post("/api/voice/preview").status_code == 409
    daily_api.session.tts = True
    monkeypatch.setattr(api, "_active_voice_sockets", 1)
    assert daily_api.client.post("/api/voice/preview").status_code == 409
    assert daily_api.provider.calls == []


def test_model_probe_http_uses_action_fallback_and_returns_metadata_only(daily_api, monkeypatch):
    from ella_runtime.modules.models import diagnostics

    config = ProviderConfig("fake", "configured-model", "https://fixture.example/v1", "private-key")
    settings = ModelSettings(chat=config)
    dependency = api.get_model_config_store
    api.app.dependency_overrides[dependency] = lambda: SimpleNamespace(settings=lambda: settings)
    calls = []

    async def fake_probe(value):
        calls.append(value)
        return {"reachable": True, "model_found": True, "model_count": 1, "detail": "fixture"}

    monkeypatch.setattr(diagnostics, "probe_model", fake_probe)
    try:
        response = daily_api.client.post("/api/models/action/probe")
        assert response.status_code == 200 and response.json()["model_found"] is True
        assert calls == [config]
        assert "private-key" not in response.text
        assert not daily_api.actions.jobs
    finally:
        api.app.dependency_overrides.pop(dependency, None)


def test_plan_revision_http_invalidates_approval_and_rejects_stale_hash(daily_api):
    task = bind_plan(daily_api, "chat")
    daily_api.agent.approve(task.id, task.plan_hash)
    step = task.steps[0]
    revision = {"plan_hash": task.plan_hash, "steps": [{
        "tool": step.tool, "arguments": {**step.arguments, "paragraphs": ["周二学习数学"]}, "reason": "用户修订",
    }]}
    response = daily_api.client.put(f"/api/tasks/{task.id}/plan", json=revision)
    assert response.status_code == 200
    updated = response.json()
    assert updated["state"] == "waiting_approval" and updated["approved_plan_hash"] is None
    assert updated["plan_hash"] != task.plan_hash
    assert updated["steps"][0]["arguments"]["paragraphs"] == ["周二学习数学"]
    assert daily_api.client.put(f"/api/tasks/{task.id}/plan", json=revision).status_code == 409
    assert daily_api.agent.store.get(task.id).plan_hash == updated["plan_hash"]
    assert not daily_api.actions.jobs


def test_plan_revision_http_rejects_unknown_result_without_replacing_evidence(daily_api):
    task = bind_plan(daily_api, "chat")
    task.state = TaskState.NEEDS_RECONCILIATION
    task.steps[0].state = StepState.NEEDS_RECONCILIATION
    task.steps[0].evidence = {"action_id": "actual-issued-action", "write_may_have_started": True}
    daily_api.agent.store.save(task)
    response = daily_api.client.put(f"/api/tasks/{task.id}/plan", json={
        "plan_hash": task.plan_hash,
        "steps": [{"tool": task.steps[0].tool, "arguments": task.steps[0].arguments}],
    })
    assert response.status_code == 409
    persisted = daily_api.agent.store.get(task.id)
    assert persisted.state == TaskState.NEEDS_RECONCILIATION
    assert persisted.steps[0].evidence == task.steps[0].evidence


def test_missing_task_revision_and_invalid_input_have_safe_http_errors(daily_api):
    payload = {"plan_hash": "a" * 64, "steps": [{"tool": "office.create_document", "arguments": {}}]}
    assert daily_api.client.put("/api/tasks/missing/plan", json=payload).status_code == 404
    assert daily_api.client.put("/api/tasks/missing/plan", json={**payload, "plan_hash": "bad"}).status_code == 422


def test_voice_rechecks_actual_source_task_each_turn_and_detaches_archived_source(daily_api):
    async def run():
        chat = daily_api.store.create()
        daily_api.context.set_context_source(chat)
        assert "没有绑定" in await daily_api.session.action_reply("任务进度")
        task = bind_plan(daily_api, chat)
        status = await daily_api.session.action_reply("任务进度")
        assert "等待确认" in status
        assert daily_api.actions._binding("voice-main")["task_id"] == task.id
        daily_api.store.set_archived(chat, True)
        assert "没有绑定" in await daily_api.session.action_reply("任务进度")
        assert daily_api.actions._binding("voice-main") is None
        assert daily_api.agent.store.get(task.id).state == TaskState.WAITING_APPROVAL

    asyncio.run(run())


def test_streamed_pause_precedes_failed_context_lookup(daily_api, monkeypatch):
    from ella_runtime.modules.voice.provider import Transcription
    from ella_runtime.modules.voice.streaming import StreamingVoiceTurn

    async def run():
        usage = TokenUsage(provider="test", model="fake-asr", purpose=ModelPurpose.VOICE,
                           occurred_at=datetime.now(UTC))

        async def transcribe(_audio, **_kwargs):
            return Transcription("暂停监听", usage)

        def broken_reference():
            raise RuntimeError("参考库不可用")

        monkeypatch.setattr(daily_api.provider, "transcribe", transcribe, raising=False)
        monkeypatch.setattr(daily_api.session, "context_messages", broken_reference)
        events = []

        async def send(value):
            events.append(value)

        turn = StreamingVoiceTurn(daily_api.session, send)
        await turn.feed(b"\0\0" * 80)
        await turn.finish_input()
        await turn._reply_task
        assert any(event["type"] == "listening_pause" for event in events)
        assert not any(event["type"] == "error" for event in events)
        await turn.interrupt()

    asyncio.run(run())


def test_unexpected_stream_failure_returns_safe_error_to_release_client(daily_api, monkeypatch):
    from ella_runtime.modules.voice.streaming import StreamingVoiceTurn

    async def run():
        async def failed_transcribe(_audio, **_kwargs):
            raise RuntimeError("内部凭据路径不能暴露")

        monkeypatch.setattr(daily_api.provider, "transcribe", failed_transcribe, raising=False)
        events = []

        async def send(value):
            events.append(value)

        turn = StreamingVoiceTurn(daily_api.session, send)
        await turn.feed(b"\0\0" * 80)
        await turn.finish_input()
        await turn._reply_task
        assert daily_api.session.state.value == "error"
        assert len(events) == 1 and events[0]["type"] == "error"
        assert "内部凭据路径" not in events[0]["message"]
        await turn.interrupt()

    asyncio.run(run())
