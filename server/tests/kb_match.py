import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.knowledge import knowledge

HELD_OUT = [
    ("my buyer paid but the box stayed quiet", {"transaction_not_announced"}),
    ("money got deducted but no voice came out of the soundbox", {"transaction_not_announced"}),
    ("the customer sent the payment but the machine stayed silent", {"transaction_not_announced"}),
    ("no sound after someone paid me", {"transaction_not_announced"}),
    ("payments are not reaching my box at all", {"transaction_not_received"}),
    ("i am not getting any payment notifications", {"transaction_not_received"}),
    ("the blue indicator never stops flashing", {"network_searching", "led_status_query"}),
    ("how long does it search before it finds the network", {"network_searching"}),
    ("red light flashing means my box is not online right", {"network_not_connected", "led_status_query"}),
    ("is everything fine if the blue light is steady", {"network_connected", "led_status_query"}),
    ("my sim is not being picked up by the unit", {"sim_not_detected"}),
    ("does the green flashing mean the sim is missing", {"sim_not_detected", "led_status_query"}),
    ("i just switched the power on and the green light is solid", {"device_starting", "led_status_query"}),
    ("the box was plugged in a minute ago and shows green", {"device_starting"}),
    ("my soundbox is acting up and i dont know where to start", {"general_soundbox_troubleshooting"}),
    ("something is wrong with my payment device help", {"general_soundbox_troubleshooting"}),
    ("payment showed up on the app but the soundbox said nothing", {"transaction_not_announced"}),
    ("blinking red on my soundbox what do i do", {"network_not_connected", "led_status_query"}),
]

NEGATIVE = [
    "what is the weather today",
    "tell me a joke",
    "how much does a pizza cost",
    "what is two plus two",
    "can you translate hello into french",
    "who won the cricket match last night",
]


def self_test() -> tuple[int, int]:
    expected = defaultdict(set)
    for entry in knowledge.entries:
        for instruction in entry["instructions"]:
            expected[instruction].add((entry["intent"], entry["led_state"]))

    passed = failed = 0
    for instruction, want in sorted(expected.items()):
        match = knowledge.match(instruction)
        got = (match.intent, match.led_state) if match else None
        ok = match is not None and got in want
        if ok:
            passed += 1
        else:
            failed += 1
            print(f"[SELF] FAIL {instruction!r} -> {got} (want {want})")
    return passed, failed


def held_out_test() -> tuple[int, int]:
    passed = failed = 0
    for text, want in HELD_OUT:
        match = knowledge.match(text)
        got = match.intent if match else None
        ok = match is not None and got in want
        if ok:
            passed += 1
        else:
            failed += 1
            print(f"[HELD] FAIL {text!r} -> {got} score={match.score if match else 0:.2f} (want {want})")
    return passed, failed


def negative_test() -> tuple[int, int]:
    passed = failed = 0
    for text in NEGATIVE:
        match = knowledge.match(text)
        if match is None:
            passed += 1
        else:
            failed += 1
            print(f"[NEG]  FAIL {text!r} -> matched {match.intent} score={match.score:.2f}")
    return passed, failed


def main() -> int:
    if not knowledge.loaded:
        print("[TEST] knowledge: FAIL — knowledge base not loaded")
        return 1

    print(f"[TEST] knowledge: {len(knowledge.entries)} entries, {len(knowledge._index)} instructions")
    results = {
        "self": self_test(),
        "held_out": held_out_test(),
        "negative": negative_test(),
    }
    ok = True
    for name, (passed, failed) in results.items():
        status = "PASS" if failed == 0 else "FAIL"
        if failed:
            ok = False
        print(f"[TEST] knowledge/{name}: {status} — {passed} passed, {failed} failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
