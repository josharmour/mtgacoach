"""Lightweight launcher for MTGA Coach with auto-restart support.

This launcher:
- Runs the TUI in a subprocess so it can be cleanly restarted
- Detects restart requests and relaunches automatically
- Can optionally watch for code changes during development
- Handles zombie process cleanup properly

Usage from command line:
    python launcher.py
    python launcher.py --watch  # Auto-restart on .py file changes

The desktop shortcut should point to coach.bat which calls this.
"""

import os
import sys
import time
import signal
import subprocess
import json
import ctypes
from pathlib import Path

# Constants
REPO_DIR = Path(__file__).parent
SRC_DIR = REPO_DIR / "src" / "arenamcp"
RESTART_EXIT_CODE = 42  # Special exit code meaning "please restart"
WATCH_EXTENSIONS = {".py"}
WATCH_DEBOUNCE_MS = 500
LOCK_DIR = Path.home() / ".arenamcp"
LOCK_FILE = LOCK_DIR / "launcher.lock"
DEFAULT_PLAYER_LOG_MAX_MB = 64
DEFAULT_PLAYER_LOG_KEEP_MB = 8


class SingleInstanceGuard:
    """Prevent multiple launcher instances from running concurrently."""

    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self._handle = None

    def acquire(self) -> bool:
        """Acquire the singleton lock for this launcher process."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.lock_path, "a+", encoding="utf-8")

        try:
            self._lock_handle()
        except OSError:
            self._close_handle()
            return False

        self._write_owner_metadata()
        return True

    def release(self) -> None:
        """Release the singleton lock."""
        if self._handle is None:
            return

        try:
            self._unlock_handle()
        except OSError:
            pass
        finally:
            self._close_handle()

    def describe_owner(self) -> str | None:
        """Return a short description of the current lock owner if readable."""
        try:
            payload = json.loads(self.lock_path.read_text(encoding="utf-8") or "{}")
        except Exception:
            return None

        pid = payload.get("pid")
        started = payload.get("started_at")
        if pid and started:
            return f"PID {pid} (started {started})"
        if pid:
            return f"PID {pid}"
        return None

    def _write_owner_metadata(self) -> None:
        if self._handle is None:
            return

        payload = {
            "pid": os.getpid(),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "cwd": str(REPO_DIR),
        }
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(json.dumps(payload))
        self._handle.flush()

    def _lock_handle(self) -> None:
        assert self._handle is not None
        self._handle.seek(0)

        if os.name == "nt":
            import msvcrt

            file_size = self._handle.seek(0, os.SEEK_END)
            if file_size == 0:
                self._handle.write("\0")
                self._handle.flush()
            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            self._handle.seek(0)
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_handle(self) -> None:
        assert self._handle is not None

        if os.name == "nt":
            import msvcrt

            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)

    def _close_handle(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def notify_already_running(owner: str | None = None) -> None:
    """Show a visible warning when a second launcher is started."""
    message = "ArenaMCP is already running."
    if owner:
        message += f"\n\nExisting launcher: {owner}"
    message += "\n\nClose the existing window first."

    print(message)
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(None, message, "ArenaMCP", 0x30)
        except Exception:
            pass


def get_python_executable():
    """Get the Python executable path."""
    return sys.executable


def get_mtga_log_path() -> Path:
    """Best-effort MTGA Player.log path for launcher maintenance."""
    custom = os.environ.get("MTGA_LOG_PATH")
    if custom:
        return Path(os.path.expandvars(os.path.expanduser(custom)))

    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if local_appdata:
        return (
            Path(local_appdata).parent
            / "LocalLow"
            / "Wizards Of The Coast"
            / "MTGA"
            / "Player.log"
        )

    userprofile = os.environ.get("USERPROFILE", "")
    if userprofile:
        return (
            Path(userprofile)
            / "AppData"
            / "LocalLow"
            / "Wizards Of The Coast"
            / "MTGA"
            / "Player.log"
        )

    return (
        Path.home()
        / "AppData"
        / "LocalLow"
        / "Wizards Of The Coast"
        / "MTGA"
        / "Player.log"
    )


def is_mtga_running() -> bool:
    """Return True when MTGA.exe appears to be running."""
    if os.name != "nt":
        return False

    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq MTGA.exe"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return False

    output = f"{result.stdout}\n{result.stderr}"
    return "MTGA.exe" in output


def _env_mb(name: str, default_mb: int) -> int:
    """Read a positive integer megabyte value from the environment."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default_mb
    try:
        value = int(raw)
    except ValueError:
        return default_mb
    return max(0, value)


