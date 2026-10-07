"""Compatibility guards for aioice 0.10.2 lifecycle races."""

import os
from typing import Any, Callable

from aioice.stun import Transaction

_installed = False
_consent_installed = False
_original_retry: Callable[..., None] | None = None


def install_aioice_retry_guard() -> None:
    """Prevent STUN retry timers from completing an already-finished future.

    aioice 0.10.2 can invoke ``Transaction.__retry`` after a response has
    completed its future.  The callback then attempts to set a timeout
    exception on an already-completed future, raising InvalidStateError.
    """
    global _installed, _original_retry

    if _installed:
        return

    _original_retry = Transaction._Transaction__retry

    def guarded_retry(transaction: Any) -> None:
        future = transaction._Transaction__future
        if future.done():
            return
        _original_retry(transaction)

    Transaction._Transaction__retry = guarded_retry
    _installed = True


def install_aioice_consent_grace() -> None:
    """Extend or disable aioice's RFC 7675 consent force-close.

    The embedded client stops answering inbound STUN the moment SRTP downlink
    starts, while it keeps sending its own keepalives (which we always answer).
    Stock aioice therefore closes every cellular call after 6 x ~5 s with
    "Consent to send expired" (~30 s) - the observed drop.

    Controlled by ICE_CONSENT_FAILURES in .env:
      unset -> 60  (force-close after ~5 min of unanswered checks)
      0     -> never force-close
      6     -> stock aioice behaviour
    """
    global _consent_installed
    if _consent_installed:
        return

    import aioice.ice as ice

    raw = os.environ.get("ICE_CONSENT_FAILURES", "").strip()
    try:
        failures = int(raw) if raw else 60
    except ValueError:
        failures = 60

    if failures <= 0:
        ice.CONSENT_FAILURES = float("inf")
        threshold = "never"
    else:
        ice.CONSENT_FAILURES = failures
        threshold = f"{failures * ice.CONSENT_INTERVAL:.0f}s"
    _consent_installed = True

    from utils.logger import get_logger

    get_logger("AIOICE").info(
        f"[AIOICE] Consent force-close threshold: {threshold} "
        "(ICE_CONSENT_FAILURES: 0=never, 6=stock aioice)"
    )
