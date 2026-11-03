"""Read the installed Velopack host's verified, completely downloaded update."""

from __future__ import annotations

import json
import os
from pathlib import Path

APPLY_UPDATE_EXIT = 86
APPLY_APPROVED_EXIT = 87
PENDING_ENV = "MTGACOACH_VELOPACK_PENDING"


def update_status() -> tuple[str, bool | None] | None:
    """Version and apply policy. A None policy means the download is in progress."""
    marker = os.environ.get(PENDING_ENV)
    if not marker:
        return None
    try:
        update = json.loads(Path(marker).read_text(encoding="utf-8"))
        version = update["version"]
        if not isinstance(version, str) or not version:
            return None
        if update.get("downloading") is True:
            return version, None
        automatic = update["automatic"]
        if isinstance(automatic, bool):
            return version, automatic
    except (ValueError, KeyError, TypeError, OSError):
        pass
    return None


def pending_update() -> tuple[str, bool] | None:
    status = update_status()
    if status and status[1] is not None:
        return status[0], status[1]
    return None