def trim_player_log_if_needed(
    log_path: Path | None = None,
    *,
    max_mb: int | None = None,
    keep_mb: int | None = None,
) -> str:
    """Trim Player.log before launch when it has grown too large.

    Returns a short status string for launcher logging.
    """
    if os.environ.get("ARENAMCP_TRIM_PLAYER_LOG", "1").strip().lower() in {"0", "false", "no"}:
        return "disabled"

    if is_mtga_running():
        return "skipped: MTGA is running"

    log_path = log_path or get_mtga_log_path()
    max_mb = _env_mb("ARENAMCP_PLAYER_LOG_MAX_MB", DEFAULT_PLAYER_LOG_MAX_MB) if max_mb is None else max_mb
    keep_mb = _env_mb("ARENAMCP_PLAYER_LOG_KEEP_MB", DEFAULT_PLAYER_LOG_KEEP_MB) if keep_mb is None else keep_mb

    if max_mb <= 0 or keep_mb <= 0:
        return "disabled"

    try:
        if not log_path.exists():
            return "skipped: log missing"

        file_size = log_path.stat().st_size
        max_bytes = max_mb * 1024 * 1024
        keep_bytes = keep_mb * 1024 * 1024

        if file_size <= max_bytes:
            return f"skipped: {file_size // (1024 * 1024)}MB <= {max_mb}MB"

        keep_bytes = min(keep_bytes, file_size)
        start_offset = file_size - keep_bytes
        temp_path = log_path.with_name(log_path.name + ".trimtmp")

        with open(log_path, "rb") as src:
            src.seek(start_offset)
            tail = src.read()

        if start_offset > 0:
            newline_idx = tail.find(b"\n")
            if newline_idx != -1 and newline_idx + 1 < len(tail):
                tail = tail[newline_idx + 1 :]

        with open(temp_path, "wb") as dst:
            dst.write(tail)
            dst.flush()
            os.fsync(dst.fileno())

        os.replace(temp_path, log_path)
        final_bytes = log_path.stat().st_size
        return (
            f"trimmed: {file_size // (1024 * 1024)}MB -> "
            f"{max(1, final_bytes // (1024 * 1024))}MB"
        )
    except Exception as exc:
        try:
            temp_path.unlink(missing_ok=True)  # type: ignore[name-defined]
        except Exception:
            pass
        return f"failed: {exc}"


def _kill_child(proc: subprocess.Popen) -> None:
    """Terminate a child process and all its descendants."""
    if proc.poll() is not None:
        return
    pid = proc.pid
    # On Windows, use taskkill /T to kill the whole process tree.
    # A simple proc.terminate() only kills the top-level process;
    # the standalone subprocess (and its threads) can survive.
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return
        except Exception:
            pass
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_coach(extra_args=None):
    """Run the coach as a subprocess and return exit code."""
    cmd = [get_python_executable(), "-m", "arenamcp.standalone"]
    if extra_args:
        cmd.extend(extra_args)

    # Set environment to signal we're in launcher mode
    env = os.environ.copy()
    env["ARENAMCP_LAUNCHER"] = "1"

    proc = None
    try:
        # Run as subprocess - this allows clean termination
        proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_DIR),
            env=env,
            # Don't capture output - let it go to the terminal
            stdout=None,
            stderr=None,
        )

        # Wait for process to complete
        return proc.wait()

    except KeyboardInterrupt:
        # User pressed Ctrl+C - terminate gracefully
        if proc:
            _kill_child(proc)
        return 0
    finally:
        # Safety net: if we exit for ANY reason (window close, crash, etc.)
        # make sure the child is dead.
        if proc and proc.poll() is None:
            _kill_child(proc)


def watch_for_changes():
    """Simple file watcher that returns True when .py files change."""
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler

        changed = [False]

        class Handler(FileSystemEventHandler):
            def on_modified(self, event):
                if event.src_path.endswith('.py'):
                    changed[0] = True

        observer = Observer()
        observer.schedule(Handler(), str(SRC_DIR), recursive=True)
        observer.start()

        return observer, lambda: changed[0]
    except ImportError:
        return None, lambda: False


def clear_screen():
    """Clear the terminal screen."""
    os.system('cls' if os.name == 'nt' else 'clear')


