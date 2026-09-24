from typing import List, Union

from av import AudioFrame
from av.audio.resampler import AudioResampler
from utils.logger import get_logger

log = get_logger("AUDIO")

TARGET_FORMAT = "s16"
TARGET_LAYOUT = "mono"
TARGET_RATE = 16000


class AudioProcessor:
    """Converts WebRTC audio frames (48kHz stereo float) to 16kHz mono 16-bit PCM."""

    def __init__(self) -> None:
        self._resampler = AudioResampler(format=TARGET_FORMAT, layout=TARGET_LAYOUT, rate=TARGET_RATE)
        self._input_frames = 0
        self._output_chunks = 0

    def process(self, frame: AudioFrame) -> List[bytes]:
        result: Union[List[AudioFrame], AudioFrame] = self._resampler.resample(frame)
        frames = result if isinstance(result, list) else [result]
        chunks: List[bytes] = []
        for out_frame in frames:
            if out_frame is None:
                continue
            data = self._extract(out_frame)
            if data:
                chunks.append(data)
                self._output_chunks += 1
        self._input_frames += 1
        if self._input_frames == 1 and chunks:
            log.info(
                f"[AUDIO] Resampler engaged: in={frame.sample_rate}Hz "
                f"{frame.format.name} -> out={TARGET_RATE}Hz {TARGET_FORMAT} mono, "
                f"first chunk={len(chunks[0])} bytes"
            )
        return chunks

    @staticmethod
    def _extract(frame: AudioFrame) -> bytes:
        bytes_per_sample = frame.format.bytes
        sample_bytes = frame.samples * bytes_per_sample
        if frame.format.is_planar:
            return b"".join(bytes(plane)[:sample_bytes] for plane in frame.planes)
        total = sample_bytes * len(frame.layout.channels)
        return bytes(frame.planes[0])[:total]

    def flush(self) -> List[bytes]:
        result = self._resampler.resample(None)
        frames = result if isinstance(result, list) else ([result] if result else [])
        chunks: List[bytes] = []
        for frame in frames:
            if frame is None:
                continue
            data = self._extract(frame)
            if data:
                chunks.append(data)
        return chunks

    @property
    def stats(self) -> dict:
        return {"input_frames": self._input_frames, "output_chunks": self._output_chunks}
