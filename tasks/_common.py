"""Shared helpers for task runners (unattended entrypoints run by launchd)."""

import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = _ROOT / "logs"


def setup_logger(task_name: str) -> logging.Logger:
    LOGS_DIR.mkdir(exist_ok=True)
    log_path = LOGS_DIR / f"{task_name}.log"

    logger = logging.getLogger(task_name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    # Rotate so the always-on chat server's log can't grow without bound.
    file_handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    return logger


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")