def print_banner(restart_count=0, autopilot=False):
    """Print startup banner."""
    title = "MTGA Autopilot Launcher" if autopilot else "MTGA Coach Launcher"
    print("=" * 50)
    print(f"  {title}")
    print("=" * 50)
    if restart_count > 0:
        print(f"  Restart #{restart_count}")
    print()
    print("  Ctrl+C = Exit | F9 in app = Restart")
    print("=" * 50)
    print()


def _register_windows_close_handler(instance_guard: SingleInstanceGuard) -> None:
    """Register a Windows console control handler to clean up on window close.

    When the user closes the terminal window, Windows sends CTRL_CLOSE_EVENT.
    Python's atexit handlers may not run in time, so we use SetConsoleCtrlHandler
    to release the lock and kill children before the OS force-terminates us.
    """
    try:
        import ctypes
        CTRL_CLOSE_EVENT = 2
        CTRL_LOGOFF_EVENT = 5
        CTRL_SHUTDOWN_EVENT = 6

        @ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)
        def _handler(event):
            if event in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
                instance_guard.release()
            return 0  # let default handler run too

        ctypes.windll.kernel32.SetConsoleCtrlHandler(_handler, True)
        # prevent garbage collection of the callback
        _register_windows_close_handler._prevent_gc = _handler
    except Exception:
        pass  # non-fatal — atexit is the primary fallback


def main():
    """Main launcher loop."""
    import argparse

    parser = argparse.ArgumentParser(description="MTGA Coach Launcher")
    parser.add_argument("--watch", "-w", action="store_true",
                        help="Auto-restart when .py files change")
    parser.add_argument("--autopilot", action="store_true",
                        help="Enable autopilot mode (AI clicks for you)")
    parser.add_argument("--afk", action="store_true",
                        help="AFK mode - no confirmation before clicks")
    parser.add_argument("--dry-run", action="store_true",
                        help="Dry run - plan actions but don't click")
    # parse_known_args allows forwarding arbitrary standalone flags.
    args, passthrough = parser.parse_known_args()

    def append_once(items, value):
        if value not in items:
            items.append(value)

    # Build pass-through arguments for the coach subprocess.
    if args.autopilot:
        append_once(passthrough, "--autopilot")
    if args.afk:
        append_once(passthrough, "--afk")
    if args.dry_run:
        append_once(passthrough, "--dry-run")

    # Setup file watcher if requested
    observer = None
    check_changed = lambda: False
    instance_guard = SingleInstanceGuard(LOCK_FILE)

    if not instance_guard.acquire():
        notify_already_running(instance_guard.describe_owner())
        return 1

    # Register cleanup so the lock is released even on abnormal exit
    # (e.g. user closes the terminal window, OS kills the process).
    import atexit
    atexit.register(instance_guard.release)

    if os.name == "nt":
        _register_windows_close_handler(instance_guard)

    if args.watch:
        observer, check_changed = watch_for_changes()
        if observer:
            print("[Launcher] Watching for code changes...")
        else:
            print("[Launcher] Warning: watchdog not installed, --watch disabled")

    restart_count = 0

    try:
        while True:
            trim_status = trim_player_log_if_needed()
            clear_screen()
            print_banner(restart_count, autopilot=args.autopilot)
            if trim_status.startswith("trimmed:"):
                print(f"[Launcher] Player.log {trim_status}")
                print()

            # Run the coach
            exit_code = run_coach(passthrough)

            # Check why it exited
            if exit_code == RESTART_EXIT_CODE:
                # Explicit restart request from F9
                print("\n[Launcher] Restart requested...")
                restart_count += 1
                time.sleep(0.5)
                continue

            elif check_changed():
                # Code changed during execution
                print("\n[Launcher] Code changed, restarting...")
                restart_count += 1
                time.sleep(0.5)
                continue

            elif exit_code != 0:
                # Error exit
                print(f"\n[Launcher] Coach exited with error code {exit_code}")
                print("\nPress Enter to restart, or Ctrl+C to exit...")
                try:
                    input()
                    restart_count += 1
                    continue
                except KeyboardInterrupt:
                    break
            else:
                # Normal exit (Ctrl+Q from app)
                print("\n[Launcher] Coach exited normally.")
                break

    except KeyboardInterrupt:
        print("\n[Launcher] Interrupted, exiting...")
    finally:
        if observer:
            observer.stop()
            observer.join()
        instance_guard.release()

    print("\nGoodbye!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
