import sqlite3

from ella_runtime.api import app, get_model_config_store
from ella_runtime.modules.models.config_store import ModelConfigInput, ModelConfigStore
from ella_runtime.modules.models.gateway import ModelGateway
from ella_runtime.modules.models.usage_store import UsageStore
from tests.api_client import authorized_client


class MemoryCredentials:
    def __init__(self):
        self.values = {}

    def get(self, slot):
        return self.values.get(slot)

    def set(self, slot, value):
        self.values[slot] = value

    def delete(self, slot):
        self.values.pop(slot, None)


def test_model_config_is_live_and_never_exposes_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ELLA_CHAT_MODEL", "local-model")
    creds = MemoryCredentials()
    path = tmp_path / "models.sqlite3"
    store = ModelConfigStore(path, creds)
    gateway = ModelGateway(store.settings, UsageStore(tmp_path / "usage.sqlite3"))
    assert gateway.settings.action is None

    app.dependency_overrides[get_model_config_store] = lambda: store
    try:
        client = authorized_client(app)
        value = {
            "provider": "deepseek",
            "model": "deepseek-flash",
            "base_url": "https://api.deepseek.com",
            "api_key": "test-secret-that-must-stay-private",
        }
        response = client.put(
            "/api/models/config/action",
            json=value,
            headers={"Origin": "http://127.0.0.1:1421"},
        )
        assert response.status_code == 200
        assert response.json()["action"]["configured"] is True
        assert response.json()["action"]["key_saved"] is True
        assert "test-secret" not in response.text
        assert gateway.settings.action.api_key == value["api_key"]
        assert "test-secret" not in path.read_bytes().decode("utf-8", errors="ignore")

        status = client.get("/api/models/status")
        assert status.json()["action"]["source"] == "saved"
        assert "test-secret" not in status.text
        reset = client.delete("/api/models/config/action")
        assert reset.status_code == 200
        assert reset.json()["action"] is None
        assert gateway.settings.action is None
        assert creds.values == {}
    finally:
        app.dependency_overrides.clear()


def test_model_config_keeps_saved_key_when_blank_and_rejects_url_secrets(tmp_path):
    creds = MemoryCredentials()
    store = ModelConfigStore(tmp_path / "models.sqlite3", creds)
    value = ModelConfigInput(
        provider="gemini",
        model="gemini-3.8-flash",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key="test-key",
    )
    store.save("persona", value)
    store.save("persona", value.model_copy(update={"api_key": None}))
    assert store.settings().persona.api_key == "test-key"
    assert store.settings().persona.base_url.endswith("/openai")
    assert store.public_status()["persona"]["key_saved"] is True
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM model_configs").fetchone()[0] == 1
    client = authorized_client(app)
    app.dependency_overrides[get_model_config_store] = lambda: store
    try:
        bad = client.put(
            "/api/models/config/persona",
            json={"provider": "gemini", "model": "m", "base_url": "https://x.test/?key=leak"},
        )
        assert bad.status_code == 422
        insecure = client.put(
            "/api/models/config/persona",
            json={"provider": "gemini", "model": "m", "base_url": "http://x.test/v1"},
        )
        assert insecure.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_model_key_never_follows_changed_service_origin(tmp_path):
    creds = MemoryCredentials()
    store = ModelConfigStore(tmp_path / "models.sqlite3", creds)
    original = ModelConfigInput(
        provider="provider-a", model="first", base_url="https://a.example/v1",
        api_key="a-only-secret",
    )
    store.save("action", original)
    assert store.settings().action.api_key == "a-only-secret"

    # A path or model change on the same effective origin keeps its credential.
    store.save("action", original.model_copy(update={
        "model": "second", "base_url": "https://A.example:443/other", "api_key": None,
    }))
    assert store.settings().action.api_key == "a-only-secret"

    # A different host, port, or protocol cannot inherit the old credential.
    creds.set("action@https://b.example", "stale-b-secret")
    store.save("action", original.model_copy(update={
        "base_url": "https://b.example/v1", "api_key": None,
    }))
    assert store.settings().action.api_key is None
    assert store.public_status()["action"]["key_saved"] is False
    assert ModelConfigStore(store.path, creds).settings().action.api_key is None
    assert "a-only-secret" not in creds.values.values()
    assert "stale-b-secret" not in creds.values.values()

    store.save("action", original.model_copy(update={
        "base_url": "https://b.example/v1", "api_key": "b-only-secret",
    }))
    assert store.settings().action.api_key == "b-only-secret"
    store.save("action", original.model_copy(update={
        "base_url": "https://b.example:8443/v1", "api_key": None,
    }))
    assert store.settings().action.api_key is None
    assert "b-only-secret" not in creds.values.values()


