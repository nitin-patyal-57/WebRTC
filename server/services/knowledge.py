import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config import settings
from utils.logger import get_logger

log = get_logger("KNOWLEDGE")

KB_PATH = Path(__file__).resolve().parent.parent / "data" / "soundbox_kb.json"

TOKEN_RE = re.compile(r"[a-z0-9']+")

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "did", "do",
    "does", "for", "from", "had", "has", "have", "he", "her", "him", "his", "how",
    "i", "if", "in", "into", "is", "it", "its", "me", "my", "of", "on",
    "or", "our", "out", "over", "she", "so", "some", "than", "that", "the", "their",
    "them", "then", "there", "these", "they", "this", "those", "to", "too", "up",
    "very", "was", "we", "were", "what", "when", "where", "which", "who", "why",
    "will", "with", "would", "you", "your", "am", "can", "could", "should", "whats",
    "it's", "there's", "any", "all", "about", "after", "again", "also", "today",
    "ago", "never", "fine", "ok", "okay", "normal", "please", "really", "still",
    "just", "much", "well", "maybe", "thing", "things", "stuff", "minute", "get",
    "take", "want", "need", "let", "us", "know", "long", "before", "everything",
    "dont", "doesn't", "doesnt", "isnt", "cant", "wont", "didnt", "wasnt",
    "werent", "isn't", "can't", "won't", "didn't", "wasn't", "don't", "even",
    "always", "ever", "else", "lot", "many", "quite",
    "someone", "somebody", "anyone", "anybody",
}

# Topics outside the curated Soundbox Q&A: never answer these from the KB.
OUT_OF_SCOPE = {
    "upi", "refund", "balance", "receipt", "recharge", "bill", "invoice",
    "offer", "discount", "loan", "emi", "warranty", "coupon", "cashback",
    "statement", "pin", "otp", "password", "email", "subscription", "delivery",
    "address", "fee", "limit", "interest", "installment", "installments",
    "battery", "charger", "volume", "weather", "news",
}

SYNONYMS = {
    "pay": "pay", "payment": "pay", "paid": "pay", "payd": "pay", "paying": "pay",
    "pays": "pay", "payer": "pay", "money": "pay", "cash": "pay", "amount": "pay",
    "deduct": "pay", "deducted": "pay", "debited": "pay", "credit": "pay",
    "credited": "pay",
    "transaction": "transaction", "txn": "transaction",
    "sale": "transaction", "sales": "transaction", "order": "transaction",
    "announce": "announce", "announcement": "announce", "announcements": "announce",
    "announced": "announce", "announcing": "announce", "alert": "announce",
    "alerts": "announce", "notify": "announce", "notification": "announce",
    "notifications": "announce", "confirm": "announce", "confirmed": "announce",
    "speak": "speak", "speaking": "speak", "spoke": "speak", "spoken": "speak",
    "voice": "speak", "voiced": "speak", "voicing": "speak", "say": "speak",
    "says": "speak", "said": "speak", "tell": "speak", "telling": "speak",
    "talk": "speak", "talking": "speak", "talked": "speak",
    "sound": "sound", "speaker": "sound", "noise": "sound", "noisy": "sound",
    "loud": "sound", "loudly": "sound", "soundbox": "soundbox", "box": "soundbox",
    "machine": "device", "device": "device", "unit": "device", "terminal": "device",
    "gadget": "device", "hardware": "device",
    "silent": "silent", "silence": "silent", "quiet": "silent", "mute": "silent",
    "muted": "silent", "nothing": "silent",
    "receive": "receive", "received": "receive", "receiving": "receive",
    "getting": "receive", "got": "receive", "reach": "receive", "reaches": "receive",
    "reaching": "receive", "come": "receive", "coming": "receive", "came": "receive",
    "send": "receive", "sent": "receive", "sending": "receive",
    "stay": "silent", "stayed": "silent", "staying": "silent",
    "connect": "connect", "connected": "connect", "connecting": "connect",
    "connection": "connect", "connectivity": "connect", "online": "connect",
    "disconnect": "connect", "disconnected": "connect",
    "network": "network", "networks": "network", "internet": "network",
    "wifi": "network", "signal": "network",
    "sim": "sim", "simcard": "sim", "chip": "sim",
    "light": "light", "lights": "light", "led": "light", "lamp": "light",
    "indicator": "light", "indication": "light",
    "blink": "blink", "blinking": "blink", "blinks": "blink", "flash": "blink",
    "flashing": "blink", "flashed": "blink", "steady": "solid", "solid": "solid",
    "searching": "search", "search": "search", "looking": "search",
    "customer": "customer", "customers": "customer", "buyer": "customer",
    "client": "customer",
    "start": "start", "started": "start", "starting": "start", "boot": "start",
    "booting": "start", "power": "start", "powered": "start", "turn": "start",
    "turning": "start", "wake": "start", "wakeup": "start",
    "plug": "power", "plugged": "power", "plugin": "power",
    "work": "work", "working": "work", "works": "work", "broken": "work",
    "issue": "issue", "issues": "issue", "problem": "issue", "problems": "issue",
    "wrong": "issue", "fault": "issue", "error": "issue", "dead": "issue",
    "act": "issue", "acting": "issue", "behave": "issue", "behaving": "issue",
    "weird": "issue",
    "find": "find", "found": "find", "detect": "find", "detected": "find",
    "detection": "find", "pick": "find", "picked": "find", "picking": "find",
    "pickup": "find",
    "check": "check", "checking": "check", "fix": "check",
}


