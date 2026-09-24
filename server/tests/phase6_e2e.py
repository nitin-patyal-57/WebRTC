import asyncio
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import uvicorn
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from av import AudioFrame
from fractions import Fraction

from main import app
from services.tts import SapiTTS
from webrtc.session import session_manager

PORT = 8766
SERVER = f"http://127.0.0.1:{PORT}"

SR = 48000
FRAME_SAMPLES = 960
FRAME_BYTES = FRAME_SAMPLES * 2
PTIME = FRAME_SAMPLES / SR

Q1 = "What is my payment status?"
Q2 = "Hello, can you hear me now?"

SILENCE = b"\x00"


def silence_pcm(seconds: float) -> bytes:
    return SILENCE * int(SR * seconds * 2)


def tone_pcm(seconds: float, freq: float = 440.0) -> bytes:
    n = int(SR * seconds)
    return b"".join(
        int(0.4 * 32767 * math.sin(2 * math.pi * freq * i / SR)).to_bytes(2, "little", signed=True)
        for i in range(n)
    )


class ScriptedTrack(MediaStreamTrack):
    """Client mic track: silence initially, then queued PCM (speech) with 20ms pacing."""

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._pending = bytearray(silence_pcm(1.5))
        self._pts = 0

    def enqueue(self, pcm: bytes) -> None:
        self._pending += pcm

    async def recv(self) -> AudioFrame:
        if self.readyState == "ended":
            raise MediaStreamError
        start = time.monotonic()
        while time.monotonic() - start < PTIME:
            await asyncio.sleep(min(PTIME - (time.monotonic() - start), 0.005))
        if self.readyState == "ended":
            raise MediaStreamError

        if len(self._pending) >= FRAME_BYTES:
            data = bytes(self._pending[:FRAME_BYTES])
            del self._pending[:FRAME_BYTES]
        else:
            data = b"\x00" * FRAME_BYTES

        frame = AudioFrame(format="s16", layout="mono", samples=FRAME_SAMPLES)
        frame.sample_rate = SR
        frame.pts = self._pts
        frame.time_base = Fraction(1, SR)
        frame.planes[0].update(data)
        self._pts += FRAME_SAMPLES
        return frame


class Receiver:
    def __init__(self) -> None:
        self.nonsilent = 0
        self.received = 0
        self.events: list[dict] = []

    def on_event(self, raw: str) -> None:
        try:
            self.events.append(json.loads(raw))
        except json.JSONDecodeError:
            pass

    def types(self) -> list[str]:
        return [e.get("type", "") for e in self.events]

    def find(self, etype: str) -> list[dict]:
        return [e for e in self.events if e.get("type") == etype]

    async def consume_audio(self, track) -> None:
        import array as arrmod

        try:
            while True:
                frame = await track.recv()
                self.received += 1
                peak = 0.0
                for plane in frame.planes:
                    data = bytes(plane)
                    if frame.format.name.startswith("flt"):
                        floats = arrmod.array("f")
                        floats.frombytes(data)
                        if floats:
                            peak = max(peak, max(abs(v) for v in floats))
                    elif frame.format.name == "s16":
                        shorts = arrmod.array("h")
                        usable = data[: len(data) - (len(data) % 2)]
                        shorts.frombytes(usable)
                        if shorts:
                            peak = max(peak, max(abs(v) for v in shorts) / 32768.0)
                if peak > 0.01:
                    self.nonsilent += 1
        except MediaStreamError:
            pass


async def wait_for(cond, timeout: float, interval: float = 0.15) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        await asyncio.sleep(interval)
    return cond()


