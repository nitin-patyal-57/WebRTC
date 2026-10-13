import asyncio
import os
import subprocess
import tempfile
import time
from typing import AsyncIterator, Optional, Union

import av
import httpx
from av.audio.resampler import AudioResampler

from config import settings
from utils.logger import get_logger

log = get_logger("TTS")

GROQ_SPEECH_URL = "https://api.groq.com/openai/v1/audio/speech"
TARGET_RATE = 8000


class StreamingWavDecoder:
    """Incremental WAV -> 8kHz mono s16 PCM.

    Feed the response body as it arrives; PCM comes out as soon as the
    ``data`` chunk starts. A streamed header's data size is often a
    placeholder, so it is ignored and everything after it is audio.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._riff_checked = False
        self._header_done = False
        self._fmt: Optional[tuple] = None  # (audio_format, channels, rate, width)
        self._passthrough = False
        self._resampler: Optional[AudioResampler] = None
        self._align = 2

    def feed(self, data: bytes) -> bytes:
        self._buf += data
        if not self._header_done and not self._parse_header():
            return b""
        usable = len(self._buf) - (len(self._buf) % self._align)
        if not usable:
            return b""
        raw = bytes(self._buf[:usable])
        del self._buf[:usable]
        return self._convert(raw)

    def flush(self) -> bytes:
        if self._resampler is None:
            return b""
        return self._frames_to_pcm(self._resampler.resample(None) or [])

    def _parse_header(self) -> bool:
        buf = self._buf
        if not self._riff_checked:
            if len(buf) < 12:
                return False
            if buf[:4] != b"RIFF" or buf[8:12] != b"WAVE":
                raise ValueError("TTS response is not a WAV stream")
            del buf[:12]
            self._riff_checked = True
        while len(buf) >= 8:
            chunk_id = bytes(buf[:4])
            size = int.from_bytes(buf[4:8], "little")
            if chunk_id == b"data":
                del buf[:8]
                if self._fmt is None:
                    raise ValueError("WAV data chunk before fmt chunk")
                self._start_audio()
                return True
            padded = size + (size & 1)
            if len(buf) < 8 + padded:
                return False
            body = bytes(buf[8 : 8 + size])
            del buf[: 8 + padded]
            if chunk_id == b"fmt ":
                self._fmt = (
                    int.from_bytes(body[0:2], "little"),
                    int.from_bytes(body[2:4], "little"),
                    int.from_bytes(body[4:8], "little"),
                    int.from_bytes(body[14:16], "little") // 8,
                )
        return False

    def _start_audio(self) -> None:
        audio_format, channels, rate, width = self._fmt
        self._header_done = True
        self._align = channels * width
        if audio_format == 1 and rate == TARGET_RATE and channels == 1 and width == 2:
            self._passthrough = True
            return
        if audio_format == 3 and width == 4:
            self._av_format = "flt"
        else:
            self._av_format = {1: "u8", 2: "s16", 4: "s32"}.get(width)
        if self._av_format is None:
            raise ValueError(f"Unsupported WAV sample width: {width}")
        self._layout = "mono" if channels == 1 else "stereo"
        self._rate = rate
        self._resampler = AudioResampler(format="s16", layout="mono", rate=TARGET_RATE)

    def _convert(self, raw: bytes) -> bytes:
        if self._passthrough:
            return raw
        frame = av.AudioFrame(format=self._av_format, layout=self._layout, samples=len(raw) // self._align)
        frame.sample_rate = self._rate
        frame.planes[0].update(raw)
        return self._frames_to_pcm(self._resampler.resample(frame))

    @staticmethod
    def _frames_to_pcm(frames) -> bytes:
        parts: list[bytes] = []
        for out in frames:
            if out is None:
                continue
            sample_bytes = out.samples * out.format.bytes
            if out.format.is_planar:
                parts.append(b"".join(bytes(p)[:sample_bytes] for p in out.planes))
            else:
                parts.append(bytes(out.planes[0])[: sample_bytes * len(out.layout.channels)])
        return b"".join(parts)


def wav_to_pcm_8k(wav_bytes: bytes) -> bytes:
    """Parse WAV and normalize to 8kHz mono 16-bit PCM for WebRTC."""
    decoder = StreamingWavDecoder()
    return decoder.feed(wav_bytes) + decoder.flush()


class GroqTTS:
    """Sentence-level TTS over Groq Orpheus. Returns 8kHz mono s16 PCM."""

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

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        """Yield 8kHz mono s16 PCM pieces as the WAV response streams in."""
        text = text.strip()
        if not text:
            return
        if self._client is None:
            raise RuntimeError("TTS client not started")
        t0 = time.perf_counter()
        first_ms = None
        total = 0
        decoder = StreamingWavDecoder()
        async with self._client.stream(
            "POST",
            GROQ_SPEECH_URL,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "voice": self.voice,
                "input": text,
                "response_format": "wav",
            },
        ) as response:
            if response.status_code >= 400:
                detail = (await response.aread())[:400].decode("utf-8", "replace")
                raise RuntimeError(f"TTS HTTP {response.status_code}: {detail}")
            async for data in response.aiter_bytes():
                pcm = decoder.feed(data)
                if pcm:
                    if first_ms is None:
                        first_ms = (time.perf_counter() - t0) * 1000
                    total += len(pcm)
                    yield pcm
        tail = decoder.flush()
        if tail:
            total += len(tail)
            yield tail
        log.info(
            f"[TTS] API stream: first audio {first_ms or 0:.0f}ms, done "
            f"{(time.perf_counter() - t0) * 1000:.0f}ms: {len(text)} chars -> "
            f"{total} bytes ({total/2/TARGET_RATE:.2f}s)"
        )

    async def synthesize(self, text: str) -> bytes:
        """Convert one sentence to 8kHz mono s16 PCM bytes."""
        return b"".join([pcm async for pcm in self.synthesize_stream(text)])

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

        t0 = time.perf_counter()
        try:
            wav_bytes = await asyncio.to_thread(_speak)
        except Exception as exc:
            raise RuntimeError(f"SAPI TTS failed: {exc}") from exc
        pcm = wav_to_pcm_8k(wav_bytes)
        log.info(
            f"[TTS] SAPI synthesized in {(time.perf_counter() - t0) * 1000:.0f}ms: "
            f"{len(text)} chars -> {len(pcm)} bytes ({len(pcm)/2/TARGET_RATE:.2f}s)"
        )
        return pcm

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        """SAPI renders a whole file; yield it as a single piece."""
        pcm = await self.synthesize(text)
        if pcm:
            yield pcm

    async def close(self) -> None:
        log.info("[TTS] Closed")


def create_tts() -> Union[GroqTTS, SapiTTS]:
    if settings.tts_provider.lower() == "sapi":
        return SapiTTS()
    return GroqTTS()
