from typing import Optional

from aiortc import RTCSessionDescription
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from utils.logger import get_logger
from webrtc.peer import create_peer_connection
from webrtc.session import session_manager

log = get_logger("WEBRTC")

router = APIRouter(prefix="/webrtc", tags=["webrtc"])
sessions_router = APIRouter(prefix="/sessions", tags=["sessions"])


class OfferRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    sdp: str = Field(min_length=1)
    type: str = "offer"


class OfferResponse(BaseModel):
    session_id: str
    sdp: str
    type: str


@router.post("/offer", response_model=OfferResponse)
async def create_offer(request: OfferRequest) -> OfferResponse:
    if request.type != "offer":
        raise HTTPException(status_code=400, detail="Only SDP type 'offer' is supported")

    session = session_manager.create(request.device_id)
    try:
        pc = create_peer_connection(session)
        await pc.setRemoteDescription(RTCSessionDescription(sdp=request.sdp, type=request.type))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        local = pc.localDescription
        session.state = "connected"
        log.info(f"[WEBRTC] Device connected: {session.device_id} session={session.session_id}")
        return OfferResponse(session_id=session.session_id, sdp=local.sdp, type=local.type)
    except Exception as exc:
        log.error(f"[WEBRTC] Offer negotiation failed for {request.device_id}: {exc}")
        await session_manager.close(session.session_id)
        raise HTTPException(status_code=400, detail=f"WebRTC negotiation failed: {exc}") from exc


@sessions_router.get("/{session_id}")
async def get_session(session_id: str) -> dict:
    session = session_manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session.snapshot()


@sessions_router.delete("/{session_id}")
async def delete_session(session_id: str) -> dict:
    closed = await session_manager.close(session_id)
    if not closed:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": "closed", "session_id": session_id}
