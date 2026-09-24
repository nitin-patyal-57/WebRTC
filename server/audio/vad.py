import array
import math
from abc import ABC, abstractmethod
from enum import Enum
from typing import List

from utils.logger import get_logger

log = get_logger("VAD")


class VADEvent(str, Enum):
    SPEECH_STARTED = "speech_started"
    SPEECH = "speech"
    SPEECH_STOPPED = "speech_stopped"


class VAD(ABC):
    """Replaceable voice activity detection interface."""

    @abstractmethod
    def process(self, pcm: bytes, sample_rate: int = 16000) -> List[VADEvent]:
        """Consume one PCM chunk and return any VAD events it triggers."""

    @abstractmethod
    def reset(self) -> None:
        """Reset internal state (e.g. after an interruption)."""


class EnergyVAD(VAD):
    """RMS energy VAD with min-speech and silence-hangover thresholds."""

    def __init__(
        self,
        silence_ms: int = 700,
        min_speech_ms: int = 300,
        threshold: float = 0.025,
    ) -> None:
        self.silence_ms = silence_ms
        self.min_speech_ms = min_speech_ms
        self.threshold = threshold
        self.in_speech = False
        self._speech_ms = 0.0
        self._silence_ms = 0.0

    def reset(self) -> None:
        self.in_speech = False
        self._speech_ms = 0.0
        self._silence_ms = 0.0

    @property
    def is_speech_active(self) -> bool:
        return self.in_speech

    @staticmethod
    def _rms(pcm: bytes) -> float:
        if len(pcm) < 2:
            return 0.0
        samples = array.array("h")
        samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
        if not samples:
            return 0.0
        acc = 0
        for s in samples:
            acc += s * s
        return math.sqrt(acc / len(samples)) / 32768.0

    def process(self, pcm: bytes, sample_rate: int = 16000) -> List[VADEvent]:
        duration_ms = (len(pcm) / 2) / sample_rate * 1000.0
        loud = self._rms(pcm) >= self.threshold
        events: List[VADEvent] = []

        if not self.in_speech:
            if loud:
                self._speech_ms += duration_ms
                self._silence_ms = 0.0
                if self._speech_ms >= self.min_speech_ms:
                    self.in_speech = True
                    events.append(VADEvent.SPEECH_STARTED)
                    log.info(f"[VAD] Speech started (rms gate open)")
            else:
                self._speech_ms = 0.0
        else:
            if loud:
                self._silence_ms = 0.0
                events.append(VADEvent.SPEECH)
            else:
                self._silence_ms += duration_ms
                if self._silence_ms >= self.silence_ms:
                    self.in_speech = False
                    self._speech_ms = 0.0
                    self._silence_ms = 0.0
                    events.append(VADEvent.SPEECH_STOPPED)
                    log.info(f"[VAD] Speech stopped ({self.silence_ms}ms silence)")
        return events
