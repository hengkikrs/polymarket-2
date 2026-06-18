"""Logging setup shared by bot runtime and dashboard state snapshots."""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import core.config as config

log_buf: list[str] = []

# Logs land under <repo>/logs/ so the root stays focused on entrypoints.
_LOG_DIR = Path(__file__).resolve().parents[1] / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = _LOG_DIR / "bot.log"


class LogCapture(logging.Handler):
    def emit(self, record):
        log_buf.append(self.format(record))
        if len(log_buf) > 100:
            log_buf.pop(0)


def setup_logging() -> logging.Logger:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    max_bytes = int(float(os.getenv("LOG_MAX_BYTES", "5000000")))
    backup_count = int(float(os.getenv("LOG_BACKUP_COUNT", "3")))
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)-8s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            RotatingFileHandler(
                LOG_FILE,
                maxBytes=max(100000, max_bytes),
                backupCount=max(1, backup_count),
                encoding="utf-8",
            ),
        ],
    )
    root = logging.getLogger()
    if not any(isinstance(h, LogCapture) for h in root.handlers):
        handler = LogCapture()
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S"
        ))
        root.addHandler(handler)
    return logging.getLogger("bot")


log = setup_logging()
