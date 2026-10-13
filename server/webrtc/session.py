import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from dashboard_store import dashboard
from utils.logger import get_logger

log = get_logger("SESSION")
# Per-turn latency summary shares the PIPELINE prefix with the stage logs.
pipeline_log = get_logger("PIPELINE")

MAX_HISTORY_MESSAGES_DEFAULT = 10


def _fmt_ts(t_ms: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t_ms / 1000.0)) + f".{int(t_ms % 1000):03d}"
AUDIO_TRAILER = b"\x00\xff"  # appended to every binary audio message sent to the client


class _Sentinel:
    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return self.name


# tts_audio_queue item: the current reply is complete (send the end marker).
REPLY_END = _Sentinel("REPLY_END")
# audio_queue item: the device signalled end of speech (RTP payload 00 FE).
END_OF_SPEECH = _Sentinel("END_OF_SPEECH")

# Turn stages in order, as printed in the per-turn latency summary.
TURN_STAGES = (
    ("speech_start", "speech_start"),
    ("speech_end", "speech_end"),
    ("stt_final", "stt_done"),
    ("llm_first_token", "llm_first_token"),
    ("tts_first_audio", "tts_first_audio"),
    ("audio_sent", "first_reply_rtp_sent"),
    ("last_voice_rtp_sent", "last_voice_rtp_sent"),
    ("end_marker_sent", "end_marker_sent"),
)


