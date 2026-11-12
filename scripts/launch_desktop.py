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
    repo_root = Path(__file__).resolve().parent.parent
    src_dir = repo_root / "src"

    # Bootstrap import: the snapshot resolver itself always comes from the
    # checkout; everything the app imports afterwards may come from an
    # immutable snapshot root (see docs/RUNTIME_SNAPSHOTS.md).
    src_text = str(src_dir)
    if src_text not in sys.path:
        sys.path.insert(0, src_text)

    selection_note = ""
    try:
        from arenamcp.desktop.runtime_snapshots import resolve_launch_selection

        selection = resolve_launch_selection(repo_root)
        if selection.snapshot_root is not None and selection.commit:
            # One selection, read once: run from the absolute versioned root.
            repo_root = selection.snapshot_root
            src_dir = selection.snapshot_root / "src"
            os.environ["MTGACOACH_SNAPSHOT_SHA"] = selection.commit
            os.environ["MTGACOACH_SNAPSHOT_ROOT"] = str(selection.snapshot_root)
            selection_note = f" snapshot={selection.commit[:7]}"
        elif os.environ.get("MTGACOACH_SNAPSHOTS_DISABLE", "").strip():
            selection_note = " snapshots=disabled"
    except Exception as exc:  # snapshots must never block a launch
        selection_note = f" snapshot_resolution_failed={exc}"

    os.chdir(repo_root)
    os.environ["MTGACOACH_APP_ROOT"] = str(repo_root)
    os.environ.setdefault("MTGACOACH_RUNTIME_ROOT", str(_runtime_root()))

    src_text = str(src_dir)
    if src_text not in sys.path:
        sys.path.insert(0, src_text)

    _write_log(
        "launch start"
        f" python={sys.executable}"
        f" repo_root={repo_root}"
        f" src_dir={src_dir}"
        f"{selection_note}"
    )

    try:
        from arenamcp.desktop.app import main as desktop_main

        return int(desktop_main())
    except Exception as exc:
        details = "".join(traceback.format_exception(exc)).rstrip()
        _write_log("launch failed\n" + details)
        _show_error(
            "mtgacoach launch failed",
            f"{exc}\n\nSee {_log_path()} for details.",
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
