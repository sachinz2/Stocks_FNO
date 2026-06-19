"""
Log management endpoints.

GET  /api/v1/logs/              — list available log files with sizes
GET  /api/v1/logs/tail?n=200   — last N lines of the active log (default 200)
GET  /api/v1/logs/download/{filename} — download a log file
"""
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

LOG_DIR = Path(os.environ.get("LOG_DIR", "/app/logs"))

router = APIRouter(prefix="/logs", tags=["logs"])


@router.get("/")
async def list_logs():
    """List all log files with their sizes."""
    if not LOG_DIR.exists():
        return {"log_dir": str(LOG_DIR), "files": []}

    files = []
    for f in sorted(LOG_DIR.glob("falcon*.log*")):
        stat = f.stat()
        files.append({
            "filename": f.name,
            "size_bytes": stat.st_size,
            "size_kb": round(stat.st_size / 1024, 1),
            "modified": stat.st_mtime,
        })
    return {"log_dir": str(LOG_DIR), "files": files}


@router.get("/tail", response_class=PlainTextResponse)
async def tail_log(n: int = 200):
    """Return the last N lines of falcon.log."""
    log_file = LOG_DIR / "falcon.log"
    if not log_file.exists():
        raise HTTPException(status_code=404, detail="Log file not found. Has the API written any logs yet?")

    with open(log_file, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()

    return "".join(lines[-n:])


@router.get("/download/{filename}")
async def download_log(filename: str):
    """Download a specific log file."""
    # Prevent path traversal
    safe_name = Path(filename).name
    if safe_name != filename or not safe_name.startswith("falcon"):
        raise HTTPException(status_code=400, detail="Invalid filename")

    log_file = LOG_DIR / safe_name
    if not log_file.exists():
        raise HTTPException(status_code=404, detail=f"{filename} not found")

    return FileResponse(
        path=str(log_file),
        media_type="text/plain",
        filename=safe_name,
    )
