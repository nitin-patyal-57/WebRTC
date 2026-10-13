import asyncio
import time
from fractions import Fraction

from aiortc import MediaStreamTrack
from aiortc.mediastreams import MediaStreamError
from av import AudioFrame, Packet

from audio.g711 import END_OF_REPLY_PAYLOAD, ULAW_SILENCE_FRAME, ulaw_encode
from utils.logger import get_logger
from webrtc.session import REPLY_END

log = get_logger("WEBRTC")

SAMPLE_RATE = 8000
CHANNELS = 1
SAMPLES_PER_FRAME = 160  # 20ms @8kHz
FRAME_BYTES = SAMPLES_PER_FRAME * 2  # s16 mono
SILENCE_FRAME = b"\x00" * FRAME_BYTES
TIME_BASE = Fraction(1, SAMPLE_RATE)


class AIResponseAudioTrack(MediaStreamTrack):
    """Server-side audio track: pulls TTS PCM from the session queue and emits
    paced 20ms WebRTC frames. Outputs silence when no AI audio is pending so the
    RTP stream stays continuous.

    When the negotiated downlink codec is PCMU the track returns pre-encoded
    u-law ``av.Packet``s instead of ``AudioFrame``s. aiortc then only packs
    them, so the track owns the RTP timestamp of every packet (+160 per
    20 ms slot). That is what lets it emit the 2-byte ``00 FF`` end-of-reply
    marker in its own slot: aiortc's PCMU encoder re-chunks frames through an
    AudioFifo that rejects pts gaps, so a marker cannot go through it.
    """

    kind = "audio"

    def __init__(self, session) -> None:
        super().__init__()
        self.session = session
        self._pending = bytearray()
        self._end_pending = False  # REPLY_END reached: marker after _pending drains
        self._reply_open = False  # voice sent since the last end marker
        self._marker_requested = False  # barge-in: send the marker next slot
        self._pts = 0
        self._pace_start = None
        self.frames_emitted = 0
        self.markers_sent = 0
        self._tx_log_t = time.monotonic()
        self._tx_log_frames = 0

    def clear(self) -> None:
        self._pending.clear()
        self._end_pending = False

    def cancel_reply(self) -> None:
        """Barge-in: drop the rest of the reply and end it with the marker."""
        self.clear()
        if self._reply_open:
            self._marker_requested = True

    @property
    def has_pending(self) -> bool:
        return len(self._pending) > 0 or self._end_pending

    @property
    def playing(self) -> bool:
        return self.has_pending or not self.session.tts_audio_queue.empty()

    @property
    def packet_mode(self) -> bool:
        return getattr(self.session, "downlink_codec", None) == "audio/pcmu"

    def _fill(self) -> None:
        """Pull queued audio until one frame is available or the reply ends.

        Stops at REPLY_END so the marker lands exactly after the reply's last
        voice frame, ahead of anything queued for a later reply."""
        queue = self.session.tts_audio_queue
        while len(self._pending) < FRAME_BYTES and not self._end_pending:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if item is REPLY_END:
                self._end_pending = True
            elif item:
                self._pending += item

    async def recv(self):
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
        self._fill()

        if self.readyState == "ended":
            raise MediaStreamError

        marker = False
        data = None
        if self._marker_requested:
            self._marker_requested = False
            marker = True
        elif len(self._pending) >= FRAME_BYTES:
            data = bytes(self._pending[:FRAME_BYTES])
            del self._pending[:FRAME_BYTES]
        elif self._end_pending:
            if self._pending:  # partial last frame: pad it, marker next slot
                data = bytes(self._pending).ljust(FRAME_BYTES, b"\x00")
                self._pending.clear()
            else:
                self._end_pending = False
                marker = True

        if data is not None:
            self._on_real_audio(data)
        elif marker:
            self._on_reply_end()

        if self.packet_mode:
            if marker:
                payload = END_OF_REPLY_PAYLOAD
            elif data is not None:
                payload = ulaw_encode(data)
            else:
                payload = ULAW_SILENCE_FRAME
            frame = Packet(payload)
        else:
            frame = AudioFrame(format="s16", layout="mono", samples=SAMPLES_PER_FRAME)
            frame.sample_rate = SAMPLE_RATE
            frame.planes[0].update(data if data is not None else SILENCE_FRAME)
        frame.pts = self._pts
        frame.time_base = TIME_BASE
        self._pts += SAMPLES_PER_FRAME

        if marker:
            self._after_marker()

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
        self._reply_open = True
        session.audio_frames_sent += 1
        session.send_audio_bytes(data)
        session.note_audio_chunk_sent(len(data))
        lat = session.latency
        if "tts_first_audio" in lat and "audio_sent" not in lat:
            session.mark_stage("audio_sent")
            session.emit_latency()
        session.mark_stage("last_voice_rtp_sent", record_dashboard=False)

    def _on_reply_end(self) -> None:
        self._reply_open = False
        self.session.ai_playing = False

    def _after_marker(self) -> None:
        session = self.session
        if self.packet_mode:
            self.markers_sent += 1
            session.mark_stage("end_marker_sent")
            log.info(f"[WEBRTC] End-of-reply marker 00 FF sent (pts={self._pts - SAMPLES_PER_FRAME})")
        session.end_response_send("complete")
        session.finish_turn()
