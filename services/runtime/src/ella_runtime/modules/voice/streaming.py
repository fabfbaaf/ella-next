"""Bounded PCM turns, concurrent model/TTS streams and playback acknowledgements."""

from __future__ import annotations

import asyncio
import base64
import io
import re
import wave
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from ella_runtime.modules.models.contracts import ModelMessage, ModelPurpose, ModelRequest
from ella_runtime.modules.models.provider import ModelProviderError
from ella_runtime.modules.voice.provider import VoiceProviderError
from ella_runtime.modules.voice.session import VoiceInterrupted, VoiceSession, VoiceState

SendEvent = Callable[[dict[str, object]], Awaitable[None]]
SAMPLE_RATE = 16000
MAX_AUDIO_BYTES = SAMPLE_RATE * 2 * 60
SENTENCE_END = re.compile(r"[。！？!?；;\n]+|\.(?=\s|$)")
MAX_SEGMENT_CHARS = 160
MAX_PENDING_SENTENCES = 3
MAX_AUDIO_IN_FLIGHT = 4
PLAYBACK_ACK_TIMEOUT = 90
MAX_ECHO_REFERENCE_CHARS = 500


def wav_audio(pcm: bytes) -> bytes:
    with io.BytesIO() as output:
        with wave.open(output, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(SAMPLE_RATE)
            writer.writeframes(pcm)
        return output.getvalue()


def matches_playback_echo(text: str, reference: str) -> bool:
    """Guard against sending the just-played reply back to the chat model."""
    spoken = "".join(char.casefold() for char in text if char.isalnum())
    played = "".join(char.casefold() for char in reference if char.isalnum())
    if len(spoken) < 8 or not played:
        return False
    return spoken in played or SequenceMatcher(None, spoken, played).ratio() >= 0.88


@dataclass
class PlaybackLedger:
    user: str
    segments: list[str] = field(default_factory=list)
    acknowledged: int = 0
    reply: str = ""
    complete: bool = False
    saved: bool = False

    def acknowledge(self, index: int) -> None:
        if index != self.acknowledged or index >= len(self.segments):
            raise ValueError("播放确认顺序无效")
        self.acknowledged += 1

    @property
    def heard(self) -> str:
        return "".join(self.segments[:self.acknowledged])


class StreamingVoiceTurn:
    def __init__(self, session: VoiceSession, send: SendEvent) -> None:
        self.session = session
        self.send = send
        self.pcm = bytearray()
        self.ledger: PlaybackLedger | None = None
        self._finished_input = False
        self._reply_task: asyncio.Task[None] | None = None
        self._closed = False
        self._speech_failed = False
        self._generation: int | None = None
        self._echo_reference = ""
        self._playback_changed = asyncio.Event()

    async def feed(self, chunk: bytes) -> None:
        if self._finished_input or self._closed:
            raise ValueError("录音已结束")
        if not chunk or len(chunk) % 2:
            raise ValueError("PCM 数据无效")
        if len(self.pcm) + len(chunk) > MAX_AUDIO_BYTES:
            raise ValueError("录音超过 60 秒")
        self.pcm.extend(chunk)
        # Compatible /audio/transcriptions is an utterance endpoint. Upload once
        # after VAD completion instead of retranscribing every growing prefix.

    async def finish_input(self, *, echo_reference: str = "") -> None:
        if self._finished_input or not self.pcm:
            raise ValueError("录音为空或已经发送")
        if not isinstance(echo_reference, str) or len(echo_reference) > MAX_ECHO_REFERENCE_CHARS:
            raise ValueError("播放回声参考无效")
        self._echo_reference = echo_reference
        self._finished_input = True
        self._reply_task = asyncio.create_task(self._reply())

    async def _reply(self) -> None:
        session = self.session
        session._generation += 1
        generation = session._generation
        self._generation = generation
        session.state = VoiceState.TRANSCRIBING
        try:
            transcript = await session.provider.transcribe(
                wav_audio(bytes(self.pcm)), filename="recording.wav", media_type="audio/wav"
            )
            session._check(generation)
            session.usage_store.record(transcript.usage)
            if not transcript.text:
                raise ValueError("没有识别到语音内容")
            if self._echo_reference and matches_playback_echo(transcript.text, self._echo_reference):
                session.state = VoiceState.IDLE
                await self.send({"type": "audio_echo"})
                return
            await self.send({"type": "transcript", "text": transcript.text})
            self.ledger = PlaybackLedger(transcript.text)
            ledger = self.ledger
            session.state = VoiceState.THINKING
            continuing = transcript.text.strip("，。！？!? ") in {
                "继续", "接着说", "继续刚才的", "接着刚才的说"
            }
            if continuing and session._remaining_reply:
                saved_reply = session._remaining_reply
                session._remaining_reply = ""

                async def chunks():
                    yield saved_reply
            else:
                session._remaining_reply = ""
                action_reply = await session.action_reply(transcript.text)
                session._check(generation)
                if session.pause_requested:
                    await self.send({"type": "listening_pause"})
                if action_reply is None:
                    request = ModelRequest(
                        purpose=ModelPurpose.VOICE,
                        messages=[*session.context_messages(), ModelMessage(role="user", content=transcript.text)],
                    )
                    chunks = lambda: session.gateway.stream_voice(request)
                else:
                    async def chunks():
                        yield action_reply

            sentences: asyncio.Queue[str | None] = asyncio.Queue(maxsize=MAX_PENDING_SENTENCES)

            async def receive_model() -> None:
                pending = ""
                async for delta in chunks():
                    session._check(generation)
                    ledger.reply += delta
                    pending += delta
                    await self.send({"type": "reply_delta", "text": delta})
                    while True:
                        match = SENTENCE_END.search(pending)
                        if match is None and len(pending) < MAX_SEGMENT_CHARS:
                            break
                        boundary = min(match.end(), MAX_SEGMENT_CHARS) if match else MAX_SEGMENT_CHARS
                        sentence, pending = pending[:boundary], pending[boundary:]
                        await sentences.put(sentence)
                if pending.strip():
                    await sentences.put(pending)
                await sentences.put(None)

            async def synthesize_sentences() -> None:
                while True:
                    sentence = await sentences.get()
                    if sentence is None:
                        return
                    session._check(generation)
                    await self._speak(sentence, ledger, generation)

            workers = [asyncio.create_task(receive_model()), asyncio.create_task(synthesize_sentences())]
            try:
                await asyncio.gather(*workers)
            finally:
                for worker in workers:
                    if not worker.done():
                        worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
            session._check(generation)
            ledger.complete = True
            await self.send({"type": "reply_done", "segments": len(ledger.segments)})
            self._save_if_complete()
            session.schedule_memory_flush()
        except asyncio.CancelledError:
            return
        except VoiceInterrupted:
            if not self._closed:
                await self.send({"type": "interrupted"})
        except (VoiceProviderError, ModelProviderError, ValueError) as exc:
            if generation == session._generation:
                session.state = VoiceState.ERROR
            await self.send({"type": "error", "message": str(exc)})
        except Exception:  # noqa: BLE001 - every failed turn must release client capture
            if generation == session._generation:
                session.state = VoiceState.ERROR
            await self.send({"type": "error", "message": "语音内部处理失败，请在后台重试或检查服务状态"})

    async def _speak(self, sentence: str, ledger: PlaybackLedger, generation: int) -> None:
        if not self.session.tts or self._speech_failed:
            return
        while len(ledger.segments) - ledger.acknowledged >= MAX_AUDIO_IN_FLIGHT:
            self._playback_changed.clear()
            try:
                await asyncio.wait_for(self._playback_changed.wait(), PLAYBACK_ACK_TIMEOUT)
            except TimeoutError as exc:
                raise VoiceProviderError("音频播放确认超时，请重新开始语音对话") from exc
            self.session._check(generation)
        self.session.state = VoiceState.SPEAKING
        try:
            speech = await self.session.provider.synthesize(sentence)
        except VoiceProviderError as exc:
            await self.send({"type": "speech_error", "message": str(exc)})
            self._speech_failed = True
            return
        self.session._check(generation)
        self.session.usage_store.record(speech.usage)
        index = len(ledger.segments)
        ledger.segments.append(sentence)
        await self.send({
            "type": "audio_segment", "index": index, "text": sentence,
            "audio_base64": base64.b64encode(speech.audio).decode(),
            "media_type": speech.media_type,
        })

    def acknowledge(self, index: int) -> None:
        ledger = self.ledger
        if ledger is None:
            raise ValueError("没有待确认的语音片段")
        ledger.acknowledge(index)
        self._playback_changed.set()
        self._save_if_complete()

    def _save_if_complete(self) -> None:
        ledger = self.ledger
        if ledger is None or ledger.saved or not ledger.complete:
            return
        if not self.session.tts or ledger.acknowledged == len(ledger.segments):
            if not self.session.tts:
                self.session._remember(ledger.user, "（语音合成未启用，回复未播出）")
            elif self._speech_failed:
                heard = ledger.heard
                if self._generation == self.session._generation:
                    self.session._remaining_reply = ledger.reply[len(heard):]
                self.session._remember(ledger.user, heard or "（艾拉的语音播报失败）")
            else:
                self.session._remember(ledger.user, ledger.reply)
            if self._generation == self.session._generation:
                self.session.state = VoiceState.IDLE
            ledger.saved = True

    async def interrupt(self) -> None:
        if self._closed:
            return
        self._closed = True
        current = self._generation is not None and self._generation == self.session._generation
        if current:
            self.session.interrupt(generation=self._generation)
        if self._reply_task is not None:
            self._reply_task.cancel()
            await asyncio.gather(self._reply_task, return_exceptions=True)
        ledger = self.ledger
        if ledger is not None and not ledger.saved:
            heard = ledger.heard
            if current:
                self.session._remaining_reply = ledger.reply[len(heard):]
            self.session._remember(ledger.user, heard or "（艾拉的回复在播放前被打断）")
            ledger.saved = True
        self.session.schedule_memory_flush()