@dataclass
class Session:
    session_id: str
    device_id: str
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)

    peer_connection: Any = None
    data_channel: Any = None
    websocket: Any = None
    audio_track: Any = None
    ai_track: Any = None
    audio_sample_rate: int = 48000
    # Negotiated downlink codec mimeType (lowercase), e.g. "audio/pcmu".
    downlink_codec: Optional[str] = None

    # Pipeline services (per-session isolation, wired in later phases)
    stt: Any = None
    llm: Any = None
    tts: Any = None
    vad: Any = None
    pipeline: Any = None

    conversation_history: List[Dict[str, str]] = field(default_factory=list)
    latency: Dict[str, float] = field(default_factory=dict)

    audio_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    transcript_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    llm_text_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    tts_audio_queue: asyncio.Queue = field(default_factory=asyncio.Queue)

    audio_frames_received: int = 0
    audio_frames_sent: int = 0
    audio_frames_emitted: int = 0
    ai_playing: bool = False
    state: str = "new"

    # Response send tracking (first/last chunk of the current AI reply)
    send_first_ms: Optional[float] = None
    send_last_ms: Optional[float] = None
    send_chunks: int = 0
    # Why the current utterance ended: silence / no-audio / marker.
    speech_end_reason: Optional[str] = None
    _turn_logged: bool = False

    def touch(self) -> None:
        self.last_activity = time.time()

    def send_event(self, event: Dict[str, Any]) -> None:
        payload = json.dumps(event)
        etype = str(event.get("type", "event"))
        detail = ""
        if etype in ("partial_transcript", "final_transcript", "assistant_delta", "assistant_message"):
            detail = str(event.get("text", ""))[:120]
        elif etype == "error":
            detail = str(event.get("message", ""))
        elif etype == "vad":
            detail = str(event.get("event", ""))
        elif etype == "latency":
            detail = json.dumps(event.get("metrics", {}))
        channel = self.data_channel
        if channel is not None:
            try:
                if channel.readyState == "open":
                    channel.send(payload)
                    dashboard.msg_to_apk(self.session_id, etype, detail, len(payload))
                    return
            except Exception as exc:
                log.debug(f"[SESSION] Event send failed: {exc!r}")
        ws = self.websocket
        if ws is not None:
            try:
                self._schedule_ws_text(payload)
                dashboard.msg_to_apk(self.session_id, etype, detail, len(payload))
            except Exception as exc:
                log.debug(f"[SESSION] WS event send failed: {exc!r}")

    def send_audio_bytes(self, data: bytes) -> None:
        ws = self.websocket
        if ws is None or not data:
            return
        data = data + AUDIO_TRAILER
        try:
            self._schedule_ws_bytes(data)
            dashboard.audio_to_apk(self.session_id, len(data))
        except Exception as exc:
            log.debug(f"[SESSION] WS audio send failed: {exc!r}")

    def _schedule_ws_text(self, payload: str) -> None:
        import asyncio

        loop = asyncio.get_running_loop()
        loop.create_task(self._ws_send_text(payload))

    def _schedule_ws_bytes(self, data: bytes) -> None:
        import asyncio

        loop = asyncio.get_running_loop()
        loop.create_task(self._ws_send_bytes(data))

    async def _ws_send_text(self, payload: str) -> None:
        ws = self.websocket
        if ws is None:
            return
        try:
            await ws.send_text(payload)
        except Exception as exc:
            log.debug(f"[SESSION] WS text failed: {exc!r}")

    async def _ws_send_bytes(self, data: bytes) -> None:
        ws = self.websocket
        if ws is None:
            return
        try:
            await ws.send_bytes(data)
        except Exception as exc:
            log.debug(f"[SESSION] WS bytes failed: {exc!r}")

    def emit_latency(self) -> None:
        lat = self.latency
        metrics: Dict[str, float] = {}
        if "speech_end" in lat and "stt_final" in lat:
            metrics["stt"] = round(lat["stt_final"] - lat["speech_end"], 1)
        if "llm_start" in lat and "llm_first_token" in lat:
            metrics["llm_first_token"] = round(lat["llm_first_token"] - lat["llm_start"], 1)
        if "tts_start" in lat and "tts_first_audio" in lat:
            metrics["tts_first_audio"] = round(lat["tts_first_audio"] - lat["tts_start"], 1)
        if "speech_end" in lat and "audio_sent" in lat:
            metrics["total"] = round(lat["audio_sent"] - lat["speech_end"], 1)
        if metrics:
            dashboard.note_latency(self.session_id, metrics)
            self.send_event({"type": "latency", "metrics": metrics})
        if "audio_sent" in lat:
            dashboard.turn_finished(self.session_id, metrics)

    def finish_turn(self) -> None:
        """Log one latency summary line for the turn (at most once per turn)."""
        lat = self.latency
        if self._turn_logged or "speech_end" not in lat:
            return
        self._turn_logged = True

        stamps = []
        for key, label in TURN_STAGES:
            value = _fmt_ts(lat[key]) if key in lat else "n/a"
            if key == "speech_end":
                value += f"({self.speech_end_reason or 'silence'})"
            stamps.append(f"{label}={value}")

        def d(a: str, b: str) -> str:
            if a in lat and b in lat:
                return f"{lat[b] - lat[a]:.0f}ms"
            return "n/a"

        pipeline_log.info(
            f"[LATENCY] session={self.session_id[:8]} " + " ".join(stamps) + " | "
            f"speech={d('speech_start', 'speech_end')} "
            f"endpoint={d('last_voiced', 'speech_end')} "
            f"stt={d('speech_end', 'stt_final')} "
            f"llm_first_token={d('stt_final', 'llm_first_token')} "
            f"tts_first_audio={d('llm_first_token', 'tts_first_audio')} "
            f"to_rtp={d('tts_first_audio', 'audio_sent')} "
            f"reply_audio={d('audio_sent', 'last_voice_rtp_sent')} "
            f"marker={d('last_voice_rtp_sent', 'end_marker_sent')} | "
            f"speech_end_to_first_rtp={d('speech_end', 'audio_sent')}"
        )

    def begin_turn(self) -> None:
        self.latency = {}
        self.speech_end_reason = None
        self._turn_logged = False

    def on_end_of_speech(self) -> None:
        """RTP receiver hook: the device sent the 00 FE end-of-speech packet."""
        if self.pipeline is not None:
            self.audio_queue.put_nowait(END_OF_SPEECH)

    def note_audio_chunk_sent(self, nbytes: int) -> None:
        """Called by the transport for every real (non-silence) audio chunk sent."""
        now = time.time() * 1000.0
        if self.send_first_ms is None:
            self.send_first_ms = now
            self.send_chunks = 0
            log.info(f"[SEND] First response chunk sent at {_fmt_ts(now)} ({nbytes} bytes)")
        self.send_last_ms = now
        self.send_chunks += 1

    def end_response_send(self, reason: str) -> None:
        """Log the last chunk sent for the current reply and reset tracking."""
        first, last = self.send_first_ms, self.send_last_ms
        if first is None or last is None:
            return
        speech_end = self.latency.get("speech_end")
        since_speech = f"{last - speech_end:.0f}ms" if speech_end is not None else "n/a"
        log.info(
            f"[SEND] Last response chunk sent at {_fmt_ts(last)} ({reason}) "
            f"first={_fmt_ts(first)} send_duration={last - first:.0f}ms "
            f"chunks={self.send_chunks} speech_end_to_last={since_speech}"
        )
        self.send_first_ms = None
        self.send_last_ms = None
        self.send_chunks = 0

    def mark_stage(self, name: str, t_ms: Optional[float] = None, record_dashboard: bool = True) -> None:
        t = t_ms if t_ms is not None else time.time() * 1000.0
        self.latency[name] = t
        if record_dashboard:
            dashboard.turn_stage(self.session_id, name, t)

    def add_history(self, role: str, content: str, max_messages: int = MAX_HISTORY_MESSAGES_DEFAULT) -> None:
        self.conversation_history.append({"role": role, "content": content})
        if len(self.conversation_history) > max_messages:
            self.conversation_history = self.conversation_history[-max_messages:]

    def snapshot(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "device_id": self.device_id,
            "state": self.state,
            "audio_frames_received": self.audio_frames_received,
            "audio_frames_sent": self.audio_frames_sent,
            "audio_frames_emitted": self.audio_frames_emitted,
            "audio_frames_per_second": 50,
            "audio_ms_per_second": 1000,
            "history_messages": len(self.conversation_history),
            "latency": dict(self.latency),
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "peer_state": getattr(self.peer_connection, "connectionState", None),
        }


