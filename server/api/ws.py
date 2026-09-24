import asyncio
import json
import time
from fractions import Fraction
from typing import Optional

from av import AudioFrame
from dashboard_store import dashboard
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from utils.logger import get_logger

log = get_logger("WS")

router = APIRouter()

SAMPLE_RATES = {8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000}
FRAME_BYTES = 1920  # 20ms @ 48kHz s16 mono — matches AIResponseAudioTrack
CHUNK_LOG_EVERY = 50


async def _pump_tts_to_ws(session) -> None:
    pending = bytearray()
    while session.websocket is not None:
        try:
            while True:
                try:
                    chunk = session.tts_audio_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if chunk:
                    pending += chunk
        except Exception:
            pass

        while len(pending) >= FRAME_BYTES:
            data = bytes(pending[:FRAME_BYTES])
            del pending[:FRAME_BYTES]
            session.audio_frames_sent += 1
            session.ai_playing = True
            session.send_audio_bytes(data)
            lat = session.latency
            if "tts_first_audio" in lat and "audio_sent" not in lat:
                session.mark_stage("audio_sent")
                session.emit_latency()

        if not session.tts_audio_queue.empty() or len(pending) >= FRAME_BYTES:
            continue
        session.ai_playing = bool(pending) or not session.tts_audio_queue.empty()
        await asyncio.sleep(0.02)


@router.websocket("/ws")
async def voice_websocket(
    websocket: WebSocket,
    device_id: str = Query(default="WS_DEVICE"),
    sample_rate: int = Query(default=48000),
) -> None:
    from pipeline.voice_pipeline import VoicePipeline
    from webrtc.session import session_manager

    await websocket.accept()
    if sample_rate not in SAMPLE_RATES:
        sample_rate = 48000

    session = session_manager.create(device_id)
    session.websocket = websocket
    session.state = "connected"
    session.audio_sample_rate = sample_rate
    client = f"{websocket.client.host}:{websocket.client.port}" if websocket.client else "?"
    dashboard.session_open(
        session.session_id,
        device_id,
        transport="websocket",
        client=client,
        sample_rate=sample_rate,
    )
    log.info(
        f"[WS] Connected device={device_id} session={session.session_id} "
        f"rate={sample_rate} client={websocket.client}"
    )

    pipeline = VoicePipeline(session)
    pump_task = asyncio.create_task(_pump_tts_to_ws(session), name=f"wspump:{session.session_id[:8]}")
    try:
        await pipeline.start()
    except Exception as exc:
        log.error(f"[WS] Pipeline start failed: {exc!r}")
        session.send_event({"type": "error", "message": f"Pipeline failed: {exc}"})

    pts = 0
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break

            data = message.get("bytes")
            if data:
                if len(data) < 4:
                    continue
                if len(data) % 2:
                    data = data[: len(data) - 1]
                frame = AudioFrame(format="s16", layout="mono", samples=len(data) // 2)
                frame.sample_rate = sample_rate
                frame.pts = pts
                frame.time_base = Fraction(1, sample_rate)
                frame.planes[0].update(data)
                pts += frame.samples
                session.audio_frames_received += 1
                session.touch()
                dashboard.bytes_from_apk(session.session_id, len(data))
                if session.audio_frames_received == 1:
                    dashboard.msg_from_apk(
                        session.session_id,
                        "audio_start",
                        f"first chunk {len(data)}B rate={sample_rate}Hz",
                    )
                elif session.audio_frames_received % CHUNK_LOG_EVERY == 0:
                    dashboard.msg_from_apk(
                        session.session_id,
                        "audio_progress",
                        f"chunk #{session.audio_frames_received} {len(data)}B "
                        f"total_in={dashboard.live.get(session.session_id, {}).get('bytes_from_apk', 0)}B",
                    )
                if session.pipeline is not None:
                    await session.audio_queue.put(frame)
                continue

            text = message.get("text")
            if text:
                try:
                    evt = json.loads(text)
                except json.JSONDecodeError:
                    dashboard.msg_from_apk(session.session_id, "bad_json", text[:200], len(text))
                    continue
                etype = evt.get("type", "unknown")
                dashboard.msg_from_apk(session.session_id, str(etype), text[:200], len(text))
                if etype == "ping":
                    pong = json.dumps({"type": "pong", "t": time.time()})
                    await websocket.send_text(pong)
                    dashboard.msg_to_apk(session.session_id, "pong", "t=" + str(time.time()), len(pong))
                elif etype == "stop":
                    break
    except WebSocketDisconnect:
        log.info(f"[WS] Client disconnected session={session.session_id}")
    except Exception as exc:
        log.error(f"[WS] Error session={session.session_id}: {exc!r}")
    finally:
        pump_task.cancel()
        try:
            await pump_task
        except (asyncio.CancelledError, Exception):
            pass
        await session_manager.close(session.session_id)
        log.info(
            f"[WS] Closed session={session.session_id} "
            f"in={session.audio_frames_received} out={session.audio_frames_sent}"
        )
