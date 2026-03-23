"""MTGA log file watcher using watchdog.

This module provides real-time monitoring of the MTGA Player.log file,
delivering new content via callback as it's written.
"""

import os
import logging
import re
import glob
import time
from pathlib import Path
from typing import Callable, Optional

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent

logger = logging.getLogger(__name__)

# Pattern to identify match start in logs
MATCH_START_PATTERN = re.compile(
    r'\[UnityCrossThreadLogger\].*(?:MatchGameRoomStateChanged|MatchCreated|Event_Join)'
)

_WINDOWS_ABS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


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

    def find_last_match_start(self) -> int:
        """Find the byte position of the last match start in the log file.

        Scans the LAST 5MB of the log file to find the most recent MatchCreated 
        indicator. This prevents reading entire 1GB+ log files into memory which
        causes massive stuttering.

        Returns:
            Byte position to start reading from.
            If no match found in last 5MB, returns current file size (start from end/live).
        """
        if not self.log_path.exists():
            return 0

        try:
            file_size = self.log_path.stat().st_size

            # Scan last 15MB for match start — long games can generate 8-10MB of log data
            MAX_SCAN_BYTES = 15 * 1024 * 1024
            read_size = min(file_size, MAX_SCAN_BYTES)
            start_offset = max(0, file_size - read_size)

            if read_size == 0:
                return 0

            # Read tail of file as bytes
            with open(self.log_path, 'rb') as f:
                f.seek(start_offset)
                content_bytes = f.read(read_size)

            # Decode for regex matching, tracking byte positions
            content = content_bytes.decode('utf-8', errors='replace')

            # Find all match start positions (character positions)
            last_char_pos = -1
            for match in MATCH_START_PATTERN.finditer(content):
                # Find the start of this line (search backward for newline)
                line_start = content.rfind('\n', 0, match.start())
                last_char_pos = line_start + 1 if line_start != -1 else 0

            if last_char_pos >= 0:
                # Convert character position back to byte position relative to read chunk
                relevant_substring = content[:last_char_pos]
                relative_byte_pos = len(relevant_substring.encode('utf-8'))

                final_pos = start_offset + relative_byte_pos
                logger.info(f"Found last match start at byte position {final_pos} (scanned last {read_size/1024/1024:.1f}MB)")
                return final_pos
            else:
                # No match found — start from end (live mode). Parsing the entire
                # log file (can be 40MB+) causes massive startup delay for no benefit.
                logger.info(f"No match start found in last {read_size/1024/1024:.1f}MB — starting from end (live mode)")
                return file_size

        except OSError as e:
            logger.warning(f"Error scanning log for match start: {e}")
            return 0

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
        - resume_same_session: read from saved offset (fastest)
        - fresh_log: skip backfill, start from beginning (fast)
        - backfill: scan for last match start (slower but recovers state)
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

        # Use resume_offset if provided (match state recovery)
        if self._resume_offset is not None and self.log_path.exists():
            file_size = self.log_path.stat().st_size
            if self._resume_offset <= file_size:
                logger.info(f"Startup mode: resumed_session (offset {self._resume_offset})")
                self._handler.read_from_position(self._resume_offset)
            else:
                logger.warning(
                    f"Resume offset {self._resume_offset} > file size {file_size}, "
                    "falling back to backfill"
                )
                if self._backfill_enabled:
                    start_pos = self.find_last_match_start()
                    self._handler.read_from_position(start_pos)
        elif self._backfill_enabled and self.log_path.exists():
            # Fast path: skip expensive backfill scan for freshly created logs
            if self._is_fresh_log():
                logger.info("Startup mode: fresh_launch (reading from start)")
                self._handler.read_from_position(0)
            else:
                logger.info("Startup mode: backfill (scanning for last match)")
                start_pos = self.find_last_match_start()
                self._handler.read_from_position(start_pos)

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
