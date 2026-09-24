import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from dashboard_store import dashboard
from utils.logger import get_logger

log = get_logger("SESSION")

MAX_HISTORY_MESSAGES_DEFAULT = 10


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
    ai_playing: bool = False
    state: str = "new"

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

    def mark_stage(self, name: str, t_ms: Optional[float] = None) -> None:
        t = t_ms if t_ms is not None else time.time() * 1000.0
        self.latency[name] = t
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
            f"frames_in={session.audio_frames_received} frames_out={session.audio_frames_sent}"
        )
        return True


session_manager = SessionManager()
