"""One voice turn with interruption and text fallback."""

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from ella_runtime.modules.models.contracts import (
    ModelMessage,
    ModelPurpose,
    ModelRequest,
    ModelResponse,
)
from ella_runtime.modules.models.usage_store import UsageStore
from ella_runtime.modules.voice.commands import pauses_listening
from ella_runtime.modules.voice.history import VoiceHistory
from ella_runtime.modules.voice.provider import Speech, Transcription, VoiceProviderError

logger = logging.getLogger(__name__)


class VoiceState(StrEnum):
    IDLE = "idle"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    SPEAKING = "speaking"
    INTERRUPTED = "interrupted"
    ERROR = "error"


class VoiceInterrupted(RuntimeError):
    """A newer turn or explicit interrupt invalidated this turn."""


class SpeechProvider(Protocol):
    async def transcribe(
        self, audio: bytes, *, filename: str, media_type: str, task_id: str | None = None
    ) -> Transcription: ...

    async def synthesize(self, text: str, *, task_id: str | None = None) -> Speech: ...


class ReplyGateway(Protocol):
    async def generate(self, request: ModelRequest) -> ModelResponse: ...


@dataclass(frozen=True)
class VoiceTurn:
    transcript: str
    reply: str
    audio: bytes | None
    media_type: str | None
    speech_error: str | None = None
    turn_id: str | None = None


class VoiceSession:
    def __init__(
        self, provider: SpeechProvider, gateway: ReplyGateway, usage_store: UsageStore, *,
        tts: bool, history: VoiceHistory | None = None, dialogue_actions=None, context=None,
    ) -> None:
        self.provider = provider
        self.gateway = gateway
        self.usage_store = usage_store
        self.tts = tts
        self.history = history
        self.dialogue_actions = dialogue_actions
        self.context = context
        self.state = VoiceState.IDLE
        self._generation = 0
        self._history: list[ModelMessage] = history.recent() if history is not None else []
        self._pending: tuple[str, str, str] | None = None
        self._remaining_reply = ""
        self.pause_requested = False
        self._memory_task: asyncio.Task[None] | None = None

    def context_messages(self) -> list[ModelMessage]:
        reference = self.context.reference_messages() if self.context is not None else []
        return [*reference, *self._history]

    async def action_reply(self, text: str) -> str | None:
        self.pause_requested = pauses_listening(text)
        if self.pause_requested:
            return "好，暂停监听。想继续时，点击人物或在后台恢复就行。"
        if self.dialogue_actions is None:
            return None
        identity = self.history.conversation_id if self.history else "voice-main"
        if self.context is not None:
            try:
                self.dialogue_actions.handoff_reference(self.context.get_context_source(), voice_id=identity)
            except ValueError as exc:
                return f"语音接续暂时不能切换：{exc}。请先处理当前语音任务。"
        return await self.dialogue_actions.process(text, self.context_messages(), identity, allow_confirmation=self.tts)

    def interrupt(self, *, generation: int | None = None) -> None:
        if generation is not None and generation != self._generation:
            return
        if self._pending is not None:
            self.acknowledge(self._pending[0], played_ratio=0)
        self._generation += 1
        self.state = VoiceState.INTERRUPTED

    def recover(self) -> None:
        self._generation += 1
        self.state = VoiceState.IDLE

    def _check(self, generation: int) -> None:
        if generation != self._generation:
            raise VoiceInterrupted("本轮语音已被打断")

    def acknowledge(self, turn_id: str, *, played_ratio: float) -> None:
        if self._pending is None or self._pending[0] != turn_id:
            raise ValueError("语音轮次已失效")
        if not 0 <= played_ratio <= 1:
            raise ValueError("播放进度无效")
        _, user, reply = self._pending
        if played_ratio >= 1:
            heard = reply
        else:
            estimated = int(len(reply) * played_ratio)
            boundary = max(
                (index + 1 for index, char in enumerate(reply[:estimated]) if char in "。！？!?"),
                default=0,
            )
            heard = reply[:boundary]
        self._remaining_reply = reply[len(heard):]
        self._remember(user, heard or "（艾拉的回复在播放前被打断）")
        self._pending = None
        self.state = VoiceState.IDLE

    async def turn(
        self, audio: bytes, *, filename: str, media_type: str, task_id: str | None = None
    ) -> VoiceTurn:
        if self._pending is not None:
            self.acknowledge(self._pending[0], played_ratio=0)
        self._generation += 1
        generation = self._generation
        self.state = VoiceState.TRANSCRIBING
        try:
            transcript = await self.provider.transcribe(
                audio, filename=filename, media_type=media_type, task_id=task_id
            )
            self._check(generation)
            self.usage_store.record(transcript.usage)
            if not transcript.text:
                raise VoiceProviderError("没有识别到语音内容")
            self.state = VoiceState.THINKING
            continuing = transcript.text.strip("，。！？!? ") in {
                "继续", "接着说", "继续刚才的", "接着刚才的说"
            }
            if continuing and self._remaining_reply:
                reply = self._remaining_reply
                self._remaining_reply = ""
            else:
                self._remaining_reply = ""
                action_reply = await self.action_reply(transcript.text)
                self._check(generation)
                if action_reply is None:
                    response = await self.gateway.generate(
                        ModelRequest(
                            purpose=ModelPurpose.VOICE,
                            messages=[
                                *self.context_messages(),
                                ModelMessage(role="user", content=transcript.text),
                            ],
                            task_id=task_id,
                        )
                    )
                    reply = response.text
                else:
                    reply = action_reply
            self._check(generation)
            if not self.tts:
                self._remember(transcript.text, "（语音合成未启用，回复未播出）")
                self.state = VoiceState.IDLE
                return VoiceTurn(transcript.text, reply, None, None)
            self.state = VoiceState.SPEAKING
            try:
                speech = await self.provider.synthesize(reply, task_id=task_id)
                self._check(generation)
                self.usage_store.record(speech.usage)
                turn_id = str(uuid4())
                self._pending = (turn_id, transcript.text, reply)
                result = VoiceTurn(
                    transcript.text, reply, speech.audio, speech.media_type, turn_id=turn_id
                )
            except VoiceProviderError as exc:
                self._check(generation)
                result = VoiceTurn(transcript.text, reply, None, None, str(exc))
                self._remaining_reply = reply
                self._remember(transcript.text, "（艾拉的语音播报失败）")
                self.state = VoiceState.IDLE
            return result
        except VoiceInterrupted:
            raise
        except Exception:
            if generation == self._generation:
                self.state = VoiceState.ERROR
            raise

    def _remember(self, user: str, assistant: str) -> None:
        self._history = [
            *self._history,
            ModelMessage(role="user", content=user),
            ModelMessage(role="assistant", content=assistant),
        ][-12:]
        if self.history is not None:
            self.history.record(user, assistant)

    async def flush_memory(self) -> None:
        if self.history is not None:
            await self.history.flush()

    def schedule_memory_flush(self) -> None:
        if self.history is None or (self._memory_task is not None and not self._memory_task.done()):
            return
        self._memory_task = asyncio.create_task(self.flush_memory())
        self._memory_task.add_done_callback(self._memory_finished)

    @staticmethod
    def _memory_finished(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error("语音记忆摘要失败", exc_info=task.exception())

    def clear_history(self) -> None:
        if self.state not in {VoiceState.IDLE, VoiceState.INTERRUPTED, VoiceState.ERROR}:
            raise ValueError("语音进行中，先结束当前对话")
        if self.history is not None:
            self.history.clear()
        self._history.clear()
        self._remaining_reply = ""
        self._pending = None
