import asyncio
import io
import time
import wave
from typing import Optional

import httpx

from config import settings
from utils.logger import get_logger

log = get_logger("STT")

GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2  # mono s16

MAX_SPEECH_SECONDS = 30
PREROLL_SECONDS = 0.45
SILENCE_PREROLL_SECONDS = 5.0
PARTIAL_MIN_MS = 900


def pcm_to_wav(pcm: bytes) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)
    return buffer.getvalue()


class GroqSTT:
    """VAD-segmented streaming STT: buffers utterance PCM, transcribes with Groq Whisper.

    Emits partial_transcript / final_transcript / error events on an asyncio.Queue.
    """

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None) -> None:
        self.api_key = api_key or settings.groq_api_key
        self.model = model or settings.groq_stt_model
        self.language = settings.stt_language
        self.queue: asyncio.Queue = asyncio.Queue()
        self._client: Optional[httpx.Client] = None
        self._running = False
        self._buffer = bytearray()
        self._lock = asyncio.Lock()
        self._speech_started = False
        self._keep_from = 0
        self._last_partial_ms = 0.0
        self._partial_task: Optional[asyncio.Task] = None

    async def _emit(self, event: dict) -> None:
        await self.queue.put(event)

    async def start(self) -> None:
        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY is not set")
        self._client = httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0))
        self._running = True
        log.info(f"[STT] Connected model={self.model} (Groq Whisper, VAD-segmented)")
        await self._emit({"type": "stt_connected"})

    def mark_speech_start(self) -> None:
        """Called on VAD speech_started: anchor keep-point to include the onset window."""
        preroll = int((settings.vad_min_speech_ms / 1000.0 + PREROLL_SECONDS) * BYTES_PER_SECOND)
        self._keep_from = max(0, len(self._buffer) - preroll)
        self._speech_started = True

    async def send_audio(self, pcm: bytes) -> None:
        if not self._running:
            return
        async with self._lock:
            self._buffer += pcm
            await self._trim_locked()

    async def _trim_locked(self) -> None:
        if self._speech_started:
            max_len = MAX_SPEECH_SECONDS * BYTES_PER_SECOND
            speech_len = len(self._buffer) - self._keep_from
            if speech_len > max_len:
                overflow = speech_len - max_len
                del self._buffer[:overflow]
                self._keep_from = max(0, self._keep_from - overflow)
        else:
            max_len = int(SILENCE_PREROLL_SECONDS * BYTES_PER_SECOND)
            if len(self._buffer) > max_len:
                del self._buffer[: len(self._buffer) - max_len]

    def _snapshot_locked(self) -> bytes:
        return bytes(self._buffer[self._keep_from :])

    async def maybe_partial(self) -> None:
        """Transcribe the growing buffer occasionally for live partial display."""
        if not self._running or not self._speech_started:
            return
        now = time.time() * 1000.0
        if now - self._last_partial_ms < PARTIAL_MIN_MS:
            return
        if self._partial_task is not None and not self._partial_task.done():
            return
        async with self._lock:
            pcm = self._snapshot_locked()
        if len(pcm) < int(0.8 * BYTES_PER_SECOND):
            return
        self._last_partial_ms = now
        self._partial_task = asyncio.create_task(self._run_partial(pcm))

    async def _run_partial(self, pcm: bytes) -> None:
        try:
            text = await self._transcribe(pcm)
            if text:
                await self._emit({"type": "partial_transcript", "text": text})
        except Exception as exc:
            log.debug(f"[STT] Partial transcription skipped: {exc!r}")

    async def finalize(self) -> None:
        """Called on VAD speech_stopped: snapshot utterance and transcribe in background."""
        if not self._running:
            return
        async with self._lock:
            pcm = self._snapshot_locked()
            self._buffer.clear()
            self._keep_from = 0
            self._speech_started = False
            self._last_partial_ms = 0.0
        if len(pcm) < int(0.4 * BYTES_PER_SECOND):
            log.info("[STT] Utterance too short, ignored")
            return
        task = self._partial_task
        if task is not None and not task.done():
            task.cancel()
        asyncio.create_task(self._run_final(pcm))

    async def _run_final(self, pcm: bytes) -> None:
        try:
            text = await self._transcribe(pcm)
        except Exception as exc:
            log.error(f"[STT] Transcription failed: {exc!r}")
            await self._emit({"type": "error", "message": f"STT failed: {exc}"})
            return
        await self._emit({"type": "final_transcript", "text": text})

    def _transcribe_sync(self, pcm: bytes) -> str:
        if self._client is None:
            raise RuntimeError("STT client not started")
        wav = pcm_to_wav(pcm)
        data = {
            "model": self.model,
            "response_format": "json",
            "temperature": "0",
        }
        if self.language:
            data["language"] = self.language
        response = self._client.post(
            GROQ_TRANSCRIBE_URL,
            headers={"Authorization": f"Bearer {self.api_key}"},
            data=data,
            files={"file": ("speech.wav", wav, "audio/wav")},
        )
        response.raise_for_status()
        return (response.json().get("text") or "").strip()

    async def _transcribe(self, pcm: bytes) -> str:
        return await asyncio.to_thread(self._transcribe_sync, pcm)

    async def send_audio_blocking_flush(self) -> None:
        """No-op kept for interface symmetry with streaming STT providers."""
        return None

    async def close(self) -> None:
        self._running = False
        task = self._partial_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self._client is not None:
            self._client.close()
            self._client = None
        log.info("[STT] Closed")
