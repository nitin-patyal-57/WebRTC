import asyncio
import time
from fractions import Fraction
from typing import Optional

from aiortc import MediaStreamTrack
from aiortc.mediastreams import MediaStreamError
from av import AudioFrame

from utils.logger import get_logger

log = get_logger("WEBRTC")

SAMPLE_RATE = 48000
CHANNELS = 1
SAMPLES_PER_FRAME = 960  # 20ms @48kHz
FRAME_BYTES = SAMPLES_PER_FRAME * 2  # s16 mono
PTIME = SAMPLES_PER_FRAME / SAMPLE_RATE
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

        start = time.monotonic()
        while time.monotonic() - start < PTIME:
            self._drain_queue()
            remaining = PTIME - (time.monotonic() - start)
            if remaining > 0:
                await asyncio.sleep(min(remaining, 0.004))
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
