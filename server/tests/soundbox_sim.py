"""Soundbox device simulator: end-to-end latency run against the real server.

Behaves like the cellular Soundbox: a single PCMU track each way, 20 ms /
160-byte frames. Streams the question as u-law speech, then a u-law silence
tail (0xFF), optionally an end-of-speech packet (payload 00 FE), then STOPS
sending RTP. Records every downlink RTP packet to check pacing, sequence /
timestamp continuity and the 00 FF end-of-reply marker.

Usage (from server/):
    .venv/Scripts/python.exe tests/soundbox_sim.py --tail-ms 1000
    .venv/Scripts/python.exe tests/soundbox_sim.py --tail-ms 600
    .venv/Scripts/python.exe tests/soundbox_sim.py --tail-ms 1000 --eos
"""

import argparse
import asyncio
import os
import sys
import time
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Force the LLM path (no instant fixed/KB/cache/live answers) so the run
# measures STT + LLM + TTS. Must be set before config is imported.
for _name in ("FIXED_ANSWERS_ENABLED", "KB_ENABLED", "ANSWER_CACHE_ENABLED", "LIVE_ANSWERS_ENABLED"):
    os.environ.setdefault(_name, "false")

import audioop  # noqa: E402  (deprecated, but present on 3.12; test-only)

import av  # noqa: E402
import httpx  # noqa: E402
import uvicorn  # noqa: E402
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCRtpSender, RTCSessionDescription  # noqa: E402

from main import app  # noqa: E402
from services.tts import SapiTTS  # noqa: E402

PORT = 8767
SERVER = f"http://127.0.0.1:{PORT}"
FRAME = 160  # samples / u-law bytes per 20 ms
ULAW_SILENCE = b"\xff" * FRAME
END_OF_SPEECH = b"\x00\xfe"
END_OF_REPLY = b"\x00\xff"


def _ms(t: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int(t * 1000) % 1000:03d}"


