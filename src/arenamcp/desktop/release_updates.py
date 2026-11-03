"""Read the installed Velopack host's verified, completely downloaded update."""

from __future__ import annotations

import json
import os
from pathlib import Path

APPLY_UPDATE_EXIT = 86
APPLY_APPROVED_EXIT = 87
PENDING_ENV = "MTGACOACH_VELOPACK_PENDING"


def pending_update() -> tuple[str, bool] | None:
    marker = os.environ.get(PENDING_ENV)
    if not marker:
        return None
    try:
        update = json.loads(Path(marker).read_text(encoding="utf-8"))
        version, automatic = update["version"], update["automatic"]
        if isinstance(version, str) and version and isinstance(automatic, bool):
            return version, automatic
    except (ValueError, KeyError, TypeError, OSError):
        pass
    return None
