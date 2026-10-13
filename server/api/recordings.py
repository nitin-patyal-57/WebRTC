from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from audio.recorder import RECORDINGS_DIR

router = APIRouter()


@router.get("/recordings")
async def list_recordings() -> dict:
    """Newest first; open /recordings/<name> in a browser to listen."""
    files = sorted(RECORDINGS_DIR.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    return {
        "recordings": [
            {"name": p.name, "bytes": p.stat().st_size, "url": f"/recordings/{p.name}"}
            for p in files
        ]
    }


@router.get("/recordings/{name}")
async def get_recording(name: str) -> FileResponse:
    path = (RECORDINGS_DIR / name).resolve()
    if path.parent != RECORDINGS_DIR.resolve() or path.suffix != ".wav" or not path.is_file():
        raise HTTPException(status_code=404, detail="recording not found")
    return FileResponse(path, media_type="audio/wav")
