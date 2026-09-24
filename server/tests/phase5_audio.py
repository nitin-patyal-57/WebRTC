import asyncio
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import uvicorn
from aiortc import RTCPeerConnection, RTCSessionDescription
from av import AudioFrame

from main import app
from tests.phase1_client import ToneAudioTrack
from webrtc.session import session_manager

PORT = 8765
SERVER = f"http://127.0.0.1:{PORT}"
MIN_NONSILENT_FRAMES = 40  # ~0.8s of real audio


def make_tts_pcm(seconds: float = 2.0, freq: float = 440.0, amp: float = 0.5) -> bytes:
    n = int(48000 * seconds)
    return b"".join(
        int(amp * 32767 * math.sin(2 * math.pi * freq * i / 48000)).to_bytes(2, "little", signed=True)
        for i in range(n)
    )


class AudioCounter:
    def __init__(self) -> None:
        self.received = 0
        self.nonsilent = 0

    async def consume(self, track) -> None:
        import array as arrmod

        while True:
            frame: AudioFrame = await track.recv()
            self.received += 1
            peak = 0.0
            for plane in frame.planes:
                data = bytes(plane)
                if frame.format.name == "flt" or frame.format.name == "fltp":
                    floats = arrmod.array("f")
                    floats.frombytes(data)
                    if floats:
                        peak = max(peak, max(abs(v) for v in floats))
                elif frame.format.name == "s16":
                    shorts = arrmod.array("h")
                    shorts.frombytes(data[: len(data) - (len(data) % 2)])
                    if shorts:
                        peak = max(peak, max(abs(v) for v in shorts) / 32768.0)
            if peak > 0.01:
                self.nonsilent += 1


async def wait_health(client: httpx.AsyncClient, timeout: float = 12.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            res = await client.get(f"{SERVER}/health")
            if res.status_code == 200:
                return True
        except Exception:
            await asyncio.sleep(0.2)
    return False


async def main() -> int:
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    pc = None
    session_id = None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            if not await wait_health(client):
                print("[TEST] FAIL — server did not start")
                return 1

            pc = RTCPeerConnection()
            pc.addTrack(ToneAudioTrack(duration_sec=30))
            counter = AudioCounter()

            @pc.on("track")
            def on_track(track) -> None:
                print(f"[TEST] client received track: {track.kind}")
                asyncio.create_task(counter.consume(track))

            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            res = await client.post(
                f"{SERVER}/webrtc/offer",
                json={"device_id": "PHASE5_TEST", "sdp": pc.localDescription.sdp, "type": "offer"},
            )
            if res.status_code != 200:
                print(f"[TEST] FAIL — offer error {res.status_code}: {res.text}")
                return 1
            data = res.json()
            session_id = data["session_id"]
            await pc.setRemoteDescription(RTCSessionDescription(sdp=data["sdp"], type=data["type"]))

            deadline = time.time() + 10
            while pc.connectionState != "connected" and time.time() < deadline:
                await asyncio.sleep(0.1)
            if pc.connectionState != "connected":
                print(f"[TEST] FAIL — not connected: {pc.connectionState}")
                return 1
            print("[TEST] WebRTC connected")

            await asyncio.sleep(1.0)
            session = session_manager.get(session_id)
            if session is None:
                print("[TEST] FAIL — session not found in-process")
                return 1

            pcm = make_tts_pcm(2.0)
            print(f"[TEST] injecting {len(pcm)} bytes of TTS PCM into tts_audio_queue")
            await session.tts_audio_queue.put(pcm)

            deadline = time.time() + 10
            while time.time() < deadline and counter.nonsilent < MIN_NONSILENT_FRAMES:
                await asyncio.sleep(0.1)

            snapshot = (
                await client.get(f"{SERVER}/sessions/{session_id}")
            ).json()
            print(
                f"[TEST] client frames={counter.received} nonsilent={counter.nonsilent}; "
                f"server frames_sent={snapshot['audio_frames_sent']}"
            )
            ok = counter.nonsilent >= MIN_NONSILENT_FRAMES and snapshot["audio_frames_sent"] > 0
            print(f"[TEST] webrtc ai audio -> {'PASS' if ok else 'FAIL'}")
            return 0 if ok else 1
    finally:
        if session_id:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.delete(f"{SERVER}/sessions/{session_id}")
            except Exception:
                pass
        if pc is not None:
            try:
                await pc.close()
            except Exception:
                pass
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=5)
        except Exception:
            server_task.cancel()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
