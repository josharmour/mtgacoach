"""Temporary macOS idle-sleep protection for active continuous autoplay."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading

logger = logging.getLogger(__name__)


class SleepInhibitor:
    """Hold a process-scoped idle/display assertion without changing settings.

    The caller enables this only while continuous autoplay is active. The
    assertion also ends if the owning coach process exits unexpectedly.
    """

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        """Ensure one assertion is active; return false when unavailable."""
        if sys.platform != "darwin":
            return False
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return True
            try:
                self._process = subprocess.Popen(
                    ["/usr/bin/caffeinate", "-di", "-w", str(os.getpid())],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as exc:
                self._process = None
                logger.warning("Could not prevent idle sleep during auto-queue: %s", exc)
                return False
            return True

    def stop(self) -> None:
        """Release the assertion, escalating only our retained helper process."""
        with self._lock:
            process = self._process
            if process is None:
                return
            if process.poll() is not None:
                self._process = None
                return
            try:
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1.0)
                self._process = None
            except ProcessLookupError:
                self._process = None
            except (OSError, subprocess.TimeoutExpired) as exc:
                # Keep the handle so a later stop can retry without spawning
                # another helper. -w still releases it when this coach exits.
                logger.warning("Could not release idle-sleep helper yet: %s", exc)
