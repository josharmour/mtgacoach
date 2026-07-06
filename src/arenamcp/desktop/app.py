from __future__ import annotations

import datetime
import faulthandler
import os
import sys
import threading
import traceback
from pathlib import Path

_LOG_HANDLE = None
_INSTANCE_MUTEX = None


def _log_path() -> Path:
    from .runtime import get_runtime_root

    runtime_root = Path(get_runtime_root())
    runtime_root.mkdir(parents=True, exist_ok=True)
    return runtime_root / "desktop.log"


def _write_log(message: str) -> None:
    handle = _LOG_HANDLE
    if handle is None:
        return
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    handle.write(f"[{timestamp}] {message}\n")
    handle.flush()


def _configure_logging() -> None:
    global _LOG_HANDLE

    from .runtime import get_app_root, get_runtime_root

    _LOG_HANDLE = _log_path().open("a", encoding="utf-8", buffering=1)
    try:
        faulthandler.enable(_LOG_HANDLE, all_threads=True)
    except Exception:
        pass

    _write_log(
        "desktop start"
        f" pid={os.getpid()}"
        f" python={sys.executable}"
        f" app_root={get_app_root()}"
        f" runtime_root={get_runtime_root()}"
    )

    def handle_exception(exc_type, exc_value, exc_traceback) -> None:
        details = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        _write_log("unhandled exception\n" + details.rstrip())
        sys.__excepthook__(exc_type, exc_value, exc_traceback)

    def handle_thread_exception(args: threading.ExceptHookArgs) -> None:
        details = "".join(
            traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
        )
        thread_name = args.thread.name if args.thread is not None else "<unknown>"
        _write_log(f"unhandled thread exception in {thread_name}\n" + details.rstrip())
        threading.__excepthook__(args)

    sys.excepthook = handle_exception
    threading.excepthook = handle_thread_exception


def _acquire_single_instance_lock() -> bool:
    """Prevent duplicate desktop instances from spawning competing overlays."""
    global _INSTANCE_MUTEX

    if os.name != "nt":
        return True

    try:
        import ctypes

        ERROR_ALREADY_EXISTS = 183
        mutex_name = "Local\\mtgacoach.desktop"
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
        if not handle:
            return True
        if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            ctypes.windll.kernel32.CloseHandle(handle)
            return False
        _INSTANCE_MUTEX = handle
        return True
    except Exception:
        return True


def _release_single_instance_lock() -> None:
    global _INSTANCE_MUTEX

    if _INSTANCE_MUTEX is None or os.name != "nt":
        return

    try:
        import ctypes

        ctypes.windll.kernel32.CloseHandle(_INSTANCE_MUTEX)
    except Exception:
        pass
    _INSTANCE_MUTEX = None


def main() -> int:
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise SystemExit(
            "PySide6 is not installed. Install it with `pip install -e .[desktop]`."
        ) from exc

    _configure_logging()

    app = QApplication(sys.argv)
    if not _acquire_single_instance_lock():
        _write_log("desktop start skipped: another instance is already running")
        QMessageBox.information(None, "mtgacoach", "mtgacoach is already running.")
        return 0
    app.aboutToQuit.connect(_release_single_instance_lock)

    from .main_window import MainWindow
    from .theme import apply_theme, load_saved_theme

    app.setApplicationName("mtgacoach")
    app.setOrganizationName("mtgacoach")
    # Base name WITHOUT the .desktop suffix (Qt convention). On Wayland this
    # becomes the window app_id; GNOME shows a taskbar/dock icon only when a
    # matching mtgacoach.desktop file is installed (see scripts/ install
    # helper) — setWindowIcon alone is not enough there.
    app.setDesktopFileName("mtgacoach")

    from PySide6.QtGui import QIcon
    from .runtime import get_app_root
    root = Path(get_app_root())
    for candidate in (
        root / "assets" / "icon.png",
        root / "mtga_coach.ico",
        root / "icon.ico",
    ):
        if candidate.exists():
            app.setWindowIcon(QIcon(str(candidate)))
            break

    apply_theme(app, load_saved_theme())

    # Audit blocker #5: never gate the main window on the setup splash —
    # its own failure text says "open the Repair tab", which only exists
    # inside MainWindow. MainWindow degrades gracefully when the runtime
    # is unprovisioned, and the Repair tab is the recovery surface.
    _run_first_run_setup(app)

    window = MainWindow()
    window.show()
    return app.exec()


def _run_first_run_setup(app) -> bool:
    """Show the setup splash when the runtime environment is not provisioned.

    Returns ``True`` if the app should continue to the main window, ``False`` if
    setup was needed but failed/cancelled (caller should exit). When no setup is
    needed this returns ``True`` immediately and shows nothing.
    """
    # This function runs from main() AFTER QApplication was constructed, so
    # PySide6 (and the whole app environment) is already proven working. A
    # pip/uv install has a complete environment by definition; the
    # provisioning splash is a relic of the old bootstrap-launcher/fat-venv
    # model and here would only try to run setup_wizard.py — which isn't
    # shipped in the wheel — and fail (v2.7.1 "Setup failed" screenshot).
    # Runtime provisioning now belongs to the stdlib launcher and the
    # Repair tab, never to the already-running Qt app.
    if "PySide6" in sys.modules:
        return True

    try:
        from .runtime import detect_runtime_state
    except Exception as exc:  # pragma: no cover - defensive
        _write_log(f"runtime detection unavailable, skipping first-run setup: {exc}")
        return True

    try:
        state = detect_runtime_state()
    except Exception as exc:  # pragma: no cover - defensive
        _write_log(f"detect_runtime_state failed, skipping first-run setup: {exc}")
        return True

    # Only provision when we have a Python but it is not yet usable for the app.
    needs_setup = state.python_exe is not None and not state.has_ready_python_runtime
    if not needs_setup:
        return True

    _write_log(
        "first-run setup needed"
        f" python_source={state.python_source}"
        f" python_ready={state.python_ready}"
        f" runtime_venv_exists={state.runtime_venv_exists}"
    )

    try:
        from .setup_splash import SetupSplashWindow
    except Exception as exc:  # pragma: no cover - defensive
        _write_log(f"setup splash unavailable, continuing without it: {exc}")
        return True

    splash = SetupSplashWindow()
    splash.show()
    splash.start_setup()

    # Drive a local event loop until the splash window is dismissed. The splash
    # closes itself on success (via the connected slot) and stays open with a
    # Retry/Close affordance on failure.
    splash.setup_completed.connect(
        lambda success, _message: splash.close() if success else None
    )
    # Block on the OS event wait between events instead of spin-looping, which
    # would otherwise peg a CPU core for the whole setup. The timeout keeps
    # isVisible() re-checked promptly (incl. the failure/Retry-Close path).
    from PySide6.QtCore import QEventLoop

    while splash.isVisible():
        app.processEvents(QEventLoop.WaitForMoreEvents, 100)

    if not splash.setup_success:
        _write_log(
            "first-run setup cancelled or failed; continuing to the main "
            "window so the Repair tab is reachable"
        )
        return False
    return True