class SessionManager:
    def __init__(self) -> None:
        self._sessions: Dict[str, Session] = {}

    def create(self, device_id: str) -> Session:
        session = Session(session_id=uuid.uuid4().hex, device_id=device_id, state="connecting")
        self._sessions[session.session_id] = session
        log.info(f"[SESSION] Created {session.session_id} for device {device_id}")
        return session

    def get(self, session_id: str) -> Optional[Session]:
        return self._sessions.get(session_id)

    def list(self) -> List[Session]:
        return list(self._sessions.values())

    def count(self) -> int:
        return len(self._sessions)

    async def close(self, session_id: str) -> bool:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False

        session.state = "closing"
        if session.pipeline is not None:
            try:
                await session.pipeline.stop()
            except Exception as exc:
                log.error(f"[SESSION] Pipeline stop error {session_id}: {exc}")
            session.pipeline = None

        ws = session.websocket
        if ws is not None:
            session.websocket = None
            try:
                if ws.client_state.name == "CONNECTED":
                    import asyncio

                    await asyncio.wait_for(ws.close(), timeout=1.0)
            except Exception:
                pass

        pc = session.peer_connection
        if pc is not None:
            try:
                await pc.close()
            except Exception as exc:
                log.error(f"[SESSION] Peer close error {session_id}: {exc}")

        for queue in (
            session.audio_queue,
            session.transcript_queue,
            session.llm_text_queue,
            session.tts_audio_queue,
        ):
            try:
                while not queue.empty():
                    queue.get_nowait()
            except Exception:
                pass

        session.state = "closed"
        dashboard.session_close(session_id)
        log.info(
            f"[SESSION] Closed {session_id} device={session.device_id} "
            f"frames_in={session.audio_frames_received} "
            f"frames_out={session.audio_frames_sent} "
            f"tx_emitted={session.audio_frames_emitted}"
        )
        return True


session_manager = SessionManager()
