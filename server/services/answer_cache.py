import json
import re
import threading
from pathlib import Path
from typing import Dict, Optional

from config import settings
from utils.logger import get_logger

log = get_logger("ANSWER_CACHE")

CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "answer_cache.json"

_PUNCT_RE = re.compile(r"[^\w\s']")

# Questions containing these words can change every time -> never remember them.
DYNAMIC_TOKENS = {
    "weather", "forecast", "temperature", "rain", "sunny",
    "time", "date", "today", "yesterday", "tomorrow", "day", "month", "year",
    "news", "summary", "summarize", "latest", "recent", "current", "now",
    "balance", "transaction", "txn", "last", "recently",
}


def normalize(text: str) -> str:
    text = _PUNCT_RE.sub(" ", text.lower())
    return " ".join(text.split())


def is_dynamic(text: str) -> bool:
    """True if the answer could change over time -> must not be remembered."""
    tokens = set(re.findall(r"[a-z0-9']+", text.lower()))
    return bool(tokens & DYNAMIC_TOKENS)


class AnswerCache:
    """Remembers question -> answer pairs that were already answered once.

    First time: full pipeline (KB or LLM) answers the question.
    Next time: O(1) exact lookup serves the same answer instantly.
    Persisted to disk so it survives restarts. Real-time questions
    (weather, last transaction, summary, ...) are never stored.
    """

    def __init__(self, path: Path = CACHE_PATH, max_entries: int = 500) -> None:
        self.path = path
        self.max_entries = max_entries
        self._map: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for question, answer in data.get("answers", {}).items():
                key = normalize(str(question))
                if key and str(answer).strip():
                    self._map[key] = str(answer).strip()
        except Exception as exc:
            log.error(f"[ANSWER_CACHE] Failed to load {self.path}: {exc!r}")
            return
        log.info(f"[ANSWER_CACHE] Loaded {len(self._map)} remembered answers")

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps({"answers": dict(self._map)}, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                tmp.replace(self.path)
                self._dirty = False
            except Exception as exc:
                log.error(f"[ANSWER_CACHE] Failed to save {self.path}: {exc!r}")

    @property
    def loaded(self) -> int:
        return len(self._map)

    def match(self, text: str) -> Optional[str]:
        if not text:
            return None
        return self._map.get(normalize(text))

    def store(self, question: str, answer: str) -> bool:
        """Remember a Q->A pair. Returns False for real-time questions."""
        key = normalize(question or "")
        if not key or not (answer or "").strip():
            return False
        if is_dynamic(question):
            log.debug(f"[ANSWER_CACHE] Skip real-time question: {question!r}")
            return False
        with self._lock:
            if self._map.get(key) == answer.strip():
                return True
            self._map[key] = answer.strip()
            while len(self._map) > self.max_entries:
                self._map.pop(next(iter(self._map)))
            self._dirty = True
        self.save()
        log.info(f"[ANSWER_CACHE] Remembered ({len(self._map)}): {question!r}")
        return True


answer_cache = AnswerCache(max_entries=settings.answer_cache_max)
