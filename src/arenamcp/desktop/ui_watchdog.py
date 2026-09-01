from __future__ import annotations

import logging
import sys
import threading
import time
import traceback
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class UiAnrWatchdog(threading.Thread):
    """Background OS thread that monitors Qt main event loop responsiveness.

    Pings the main event loop every 500ms. If the main thread fails to respond
    within stall_threshold_s (default 1.5s), it captures and logs full thread
    stack traces to diagnose and isolate the exact line causing the ANR.
    """

    def __init__(
        self,
        ping_fn: Callable[[Callable[[], None]], None],
        stall_threshold_s: float = 1.5,
        check_interval_s: float = 0.5,
        dump_dir: Path | None = None,
    ) -> None:
        super().__init__(daemon=True, name="UiAnrWatchdog")
        self._ping_fn = ping_fn
        self._stall_threshold_s = stall_threshold_s
        self._check_interval_s = check_interval_s
        self._last_pong = time.time()
        self._running = False
        self._dump_dir = dump_dir or (Path.home() / ".mtgacoach" / "anr_dumps")
        self._last_dump_time = 0.0
        self._main_thread_id = threading.main_thread().ident

    def pong(self) -> None:
        """Called on the main Qt thread in response to a ping."""
        self._last_pong = time.time()

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        self._running = True
        self._last_pong = time.time()
        while self._running:
            time.sleep(self._check_interval_s)
            if not self._running:
                break

            now = time.time()
            try:
                self._ping_fn(self.pong)
            except Exception as e:
                logger.debug(f"Watchdog ping failed: {e}")
                continue

            stall = now - self._last_pong
            if stall > self._stall_threshold_s:
                if now - self._last_dump_time >= 10.0:
                    self._last_dump_time = now
                    self._dump_hung_stack(stall)

    def _dump_hung_stack(self, stall_s: float) -> None:
        """Dump all running thread call stacks to a diagnostic log."""
        try:
            self._dump_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            dump_file = self._dump_dir / f"anr_{ts}.log"

            lines = [
                "=== MTGACOACH UI THREAD ANR DETECTED ===",
                f"Timestamp: {datetime.now().isoformat()}",
                f"Stall Duration: {stall_s:.2f} seconds (threshold: {self._stall_threshold_s:.2f}s)",
                "",
                "=== MAIN THREAD CALL STACK ===",
            ]

            frames = sys._current_frames()
            if self._main_thread_id in frames:
                main_frame = frames[self._main_thread_id]
                lines.extend(traceback.format_stack(main_frame))
            else:
                lines.append("Main thread frame not available")

            lines.append("\n=== ALL ACTIVE THREADS ===")
            for tid, frame in frames.items():
                if tid != self._main_thread_id:
                    lines.append(f"\n--- Thread ID {tid} ---")
                    lines.extend(traceback.format_stack(frame))

            dump_text = "\n".join(lines)
            dump_file.write_text(dump_text, encoding="utf-8", errors="replace")

            logger.warning(
                f"UI thread stall detected ({stall_s:.2f}s) — ANR diagnostic saved to {dump_file}"
            )
        except Exception as e:
            logger.error(f"Failed to dump ANR stack trace: {e}")
