"""Configurable OpenAI-compatible speech-to-text and text-to-speech transport."""

import os
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

import httpx

from ella_runtime.modules.models.contracts import ModelPurpose, TokenUsage


class VoiceProviderError(RuntimeError):
    """A voice provider failed without including credentials or audio in the error."""


@dataclass(frozen=True)
class VoiceSettings:
    provider: str
    base_url: str
    api_key: str | None
    transcription_model: str
    speech_model: str
    voice: str

    @classmethod
    def from_env(cls) -> "VoiceSettings":
        return cls(
            provider=os.getenv("ELLA_VOICE_PROVIDER", "openai").strip(),
            base_url=os.getenv("ELLA_VOICE_BASE_URL", "https://api.openai.com/v1").strip(),
            api_key=os.getenv("ELLA_VOICE_API_KEY") or None,
            transcription_model=os.getenv("ELLA_STT_MODEL", "").strip(),
            speech_model=os.getenv("ELLA_TTS_MODEL", "").strip(),
            voice=os.getenv("ELLA_TTS_VOICE", "").strip(),
        )

    @property
    def _reachable(self) -> bool:
        parsed = urlparse(self.base_url)
        return bool(
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and (parsed.hostname in {"127.0.0.1", "localhost", "::1"} or self.api_key)
        )

    @property
    def can_transcribe(self) -> bool:
        return bool(self._reachable and self.transcription_model)

    @property
    def can_speak(self) -> bool:
        return bool(self._reachable and self.speech_model and self.voice)

    def public_status(self) -> dict[str, str | bool]:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "transcription_model": self.transcription_model,
            "speech_model": self.speech_model,
            "voice": self.voice,
            "can_transcribe": self.can_transcribe,
            "transcription_mode": "utterance",
            "can_speak": self.can_speak,
        }


@dataclass(frozen=True)
class Transcription:
    text: str
    usage: TokenUsage


@dataclass(frozen=True)
class Speech:
    audio: bytes
    media_type: str
    usage: TokenUsage


def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


class OpenAICompatibleVoiceProvider:
    def __init__(self, settings: VoiceSettings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.api_key}"} if self.settings.api_key else {}

    async def transcribe(
        self, audio: bytes, *, filename: str, media_type: str, task_id: str | None = None
    ) -> Transcription:
        if not self.settings.can_transcribe:
            raise VoiceProviderError("语音转文字服务尚未配置完整")
        if not audio:
            raise VoiceProviderError("录音为空")

        async def send(client: httpx.AsyncClient) -> httpx.Response:
            return await client.post(
                f"{self.settings.base_url.rstrip('/')}/audio/transcriptions",
                headers=self._headers(),
                data={"model": self.settings.transcription_model, "response_format": "json"},
                files={"file": (filename, audio, media_type)},
            )

        response = await self._request(send)
        try:
            payload = response.json()
        except ValueError as exc:
            raise VoiceProviderError("语音转文字服务返回格式无效") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise VoiceProviderError("语音转文字服务未返回文字")
        details = payload.get("usage")
        usage = details if isinstance(details, dict) else {}
        return Transcription(
            text=payload["text"].strip(),
            usage=TokenUsage(
                provider=self.settings.provider,
                model=self.settings.transcription_model,
                purpose=ModelPurpose.VOICE,
                task_id=task_id,
                occurred_at=datetime.now().astimezone(),
                input_tokens=_count(usage.get("input_tokens")),
                output_tokens=_count(usage.get("output_tokens")),
            ),
        )

    async def synthesize(self, text: str, *, task_id: str | None = None) -> Speech:
        if not self.settings.can_speak:
            raise VoiceProviderError("语音合成服务尚未配置完整")

        async def send(client: httpx.AsyncClient) -> httpx.Response:
            return await client.post(
                f"{self.settings.base_url.rstrip('/')}/audio/speech",
                headers=self._headers(),
                json={
                    "model": self.settings.speech_model,
                    "voice": self.settings.voice,
                    "input": text,
                    "response_format": "mp3",
                },
            )

        response = await self._request(send)
        if not response.content:
            raise VoiceProviderError("语音合成服务返回了空音频")
        return Speech(
            audio=response.content,
            media_type="audio/mpeg",
            usage=TokenUsage(
                provider=self.settings.provider,
                model=self.settings.speech_model,
                purpose=ModelPurpose.VOICE,
                task_id=task_id,
                occurred_at=datetime.now().astimezone(),
            ),
        )

    async def _request(self, send) -> httpx.Response:
        try:
            if self._client is None:
                async with httpx.AsyncClient(timeout=90.0) as client:
                    response = await send(client)
            else:
                response = await send(self._client)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            raise VoiceProviderError(f"语音服务请求失败：HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise VoiceProviderError("语音服务连接失败") from exc
