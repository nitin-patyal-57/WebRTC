import time
from collections import deque
from typing import Any, Dict, List, Optional

from utils.logger import get_logger

log = get_logger("DASH")


def now_ms() -> float:
    return time.time() * 1000.0


class DashboardStore:
    def __init__(self) -> None:
        self.started_at = time.time()
        self.live: Dict[str, Dict[str, Any]] = {}
        self.closed: deque = deque(maxlen=50)
        self.turns: deque = deque(maxlen=200)
        self.events: deque = deque(maxlen=800)
        self.counters: Dict[str, int] = {
            "sessions_total": 0,
            "sessions_live": 0,
            "turns_total": 0,
            "bytes_from_apk": 0,
            "bytes_to_apk": 0,
            "msgs_from_apk": 0,
            "msgs_to_apk": 0,
            "audio_chunks_from_apk": 0,
            "audio_frames_to_apk": 0,
            "events_to_apk": 0,
        }
        self.recent_chunk_sizes: deque = deque(maxlen=20)
        self._turn_seq = 0
        self._event_seq = 0

    def _ev(
        self,
        session_id: str,
        direction: str,
        kind: str,
        detail: str = "",
        size: int = 0,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._event_seq += 1
        entry = {
            "seq": self._event_seq,
            "t": time.time(),
            "t_ms": now_ms(),
            "session_id": (session_id or "")[:12],
            "direction": direction,
            "kind": kind,
            "detail": detail[:300],
            "size": size,
        }
        if extra:
            entry.update(extra)
        self.events.append(entry)
        if direction == "apk->server":
            self.counters["msgs_from_apk"] += 1
            self.counters["bytes_from_apk"] += size
        elif direction == "server->apk":
            self.counters["msgs_to_apk"] += 1
            self.counters["bytes_to_apk"] += size

    def session_open(
        self,
        session_id: str,
        device_id: str,
        transport: str,
        client: str,
        sample_rate: int = 48000,
    ) -> None:
        self.counters["sessions_total"] += 1
        self.counters["sessions_live"] = len(self.live) + 1
        self.live[session_id] = {
            "session_id": session_id,
            "device_id": device_id,
            "transport": transport,
            "client": client,
            "sample_rate": sample_rate,
            "opened_at": time.time(),
            "last_activity": time.time(),
            "bytes_from_apk": 0,
            "bytes_to_apk": 0,
            "audio_chunks_from_apk": 0,
            "audio_frames_to_apk": 0,
            "events_to_apk": 0,
            "state": "connected",
            "current_turn": None,
            "latency": {},
            "frames_in": 0,
            "frames_out": 0,
        }
        self._ev(
            session_id,
            "system",
            "session_open",
            f"{transport} device={device_id} client={client} rate={sample_rate}",
        )

    def session_close(self, session_id: str) -> None:
        info = self.live.pop(session_id, None)
        if info is None:
            return
        info["state"] = "closed"
        info["closed_at"] = time.time()
        info["duration_s"] = round(info["closed_at"] - info["opened_at"], 2)
        if info.get("current_turn"):
            self.turns.append(info["current_turn"])
            info["current_turn"] = None
        self.closed.append(info)
        self.counters["sessions_live"] = len(self.live)
        self._ev(session_id, "system", "session_close", f"duration={info['duration_s']}s")

    def touch(self, session_id: str) -> None:
        info = self.live.get(session_id)
        if info:
            info["last_activity"] = time.time()

    def bytes_from_apk(self, session_id: str, n: int) -> None:
        info = self.live.get(session_id)
        if info:
            info["bytes_from_apk"] += n
            info["audio_chunks_from_apk"] += 1
            info["frames_in"] += 1
            info["last_activity"] = time.time()
        self.counters["bytes_from_apk"] += n
        self.counters["audio_chunks_from_apk"] += 1
        self.recent_chunk_sizes.append(n)

    def audio_to_apk(self, session_id: str, n: int) -> None:
        info = self.live.get(session_id)
        if info:
            info["bytes_to_apk"] += n
            info["audio_frames_to_apk"] += 1
            info["frames_out"] += 1
            info["last_activity"] = time.time()
        self.counters["bytes_to_apk"] += n
        self.counters["audio_frames_to_apk"] += 1

    def msg_from_apk(self, session_id: str, kind: str, detail: str, size: int = 0) -> None:
        self.touch(session_id)
        self._ev(session_id, "apk->server", kind, detail, size)

    def msg_to_apk(self, session_id: str, kind: str, detail: str, size: int = 0) -> None:
        info = self.live.get(session_id)
        if info:
            info["events_to_apk"] += 1
            info["last_activity"] = time.time()
        self.counters["events_to_apk"] += 1
        self._ev(session_id, "server->apk", kind, detail, size)
        # events also carry byte size already counted by _ev

    def note_latency(self, session_id: str, metrics: Dict[str, float]) -> None:
        info = self.live.get(session_id)
        if info:
            info["latency"] = dict(metrics)

    def turn_started(self, session_id: str) -> Dict[str, Any]:
        self._turn_seq += 1
        turn = {
            "id": self._turn_seq,
            "session_id": (session_id or "")[:12],
            "started_ms": now_ms(),
            "started_at": time.time(),
            "question": "",
            "answer": "",
            "stages": {},
            "metrics": {},
            "done": False,
        }
        info = self.live.get(session_id)
        if info and info.get("current_turn") and not info["current_turn"].get("done"):
            prev = info["current_turn"]
            prev["done"] = True
            self.turns.append(prev)
        if info:
            info["current_turn"] = turn
        return turn

    def turn_stage(self, session_id: str, name: str, t_ms: Optional[float] = None) -> None:
        info = self.live.get(session_id)
        if not info or not info.get("current_turn"):
            return
        turn = info["current_turn"]
        turn["stages"][name] = round(t_ms if t_ms is not None else now_ms(), 1)

    def turn_content(self, session_id: str, question: str = "", answer: str = "") -> None:
        info = self.live.get(session_id)
        if not info or not info.get("current_turn"):
            return
        if question:
            info["current_turn"]["question"] = question
        if answer:
            info["current_turn"]["answer"] = answer

    def turn_finished(self, session_id: str, metrics: Dict[str, float]) -> None:
        info = self.live.get(session_id)
        if not info or not info.get("current_turn"):
            return
        turn = info["current_turn"]
        turn["metrics"] = dict(metrics)
        turn["stages"].setdefault("audio_sent", now_ms())
        stages = turn["stages"]

        def delta(a: str, b: str) -> Optional[float]:
            if a in stages and b in stages:
                return round(stages[b] - stages[a], 1)
            return None

        turn["breakdown"] = {
            "user_speak_ms": delta("speech_start", "speech_end"),
            "stt_ms": delta("speech_end", "stt_final"),
            "llm_wait_ms": delta("stt_final", "llm_start"),
            "llm_first_token_ms": delta("llm_start", "llm_first_token"),
            "llm_total_ms": delta("llm_start", "llm_end"),
            "tts_first_audio_ms": delta("tts_start", "tts_first_audio"),
            "tts_queue_to_wire_ms": delta("tts_first_audio", "audio_sent"),
            "server_total_ms": delta("speech_end", "audio_sent"),
            "end_to_end_ms": delta("speech_start", "audio_sent"),
        }
        if metrics:
            turn["server_metrics"] = dict(metrics)
        turn["done"] = True
        info["current_turn"] = None
        self.turns.append(turn)
        self.counters["turns_total"] += 1
        self._ev(
            session_id,
            "system",
            "turn_complete",
            f"turn#{turn['id']} total={turn['breakdown'].get('server_total_ms')}ms",
        )

    def snapshot(self) -> Dict[str, Any]:
        live = list(self.live.values())
        for s in live:
            s = dict(s)
            s["age_s"] = round(time.time() - s["opened_at"], 1)
            s["idle_s"] = round(time.time() - s["last_activity"], 1)
        recent_turns = list(self.turns)
        cur = []
        for s in self.live.values():
            if s.get("current_turn"):
                cur.append(dict(s["current_turn"]))
        turns_out = recent_turns[::-1] + cur
        live_out = []
        for s in self.live.values():
            live_out.append(
                {
                    **{k: v for k, v in s.items() if k != "current_turn"},
                    "current_turn": s.get("current_turn"),
                    "age_s": round(time.time() - s["opened_at"], 1),
                    "idle_s": round(time.time() - s["last_activity"], 1),
                }
            )
        return {
            "server_time": time.time(),
            "uptime_s": round(time.time() - self.started_at, 1),
            "counters": dict(self.counters),
            "recent_chunk_sizes": list(self.recent_chunk_sizes),
            "live_sessions": live_out,
            "closed_sessions": list(self.closed)[-10:][::-1],
            "turns": turns_out,
            "events": list(self.events)[::-1][:200],
        }


dashboard = DashboardStore()
