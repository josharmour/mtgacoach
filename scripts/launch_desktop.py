"""Coach desktop launcher.

Two-phase bootstrap so the application never imports a single module from a
mutable checkout when a runtime snapshot is selected:

* Phase A (this interpreter): resolve the launch selection ONCE via the
  checkout's resolver, prepare/validate the versioned dependency environment,
  then spawn phase B as a fresh process.
* Phase B (fresh interpreter, ``MTGACOACH_LAUNCH_CHILD=1``): ``sys.path``
  contains only the snapshot's ``src``; every ``arenamcp`` import - including
  ``arenamcp.desktop.app`` - resolves inside the immutable snapshot root.

Any failure in selection/validation/environment preparation falls back to a
coherent checkout launch (everything from the checkout), never a mixed state.
See docs/RUNTIME_SNAPSHOTS.md for the operator contract.
"""

from __future__ import annotations

import datetime
import os
import subprocess
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
    try:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with _log_path().open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] {message}\n")
    except OSError:
        pass


def _show_error(title: str, message: str) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
    except Exception:
        pass


def _venv_python(env_dir: Path) -> Path:
    for candidate in (
        env_dir / "bin" / "python",
        env_dir / "Scripts" / "python.exe",
        env_dir / "Scripts" / "python",
    ):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"dependency environment {env_dir} has no python interpreter")


def _run_child_from_snapshot(snapshot_root: Path) -> int | None:
    """Phase B: fresh interpreter whose imports come only from the snapshot.

    Returns ``None`` when the snapshot cannot be launched cleanly so the
    caller falls back to a coherent checkout launch.
    """
    child_script = snapshot_root / "scripts" / "launch_desktop.py"
    if not child_script.is_file():
        child_script = Path(__file__).resolve()
    try:
        from arenamcp.desktop.dep_env import child_env_for

        child_environment = child_env_for(snapshot_root)
        python_bin = _venv_python(Path(child_environment["VIRTUAL_ENV"]))
    except Exception as exc:
        _write_log(f"dependency environment unavailable ({exc}); falling back to checkout")
        return None
    child_environment["MTGACOACH_LAUNCH_CHILD"] = "1"
    proc = subprocess.Popen(
        [str(python_bin), str(child_script)],
        env=child_environment,
        cwd=str(snapshot_root),
    )
    return proc.wait()


def main() -> int:
    # ---- Phase B: fresh interpreter bound to one immutable snapshot --------
    if os.environ.get("MTGACOACH_LAUNCH_CHILD") == "1":
        snapshot_root = Path(os.environ["MTGACOACH_SNAPSHOT_ROOT"]).resolve()
        src_dir = snapshot_root / "src"
        os.chdir(snapshot_root)
        os.environ["MTGACOACH_APP_ROOT"] = str(snapshot_root)
        os.environ.setdefault("MTGACOACH_RUNTIME_ROOT", str(_runtime_root()))
        # Prepend the snapshot's src tree WITHOUT dropping the interpreter's
        # stdlib/site-packages: the child keeps its own runtime modules while
        # arenamcp.* resolves only to the immutable snapshot.
        src_text = str(src_dir)
        if src_text not in sys.path:
            sys.path.insert(0, src_text)
        _write_log(
            "launch start (snapshot child)"
            f" python={sys.executable}"
            f" snapshot_root={snapshot_root}"
            f" sha={os.environ.get('MTGACOACH_SNAPSHOT_SHA', '')[:7]}"
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

    # ---- Phase A: bootstrap from the checkout ------------------------------
    repo_root = Path(__file__).resolve().parent.parent
    src_dir = repo_root / "src"

    src_text = str(src_dir)
    if src_text not in sys.path:
        sys.path.insert(0, src_text)

    selection_note = ""
    try:
        from arenamcp.desktop.runtime_snapshots import resolve_launch_selection

        configured_dir = os.environ.get("MTGACOACH_SNAPSHOTS_DIR", "").strip()
        selection = resolve_launch_selection(
            repo_root, snapshots_dir=Path(configured_dir) if configured_dir else None
        )
        if selection.snapshot_root is not None and selection.commit:
            # One selection, read once; hand it to a clean child process.
            os.environ["MTGACOACH_SNAPSHOT_SHA"] = selection.commit
            os.environ["MTGACOACH_SNAPSHOT_ROOT"] = str(selection.snapshot_root)
            selection_note = f" snapshot={selection.commit[:7]}"
            child_rc = _run_child_from_snapshot(selection.snapshot_root)
            if child_rc is not None:
                return child_rc
            os.environ.pop("MTGACOACH_SNAPSHOT_SHA", None)
            os.environ.pop("MTGACOACH_SNAPSHOT_ROOT", None)
            selection_note += " dep_env_fallback=checkout"
        os.environ.pop("MTGACOACH_SNAPSHOT_SHA", None)
        os.environ.pop("MTGACOACH_SNAPSHOT_ROOT", None)
        if os.environ.get("MTGACOACH_SNAPSHOTS_DISABLE", "").strip():
            selection_note = " snapshots=disabled"
    except Exception as exc:  # snapshots must never block a launch
        os.environ.pop("MTGACOACH_SNAPSHOT_SHA", None)
        os.environ.pop("MTGACOACH_SNAPSHOT_ROOT", None)
        selection_note = f" snapshot_resolution_failed={exc}"

    os.chdir(repo_root)
    os.environ["MTGACOACH_APP_ROOT"] = str(repo_root)
    os.environ.setdefault("MTGACOACH_RUNTIME_ROOT", str(_runtime_root()))

    src_text = str(src_dir)
    if src_text not in sys.path:
        sys.path.insert(0, src_text)

    _write_log(
        f"launch start python={sys.executable} repo_root={repo_root} src_dir={src_dir}{selection_note}"
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
