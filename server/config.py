import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


def _split_csv(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass
class Settings:
    groq_api_key: str = field(default_factory=lambda: os.getenv("GROQ_API_KEY", ""))
    groq_model: str = field(default_factory=lambda: os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"))
    groq_stt_model: str = field(default_factory=lambda: os.getenv("GROQ_STT_MODEL", "whisper-large-v3-turbo"))
    groq_tts_model: str = field(default_factory=lambda: os.getenv("GROQ_TTS_MODEL", "canopylabs/orpheus-v1-english"))
    groq_tts_voice: str = field(default_factory=lambda: os.getenv("GROQ_TTS_VOICE", "troy"))
    tts_provider: str = field(default_factory=lambda: os.getenv("TTS_PROVIDER", "groq"))
    stt_language: str = field(default_factory=lambda: os.getenv("STT_LANGUAGE", "en"))

    stun_urls: List[str] = field(
        default_factory=lambda: _split_csv(os.getenv("STUN_URLS", "stun:stun.l.google.com:19302"))
    )
    turn_url: str = field(default_factory=lambda: os.getenv("TURN_URL", ""))
    turn_username: str = field(default_factory=lambda: os.getenv("TURN_USERNAME", ""))
    turn_credential: str = field(default_factory=lambda: os.getenv("TURN_CREDENTIAL", ""))

    @property
    def turn_urls(self) -> List[str]:
        return _split_csv(self.turn_url)

    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    max_history_messages: int = field(default_factory=lambda: int(os.getenv("MAX_HISTORY_MESSAGES", "10")))
    vad_silence_ms: int = field(default_factory=lambda: int(os.getenv("VAD_SILENCE_MS", "700")))
    vad_min_speech_ms: int = field(default_factory=lambda: int(os.getenv("VAD_MIN_SPEECH_MS", "300")))


settings = Settings()

SYSTEM_PROMPT = (
    "You are a voice assistant inside a smart payment soundbox.\n"
    "Keep responses short and natural.\n"
    "Prefer responses under 15 words when possible.\n"
    "Do not use markdown.\n"
    "Do not use bullet points.\n"
    "Do not repeat the user's question.\n"
    "Do not provide unnecessary explanations.\n"
    "Speak naturally because the response will be converted to audio."
)
