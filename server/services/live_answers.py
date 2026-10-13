import re
import time
from typing import Dict, Optional, Tuple

import httpx

from config import settings
from services.transactions import spoken_rupees, transactions
from utils.logger import get_logger

log = get_logger("LIVE")

HTTP_TIMEOUT = 6.0
WEATHER_TTL = 600.0
GOLD_TTL = 600.0
FX_TTL = 6 * 3600.0
TROY_OUNCE_GRAMS = 31.1035

WEATHER_WORDS = {
    "weather", "temperature", "forecast", "rain", "raining", "rainy", "humidity",
    "humid", "umbrella", "sunny", "cloudy", "climate", "degrees",
}
GOLD_WORDS = {"gold"}
TXN_WORDS = {
    "transaction", "transactions", "txn", "txns", "payment", "payments", "sale",
    "sales", "collection", "collections", "earning", "earnings", "earn", "earned",
    "collect", "collected", "received", "money", "income",
}
TXN_QUERY_WORDS = {
    "last", "latest", "recent", "previous", "newest", "summary", "summarise",
    "summarize", "total", "many", "much", "list", "show", "details", "detail",
    "history", "today", "today's", "yesterday", "yesterday's", "week", "all",
}
# Device-problem wording belongs to the Soundbox knowledge base, not here.
TXN_EXCLUDE = {
    "not", "no", "didn't", "didnt", "never", "isn't", "isnt", "wasn't", "wasnt",
    "announce", "announced", "announcing", "announcement", "voice", "voiced",
    "speak", "speaking", "light", "led", "sim", "network", "silent", "quiet",
    "sound", "why", "problem", "issue", "wrong", "notification", "notifications",
    "none", "reach", "reaching", "coming", "stopped", "missing",
}
SUMMARY_WORDS = {"summary", "summarise", "summarize"}

CITY_RE = re.compile(r"\b(?:in|at|for|of)\s+([a-z][a-z .'-]*)")
CITY_NOISE = re.compile(
    r"\b(today|tomorrow|tonight|now|right now|currently|please|weather|"
    r"temperature|forecast|the day|this week|outside|like)\b"
)

WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle", 61: "light rain", 63: "rain",
    65: "heavy rain", 66: "freezing rain", 67: "freezing rain", 71: "light snow",
    73: "snow", 75: "heavy snow", 77: "snow", 80: "light showers", 81: "showers",
    82: "heavy showers", 85: "snow showers", 86: "snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}


def detect_intent(text: str) -> Optional[str]:
    """'weather' | 'gold_price' | 'transactions' | None (not a live question)."""
    words = set(re.findall(r"[a-z0-9']+", text.lower()))
    if words & GOLD_WORDS:
        return "gold_price"
    if words & WEATHER_WORDS:
        return "weather"
    if words & TXN_EXCLUDE:
        return None
    if words & SUMMARY_WORDS or (words & TXN_WORDS and words & TXN_QUERY_WORDS):
        return "transactions"
    return None


