import json
import re
from pathlib import Path
from typing import Dict, Optional

from utils.logger import get_logger

log = get_logger("FIXED")

FIXED_PATH = Path(__file__).resolve().parent.parent / "data" / "fixed_answers.json"

_PUNCT_RE = re.compile(r"[^\w\s']")


def normalize(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace -> stable lookup key."""
    text = _PUNCT_RE.sub(" ", text.lower())
    return " ".join(text.split())


class FixedAnswers:
    """O(1) exact lookup for questions whose answer never changes.

    Checked before the knowledge base and the LLM, so greetings and other
    static replies return instantly while real-time questions (weather,
    last transaction, summary, ...) still go through the full pipeline.
    """

    def __init__(self, path: Path = FIXED_PATH) -> None:
        self.path = path
        self._map: Dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.error(f"[FIXED] Failed to load {self.path}: {exc!r}")
            return
        for question, answer in data.get("answers", {}).items():
            if not isinstance(question, str) or not isinstance(answer, str):
                continue
            key = normalize(question)
            if key and answer.strip():
                self._map[key] = answer.strip()
        log.info(f"[FIXED] Loaded {len(self._map)} fixed answers from {self.path.name}")

    @property
    def loaded(self) -> bool:
        return bool(self._map)

    def match(self, text: str) -> Optional[str]:
        if not text or not self._map:
            return None
        return self._map.get(normalize(text))


fixed_answers = FixedAnswers()
