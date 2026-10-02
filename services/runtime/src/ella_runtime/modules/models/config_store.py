"""Persist model endpoints locally while keeping API keys in Windows Credential Locker."""

import os
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Literal

import keyring
from pydantic import BaseModel, Field, ValidationInfo, field_validator

from ella_runtime.modules.models.settings import (
    ModelSettings,
    ProviderConfig,
    canonical_model_id,
    compatible_endpoint,
    normalize_base_url,
    service_origin,
)
from ella_runtime.storage_paths import default_data_dir

ModelSlot = Literal["chat", "action", "vision", "persona"]
SLOTS: tuple[ModelSlot, ...] = ("chat", "action", "vision", "persona")
KEYRING_SERVICE = "Ella Next model API keys"


def _service_origin(base_url: str) -> str:
    """Bind a saved credential to a scheme, host and effective port."""
    origin = service_origin(base_url)
    if origin is None:
        raise ModelConfigError("模型服务地址无效")
    return origin


def _credential_name(slot: ModelSlot, origin: str) -> str:
    return f"{slot}@{origin}"


class ModelConfigError(RuntimeError):
    """A model setting could not be saved safely."""


class ModelConfigInput(BaseModel):
    provider: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=1000)
    api_key: str | None = Field(default=None, max_length=4096)

    @field_validator("provider", "model", "base_url")
    @classmethod
    def strip_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("此字段不能为空")
        return value

    @field_validator("model")
    @classmethod
    def normalize_model(cls, value: str, info: ValidationInfo) -> str:
        return canonical_model_id(info.data.get("provider", ""), value)

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str, info: ValidationInfo) -> str:
        value = normalize_base_url(info.data.get("provider", ""), value)
        if service_origin(value) is None:
            raise ValueError("请输入有效服务地址；远程服务必须使用 HTTPS，地址中不能包含凭据")
        if not compatible_endpoint(value):
            raise ValueError("当前接入使用 Chat Completions 兼容接口，请填写服务基础地址；不支持原生 generateContent、Responses 或 Ollama /api/chat 地址")
        return value


class WindowsCredentialStore:
    def _backend(self):
        backend = keyring.get_keyring()
        if os.name != "nt" or not type(backend).__module__.startswith("keyring.backends.Windows"):
            raise ModelConfigError("保存密钥需要 Windows 凭据库；可继续用环境变量配置密钥")
        return backend

    def get(self, identity: str) -> str | None:
        try:
            return self._backend().get_password(KEYRING_SERVICE, identity)
        except ModelConfigError:
            raise
        except Exception as exc:
            raise ModelConfigError("读取 Windows 凭据库失败") from exc

    def set(self, identity: str, value: str) -> None:
        try:
            self._backend().set_password(KEYRING_SERVICE, identity, value)
        except ModelConfigError:
            raise
        except Exception as exc:
            raise ModelConfigError("写入 Windows 凭据库失败") from exc

    def delete(self, identity: str) -> None:
        try:
            self._backend().delete_password(KEYRING_SERVICE, identity)
        except keyring.errors.PasswordDeleteError:
            pass
        except ModelConfigError:
            raise
        except Exception as exc:
            raise ModelConfigError("删除 Windows 凭据失败") from exc


class ModelConfigStore:
    def __init__(self, path: Path | None = None, credentials=None) -> None:
        self.path = path or default_data_dir() / "models.sqlite3"
        self.credentials = credentials or WindowsCredentialStore()
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS model_configs ("
                "slot TEXT PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL, "
                "base_url TEXT NOT NULL, key_origin TEXT)"
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(model_configs)")
            }
            if "key_origin" not in columns:
                connection.execute("ALTER TABLE model_configs ADD COLUMN key_origin TEXT")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _saved(self) -> dict[str, dict[str, str | None]]:
        with closing(self._connect()) as connection:
            return {
                row["slot"]: dict(row)
                for row in connection.execute(
                    "SELECT slot, provider, model, base_url, key_origin FROM model_configs"
                )
            }

    def _saved_key(self, slot: ModelSlot, item: dict[str, str | None]) -> str | None:
        origin = item["key_origin"]
        if origin is None:  # Existing installation: legacy key belongs to the saved URL only.
            return self.credentials.get(slot)
        if origin != _service_origin(str(item["base_url"])):
            return None
        return self.credentials.get(_credential_name(slot, origin))

    def settings(self) -> ModelSettings:
        with self._lock:
            baseline = ModelSettings.from_env()
            overrides = self._saved()
            values = {slot: getattr(baseline, slot) for slot in SLOTS}
            for slot in SLOTS:
                item = overrides.get(slot)
                if item is None:
                    continue
                values[slot] = ProviderConfig(
                    name=str(item["provider"]),
                    model=str(item["model"]),
                    base_url=str(item["base_url"]),
                    api_key=self._saved_key(slot, item),
                )
            return ModelSettings(**values)

    def public_status(self) -> dict[str, object]:
        with self._lock:
            status = self.settings().public_status()
            saved = self._saved()
            for slot in SLOTS:
                value = status[slot]
                if isinstance(value, dict):
                    value["source"] = "saved" if slot in saved else "environment"
                    value["key_saved"] = bool(
                        slot in saved and self._saved_key(slot, saved[slot])
                    )
            return status

    def save(self, slot: ModelSlot, value: ModelConfigInput) -> dict[str, object]:
        with self._lock:
            previous = self._saved().get(slot)
            origin = _service_origin(value.base_url)
            same_origin = previous is not None and _service_origin(
                str(previous["base_url"])
            ) == origin
            key_origin: str | None = origin
            if value.api_key is None and same_origin and previous is not None:
                key_origin = previous["key_origin"]
            else:
                name = _credential_name(slot, origin)
                new_key = value.api_key
                if new_key is None and previous is None:
                    settings = self.settings()
                    source = getattr(settings, settings.effective_slot(slot))
                    if source is not None and service_origin(source.base_url) == origin:
                        new_key = source.api_key
                if new_key and new_key.strip():
                    self.credentials.set(name, new_key.strip())
                else:
                    # A blank or omitted key on a new host must not revive a stale key.
                    self.credentials.delete(name)
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    "INSERT INTO model_configs(slot, provider, model, base_url, key_origin) "
                    "VALUES(?, ?, ?, ?, ?) ON CONFLICT(slot) DO UPDATE SET "
                    "provider=excluded.provider, model=excluded.model, "
                    "base_url=excluded.base_url, key_origin=excluded.key_origin",
                    (slot, value.provider, value.model, value.base_url, key_origin),
                )
            if previous is not None and not same_origin:
                old_origin = previous["key_origin"]
                old_name = slot if old_origin is None else _credential_name(slot, old_origin)
                self.credentials.delete(old_name)
            elif previous is not None and previous["key_origin"] is None and key_origin:
                self.credentials.delete(slot)
            return self.public_status()

    def reset(self, slot: ModelSlot) -> dict[str, object]:
        with self._lock:
            previous = self._saved().get(slot)
            # Remove the visible override first; an unavailable vault cannot leave it active.
            with closing(self._connect()) as connection, connection:
                connection.execute("DELETE FROM model_configs WHERE slot = ?", (slot,))
            if previous is not None:
                origin = previous["key_origin"]
                self.credentials.delete(
                    slot if origin is None else _credential_name(slot, origin)
                )
            return self.public_status()
