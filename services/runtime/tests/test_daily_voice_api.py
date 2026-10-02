"""Daily voice setup and notification playback, using isolated data and fake services."""

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from ella_runtime import api
from ella_runtime.modules.companion.store import CompanionStore
from ella_runtime.modules.models.contracts import ModelPurpose, TokenUsage
from ella_runtime.modules.models.conversations import ConversationStore
from ella_runtime.modules.models.settings import ModelSettings
from ella_runtime.modules.models.usage_store import UsageStore
from ella_runtime.modules.voice.config_store import VoiceConfigStore
from ella_runtime.modules.voice.fish import FishSpeechProvider
from ella_runtime.modules.voice.provider import OpenAICompatibleVoiceProvider, Speech
from ella_runtime.modules.voice.session import VoiceState
from tests.api_client import authorized_client


class MemoryCredentials:
    def __init__(self):
        self.values = {}

    def get(self, identity):
        return self.values.get(identity)

    def set(self, identity, value):
        self.values[identity] = value

    def delete(self, identity):
        self.values.pop(identity, None)


@pytest.fixture
def voice_api(tmp_path, monkeypatch):
    for name in (
        "ELLA_VOICE_API_KEY", "ELLA_ASR_API_KEY", "ELLA_TTS_API_KEY", "ELLA_FISH_API_KEY",
        "ELLA_ASR_BASE_URL", "ELLA_TTS_BASE_URL", "ELLA_TTS_PROVIDER",
        "ELLA_FISH_REFERENCE_ID", "ELLA_FISH_TTS_MODEL", "ELLA_FISH_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ELLA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ELLA_VOICE_BASE_URL", "https://default.example/v1")
    monkeypatch.setenv("ELLA_STT_MODEL", "")
    monkeypatch.setenv("ELLA_TTS_MODEL", "")
    monkeypatch.setenv("ELLA_TTS_VOICE", "")
    credentials = MemoryCredentials()
    store = VoiceConfigStore(tmp_path / "voice.sqlite3", credentials)
    usage = UsageStore(tmp_path / "usage.sqlite3")
    companion = CompanionStore(tmp_path / "companion.sqlite3")
    companion.set_quiet_hours("00:00", "00:00")
    original_session = api.get_voice_session
    original_session.cache_clear()
    monkeypatch.setattr(api, "get_voice_config_store", lambda: store)
    monkeypatch.setattr(api, "get_usage_store", lambda: usage)
    monkeypatch.setattr(api, "get_companion_store", lambda: companion)
    monkeypatch.setattr(api, "get_model_config_store", lambda: SimpleNamespace(
        settings=ModelSettings.from_env,
    ))
    monkeypatch.setattr(api, "get_memory_retriever", lambda: None)
    monkeypatch.setattr(api, "get_memory_store", lambda: None)
    monkeypatch.setattr(api, "get_web_research", lambda: SimpleNamespace(context=None))
    monkeypatch.setattr(api, "get_dialogue_actions", lambda: None)
    conversations = ConversationStore(tmp_path / "conversations.sqlite3")
    monkeypatch.setattr(api, "ConversationStore", lambda: conversations)
    monkeypatch.setattr(api, "MemorySummaryExtractor", lambda *_: None)
    monkeypatch.setattr(api, "VoiceHistory", lambda *_: None)
    monkeypatch.setattr(api, "_active_voice_sockets", 0)
    monkeypatch.setattr(api, "_notification_audio", {})
    monkeypatch.setattr(api, "_notification_lock", asyncio.Lock())
    client = authorized_client(api.app)
    try:
        yield SimpleNamespace(client=client, store=store, credentials=credentials,
                              usage=usage, companion=companion)
    finally:
        client.close()
        original_session.cache_clear()


def save_asr(client):
    return client.put("/api/voice/config/asr", json={
        "provider": "openai", "base_url": "https://asr.example/v1",
        "model": "recognition", "api_key": "asr-private-key",
    })


def save_tts(client, *, provider="openai"):
    return client.put("/api/voice/config/tts", json={
        "provider": provider,
        "base_url": "https://tts.example/v1" if provider == "openai" else "https://fish.example",
        "model": "synthesis", "voice": "voice-reference", "api_key": "tts-private-key",
    })


