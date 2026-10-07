import asyncio
import time

from webrtc.audio_track import AIResponseAudioTrack
from webrtc.session import Session


class FakeSession(Session):
    def __init__(self) -> None:
        super().__init__(session_id="test", device_id="test-device")
        self.send_audio_bytes_calls: list[bytes] = []

    def send_audio_bytes(self, data: bytes) -> None:
        self.send_audio_bytes_calls.append(data)


def test_recv_returns_immediate_20ms_frame_without_model_wait() -> None:
    session = FakeSession()
    track = AIResponseAudioTrack(session)

    started = time.perf_counter()
    frame = asyncio.run(track.recv())
    elapsed = time.perf_counter() - started

    assert elapsed < 0.01
    assert frame.sample_rate == 8000
    assert frame.samples == 160
    assert frame.format.name == "s16"
    assert len(bytes(frame.planes[0])) == 320
    assert all(value == 0 for value in bytes(frame.planes[0]))


def test_idle_stats_expose_50_frames_per_second_and_1000ms_per_second() -> None:
    session = FakeSession()
    snapshot = session.snapshot()

    assert snapshot["audio_frames_per_second"] == 50
    assert snapshot["audio_ms_per_second"] == 1000
