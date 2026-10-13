"""G.711 u-law helpers and the Soundbox in-band RTP control payloads."""

import array

FRAME_SAMPLES = 160  # 20 ms @ 8 kHz
ULAW_SILENCE_BYTE = 0xFF
ULAW_SILENCE_FRAME = bytes([ULAW_SILENCE_BYTE]) * FRAME_SAMPLES

# 2-byte RTP payloads the Soundbox uses as in-band signals (never audio).
END_OF_REPLY_PAYLOAD = b"\x00\xff"  # server -> device: reply finished
END_OF_SPEECH_PAYLOAD = b"\x00\xfe"  # device -> server: user stopped talking

_BIAS = 0x84
_CLIP = 32635


def _encode_sample(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0
    if sample < 0:
        sample = -sample
    sample = min(sample, _CLIP) + _BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not sample & mask:
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def _decode_byte(code: int) -> int:
    code = ~code & 0xFF
    sign = code & 0x80
    exponent = (code >> 4) & 0x07
    mantissa = code & 0x0F
    sample = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return -sample if sign else sample


# Indexed by the unsigned 16-bit view of each s16 sample.
_ENCODE_TABLE = bytes(_encode_sample(s - 65536 if s >= 32768 else s) for s in range(65536))
_DECODE_TABLE = array.array("h", (_decode_byte(c) for c in range(256)))


def ulaw_encode(pcm: bytes) -> bytes:
    """s16 little-endian mono PCM -> u-law bytes (one byte per sample)."""
    samples = array.array("H")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    return bytes(map(_ENCODE_TABLE.__getitem__, samples))


def ulaw_decode(data: bytes) -> bytes:
    """u-law bytes -> s16 little-endian mono PCM."""
    out = array.array("h", (_DECODE_TABLE[b] for b in data))
    return out.tobytes()