class DeviceMic(MediaStreamTrack):
    """Pre-encoded PCMU uplink: speech, silence tail, optional 00 FE, then nothing."""

    kind = "audio"

    def __init__(self, speech_ulaw: bytes, tail_ms: int, eos: bool) -> None:
        super().__init__()
        self._packets: list[bytes] = []
        for i in range(0, len(speech_ulaw), FRAME):
            self._packets.append(speech_ulaw[i : i + FRAME].ljust(FRAME, b"\xff"))
        self._n_speech = len(self._packets)
        self._packets += [ULAW_SILENCE] * (tail_ms // 20)
        if eos:
            self._packets.append(END_OF_SPEECH)
        self._index = 0
        self._pts = 0
        self._t0 = None
        self.last_speech_sent = None
        self.finished = asyncio.Event()
        self.start = asyncio.Event()

    async def recv(self):
        await self.start.wait()
        if self._index >= len(self._packets):
            self.finished.set()
            await asyncio.Event().wait()  # device stops sending RTP
        now = time.time()
        if self._t0 is None:
            self._t0 = now
        wait = self._t0 + self._index * 0.02 - now
        if wait > 0:
            await asyncio.sleep(wait)
        payload = self._packets[self._index]
        self._index += 1
        if self._index == self._n_speech:
            self.last_speech_sent = time.time()
        packet = av.Packet(payload)
        packet.pts = self._pts
        packet.time_base = Fraction(1, 8000)
        self._pts += FRAME
        return packet


class DownlinkRecorder:
    def __init__(self) -> None:
        self.packets: list[tuple[float, int, int, bytes]] = []

    def install(self, receiver) -> None:
        original = receiver._handle_rtp_packet

        async def handle(packet, arrival_time_ms):
            self.packets.append((time.time(), packet.sequence_number, packet.timestamp, bytes(packet.payload)))
            await original(packet, arrival_time_ms)

        receiver._handle_rtp_packet = handle

    def report(self, since: float) -> dict:
        pkts = [p for p in self.packets if p[0] >= since]
        gaps = sum(1 for a, b in zip(pkts, pkts[1:]) if (b[1] - a[1]) % 65536 != 1)
        ts_bad = sum(1 for a, b in zip(pkts, pkts[1:]) if (b[2] - a[2]) % 2**32 != FRAME)
        voice = [p for p in pkts if len(p[3]) == FRAME and p[3] != ULAW_SILENCE]
        markers = [p for p in pkts if p[3] == END_OF_REPLY]
        out = {
            "packets": len(pkts),
            "seq_gaps": gaps,
            "ts_steps_not_160": ts_bad,
            "voice_packets": len(voice),
            "end_markers": len(markers),
            "first_voice": voice[0][0] if voice else None,
            "last_voice": voice[-1][0] if voice else None,
            "marker": markers[0][0] if markers else None,
        }
        if voice:
            span = [p for p in pkts if voice[0][0] <= p[0] <= voice[-1][0]]
            dur = voice[-1][0] - voice[0][0]
            out["rate_pps"] = round((len(span) - 1) / dur, 1) if dur > 0 else None
            if markers:
                m = markers[0]
                last_v = voice[-1]
                out["marker_seq_step"] = (m[1] - last_v[1]) % 65536
                out["marker_ts_step"] = (m[2] - last_v[2]) % 2**32
        return out


async def run(args) -> int:
    tts = SapiTTS()
    await tts.start()
    pcm8k = await tts.synthesize(args.question)
    # Strip SAPI's own leading/trailing silence so the tail is exactly --tail-ms.
    frames = [pcm8k[i : i + FRAME * 2] for i in range(0, len(pcm8k), FRAME * 2)]
    voiced = [i for i, f in enumerate(frames) if audioop.rms(f, 2) / 32768 >= 0.01]
    pcm8k = b"".join(frames[voiced[0] : voiced[-1] + 1])
    speech = audioop.lin2ulaw(pcm8k, 2)
    print(f"[SIM] question={args.question!r} speech={len(speech) / 8000:.2f}s tail={args.tail_ms}ms eos={args.eos}")

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    server_task = asyncio.create_task(server.serve())
    pc = RTCPeerConnection()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            for _ in range(100):
                try:
                    if (await client.get(f"{SERVER}/health")).status_code == 200:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.1)

            mic = DeviceMic(speech, args.tail_ms, args.eos)
            transceiver = pc.addTransceiver(mic, direction="sendrecv")
            pcmu = [c for c in RTCRtpSender.getCapabilities("audio").codecs if c.mimeType == "audio/PCMU"]
            transceiver.setCodecPreferences(pcmu)
            recorder = DownlinkRecorder()
            recorder.install(transceiver.receiver)

            await pc.setLocalDescription(await pc.createOffer())
            res = await client.post(
                f"{SERVER}/webrtc/offer",
                json={"device_id": "SIM_SOUNDBOX", "sdp": pc.localDescription.sdp, "type": "offer"},
            )
            res.raise_for_status()
            data = res.json()
            await pc.setRemoteDescription(RTCSessionDescription(sdp=data["sdp"], type=data["type"]))
            for _ in range(100):
                if pc.connectionState == "connected":
                    break
                await asyncio.sleep(0.1)
            await asyncio.sleep(1.0)  # let the pipeline start

            t_start = time.time()
            mic.start.set()
            # Wait for the reply to finish (marker, or downlink voice idle).
            deadline = time.time() + args.timeout
            while time.time() < deadline:
                await asyncio.sleep(0.2)
                rep = recorder.report(t_start)
                if rep["marker"] is not None:
                    await asyncio.sleep(0.5)
                    break
                if rep["last_voice"] is not None and time.time() - rep["last_voice"] > 2.0:
                    break
            rep = recorder.report(t_start)
            ls = mic.last_speech_sent
            print(f"[SIM] last speech frame sent at {_ms(ls)}")
            for key in ("first_voice", "last_voice", "marker"):
                if rep[key] is not None:
                    print(f"[SIM] {key} received at {_ms(rep[key])} (+{(rep[key] - ls) * 1000:.0f}ms after last speech)")
            print(
                "[SIM] downlink: "
                + " ".join(f"{k}={v}" for k, v in rep.items() if k not in ("first_voice", "last_voice", "marker"))
            )
    finally:
        await pc.close()
        server.should_exit = True
        await server_task
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--question", default="Can you play some music for me?")
    parser.add_argument("--tail-ms", type=int, default=1000)
    parser.add_argument("--eos", action="store_true", help="send a 00 FE end-of-speech packet after the tail")
    parser.add_argument("--timeout", type=float, default=20.0)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
