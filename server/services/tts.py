import asyncio
import io
import os
import subprocess
import tempfile
import wave
from typing import Optional, Union

import httpx
from av.audio.resampler import AudioResampler

from config import settings
from utils.logger import get_logger

log = get_logger("TTS")

GROQ_SPEECH_URL = "https://api.groq.com/openai/v1/audio/speech"
TARGET_RATE = 48000


def wav_to_pcm_48k(wav_bytes: bytes) -> bytes:
    """Parse WAV and normalize to 48kHz mono 16-bit PCM for WebRTC."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())

    if rate == TARGET_RATE and channels == 1 and width == 2:
        return frames

    import av

    resampler = AudioResampler(format="s16", layout="mono", rate=TARGET_RATE)
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        src_rate = wf.getframerate()
        src_channels = wf.getnchannels()
        src_width = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())

    fmt = {1: "u8", 2: "s16", 4: "s32"}.get(src_width)
    if fmt is None:
        raise ValueError(f"Unsupported WAV sample width: {src_width}")

    frame = av.AudioFrame(format=fmt, layout="mono" if src_channels == 1 else "stereo", samples=len(raw) // (src_width * src_channels))
    frame.sample_rate = src_rate
    layout_samples = len(raw) // (src_width * src_channels)
    frame.planes[0].update(raw)

    parts: list[bytes] = []
    for out in resampler.resample(frame):
        if out is None:
            continue
        sample_bytes = out.samples * out.format.bytes
        if out.format.is_planar:
            parts.append(b"".join(bytes(p)[:sample_bytes] for p in out.planes))
        else:
            parts.append(bytes(out.planes[0])[: sample_bytes * len(out.layout.channels)])
    for out in resampler.resample(None) or []:
        sample_bytes = out.samples * out.format.bytes
        if out.format.is_planar:
            parts.append(b"".join(bytes(p)[:sample_bytes] for p in out.planes))
        else:
            parts.append(bytes(out.planes[0])[: sample_bytes * len(out.layout.channels)])
    return b"".join(parts)


class GroqTTS:
    """Sentence-level TTS over Groq Orpheus. Returns 48kHz mono s16 PCM."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        voice: Optional[str] = None,
    ) -> None:
        self.api_key = api_key or settings.groq_api_key
        self.model = model or settings.groq_tts_model
        self.voice = voice or settings.groq_tts_voice
        self._client: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY is not set")
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))
        log.info(f"[TTS] Connected model={self.model} voice={self.voice}")

    async def synthesize(self, text: str) -> bytes:
        """Convert one sentence to 48kHz mono s16 PCM bytes."""
        if self._client is None:
            raise RuntimeError("TTS client not started")
        text = text.strip()
        if not text:
            return b""
        response = await self._client.post(
            GROQ_SPEECH_URL,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "voice": self.voice,
                "input": text,
                "response_format": "wav",
            },
        )
        if response.status_code >= 400:
            detail = response.text[:400]
            raise RuntimeError(f"TTS HTTP {response.status_code}: {detail}")
        pcm = wav_to_pcm_48k(response.content)
        log.info(
            f"[TTS] Synthesized {len(text)} chars -> {len(pcm)} bytes "
            f"({len(pcm)/2/TARGET_RATE:.2f}s)"
        )
        return pcm

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        log.info("[TTS] Closed")


class SapiTTS:
    """Local Windows SAPI TTS fallback (development only). Same interface as GroqTTS."""

    async def start(self) -> None:
        log.info("[TTS] Windows SAPI local provider active (dev fallback)")

    async def synthesize(self, text: str) -> bytes:
        text = text.strip()
        if not text:
            return b""

        def _speak() -> bytes:
            with tempfile.TemporaryDirectory() as tmp:
                wav_path = os.path.join(tmp, "out.wav")
                txt_path = os.path.join(tmp, "in.txt")
                with open(txt_path, "w", encoding="utf-8") as fh:
                    fh.write(text)
                ps = (
                    "Add-Type -AssemblyName System.Speech; "
                    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                    f"$s.SetOutputToWaveFile('{wav_path}'); "
                    f"$s.Speak([IO.File]::ReadAllText('{txt_path}')); "
                    "$s.Dispose()"
                )
                subprocess.run(
                    ["powershell", "-Command", ps],
                    check=True,
                    capture_output=True,
                    timeout=15,
                )
                with open(wav_path, "rb") as fh:
                    return fh.read()

        try:
            wav_bytes = await asyncio.to_thread(_speak)
        except Exception as exc:
            raise RuntimeError(f"SAPI TTS failed: {exc}") from exc
        pcm = wav_to_pcm_48k(wav_bytes)
        log.info(
            f"[TTS] Synthesized {len(text)} chars -> {len(pcm)} bytes "
            f"({len(pcm)/2/TARGET_RATE:.2f}s)"
        )
        return pcm

    async def close(self) -> None:
        log.info("[TTS] Closed")


def create_tts() -> Union[GroqTTS, SapiTTS]:
    if settings.tts_provider.lower() == "sapi":
        return SapiTTS()
    return GroqTTS()
