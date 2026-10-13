"""Soundbox latency work: endpointing, streaming TTS, end marker, RTP continuity.

Run from server/:  .venv/Scripts/python.exe tests/test_soundbox_latency.py
"""

import asyncio
import io
import math
import sys
import time
import types
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiortc import RTCRtpSender  # noqa: E402
from aiortc.codecs.g711 import PcmuDecoder  # noqa: E402
from aiortc.jitterbuffer import JitterFrame  # noqa: E402
from aiortc.rtcrtpparameters import (  # noqa: E402
    RTCRtcpParameters,
    RTCRtpCodecParameters,
    RTCRtpSendParameters,
)
from aiortc.rtp import RtpPacket, is_rtcp  # noqa: E402

from audio.g711 import (  # noqa: E402
    END_OF_REPLY_PAYLOAD,
    END_OF_SPEECH_PAYLOAD,
    ULAW_SILENCE_FRAME,
    ulaw_decode,
    ulaw_encode,
)
from audio.processor import AudioProcessor  # noqa: E402
from audio.vad import EnergyVAD, VADEvent  # noqa: E402
from config import settings  # noqa: E402
from pipeline.reply_audio import FRAME_BYTES, frame_rms, split_tts_chunk  # noqa: E402
from pipeline.voice_pipeline import VoicePipeline  # noqa: E402
from services.tts import StreamingWavDecoder, wav_to_pcm_8k  # noqa: E402
from webrtc.audio_track import AIResponseAudioTrack  # noqa: E402
from webrtc.rtp_markers import intercept_end_of_speech  # noqa: E402
from webrtc.session import REPLY_END, Session  # noqa: E402

FRAME_MS = 20


def _tone_pcm(ms: int, amplitude: float = 0.3, freq: float = 440.0, rate: int = 8000) -> bytes:
    n = rate * ms // 1000
    return b"".join(
        int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / rate)).to_bytes(2, "little", signed=True)
        for i in range(n)
    )


def _ulaw_frames(pcm: bytes) -> list:
    data = ulaw_encode(pcm)
    return [data[i : i + 160] for i in range(0, len(data), 160)]


def _decode_ulaw_frames(payloads: list) -> list:
    """Through aiortc's real PCMU decoder, as the receiver does."""
    decoder = PcmuDecoder()
    frames = []
    for i, payload in enumerate(payloads):
        frames += decoder.decode(JitterFrame(data=payload, timestamp=i * 160))
    return frames


def _session() -> Session:
    return Session(session_id="test0000session", device_id="test-device")


class FakeSTT:
    def __init__(self) -> None:
        self.finalized_at = []
        self.audio_bytes = 0

    def mark_speech_start(self) -> None:
        pass

    async def send_audio(self, pcm: bytes) -> None:
        self.audio_bytes += len(pcm)

    async def maybe_partial(self) -> None:
        pass

    async def finalize(self) -> None:
        self.finalized_at.append(time.monotonic())


def _pipeline_with_fake_stt():
    session = _session()
    pipeline = VoicePipeline(session)
    session.pipeline = pipeline
    pipeline.stt = FakeSTT()
    return session, pipeline


