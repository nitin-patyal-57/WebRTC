import asyncio
import logging

logging.basicConfig(level=logging.DEBUG)
logging.getLogger("aioice").setLevel(logging.DEBUG)

from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection


async def main() -> None:
    pc = RTCPeerConnection(
        RTCConfiguration(
            iceServers=[
                RTCIceServer(
                    urls=["turn:freeturn.net:3478"],
                    username="free",
                    credential="free",
                ),
            ]
        )
    )
    pc.createDataChannel("t")
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    for i in range(16):
        await asyncio.sleep(0.25)
        sdp = pc.localDescription.sdp
        if "typ relay" in sdp:
            print("GOT RELAY")
            break
    else:
        print("NO RELAY")
        for line in pc.localDescription.sdp.splitlines():
            if line.startswith("a=candidate"):
                print(line)
    await pc.close()


asyncio.run(main())
