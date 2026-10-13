"""Inbound Soundbox end-of-speech packet (RTP payload 00 FE).

The device can send one RTP packet with the 2-byte payload ``00 FE`` when it
has detected the end of the user's speech. aiortc would otherwise decode it
as two audio samples. We intercept it before depayloading, notify the
session, and substitute a normal 20 ms u-law silence payload so the audio
jitter buffer still sees a contiguous sequence (dropping it would stall the
buffer until 16 more packets arrived). The marker itself never reaches the
decoder or the VAD as audio.
"""

import time
from typing import Any, Callable, Dict, Tuple

from aiortc.rtcrtpreceiver import RTCRtpReceiver

from audio.g711 import END_OF_SPEECH_PAYLOAD, ULAW_SILENCE_FRAME, ulaw_decode
from utils.logger import get_logger

log = get_logger("WEBRTC")

_installed = False
MAX_LATE_PACKETS = 100


def intercept_end_of_speech(receiver: Any, packet: Any) -> bool:
    """Handle a PCMU ``00 FE`` packet in place; True if it was one."""
    if packet.payload != END_OF_SPEECH_PAYLOAD:
        return False
    codecs = getattr(receiver, "_RTCRtpReceiver__codecs", {})
    codec = codecs.get(packet.payload_type)
    if codec is None or codec.mimeType.lower() != "audio/pcmu":
        return False
    packet.payload = ULAW_SILENCE_FRAME
    callback: Callable[[], None] | None = getattr(receiver, "_on_end_of_speech", None)
    if callback is not None:
        try:
            callback()
        except Exception as exc:
            log.error(f"[WEBRTC] End-of-speech callback failed: {exc!r}")
    return True


class UplinkCapture:
    """Debug: every inbound RTP packet as it arrives, before aiortc's jitter
    buffer (which drops audio around sequence gaps). Shows exactly what the
    device sent: packet count, sequence gaps, reordering and arrival timing.
    """

    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self.payloads: Dict[int, bytes] = {}  # extended seq -> u-law payload
        self.packets = 0
        self.duplicates = 0
        self.reordered = 0
        self.markers = 0
        self.other_codec = 0
        self._last_ext = None
        self._max_ext = None
        self._first_arrival = None
        self._last_arrival = None
        self._max_gap_ms = 0.0
        self._first_ts = None
        self._last_ts = None

    def add(self, packet: Any, is_pcmu: bool) -> None:
        now = time.monotonic()
        if self._last_arrival is not None:
            self._max_gap_ms = max(self._max_gap_ms, (now - self._last_arrival) * 1000.0)
        else:
            self._first_arrival = now
        self._last_arrival = now
        self.packets += 1
        if packet.payload == END_OF_SPEECH_PAYLOAD:
            self.markers += 1
            return
        if not is_pcmu:
            self.other_codec += 1
            return
        seq = packet.sequence_number
        if self._last_ext is None:
            ext = seq
        else:
            diff = (seq - self._last_ext) & 0xFFFF
            ext = self._last_ext + (diff - 0x10000 if diff >= 0x8000 else diff)
        if self._max_ext is not None and ext < self._max_ext:
            self.reordered += 1
        self._max_ext = ext if self._max_ext is None else max(self._max_ext, ext)
        self._last_ext = ext
        if ext in self.payloads:
            self.duplicates += 1
        self.payloads[ext] = bytes(packet.payload)
        if self._first_ts is None:
            self._first_ts = packet.timestamp
        self._last_ts = packet.timestamp

    def take(self) -> Tuple[Dict[str, Any], bytes]:
        """Stats and s16 8 kHz PCM (in sequence order) since the last take."""
        seqs = sorted(self.payloads)
        missing = (seqs[-1] - seqs[0] + 1 - len(seqs)) if seqs else 0
        span = (self._last_arrival - self._first_arrival) * 1000.0 if self.packets else 0.0
        stats = {
            "packets": self.packets,
            "audio_packets": len(seqs),
            "seq": f"{seqs[0] & 0xFFFF}-{seqs[-1] & 0xFFFF}" if seqs else "-",
            "missing": missing,
            "reordered": self.reordered,
            "duplicates": self.duplicates,
            "eos_markers": self.markers,
            "non_pcmu": self.other_codec,
            "audio_ms": round(sum(len(self.payloads[s]) for s in seqs) / 8.0),
            "arrival_span_ms": round(span),
            "max_arrival_gap_ms": round(self._max_gap_ms),
            "ts": f"{self._first_ts}-{self._last_ts}",
        }
        pcm = ulaw_decode(b"".join(self.payloads[s] for s in seqs))
        self._reset()
        return stats, pcm


