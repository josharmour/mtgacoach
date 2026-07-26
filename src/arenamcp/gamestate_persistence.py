"""Match-state persistence helpers for arenamcp.

Extracted from arenamcp.gamestate (2026-07-23) — save/load/mark-ended
helpers for ~/.arenamcp/last_match.json plus log-identity validation for
session-aware resume. Re-exported from arenamcp.gamestate for backwards
compatibility.
"""

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arenamcp.gamestate import GameState

logger = logging.getLogger(__name__)

MATCH_STATE_PATH = Path.home() / ".arenamcp" / "last_match.json"
MATCH_STATE_MAX_AGE = 1800  # 30 minutes


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
        match_state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(match_state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
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
        with open(match_state_path, encoding="utf-8") as f:
            state = json.load(f)

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
        with open(match_state_path, encoding="utf-8") as f:
            state = json.load(f)

        state["status"] = "ended"
        state["ended_at"] = time.time()

        with open(match_state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        logger.info("Marked match state as ended")
    except Exception as e:
        logger.warning(f"Failed to mark match ended: {e}")
