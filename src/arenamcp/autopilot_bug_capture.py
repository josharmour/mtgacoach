"""Bounded, local capture of an autopilot failure and the player's recovery."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def capture_mtga_screenshot(directory: Path, stem: str) -> dict[str, str]:
    """Capture only Arena's window rectangle, using the existing desktop locator."""
    import sys

    from PIL import ImageGrab

    from arenamcp.desktop.window_tracking import get_mtga_window_rect

    rect = get_mtga_window_rect()
    if rect is None:
        return {}
    left, top, width, height = rect
    if width <= 0 or height <= 0:
        return {}
    directory.mkdir(parents=True, exist_ok=True)
    kwargs = {"all_screens": True} if sys.platform == "win32" else {}
    image = ImageGrab.grab(bbox=(left, top, left + width, top + height), **kwargs)
    path = directory / f"{stem}_mtga.png"
    image.save(str(path), "PNG")
    return {"mtga": str(path)}


class AutopilotBugCapture:
    """Persist the initial diagnostics plus observed manual-recovery transitions.

    State transitions are observations, not claims about which mouse click caused
    an action. Arena's action_history/recent_events remain attached as evidence.
    There is no input interception, screen video, model call, or network upload.
    """

    def __init__(
        self,
        report_dir: Path,
        *,
        max_duration_s: float = 120.0,
        max_events: int = 240,
        max_bytes: int = 8 * 1024 * 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.report_dir = Path(report_dir)
        self.max_duration_s = max_duration_s
        self.max_events = max_events
        self.max_bytes = max_bytes
        self._clock = clock
        self._lock = threading.RLock()
        self._active = False
        self._capture_id = ""
        self._report: dict[str, Any] = {}
        self.report_path: Path | None = None
        self._events_path: Path | None = None
        self._started = 0.0
        self._event_count = 0
        self._bytes = 0
        self._last_signature = ""
        self._match_id: Any = None

    @property
    def active(self) -> bool:
        return self._active

    @property
    def status(self) -> dict[str, Any]:
        with self._lock:
            recovery = self._report.get("manual_recovery", {})
            return {
                "capture_id": self._capture_id,
                "automatic": bool(self._report.get("automatic", False)),
                "phase": "recording" if self._active else "completed",
                "path": str(self.report_path or ""),
                "event_count": self._event_count,
                "elapsed_s": round(max(0.0, self._clock() - self._started), 1),
                "finish_reason": recovery.get("finish_reason"),
                "message": (
                    "Autopilot paused — recording your recovery for up to two minutes. Resume autopilot when finished."
                    if self._active
                    else "Recovery capture saved locally."
                ),
            }

    def begin(self, capture_id: str, initial_report: dict[str, Any]) -> Path:
        with self._lock:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", capture_id):
                raise ValueError("Invalid autopilot bug capture ID")
            if capture_id == self._capture_id and self.report_path is not None:
                return self.report_path
            if self._active:
                self.finish("superseded")
            self.report_dir.mkdir(parents=True, exist_ok=True)
            self._capture_id = capture_id
            self.report_path = self.report_dir / f"autopilot_bug_{capture_id}.json"
            self._events_path = self.report_dir / f"autopilot_bug_{capture_id}_recovery.jsonl"
            # Serialize now: later mutation/clearing of the planner must not
            # change what was captured before the pause.
            self._report = json.loads(json.dumps(initial_report, default=str))
            from arenamcp import __version__

            self._report.setdefault("version", __version__)
            self._report.setdefault("timestamp", _now())
            self._report["reason"] = "Autopilot bug — manual recovery capture"
            self._report["manual_recovery"] = {
                "capture_id": capture_id,
                "status": "recording",
                "started_at": _now(),
                "events_path": str(self._events_path),
                "max_duration_s": self.max_duration_s,
                "max_events": self.max_events,
                "max_bytes": self.max_bytes,
                "observation_source": "Arena game state, action history, and recent events while autopilot is paused",
            }
            self._started = self._clock()
            self._event_count = 0
            self._bytes = 0
            self._last_signature = ""
            self._match_id = (self._report.get("game_state") or {}).get("match_id")
            self._events_path.write_text("", encoding="utf-8")
            self._active = True
            try:
                self._write_report()
                self._append_state(self._report.get("game_state") or {}, "initial")
            except Exception:
                self._active = False
                raise
            return self.report_path

    def attach_screenshots(self, capture_id: str, screenshots: dict[str, str], *, error: str = "") -> bool:
        with self._lock:
            if capture_id != self._capture_id or self.report_path is None:
                return False
            self._report.setdefault("screenshots", {}).update(screenshots)
            if error:
                self._report["screenshot_error"] = error
            self._write_report()
            return True

    def observe(self, game_state: dict[str, Any], *, autopilot_active: bool = False) -> bool:
        """Record a changed state; return true only when this call finalizes capture."""
        with self._lock:
            if not self._active:
                return False
            match_id = game_state.get("match_id")
            if match_id and self._match_id and match_id != self._match_id:
                self.finish("match_changed")
                return True
            if autopilot_active:
                self._append_state(game_state, "autopilot_resuming")
                self.finish("autopilot_resumed")
                return True
            if self._clock() - self._started >= self.max_duration_s:
                self.finish("time_limit")
                return True
            if self._match_id is None and match_id:
                self._match_id = match_id
            self._append_state(game_state, "state_change")
            if not self._active:
                return True
            if (
                game_state.get("game_over")
                or game_state.get("last_game_result")
                or (self._match_id is not None and "match_id" in game_state and match_id is None)
            ):
                self.finish("match_ended")
                return True
            return False

    def finish(self, reason: str) -> Path | None:
        with self._lock:
            if not self._active:
                return self.report_path
            self._active = False
            self._report["manual_recovery"].update(
                {
                    "status": "completed",
                    "finished_at": _now(),
                    "finish_reason": reason,
                    "elapsed_s": round(max(0.0, self._clock() - self._started), 3),
                    "event_count": self._event_count,
                    "bytes": self._bytes,
                }
            )
            self._write_report()
            return self.report_path

    def _append_state(self, game_state: dict[str, Any], event: str) -> None:
        # Immutable deck definitions are in the initial report. Keep each
        # subsequent board/decision/action snapshot small enough to write fast.
        state = {
            key: value
            for key, value in game_state.items()
            if key not in {"deck_cards", "sideboard_cards", "library_summary", "deck_context"}
        }
        packed = json.dumps(state, sort_keys=True, separators=(",", ":"), default=str)
        signature = hashlib.sha256(packed.encode()).hexdigest()
        if signature == self._last_signature:
            return
        record = {
            "timestamp": _now(),
            "elapsed_s": round(max(0.0, self._clock() - self._started), 3),
            "event": event,
            "game_state": state,
        }
        line = (json.dumps(record, separators=(",", ":"), default=str) + "\n").encode("utf-8")
        if self._event_count >= self.max_events or self._bytes + len(line) > self.max_bytes:
            self.finish("event_limit" if self._event_count >= self.max_events else "size_limit")
            return
        assert self._events_path is not None
        with self._events_path.open("ab") as stream:
            stream.write(line)
        self._last_signature = signature
        self._event_count += 1
        self._bytes += len(line)

    def _write_report(self) -> None:
        assert self.report_path is not None
        temporary = self.report_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._report, indent=2, default=str), encoding="utf-8")
        temporary.replace(self.report_path)
