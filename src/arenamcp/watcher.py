"""MTGA log file watcher using watchdog.

This module provides real-time monitoring of the MTGA Player.log file,
delivering new content via callback as it's written.
"""

import os
import logging
import glob
import re
import time
from pathlib import Path
from typing import Callable, Optional

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent

logger = logging.getLogger(__name__)

MATCH_START_TOKENS = (
    "MatchCreated",
    "MatchGameRoomStateChangedEvent",
    "Event_Join",
)
MATCH_END_TOKENS = (
    "GREMessageType_IntermissionReq",
    "MatchState_MatchComplete",
    "MatchState_GameComplete",
    "MatchGameRoomStateType_MatchCompleted",
    '"resultList"',
)
DRAFT_ACTIVITY_TOKENS = (
    "CardsInPack",
    "PackCards",
    "DraftPack",
    "DraftStatus",
    "SelfPack",
    "SelfPick",
)
ACTIVE_GAMEPLAY_TOKENS = (
    "GREMessageType_GameStateMessage",
    "GREMessageType_QueuedGameStateMessage",
    "GREMessageType_MulliganReq",
    "GREMessageType_PromptReq",
    "GREMessageType_SubmitDeckReq",
)
INACTIVE_GAMEPLAY_TOKENS = (
    "MatchState_MatchComplete",
    "MatchState_GameComplete",
    "GameStage_GameOver",
)
RESUME_OVERRIDE_SLACK_BYTES = 4096

_WINDOWS_ABS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _contains_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def _is_active_gameplay_line(line: str) -> bool:
    return _contains_any(line, ACTIVE_GAMEPLAY_TOKENS) and not _contains_any(
        line,
        INACTIVE_GAMEPLAY_TOKENS,
    )


def _startup_anchor_from_tail(
    content_bytes: bytes,
    *,
    start_offset: int,
    file_size: int,
) -> tuple[int, str]:
    """Choose a relevant startup position from a tail chunk of Player.log.

    The goal is to reconstruct the current live session without replaying old
    draft or match history on startup.
    """
    if not content_bytes:
        return file_size, "empty_log"

    content = content_bytes.decode("utf-8", errors="replace")
    if not content:
        return file_size, "empty_log"

    events: list[tuple[str, int]] = []
    byte_pos = 0

    for raw_line in content.splitlines(keepends=True):
        line = raw_line.strip()
        absolute_pos = start_offset + byte_pos

        if line:
            if _contains_any(line, MATCH_START_TOKENS):
                events.append(("match_start", absolute_pos))
            if _contains_any(line, MATCH_END_TOKENS):
                events.append(("match_end", absolute_pos))
            if _contains_any(line, DRAFT_ACTIVITY_TOKENS):
                events.append(("draft_activity", absolute_pos))
            if _is_active_gameplay_line(line):
                events.append(("active_gameplay", absolute_pos))

        byte_pos += len(raw_line.encode("utf-8"))

    if not events:
        return file_size, "no_relevant_events"

    last_active_index = next(
        (idx for idx in range(len(events) - 1, -1, -1) if events[idx][0] == "active_gameplay"),
        None,
    )
    if last_active_index is not None:
        if last_active_index < len(events) - 1:
            trailing_kind, trailing_pos = events[-1]
            if trailing_kind == "draft_activity":
                return trailing_pos, "draft_waiting"
            if trailing_kind in {"match_start", "match_end"}:
                return file_size, "idle_or_completed"

        active_anchor = events[last_active_index][1]
        for kind, pos in reversed(events[:last_active_index]):
            if kind == "active_gameplay":
                active_anchor = pos
                continue
            if kind == "match_start":
                return pos, "active_match"
            if kind == "draft_activity":
                return pos, "active_draft"
            if kind == "match_end":
                return active_anchor, "mid_session_after_match_end"
        return active_anchor, "mid_session_active"

    last_kind, last_pos = events[-1]
    if last_kind == "draft_activity":
        return last_pos, "draft_waiting"

    if last_kind in {"match_start", "match_end"}:
        return file_size, "idle_or_completed"

    return file_size, "no_active_session"


