import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from services.llm import GroqLLM

QUESTION = "What is my payment status?"


async def test_llm() -> bool:
    if not settings.groq_api_key:
        print("[TEST] llm: FAIL — GROQ_API_KEY missing in server/.env")
        return False

    llm = GroqLLM()
    try:
        await llm.start()
    except Exception as exc:
        print(f"[TEST] llm: FAIL — start error: {exc!r}")
        return False

    history = [{"role": "user", "content": QUESTION}]
    chunks: list[str] = []
    started = time.time()
    first_token_at = None

    try:
        async for delta in llm.stream(history):
            if first_token_at is None:
                first_token_at = time.time() - started
            chunks.append(delta)
            print(f"[TEST] token: {delta!r}")
    except Exception as exc:
        print(f"[TEST] llm: FAIL — stream error: {exc!r}")
        await llm.close()
        return False
    finally:
        await llm.close()

    text = "".join(chunks)
    total = time.time() - started
    print(f"[TEST] full response: {text!r}")
    print(f"[TEST] first token: {first_token_at*1000:.0f} ms, total: {total*1000:.0f} ms, chunks={len(chunks)}")

    has_markdown = "#" in text or "*" in text or "-" in text.strip()[:2]
    word_count = len(text.split())
    ok = (
        len(chunks) >= 2
        and word_count > 0
        and word_count <= 30
        and not has_markdown
        and first_token_at is not None
        and first_token_at < 3.0
    )
    print(f"[TEST] llm -> {'PASS' if ok else 'FAIL'}")
    return ok


async def main() -> int:
    ok = await test_llm()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
