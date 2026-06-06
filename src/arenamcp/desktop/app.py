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
    app.setDesktopFileName("mtgacoach.desktop")

    from PySide6.QtGui import QIcon
    from .runtime import get_app_root
    icon_path = Path(get_app_root()) / "assets" / "icon.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    apply_theme(app, load_saved_theme())
    window = MainWindow()
    window.show()
    return app.exec()
