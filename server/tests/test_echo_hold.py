import asyncio
import types

from config import settings
from pipeline.echo_pipeline import EchoPipeline
from webrtc.audio_track import FRAME_BYTES
from webrtc.session import REPLY_END


def _fake_session() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        session_id="hold-test-session",
        audio_queue=asyncio.Queue(),
        tts_audio_queue=asyncio.Queue(),
        ai_track=None,
        uplink_capture=None,
        pipeline=None,
        speech_end_reason=None,
        finish_turn=lambda: None,
        begin_turn=lambda: None,
        mark_stage=lambda *args, **kwargs: None,
    )


def _drain(queue: asyncio.Queue) -> list:
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


async def _hold_sends_utterance_after_it_ends() -> None:
    settings.echo_mode = "hold"
    settings.record_echo = False
    settings.vad_no_audio_ms = 400
    session = _fake_session()
    pipeline = EchoPipeline(session)
    await pipeline.start()
    try:
        frames = [bytes([i, 0]) * (FRAME_BYTES // 2) for i in range(1, 51)]  # 1 s
        for frame in frames:
            session.audio_queue.put_nowait(frame)
        await asyncio.sleep(0.1)
        # Mid-utterance: nothing is sent back yet.
        assert session.tts_audio_queue.empty(), "hold mode echoed before the utterance ended"
        # No inbound audio for VAD_NO_AUDIO_MS ends the utterance.
        await asyncio.sleep(0.5)
        items = _drain(session.tts_audio_queue)
        assert items[-1] is REPLY_END, items[-1:]
        assert b"".join(items[:-1]) == b"".join(frames), "held audio differs from input"
        assert session.speech_end_reason == "no-audio", session.speech_end_reason
    finally:
        await pipeline.stop()
        settings.echo_mode = "live"


async def _live_still_streams() -> None:
    settings.echo_mode = "live"
    settings.record_echo = False
    session = _fake_session()
    pipeline = EchoPipeline(session)
    await pipeline.start()
    try:
        session.audio_queue.put_nowait(b"\x01\x00" * (FRAME_BYTES // 2))
        await asyncio.sleep(0.1)
        # No ai_track (WebSocket path): audio is queued immediately.
        assert not session.tts_audio_queue.empty(), "live mode did not stream"
    finally:
        await pipeline.stop()


def test_hold_sends_utterance_after_it_ends() -> None:
    asyncio.run(_hold_sends_utterance_after_it_ends())


def test_live_still_streams() -> None:
    asyncio.run(_live_still_streams())


if __name__ == "__main__":
    tests = [
        test_hold_sends_utterance_after_it_ends,
        test_live_still_streams,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} passed")
