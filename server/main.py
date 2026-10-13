import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from aioice_compat import install_aioice_consent_grace, install_aioice_retry_guard
from utils.console import disable_quick_edit

# A click in the console window would otherwise freeze the server until a key is pressed.
disable_quick_edit()
install_aioice_retry_guard()

# Import config before anything that logs so .env (dotenv) is loaded prior
# to the first configure_logging() call.
from config import settings  # noqa: E402

# Reads ICE_CONSENT_FAILURES from .env - must run after the config import.
install_aioice_consent_grace()

from api import dashboard, health, recordings, webrtc, ws  # noqa: E402
from utils.logger import get_logger, ice_debug_enabled  # noqa: E402

log = get_logger("APP")

BASE_DIR = Path(__file__).resolve().parent
CLIENT_DIR = BASE_DIR.parent / "client"

app = FastAPI(title="AI Voice Support", version="0.1.0")

app.include_router(health.router)
app.include_router(webrtc.router)
app.include_router(webrtc.sessions_router)
app.include_router(ws.router)
app.include_router(dashboard.router)
app.include_router(recordings.router)

if CLIENT_DIR.exists():
    app.mount("/client", StaticFiles(directory=str(CLIENT_DIR)), name="client")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(CLIENT_DIR / "test-client.html")


@app.on_event("startup")
async def on_startup() -> None:
    log.info("[APP] Voice echo server starting")
    log.info(f"[APP] STUN: {settings.stun_urls}")
    log.info(f"[APP] TURN configured: {bool(settings.turn_url)}")
    if ice_debug_enabled():
        log.info(
            "[APP] ICE debug ON: tracing STUN in/out and consent "
            "(look for 'Consent to send expired' or 400 replies before a drop)"
        )
    from utils.clock import check_clock

    asyncio.create_task(check_clock(settings.ntp_check_server))


@app.on_event("shutdown")
async def on_shutdown() -> None:
    from webrtc.session import session_manager

    for session in list(session_manager.list()):
        await session_manager.close(session.session_id)
    log.info("[APP] Shutdown complete")
