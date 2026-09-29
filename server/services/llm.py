import time
from typing import AsyncIterator, Dict, List, Optional

from groq import AsyncGroq

from config import SYSTEM_PROMPT, settings
from services.knowledge import knowledge
from utils.logger import get_logger

log = get_logger("LLM")


def build_system_prompt() -> str:
    """System prompt plus curated Soundbox device facts (fallback answers)."""
    facts = knowledge.facts()
    if not facts:
        return SYSTEM_PROMPT
    return (
        f"{SYSTEM_PROMPT}\n"
        "Device facts:\n"
        f"{facts}\n"
        "When a question is about the Soundbox, its LED, SIM or transactions, "
        "answer using these facts with the same short wording."
    )


class GroqLLM:
    """Streaming LLM over Groq chat completions."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None) -> None:
        self.api_key = api_key or settings.groq_api_key
        self.model = model or settings.groq_model
        self._client: Optional[AsyncGroq] = None

    async def start(self) -> None:
        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY is not set")
        self._client = AsyncGroq(api_key=self.api_key)
        log.info(f"[LLM] Connected model={self.model}")

    async def stream(
        self, history: List[Dict[str, str]]
    ) -> AsyncIterator[str]:
        """Yield response text chunks. history: session conversation messages."""
        if self._client is None:
            raise RuntimeError("LLM not started")
        messages = [{"role": "system", "content": build_system_prompt()}, *history]
        params = dict(
            model=self.model,
            messages=messages,
            stream=True,
            temperature=0.4,
            max_tokens=300,
        )
        if "gpt-oss" in self.model:
            params["reasoning_effort"] = "low"
        stream = await self._client.chat.completions.create(**params)
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

    async def close(self) -> None:
        self._client = None
        log.info("[LLM] Closed")


def now_ms() -> float:
    return time.time() * 1000.0
