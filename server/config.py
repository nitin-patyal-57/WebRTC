import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv

# Resolve the environment file relative to this module instead of the
# process working directory. This keeps TURN/STUN settings available when
# the server is started from the repository root.
load_dotenv(Path(__file__).with_name(".env"))


def _split_csv(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass
class Settings:
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
    # End the utterance when no inbound audio arrives for this long while echoing
    # (the Soundbox stops sending RTP after its silence tail).
    vad_no_audio_ms: int = field(default_factory=lambda: int(os.getenv("VAD_NO_AUDIO_MS", "400")))
    # Echo jitter buffer: audio held before playback starts (and after an
    # underrun) so network jitter does not chop the echoed audio.
    echo_prebuffer_ms: int = field(default_factory=lambda: int(os.getenv("ECHO_PREBUFFER_MS", "200")))

    # SNTP server used to check the host clock at startup (blank = skip).
    ntp_check_server: str = field(default_factory=lambda: os.getenv("NTP_CHECK_SERVER", "pool.ntp.org"))


settings = Settings()
