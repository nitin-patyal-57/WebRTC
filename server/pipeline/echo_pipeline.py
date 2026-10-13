import array
import asyncio
import time

from audio.processor import AudioProcessor
from audio.recorder import pcm_stats, recording_name, save_wav
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
        self._underruns = 0  # times the outbound ran dry mid-echo (rebuffered)
        self._held = bytearray()  # jitter prebuffer, see _echo
        self._prebuffer_bytes = max(1, settings.echo_prebuffer_ms * ECHO_RATE * 2 // 1000)
        self._last_audio_time = time.monotonic()
        # Debug recording of each utterance (RECORD_ECHO): the 8 kHz audio
        # that is echoed back, and the decoded input before resampling.
        self._utterances = 0
        self._rec_pcm = bytearray()
        self._rec_raw = bytearray()
        self._raw_format = ""
        self._raw_rate = 0

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
            if self._echoing and settings.record_echo:
                self._record_raw(frame)

    def _record_raw(self, frame) -> None:
        """Keep the first channel of the decoded input frame (s16 only)."""
        channels = len(frame.layout.channels)
        self._raw_format = f"{frame.format.name} {frame.sample_rate}Hz ch={channels}"
        self._raw_rate = frame.sample_rate
        if frame.format.name != "s16":
            return
        samples = array.array("h")
        samples.frombytes(bytes(frame.planes[0])[: frame.samples * 2 * channels])
        self._rec_raw += samples[::channels].tobytes()

    def _echo(self, pcm: bytes) -> None:
        session = self.session
        if not self._echoing:
            self._echoing = True
            self._echoed_bytes = 0
            self._underruns = 0
            self._utterances += 1
            self._rec_pcm.clear()
            self._rec_raw.clear()
            session.finish_turn()
            session.begin_turn()
            now = time.time() * 1000.0
            session.mark_stage("speech_start", now)
            # Audio is sent back as-is: it is "ready" the moment it arrives.
            session.mark_stage("tts_first_audio", now, record_dashboard=False)
            log.info("[ECHO] Echo started")
        self._echoed_bytes += len(pcm)
        if settings.record_echo:
            self._rec_pcm += pcm
        # Jitter buffer: inbound audio arrives unevenly while the track sends
        # exactly one 20 ms frame per 20 ms, inserting silence whenever it runs
        # dry. Hold audio until a prebuffer is ready (again after any underrun)
        # so playback stays continuous instead of chopped into noise.
        # (WebSocket clients schedule playback themselves: no ai_track there.)
        starved = session.ai_track is not None and self._outbound_starved()
        if starved and not self._held and self._echoed_bytes > len(pcm):
            self._underruns += 1
        if self._held or starved:
            self._held += pcm
            if len(self._held) < self._prebuffer_bytes:
                return
            pcm = bytes(self._held)
            self._held.clear()
        session.tts_audio_queue.put_nowait(pcm)

    def _outbound_starved(self) -> bool:
        """True when no echo audio is waiting to be sent."""
        session = self.session
        if not session.tts_audio_queue.empty():
            return False
        track = session.ai_track
        return track is None or not track.has_pending

    def _end_echo(self, detail: str) -> None:
        if not self._echoing:
            return
        self._echoing = False
        session = self.session
        if self._held:
            session.tts_audio_queue.put_nowait(bytes(self._held))
            self._held.clear()
        session.mark_stage("speech_end")
        session.speech_end_reason = "marker" if "00 FE" in detail else "no-audio"
        session.tts_audio_queue.put_nowait(REPLY_END)
        log.info(
            f"[ECHO] Echo ended ({detail}): {self._echoed_bytes} bytes "
            f"({self._echoed_bytes / (ECHO_RATE * 2):.2f}s), rebuffers={self._underruns}"
        )
        if settings.record_echo:
            self._save_recording()

    def _save_recording(self) -> None:
        """Save the utterance and log its stats (zcr ~0.5 = noise, not speech)."""
        sid = self.session.session_id
        try:
            echo = save_wav(recording_name(sid, self._utterances, "echo8k"), bytes(self._rec_pcm), ECHO_RATE)
            raw_note = ""
            if self._rec_raw:
                raw = save_wav(
                    recording_name(sid, self._utterances, f"in{self._raw_rate}"),
                    bytes(self._rec_raw),
                    self._raw_rate,
                )
                raw_note = f" in={pcm_stats(bytes(self._rec_raw))} file=/recordings/{raw.name}"
            log.info(
                f"[ECHO] Recorded utterance {self._utterances} input=({self._raw_format}) "
                f"echo={pcm_stats(bytes(self._rec_pcm))} file=/recordings/{echo.name}{raw_note}"
            )
        except Exception as exc:
            log.error(f"[ECHO] Recording failed: {exc!r}")
        finally:
            self._rec_pcm.clear()
            self._rec_raw.clear()

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