async def _drain(queue: asyncio.Queue, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not queue.empty() and time.monotonic() < deadline:
        await asyncio.sleep(0.005)


# --- 1. Endpointing -------------------------------------------------------


def test_ulaw_silence_decodes_to_zero() -> None:
    assert ulaw_decode(b"\xff" * 160) == b"\x00" * 320
    frames = _decode_ulaw_frames([ULAW_SILENCE_FRAME])
    pcm = b"".join(AudioProcessor().process(frames[0]))
    assert EnergyVAD._rms(pcm) == 0.0


def test_silence_tail_ends_speech_within_800ms() -> None:
    """Speech + 1000 ms of 0xFF through the real decode path: stop at ~700 ms."""
    speech = _ulaw_frames(_tone_pcm(1000))
    tail = [ULAW_SILENCE_FRAME] * 50
    processor = AudioProcessor()
    vad = EnergyVAD(silence_ms=settings.vad_silence_ms, min_speech_ms=300, threshold=0.025)
    stopped_at = None
    started = False
    for index, frame in enumerate(_decode_ulaw_frames(speech + tail)):
        for pcm in processor.process(frame):
            events = vad.process(pcm)
            started = started or VADEvent.SPEECH_STARTED in events
            if VADEvent.SPEECH_STOPPED in events and stopped_at is None:
                stopped_at = index
    assert started
    assert stopped_at is not None, "1000 ms of 0xFF silence did not end the utterance"
    after_last_voice_ms = (stopped_at - (len(speech) - 1)) * FRAME_MS
    assert settings.vad_silence_ms <= after_last_voice_ms <= 800, after_last_voice_ms


async def _silence_tail_pipeline() -> None:
    session, pipeline = _pipeline_with_fake_stt()
    tasks = [asyncio.create_task(pipeline._audio_loop()), asyncio.create_task(pipeline._no_audio_watchdog())]
    try:
        for frame in _decode_ulaw_frames(_ulaw_frames(_tone_pcm(1000)) + [ULAW_SILENCE_FRAME] * 50):
            await session.audio_queue.put(frame)
        await _drain(session.audio_queue)
        await asyncio.sleep(0.05)
        assert len(pipeline.stt.finalized_at) == 1
        assert session.speech_end_reason == "silence", session.speech_end_reason
        await asyncio.sleep(settings.vad_no_audio_ms / 1000 + 0.1)
        assert len(pipeline.stt.finalized_at) == 1, "watchdog fired after the VAD already ended"
    finally:
        for task in tasks:
            task.cancel()


def test_silence_tail_pipeline_reason() -> None:
    asyncio.run(_silence_tail_pipeline())


async def _no_audio_endpoint() -> None:
    session, pipeline = _pipeline_with_fake_stt()
    tasks = [asyncio.create_task(pipeline._audio_loop()), asyncio.create_task(pipeline._no_audio_watchdog())]
    try:
        # Speech, then the device stops sending (only 200 ms of tail: the VAD
        # alone would never reach its 700 ms).
        for frame in _decode_ulaw_frames(_ulaw_frames(_tone_pcm(1000)) + [ULAW_SILENCE_FRAME] * 10):
            await session.audio_queue.put(frame)
        await _drain(session.audio_queue)
        last_audio = time.monotonic()
        assert pipeline.vad.is_speech_active
        await asyncio.sleep(1.0)
        assert len(pipeline.stt.finalized_at) == 1
        waited_ms = (pipeline.stt.finalized_at[0] - last_audio) * 1000
        assert settings.vad_no_audio_ms - 10 <= waited_ms <= settings.vad_no_audio_ms + 150, waited_ms
        assert session.speech_end_reason == "no-audio"
        assert not pipeline.vad.is_speech_active
    finally:
        for task in tasks:
            task.cancel()


def test_no_audio_endpoint() -> None:
    asyncio.run(_no_audio_endpoint())


def test_end_of_speech_packet_intercepted() -> None:
    pcmu = RTCRtpCodecParameters(mimeType="audio/PCMU", clockRate=8000, channels=1, payloadType=0)
    opus = RTCRtpCodecParameters(mimeType="audio/opus", clockRate=48000, channels=2, payloadType=111)
    calls = []
    receiver = types.SimpleNamespace(
        _RTCRtpReceiver__codecs={0: pcmu, 111: opus},
        _on_end_of_speech=lambda: calls.append(1),
    )

    packet = RtpPacket(payload_type=0, sequence_number=1, timestamp=160)
    packet.payload = END_OF_SPEECH_PAYLOAD
    assert intercept_end_of_speech(receiver, packet)
    assert calls == [1]
    # Not decoded as 2 samples: replaced by a full frame of u-law silence so
    # the jitter buffer sees no sequence gap.
    assert packet.payload == ULAW_SILENCE_FRAME

    voice = RtpPacket(payload_type=0, sequence_number=2, timestamp=320)
    voice.payload = b"\x10" * 160
    assert not intercept_end_of_speech(receiver, voice)
    assert voice.payload == b"\x10" * 160

    other = RtpPacket(payload_type=111, sequence_number=3, timestamp=480)
    other.payload = END_OF_SPEECH_PAYLOAD
    assert not intercept_end_of_speech(receiver, other)
    assert calls == [1]


async def _end_of_speech_pipeline() -> None:
    session, pipeline = _pipeline_with_fake_stt()
    tasks = [asyncio.create_task(pipeline._audio_loop()), asyncio.create_task(pipeline._no_audio_watchdog())]
    try:
        for frame in _decode_ulaw_frames(_ulaw_frames(_tone_pcm(1000))):
            await session.audio_queue.put(frame)
        await _drain(session.audio_queue)
        assert pipeline.vad.is_speech_active
        audio_before = pipeline.stt.audio_bytes
        t0 = time.monotonic()
        session.on_end_of_speech()
        await asyncio.sleep(0.05)
        assert len(pipeline.stt.finalized_at) == 1
        assert (pipeline.stt.finalized_at[0] - t0) * 1000 < 50
        assert session.speech_end_reason == "marker"
        assert pipeline.stt.audio_bytes == audio_before, "marker was fed to STT as audio"
        await asyncio.sleep(settings.vad_no_audio_ms / 1000 + 0.1)
        assert len(pipeline.stt.finalized_at) == 1
    finally:
        for task in tasks:
            task.cancel()


def test_end_of_speech_packet_ends_utterance() -> None:
    asyncio.run(_end_of_speech_pipeline())


# --- 2. Streaming TTS -----------------------------------------------------


def test_clause_splitting() -> None:
    assert split_tts_chunk("Sure, ") == (None, "Sure, ")  # too short for a clause
    chunk, rest = split_tts_chunk("I can help with that, just a moment")
    assert chunk == "I can help with that," and rest == "just a moment"
    assert split_tts_chunk("It costs 3.5") == (None, "It costs 3.5")  # no split mid-number
    assert split_tts_chunk("Done! Next") == ("Done!", "Next")
    chunk, rest = split_tts_chunk("word " * 20)
    assert chunk is not None and len(chunk) <= 80 and rest


class FakeStreamingTTS:
    """Chunk i is a tone with amplitude level(i), plus TTS-style silence."""

    LEAD_MS = 300
    TRAIL_MS = 1300

    def __init__(self) -> None:
        self.calls = []

    @staticmethod
    def level(index: int) -> int:
        return 3000 * (index + 1)

    async def synthesize_stream(self, text: str):
        index = len(self.calls)
        self.calls.append(text)
        tone = self.level(index).to_bytes(2, "little", signed=True) * (8000 * 400 // 1000)
        audio = b"\x00" * (16 * self.LEAD_MS) + tone + b"\x00" * (16 * self.TRAIL_MS)
        # Chunk 0 streams slowly, later chunks finish first: order must hold.
        delay = 0.03 if index == 0 else 0.001
        for i in range(0, len(audio), 1600):
            await asyncio.sleep(delay)
            yield audio[i : i + 1600]


async def _run_reply(deltas, ai_track=None):
    session = _session()
    pipeline = VoicePipeline(session)
    session.pipeline = pipeline
    session.ai_track = ai_track
    pipeline.tts = FakeStreamingTTS()
    tasks = [asyncio.create_task(pipeline._tts_loop()), asyncio.create_task(pipeline._reply_writer())]
    try:
        for delta in deltas:
            await session.llm_text_queue.put((pipeline._generation, delta))
        await session.llm_text_queue.put((pipeline._generation, None))
        items = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            item = await asyncio.wait_for(session.tts_audio_queue.get(), timeout=10)
            items.append(item)
            if item is REPLY_END:
                break
        return pipeline, items
    finally:
        for task in tasks:
            task.cancel()


def _frame_levels(items) -> list:
    audio = b"".join(i for i in items if i is not REPLY_END)
    assert len(audio) % FRAME_BYTES == 0
    levels = []
    for i in range(0, len(audio), FRAME_BYTES):
        frame = audio[i : i + FRAME_BYTES]
        levels.append(0 if frame_rms(frame) == 0 else int.from_bytes(frame[:2], "little", signed=True))
    return levels


def test_tts_chunks_keep_order_and_trim_silence() -> None:
    deltas = ["Your payment of five ", "hundred rupees arrived. ", "The ", "refund, which you asked about, ", "is done."]
    pipeline, items = asyncio.run(_run_reply(deltas))
    assert pipeline.tts.calls == [
        "Your payment of five hundred rupees arrived.",
        "The refund, which you asked about,",
        "is done.",
    ]
    assert items[-1] is REPLY_END and items.count(REPLY_END) == 1
    levels = _frame_levels(items)
    voiced = [lvl for lvl in levels if lvl]
    order = [lvl for i, lvl in enumerate(voiced) if i == 0 or voiced[i - 1] != lvl]
    assert order == [FakeStreamingTTS.level(0), FakeStreamingTTS.level(1), FakeStreamingTTS.level(2)], order
    assert levels[0] != 0, "leading TTS silence was not trimmed"
    assert levels[-1] != 0, "trailing TTS silence was not trimmed before the marker"
    # Each chunk boundary had 1.3 s + 0.3 s of TTS silence; at most
    # TTS_MAX_PAUSE_MS of it is kept.
    runs, run = [], 0
    for lvl in levels + [1]:
        if lvl == 0:
            run += 1
        elif run:
            runs.append(run)
            run = 0
    assert len(runs) <= 2 and all(r <= settings.tts_max_pause_ms // FRAME_MS for r in runs), runs


def test_tts_boundary_pause_capped_while_playing() -> None:
    busy_track = types.SimpleNamespace(has_pending=True)
    _, items = asyncio.run(_run_reply(["First sentence goes here. ", "Second sentence goes here."], busy_track))
    levels = _frame_levels(items)
    gap = len(levels) - len([lvl for lvl in levels if lvl])
    assert gap == settings.tts_max_pause_ms // FRAME_MS, gap
    assert levels[-1] != 0


def test_streaming_wav_decoder_matches_whole_file() -> None:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(_tone_pcm(500, rate=24000))
    data = buf.getvalue()
    decoder = StreamingWavDecoder()
    streamed = b"".join(decoder.feed(data[i : i + 777]) for i in range(0, len(data), 777)) + decoder.flush()
    whole = wav_to_pcm_8k(data)
    assert abs(len(streamed) - 8000) <= 64 and abs(len(whole) - 8000) <= 64, (len(streamed), len(whole))


# --- 3. End marker + RTP continuity (real aiortc RTCRtpSender) ------------


class CaptureTransport:
    state = "connected"
    _stats_id = "t"

    def __init__(self) -> None:
        self.rtp = []

    def _register_rtp_sender(self, sender, parameters) -> None:
        pass

    def _unregister_rtp_sender(self, sender) -> None:
        pass

    async def _send_rtp(self, data: bytes) -> None:
        if not is_rtcp(data):
            self.rtp.append((time.monotonic(), RtpPacket.parse(data)))


async def _run_sender(track, transport, until) -> None:
    sender = RTCRtpSender(track, transport)
    await sender.send(
        RTCRtpSendParameters(
            codecs=[RTCRtpCodecParameters(mimeType="audio/PCMU", clockRate=8000, channels=1, payloadType=0)],
            rtcp=RTCRtcpParameters(cname="test"),
            muxId="0",
        )
    )
    try:
        await until()
    finally:
        await sender.stop()


def _voice(level: int, frames: int) -> bytes:
    return level.to_bytes(2, "little", signed=True) * (160 * frames)


def _check_continuity(packets) -> None:
    for a, b in zip(packets, packets[1:]):
        assert (b.sequence_number - a.sequence_number) % 65536 == 1, (a.sequence_number, b.sequence_number)
        assert (b.timestamp - a.timestamp) % 2**32 == 160, (a.timestamp, b.timestamp)
        assert b.ssrc == a.ssrc


async def _marker_and_continuity() -> None:
    session = _session()
    session.downlink_codec = "audio/pcmu"
    track = AIResponseAudioTrack(session)
    transport = CaptureTransport()
    q = session.tts_audio_queue

    async def scenario() -> None:
        await asyncio.sleep(0.1)  # idle silence first
        q.put_nowait(_voice(1000, 5))  # reply 1, chunk A
        await asyncio.sleep(0.04)
        q.put_nowait(_voice(2000, 5))  # chunk B (streams in while A plays)
        q.put_nowait(REPLY_END)
        q.put_nowait(_voice(3000, 3))  # reply 2 already queued behind it
        q.put_nowait(REPLY_END)
        await asyncio.sleep(0.6)

    await _run_sender(track, transport, scenario)
    packets = [p for _, p in transport.rtp]
    times = [t for t, _ in transport.rtp]
    _check_continuity(packets)
    payloads = [p.payload for p in packets]
    markers = [i for i, p in enumerate(payloads) if p == END_OF_REPLY_PAYLOAD]
    assert len(markers) == 2, markers
    assert track.markers_sent == 2
    voice = [i for i, p in enumerate(payloads) if len(p) == 160 and p != ULAW_SILENCE_FRAME]
    assert len(voice) == 13
    # Reply 1: 10 voice frames back-to-back (A then B, no gap), then the marker.
    first = voice[0]
    assert voice[:10] == list(range(first, first + 10))
    assert payloads[first] == ulaw_encode(_voice(1000, 1)) and payloads[first + 5] == ulaw_encode(_voice(2000, 1))
    assert markers[0] == first + 10
    # Reply 2 follows its own marker slot directly.
    assert voice[10:] == [markers[0] + 1, markers[0] + 2, markers[0] + 3]
    assert markers[1] == markers[0] + 4
    # After the last marker: normal 0xFF silence frames keep the stream alive.
    assert payloads[markers[1] + 1 :] and all(p == ULAW_SILENCE_FRAME for p in payloads[markers[1] + 1 :])
    rate = (len(times) - 1) / (times[-1] - times[0])
    assert 45 <= rate <= 52, rate
    assert "end_marker_sent" in session.latency


def test_end_marker_and_rtp_continuity() -> None:
    asyncio.run(_marker_and_continuity())


async def _barge_in_marker() -> None:
    session = _session()
    session.downlink_codec = "audio/pcmu"
    track = AIResponseAudioTrack(session)
    transport = CaptureTransport()

    async def scenario() -> None:
        session.tts_audio_queue.put_nowait(_voice(1000, 50))
        session.tts_audio_queue.put_nowait(REPLY_END)
        await asyncio.sleep(0.15)
        # What VoicePipeline.interrupt() does on barge-in.
        while not session.tts_audio_queue.empty():
            session.tts_audio_queue.get_nowait()
        track.cancel_reply()
        await asyncio.sleep(0.2)

    await _run_sender(track, transport, scenario)
    packets = [p for _, p in transport.rtp]
    _check_continuity(packets)
    payloads = [p.payload for p in packets]
    markers = [i for i, p in enumerate(payloads) if p == END_OF_REPLY_PAYLOAD]
    assert len(markers) == 1, markers
    voice = [i for i, p in enumerate(payloads) if len(p) == 160 and p != ULAW_SILENCE_FRAME]
    assert 3 <= len(voice) < 50 and markers[0] == voice[-1] + 1
    assert all(p == ULAW_SILENCE_FRAME for p in payloads[markers[0] + 1 :])


def test_barge_in_stops_reply_with_marker() -> None:
    asyncio.run(_barge_in_marker())


def test_non_pcmu_clients_get_audio_frames_and_no_marker() -> None:
    async def run() -> None:
        session = _session()
        track = AIResponseAudioTrack(session)  # downlink_codec unknown (e.g. Opus)
        session.tts_audio_queue.put_nowait(_voice(1000, 1))
        session.tts_audio_queue.put_nowait(REPLY_END)
        frames = [await track.recv() for _ in range(3)]
        assert all(f.__class__.__name__ == "AudioFrame" for f in frames)
        assert [f.pts for f in frames] == [0, 160, 320]
        assert track.markers_sent == 0

    asyncio.run(run())


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {name}: {exc!r}")
            import traceback

            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
