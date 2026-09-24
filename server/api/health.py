import time

from fastapi import APIRouter

from webrtc.session import session_manager

router = APIRouter()

STARTED_AT = time.time()


@router.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "uptime_seconds": round(time.time() - STARTED_AT, 1),
        "sessions": session_manager.count(),
        "time": time.time(),
    }
