"""Verify the project's TURN server allocates a relay candidate.

Usage (from server/):
    .venv/Scripts/python.exe scripts/check_turn.py            # settings from .env
    .venv/Scripts/python.exe scripts/check_turn.py --tcp      # force TCP transport
    .venv/Scripts/python.exe scripts/check_turn.py --url turn:x:3478 --user u --cred p

Exit code 0 = relay candidate gathered (server reachable + credentials OK).
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection


def load_dotenv() -> None:
    path = Path(__file__).resolve().parents[1] / ".env"
    try:
        from dotenv import load_dotenv

        load_dotenv(path)
    except ImportError:
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())


async def check(url: str, username: str, credential: str, stun: list[str], timeout: float) -> bool:
    servers = [RTCIceServer(urls=url, username=username, credential=credential)]
    if stun:
        servers.append(RTCIceServer(urls=stun))
    pc = RTCPeerConnection(RTCConfiguration(iceServers=servers))
    pc.createDataChannel("check")
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)

    relay = ""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.25)
        for line in (pc.localDescription.sdp or "").splitlines():
            if "typ relay" in line:
                relay = line.strip()
                break
        if relay:
            break
    await pc.close()

    print(f"{'OK  ' if relay else 'FAIL'} {url}")
    if relay:
        print("     ", relay[:130])
    return bool(relay)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="TURN URL (default: TURN_URL from .env)")
    parser.add_argument("--user", help="TURN username (default: TURN_USERNAME)")
    parser.add_argument("--cred", help="TURN credential (default: TURN_CREDENTIAL)")
    parser.add_argument("--stun", help="extra STUN URL, comma separated")
    parser.add_argument("--tcp", action="store_true", help="force ?transport=tcp")
    parser.add_argument("--timeout", type=float, default=6.0)
    args = parser.parse_args()

    load_dotenv()

    url = args.url or os.environ.get("TURN_URL", "")
    username = args.user or os.environ.get("TURN_USERNAME", "")
    credential = args.cred or os.environ.get("TURN_CREDENTIAL", "")
    if args.tcp and url and "transport=" not in url:
        url += "?transport=tcp"
    if args.stun is not None:
        stun = [u for u in args.stun.split(",") if u]
    else:
        stun = [u for u in os.environ.get("STUN_URLS", "").split(",") if u]

    if not url:
        print("FAIL no TURN URL (set TURN_URL in .env or pass --url)")
        return 1

    ok = await check(url, username, credential, stun, args.timeout)
    if not ok and not args.tcp and not args.url:
        # .env URL failed over UDP: try TCP before declaring failure.
        ok = await check(url + "?transport=tcp", username, credential, stun, args.timeout)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
