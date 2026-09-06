from __future__ import annotations

import datetime
import os
import sys
import traceback
from pathlib import Path


def _runtime_root() -> Path:
    local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
    if local_appdata:
        return Path(local_appdata) / "mtgacoach"
    return Path.home() / ".local" / "share" / "mtgacoach"


def _log_path() -> Path:
    root = _runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    return root / "desktop-launch.log"


def _write_log(message: str) -> None:
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _log_path().open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {message}\n")


def _show_error(title: str, message: str) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
    except Exception:
        pass


def main() -> int:
    app_root = Path(__file__).resolve().parent.parent
    src_dir = app_root / "src"

    os.chdir(app_root)
    os.environ["MTGACOACH_APP_ROOT"] = str(app_root)
    os.environ.setdefault("MTGACOACH_RUNTIME_ROOT", str(_runtime_root()))

    src_text = str(src_dir)
    if src_text not in sys.path:
        sys.path.insert(0, src_text)

    _write_log(
        "installed launch start"
        f" python={sys.executable}"
        f" app_root={app_root}"
        f" src_dir={src_dir}"
    )

    try:
        from arenamcp.desktop.app import main as desktop_main

        return int(desktop_main())
    except BaseException as exc:  # noqa: BLE001
        # Audit blocker #2: a missing/broken PySide6 raises SystemExit,
        # which escaped `except Exception` — double-clicking the app did
        # literally nothing, forever, with no message and no log.
        if isinstance(exc, SystemExit) and (exc.code in (0, None)):
            return 0  # normal exit
        details = "".join(traceback.format_exception(exc)).rstrip()
        _write_log("installed launch failed\n" + details)
        _show_error(
            "mtgacoach launch failed",
            f"{exc}\n\nSee {_log_path()} for details.\n\n"
            "You can repair this install from a terminal with: "
            "mtgacoach-repair",
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
