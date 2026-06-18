"""Trading strategy registry."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _b(name: str, default: str = "false") -> bool:
    return str(os.getenv(name, default)).strip().lower() in {"true", "1", "yes", "on"}


def enabled_strategies() -> frozenset[str]:
    """Return enabled strategy short names. Reads env on every call."""
    out: set[str] = set()
    mode = {p.strip().upper() for p in os.getenv("STRATEGY_MODE", "END_WINDOW").replace("+", ",").split(",")}
    if _b("END_WINDOW_ENABLED", "true") and ("END_WINDOW" in mode or "ALL" in mode):
        out.add("END_WINDOW")
    return frozenset(out)


__all__ = ["enabled_strategies"]