@pytest.mark.parametrize("tts_provider", ["openai", "fish"])
def test_config_api_selects_independent_providers_and_survives_reload(voice_api, tts_provider):
    client = voice_api.client
    assert save_asr(client).status_code == 200
    saved = save_tts(client, provider=tts_provider)
    assert saved.status_code == 200
    assert saved.json()["asr"]["key_saved"] is True
    assert saved.json()["tts"]["source"] == "saved"
    assert "private-key" not in saved.text
    session = api.get_voice_session()
    assert isinstance(session.provider.recognizer, OpenAICompatibleVoiceProvider)
    assert session.provider.recognizer.settings.base_url == "https://asr.example/v1"
    assert session.provider.recognizer.settings.api_key == "asr-private-key"
    if tts_provider == "fish":
        assert isinstance(session.provider.speaker, FishSpeechProvider)
        assert session.provider.speaker.settings.reference_id == "voice-reference"
    else:
        assert isinstance(session.provider.speaker, OpenAICompatibleVoiceProvider)
        assert session.provider.speaker.settings.base_url == "https://tts.example/v1"
    assert session.provider.speaker.settings.api_key == "tts-private-key"
    assert session.tts is True
    status = client.get("/api/voice/status")
    assert status.status_code == 200
    assert status.json()["can_transcribe"] is True
    assert status.json()["can_speak"] is True
    assert status.json()["speech_provider"] == tts_provider
    assert "private-key" not in status.text
    restarted = VoiceConfigStore(voice_api.store.path, voice_api.credentials)
    assert restarted.public_status() == client.get("/api/voice/config").json()
    assert restarted.asr_settings().api_key == "asr-private-key"
    assert restarted.tts_settings().api_key == "tts-private-key"


def test_save_invalidates_voice_session_and_restores_environment(voice_api):
    client = voice_api.client
    assert save_asr(client).status_code == 200
    first = api.get_voice_session()
    assert save_tts(client).status_code == 200
    second = api.get_voice_session()
    assert first is not second
    assert second.tts is True
    reset = client.delete("/api/voice/config/tts")
    assert reset.status_code == 200
    assert reset.json()["tts"]["source"] == "environment"
    assert reset.json()["tts"]["key_saved"] is False
    assert api.get_voice_session().tts is False
    assert "tts-private-key" not in voice_api.credentials.values.values()


@pytest.mark.parametrize("state", [VoiceState.TRANSCRIBING, VoiceState.THINKING, VoiceState.SPEAKING])
def test_config_changes_are_rejected_during_a_voice_turn(voice_api, state):
    api.get_voice_session().state = state
    client = voice_api.client
    before = voice_api.store.public_status()
    assert save_asr(client).status_code == 409
    assert client.delete("/api/voice/config/tts").status_code == 409
    assert voice_api.store.public_status() == before
    assert voice_api.credentials.values == {}


@pytest.mark.parametrize("busy_source", ["listener", "notification"])
def test_config_changes_wait_for_listening_and_notification_audio(voice_api, monkeypatch, busy_source):
    if busy_source == "listener":
        monkeypatch.setattr(api, "_active_voice_sockets", 1)
    else:
        monkeypatch.setattr(api, "_notification_lock", SimpleNamespace(locked=lambda: True))
    assert save_asr(voice_api.client).status_code == 409
    assert voice_api.client.delete("/api/voice/config/asr").status_code == 409
    assert voice_api.store.public_status()["asr"]["source"] == "environment"


@pytest.mark.parametrize("invalid_field", ["base_url", "model", "api_key"])
def test_validation_never_returns_submitted_credentials(voice_api, invalid_field):
    secret = "secret-must-never-appear"
    payload = {"provider": "openai", "base_url": "https://asr.example/v1",
               "model": "recognition", "api_key": secret}
    payload[invalid_field] = {
        "base_url": f"https://user:{secret}@asr.example/v1?key={secret}",
        "model": "",
        "api_key": secret * 200,
    }[invalid_field]
    response = voice_api.client.put("/api/voice/config/asr", json=payload)
    assert response.status_code == 422
    assert secret not in response.text
    assert all(set(item) == {"loc", "type", "msg"} for item in response.json()["detail"])
    assert voice_api.credentials.values == {}


class FakeSpeaker:
    def __init__(self):
        self.calls = []

    async def synthesize(self, text, *, task_id=None):
        self.calls.append((text, task_id))
        return Speech(b"synthetic-mp3", "audio/mpeg", TokenUsage(
            provider="fake", model="fake-tts", purpose=ModelPurpose.VOICE,
            occurred_at=datetime.now(UTC), task_id=task_id,
        ))


def due_notification(store):
    reminder = store.add_reminder("喝水", datetime.now(UTC) - timedelta(minutes=1))
    assert any(event["id"] == reminder["id"] for event in store.poll_events())
    return reminder["id"]


