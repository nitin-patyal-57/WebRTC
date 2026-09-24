import asyncio

from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection


async def try_turn(urls, username=None, credential=None, label="") -> bool:
    servers = []
    if username:
        servers.append(RTCIceServer(urls=urls, username=username, credential=credential))
    else:
        servers.append(RTCIceServer(urls=urls))
    servers.append(RTCIceServer(urls=["stun:stun.l.google.com:19302"]))
    pc = RTCPeerConnection(RTCConfiguration(iceServers=servers))
    pc.createDataChannel("t")
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    ok = False
    relay = ""
    for _ in range(24):
        await asyncio.sleep(0.25)
        sdp = pc.localDescription.sdp
        for line in sdp.splitlines():
            if "typ relay" in line:
                ok = True
                relay = line[:130]
                break
        if ok:
            break
    await pc.close()
    print(f"{'OK  ' if ok else 'FAIL'} {label}")
    if relay:
        print("     ", relay)
    return ok


async def main() -> None:
    await try_turn(["turn:freeturn.net:3478"], "free", "free", "freeturn udp3478")
    await try_turn(["turn:freeturn.net:3478?transport=tcp"], "free", "free", "freeturn tcp3478")
    await try_turn(["turns:freeturn.net:5349?transport=tcp"], "free", "free", "freeturns tcp5349")
    await try_turn(["turn:freeturn.tel:3478"], "free", "free", "freeturn.tel udp")

    import base64
    import hashlib
    import hmac
    import time

    secret = b"openrelayprojectsecret"
    username = f"{int(time.time()) + 600}:mobile"
    credential = base64.b64encode(
        hmac.new(secret, username.encode(), hashlib.sha1).digest()
    ).decode()
    await try_turn(
        ["turn:staticauth.openrelay.metered.ca:80"],
        username,
        credential,
        "openrelay staticauth udp80",
    )
    await try_turn(
        ["turn:staticauth.openrelay.metered.ca:443?transport=tcp"],
        username,
        credential,
        "openrelay staticauth tcp443",
    )


asyncio.run(main())
