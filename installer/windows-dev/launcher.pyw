"""Local Windows entrypoint; selection and imports stay out of this process.

This launcher never imports ``arenamcp`` itself: importing the package here
would pin ``arenamcp.__path__`` to the checkout for the whole process and let
mutable-checkout modules leak into a snapshot launch. Instead it hands off to
``scripts/launch_desktop.py``, whose two-phase bootstrap resolves the published
selection once and starts the application from a fresh interpreter bound to
the immutable snapshot root (or coherently from the checkout).
"""

import json
import os
import subprocess
import sys
import traceback
from pathlib import Path


def launch(config):
    checkout = Path(config["checkout"])
    entry = checkout / "scripts" / "launch_desktop.py"
    if not entry.is_file():
        raise FileNotFoundError(f"Connect to the source share before opening Coach Dev: {checkout}")
    environment = dict(os.environ)
    environment.pop("PYTHONHOME", None)
    environment.pop("MTGACOACH_VELOPACK_PENDING", None)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    if config.get("git_dir"):
        environment["MTGACOACH_DEV_GIT_DIR"] = config["git_dir"]
    else:
        environment.pop("MTGACOACH_DEV_GIT_DIR", None)
    # Fresh selection on every launch: never inherit a previous snapshot pin.
    for key in ("MTGACOACH_SNAPSHOT_SHA", "MTGACOACH_SNAPSHOT_ROOT", "MTGACOACH_LAUNCH_CHILD"):
        environment.pop(key, None)
    if config.get("runtime_snapshots"):
        snapshots_dir = config.get("snapshots_dir")
        if snapshots_dir:
            environment["MTGACOACH_SNAPSHOTS_DIR"] = str(snapshots_dir)
        else:
            environment.pop("MTGACOACH_SNAPSHOTS_DIR", None)
    else:
        environment["MTGACOACH_DISABLE_RUNTIME_SNAPSHOT"] = "1"
    environment["MTGACOACH_DEV_CHECKOUT"] = str(checkout)
    proc = subprocess.Popen([sys.executable, str(entry)], env=environment, cwd=str(checkout))
    return proc.wait()


def main():
    root = Path(__file__).parent
    with (root / "launcher.log").open("a", encoding="utf-8", buffering=1) as log:
        sys.stdout = sys.stderr = log
        try:
            raise SystemExit(launch(json.loads((root / "launcher.json").read_text(encoding="utf-8"))))
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
