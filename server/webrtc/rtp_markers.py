"""Inbound Soundbox end-of-speech packet (RTP payload 00 FE).

The device can send one RTP packet with the 2-byte payload ``00 FE`` when it
has detected the end of the user's speech. aiortc would otherwise decode it
as two audio samples. We intercept it before depayloading, notify the
session, and substitute a normal 20 ms u-law silence payload so the audio
jitter buffer still sees a contiguous sequence (dropping it would stall the
buffer until 16 more packets arrived). The marker itself never reaches the
decoder or the VAD as audio.
"""

from typing import Any, Callable

from aiortc.rtcrtpreceiver import RTCRtpReceiver

from audio.g711 import END_OF_SPEECH_PAYLOAD, ULAW_SILENCE_FRAME
from utils.logger import get_logger

log = get_logger("WEBRTC")

_installed = False


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
        intercept_end_of_speech(self, packet)
        await original(self, packet, arrival_time_ms)

    RTCRtpReceiver._handle_rtp_packet = handle_rtp_packet
    _installed = True
