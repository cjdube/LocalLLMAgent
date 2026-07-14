"""Shared helpers for task runners (unattended entrypoints run by launchd)."""

import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from agent.tools.notify import notify

_ROOT = Path(__file__).resolve().parent.parent
# Unset in production, so this is the real logs/. The test suite sets it (see
# tests/conftest.py) because it is the only redirect that survives into a child
# interpreter — monkeypatching this module's attribute can't cross a subprocess.
LOGS_DIR = Path(os.getenv("WREN_LOGS_DIR") or _ROOT / "logs")


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


def notify_failure(task_name: str, detail: object, logger: logging.Logger = None) -> None:
    """Push a one-line failure alert for a scheduled task (best-effort).

    Swallows any error from the push itself so an ntfy outage can never mask
    the original task failure — the failure is already logged by the caller."""
    try:
        result = notify(
            message=f"{task_name} failed: {detail}",
            title=f"Wren: {task_name} failed",
            priority="high",
        )
        if logger and result.get("error"):
            logger.warning(f"Failure push via ntfy did not send: {result['error']}")
    except Exception:
        if logger:
            logger.exception("notify_failure raised while sending the failure push")
