"""The coaching loop names its blocker when it stalls.

2026-09-24: the loop froze twice for ~70s inside the attack solver; the
log showed only a silent gap between two timestamps.
"""

import logging
import threading
import time

from arenamcp.standalone import StandaloneCoach


def test_stall_is_logged_with_the_blocking_frame(caplog):
    coach = StandaloneCoach.__new__(StandaloneCoach)
    coach._LOOP_STALL_WARN_S = 0.3
    coach._LOOP_STALL_POLL_S = 0.05
    coach._running = True
    release = threading.Event()

    def slow_attack_search():
        release.wait(5.0)

    def loop():
        coach._loop_heartbeat = time.monotonic()
        slow_attack_search()
        while coach._running:  # the loop keeps cycling once unblocked
            coach._loop_heartbeat = time.monotonic()
            time.sleep(0.02)

    coach._loop_heartbeat = None
    coach._coaching_thread = threading.Thread(target=loop, daemon=True)
    with caplog.at_level(logging.WARNING, logger="arenamcp.standalone"):
        coach._coaching_thread.start()
        watchdog = threading.Thread(target=coach._coaching_stall_watchdog, daemon=True)
        watchdog.start()
        deadline = time.monotonic() + 3.0
        while "stalled for" not in caplog.text and time.monotonic() < deadline:
            time.sleep(0.05)
        release.set()
        deadline = time.monotonic() + 3.0
        while "resumed after" not in caplog.text and time.monotonic() < deadline:
            time.sleep(0.05)
        coach._running = False
        watchdog.join(1.0)

    assert "Coaching loop stalled for" in caplog.text
    assert "slow_attack_search" in caplog.text
    assert "Coaching loop resumed after" in caplog.text
