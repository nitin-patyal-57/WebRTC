"""Streaming-reply helpers: LLM text chunking and TTS audio shaping."""

import array
import math
import re
from typing import Callable, List, Optional, Tuple

# One downlink frame: 20ms @ 8kHz s16 mono.
FRAME_BYTES = 320
FRAME_MS = 20

# Sentence / clause boundary: punctuation (optionally closed by a quote or
# bracket) followed by whitespace. Requiring whitespace keeps "3.5" and
# "1,000" whole while tokens are still streaming in.
BOUNDARY_RE = re.compile(r'([.!?,;])["\')\]]?\s+')
SENTENCE_END = ".!?"
FORCE_SPLIT_CHARS = 80
MIN_SPLIT_CHARS = 20
# A clause (, ;) is only its own TTS chunk once it has this many characters,
# so "Yes," or "Sure," does not become a separate request.
MIN_CLAUSE_CHARS = 12


def split_tts_chunk(buffer: str) -> Tuple[Optional[str], str]:
    """Return (chunk, rest): the first complete chunk of ``buffer``, or None."""
    for match in BOUNDARY_RE.finditer(buffer):
        chunk = buffer[: match.end()].strip()
        if not chunk:
            continue
        if match.group(1) in SENTENCE_END or len(chunk) >= MIN_CLAUSE_CHARS:
            return chunk, buffer[match.end() :]
    if len(buffer) >= FORCE_SPLIT_CHARS:
        cut = buffer.rfind(" ", 0, FORCE_SPLIT_CHARS)
        if cut >= MIN_SPLIT_CHARS:
            return buffer[:cut].strip(), buffer[cut:]
    return None, buffer


def frame_rms(frame: bytes) -> float:
    samples = array.array("h")
    samples.frombytes(frame[: len(frame) - (len(frame) % 2)])
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0


class ReplyAudioShaper:
    """Cuts one reply's TTS audio into 20ms frames and trims silence.

    - Leading silence of the reply is dropped (earlier first audio).
    - Silence inside a chunk is kept as synthesized.
    - Silence at a chunk boundary (trailing + next chunk's leading) is capped
      at ``max_pause_ms``, and dropped entirely if the outbound stream already
      ran dry (``is_starved``), since that pause has already happened.
    - Trailing silence of the reply is dropped by ``finish()``.
    """

    def __init__(
        self,
        threshold: float,
        max_pause_ms: int,
        is_starved: Callable[[], bool] = lambda: False,
    ) -> None:
        self.threshold = threshold
        self.max_pause_frames = max(0, max_pause_ms // FRAME_MS)
        self.is_starved = is_starved
        self._partial = bytearray()
        self._held: List[bytes] = []
        self._started = False
        self._at_boundary = False
        self.trimmed_frames = 0

    def feed(self, pcm: bytes) -> List[bytes]:
        """Add TTS PCM; return frames that are ready to send, in order."""
        self._partial += pcm
        out: List[bytes] = []
        while len(self._partial) >= FRAME_BYTES:
            frame = bytes(self._partial[:FRAME_BYTES])
            del self._partial[:FRAME_BYTES]
            self._push(frame, out)
        return out

    def end_chunk(self) -> List[bytes]:
        """The current chunk's synthesis is complete."""
        out: List[bytes] = []
        if self._partial:
            frame = bytes(self._partial).ljust(FRAME_BYTES, b"\x00")
            self._partial.clear()
            self._push(frame, out)
        self._at_boundary = True
        return out

    def finish(self) -> None:
        """The reply is complete: drop its trailing silence."""
        self.trimmed_frames += len(self._held)
        self._held.clear()
        self._partial.clear()

    def _push(self, frame: bytes, out: List[bytes]) -> None:
        if frame_rms(frame) < self.threshold:
            if self._started:
                self._held.append(frame)
            else:
                self.trimmed_frames += 1
            return
        if self._held:
            if self._at_boundary:
                keep = 0 if self.is_starved() else self.max_pause_frames
                self.trimmed_frames += max(0, len(self._held) - keep)
                out.extend(self._held[:keep])
            else:
                out.extend(self._held)
            self._held.clear()
        self._at_boundary = False
        self._started = True
        out.append(frame)