def deliver_pcmu_audio(receiver: Any, packet: Any) -> None:
    """Hand PCMU audio straight to the session, bypassing aiortc's jitter
    buffer: with capacity 16 / prefetch 4 it stalls at any sequence gap and
    drops ~15 frames per lost packet (a short "hello" came out as 0.3 s).
    G.711 needs no decoder state, so each packet is decoded on its own. Late
    or duplicate packets are dropped; gaps are skipped (the echo's playout
    buffer keeps the output continuous)."""
    callback = getattr(receiver, "_on_pcmu_audio", None)
    if callback is None:
        return
    codec = getattr(receiver, "_RTCRtpReceiver__codecs", {}).get(packet.payload_type)
    if codec is None or codec.mimeType.lower() != "audio/pcmu" or not packet.payload:
        return
    last = getattr(receiver, "_pcmu_last_seq", None)
    if last is not None:
        behind = (last - packet.sequence_number) & 0xFFFF
        # Slightly behind = late/duplicate. Far behind = the device restarted
        # its sequence numbers (it starts at 0 per utterance): resync.
        if behind < MAX_LATE_PACKETS:
            return
    receiver._pcmu_last_seq = packet.sequence_number
    try:
        callback(ulaw_decode(packet.payload))
    except Exception as exc:
        log.error(f"[WEBRTC] PCMU audio callback failed: {exc!r}")


def log_raw_packet(receiver: Any, packet: Any) -> None:
    """Debug: log the first RTP_DEBUG_PACKETS inbound packets as received."""
    from config import settings

    seen = getattr(receiver, "_debug_packets_logged", 0)
    if seen >= settings.rtp_debug_packets:
        return
    receiver._debug_packets_logged = seen + 1
    codecs = getattr(receiver, "_RTCRtpReceiver__codecs", {})
    codec = codecs.get(packet.payload_type)
    name = f"{codec.mimeType}/{codec.clockRate}" if codec is not None else "unknown"
    payload = bytes(packet.payload)
    log.info(
        f"[WEBRTC] RTP in #{seen + 1}: pt={packet.payload_type} ({name}) "
        f"seq={packet.sequence_number} ts={packet.timestamp} marker={packet.marker} "
        f"len={len(payload)} head={payload[:24].hex(' ')}"
    )


def install_end_of_speech_hook() -> None:
    global _installed
    if _installed:
        return
    original = RTCRtpReceiver._handle_rtp_packet

    async def handle_rtp_packet(self: Any, packet: Any, arrival_time_ms: int) -> None:
        log_raw_packet(self, packet)
        capture = getattr(self, "_uplink_capture", None)
        if capture is not None:
            codec = getattr(self, "_RTCRtpReceiver__codecs", {}).get(packet.payload_type)
            capture.add(packet, codec is not None and codec.mimeType.lower() == "audio/pcmu")
        if not intercept_end_of_speech(self, packet):
            deliver_pcmu_audio(self, packet)
        await original(self, packet, arrival_time_ms)

    RTCRtpReceiver._handle_rtp_packet = handle_rtp_packet
    _installed = True
