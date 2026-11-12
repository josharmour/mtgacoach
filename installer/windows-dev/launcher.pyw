"""Local Windows entrypoint; source and subprocesses use the configured checkout."""

import json
import os
import runpy
import sys
import traceback
from pathlib import Path


def launch(config):
    checkout = Path(config["checkout"])
    entry = checkout / "scripts" / "launch_desktop.py"
    if not entry.is_file():
        raise FileNotFoundError(f"Connect to the source share before opening Coach Dev: {checkout}")
    os.environ.pop("PYTHONHOME", None)
    os.environ.pop("MTGACOACH_VELOPACK_PENDING", None)
    os.environ["PYTHONNOUSERSITE"] = "1"
    if config.get("git_dir"):
        os.environ["MTGACOACH_DEV_GIT_DIR"] = config["git_dir"]
    else:
        os.environ.pop("MTGACOACH_DEV_GIT_DIR", None)
    sys.path.insert(0, str(checkout / "src"))
    if config.get("runtime_snapshots"):
        # Opt-in immutable runtime snapshots: resolve the published selection
        # once here; launch_desktop honours the pre-made selection via
        # MTGACOACH_SNAPSHOT_SHA/ROOT and never re-resolves.
        try:
            from arenamcp.desktop.runtime_snapshots import resolve_launch_selection

            selection = resolve_launch_selection(
                checkout, snapshots_dir=config.get("snapshots_dir")
            )
            if selection.snapshot_root is not None and selection.commit:
                checkout = selection.snapshot_root
                os.environ["MTGACOACH_SNAPSHOT_SHA"] = selection.commit
                os.environ["MTGACOACH_SNAPSHOT_ROOT"] = str(selection.snapshot_root)
        except Exception as error:
            print(f"runtime snapshot resolution failed; using checkout: {error}")
    os.environ["PYTHONPATH"] = str(checkout / "src")
    os.environ["MTGACOACH_DEV_CHECKOUT"] = str(checkout)
    os.environ["MTGACOACH_BUILD_LABEL"] = "dev source"
    os.chdir(checkout)
    sys.argv[0] = str(entry)
    runpy.run_path(str(entry), run_name="__main__")


def main():
    root = Path(__file__).parent
    with (root / "launcher.log").open("a", encoding="utf-8", buffering=1) as log:
        sys.stdout = sys.stderr = log
        try:
            launch(json.loads((root / "launcher.json").read_text(encoding="utf-8")))
        except Exception as error:
            traceback.print_exc()
            if sys.platform == "win32":
                import ctypes

                ctypes.windll.user32.MessageBoxW(
                    None, f"{error}\n\nDetails: {root / 'launcher.log'}", "Coach Dev could not start", 0x10
                )
            raise SystemExit(1)


if __name__ == "__main__":
    main()
