import asyncio
import time
import types

from webrtc.audio_track import AIResponseAudioTrack, FRAME_BYTES, SAMPLES_PER_FRAME


def _fake_session() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        tts_audio_queue=asyncio.Queue(),
        ai_playing=False,
        audio_frames_emitted=0,
        audio_frames_sent=0,
        latency={},
        send_audio_bytes=lambda data: None,
        mark_stage=lambda name: None,
        emit_latency=lambda: None,
    )


def _run(coro):
    return asyncio.run(coro)


async def _pace_holds_50_fps() -> None:
    track = AIResponseAudioTrack(_fake_session())
    n = 11  # first frame is immediate, then 10 gaps of 20 ms -> ~0.20 s
    t0 = time.monotonic()
    pts = []
    for _ in range(n):
        frame = await track.recv()
        pts.append(frame.pts)
    elapsed = time.monotonic() - t0
    expected = (n - 1) * 0.02
    # Lower bound proves no flood (the old code returned in ~0 s);
    # upper bound proves the schedule is not drifting.
    assert elapsed >= expected - 0.05, f"too fast: {elapsed:.3f}s for {n} frames"
    assert elapsed <= expected + 0.5, f"too slow: {elapsed:.3f}s for {n} frames"
    assert pts == [i * SAMPLES_PER_FRAME for i in range(n)], pts
    assert track.frames_emitted == n
    assert track.session.audio_frames_emitted == n


async def _stall_reanchors_instead_of_sleeping_old_schedule() -> None:
    track = AIResponseAudioTrack(_fake_session())
    # Pretend the last frame went out 10 s ago (event-loop stall).
    track._pace_start = time.time() - 10
    t0 = time.monotonic()
    await track.recv()
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5, f"stalled schedule slept {elapsed:.3f}s instead of re-anchoring"
    # The next frame must be scheduled 20 ms after the re-anchor, not burst.
    t0 = time.monotonic()
    await track.recv()
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.015, f"frame after re-anchor returned in {elapsed:.3f}s"


async def _voice_frames_still_counted() -> None:
    session = _fake_session()
    track = AIResponseAudioTrack(session)
    session.tts_audio_queue.put_nowait(b"\x01\x02" * (FRAME_BYTES // 2))
    await track.recv()
    assert session.audio_frames_sent == 1, session.audio_frames_sent
    await track.recv()  # queue empty -> silence, voice count unchanged
    assert session.audio_frames_sent == 1, session.audio_frames_sent


def test_pace_holds_50_fps() -> None:
    _run(_pace_holds_50_fps())


def test_stall_reanchors() -> None:
    _run(_stall_reanchors_instead_of_sleeping_old_schedule())


def test_voice_frames_still_counted() -> None:
    _run(_voice_frames_still_counted())


if __name__ == "__main__":
    tests = [
        test_pace_holds_50_fps,
        test_stall_reanchors,
        test_voice_frames_still_counted,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} passed")
