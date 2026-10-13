"""Debug recordings: save echoed utterances as WAV and summarize their audio."""

import array
import math
import time
import wave
from pathlib import Path
from typing import Dict

from config import settings
from utils.logger import get_logger

log = get_logger("AUDIO")

RECORDINGS_DIR = Path(__file__).resolve().parent.parent / "recordings"


def pcm_stats(pcm: bytes) -> Dict[str, float]:
    """Level and shape of s16 mono PCM: speech has a moderate zero-crossing
    rate (~0.05-0.15); white noise or misdecoded bytes sit near 0.5."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    n = len(samples)
    if not n:
        return {"rms": 0.0, "peak": 0.0, "clipped_pct": 0.0, "zcr": 0.0}
    rms = math.sqrt(sum(s * s for s in samples) / n) / 32768.0
    peak = max(abs(s) for s in samples) / 32768.0
    clipped = sum(1 for s in samples if s >= 32000 or s <= -32000)
    crossings = sum(1 for a, b in zip(samples, samples[1:]) if (a < 0) != (b < 0))
    return {
        "rms": round(rms, 4),
        "peak": round(peak, 4),
        "clipped_pct": round(100.0 * clipped / n, 2),
        "zcr": round(crossings / max(1, n - 1), 3),
    }


def save_wav(name: str, pcm: bytes, rate: int) -> Path:
    """Write s16 mono PCM to recordings/<name>.wav, keeping the newest files."""
    RECORDINGS_DIR.mkdir(exist_ok=True)
    path = RECORDINGS_DIR / f"{name}.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    _prune()
    return path


def recording_name(session_id: str, index: int, kind: str) -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}_{session_id[:8]}_{index:03d}_{kind}"


def _prune() -> None:
    files = sorted(RECORDINGS_DIR.glob("*.wav"), key=lambda p: p.stat().st_mtime)
    for old in files[: max(0, len(files) - settings.record_max_files)]:
        try:
            old.unlink()
        except OSError as exc:
            log.debug(f"[AUDIO] Could not prune {old.name}: {exc!r}")
