import logging
import os
import sys
from logging.handlers import RotatingFileHandler

LOG_DIR = os.environ.get("LOG_DIR", "/app/logs")


def setup_logging(log_level: str = "INFO"):
    level = getattr(logging, log_level.upper(), logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    if not root.handlers:
        # Console handler
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(formatter)
        root.addHandler(console)

        # Rotating file handler — 10 MB per file, keep 7 days of files
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            file_handler = RotatingFileHandler(
                filename=os.path.join(LOG_DIR, "falcon.log"),
                maxBytes=10 * 1024 * 1024,   # 10 MB
                backupCount=7,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except Exception as e:
            root.warning(f"Could not set up file logging at {LOG_DIR}: {e}")

    # Silence noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
