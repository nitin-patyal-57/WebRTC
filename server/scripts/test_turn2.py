import asyncio
import sys

from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection


async def try_turn(urls, username=None, credential=None, label="") -> bool:
    servers = []
    if username and credential:
        servers.append(RTCIceServer(urls=urls, username=username, credential=credential))
    servers.append(RTCIceServer(urls=["stun:stun.l.google.com:19302"]))
    pc = RTCPeerConnection(RTCConfiguration(iceServers=servers))
    pc.createDataChannel("t")
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    ok = False
    for _ in range(24):
        await asyncio.sleep(0.25)
        sdp = pc.localDescription.sdp
        if "typ relay" in sdp:
            ok = True
            break
    await pc.close()
    print(f"{'OK' if ok else 'FAIL'} {label or urls}")
    return ok


async def main() -> None:
    user, cred = "openrelayproject", "openrelayproject"
    await try_turn(["turn:openrelay.metered.ca:80"], user, cred, "openrelay udp80")
    await try_turn(["turn:openrelay.metered.ca:443?transport=tcp"], user, cred, "openrelay tcp443")
    await try_turn(["turn:openrelay.metered.ca:3478?transport=tcp"], user, cred, "openrelay tcp3478")
    await try_turn(["turns:openrelay.metered.ca:5349?transport=tcp"], user, cred, "openrelay tls5349")
    # no creds variants
    await try_turn(["turn:openrelay.metered.ca:443?transport=tcp"], "", "", "openrelay tcp empty")


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
