"""Post-match navigation never stalls silently.

2026-10-18 (standalone.log): 19:02:12.869 "Match finished; returning to Recently
Played", 19:02:12.917 "Autoplay is waiting: bring Arena to the foreground (or
leave the keyboard and mouse for 20s).", then no auto-queue line for 14 minutes
until 19:16:11.185 "observed screen=queue". Player.log kept MatchEndScene (the
Victory screen) until 19:16:09 OnExitMatchScene; no navigator click was logged.
Every 3 s the step retried the foreground check, and _status dropped the
identical detail, so the wait never reached the log.

These tests replay that sequence on a virtual clock with a fake Arena window
and fake vision answers.
"""

from __future__ import annotations

import json
import logging
import threading

import pytest
from PIL import Image
from tests.fakes import VirtualClock

import arenamcp.auto_queue as auto_queue
from arenamcp.auto_queue import AutoQueueNavigator
from arenamcp.native_mac_input import DesktopFrame, DesktopUnavailable, GameWindow

MATCH = "1e900e6e-17aa-44d0-ba64-f387c391f405"
ENDED = {"match_id": MATCH, "event_id": "Brawl_Ladder", "deck_name": "Michelangelo, Weirdness to 11"}
FOREGROUND = "Autoplay is waiting: bring Arena to the foreground (or leave the keyboard and mouse for 20s)."
BEHIND = "Arena is behind Terminal (pid 4242); keyboard/mouse used 3s ago (Arena is brought forward after 20s idle)"
WINDOW = GameWindow(7, 58967, (0, 0, 1600, 928))


def answer(screen: str, action: str, label=None, **changes) -> str:
    data = {
        "screen": screen,
        "action": action,
        "label": label,
        "point": None,
        "confidence": 0.98,
        "recent_selected": False,
        "deck_selected": False,
        "free_entry": False,
        "result_visible": False,
        "result_title": None,
        "matchmaking_visible": False,
        "queue_name": None,
        "deck_name": None,
        "reason": f"{screen} screen",
    }
    data.update(changes)
    return json.dumps(data)


VICTORY = answer("results", "dismiss_result", "VICTORY", result_visible=True, result_title="VICTORY")
HOME = answer("home", "open_play", "Play", point=[0.9, 0.93])
RECENT = answer(
    "recent",
    "start_queue",
    "Play",
    point=[0.9, 0.93],
    recent_selected=True,
    deck_selected=True,
    free_entry=True,
    queue_name="Competitive Brawl",
    deck_name="Michelangelo, Weirdness to 11",
)
MATCHMAKING = answer("queue", "wait", matchmaking_visible=True)
LOADING = answer("queue", "wait", reason="Waiting for the Server spinner")


class FakeArena:
    """Arena's window: behind another app until ``front_at``, then each click changes the screen."""

    def __init__(self, clock: VirtualClock, front_at: float = 0.0):
        self.clock = clock
        self.front_at = front_at
        self.focus_wait = ""
        self.clicks: list[tuple[float, float]] = []
        self.captures = 0
        self._shade = 0

    def capture(self) -> DesktopFrame:
        self.captures += 1
        if self.clock.now < self.front_at:
            self.focus_wait = BEHIND
            raise DesktopUnavailable(FOREGROUND)
        self.focus_wait = ""
        image = Image.new("RGB", (160, 93), (self._shade, 90, 40))
        return DesktopFrame(WINDOW, image, self.clock.now)

    def execute(self, frame, action, aborted) -> bool:
        if aborted.is_set():
            return False
        self.clicks.append(action.point)
        self._shade = (self._shade + 120) % 256  # the click visibly changed the screen
        return True


class Answers:
    """The navigation model: one scripted answer per screenshot, then the last one forever."""

    def __init__(self, *answers: str):
        self.answers = list(answers)
        self.calls = 0

    def complete_with_image(self, system, user, image, **kwargs):
        self.calls += 1
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


