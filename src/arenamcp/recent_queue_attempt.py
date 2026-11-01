"""Fail-closed duplicate protection across bridge reconnects and Coach restarts."""

import json
import os
import threading
import time
from pathlib import Path

_lock = threading.Lock()


def reserve(identity: str, target: dict, completed_at: float = 0, *, path: Path | None = None) -> None:
    if not identity:
        raise ValueError("Arena process identity is unavailable")
    if path is None:
        from arenamcp.settings import SETTINGS_DIR

        path = SETTINGS_DIR / "recent_queue_attempt.json"
    with _lock:
        if path.exists():
            previous = json.loads(path.read_text())
            if previous.get("arena_identity") == identity and completed_at <= previous["attempted_at"]:
                raise ValueError(
                    "A Recent Match submission is already pending for this Arena session; "
                    "waiting without submitting twice. If it failed, restart Arena through Coach."
                )
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "arena_identity": identity,
            "attempted_at": time.time(),
            "event_id": target["event_id"],
            "deck_id": target["deck_id"],
        }
        temporary = path.with_suffix(".tmp")
        with temporary.open("w") as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
