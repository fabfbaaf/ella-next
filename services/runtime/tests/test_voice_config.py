import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from ella_runtime.modules.voice.config_store import (
    VoiceConfigError,
    VoiceConfigInput,
    VoiceConfigStore,
)
from ella_runtime.modules.voice.fish import FishSpeechSettings
from ella_runtime.modules.voice.provider import OpenAICompatibleVoiceProvider


class MemoryCredentials:
    def __init__(self):
        self.values = {}

    def get(self, identity):
        return self.values.get(identity)

    def set(self, identity, value):
        self.values[identity] = value

    def delete(self, identity):
        self.values.pop(identity, None)


def test_separate_voice_services_survive_restart_and_keep_keys_private(tmp_path):
    credentials = MemoryCredentials()
    path = tmp_path / "voice.sqlite3"
    store = VoiceConfigStore(path, credentials)
    store.save("asr", VoiceConfigInput(
        base_url="https://asr.example/v1", model="recognition", api_key="asr-secret",
    ))
    status = store.save("tts", VoiceConfigInput(
        base_url="https://tts.example/v1", model="speech", voice="voice-a", api_key="tts-secret",
    ))
    assert status["asr"]["configured"] is True
    assert status["tts"]["configured"] is True
    assert status["tts"]["source"] == "saved"
    assert "secret" not in json.dumps(status)
    assert b"asr-secret" not in path.read_bytes()
    assert b"tts-secret" not in path.read_bytes()
    restarted = VoiceConfigStore(path, credentials)
    assert restarted.asr_settings().api_key == "asr-secret"
    assert restarted.tts_settings().api_key == "tts-secret"

    requests = []

    def respond(request):
        requests.append(request)
        if request.url.host == "asr.example":
            return httpx.Response(200, json={"text": "你好"})
        return httpx.Response(200, content=b"audio")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await OpenAICompatibleVoiceProvider(restarted.asr_settings(), client).transcribe(
                b"recording", filename="recording.webm", media_type="audio/webm",
            )
            await OpenAICompatibleVoiceProvider(restarted.tts_settings(), client).synthesize("你好")

    asyncio.run(run())
    assert requests[0].url.host == "asr.example"
    assert requests[0].headers["authorization"] == "Bearer asr-secret"
    assert requests[1].url.host == "tts.example"
    assert requests[1].headers["authorization"] == "Bearer tts-secret"


def test_voice_key_never_moves_to_another_origin_or_revives_stale_key(tmp_path):
    credentials = MemoryCredentials()
    store = VoiceConfigStore(tmp_path / "voice.sqlite3", credentials)
    value = VoiceConfigInput(base_url="https://a.example/v1", model="asr", api_key="a-secret")
    store.save("asr", value)
    store.save("asr", value.model_copy(update={"api_key": None, "base_url": "https://A.example:443/v2"}))
    assert store.asr_settings().api_key == "a-secret"
    credentials.set("asr@https://b.example", "stale-b-secret")
    store.save("asr", value.model_copy(update={"api_key": None, "base_url": "https://b.example/v1"}))
    assert store.asr_settings().api_key is None
    assert store.public_status()["asr"]["key_saved"] is False
    assert "a-secret" not in credentials.values.values()
    store.save("asr", value.model_copy(update={"api_key": None, "base_url": "https://b.example/v2"}))
    assert store.asr_settings().api_key is None
    store.save("asr", value.model_copy(update={"api_key": "b-new", "base_url": "https://b.example/v1"}))
    store.save("asr", value.model_copy(update={"api_key": None, "base_url": "https://b.example:8443/v1"}))
    assert store.asr_settings().api_key is None


def test_environment_keys_are_retained_only_on_same_origin_and_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("ELLA_VOICE_BASE_URL", "https://environment.example/v1")
    monkeypatch.setenv("ELLA_VOICE_API_KEY", "environment-key")
    monkeypatch.setenv("ELLA_STT_MODEL", "env-asr")
    monkeypatch.setenv("ELLA_TTS_MODEL", "env-tts")
    monkeypatch.setenv("ELLA_TTS_VOICE", "env-voice")
    monkeypatch.delenv("ELLA_TTS_PROVIDER", raising=False)
    credentials = MemoryCredentials()
    store = VoiceConfigStore(tmp_path / "voice.sqlite3", credentials)
    assert store.public_status()["asr"]["source"] == "environment"
    store.save("asr", VoiceConfigInput(base_url="https://environment.example/v2", model="new"))
    assert store.asr_settings().api_key == "environment-key"
    assert store.public_status()["asr"]["key_saved"] is True
    store.save("asr", VoiceConfigInput(base_url="https://environment.example/v2", model="new", api_key=""))
    assert store.asr_settings().api_key is None
    status = store.reset("asr")
    assert status["asr"]["source"] == "environment"
    assert store.asr_settings().api_key == "environment-key"
    assert credentials.values == {}
    store.save("asr", VoiceConfigInput(base_url="https://other.example/v1", model="new"))
    assert store.asr_settings().api_key is None


def test_local_keyless_config_does_not_require_a_credential_vault(tmp_path):
    class UnavailableCredentials:
        def get(self, identity):
            raise AssertionError("keyless local settings must not access credentials")

        set = delete = get

    store = VoiceConfigStore(tmp_path / "voice.sqlite3", UnavailableCredentials())
    status = store.save("asr", VoiceConfigInput(base_url="http://127.0.0.1:8000/v1", model="local"))
    assert status["asr"]["configured"] is True
    assert store.asr_settings().api_key is None
    store.reset("asr")


def test_fish_tts_is_independent_and_incomplete_config_is_rejected(tmp_path):
    store = VoiceConfigStore(tmp_path / "voice.sqlite3", MemoryCredentials())
    store.save("asr", VoiceConfigInput(base_url="http://localhost:9000/v1", model="local-asr"))
    fish = VoiceConfigInput(provider="fish", base_url="https://fish.example", model="fish-model",
                            voice="reference", api_key="fish-secret")
    store.save("tts", fish)
    assert isinstance(store.tts_settings(), FishSpeechSettings)
    assert store.tts_settings().reference_id == "reference"
    assert store.voice_status()["can_transcribe"] is True
    assert store.voice_status()["speech_provider"] == "fish"
    assert store.voice_status()["can_speak"] is True
    with pytest.raises(VoiceConfigError, match="仅用于语音合成"):
        store.save("asr", fish)
    with pytest.raises(VoiceConfigError, match="音色"):
        store.save("tts", VoiceConfigInput(base_url="http://localhost:8000/v1", model="tts"))


@pytest.mark.parametrize("url", [
    "https://user:secret@example.com/v1", "https://example.com/?key=secret",
    "http://remote.example/v1", "https://example.com:0/v1", "file:///config",
])
def test_voice_urls_reject_credentials_and_insecure_remote_hosts(url):
    with pytest.raises(ValidationError):
        VoiceConfigInput(base_url=url, model="asr")