class Recorder(logging.Handler):
    """Auto-queue log lines stamped with virtual time."""

    def __init__(self, clock: VirtualClock):
        super().__init__(logging.INFO)
        self.clock = clock
        self.lines: list[tuple[float, str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append((self.clock.now, record.levelname, record.getMessage()))

    def matching(self, text: str) -> list[tuple[float, str, str]]:
        return [line for line in self.lines if text in line[2]]


@pytest.fixture
def clock(monkeypatch):
    clock = VirtualClock(start=10_000.0)
    monkeypatch.setattr(auto_queue, "time", clock)
    return clock


@pytest.fixture
def log(clock):
    recorder = Recorder(clock)
    logger = logging.getLogger("arenamcp.auto_queue")
    previous = logger.level
    logger.addHandler(recorder)
    logger.setLevel(logging.INFO)
    yield recorder
    logger.removeHandler(recorder)
    logger.setLevel(previous)


def navigator(arena: FakeArena, model: Answers, state: dict) -> AutoQueueNavigator:
    nav = AutoQueueNavigator(
        backend=model,
        get_game_state=lambda: state,
        controller=arena,
        bridge=False,  # no GRE bridge: the screenshot path, as on native macOS
        refine_target=lambda frame, data, action: action,
    )
    nav.set_enabled(True)
    assert nav.note_match_end(dict(ENDED), confirmed_match_end=True)
    return nav


def run(nav: AutoQueueNavigator, state: dict, clock: VirtualClock, seconds: float, tick: float = 0.5) -> None:
    """The coach loop: process_tick every ``tick`` seconds, each step finishing before the next tick."""
    end = clock.now + seconds
    while clock.now < end:
        clock.sleep(tick)
        nav.process_tick(state)
        worker = nav._worker
        if worker is not None:
            worker.join(5)
            assert not worker.is_alive()


def test_fourteen_minute_foreground_wait_is_logged_and_navigation_resumes(clock, log):
    state = {"match_id": None}
    armed = clock.now
    arena = FakeArena(clock, front_at=armed + 838.3)  # 19:02:12.917 -> 19:16:11.185
    model = Answers(VICTORY, HOME, RECENT, MATCHMAKING)
    nav = navigator(arena, model, state)

    run(nav, state, clock, 838.0)
    assert model.calls == 0 and arena.clicks == []  # Arena never came forward
    assert arena.captures > 200  # retried every ~3 s the whole time

    # Never silent: from the match end on, auto-queue logs at least once a minute.
    stamps = [armed] + [at for at, _, _ in log.lines if at <= armed + 838.0]
    assert max(later - earlier for earlier, later in zip(stamps, stamps[1:], strict=False)) <= 60.0
    still = log.matching("Auto queue still waiting")
    assert still[0][0] == pytest.approx(armed + 30.0, abs=1.0)
    assert FOREGROUND in still[0][2] and BEHIND in still[0][2]
    assert "stage=results" in still[0][2] and f"since match {MATCH} ended" in still[0][2]
    # The watchdog fires once there has been no progress for a minute...
    watchdog = log.matching("Auto queue watchdog: no navigation progress")
    assert watchdog[0][0] == pytest.approx(armed + 60.0, abs=1.0)
    assert watchdog[0][1] == "WARNING"
    assert f"since match {MATCH} ended" in watchdog[0][2] and BEHIND in watchdog[0][2]
    # ...re-observes Arena at once and says what it found.
    recheck = log.matching("Auto queue watchdog re-check")
    assert recheck and recheck[0][0] - watchdog[0][0] <= 1.0 and FOREGROUND in recheck[0][2]
    assert nav.get_debug_info()["since_progress_s"] >= 830

    # Arena comes forward (19:16:09): navigation carries on from the Victory screen.
    run(nav, state, clock, 30.0)
    assert [line[2] for line in log.matching("Auto queue clicked")] == [
        "Auto queue clicked dismiss_result 'VICTORY' at (0.500, 0.500) (estimate 0.500, 0.500; image 160x93)",
        "Auto queue clicked open_play 'Play' at (0.900, 0.930) (estimate 0.900, 0.930; image 160x93)",
        "Auto queue clicked start_queue 'Play' at (0.900, 0.930) (estimate 0.900, 0.930; image 160x93)",
    ]
    assert nav.get_debug_info()["queue_observed"]
    state["match_id"] = "next-match"
    assert not nav.process_tick(state)
    assert not nav.active


def test_watchdog_abandons_a_step_stuck_past_its_bounds(clock, log):
    state = {"match_id": None}
    arena = FakeArena(clock)
    release = threading.Event()
    calls: list[int] = []

    class StuckOnce:
        def complete_with_image(self, *args, **kwargs):
            calls.append(len(calls))
            if len(calls) == 1:
                assert release.wait(10)  # a request that ignored its timeout
                return RECENT  # a stale answer: must never become a click
            return LOADING

    nav = navigator(arena, StuckOnce(), state)
    nav.process_tick(state)
    stuck = nav._worker
    try:
        for _ in range(200):  # 100 s of coach-loop ticks while the step hangs
            clock.sleep(0.5)
            nav.process_tick(state)
            if nav._worker is not stuck and nav._worker is not None:
                nav._worker.join(5)
            if len(calls) > 1:
                break
        assert len(calls) == 2, "the watchdog never started a fresh observation"
        abandoned = log.matching("abandoning a navigation step stuck in navigation model")
        assert abandoned and abandoned[0][1] == "WARNING"
        assert abandoned[0][0] - nav._armed_at >= auto_queue._STEP_LIMIT_S
        assert log.matching("Auto queue watchdog re-check: Waiting for Arena to finish loading")
    finally:
        release.set()
        stuck.join(5)
    assert arena.clicks == []  # the abandoned step's late answer did not click
    assert nav.active and not nav.paused_reason


def test_endless_loading_screen_pauses_with_a_logged_reason(clock, log):
    state = {"match_id": None}
    arena = FakeArena(clock)
    model = Answers(LOADING)
    nav = navigator(arena, model, state)
    run(nav, state, clock, 200.0)
    assert nav.paused_reason.startswith("Arena stayed on a queue screen with nothing to click for 18")
    warning = log.matching("screenshots answered wait for")
    assert warning and warning[0][1] == "WARNING"
    calls = model.calls
    run(nav, state, clock, 120.0)
    assert model.calls == calls  # paused: no more screenshots or model requests
    # The pause is still reported while it lasts.
    assert log.matching("Auto queue still waiting")[-1][0] > warning[0][0] + 60


def test_matchmaking_is_reported_but_not_treated_as_a_stall(clock, log):
    state = {"match_id": None}
    arena = FakeArena(clock)
    model = Answers(VICTORY, HOME, RECENT, MATCHMAKING)
    nav = navigator(arena, model, state)
    run(nav, state, clock, 400.0)  # a long search for an opponent
    assert len(arena.clicks) == 3 and not nav.paused_reason
    assert not log.matching("watchdog")
    still = log.matching("Auto queue still waiting")
    assert len(still) >= 6 and all("on: Waiting for the next match" in line[2] for line in still)
