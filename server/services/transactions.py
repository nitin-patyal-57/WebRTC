import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from config import settings
from utils.logger import get_logger

log = get_logger("TXN")

TXN_PATH = Path(__file__).resolve().parent.parent / "data" / "transactions.json"

MONTHS = {
    name: i + 1
    for i, names in enumerate(
        [
            ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"),
            ("may",), ("june", "jun"), ("july", "jul"), ("august", "aug"),
            ("september", "sep", "sept"), ("october", "oct"), ("november", "nov"),
            ("december", "dec"),
        ]
    )
    for name in names
}
_MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))
DAY_MONTH_RE = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_RE})\b")
MONTH_DAY_RE = re.compile(rf"\b({_MONTH_RE})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b")
COUNT_RE = re.compile(r"\b(?:last|latest|recent|previous|past)\s+(\d+|two|three|four|five)\b")
WORD_NUMBERS = {"two": 2, "three": 3, "four": 4, "five": 5}

PLURAL = {"transactions", "payments", "txns", "sales", "collections"}
LAST_WORDS = {"last", "latest", "recent", "previous", "newest"}
LIST_WORDS = {"list", "show", "details", "detail", "history", "all"}
MAX_LISTED = 5


def local_tz() -> timezone:
    return timezone(timedelta(minutes=settings.tz_offset_minutes))


def now_local() -> datetime:
    return datetime.now(local_tz()).replace(tzinfo=None)


def spoken_rupees(amount: float) -> str:
    """'1 lakh 18,700 rupees' - Indian grouping reads naturally over TTS."""
    whole = int(round(amount)) if abs(amount - round(amount)) < 0.005 else None
    if whole is None:
        return f"{amount:,.2f} rupees"
    if whole >= 10_000_000:
        crore, rest = divmod(whole, 10_000_000)
        lakh = rest // 100_000
        return f"{crore} crore {lakh} lakh rupees" if lakh else f"{crore} crore rupees"
    if whole >= 100_000:
        lakh, rest = divmod(whole, 100_000)
        return f"{lakh} lakh {rest:,} rupees" if rest else f"{lakh} lakh rupees"
    return f"{whole:,} rupees"


