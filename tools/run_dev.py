"""
run_dev.py — Development runner with auto-reload
=================================================
Watches .py and .env files. When any change is detected,
the bot (main.py) is automatically restarted.

Dashboard & Tracker run in separate processes and are also
restarted on code change.

Usage:
    python run_dev.py           (bot + dashboard + tracker)
    python run_dev.py --bot     (bot only, no dashboard/tracker)

Requires: pip install watchdog
"""
from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
import time
import threading
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_IMPORT_ERROR: ImportError | None = None
except ImportError as exc:
    Observer = None
    FileSystemEventHandler = object
    WATCHDOG_IMPORT_ERROR = exc


# ── Config ──────────────────────────────────────────────────────
WATCH_DIR = Path(__file__).resolve().parent.parent  # repo root
WATCH_EXTENSIONS = {".py", ".env"}
IGNORE_PATTERNS = {"__pycache__", ".git", "node_modules", ".state_"}
DEBOUNCE_SECS = 1.5        # tunggu 1.5s setelah change terakhir sebelum restart
BOT_SCRIPT = "main.py"
DASHBOARD_MODULE = "web.dashboard"
TRACKER_MODULE = "analysis.tracker"
SUPERVISOR_LOCK = WATCH_DIR / "runtime_data" / "dev_supervisor.lock"


