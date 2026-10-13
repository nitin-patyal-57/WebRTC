import asyncio
import time

from audio.processor import AudioProcessor
from config import settings
from utils.logger import get_logger
from webrtc.session import END_OF_SPEECH, REPLY_END, Session

log = get_logger("PIPELINE")

ECHO_RATE = 8000  # downlink track / WS pump format: 8kHz s16 mono
NO_AUDIO_POLL_S = 0.05


class EchoPipeline:
    """Per-session loopback: every received audio frame is sent straight back.

    Inbound audio (any rate/layout) is resampled to 8kHz s16 mono and queued
    for the downlink. The reply is closed with REPLY_END (00 FF marker on
    PCMU) when the device sends end-of-speech (00 FE) or inbound audio stops.
    """

    # The peer heartbeat reads pipeline.vad; the echo path has no VAD.
    vad = None

    def __init__(self, session: Session) -> None:
        self.session = session
        self.processor = AudioProcessor(rate=ECHO_RATE)
        self._tasks: list[asyncio.Task] = []
        self._stopping = False
        self._echoing = False  # audio echoed since the last REPLY_END
        self._echoed_bytes = 0
        self._last_audio_time = time.monotonic()

    async def start(self) -> None:
        session = self.session
        session.pipeline = self
        self._tasks = [
            asyncio.create_task(self._audio_loop(), name=f"echo:{session.session_id[:8]}"),
            asyncio.create_task(self._no_audio_watchdog(), name=f"noaudio:{session.session_id[:8]}"),
        ]
        log.info(f"[PIPELINE] Echo started for session {session.session_id}")

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        self.session.pipeline = None
        log.info(f"[PIPELINE] Echo stopped for session {self.session.session_id}")

    async def _audio_loop(self) -> None:
        # Persistent: an error must never stop the echo. Only cancellation ends it.
        while True:
            try:
                await self._audio_loop_body()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error(f"[PIPELINE] Echo loop error (continuing): {exc!r}")

    async def _audio_loop_body(self) -> None:
        session = self.session
        while True:
            frame = await session.audio_queue.get()
            if frame is END_OF_SPEECH:
                self._end_echo("end-of-speech packet 00 FE")
                continue
            self._last_audio_time = time.monotonic()
            for pcm in self.processor.process(frame):
                self._echo(pcm)

    def _echo(self, pcm: bytes) -> None:
        session = self.session
        if not self._echoing:
            self._echoing = True
            self._echoed_bytes = 0
            session.finish_turn()
            session.begin_turn()
            now = time.time() * 1000.0
            session.mark_stage("speech_start", now)
            # Audio is sent back as-is: it is "ready" the moment it arrives.
            session.mark_stage("tts_first_audio", now, record_dashboard=False)
            log.info("[ECHO] Echo started")
        self._echoed_bytes += len(pcm)
        session.tts_audio_queue.put_nowait(pcm)

    def _end_echo(self, detail: str) -> None:
        if not self._echoing:
            return
        self._echoing = False
        session = self.session
        session.mark_stage("speech_end")
        session.speech_end_reason = "marker" if "00 FE" in detail else "no-audio"
        session.tts_audio_queue.put_nowait(REPLY_END)
        log.info(
            f"[ECHO] Echo ended ({detail}): {self._echoed_bytes} bytes "
            f"({self._echoed_bytes / (ECHO_RATE * 2):.2f}s)"
        )

    async def _no_audio_watchdog(self) -> None:
        """Close the echoed reply when inbound audio stops arriving."""
        try:
            while True:
                await asyncio.sleep(NO_AUDIO_POLL_S)
                if not self._echoing:
                    continue
                idle_ms = (time.monotonic() - self._last_audio_time) * 1000.0
                if idle_ms >= settings.vad_no_audio_ms:
                    self._end_echo(f"no audio for {idle_ms:.0f}ms")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(f"[PIPELINE] No-audio watchdog error: {exc!r}")