async def main() -> int:
    tts = SapiTTS()
    await tts.start()
    speech1 = await tts.synthesize(Q1)
    speech2 = await tts.synthesize(Q2)
    await tts.close()
    print(f"[TEST] speech1={len(speech1)}B speech2={len(speech2)}B")

    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    pc = None
    session_id = None
    failures: list[str] = []

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            ok = False
            for _ in range(60):
                try:
                    if (await client.get(f"{SERVER}/health")).status_code == 200:
                        ok = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.2)
            if not ok:
                print("[TEST] FAIL — server start")
                return 1

            pc = RTCPeerConnection()
            mic = ScriptedTrack()
            pc.addTrack(mic)
            rx = Receiver()

            @pc.on("track")
            def on_track(track) -> None:
                print(f"[TEST] AI track received: {track.kind}")
                asyncio.create_task(rx.consume_audio(track))

            channel = pc.createDataChannel("events")
            channel.on("message", rx.on_event)
            channel.on("open", lambda: print("[TEST] datachannel open"))

            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            res = await client.post(
                f"{SERVER}/webrtc/offer",
                json={"device_id": "WM_SB_1605_001", "sdp": pc.localDescription.sdp, "type": "offer"},
            )
            if res.status_code != 200:
                print(f"[TEST] FAIL — offer {res.status_code}: {res.text}")
                return 1
            data = res.json()
            session_id = data["session_id"]
            await pc.setRemoteDescription(RTCSessionDescription(sdp=data["sdp"], type=data["type"]))

            if not await wait_for(lambda: pc.connectionState == "connected", 10):
                print(f"[TEST] FAIL — connect: {pc.connectionState}")
                return 1
            print("[TEST] connected")
            await asyncio.sleep(0.5)

            # --- Turn 1: speak, expect transcript + answer + heard audio ---
            print(f"[TEST] speaking: {Q1!r}")
            mic.enqueue(speech1 + silence_pcm(2.0))

            got_final = await wait_for(
                lambda: any(
                    "payment" in (e.get("text") or "").lower() for e in rx.find("final_transcript")
                ),
                20,
            )
            if not got_final:
                failures.append("no final transcript with 'payment'")

            got_answer = await wait_for(
                lambda: bool(rx.find("assistant_message")), 20
            )
            if not got_answer:
                failures.append("no assistant_message")

            got_audio = await wait_for(lambda: rx.nonsilent >= 25, 15)
            if not got_audio:
                failures.append(f"AI audio not heard (nonsilent={rx.nonsilent})")

            latency = rx.find("latency")
            metrics = latency[-1].get("metrics", {}) if latency else {}
            print(f"[TEST] turn1 events: {sorted(set(rx.types()))}")
            print(f"[TEST] latency metrics: {metrics}")
            for key in ("stt", "llm_first_token", "tts_first_audio"):
                if key not in metrics:
                    failures.append(f"latency missing {key}")

            # --- Barge-in: AI still 'speaking' (8s audio queued), user talks again ---
            session = session_manager.get(session_id)
            if session is None:
                failures.append("session lost")
            else:
                await session.tts_audio_queue.put(tone_pcm(8.0))
                await asyncio.sleep(0.4)
                print(f"[TEST] barge-in speaking while AI playing: {Q2!r}")
                mic.enqueue(speech2 + silence_pcm(2.5))

                got_interrupt = await wait_for(
                    lambda: bool(rx.find("interrupted")), 8
                )
                if not got_interrupt:
                    failures.append("no interrupted event on barge-in")
                else:
                    print("[TEST] interrupted event received")
                    await asyncio.sleep(1.0)
                    q_empty = session.tts_audio_queue.empty() and not session.ai_track.has_pending
                    if not q_empty:
                        failures.append("AI audio not cleared after interrupt")
                    else:
                        print("[TEST] pending AI audio cleared")

                got_final2 = await wait_for(
                    lambda: any(
                        "hear" in (e.get("text") or "").lower()
                        or "hello" in (e.get("text") or "").lower()
                        for e in rx.find("final_transcript")[1:]
                    )
                    or len(rx.find("final_transcript")) >= 2,
                    15,
                )
                if not got_final2:
                    failures.append("no second final transcript after barge-in")

            print(f"[TEST] AI frames nonsilent={rx.nonsilent} received={rx.received}")

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

    if failures:
        print(f"[TEST] PHASE6 FAIL — {failures}")
        return 1
    print("[TEST] PHASE6 PASS — speak -> STT -> LLM -> TTS -> speaker + barge-in works")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
