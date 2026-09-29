import asyncio
import time
from typing import List

from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection
from aiortc.mediastreams import MediaStreamError

from config import settings
from utils.logger import get_logger
from webrtc.audio_track import AIResponseAudioTrack
from webrtc.session import Session, session_manager

log = get_logger("WEBRTC")

AUDIO_FRAME_LOG_EVERY = 500
HEARTBEAT_SECONDS = 5.0


def build_ice_servers() -> List[RTCIceServer]:
    servers: List[RTCIceServer] = []
    if settings.stun_urls:
        servers.append(RTCIceServer(urls=settings.stun_urls))
    turn_urls = settings.turn_urls
    if turn_urls:
        if settings.turn_username and settings.turn_credential:
            servers.append(
                RTCIceServer(
                    urls=turn_urls,
                    username=settings.turn_username,
                    credential=settings.turn_credential,
                )
            )
        else:
            servers.append(RTCIceServer(urls=turn_urls))
        log.info(f"[WEBRTC] ICE: STUN + TURN ({turn_urls})")
    else:
        log.info("[WEBRTC] ICE: STUN only (set TURN_URL for production/cellular)")
    return servers


async def _consume_audio_track(track, session: Session) -> None:
    log.info("[WEBRTC] Audio track received")
    count = 0
    last_heartbeat = time.monotonic()
    try:
        while True:
            frame = await track.recv()
            count += 1
            session.audio_frames_received = count
            session.touch()
            if count == 1:
                channels = len(frame.layout.channels)
                log.info(
                    f"[WEBRTC] First audio frame: {frame.format.name} "
                    f"{frame.sample_rate}Hz ch={channels}"
                )
            now = time.monotonic()
            if now - last_heartbeat >= HEARTBEAT_SECONDS:
                last_heartbeat = now
                vad = session.pipeline.vad if session.pipeline is not None else None
                vad_state = "speech" if vad is not None and vad.is_speech_active else "idle"
                rms = f"{vad.last_rms:.4f}" if vad is not None else "n/a"
                log.info(
                    f"[WEBRTC] Audio heartbeat: frames_in={count} "
                    f"vad={vad_state} rms={rms} "
                    f"out={session.audio_frames_sent}"
                )
            if count % AUDIO_FRAME_LOG_EVERY == 0:
                log.info(f"[WEBRTC] Audio frames received: {count}")
            if session.pipeline is not None:
                await session.audio_queue.put(frame)
    except asyncio.CancelledError:
        raise
    except MediaStreamError:
        log.info("[WEBRTC] Remote track ended")
    except Exception as exc:
        log.error(f"[WEBRTC] Audio track error: {exc!r}")
    finally:
        log.info(f"[WEBRTC] Audio track ended. Total frames received: {count}")


def create_peer_connection(session: Session) -> RTCPeerConnection:
    configuration = RTCConfiguration(iceServers=build_ice_servers())
    pc = RTCPeerConnection(configuration)
    session.peer_connection = pc

    if session.ai_track is None:
        session.ai_track = AIResponseAudioTrack(session)
    pc.addTrack(session.ai_track)

    @pc.on("connectionstatechange")
    async def on_connectionstatechange() -> None:
        state = pc.connectionState
        session.state = state
        log.info(f"[WEBRTC] Connection state [{session.device_id}]: {state}")
        if state == "connected" and session.pipeline is None:
            from pipeline.voice_pipeline import VoicePipeline

            try:
                pipeline = VoicePipeline(session)
                await pipeline.start()
            except Exception as exc:
                log.error(f"[PIPELINE] Start failed: {exc!r}")
                session.send_event({"type": "error", "message": f"Pipeline failed: {exc}"})
        elif state in ("failed", "closed"):
            await session_manager.close(session.session_id)

    @pc.on("iceconnectionstatechange")
    async def on_iceconnectionstatechange() -> None:
        log.info(f"[WEBRTC] ICE state [{session.device_id}]: {pc.iceConnectionState}")

    @pc.on("icegatheringstatechange")
    async def on_icegatheringstatechange() -> None:
        log.debug(f"[WEBRTC] ICE gathering [{session.device_id}]: {pc.iceGatheringState}")

    @pc.on("datachannel")
    def on_datachannel(channel) -> None:
        session.data_channel = channel
        log.info(f"[WEBRTC] Data channel received: {channel.label}")

        @channel.on("open")
        def on_open() -> None:
            log.info(f"[WEBRTC] Data channel open: {channel.label}")

        @channel.on("message")
        def on_message(message) -> None:
            session.touch()
            log.debug(f"[WEBRTC] Data channel message: {message}")

        @channel.on("error")
        def on_error(error) -> None:
            log.error(f"[WEBRTC] Data channel error: {error}")

    @pc.on("track")
    def on_track(track) -> None:
        session.audio_track = track
        session.touch()
        log.info(f"[WEBRTC] Track added: kind={track.kind} id={track.id}")
        if track.kind == "audio":
            asyncio.create_task(_consume_audio_track(track, session))

        @track.on("ended")
        async def on_ended() -> None:
            log.info("[WEBRTC] Remote audio track ended")

    @pc.on("trackended")
    def on_trackended() -> None:
        log.info("[WEBRTC] Track ended")

    return pc
