from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api import dashboard, health, webrtc, ws
from config import settings
from utils.logger import get_logger

log = get_logger("APP")

BASE_DIR = Path(__file__).resolve().parent
CLIENT_DIR = BASE_DIR.parent / "client"

app = FastAPI(title="AI Voice Support", version="0.1.0")

app.include_router(health.router)
app.include_router(webrtc.router)
app.include_router(webrtc.sessions_router)
app.include_router(ws.router)
app.include_router(dashboard.router)

if CLIENT_DIR.exists():
    app.mount("/client", StaticFiles(directory=str(CLIENT_DIR)), name="client")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(CLIENT_DIR / "test-client.html")


@app.on_event("startup")
async def on_startup() -> None:
    log.info("[APP] AI Voice Support starting")
    log.info(f"[APP] STUN: {settings.stun_urls}")
    log.info(f"[APP] TURN configured: {bool(settings.turn_url)}")
    log.info(f"[APP] Groq key set: {bool(settings.groq_api_key)}")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    from webrtc.session import session_manager

    for session in list(session_manager.list()):
        await session_manager.close(session.session_id)
    log.info("[APP] Shutdown complete")