class LiveAnswers:
    """Real-time answers: weather (Open-Meteo), gold price, and payments.

    Checked before the answer cache, knowledge base and LLM so these
    questions get current data instead of a remembered or invented reply.
    """

    def __init__(self) -> None:
        self._cache: Dict[str, Tuple[float, object]] = {}

    def _cached(self, key: str, ttl: float):
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        return None

    def _put(self, key: str, value) -> None:
        self._cache[key] = (time.monotonic(), value)

    async def _get_json(self, url: str, params: Optional[dict] = None) -> dict:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()

    async def answer(self, text: str) -> Optional[Tuple[str, str]]:
        """Return (intent, spoken answer), or None if this is not a live question."""
        intent = detect_intent(text)
        if intent is None:
            return None
        try:
            if intent == "weather":
                return intent, await self.weather(text)
            if intent == "gold_price":
                return intent, await self.gold_price()
            return intent, transactions.answer(text)
        except Exception as exc:
            log.error(f"[LIVE] {intent} failed: {exc!r}")
            what = {"weather": "the weather", "gold_price": "the gold price"}.get(intent, "your transactions")
            return intent, f"Sorry, I couldn't get {what} right now. Please try again in a moment."

    # --- weather ---------------------------------------------------------

    @staticmethod
    def _city(text: str) -> Optional[str]:
        m = CITY_RE.search(text.lower())
        if not m:
            return None
        city = CITY_NOISE.sub(" ", m.group(1))
        city = re.sub(r"[^a-z .'-]", " ", city)
        city = re.sub(r"^\s*the\s+", "", " ".join(city.split()))
        return city or None

    async def _geocode(self, city: str) -> Optional[dict]:
        key = f"geo:{city}"
        hit = self._cached(key, 24 * 3600.0)
        if hit is not None:
            return hit
        url = "https://geocoding-api.open-meteo.com/v1/search"
        params = {"name": city, "count": 1}
        results = []
        if settings.weather_country:
            data = await self._get_json(url, {**params, "countryCode": settings.weather_country})
            results = data.get("results") or []
        if not results:
            data = await self._get_json(url, params)
            results = data.get("results") or []
        place = results[0] if results else None
        if place is not None:
            self._put(key, place)
        return place

    async def weather(self, text: str) -> str:
        asked = self._city(text)
        city = asked or settings.weather_city
        place = await self._geocode(city)
        if place is None:
            return f"Sorry, I couldn't find a place called {city}."
        name = place.get("name", city.title())
        key = f"wx:{place['latitude']:.2f},{place['longitude']:.2f}"
        data = self._cached(key, WEATHER_TTL)
        if data is None:
            data = await self._get_json(
                "https://api.open-meteo.com/v1/forecast",
                {
                    "latitude": place["latitude"],
                    "longitude": place["longitude"],
                    "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
                    "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
                    "timezone": "auto",
                    "forecast_days": 2,
                },
            )
            self._put(key, data)

        daily = data["daily"]
        if "tomorrow" in text.lower():
            sky = WMO.get(daily["weather_code"][1], "mixed conditions")
            return (
                f"Tomorrow in {name}, expect {sky}, with a high of {round(daily['temperature_2m_max'][1])} "
                f"and a low of {round(daily['temperature_2m_min'][1])} degrees, and a "
                f"{daily['precipitation_probability_max'][1] or 0} percent chance of rain."
            )
        cur = data["current"]
        sky = WMO.get(cur["weather_code"], "mixed conditions")
        return (
            f"In {name} it's {round(cur['temperature_2m'])} degrees with {sky} right now, "
            f"humidity {cur['relative_humidity_2m']} percent. Today's high is "
            f"{round(daily['temperature_2m_max'][0])} and low {round(daily['temperature_2m_min'][0])}, "
            f"with a {daily['precipitation_probability_max'][0] or 0} percent chance of rain."
        )

    # --- gold ------------------------------------------------------------

    async def _usd_inr(self) -> float:
        rate = self._cached("fx:usdinr", FX_TTL)
        if rate is None:
            data = await self._get_json("https://open.er-api.com/v6/latest/USD")
            rate = float(data["rates"]["INR"])
            self._put("fx:usdinr", rate)
        return rate

    async def gold_price(self) -> str:
        usd_oz = self._cached("gold:usd", GOLD_TTL)
        if usd_oz is None:
            data = await self._get_json("https://api.gold-api.com/price/XAU")
            usd_oz = float(data["price"])
            self._put("gold:usd", usd_oz)
        inr_per_gram = usd_oz * await self._usd_inr() / TROY_OUNCE_GRAMS
        k24 = round(inr_per_gram * 10, -1)
        k22 = round(inr_per_gram * 10 * 22 / 24, -1)
        return (
            f"24 karat gold is about {spoken_rupees(k24)} per 10 grams, and 22 karat about "
            f"{spoken_rupees(k22)}. This is the international rate; local shop prices "
            f"include duty and GST, so they may be higher."
        )


live_answers = LiveAnswers()
