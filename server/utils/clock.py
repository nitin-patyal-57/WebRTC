"""Startup check that the host clock agrees with NTP.

Latency logs are compared against device logs, so a skewed host clock
(once ~11 s off) makes them misleading. This only measures and warns; the
host must run an NTP client (Windows Time service, chrony, systemd-timesyncd).
"""

import asyncio
import socket
import struct
import time
from typing import Optional

from utils.logger import get_logger

log = get_logger("APP")

NTP_EPOCH_OFFSET = 2208988800  # seconds between 1900-01-01 and 1970-01-01
WARN_OFFSET_MS = 200.0


def _ntp_to_unix(data: bytes, offset: int) -> float:
    seconds, fraction = struct.unpack("!II", data[offset : offset + 8])
    return seconds - NTP_EPOCH_OFFSET + fraction / 2**32


def query_clock_offset_ms(server: str, timeout: float = 2.0) -> float:
    """SNTP query; returns (server clock - host clock) in ms."""
    packet = b"\x23" + 47 * b"\0"  # LI=0, VN=4, mode=3 (client)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        t1 = time.time()
        sock.sendto(packet, (server, 123))
        data, _ = sock.recvfrom(48)
        t4 = time.time()
    t2 = _ntp_to_unix(data, 32)  # server receive
    t3 = _ntp_to_unix(data, 40)  # server transmit
    return ((t2 - t1) + (t3 - t4)) / 2 * 1000.0


async def check_clock(server: str) -> Optional[float]:
    if not server:
        return None
    try:
        offset = await asyncio.to_thread(query_clock_offset_ms, server)
    except Exception as exc:
        log.warning(f"[APP] Clock check against {server} failed: {exc!r}")
        return None
    if abs(offset) > WARN_OFFSET_MS:
        log.warning(
            f"[APP] Host clock is {offset:+.0f}ms off NTP ({server}); latency "
            "timestamps will not line up with device logs. Enable NTP sync on the host."
        )
    else:
        log.info(f"[APP] Host clock within {offset:+.0f}ms of NTP ({server})")
    return offset