def _suffix_stem(token: str) -> str:
    if len(token) <= 3:
        return token
    if token.endswith("ies") and len(token) > 4:
        token = token[:-3] + "y"
    elif token.endswith("ing") and len(token) > 5:
        token = token[:-3]
        if len(token) > 3 and token[-1] == token[-2]:
            token = token[:-1]
    elif token.endswith("ed") and len(token) > 4:
        token = token[:-2]
        if len(token) > 3 and token[-1] == token[-2]:
            token = token[:-1]
    elif token.endswith("s") and not token.endswith("ss") and len(token) > 3:
        token = token[:-1]
    return token


def _canon(token: str) -> str:
    seen = set()
    current = token
    for _ in range(4):
        if current in seen:
            break
        seen.add(current)
        if current in SYNONYMS:
            current = SYNONYMS[current]
            continue
        stemmed = _suffix_stem(current)
        if stemmed == current:
            break
        current = stemmed
    return current


def tokenize(text: str) -> List[str]:
    tokens: List[str] = []
    for raw in TOKEN_RE.findall(text.lower()):
        if raw in STOPWORDS:
            continue
        token = _canon(raw)
        if token in STOPWORDS or len(token) <= 2:
            continue
        tokens.append(token)
    return tokens


def normalize(text: str) -> str:
    return " ".join(tokenize(text))


@dataclass
class KBMatch:
    intent: str
    led_state: Optional[str]
    context: str
    response: str
    instruction: str
    score: float


class KnowledgeBase:
    """Fuzzy lookup over the curated Soundbox Q&A training data.

    A user utterance that is similar to a training instruction gets the exact
    canned response for that intent instead of a free-form LLM answer.
    """

    def __init__(self, path: Path = KB_PATH) -> None:
        self.path = path
        self.contexts: Dict[str, str] = {}
        self.entries: List[dict] = []
        self._index: List[Tuple[int, str, set, str]] = []
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.error(f"[KNOWLEDGE] Failed to load {self.path}: {exc!r}")
            return
        self.contexts = data.get("contexts", {})
        for entry in data.get("entries", []):
            context = self.contexts.get(entry.get("context", ""), entry.get("context", ""))
            record = {
                "intent": entry.get("intent", ""),
                "led_state": entry.get("led_state"),
                "context": context,
                "response": entry.get("response", ""),
                "instructions": list(entry.get("instructions", [])),
            }
            idx = len(self.entries)
            self.entries.append(record)
            for instruction in record["instructions"]:
                tokens = set(tokenize(instruction))
                self._index.append((idx, instruction, tokens, normalize(instruction)))
        log.info(
            f"[KNOWLEDGE] Loaded {len(self.entries)} entries, "
            f"{len(self._index)} instructions from {self.path.name}"
        )

    @property
    def loaded(self) -> bool:
        return bool(self._index)

    @staticmethod
    def _score(query_tokens: set, query_norm: str, inst_tokens: set, inst_norm: str) -> float:
        if not query_tokens or not inst_tokens:
            return 0.0
        overlap = query_tokens & inst_tokens
        if not overlap:
            return 0.0
        coverage = len(overlap) / len(query_tokens)
        jaccard = len(overlap) / len(query_tokens | inst_tokens)
        ratio = SequenceMatcher(None, query_norm, inst_norm).ratio()
        score = 0.5 * coverage + 0.3 * jaccard + 0.2 * ratio
        # LED state clash: a query about a blinking light must not match an
        # instruction about a solid light (and vice versa).
        if ("blink" in query_tokens and "solid" not in query_tokens and "solid" in inst_tokens) or (
            "solid" in query_tokens and "blink" not in query_tokens and "blink" in inst_tokens
        ):
            score *= 0.6
        return score

    def match(self, text: str, threshold: Optional[float] = None) -> Optional[KBMatch]:
        if not text or not self._index:
            return None
        cutoff = settings.kb_match_threshold if threshold is None else threshold
        query_tokens = set(tokenize(text))
        query_norm = normalize(text)
        if not query_tokens:
            return None
        off_topic = query_tokens & OUT_OF_SCOPE
        if off_topic:
            log.debug(f"[KNOWLEDGE] Out-of-scope tokens {sorted(off_topic)} in: {text!r}")
            return None

        best: Optional[KBMatch] = None
        best_score = 0.0
        for idx, instruction, inst_tokens, inst_norm in self._index:
            score = self._score(query_tokens, query_norm, inst_tokens, inst_norm)
            if score > best_score:
                best_score = score
                entry = self.entries[idx]
                best = KBMatch(
                    intent=entry["intent"],
                    led_state=entry["led_state"],
                    context=entry["context"],
                    response=entry["response"],
                    instruction=instruction,
                    score=score,
                )
        if best is not None and best.score >= cutoff:
            return best
        return None

    def facts(self) -> str:
        """Unique device facts for the LLM system prompt (fallback path)."""
        seen: List[str] = []
        for context in self.contexts.values():
            if context and context not in seen:
                seen.append(context)
        return "\n".join(f"- {c}" for c in seen)


knowledge = KnowledgeBase()
