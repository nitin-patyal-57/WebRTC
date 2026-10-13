import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.fixed_answers import fixed_answers
from services.knowledge import knowledge
from services.live_answers import detect_intent, live_answers

ROUTING = [
    ("what's the weather today", "weather"),
    ("what is the temperature in pune", "weather"),
    ("will it rain tomorrow", "weather"),
    ("what is the gold price today", "gold_price"),
    ("gold rate", "gold_price"),
    ("what was my last transaction", "transactions"),
    ("tell me my last 3 payments", "transactions"),
    ("give me today's summary", "transactions"),
    ("how much did i collect yesterday", "transactions"),
    ("how many payments did i receive today", "transactions"),
    ("show transactions on 11 october", "transactions"),
    ("my transaction is not being voiced", None),
    ("payment received but soundbox didn't announce it", None),
    ("why is the red light blinking", None),
    ("tell me a joke", None),
]

LIVE_QUERIES = [
    "what's the weather today",
    "weather in delhi tomorrow",
    "what is the gold price today",
    "what was my last transaction",
    "tell me my last 3 payments",
    "give me today's summary",
    "today's transactions with details",
    "how much did i collect yesterday",
    "transaction summary this week",
]


def routing_test() -> int:
    failed = 0
    for text, want in ROUTING:
        got = detect_intent(text)
        if got != want:
            failed += 1
            print(f"[ROUTE] FAIL {text!r} -> {got} (want {want})")
    # Curated Soundbox questions and fixed answers must never be hijacked.
    for entry in knowledge.entries:
        for instruction in entry["instructions"]:
            got = detect_intent(instruction)
            if got is not None:
                failed += 1
                print(f"[ROUTE] KB instruction hijacked {instruction!r} -> {got}")
    for question in fixed_answers._map:
        if detect_intent(question) is not None:
            failed += 1
            print(f"[ROUTE] fixed answer hijacked {question!r}")
    print(f"[ROUTE] {'OK' if not failed else f'{failed} failures'}")
    return failed


async def live_run() -> None:
    for text in LIVE_QUERIES:
        intent, answer = await live_answers.answer(text)
        print(f"\nQ: {text}\n[{intent}] {answer}")


if __name__ == "__main__":
    failures = routing_test()
    if "--offline" not in sys.argv:
        asyncio.run(live_run())
    sys.exit(1 if failures else 0)