def test_legacy_slot_key_is_bound_to_existing_url_and_can_be_cleared(tmp_path):
    path = tmp_path / "models.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE model_configs (slot TEXT PRIMARY KEY, provider TEXT NOT NULL, "
            "model TEXT NOT NULL, base_url TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO model_configs VALUES ('persona', 'gemini', 'old', 'https://old.example/v1')"
        )
    creds = MemoryCredentials()
    creds.set("persona", "legacy-secret")
    store = ModelConfigStore(path, creds)
    assert store.settings().persona.api_key == "legacy-secret"
    store.save("persona", ModelConfigInput(
        provider="gemini", model="new", base_url="https://old.example/other",
    ))
    assert store.settings().persona.api_key == "legacy-secret"
    store.save("persona", ModelConfigInput(
        provider="gemini", model="new", base_url="https://new.example/v1",
    ))
    assert store.settings().persona.api_key is None
    assert creds.values == {}

    store.save("persona", ModelConfigInput(
        provider="gemini", model="new", base_url="https://new.example/v1",
        api_key="new-secret",
    ))
    assert store.settings().persona.api_key == "new-secret"
    store.save("persona", ModelConfigInput(
        provider="gemini", model="new", base_url="https://new.example/v1",
        api_key="",
    ))
    assert store.settings().persona.api_key is None
    assert creds.values == {}


def test_first_save_keeps_same_service_environment_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ELLA_CHAT_PROVIDER", "gemini")
    monkeypatch.setenv("ELLA_CHAT_MODEL", "gemini-3.8-flash")
    monkeypatch.setenv("ELLA_CHAT_API_KEY", "environment-only-secret")
    creds = MemoryCredentials()
    store = ModelConfigStore(tmp_path / "models.sqlite3", creds)
    store.save("chat", ModelConfigInput(provider="gemini", model="gemini-other", base_url="https://generativelanguage.googleapis.com:443/v1beta/openai"))
    assert store.settings().chat.api_key == "environment-only-secret"
    assert store.public_status()["chat"]["key_saved"] is True
    assert "environment-only-secret" not in store.path.read_bytes().decode("utf-8", errors="ignore")
    store.save("chat", ModelConfigInput(provider="gemini", model="gemini-other", base_url="https://generativelanguage.googleapis.com/v1beta/openai", api_key=""))
    assert store.settings().chat.api_key is None


def test_first_save_on_different_host_never_copies_environment_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ELLA_CHAT_PROVIDER", "gemini")
    monkeypatch.setenv("ELLA_CHAT_MODEL", "gemini-3.8-flash")
    monkeypatch.setenv("ELLA_CHAT_API_KEY", "official-only-secret")
    creds = MemoryCredentials()
    store = ModelConfigStore(tmp_path / "models.sqlite3", creds)
    store.save("chat", ModelConfigInput(provider="gemini", model="m", base_url="https://proxy.example/v1"))
    assert store.settings().chat.api_key is None
    assert "official-only-secret" not in creds.values.values()


def test_new_independent_vision_keeps_same_origin_inherited_chat_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ELLA_VISION_MODEL", raising=False)
    creds = MemoryCredentials()
    store = ModelConfigStore(tmp_path / "models.sqlite3", creds)
    store.save("chat", ModelConfigInput(provider="custom", model="chat", base_url="https://service.example/v1", api_key="same-service-secret"))
    assert store.settings().vision is None
    store.save("vision", ModelConfigInput(provider="custom", model="vision", base_url="https://service.example:443/v1"))
    assert store.settings().vision.api_key == "same-service-secret"
    assert store.public_status()["vision"]["key_saved"] is True
    store.save("vision", ModelConfigInput(provider="custom", model="vision", base_url="https://other.example/v1"))
    assert store.settings().vision.api_key is None
    assert store.settings().chat.api_key == "same-service-secret"
