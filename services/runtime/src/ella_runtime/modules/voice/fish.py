"""Fish Audio cloud TTS adapter; ASR remains independently configurable."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

import httpx

from ella_runtime.modules.models.contracts import ModelPurpose, TokenUsage
from ella_runtime.modules.voice.provider import Speech, VoiceProviderError


@dataclass(frozen=True)
class FishSpeechSettings:
    api_key: str | None
    reference_id: str
    model: str = "s2.1-pro-free"
    base_url: str = "https://api.fish.audio"

    @classmethod
    def from_env(cls) -> FishSpeechSettings:
        return cls(
            api_key=os.getenv("ELLA_FISH_API_KEY") or None,
            reference_id=os.getenv("ELLA_FISH_REFERENCE_ID", "").strip(),
            model=os.getenv("ELLA_FISH_TTS_MODEL", "s2.1-pro-free").strip(),
            base_url=os.getenv("ELLA_FISH_BASE_URL", "https://api.fish.audio").strip(),
        )

    @property
    def enabled(self) -> bool:
        parsed = urlparse(self.base_url)
        return bool(
            self.api_key and self.reference_id and self.model
            and parsed.scheme == "https" and parsed.hostname
        )


class FishSpeechProvider:
    def __init__(
        self, settings: FishSpeechSettings, client: httpx.AsyncClient | None = None
    ) -> None:
        self.settings = settings
        self.client = client

    async def synthesize(self, text: str, *, task_id: str | None = None) -> Speech:
        if not self.settings.enabled:
            raise VoiceProviderError("Fish Audio 语音合成尚未配置完整")
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "model": self.settings.model,
        }
        payload = {
            "text": text,
            "reference_id": self.settings.reference_id,
            "format": "mp3",
            "latency": "balanced",
        }
        try:
            if self.client is None:
                async with httpx.AsyncClient(timeout=90) as client:
                    response = await client.post(
                        f"{self.settings.base_url.rstrip('/')}/v1/tts",
                        headers=headers, json=payload,
                    )
            else:
                response = await self.client.post(
                    f"{self.settings.base_url.rstrip('/')}/v1/tts",
                    headers=headers, json=payload,
                )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise VoiceProviderError(f"Fish Audio 合成失败：HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise VoiceProviderError("Fish Audio 连接失败") from exc
        if not response.content:
            raise VoiceProviderError("Fish Audio 返回了空音频")
        return Speech(
            audio=response.content,
            media_type="audio/mpeg",
            usage=TokenUsage(
                provider="fish", model=self.settings.model, purpose=ModelPurpose.VOICE,
                task_id=task_id, occurred_at=datetime.now().astimezone(),
            ),
        )


class CombinedVoiceProvider:
    def __init__(self, recognizer, speaker) -> None:
        self.recognizer = recognizer
        self.speaker = speaker

    async def transcribe(self, audio: bytes, *, filename: str, media_type: str, task_id=None):
        return await self.recognizer.transcribe(
            audio, filename=filename, media_type=media_type, task_id=task_id
        )

    async def synthesize(self, text: str, *, task_id=None):
        return await self.speaker.synthesize(text, task_id=task_id)
