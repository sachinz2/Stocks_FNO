"""
Log management endpoints — protected by a secret token.

Pass the token as a query parameter:
  GET  /api/v1/logs/?token=<LOGS_API_TOKEN>
  GET  /api/v1/logs/tail?n=200&token=<LOGS_API_TOKEN>
  GET  /api/v1/logs/download/falcon.log?token=<LOGS_API_TOKEN>

Set LOGS_API_TOKEN in .env on the server. If the env var is empty or
unset the endpoints return 403 so the server is safe by default.
"""
import os
import secrets
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse

LOG_DIR = Path(os.environ.get("LOG_DIR", "/app/logs"))

router = APIRouter(prefix="/logs", tags=["logs"])


def _check_token(token: str) -> None:
    expected = os.environ.get("LOGS_API_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=403, detail="Logs API is disabled. Set LOGS_API_TOKEN in .env to enable.")
    if not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="Invalid token.")


@router.get("/")
async def list_logs(token: str = Query(..., description="LOGS_API_TOKEN from .env")):
    """List all log files with their sizes."""
    _check_token(token)
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
async def tail_log(
    token: str = Query(..., description="LOGS_API_TOKEN from .env"),
    n: int = Query(200, description="Number of lines to return"),
):
    """Return the last N lines of falcon.log."""
    _check_token(token)
    log_file = LOG_DIR / "falcon.log"
    if not log_file.exists():
        raise HTTPException(status_code=404, detail="Log file not found. Has the API written any logs yet?")

    with open(log_file, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()

    return "".join(lines[-n:])


@router.get("/download/{filename}")
async def download_log(
    filename: str,
    token: str = Query(..., description="LOGS_API_TOKEN from .env"),
):
    """Download a specific log file."""
    _check_token(token)

    # Prevent path traversal
    safe_name = Path(filename).name
    if safe_name != filename or not safe_name.startswith("falcon"):
        raise HTTPException(status_code=400, detail="Invalid filename.")

    log_file = LOG_DIR / safe_name
    if not log_file.exists():
        raise HTTPException(status_code=404, detail=f"{filename} not found.")

    return FileResponse(
        path=str(log_file),
        media_type="text/plain",
        filename=safe_name,
    )
