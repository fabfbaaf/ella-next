"""Persist separate speech services without putting credentials in the settings file."""

import os
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import keyring
from pydantic import BaseModel, Field, field_validator

from ella_runtime.modules.voice.fish import FishSpeechSettings
from ella_runtime.modules.voice.provider import VoiceSettings
from ella_runtime.storage_paths import default_data_dir

VoiceSlot = Literal["asr", "tts"]
KEYRING_SERVICE = "Ella Next voice API keys"


class VoiceConfigError(RuntimeError):
    """A speech configuration could not be read or saved safely."""


def service_origin(base_url: str) -> str:
    parsed = urlsplit(base_url)
    host = parsed.hostname
    if parsed.scheme not in {"http", "https"} or not host:
        raise VoiceConfigError("语音服务地址无效")
    try:
        port = parsed.port
    except ValueError as exc:
        raise VoiceConfigError("语音服务端口无效") from exc
    authority = f"[{host.lower()}]" if ":" in host else host.lower()
    if port is not None and port != (80 if parsed.scheme == "http" else 443):
        authority += f":{port}"
    return f"{parsed.scheme.lower()}://{authority}"


class VoiceConfigInput(BaseModel):
    provider: Literal["openai", "fish"] = "openai"
    base_url: str = Field(min_length=1, max_length=1000)
    model: str = Field(min_length=1, max_length=200)
    voice: str = Field(default="", max_length=200)
    api_key: str | None = Field(default=None, max_length=4096)

    @field_validator("base_url", "model", "voice")
    @classmethod
    def strip_fields(cls, value: str) -> str:
        return value.strip()

    @field_validator("model")
    @classmethod
    def required_model(cls, value: str) -> str:
        if not value:
            raise ValueError("请输入模型 ID")
        return value

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("语音服务端口无效") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or port == 0
            or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})
        ):
            raise ValueError("远程语音服务必须使用 HTTPS，地址不能包含凭据或查询参数")
        return value.rstrip("/")


class WindowsVoiceCredentialStore:
    def _backend(self):
        backend = keyring.get_keyring()
        if os.name != "nt" or not type(backend).__module__.startswith("keyring.backends.Windows"):
            raise VoiceConfigError("保存语音密钥需要 Windows 凭据库；也可用环境变量设置密钥")
        return backend

    def get(self, identity: str) -> str | None:
        try:
            return self._backend().get_password(KEYRING_SERVICE, identity)
        except VoiceConfigError:
            raise
        except Exception as exc:
            raise VoiceConfigError("读取语音服务凭据失败") from exc

    def set(self, identity: str, value: str) -> None:
        try:
            self._backend().set_password(KEYRING_SERVICE, identity, value)
        except VoiceConfigError:
            raise
        except Exception as exc:
            raise VoiceConfigError("保存语音服务凭据失败") from exc

    def delete(self, identity: str) -> None:
        try:
            self._backend().delete_password(KEYRING_SERVICE, identity)
        except keyring.errors.PasswordDeleteError:
            pass
        except VoiceConfigError:
            raise
        except Exception as exc:
            raise VoiceConfigError("删除语音服务凭据失败") from exc


