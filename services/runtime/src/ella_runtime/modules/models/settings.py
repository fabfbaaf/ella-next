"""Model routing settings and shared OpenAI-compatible endpoint rules."""

import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

DEFAULT_BASE_URLS = {
    "ollama": "http://127.0.0.1:11434/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "deepseek": "https://api.deepseek.com",
    "openai": "https://api.openai.com/v1",
}
_GLOBAL_KEYS = {"gemini": "GEMINI_API_KEY", "deepseek": "DEEPSEEK_API_KEY", "openai": "OPENAI_API_KEY"}
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def canonical_model_id(provider: str, model: str) -> str:
    """Accept Gemini list IDs/display names while preserving other model aliases."""
    value = model.strip()
    if provider.strip().casefold() == "gemini":
        candidate = value.removeprefix("models/")
        if re.fullmatch(r"gemini(?:[\s_-]+[a-z0-9.]+)+", candidate, flags=re.IGNORECASE):
            return re.sub(r"[\s_]+", "-", candidate).lower()
    return value


def normalize_base_url(provider: str, value: str) -> str:
    """Normalize common pasted completion URLs without guessing proxy paths."""
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return value
    path = parsed.path.rstrip("/")
    path = path.removesuffix("/chat/completions")
    if host == "generativelanguage.googleapis.com" and path in {"", "/v1beta"}:
        path = "/v1beta/openai"
    elif host == "api.openai.com" and not path or provider.casefold() == "ollama" and host in _LOCAL_HOSTS and port == 11434 and not path:
        path = "/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def service_origin(value: str) -> str | None:
    """Return a valid credential origin, or None for unsafe/invalid endpoints."""
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
        if (
            parsed.scheme not in {"http", "https"} or not host or port == 0
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
            or (parsed.scheme == "http" and host not in _LOCAL_HOSTS)
        ):
            return None
        authority = f"[{host}]" if ":" in host else host
        if port is not None and port != (80 if parsed.scheme == "http" else 443):
            authority += f":{port}"
        return f"{parsed.scheme}://{authority}"
    except ValueError:
        return None


def compatible_endpoint(value: str) -> bool:
    try:
        path = urlsplit(value).path.casefold().rstrip("/")
    except ValueError:
        return False
    return not (
        path.endswith(("/responses", "/api/chat", "/api/generate", "/models"))
        or ":generatecontent" in path or ":streamgeneratecontent" in path
    )


def _environment_key(provider: str, base_url: str) -> str | None:
    """Global vendor keys belong only to that vendor's official origin."""
    variable = _GLOBAL_KEYS.get(provider)
    official = DEFAULT_BASE_URLS.get(provider)
    if variable and official and service_origin(base_url) == service_origin(official):
        return os.getenv(variable) or None
    return None


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    model: str
    base_url: str
    api_key: str | None = None

    def __post_init__(self) -> None:
        name = self.name.strip().lower()
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "model", canonical_model_id(name, self.model))
        object.__setattr__(self, "base_url", normalize_base_url(name, self.base_url))

    @property
    def configured(self) -> bool:
        if not service_origin(self.base_url) or not compatible_endpoint(self.base_url):
            return False
        local = urlsplit(self.base_url).hostname in _LOCAL_HOSTS
        return bool(self.name and self.model and (local or (self.api_key and self.api_key.strip())))

    def public_status(self) -> dict[str, str | bool]:
        try:
            parsed = urlsplit(self.base_url)
            safe_url = urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))
        except ValueError:
            safe_url = ""
        if self.api_key:
            safe_url = safe_url.replace(self.api_key, "[已隐藏密钥]")
        return {
            "provider": self.name, "model": self.model, "base_url": safe_url,
            "configured": self.configured,
        }


@dataclass(frozen=True)
class ModelSettings:
    chat: ProviderConfig
    action: ProviderConfig | None = None
    vision: ProviderConfig | None = None
    persona: ProviderConfig | None = None

    @classmethod
    def from_env(cls) -> "ModelSettings":
        def name(slot: str, default: str) -> str:
            return os.getenv(f"ELLA_{slot}_PROVIDER", default).strip().lower()

        def base(slot: str, provider: str, default: str | None = None) -> str:
            return normalize_base_url(provider, os.getenv(
                f"ELLA_{slot}_BASE_URL", DEFAULT_BASE_URLS.get(provider, "") if default is None else default,
            ))

        def key(slot: str, provider: str, url: str) -> str | None:
            return os.getenv(f"ELLA_{slot}_API_KEY") or _environment_key(provider, url)

        chat_name = name("CHAT", "ollama")
        chat_url = base("CHAT", chat_name)
        chat = ProviderConfig(chat_name, os.getenv("ELLA_CHAT_MODEL", ""), chat_url, key("CHAT", chat_name, chat_url))

        action_name = name("ACTION", "deepseek")
        action_url = base("ACTION", action_name)
        action_key = key("ACTION", action_name, action_url)
        action = None
        if action_key or any(os.getenv(f"ELLA_ACTION_{field}") for field in ("PROVIDER", "MODEL", "BASE_URL")):
            action = ProviderConfig(action_name, os.getenv("ELLA_ACTION_MODEL", "deepseek-flash" if action_name == "deepseek" else ""), action_url, action_key)

        vision_name = name("VISION", chat_name)
        vision_model = os.getenv("ELLA_VISION_MODEL", "").strip()
        vision = None
        if vision_model:
            vision_url = base("VISION", vision_name, chat.base_url if vision_name == chat_name else None)
            vision_key = os.getenv("ELLA_VISION_API_KEY")
            if not vision_key and service_origin(vision_url) is not None and service_origin(vision_url) == service_origin(chat.base_url):
                vision_key = chat.api_key
            vision = ProviderConfig(vision_name, vision_model, vision_url, vision_key or _environment_key(vision_name, vision_url))

        persona_name = name("PERSONA", "gemini")
        persona_model = os.getenv("ELLA_PERSONA_MODEL", "").strip()
        persona_url = base("PERSONA", persona_name)
        persona = ProviderConfig(persona_name, persona_model, persona_url, key("PERSONA", persona_name, persona_url)) if persona_model else None
        return cls(chat=chat, action=action, vision=vision, persona=persona)

    def for_purpose(self, purpose: str) -> ProviderConfig:
        return self.action if purpose == "action" and self.action is not None else self.chat

    def effective_slot(self, slot: str) -> str:
        return "chat" if slot in {"action", "vision"} and getattr(self, slot) is None else slot

    def public_status(self) -> dict[str, object]:
        return {
            "chat": self.chat.public_status(),
            "action": self.action.public_status() if self.action else None,
            "action_uses_chat": self.action is None,
            "vision": self.vision.public_status() if self.vision else None,
            "vision_uses_chat": self.vision is None,
            "persona": self.persona.public_status() if self.persona else None,
        }
