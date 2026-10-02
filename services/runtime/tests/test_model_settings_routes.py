import json

import pytest
from pydantic import ValidationError

from ella_runtime.modules.models.config_store import ModelConfigInput
from ella_runtime.modules.models.settings import ModelSettings, ProviderConfig, canonical_model_id


@pytest.fixture(autouse=True)
def isolated_model_environment(monkeypatch):
    import os
    for variable in tuple(os.environ):
        if variable.startswith(("ELLA_CHAT_", "ELLA_ACTION_", "ELLA_VISION_", "ELLA_PERSONA_")) or variable in {"GEMINI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"}:
            monkeypatch.delenv(variable)


def test_action_uses_its_provider_key_not_deepseek_key(monkeypatch):
    monkeypatch.setenv("ELLA_ACTION_PROVIDER", "gemini")
    monkeypatch.setenv("ELLA_ACTION_MODEL", "models/gemini-3.8-flash")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-only")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-only")
    settings = ModelSettings.from_env()
    assert settings.action.api_key == "gemini-only"
    assert settings.action.model == "gemini-3.8-flash"


@pytest.mark.parametrize("slot", ["CHAT", "ACTION", "VISION", "PERSONA"])
def test_global_vendor_key_never_follows_custom_host(monkeypatch, slot):
    monkeypatch.setenv(f"ELLA_{slot}_PROVIDER", "gemini")
    monkeypatch.setenv(f"ELLA_{slot}_MODEL", "gemini-3.8-flash")
    monkeypatch.setenv(f"ELLA_{slot}_BASE_URL", "https://proxy.example/v1")
    monkeypatch.setenv("GEMINI_API_KEY", "official-only")
    config = getattr(ModelSettings.from_env(), slot.lower())
    assert config.api_key is None
    assert not config.configured
    monkeypatch.setenv(f"ELLA_{slot}_API_KEY", "proxy-only")
    assert getattr(ModelSettings.from_env(), slot.lower()).api_key == "proxy-only"


@pytest.mark.parametrize("url", ["https://api.openai.com:8443/v1", "https://untrusted.example/v1"])
def test_official_key_also_requires_same_effective_port(monkeypatch, url):
    monkeypatch.setenv("ELLA_CHAT_PROVIDER", "openai")
    monkeypatch.setenv("ELLA_CHAT_MODEL", "test-model")
    monkeypatch.setenv("ELLA_CHAT_BASE_URL", url)
    monkeypatch.setenv("OPENAI_API_KEY", "official-only")
    assert ModelSettings.from_env().chat.api_key is None


def test_same_provider_vision_inherits_actual_chat_endpoint(monkeypatch):
    monkeypatch.setenv("ELLA_CHAT_PROVIDER", "gemini")
    monkeypatch.setenv("ELLA_CHAT_BASE_URL", "https://proxy.example:443/v1")
    monkeypatch.setenv("ELLA_CHAT_API_KEY", "proxy-only")
    monkeypatch.setenv("ELLA_VISION_MODEL", "gemini-3.8-flash")
    settings = ModelSettings.from_env()
    assert settings.vision.base_url == settings.chat.base_url
    assert settings.vision.api_key == "proxy-only"


def test_vision_different_vendor_uses_own_official_key(monkeypatch):
    monkeypatch.setenv("ELLA_CHAT_PROVIDER", "deepseek")
    monkeypatch.setenv("ELLA_CHAT_API_KEY", "deepseek-only")
    monkeypatch.setenv("ELLA_VISION_PROVIDER", "gemini")
    monkeypatch.setenv("ELLA_VISION_MODEL", "gemini-3.8-flash")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-only")
    assert ModelSettings.from_env().vision.api_key == "gemini-only"
    monkeypatch.delenv("GEMINI_API_KEY")
    assert ModelSettings.from_env().vision.api_key is None


def test_vision_host_override_never_inherits_chat_key(monkeypatch):
    monkeypatch.setenv("ELLA_CHAT_PROVIDER", "gemini")
    monkeypatch.setenv("ELLA_CHAT_API_KEY", "official-only")
    monkeypatch.setenv("ELLA_VISION_MODEL", "gemini-3.8-flash")
    monkeypatch.setenv("ELLA_VISION_BASE_URL", "https://vision.example/v1")
    assert ModelSettings.from_env().vision.api_key is None


def test_action_model_or_url_alone_does_not_silently_use_chat(monkeypatch):
    monkeypatch.setenv("ELLA_ACTION_MODEL", "explicit-action")
    settings = ModelSettings.from_env()
    assert settings.action.model == "explicit-action"
    assert not settings.action.configured
    assert settings.for_purpose("action") is settings.action


def test_unset_action_and_vision_show_actual_inherited_routes():
    settings = ModelSettings.from_env()
    assert settings.effective_slot("action") == "chat"
    assert settings.effective_slot("vision") == "chat"
    assert settings.effective_slot("persona") == "persona"
    assert settings.public_status()["vision_uses_chat"]


@pytest.mark.parametrize(("provider", "url", "expected"), [
    ("gemini", "https://generativelanguage.googleapis.com", "https://generativelanguage.googleapis.com/v1beta/openai"),
    ("gemini", "https://generativelanguage.googleapis.com/v1beta/", "https://generativelanguage.googleapis.com/v1beta/openai"),
    ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "https://generativelanguage.googleapis.com/v1beta/openai"),
    ("openai", "https://api.openai.com", "https://api.openai.com/v1"),
    ("custom", "https://proxy.example/other/chat/completions/", "https://proxy.example/other"),
    ("ollama", "http://localhost:11434", "http://localhost:11434/v1"),
    ("custom", "https://proxy.example/other", "https://proxy.example/other"),
])
def test_endpoint_normalization_in_saved_and_environment_configs(provider, url, expected):
    value = ModelConfigInput(provider=provider, model="m", base_url=url)
    assert value.base_url == expected
    assert ProviderConfig(provider, "m", url).base_url == expected


@pytest.mark.parametrize("url", [
    "https://[broken", "https://service.example:bad/v1", "https://service.example:0/v1",
    "https://secret:password@service.example/v1", "https://service.example/v1?key=secret",
    "http://remote.example/v1", "https://service.example/v1/responses",
    "http://localhost:11434/api/chat", "https://generativelanguage.googleapis.com/v1beta/models/m:generateContent",
])
def test_invalid_endpoint_rejected_and_status_safe(url):
    with pytest.raises(ValidationError):
        ModelConfigInput(provider="custom", model="m", base_url=url)
    config = ProviderConfig("custom", "m", url, "secret")
    assert not config.configured
    assert "secret" not in json.dumps(config.public_status())


def test_gemini_prefix_normalization_does_not_rewrite_custom_aliases():
    assert canonical_model_id("gemini", "models/gemini-3.8-flash") == "gemini-3.8-flash"
    assert canonical_model_id("gemini", " Gemini 3.8 Flash ") == "gemini-3.8-flash"
    assert canonical_model_id("custom", "models/My Model") == "models/My Model"
    assert canonical_model_id("gemini", "custom-alias") == "custom-alias"


def test_changing_action_vendor_requires_an_explicit_model(monkeypatch):
    monkeypatch.setenv("ELLA_ACTION_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-only")
    config = ModelSettings.from_env().action
    assert config.model == ""
    assert not config.configured


def test_internal_url_whitespace_rejected():
    with pytest.raises(ValidationError):
        ModelConfigInput(provider="custom", model="m", base_url="https://bad host.example/v1")
