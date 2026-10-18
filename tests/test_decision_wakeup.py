"""A new decision reaches the coaching loop in milliseconds, not after its poll sleep.

2026-10-11 latency audit: on macOS the default FSEvents observer never fired
on Player.log appends (Unity keeps the file open), so the log was read only by
the coaching loop's poll_log(); the loop slept 0.15-2.5 s between polls and
fetched the bridge's pending request only between those sleeps.
"""

import sys
import threading
import time
from types import SimpleNamespace

import pytest

from arenamcp import server, watcher
from arenamcp.gre_bridge import BridgeDecisionPoller
from arenamcp.standalone import StandaloneCoach

# --- watcher -----------------------------------------------------------------


def test_macos_watches_player_log_with_kqueue(monkeypatch):
    from watchdog.observers.kqueue import KqueueObserver

    monkeypatch.setattr(watcher.sys, "platform", "darwin")
    observer = watcher._make_observer()
    assert isinstance(observer, KqueueObserver)


def test_other_platforms_keep_the_default_observer(monkeypatch):
    monkeypatch.setattr(watcher.sys, "platform", "win32")
    sentinel = object()
    monkeypatch.setattr(watcher, "Observer", lambda: sentinel)
    assert watcher._make_observer() is sentinel


@pytest.mark.skipif(sys.platform != "darwin", reason="kqueue is the macOS observer")
def test_appends_to_an_open_log_are_delivered_without_polling(tmp_path):
    log = tmp_path / "Player.log"
    log.write_text("start\n")
    chunks: list[str] = []
    got = threading.Event()

    def callback(text: str) -> None:
        chunks.append(text)
        if "GREMessageType_ActionsAvailableReq" in "".join(chunks):
            got.set()

    w = watcher.MTGALogWatcher(callback=callback, log_path=str(log), backfill=False)
    w.start()
    try:
        with open(log, "a", encoding="utf-8") as held_open:  # Unity never closes it
            time.sleep(0.2)  # let the observer arm
            held_open.write('{"type": "GREMessageType_ActionsAvailableReq"}\n')
            held_open.flush()
            assert got.wait(2.0), "the observer never delivered the append"
    finally:
        w.stop()


def test_observer_and_poll_never_deliver_a_chunk_twice(tmp_path):
    log = tmp_path / "Player.log"
    log.write_text("")
    seen: list[str] = []
    handler = watcher.MTGALogHandler(str(log), lambda text: (time.sleep(0.001), seen.append(text)))
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            handler._read_new_content()

    threads = [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    with open(log, "a", encoding="utf-8") as f:
        for i in range(200):
            f.write(f"line {i}\n")
            f.flush()
    time.sleep(0.05)
    stop.set()
    for t in threads:
        t.join()
    handler._read_new_content()
    assert "".join(seen) == "".join(f"line {i}\n" for i in range(200))


# --- server: the log wakes the loop for a new request --------------------------


def test_a_gre_request_in_the_log_wakes_the_coaching_loop(monkeypatch):
    parsed: list[str] = []
    monkeypatch.setattr(server.parser, "process_chunk", parsed.append)
    server.loop_wake.clear()
    server._process_log_chunk('{"type": "GREMessageType_GameStateMessage"}')
    assert not server.loop_wake.is_set()
    server._process_log_chunk('{"type": "GREMessageType_DeclareAttackersReq"}')
    assert server.loop_wake.is_set() and len(parsed) == 2
    server.loop_wake.clear()


# --- coaching loop sleep ------------------------------------------------------------


def _coach_with_wake(event):
    coach = StandaloneCoach.__new__(StandaloneCoach)
    coach._mcp = SimpleNamespace(wake_event=event)
    return coach


def test_a_wake_ends_the_poll_sleep_early_but_keeps_a_minimum_gap():
    wake = threading.Event()
    coach = _coach_with_wake(wake)
    threading.Timer(0.02, wake.set).start()
    started = time.monotonic()
    assert coach._wait_for_wake(2.0) is True
    elapsed = time.monotonic() - started
    assert StandaloneCoach._WAKE_MIN_GAP_S - 0.005 <= elapsed < 0.5
    assert not wake.is_set()  # consumed by the coaching thread


def test_without_a_wake_event_the_loop_sleeps_as_before():
    coach = _coach_with_wake(None)
    started = time.monotonic()
    assert coach._wait_for_wake(0.03) is False
    assert time.monotonic() - started >= 0.025


# --- bridge prefetch thread --------------------------------------------------


class _Bridge:
    connected = True

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get_pending_actions(self):
        self.calls += 1
        return self.responses[min(self.calls - 1, len(self.responses) - 1)]

    def connect(self):
        return True

    def resolve_pending_dynamic_cards(self):
        pass

    def prewarm_grp_ids(self, ids):
        pass


IDLE = {"ok": True, "has_pending": False}
ATTACK = {"ok": True, "has_pending": True, "request_type": "DeclareAttackers", "game_state_id": 7, "msg_id": 3}


def test_a_changed_request_wakes_the_loop_and_tells_watchers_once():
    bridge = _Bridge([IDLE, IDLE, ATTACK, ATTACK])
    poller = BridgeDecisionPoller(bridge)
    wake = threading.Event()
    poller._prefetch_wake = wake
    heard: list[dict] = []
    unsubscribe = poller.watch_requests(heard.append)
    assert poller._prefetch_once() is True  # first sight counts as a change
    wake.clear()
    heard.clear()
    assert poller._prefetch_once() is False and not wake.is_set()
    assert poller._prefetch_once() is True and wake.is_set() and heard == [ATTACK]
    assert poller._prefetch_once() is False and heard == [ATTACK]
    unsubscribe()
    assert poller._request_listeners == []


def test_poll_reuses_a_fresh_prefetch_and_asks_again_when_it_is_stale():
    bridge = _Bridge([ATTACK])
    poller = BridgeDecisionPoller(bridge)
    poller._prefetch_once()
    assert bridge.calls == 1
    trigger = poller.poll()
    assert trigger and trigger["trigger"] == "decision_required" and bridge.calls == 1
    poller._prefetched = (time.monotonic() - 1.0, ATTACK)
    poller.poll()
    assert bridge.calls == 2


def test_the_prefetch_thread_notices_a_new_request_within_a_few_ticks():
    bridge = _Bridge([IDLE] * 3 + [ATTACK])
    poller = BridgeDecisionPoller(bridge)
    wake = threading.Event()
    poller.start_prefetch(wake)
    try:
        assert wake.wait(1.0)
        wake.clear()
        assert wake.wait(1.0)  # IDLE -> ATTACK after ~3 ticks of 50 ms
        assert poller._prefetch_fp[1] == "DeclareAttackers"
    finally:
        poller.stop_prefetch()
    assert poller._prefetch_thread is None


def test_a_disconnected_bridge_is_not_polled_by_the_prefetch_thread():
    bridge = _Bridge([ATTACK])
    bridge.connected = False
    poller = BridgeDecisionPoller(bridge)
    assert poller._prefetch_once() is False and bridge.calls == 0