def _is_wsl() -> bool:
    """Return True when running under WSL."""
    if os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def _windows_path_to_wsl(path: str) -> Path:
    """Convert a Windows absolute path (C:\\...) to WSL mount path (/mnt/c/...)."""
    drive = path[0].lower()
    rest = path[2:].lstrip("\\/")
    rest = rest.replace("\\", "/")
    return Path(f"/mnt/{drive}/{rest}")


def _default_log_path() -> str:
    """Best-effort default MTGA Player.log path for Windows/WSL."""
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if local_appdata:
        return os.path.join(
            os.path.dirname(local_appdata),
            "LocalLow",
            "Wizards Of The Coast",
            "MTGA",
            "Player.log",
        )

    if _is_wsl():
        wsl_candidates = glob.glob(
            "/mnt/c/Users/*/AppData/LocalLow/Wizards Of The Coast/MTGA/Player.log"
        )
        if wsl_candidates:
            return wsl_candidates[0]

    userprofile = os.environ.get("USERPROFILE", "")
    if userprofile:
        return os.path.join(
            userprofile,
            "AppData",
            "LocalLow",
            "Wizards Of The Coast",
            "MTGA",
            "Player.log",
        )

    # Last-resort fallback: construct from platform-appropriate home directory.
    # On Windows (or WSL without env vars) this resolves to the standard MTGA
    # log location under the current user's profile.
    home = Path.home()
    return str(
        home / "AppData" / "LocalLow" / "Wizards Of The Coast" / "MTGA" / "Player.log"
    )


def _normalize_log_path(path: str) -> Path:
    """Normalize log path across Windows and WSL environments."""
    expanded = os.path.expandvars(os.path.expanduser(path))

    # In WSL, a raw "C:\..." path is interpreted as relative by pathlib.
    # Convert it explicitly so the watcher targets the real Player.log path.
    if _is_wsl() and _WINDOWS_ABS_PATH_RE.match(expanded):
        return _windows_path_to_wsl(expanded)

    return Path(expanded).resolve()

# Default MTGA log path on Windows
# Use LOCALAPPDATA approach which is more reliable than APPDATA/../LocalLow
DEFAULT_LOG_PATH = _default_log_path()


class MTGALogHandler(FileSystemEventHandler):
    """FileSystemEventHandler that tracks file position for incremental reads."""

    def __init__(self, log_path: str, callback: Callable[[str], None]) -> None:
        """Initialize the handler.

        Args:
            log_path: Path to the MTGA Player.log file.
            callback: Function called with new log content as it's written.
        """
        super().__init__()
        self.log_path = _normalize_log_path(log_path)
        self.callback = callback
        self.file_position: int = 0

        # Initialize position to end of file if it exists
        if self.log_path.exists():
            try:
                self.file_position = self.log_path.stat().st_size
                logger.debug(f"Initialized file position to {self.file_position}")
            except OSError as e:
                logger.warning(f"Could not get file size: {e}")
                self.file_position = 0

    def on_modified(self, event: FileModifiedEvent) -> None:
        """Handle file modification events."""
        if event.is_directory:
            return

        # Check if this is our target file
        event_path = Path(event.src_path).resolve()
        if event_path != self.log_path:
            return

        self._read_new_content()

    def on_created(self, event: FileCreatedEvent) -> None:
        """Handle file creation events (log truncation on MTGA restart)."""
        if event.is_directory:
            return

        event_path = Path(event.src_path).resolve()
        if event_path != self.log_path:
            return

        # Reset position when file is recreated
        logger.info("Log file recreated, resetting position to 0")
        self.file_position = 0
        self._read_new_content()

    def _read_new_content(self) -> None:
        """Read new content from the log file and invoke callback."""
        try:
            with open(self.log_path, 'r', encoding='utf-8', errors='replace') as f:
                # Check if file was truncated (position beyond file size)
                f.seek(0, 2)  # Seek to end
                file_size = f.tell()

                if file_size < self.file_position:
                    # File was truncated, reset to beginning
                    logger.info(f"File truncated (size {file_size} < position {self.file_position}), resetting")
                    self.file_position = 0

                # Seek to our tracked position and read new content
                f.seek(self.file_position)
                new_content = f.read()

                if new_content:
                    self.file_position = f.tell()
                    logger.debug(f"Read {len(new_content)} chars, new position: {self.file_position}")
                    self.callback(new_content)

        except FileNotFoundError:
            logger.debug("Log file not found (MTGA may not be running)")
        except PermissionError as e:
            # Windows file locking - retry is handled by watchdog's next event
            logger.debug(f"Permission error reading log: {e}")
        except OSError as e:
            logger.warning(f"Error reading log file: {e}")

    def read_from_position(self, start_position: int) -> None:
        """Read content from a specific position and invoke callback.

        Used for backfilling existing log content on startup.

        Args:
            start_position: Byte position to start reading from.
        """
        try:
            with open(self.log_path, 'r', encoding='utf-8', errors='replace') as f:
                f.seek(start_position)
                content = f.read()
                self.file_position = f.tell()

                if content:
                    logger.info(f"Backfill: read {len(content)} chars from position {start_position}")
                    self.callback(content)

        except FileNotFoundError:
            logger.debug("Log file not found for backfill")
        except OSError as e:
            logger.warning(f"Error during backfill read: {e}")


