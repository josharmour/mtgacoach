"""Owner-local speech attempts and playback receipts, partitioned by Arena game."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from arenamcp.logging_config import LOG_FILE

logger = logging.getLogger(__name__)


def _folder(match_id, game_number):
    key = json.dumps([match_id, game_number], separators=(",", ":")).encode()
    return Path(LOG_FILE).parent / "speech_transcripts" / hashlib.sha256(key).hexdigest()


def record(epoch, stage, **details):
    """One append per event. Separate files keep desktop and engine writers independent."""
    try:
        folder = _folder(epoch.match_id, epoch.game_number)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        row = dict(
            time=datetime.now().astimezone().isoformat(timespec="microseconds"), stage=stage, **details
        )
        data = (json.dumps(row, ensure_ascii=False) + "\n").encode()
        path = folder / f"{os.getpid()}.jsonl"
        with open(path, "ab", opener=lambda p, flags: os.open(p, flags, 0o600)) as f:
            f.write(data)
    except (OSError, UnicodeError):
        logger.warning("Local speech transcript write failed", exc_info=True)


def for_game(snapshot):
    if not isinstance(snapshot, dict):
        return []
    folder = _folder(snapshot.get("match_id") or "desktop", snapshot.get("game_number") or 1)
    rows = []
    try:
        for path in folder.glob("*.jsonl"):
            with path.open("rb") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                        if isinstance(row, dict) and isinstance(row.get("time"), str):
                            rows.append(row)
                    except (ValueError, UnicodeError):
                        continue
    except OSError:
        logger.warning("Local speech transcript read failed", exc_info=True)
    return sorted(rows, key=lambda row: row["time"])
