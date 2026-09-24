import asyncio
import json
import math
import sys
from fractions import Fraction

import httpx
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from av import AudioFrame

SERVER = "http://127.0.0.1:8000"
DURATION_SEC = 6
SAMPLE_RATE = 48000
SAMPLES_PER_FRAME = 480


class ToneAudioTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, duration_sec: float = DURATION_SEC) -> None:
        super().__init__()
        self._pts = 0
        self._max_pts = int(duration_sec * SAMPLE_RATE)

    async def recv(self) -> AudioFrame:
        if self._pts >= self._max_pts:
            self.stop()
            raise MediaStreamError

        frame = AudioFrame(format="s16", layout="mono", samples=SAMPLES_PER_FRAME)
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = Fraction(1, SAMPLE_RATE)

        samples = bytearray()
        for i in range(SAMPLES_PER_FRAME):
            t = (self._pts + i) / SAMPLE_RATE
            value = int(0.2 * 32767 * math.sin(2 * math.pi * 440 * t))
            samples += value.to_bytes(2, "little", signed=True)
        frame.planes[0].update(bytes(samples))

        self._pts += SAMPLES_PER_FRAME
        await asyncio.sleep(SAMPLES_PER_FRAME / SAMPLE_RATE)
        return frame


async def main() -> int:
    pc = RTCPeerConnection()
    pc.addTrack(ToneAudioTrack())

    done = asyncio.Event()

    @pc.on("connectionstatechange")
    async def on_state() -> None:
        print(f"[CLIENT] connectionState={pc.connectionState}")
        if pc.connectionState in ("failed", "closed"):
            done.set()
        elif pc.connectionState == "connected":
            pass

    @pc.on("track")
    def on_track(track) -> None:
        print(f"[CLIENT] server track: {track.kind}")

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)

    async with httpx.AsyncClient(timeout=15) as client:
        health = await client.get(f"{SERVER}/health")
        print(f"[CLIENT] health={health.json()}")
        if health.status_code != 200:
            return 1

        res = await client.post(
            f"{SERVER}/webrtc/offer",
            json={"device_id": "PHASE1_TEST", "sdp": pc.localDescription.sdp, "type": "offer"},
        )
        if res.status_code != 200:
            print(f"[CLIENT] offer failed: {res.status_code} {res.text}")
            return 1
        data = res.json()
        session_id = data["session_id"]
        print(f"[CLIENT] session_id={session_id}")

        await pc.setRemoteDescription(RTCSessionDescription(sdp=data["sdp"], type=data["type"]))

        await asyncio.sleep(DURATION_SEC + 1.5)

        status = await client.get(f"{SERVER}/sessions/{session_id}")
        snapshot = status.json()
        print(f"[CLIENT] session snapshot={json.dumps(snapshot, indent=2)}")

        await client.delete(f"{SERVER}/sessions/{session_id}")

    await pc.close()

    frames = snapshot.get("audio_frames_received", 0)
    if frames > 0:
        print(f"[CLIENT] PASS — server received {frames} audio frames")
        return 0
    print("[CLIENT] FAIL — server received 0 audio frames")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