def test_notification_speech_caches_audio_until_played_and_preserves_unread(voice_api):
    client = voice_api.client
    assert save_tts(client).status_code == 200
    session = api.get_voice_session()
    speaker = FakeSpeaker()
    session.provider = speaker
    identity = due_notification(voice_api.companion)
    route = f"/api/companion/notifications/{identity}"
    first = client.post(f"{route}/speech")
    cached = client.post(f"{route}/speech")
    assert first.status_code == cached.status_code == 200
    assert first.json() == cached.json()
    assert base64.b64decode(first.json()["audio_base64"]) == b"synthetic-mp3"
    assert speaker.calls == [("喝水", f"notification:{identity}")]
    assert voice_api.usage.summary()["totals"]["requests"] == 1
    failed = client.post(f"{route}/spoken", json={"status": "failed", "error": "扬声器未连接"})
    assert failed.status_code == 200
    persisted = CompanionStore(voice_api.companion.path).get_notification(identity)
    assert persisted["read_at"] is None
    assert persisted["spoken_at"] is None
    assert persisted["speech_error"] == "扬声器未连接"
    assert client.post(f"{route}/speech").json() == first.json()
    assert len(speaker.calls) == 1
    played = client.post(f"{route}/spoken", json={"status": "played"})
    assert played.status_code == 200
    persisted = CompanionStore(voice_api.companion.path).get_notification(identity)
    assert persisted["read_at"] is None
    assert persisted["spoken_at"] is not None
    assert persisted["speech_error"] is None
    assert identity not in api._notification_audio
    assert client.post(f"{route}/speech").status_code == 409
    assert len(speaker.calls) == 1


def test_switching_speech_service_discards_cached_notification_audio(voice_api):
    client = voice_api.client
    assert save_tts(client).status_code == 200
    first_speaker = FakeSpeaker()
    api.get_voice_session().provider = first_speaker
    identity = due_notification(voice_api.companion)
    route = f"/api/companion/notifications/{identity}/speech"
    assert client.post(route).status_code == 200
    assert identity in api._notification_audio
    assert save_tts(client, provider="fish").status_code == 200
    assert identity not in api._notification_audio
    second_speaker = FakeSpeaker()
    api.get_voice_session().provider = second_speaker
    assert client.post(route).status_code == 200
    assert len(first_speaker.calls) == len(second_speaker.calls) == 1
    assert voice_api.usage.summary()["totals"]["requests"] == 2


@pytest.mark.parametrize("handled_as", ["read", "played"])
def test_waiting_notification_is_rechecked_before_synthesis(voice_api, handled_as):
    assert save_tts(voice_api.client).status_code == 200
    speaker = FakeSpeaker()
    api.get_voice_session().provider = speaker
    identity = due_notification(voice_api.companion)

    async def run():
        await api._notification_lock.acquire()
        pending = asyncio.create_task(api.notification_speech(identity))
        await asyncio.sleep(0)
        assert not pending.done()
        if handled_as == "read":
            voice_api.companion.mark_notification_read(identity)
        else:
            voice_api.companion.speech_receipt(identity, "played")
        api._notification_lock.release()
        with pytest.raises(HTTPException) as rejected:
            await pending
        assert rejected.value.status_code == 409

    asyncio.run(run())
    assert speaker.calls == []
    assert identity not in api._notification_audio


@pytest.mark.parametrize("blocked_by", ["thinking", "no_tts"])
def test_waiting_notification_rechecks_speech_availability(voice_api, blocked_by):
    assert save_tts(voice_api.client).status_code == 200
    session = api.get_voice_session()
    speaker = FakeSpeaker()
    session.provider = speaker
    identity = due_notification(voice_api.companion)

    async def run():
        await api._notification_lock.acquire()
        pending = asyncio.create_task(api.notification_speech(identity))
        await asyncio.sleep(0)
        assert not pending.done()
        if blocked_by == "thinking":
            session.state = VoiceState.THINKING
        else:
            session.tts = False
        api._notification_lock.release()
        with pytest.raises(HTTPException) as rejected:
            await pending
        assert rejected.value.status_code == 409

    asyncio.run(run())
    assert speaker.calls == []
    assert identity not in api._notification_audio
    assert voice_api.usage.summary()["totals"]["requests"] == 0


@pytest.mark.parametrize("blocked_by", ["quiet", "busy", "no_tts", "read"])
def test_notifications_do_not_synthesize_when_ineligible(voice_api, blocked_by):
    client = voice_api.client
    assert save_tts(client).status_code == 200
    session = api.get_voice_session()
    speaker = FakeSpeaker()
    session.provider = speaker
    identity = due_notification(voice_api.companion)
    if blocked_by == "quiet":
        voice_api.companion.status = lambda **_: {"quiet_now": True}
    elif blocked_by == "busy":
        session.state = VoiceState.THINKING
    elif blocked_by == "no_tts":
        session.tts = False
    else:
        assert voice_api.companion.mark_notification_read(identity)
    response = client.post(f"/api/companion/notifications/{identity}/speech")
    assert response.status_code == 409
    assert speaker.calls == []
    assert voice_api.usage.summary()["totals"]["requests"] == 0
