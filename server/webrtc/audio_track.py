import asyncio
import time
from fractions import Fraction

from aiortc import MediaStreamTrack
from aiortc.mediastreams import MediaStreamError
from av import AudioFrame

from utils.logger import get_logger

log = get_logger("WEBRTC")

SAMPLE_RATE = 8000
CHANNELS = 1
SAMPLES_PER_FRAME = 160  # 20ms @8kHz
FRAME_BYTES = SAMPLES_PER_FRAME * 2  # s16 mono
SILENCE_FRAME = b"\x00" * FRAME_BYTES


class AIResponseAudioTrack(MediaStreamTrack):
    """Server-side audio track: pulls TTS PCM from the session queue and emits
    paced 20ms WebRTC frames. Outputs silence when no AI audio is pending so the
    RTP stream stays continuous."""

    kind = "audio"

    def __init__(self, session) -> None:
        super().__init__()
        self.session = session
        self._pending = bytearray()
        self._pts = 0
        self._pace_start = None
        self.frames_emitted = 0
        self._tx_log_t = time.monotonic()
        self._tx_log_frames = 0

    def clear(self) -> None:
        self._pending.clear()

    @property
    def has_pending(self) -> bool:
        return len(self._pending) > 0

    @property
    def playing(self) -> bool:
        return bool(self._pending) or not self.session.tts_audio_queue.empty()

    def _drain_queue(self) -> None:
        queue = self.session.tts_audio_queue
        while True:
            try:
                chunk = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if chunk:
                self._pending += chunk

    async def recv(self) -> AudioFrame:
        if self.readyState == "ended":
            raise MediaStreamError

        # aiortc's sender sends a packet as fast as recv() returns, so the
        # track itself must hold the clock: exactly one frame per 20 ms of
        # wall clock, never early (same pacing as the vendor reference
        # server). If the event loop stalled more than 0.2 s, re-anchor the
        # schedule instead of bursting to catch up.
        now = time.time()
        if self._pace_start is None:
            self._pace_start = now
        else:
            wait = self._pace_start + self._pts / SAMPLE_RATE - now
            if wait > 0:
                await asyncio.sleep(wait)
            elif wait < -0.2:
                self._pace_start = now - self._pts / SAMPLE_RATE

        # Do not wait for model output. A full frame is immediately available
        # from the queue; otherwise the track emits silence at the fixed 20ms
        # cadence so the RTP stream remains continuous.
        self._drain_queue()

        if self.readyState == "ended":
            raise MediaStreamError

        is_real = len(self._pending) >= FRAME_BYTES
        if is_real:
            data = bytes(self._pending[:FRAME_BYTES])
            del self._pending[:FRAME_BYTES]
            self._on_real_audio(data)
        else:
            data = SILENCE_FRAME

        frame = AudioFrame(format="s16", layout="mono", samples=SAMPLES_PER_FRAME)
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = Fraction(1, SAMPLE_RATE)
        frame.planes[0].update(data)
        self._pts += SAMPLES_PER_FRAME

        self.frames_emitted += 1
        self.session.audio_frames_emitted = self.frames_emitted
        t = time.monotonic()
        dt = t - self._tx_log_t
        if dt >= 10.0:
            rate = (self.frames_emitted - self._tx_log_frames) / dt
            log.info(
                f"[WEBRTC] Downlink tx: {rate:.0f} frames/s "
                f"(voice={self.session.audio_frames_sent} total={self.frames_emitted})"
            )
            self._tx_log_t = t
            self._tx_log_frames = self.frames_emitted

        if not self.playing and self.session.ai_playing:
            self.session.ai_playing = False

        return frame

    def _on_real_audio(self, data: bytes) -> None:
        session = self.session
        if not session.ai_playing:
            session.ai_playing = True
            log.info("[WEBRTC] Audio playback started")
        session.audio_frames_sent += 1
        session.send_audio_bytes(data)
        lat = session.latency
        if "tts_first_audio" in lat and "audio_sent" not in lat:
            session.mark_stage("audio_sent")
            session.emit_latency()
