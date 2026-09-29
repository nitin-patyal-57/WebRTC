import asyncio
import io
import struct
import sys
import wave

import httpx

from audio.processor import AudioProcessor
from audio.vad import EnergyVAD, VADEvent
from config import settings
from services.stt import GroqSTT

SERVER = "http://127.0.0.1:3000"
PHRASE = "What is my payment status?"


def _transcode_to_16k_mono_s16(path: str) -> bytes:
    import av
    from av.audio.resampler import AudioResampler

    resampler = AudioResampler(format="s16", layout="mono", rate=16000)
    parts: list[bytes] = []

    def take(frames) -> None:
        for frame in frames:
            if frame is None:
                continue
            sample_bytes = frame.samples * frame.format.bytes
            if frame.format.is_planar:
                parts.append(b"".join(bytes(p)[:sample_bytes] for p in frame.planes))
            else:
                parts.append(
                    bytes(frame.planes[0])[: sample_bytes * len(frame.layout.channels)]
                )

    with av.open(path) as container:
        stream = container.streams.audio[0]
        for frame in container.decode(stream):
            take(resampler.resample(frame))
        take(resampler.resample(None))
    return b"".join(parts)


def make_speech_pcm_16k() -> bytes:
    """Generate 16kHz mono s16 PCM speech using Windows SAPI TTS + PyAV transcode."""
    import os
    import subprocess
    import tempfile

    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.SetOutputToWaveFile('{out}'); "
        "$s.Speak('{phrase}'); $s.Dispose()"
    )
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "speech.wav")
        subprocess.run(
            ["powershell", "-Command", ps.format(out=out.replace("\\", "\\\\"), phrase=PHRASE)],
            check=True,
            capture_output=True,
        )
        return _transcode_to_16k_mono_s16(out)


def test_processor() -> bool:
    from av import AudioFrame
    from fractions import Fraction

    def make_frame(pts: int) -> AudioFrame:
        frame = AudioFrame(format="fltp", layout="stereo", samples=960)
        frame.sample_rate = 48000
        frame.pts = pts
        frame.time_base = Fraction(1, 48000)
        for plane in frame.planes:
            plane.update(b"\x00" * (960 * 4))
        return frame

    proc = AudioProcessor()
    proc.process(make_frame(0))  # first chunk absorbs resampler filter delay
    steady = proc.process(make_frame(960))
    ok = len(steady) == 1 and len(steady[0]) == 640  # 20ms @16kHz mono s16
    print(
        f"[TEST] processor steady chunk: {len(steady)} chunk(s), "
        f"{len(steady[0]) if steady else 0} bytes -> {'PASS' if ok else 'FAIL'}"
    )
    return ok


def test_vad() -> bool:
    vad = EnergyVAD(silence_ms=200, min_speech_ms=100, threshold=0.02)
    import math

    def tone(ms: int, amp: float) -> bytes:
        n = int(16000 * ms / 1000)
        return b"".join(
            struct.pack("<h", int(amp * 32767 * math.sin(2 * math.pi * 220 * i / 16000)))
            for i in range(n)
        )

    events = []
    for _ in range(5):
        events += vad.process(b"\x00\x00" * 320)
    for _ in range(6):  # 120ms >= min_speech_ms=100
        events += vad.process(tone(20, 0.5))
    for _ in range(15):  # 300ms >= silence_ms=200
        events += vad.process(b"\x00\x00" * 320)

    kinds = [e for e in events if e != VADEvent.SPEECH]
    ok = VADEvent.SPEECH_STARTED in kinds and VADEvent.SPEECH_STOPPED in kinds
    print(f"[TEST] vad events: {[e.value for e in kinds]} -> {'PASS' if ok else 'FAIL'}")
    return ok


async def test_stt() -> bool:
    if not settings.groq_api_key:
        print("[TEST] stt: FAIL — GROQ_API_KEY missing in server/.env")
        return False

    print("[TEST] Generating speech sample via Windows SAPI…")
    pcm = make_speech_pcm_16k()
    print(f"[TEST] speech sample: {len(pcm)} bytes ({len(pcm)/2/16000:.1f}s)")

    stt = GroqSTT()
    finals: list[str] = []
    partials: list[str] = []
    try:
        await stt.start()
    except Exception as exc:
        print(f"[TEST] stt: FAIL — start error: {exc!r}")
        return False

    try:
        stt.mark_speech_start()
        chunk = 3200  # 100ms
        for i in range(0, len(pcm), chunk):
            await stt.send_audio(pcm[i : i + chunk])
            await stt.maybe_partial()
            await asyncio.sleep(0.01)

        await stt.finalize()
        await asyncio.sleep(1.0)

        while not stt.queue.empty():
            evt = await stt.queue.get()
            if evt["type"] == "partial_transcript":
                partials.append(evt["text"])
            elif evt["type"] == "final_transcript":
                finals.append(evt["text"])
            elif evt["type"] == "error":
                print(f"[TEST] stt error event: {evt.get('message')}")
    finally:
        await stt.close()

    print(f"[TEST] partials: {partials}")
    print(f"[TEST] finals: {finals}")
    joined = " ".join(finals).lower()
    ok = "payment" in joined and "status" in joined
    print(f"[TEST] stt -> {'PASS' if ok else 'FAIL'}")
    return ok


async def test_health() -> bool:
    async with httpx.AsyncClient(timeout=5) as client:
        res = await client.get(f"{SERVER}/health")
        ok = res.status_code == 200 and res.json()["status"] == "ok"
        print(f"[TEST] health -> {'PASS' if ok else 'FAIL'}")
        return ok


async def main() -> int:
    results = [await test_health(), test_processor(), test_vad(), await test_stt()]
    passed = sum(results)
    print(f"\n[TEST] {passed}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