class MTGALogWatcher:
    """Watches the MTGA Player.log file for changes and delivers new content via callback."""

    def __init__(
        self,
        callback: Callable[[str], None],
        log_path: Optional[str] = None,
        backfill: bool = True,
        resume_offset: Optional[int] = None,
    ) -> None:
        """Initialize the log watcher.

        Args:
            callback: Function called with new log content as chunks of text.
            log_path: Path to Player.log. Defaults to MTGA_LOG_PATH env var
                     or standard Windows location.
            backfill: If True, parse existing log content from the last match
                     start on first call to start(). Enables catching up on
                     in-progress games. Defaults to True.
            resume_offset: If provided, start reading from this byte offset
                          instead of the default position. Used for match
                          state recovery after restart.
        """
        # Resolve log path
        if log_path is None:
            log_path = os.environ.get("MTGA_LOG_PATH", DEFAULT_LOG_PATH)

        self.log_path = _normalize_log_path(log_path)
        self.callback = callback
        self._backfill_enabled = backfill
        self._resume_offset = resume_offset
        self._observer: Optional[Observer] = None
        self._handler: Optional[MTGALogHandler] = None

        # No-growth detection: track when we last saw the file grow
        self._last_known_size: int = 0
        self._last_growth_time: float = time.time()
        self._no_growth_warned: bool = False

        logger.info(f"MTGALogWatcher initialized for: {self.log_path}")

    def find_relevant_start(self) -> tuple[int, str]:
        """Find the most relevant startup position for the current session.

        Scans only the tail of the log and classifies whether the latest
        relevant activity looks like an active match, active draft, or an
        already completed session.
        """
        if not self.log_path.exists():
            return 0, "missing_log"

        try:
            file_size = self.log_path.stat().st_size

            # Scan last 15MB. This is enough to cover long matches without
            # replaying the whole file on startup.
            MAX_SCAN_BYTES = 15 * 1024 * 1024
            read_size = min(file_size, MAX_SCAN_BYTES)
            start_offset = max(0, file_size - read_size)

            if read_size == 0:
                return 0, "empty_log"

            with open(self.log_path, 'rb') as f:
                f.seek(start_offset)
                content_bytes = f.read(read_size)

            start_pos, mode = _startup_anchor_from_tail(
                content_bytes,
                start_offset=start_offset,
                file_size=file_size,
            )
            logger.info(
                "Startup anchor: mode=%s offset=%s (scanned last %.1fMB)",
                mode,
                start_pos,
                read_size / 1024 / 1024,
            )
            return start_pos, mode

        except OSError as e:
            logger.warning(f"Error scanning log for startup anchor: {e}")
            return 0, "scan_error"

    def _is_fresh_log(self) -> bool:
        """Detect if the log file is freshly created (MTGA just launched).

        A fresh log is small (< 100KB) and was modified recently (< 60s).
        When MTGA launches, it creates a new Player.log with w+ mode,
        so the file starts near-empty and grows quickly.

        Returns:
            True if the log appears freshly created.
        """
        if not self.log_path.exists():
            return False
        try:
            stat = self.log_path.stat()
            age = time.time() - stat.st_mtime
            is_fresh = stat.st_size < 100 * 1024 and age < 60
            if is_fresh:
                logger.info(
                    f"Fresh log detected: size={stat.st_size}, "
                    f"age={age:.1f}s — skipping backfill"
                )
            return is_fresh
        except OSError:
            return False

    def start(self) -> None:
        """Start watching the log file.

        Creates a watchdog Observer that monitors the directory containing
        the log file for modifications. If backfill is enabled, first processes
        existing log content from the last match start.

        Startup modes:
        - resumed_session: read from saved offset when still relevant
        - readahead_override: tail scan detected a newer active/completed session
        - fresh_launch: skip scan and read from start of a new log
        - readahead: choose a relevant anchor from the recent tail
        """
        if self._observer is not None:
            logger.warning("Watcher already started")
            return

        # Ensure parent directory exists
        watch_dir = self.log_path.parent
        if not watch_dir.exists():
            logger.warning(f"Watch directory does not exist: {watch_dir}")
            # Still set up the watcher - it will detect when directory is created

        self._handler = MTGALogHandler(str(self.log_path), self.callback)
        relevant_start, relevant_mode = (
            self.find_relevant_start() if self.log_path.exists() else (0, "missing_log")
        )

        if self._resume_offset is not None and self.log_path.exists():
            file_size = self.log_path.stat().st_size
            if self._resume_offset <= file_size:
                if relevant_start > self._resume_offset + RESUME_OVERRIDE_SLACK_BYTES:
                    logger.info(
                        "Startup mode: readahead_override (%s at %s supersedes saved offset %s)",
                        relevant_mode,
                        relevant_start,
                        self._resume_offset,
                    )
                    self._handler.read_from_position(relevant_start)
                else:
                    logger.info(f"Startup mode: resumed_session (offset {self._resume_offset})")
                    self._handler.read_from_position(self._resume_offset)
            else:
                logger.warning(
                    f"Resume offset {self._resume_offset} > file size {file_size}, "
                    "falling back to backfill"
                )
                if self._backfill_enabled:
                    start_pos = relevant_start
                    self._handler.read_from_position(start_pos)
        elif self._backfill_enabled and self.log_path.exists():
            if self._is_fresh_log():
                logger.info("Startup mode: fresh_launch (reading from start)")
                self._handler.read_from_position(0)
            else:
                logger.info("Startup mode: readahead (%s)", relevant_mode)
                self._handler.read_from_position(relevant_start)

        self._observer = Observer()

        # Watch the parent directory (watchdog requires watching directories)
        self._observer.schedule(self._handler, str(watch_dir), recursive=False)
        self._observer.start()

        logger.info(f"Started watching: {watch_dir}")

    def stop(self) -> None:
        """Stop watching the log file and clean up resources."""
        if self._observer is None:
            logger.debug("Watcher not running")
            return

        self._observer.stop()
        self._observer.join(timeout=5.0)
        self._observer = None
        self._handler = None

        logger.info("Watcher stopped")

    @property
    def file_position(self) -> int:
        """Current byte position in the log file.

        Useful for saving match state for recovery after restart.
        """
        if self._handler:
            return self._handler.file_position
        return 0

    def check_log_health(self) -> Optional[str]:
        """Check if the log file is growing as expected.

        Call periodically (e.g. every poll cycle) to detect no-growth conditions
        that suggest nolog mode or a wrong log path.

        Returns:
            Warning message if log hasn't grown in >120s, else None.
        """
        if not self.log_path.exists():
            return None

        try:
            current_size = self.log_path.stat().st_size
            now = time.time()

            if current_size > self._last_known_size:
                self._last_known_size = current_size
                self._last_growth_time = now
                self._no_growth_warned = False
                return None

            stale_seconds = now - self._last_growth_time
            if stale_seconds > 120 and not self._no_growth_warned:
                self._no_growth_warned = True
                msg = (
                    f"Log file has not grown in {stale_seconds:.0f}s. "
                    "MTGA may be using -nolog, a custom -logfile path, "
                    "or may not be running."
                )
                logger.warning(msg)
                return msg

        except OSError:
            pass

        return None

    def poll(self) -> None:
        """Manually poll for new log content.

        Call this periodically as a backup when watchdog events are missed.
        Safe to call even if watcher isn't running.
        """
        if self._handler:
            self._handler._read_new_content()

    def __enter__(self) -> "MTGALogWatcher":
        """Context manager entry."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.stop()
