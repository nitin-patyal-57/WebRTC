import logging
import os
import sys

_CONFIGURED = False
_FILE_CONFIGURED = False

# Loggers traced when ICE_DEBUG is enabled: aioice logs every STUN
# request/response in/out with address and method; aiortc logs ICE/DTLS
# transport state events. Together they show whether the peer's keepalives
# arrive, what the server replies (200 vs 400) and whether consent
# freshness expires right before a drop.
_ICE_DEBUG_LOGGERS = ("aioice", "aiortc")

# Per-packet/per-message loggers stay at INFO: one line per RTP/SCTP packet
# buries the STUN traces.
_ICE_DEBUG_QUIET = (
    "aiortc.rtcrtpsender",
    "aiortc.rtcrtpreceiver",
    "aiortc.rtcsctptransport",
    "aiortc.rtcdatachannel",
)


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").lower() in ("1", "true", "yes", "on")


def _formatter() -> logging.Formatter:
    return logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )


def _apply_ice_debug() -> None:
    # Runs on every configure call: .env (dotenv) may be loaded AFTER the
    # first get_logger() call, so the flag must be re-checked each time.
    if _env_flag("ICE_DEBUG"):
        for name in _ICE_DEBUG_LOGGERS:
            logging.getLogger(name).setLevel(logging.DEBUG)
        for name in _ICE_DEBUG_QUIET:
            logging.getLogger(name).setLevel(logging.INFO)


def _apply_log_file() -> None:
    global _FILE_CONFIGURED
    if _FILE_CONFIGURED:
        return
    log_file = os.getenv("LOG_FILE", "").strip()
    if not log_file:
        return
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(_formatter())
    logging.getLogger().addHandler(handler)
    _FILE_CONFIGURED = True


def configure_logging() -> None:
    global _CONFIGURED
    if not _CONFIGURED:
        level = os.getenv("LOG_LEVEL", "INFO").upper()
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(_formatter())
        root = logging.getLogger()
        root.setLevel(level)
        root.addHandler(handler)
        _CONFIGURED = True
    _apply_ice_debug()
    _apply_log_file()


def ice_debug_enabled() -> bool:
    configure_logging()
    return _env_flag("ICE_DEBUG")


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)