def spoken_time(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def spoken_day(day: date, today: date) -> str:
    if day == today:
        return "today"
    if day == today - timedelta(days=1):
        return "yesterday"
    return f"on {day.strftime('%A')}, {day.day} {day.strftime('%B')}"


@dataclass
class Transaction:
    id: str
    when: datetime
    amount: float
    payer: str
    mode: str
    status: str

    @property
    def ok(self) -> bool:
        return self.status.lower() == "success"

    def describe(self, today: date, with_day: bool = True) -> str:
        day = f"{spoken_day(self.when.date(), today)} " if with_day else ""
        text = f"{spoken_rupees(self.amount)} from {self.payer} via {self.mode}, {day}at {spoken_time(self.when)}"
        return text if self.ok else f"{text}, which failed"


@dataclass
class Period:
    start: date
    end: date  # inclusive
    label: str


class TransactionStore:
    """Payments received by the Soundbox, answered as short spoken replies."""

    def __init__(self, path: Path = TXN_PATH) -> None:
        self.path = path

    def load(self, now: Optional[datetime] = None) -> List[Transaction]:
        """Re-read on every query so edits to the file show up immediately."""
        now = now or now_local()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.error(f"[TXN] Failed to load {self.path}: {exc!r}")
            return []
        txns: List[Transaction] = []
        for raw in data.get("transactions", []):
            try:
                if raw.get("timestamp"):
                    when = datetime.fromisoformat(raw["timestamp"]).replace(tzinfo=None)
                else:
                    hh, mm = (int(x) for x in str(raw["time"]).split(":"))
                    day = now.date() - timedelta(days=int(raw.get("days_ago", 0)))
                    when = datetime.combine(day, datetime.min.time()).replace(hour=hh, minute=mm)
                txn = Transaction(
                    id=str(raw.get("id", "")),
                    when=when,
                    amount=float(raw["amount"]),
                    payer=str(raw.get("payer", "a customer")),
                    mode=str(raw.get("mode", "UPI")),
                    status=str(raw.get("status", "success")),
                )
            except Exception as exc:
                log.warning(f"[TXN] Skipping bad entry {raw!r}: {exc!r}")
                continue
            if txn.when <= now:
                txns.append(txn)
        txns.sort(key=lambda t: t.when)
        return txns

    # --- query parsing -------------------------------------------------

    @staticmethod
    def _period(text: str, today: date) -> Optional[Period]:
        if "yesterday" in text:
            day = today - timedelta(days=1)
            return Period(day, day, "yesterday")
        if re.search(r"\b(this week|last 7 days|past 7 days|last week|past week|weekly)\b", text):
            return Period(today - timedelta(days=6), today, "in the last 7 days")
        m = DAY_MONTH_RE.search(text)
        month_day = (MONTHS[m.group(2)], int(m.group(1))) if m else None
        if month_day is None:
            m = MONTH_DAY_RE.search(text)
            month_day = (MONTHS[m.group(1)], int(m.group(2))) if m else None
        if month_day is not None:
            try:
                day = date(today.year, *month_day)
            except ValueError:
                return None
            if day > today:
                day = day.replace(year=today.year - 1)
            return Period(day, day, spoken_day(day, today))
        if re.search(r"\btoday'?s?\b", text):
            return Period(today, today, "today")
        return None

    @staticmethod
    def _count(text: str) -> Optional[int]:
        m = COUNT_RE.search(text)
        if not m:
            return None
        value = m.group(1)
        n = WORD_NUMBERS.get(value) or int(value)
        return max(1, min(n, MAX_LISTED))

    # --- answers ---------------------------------------------------------

    def answer(self, text: str) -> str:
        now = now_local()
        today = now.date()
        t = text.lower()
        words = set(re.findall(r"[a-z0-9']+", t))
        txns = self.load(now)
        period = self._period(t, today)
        if period is not None:
            txns = [x for x in txns if period.start <= x.when.date() <= period.end]
        count = self._count(t)
        wants_last = bool(words & LAST_WORDS)
        plural = bool(words & PLURAL)

        if wants_last and count is None and not plural:
            return self._last(txns, period, today)
        if count is not None or (wants_last and plural) or (words & LIST_WORDS and period is None):
            return self._list(txns, count or 3, period, today)
        if period is None:
            period = Period(today, today, "today")
            txns = [x for x in txns if x.when.date() == today]
        return self._summary(txns, period, today, detail=bool(words & LIST_WORDS))

    def _last(self, txns: List[Transaction], period: Optional[Period], today: date) -> str:
        ok = [x for x in txns if x.ok]
        if not ok:
            return f"There were no payments {period.label}." if period else "There are no payments yet."
        return f"Your last payment was {ok[-1].describe(today)}."

    def _list(self, txns: List[Transaction], n: int, period: Optional[Period], today: date) -> str:
        recent = txns[-n:][::-1]
        if not recent:
            return f"There were no payments {period.label}." if period else "There are no payments yet."
        same_day = period is not None and period.start == period.end
        items = "; ".join(x.describe(today, with_day=not same_day) for x in recent)
        scope = f" {period.label}" if same_day else ""
        head = "Your last payment" if len(recent) == 1 else f"Your last {len(recent)} payments"
        return f"{head}{scope}: {items}."

    def _summary(self, txns: List[Transaction], period: Period, today: date, detail: bool = False) -> str:
        ok = [x for x in txns if x.ok]
        failed = len(txns) - len(ok)
        if not ok:
            text = f"You have not received any payments {period.label}."
            if failed:
                text += f" {failed} payment{'s' if failed > 1 else ''} failed."
            return text
        total = sum(x.amount for x in ok)
        noun = "payment" if len(ok) == 1 else "payments"
        parts = [f"{period.label.capitalize()} you received {len(ok)} {noun}, totalling {spoken_rupees(total)}."]
        if len(ok) > 1:
            big = max(ok, key=lambda x: x.amount)
            parts.append(f"The largest was {spoken_rupees(big.amount)} from {big.payer}.")
            if period.start == period.end:
                parts.append(f"The first came at {spoken_time(ok[0].when)} and the last at {spoken_time(ok[-1].when)}.")
        if failed:
            parts.append(f"{failed} payment{'s' if failed > 1 else ''} failed.")
        if detail:
            same_day = period.start == period.end
            recent = ok[-MAX_LISTED:][::-1]
            parts.append("Latest: " + "; ".join(x.describe(today, with_day=not same_day) for x in recent) + ".")
        return " ".join(parts)

    def prompt_context(self, limit: int = 10) -> str:
        """Recent payments for the LLM system prompt (free-form follow-ups)."""
        now = now_local()
        txns = self.load(now)[-limit:]
        lines = [f"Current local date and time: {now.strftime('%A %d %B %Y, %I:%M %p')}."]
        if txns:
            lines.append("Recent payments received by this Soundbox (oldest first, amounts in INR):")
            for x in txns:
                lines.append(
                    f"- {x.when.strftime('%d %b %Y %I:%M %p')}: {x.amount:g} rupees from {x.payer} "
                    f"via {x.mode} ({x.status}, id {x.id})"
                )
        return "\n".join(lines)


transactions = TransactionStore()
