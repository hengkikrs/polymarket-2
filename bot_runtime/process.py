"""Single-instance process lock for the trading runtime."""
from __future__ import annotations

import os
from pathlib import Path

_INSTANCE_LOCK_FILE = None
_ROOT_DIR = Path(__file__).resolve().parents[1]
_DATA_DIR = _ROOT_DIR / "runtime_data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)


def acquire_instance_lock(log) -> bool:
    """Prevent two bot processes from trading the same account/window."""
    global _INSTANCE_LOCK_FILE
    lock_path = _DATA_DIR / "bot_instance.lock"
    f = open(lock_path, "a+", encoding="utf-8")
    try:
        f.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error("Another bot instance is already running; aborting this process.")
        f.close()
        return False
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    _INSTANCE_LOCK_FILE = f
    return True


def release_instance_lock() -> None:
    global _INSTANCE_LOCK_FILE
    if not _INSTANCE_LOCK_FILE:
        return
    try:
        if os.name == "nt":
            import msvcrt
            _INSTANCE_LOCK_FILE.seek(0)
            msvcrt.locking(_INSTANCE_LOCK_FILE.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(_INSTANCE_LOCK_FILE.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        _INSTANCE_LOCK_FILE.close()
    finally:
        _INSTANCE_LOCK_FILE = None
