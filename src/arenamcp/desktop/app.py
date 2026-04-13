from __future__ import annotations

import datetime
import faulthandler
import os
import sys
import threading
import traceback
from pathlib import Path

_LOG_HANDLE = None


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


def main() -> int:
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise SystemExit(
            "PySide6 is not installed. Install it with `pip install -e .[desktop]`."
        ) from exc

    _configure_logging()
    from .main_window import MainWindow
    from .theme import apply_theme, load_saved_theme

    app = QApplication(sys.argv)
    app.setApplicationName("mtgacoach")
    app.setOrganizationName("mtgacoach")
    apply_theme(app, load_saved_theme())
    window = MainWindow()
    window.show()
    return app.exec()