class SupervisorLock:
    """Prevent multiple dev supervisors from fighting over child processes."""

    def __init__(self, path: Path = SUPERVISOR_LOCK):
        self.path = path
        self.pid = os.getpid()
        self.owned = False

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            try:
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except OSError:
                return False
            for line in result.stdout.splitlines():
                fields = [field.strip('"') for field in line.split('","')]
                if len(fields) > 1 and fields[1] == str(pid):
                    return True
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def acquire(self) -> tuple[bool, int]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                try:
                    existing_pid = int(self.path.read_text(encoding="ascii").strip())
                except (OSError, ValueError):
                    existing_pid = 0
                if self._pid_alive(existing_pid):
                    return False, existing_pid
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue

            with os.fdopen(fd, "w", encoding="ascii") as lock_file:
                lock_file.write(str(self.pid))
            self.owned = True
            return True, self.pid
        return False, 0

    def release(self):
        if not self.owned:
            return
        try:
            lock_pid = int(self.path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            lock_pid = 0
        if lock_pid == self.pid:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self.owned = False


class ChangeHandler(FileSystemEventHandler):
    """Detect .py / .env file changes with debounce."""

    def __init__(self, callback):
        super().__init__()
        self._callback = callback
        self._last_trigger = 0.0
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def _should_watch(self, path: str) -> bool:
        p = Path(path)
        # Skip ignored dirs
        for part in p.parts:
            if any(ig in part for ig in IGNORE_PATTERNS):
                return False
        # Only watch specific extensions
        return p.suffix in WATCH_EXTENSIONS

    def on_modified(self, event):
        if event.is_directory:
            return
        if self._should_watch(event.src_path):
            self._schedule_restart(event.src_path)

    def on_created(self, event):
        if event.is_directory:
            return
        if self._should_watch(event.src_path):
            self._schedule_restart(event.src_path)

    def _schedule_restart(self, path: str):
        """Debounce: tunggu DEBOUNCE_SECS setelah change terakhir."""
        with self._lock:
            if self._timer:
                self._timer.cancel()
            rel = os.path.relpath(path, WATCH_DIR)
            self._timer = threading.Timer(
                DEBOUNCE_SECS,
                self._do_restart,
                args=(rel,),
            )
            self._timer.daemon = True
            self._timer.start()

    def _do_restart(self, changed_file: str):
        print(f"\n{'='*60}")
        print(f"  🔄 File berubah: {changed_file}")
        print(f"  Restarting in {DEBOUNCE_SECS}s...")
        print(f"{'='*60}\n")
        self._callback(changed_file)


class ProcessManager:
    """Manage bot, dashboard, and tracker subprocesses."""

    def __init__(self, bot_only: bool = False):
        self.bot_only = bot_only
        self.bot_proc: subprocess.Popen | None = None
        self.dash_proc: subprocess.Popen | None = None
        self.tracker_proc: subprocess.Popen | None = None
        self._stopping = False
        self._cleaned_stale = False

    def start_all(self):
        """Start all processes."""
        self._stopping = False
        if not self._cleaned_stale:
            self._cleanup_stale_dev_processes()
            self._cleaned_stale = True
        cwd = str(WATCH_DIR)

        # Bot
        print(f"[DEV] Starting {BOT_SCRIPT}...")
        self.bot_proc = subprocess.Popen(
            [sys.executable, "-u", str(WATCH_DIR / BOT_SCRIPT)],
            cwd=cwd,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )

        if not self.bot_only:
            # Dashboard
            print(f"[DEV] Starting {DASHBOARD_MODULE}...")
            self.dash_proc = subprocess.Popen(
                [sys.executable, "-u", "-m", DASHBOARD_MODULE],
                cwd=cwd,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
            # Tracker
            print(f"[DEV] Starting {TRACKER_MODULE}...")
            self.tracker_proc = subprocess.Popen(
                [sys.executable, "-u", "-m", TRACKER_MODULE],
                cwd=cwd,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )

        print()
        print("=" * 60)
        print("  🟢 DEV MODE — Auto-reload aktif")
        if not self.bot_only:
            print("  Dashboard : http://localhost:5004")
            print("  Tracker   : http://localhost:5005")
        print("  Bot PID   :", self.bot_proc.pid if self.bot_proc else "-")
        print("  Watching  : *.py, .env")
        print("  Ctrl+C    : Stop semua")
        print("=" * 60)
        print()

    def _taskkill(self, pid: int, reason: str):
        if pid <= 0 or pid == os.getpid():
            return
        print(f"[DEV] Cleaning stale process PID {pid} ({reason})...")
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F", "/T"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    def _powershell_lines(self, command: str) -> list[str]:
        if os.name != "nt":
            return []
        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", command],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            return [line.strip() for line in out.splitlines() if line.strip()]
        except (subprocess.SubprocessError, OSError):
            return []

    def _python_script_pids(self, script: str) -> list[int]:
        """Return running Python PIDs for a repo script, excluding this runner."""
        if os.name != "nt":
            return []
        script_path = str((WATCH_DIR / script).resolve()).replace("'", "''")
        cmd = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -like 'python*' -and "
            f"$_.CommandLine -like '*{script_path}*' }} | "
            "Select-Object -ExpandProperty ProcessId"
        )
        pids: list[int] = []
        for line in self._powershell_lines(cmd):
            try:
                pid = int(line)
            except ValueError:
                continue
            if pid != os.getpid():
                pids.append(pid)
        return pids

    def _free_ports(self, ports: list[int]):
        """Find and terminate any processes holding the specified ports (except ourselves)."""
        if os.name != "nt":
            return
        pids = []
        try:
            out = subprocess.check_output(["netstat", "-ano"], text=True, stderr=subprocess.DEVNULL)
            for line in out.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split()
                if len(parts) >= 5 and parts[0].upper() in ("TCP", "UDP"):
                    local_address = parts[1]
                    try:
                        port_str = local_address.split(":")[-1]
                        port = int(port_str)
                        if port in ports:
                            pid = int(parts[-1])
                            if pid > 0 and pid != os.getpid():
                                pids.append(pid)
                    except (ValueError, IndexError):
                        continue
        except Exception:
            pass

        for pid in set(pids):
            self._taskkill(pid, f"blocking port {ports}")

    def _cleanup_stale_dev_processes(self):
        """Remove stale bot/dashboard/tracker left by a previous dev run."""
        if os.name != "nt":
            return

        # Bot: use the single-instance lock file as source of truth.
        lock_path = WATCH_DIR / "runtime_data" / "bot_instance.lock"
        if lock_path.exists():
            try:
                pid = int(lock_path.read_text(encoding="utf-8").strip() or "0")
            except (ValueError, OSError):
                pid = 0
            if pid > 0:
                cmd = (
                    f"$p=Get-CimInstance Win32_Process -Filter \"ProcessId = {pid}\";"
                    "$p.CommandLine"
                )
                command_line = " ".join(self._powershell_lines(cmd))
                if "main.py" in command_line:
                    self._taskkill(pid, "old main.py lock holder")

        # Fallback: find only this repo's Python main.py process.
        for pid in self._python_script_pids(BOT_SCRIPT):
            self._taskkill(pid, "old repo main.py process")

        # Dashboard/tracker: free their fixed dev ports before binding.
        self._free_ports([5004, 5005])

        time.sleep(0.5)

    def stop_all(self):
        """Gracefully stop all processes."""
        self._stopping = True
        for name, proc in [
            ("Bot", self.bot_proc),
            ("Dashboard", self.dash_proc),
            ("Tracker", self.tracker_proc),
        ]:
            if proc and proc.poll() is None:
                print(f"[DEV] Stopping {name} (PID {proc.pid})...")
                try:
                    if os.name == "nt":
                        # Windows: send CTRL_BREAK_EVENT to process group
                        proc.send_signal(signal.CTRL_BREAK_EVENT)
                    else:
                        proc.terminate()
                    proc.wait(timeout=5)
                except (subprocess.TimeoutExpired, OSError):
                    print(f"[DEV] Force-killing {name}...")
                    proc.kill()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass

        self.bot_proc = None
        self.dash_proc = None
        self.tracker_proc = None

    def restart_all(self, changed_file: str = ""):
        """Stop then start all processes."""
        if self._stopping:
            return
        print(f"[DEV] ♻️  Restarting semua proses...")
        self.stop_all()
        time.sleep(0.5)
        self.start_all()

    def restart_bot_only(self, changed_file: str = ""):
        """Restart only the bot, keep dashboard/tracker running."""
        if self._stopping:
            return
        # File path heuristics: a change inside web/ rebuilds dashboard,
        # change inside analysis/tracker rebuilds tracker, otherwise bot.
        norm = changed_file.replace("\\", "/").lower()
        if any(part in norm for part in (
            "/core/",
            "/strategies/",
            "/bot_runtime/end_window_runner",
        )):
            self.restart_all(changed_file)
            return
        if "web/dashboard" in norm and self.dash_proc:
            print("[DEV] ♻️  Restarting Dashboard...")
            self._restart_proc("dash_proc", module=DASHBOARD_MODULE)
            return
        if "analysis/tracker" in norm and self.tracker_proc:
            print("[DEV] ♻️  Restarting Tracker...")
            self._restart_proc("tracker_proc", module=TRACKER_MODULE)
            return

        # Default: restart bot (covers .env, strategy_*.py, main.py, etc.)
        print(f"[DEV] ♻️  Restarting Bot (karena {changed_file})...")
        self._restart_proc("bot_proc", script=BOT_SCRIPT)

    def _restart_proc(self, attr: str, *, script: str | None = None,
                      module: str | None = None):
        proc = getattr(self, attr, None)
        if proc and proc.poll() is None:
            try:
                if os.name == "nt":
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    proc.terminate()
                proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass

        if script == BOT_SCRIPT:
            current_pid = proc.pid if proc else 0
            for pid in self._python_script_pids(BOT_SCRIPT):
                if pid != current_pid:
                    self._taskkill(pid, "existing main.py before restart")

        # Clean up ports if we are starting dashboard or tracker to prevent binding errors
        if module == DASHBOARD_MODULE:
            self._free_ports([5004])
        elif module == TRACKER_MODULE:
            self._free_ports([5005])

        time.sleep(0.3)
        if module:
            cmd = [sys.executable, "-u", "-m", module]
            label = module
        else:
            cmd = [sys.executable, "-u", str(WATCH_DIR / script)]
            label = script
        new_proc = subprocess.Popen(
            cmd,
            cwd=str(WATCH_DIR),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        setattr(self, attr, new_proc)
        print(f"[DEV] ✅ {label} restarted (PID {new_proc.pid})")


def main():
    if WATCHDOG_IMPORT_ERROR is not None:
        print("=" * 60)
        print("  watchdog belum terinstall. Jalankan:")
        print("  pip install watchdog")
        print("=" * 60)
        sys.exit(1)
    bot_only = "--bot" in sys.argv
    supervisor_lock = SupervisorLock()
    acquired, existing_pid = supervisor_lock.acquire()
    if not acquired:
        print(
            f"[DEV] Supervisor already running (PID {existing_pid}); "
            "aborting duplicate."
        )
        return
    atexit.register(supervisor_lock.release)

    print()
    print("=" * 60)
    print("  🛠️  BTC Polymarket Bot — DEV MODE (Auto-Reload)")
    print("=" * 60)
    print()

    pm = ProcessManager(bot_only=bot_only)
    pm.start_all()

    # Smart restart: hanya restart proses yang relevan
    handler = ChangeHandler(callback=pm.restart_bot_only)
    observer = Observer()
    observer.schedule(handler, str(WATCH_DIR), recursive=True)
    observer.start()

    try:
        while True:
            # Check if bot exited without file change. Keep dev mode alive;
            # otherwise the dashboard can stay open while the bot is dead.
            if pm.bot_proc and pm.bot_proc.poll() is not None:
                exit_code = pm.bot_proc.returncode
                live_main = pm._python_script_pids(BOT_SCRIPT)
                if live_main:
                    print(f"\n[DEV] Bot child exited (exit code {exit_code}), "
                          f"but main.py is already running (PID {live_main[0]}). "
                          "Skip auto-restart.")
                    pm.bot_proc = None
                    time.sleep(0.5)
                    continue
                print(f"\n[DEV] Bot exited (exit code {exit_code}). "
                      f"Auto-restarting in 3s...")
                time.sleep(3)
                if not pm._stopping:
                    pm._restart_proc("bot_proc", script=BOT_SCRIPT)
            if not pm.bot_only:
                for attr, module, label in (
                    ("dash_proc", DASHBOARD_MODULE, "Dashboard"),
                    ("tracker_proc", TRACKER_MODULE, "Tracker"),
                ):
                    proc = getattr(pm, attr)
                    if proc and proc.poll() is not None:
                        print(
                            f"\n[DEV] {label} exited (exit code {proc.returncode}). "
                            "Auto-restarting in 2s..."
                        )
                        time.sleep(2)
                        if not pm._stopping:
                            pm._restart_proc(attr, module=module)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[DEV] Ctrl+C — stopping...")
    finally:
        observer.stop()
        observer.join(timeout=3)
        pm.stop_all()
        supervisor_lock.release()
        print("[DEV] Semua proses dihentikan. Bye!")


if __name__ == "__main__":
    main()