class VoiceConfigStore:
    def __init__(self, path: Path | None = None, credentials=None) -> None:
        self.path = path or default_data_dir() / "voice.sqlite3"
        self.credentials = credentials or WindowsVoiceCredentialStore()
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS voice_configs (slot TEXT PRIMARY KEY, "
                "provider TEXT NOT NULL, base_url TEXT NOT NULL, model TEXT NOT NULL, "
                "voice TEXT NOT NULL, key_origin TEXT)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _saved(self) -> dict[str, dict]:
        with closing(self._connect()) as connection:
            return {row["slot"]: dict(row) for row in connection.execute("SELECT * FROM voice_configs")}

    @staticmethod
    def _environment(slot: VoiceSlot) -> dict:
        base = VoiceSettings.from_env()
        if slot == "tts" and os.getenv("ELLA_TTS_PROVIDER", "").strip().casefold() == "fish":
            fish = FishSpeechSettings.from_env()
            return {"provider": "fish", "base_url": fish.base_url, "model": fish.model,
                    "voice": fish.reference_id, "api_key": fish.api_key}
        prefix = "ELLA_ASR" if slot == "asr" else "ELLA_TTS"
        return {
            "provider": "openai",
            "base_url": os.getenv(f"{prefix}_BASE_URL", base.base_url).strip(),
            "model": base.transcription_model if slot == "asr" else base.speech_model,
            "voice": "" if slot == "asr" else base.voice,
            "api_key": os.getenv(f"{prefix}_API_KEY", base.api_key or "") or None,
        }

    def _value(self, slot: VoiceSlot) -> dict:
        item = self._saved().get(slot)
        if item is None:
            return self._environment(slot)
        origin = item["key_origin"]
        key = None
        if origin and origin == service_origin(item["base_url"]):
            key = self.credentials.get(f"{slot}@{origin}")
        return {**item, "api_key": key}

    def asr_settings(self) -> VoiceSettings:
        with self._lock:
            value = self._value("asr")
            return VoiceSettings(value["provider"], value["base_url"], value["api_key"],
                                 value["model"], "", "")

    def tts_settings(self) -> VoiceSettings | FishSpeechSettings:
        with self._lock:
            value = self._value("tts")
            if value["provider"] == "fish":
                return FishSpeechSettings(value["api_key"], value["voice"], value["model"],
                                          value["base_url"])
            return VoiceSettings(value["provider"], value["base_url"], value["api_key"],
                                 "", value["model"], value["voice"])

    def public_status(self) -> dict[str, object]:
        with self._lock:
            saved = self._saved()
            status = {}
            for slot in ("asr", "tts"):
                value = self._value(slot)
                settings = self.asr_settings() if slot == "asr" else self.tts_settings()
                configured = (settings.enabled if isinstance(settings, FishSpeechSettings)
                              else settings.can_transcribe if slot == "asr" else settings.can_speak)
                status[slot] = {
                    **{field: value[field] for field in ("provider", "base_url", "model", "voice")},
                    "configured": configured, "key_saved": bool(slot in saved and value["api_key"]),
                    "key_present": bool(value["api_key"]),
                    "source": "saved" if slot in saved else "environment",
                }
            return status

    def voice_status(self) -> dict[str, object]:
        status = self.public_status()
        asr, tts = status["asr"], status["tts"]
        return {
            "provider": asr["provider"], "base_url": asr["base_url"],
            "transcription_model": asr["model"], "transcription_mode": "utterance",
            "can_transcribe": asr["configured"], "speech_provider": tts["provider"],
            "speech_base_url": tts["base_url"], "speech_model": tts["model"],
            "voice": tts["voice"], "can_speak": tts["configured"],
        }

    def save(self, slot: VoiceSlot, value: VoiceConfigInput) -> dict[str, object]:
        if slot not in {"asr", "tts"}:
            raise VoiceConfigError("语音配置位置无效")
        if slot == "asr" and value.provider == "fish":
            raise VoiceConfigError("Fish Audio 仅用于语音合成，识别请使用兼容 OpenAI 的服务")
        if slot == "tts" and not value.voice:
            raise VoiceConfigError("请输入音色 ID；Fish Audio 请填 reference_id")
        if value.provider == "fish" and not value.base_url.startswith("https://"):
            raise VoiceConfigError("Fish Audio 服务必须使用 HTTPS")
        with self._lock:
            previous = self._saved().get(slot)
            current = self._value(slot)
            origin = service_origin(value.base_url)
            same_origin = service_origin(current["base_url"]) == origin
            name = f"{slot}@{origin}"
            requested = value.api_key.strip() if value.api_key is not None else None
            key = current["api_key"] if requested is None and same_origin else requested
            if key:
                self.credentials.set(name, key)
            elif previous and previous["key_origin"] == origin:
                self.credentials.delete(name)
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    "INSERT INTO voice_configs(slot, provider, base_url, model, voice, key_origin) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(slot) DO UPDATE SET "
                    "provider=excluded.provider, base_url=excluded.base_url, model=excluded.model, "
                    "voice=excluded.voice, key_origin=excluded.key_origin",
                    (slot, value.provider, value.base_url, value.model,
                     value.voice if slot == "tts" else "", origin if key else None),
                )
            if previous and previous["key_origin"] and previous["key_origin"] != origin:
                self.credentials.delete(f"{slot}@{previous['key_origin']}")
            return self.public_status()

    def reset(self, slot: VoiceSlot) -> dict[str, object]:
        if slot not in {"asr", "tts"}:
            raise VoiceConfigError("语音配置位置无效")
        with self._lock:
            previous = self._saved().get(slot)
            with closing(self._connect()) as connection, connection:
                connection.execute("DELETE FROM voice_configs WHERE slot = ?", (slot,))
            if previous and previous["key_origin"]:
                self.credentials.delete(f"{slot}@{previous['key_origin']}")
            return self.public_status()
