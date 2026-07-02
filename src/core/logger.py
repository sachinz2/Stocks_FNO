import datetime
import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler

LOG_DIR = os.environ.get("LOG_DIR", "/app/logs")

_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


class _ISTFormatter(logging.Formatter):
    """Logging formatter that always stamps records in IST (UTC+5:30)."""

    def formatTime(self, record: logging.LogRecord, datefmt: str = None) -> str:
        dt = datetime.datetime.fromtimestamp(record.created, tz=_IST)
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S")


def setup_logging(log_level: str = "INFO"):
    level = getattr(logging, log_level.upper(), logging.INFO)

    formatter = _ISTFormatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    # Console handler — only if nothing is set up yet (uvicorn manages its own)
    if not root.handlers:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(formatter)
        root.addHandler(console)

    # File handler — always add if not already present.
    # Checked separately because uvicorn sets its own console handler first,
    # which causes `if not root.handlers` to skip the file handler entirely.
    has_file_handler = any(isinstance(h, TimedRotatingFileHandler) for h in root.handlers)
    if not has_file_handler:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            file_handler = TimedRotatingFileHandler(
                filename=os.path.join(LOG_DIR, "falcon.log"),
                when="midnight",
                interval=1,
                backupCount=30,       # keep 30 days of logs
                encoding="utf-8",
                utc=False,            # rotate at local midnight (IST on the server)
            )
            file_handler.suffix = "%Y-%m-%d"   # falcon.log.2026-06-24
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
            root.info(f"File logging active → {os.path.join(LOG_DIR, 'falcon.log')}")
        except Exception as e:
            root.warning(f"Could not set up file logging at {LOG_DIR}: {e}")

    # Silence noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("peewee").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
