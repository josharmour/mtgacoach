"""Match-state persistence helpers for arenamcp.

Extracted from arenamcp.gamestate (2026-07-23) — save/load/mark-ended
helpers for ~/.arenamcp/last_match.json plus log-identity validation for
session-aware resume. Re-exported from arenamcp.gamestate for backwards
compatibility.
"""

import contextlib
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arenamcp.gamestate import GameState

logger = logging.getLogger(__name__)

MATCH_STATE_PATH = Path.home() / ".arenamcp" / "last_match.json"
MATCH_STATE_MAX_AGE = 1800  # 30 minutes

# 2026-10-19 10:11:10 "Failed to mark match ended: Expecting value: line 1
# column 1 (char 0)" (also "Extra data" 2026-10-18 21:13:05): the checkpoint
# save truncated the file in place while the parser thread read it. Every
# write now goes to a temp file in the same directory and is swapped in with
# os.replace, and read-modify-write (mark ended, the sticky "ended" check) runs
# under one lock.
_STATE_LOCK = threading.RLock()


def _read_state_file(path: Path) -> dict[str, Any] | None:
    """The saved state, or None when there is none (the caller holds _STATE_LOCK)."""
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        state = json.load(f)
    return state if isinstance(state, dict) else None


def _write_state_file(path: Path, state: dict[str, Any]) -> None:
    """Replace *path* atomically: readers see the old file or the new one, never a partial one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _ended_game_still_over(existing: dict[str, Any] | None, match_id: str, game_number: Any) -> bool:
    """mark_match_ended() closed this match and no later game of it has started.

    2026-10-19 09:34:55 and 10:05:59: after the match ended, Arena's
    MatchCompleted re-bound the finished match id to the reset game state, and
    the next checkpoint save rewrote the "ended" file as "active" (turn 0). The
    coach restarts at 09:53:35 and 10:06:23 then resumed that finished match,
    re-recorded it and overwrote its match bundle (#29). Only the match-scope
    mark sticks (it records ended_game_number); a save's own "ended" for a
    game result still gives way to the next game (Bo3 sideboarding resumes).
    """
    if not existing or existing.get("match_id") != match_id or existing.get("status") != "ended":
        return False
    if existing.get("ended_game_number") is None:
        return False
    try:
        number = int(game_number or 0)
        ended_number = int(existing.get("ended_game_number") or 1)
    except (TypeError, ValueError):
        return True
    # A Bo3 whose intermission carried no scope was marked ended after game 1.
    return number <= ended_number


def _match_state_path() -> Path:
    """Return the active match-state file path.

    Resolved through arenamcp.gamestate at call time (not captured at import)
    so that tests monkeypatching ``gamestate.MATCH_STATE_PATH`` keep working
    after the module split.
    """
    from arenamcp import gamestate

    return gamestate.MATCH_STATE_PATH


def save_match_state(
    game_state: "GameState",
    log_offset: int = 0,
    log_path: str | None = None,
) -> None:
    """Save current match state for recovery after restart.

    Writes match_id, local_seat_id, log_offset, log identity metadata,
    turn info, and timestamp to ~/.arenamcp/last_match.json.

    Args:
        game_state: Current GameState instance.
        log_offset: Current file position in the log file.
        log_path: Path to the log file (for session identity validation on resume).
    """
    if not game_state.match_id:
        return

    status = (
        "ended" if (game_state.last_game_result or getattr(game_state, "match_ended", False)) else "active"
    )
    state = {
        "match_id": game_state.match_id,
        "local_seat_id": game_state.local_seat_id,
        "log_offset": log_offset,
        "turn_number": game_state.turn_info.turn_number,
        "phase": game_state.turn_info.phase,
        "timestamp": time.time(),
        "status": status,
        "checkpoint": game_state.export_checkpoint(),
    }

    # Attach log identity for session-aware resume validation
    if log_path:
        try:
            log_p = Path(log_path)
            if log_p.exists():
                stat = log_p.stat()
                state["log_identity"] = {
                    "path": str(log_p),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                }
        except OSError:
            pass

    match_state_path = _match_state_path()
    try:
        with _STATE_LOCK:
            existing = None
            with contextlib.suppress(Exception):
                existing = _read_state_file(match_state_path)
            if _ended_game_still_over(existing, state["match_id"], getattr(game_state, "game_number", None)):
                state["status"] = "ended"
                for key in ("ended_at", "ended_game_number"):
                    if key in existing:
                        state[key] = existing[key]
            _write_state_file(match_state_path, state)
        logger.debug(f"Saved match state: turn {state['turn_number']}, offset {log_offset}")
    except Exception as e:
        logger.warning(f"Failed to save match state: {e}")


def validate_log_identity(
    saved_state: dict[str, Any],
    current_log_path: str | None = None,
) -> str:
    """Validate whether saved resume state matches the current log session.

    Compares saved log identity metadata against the current log file to
    determine if the offset is safe to resume from.

    Args:
        saved_state: Previously saved match state dict.
        current_log_path: Path to the current log file.

    Returns:
        One of:
        - "resume_same_session": offset is valid, same log session
        - "fresh_log_after_restart": log was recreated, offset invalid
        - "resume_invalid_path": log path changed or missing
        - "resume_no_identity": no log identity saved (legacy state), allow resume
        - "resume_append_mode_ambiguous": file grew but mtime changed significantly
    """
    saved_identity = saved_state.get("log_identity")
    if not saved_identity:
        logger.info("No log identity in saved state (legacy format) — allowing resume")
        return "resume_no_identity"

    if not current_log_path:
        logger.warning("No current log path provided for identity check")
        return "resume_invalid_path"

    try:
        current_path = Path(current_log_path)
        if not current_path.exists():
            logger.warning(f"Current log file does not exist: {current_path}")
            return "resume_invalid_path"

        stat = current_path.stat()
        saved_path = saved_identity.get("path", "")
        saved_size = saved_identity.get("size", 0)
        saved_mtime = saved_identity.get("mtime", 0)

        # Path mismatch (different log file entirely)
        if str(current_path) != saved_path:
            logger.info(f"Log path changed: saved={saved_path}, current={current_path}")
            return "resume_invalid_path"

        # File is smaller than saved size → file was recreated (MTGA restart)
        if stat.st_size < saved_size:
            logger.info(
                f"Log file shrank: saved_size={saved_size}, current_size={stat.st_size} "
                "— fresh log after restart"
            )
            return "fresh_log_after_restart"

        # mtime jumped significantly backward or forward (>2s tolerance) AND
        # file is smaller → strong signal of recreation
        # If file grew and mtime advanced, that's normal append behavior.
        mtime_delta = abs(stat.st_mtime - saved_mtime)

        # File is same size or larger, mtime advanced normally → same session
        if stat.st_size >= saved_size and mtime_delta < 300:
            logger.info(
                f"Log identity matches: size {saved_size}->{stat.st_size}, "
                f"mtime delta {mtime_delta:.1f}s — resuming same session"
            )
            return "resume_same_session"

        # File grew but mtime jumped a lot — could be appendlog mode
        if stat.st_size >= saved_size and mtime_delta >= 300:
            logger.warning(
                f"Log identity ambiguous: size grew ({saved_size}->{stat.st_size}) "
                f"but mtime delta is {mtime_delta:.0f}s — possible appendlog mode"
            )
            return "resume_append_mode_ambiguous"

        # Default: treat as fresh
        logger.info("Log identity does not match saved state — treating as fresh")
        return "fresh_log_after_restart"

    except OSError as e:
        logger.warning(f"Error checking log identity: {e}")
        return "resume_invalid_path"


def load_match_state() -> dict[str, Any] | None:
    """Load saved match state if valid (< 30 min old, status active).

    Returns:
        Dict with match_id, local_seat_id, log_offset, turn_number, phase,
        timestamp, status, and optionally log_identity. Or None if no valid
        state exists.
    """
    match_state_path = _match_state_path()
    if not match_state_path.exists():
        return None

    try:
        with _STATE_LOCK:
            state = _read_state_file(match_state_path)
        if state is None:
            return None

        # Validate age
        age = time.time() - state.get("timestamp", 0)
        if age > MATCH_STATE_MAX_AGE:
            logger.info(f"Match state too old ({age:.0f}s > {MATCH_STATE_MAX_AGE}s)")
            return None

        # Validate status
        if state.get("status") != "active":
            logger.info(f"Match state status is '{state.get('status')}', not active")
            return None

        logger.info(
            f"Loaded match state: match={state.get('match_id')}, "
            f"turn={state.get('turn_number')}, offset={state.get('log_offset')}"
        )
        return state

    except Exception as e:
        logger.warning(f"Failed to load match state: {e}")
        return None


def mark_match_ended() -> None:
    """Mark the saved match state as ended."""
    match_state_path = _match_state_path()
    if not match_state_path.exists():
        return

    try:
        with _STATE_LOCK:
            state = _read_state_file(match_state_path)
            if state is None:
                return
            state["status"] = "ended"
            state["ended_at"] = time.time()
            # Which game ended: a later game of the same (Bo3) match may resume.
            checkpoint = state.get("checkpoint")
            game_number = checkpoint.get("game_number") if isinstance(checkpoint, dict) else None
            try:
                ended = max(int(game_number or 1), int(state.get("ended_game_number") or 1))
            except (TypeError, ValueError):
                ended = 1
            state["ended_game_number"] = ended
            _write_state_file(match_state_path, state)
        logger.info("Marked match state as ended")
    except Exception as e:
        logger.warning(f"Failed to mark match ended: {e}")
