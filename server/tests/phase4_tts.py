import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from pipeline.voice_pipeline import VoicePipeline
from services.tts import create_tts

SENTENCE = "Your payment was successful."
LONG_TEXT = (
    "Your payment of fifty dollars was successful. "
    "The money has been transferred to the merchant. "
    "You will receive a receipt by SMS shortly."
)


def test_sentence_buffer() -> bool:
    cases = [
        ("Your", None, "Your"),
        (" payment was successful.", "Your payment was successful.", ""),
        ("A long text without any punctuation mark at all goes on and on", None, None),
    ]
    buffer = ""
    emitted = []
    for token in ["Your", " payment", " was", " successful", ".", " The", " refund", " is", " done"]:
        buffer += token
        while True:
            chunk, buffer = VoicePipeline._extract_sentence(buffer)
            if chunk is None:
                break
            emitted.append(chunk)

    ok = emitted == ["Your payment was successful."] and buffer.strip() == "The refund is done"
    print(f"[TEST] sentence buffer: emitted={emitted!r} rest={buffer!r} -> {'PASS' if ok else 'FAIL'}")
    return ok


def test_force_split() -> bool:
    text = "This is a very long clause without punctuation marks that keeps going and going until it exceeds"
    chunk, rest = VoicePipeline._extract_sentence(text)
    ok = bool(chunk is not None and len(chunk) >= 20 and rest)
    print(f"[TEST] force split: chunk={chunk!r} rest={rest!r} -> {'PASS' if ok else 'FAIL'}")
    return ok


async def test_tts() -> bool:
    provider = settings.tts_provider
    print(f"[TEST] tts provider: {provider}")
    if provider != "sapi" and not settings.groq_api_key:
        print("[TEST] tts: FAIL — GROQ_API_KEY missing in server/.env")
        return False

    tts = create_tts()
    try:
        await tts.start()
    except Exception as exc:
        print(f"[TEST] tts: FAIL — start error: {exc!r}")
        return False

    try:
        t0 = time.time()
        pcm = await tts.synthesize(SENTENCE)
        elapsed = time.time() - t0
    except Exception as exc:
        print(f"[TEST] tts: FAIL — synthesis error: {exc!r}")
        await tts.close()
        return False
    finally:
        await tts.close()

    duration = len(pcm) / 2 / 48000
    # check non-silent: any sample above threshold
    import struct

    peak = 0
    for i in range(0, min(len(pcm), 48000 * 2), 200):
        (val,) = struct.unpack_from("<h", pcm, i)
        peak = max(peak, abs(val))

    print(f"[TEST] tts audio: {len(pcm)} bytes, {duration:.2f}s, peak={peak}, latency={elapsed*1000:.0f}ms")
    ok = duration >= 0.5 and duration <= 6.0 and peak > 1000 and elapsed < 10.0
    print(f"[TEST] tts -> {'PASS' if ok else 'FAIL'}")
    return ok


async def main() -> int:
    results = [test_sentence_buffer(), test_force_split(), await test_tts()]
    print(f"PHASE4: {sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
