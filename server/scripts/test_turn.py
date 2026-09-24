import asyncio

from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection


async def main() -> None:
    pc = RTCPeerConnection(
        RTCConfiguration(
            iceServers=[
                RTCIceServer(
                    urls=["turn:openrelay.metered.ca:80"],
                    username="openrelayproject",
                    credential="openrelayproject",
                ),
                RTCIceServer(urls=["stun:stun.l.google.com:19302"]),
            ]
        )
    )
    pc.createDataChannel("t")
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    for i in range(40):
        await asyncio.sleep(0.25)
        sdp = pc.localDescription.sdp
        relay = [line for line in sdp.splitlines() if "typ relay" in line]
        if relay:
            print("RELAY OK:", relay[0][:140])
            break
        if i == 39:
            print("NO RELAY after 10s")
            print("gathering:", pc.iceGatheringState)
            for line in sdp.splitlines():
                if line.startswith("a=candidate"):
                    print(line[:140])
    await pc.close()

asyncio.run(main())
