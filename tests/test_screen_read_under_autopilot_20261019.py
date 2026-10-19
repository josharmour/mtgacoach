"""The screenshot read never runs or speaks beside an autopilot pick (ledger #81, match 772233af).

2026-10-19 11:35:24.224 (standalone.log): "Generic selection still unresolved
(Select Items/select_n) — triggering screenshot analysis" for Unscrupulous
Agent's hand exile, while the autopilot answered the same SelectN. The read took
3994 ms and was spoken at 11:35:29.042 — "Exile one of your duplicate Tectonic
Hazards" — 18 ms after the autopilot had exiled Shock. #24 (0cbbde2) exempted
only mulligan bottom, scry and surveil. Coaching without the autopilot keeps
the read.
"""

from __future__ import annotations

from tests.test_screen_analysis import _engine, capture  # noqa: F401  (pytest fixture)

from arenamcp import standalone


class _DummyMcp:
    def __init__(self, next_state: dict) -> None:
        self._next_state = next_state

    def poll_log(self) -> None:
        pass

    def get_game_state(self) -> dict:
        return self._next_state


SELECT_N = {"pending_decision": "Select Cards", "decision_context": {"type": "select_n", "source_id": 353}}


def _coach(drives: bool):
    coach = standalone.StandaloneCoach.__new__(standalone.StandaloneCoach)
    coach._mcp = _DummyMcp(dict(SELECT_N))
    coach._normalize_turn_snapshot = lambda state: state
    queued: list[str] = []
    coach._queue_screenshot_analysis = lambda: queued.append("queued")
    seen: list[dict | None] = []

    def autopilot_drives(state=None):
        seen.append(state)
        return drives

    coach._autopilot_drives = autopilot_drives
    return coach, queued, seen


def test_the_autopilots_selection_gets_no_screen_read(monkeypatch):
    monkeypatch.setattr(standalone.time, "sleep", lambda _seconds: None)
    coach, queued, seen = _coach(drives=True)
    refreshed, should_stop = coach._handle_unresolved_generic_selection(dict(SELECT_N))
    assert should_stop is False and queued == []
    assert refreshed["decision_context"]["type"] == "select_n"
    assert seen and seen[-1] is refreshed  # judged on the refreshed window


def test_coaching_without_the_autopilot_keeps_the_screen_read(monkeypatch):
    monkeypatch.setattr(standalone.time, "sleep", lambda _seconds: None)
    coach, queued, _seen = _coach(drives=False)
    _refreshed, should_stop = coach._handle_unresolved_generic_selection(dict(SELECT_N))
    assert should_stop is True and queued == ["queued"]


def test_the_queued_read_is_marked_automatic(monkeypatch):
    calls: list[dict] = []

    class _Thread:
        def __init__(self, target, kwargs=None, daemon=None, **_extra):
            self._target, self._kwargs = target, kwargs or {}

        def start(self):
            self._target(**self._kwargs)

    monkeypatch.setattr(standalone.threading, "Thread", _Thread)
    coach = standalone.StandaloneCoach.__new__(standalone.StandaloneCoach)
    coach.take_screenshot_analysis = lambda **kwargs: calls.append(kwargs)
    coach._queue_screenshot_analysis()
    assert calls == [{"automatic": True}]


def _speaking_engine(drives: bool):
    engine = _engine()
    engine._auto_speak = True
    spoken: list[str] = []
    engine.speak_advice = lambda text, blocking=True: spoken.append(text)
    engine._autopilot_drives = lambda state=None: drives
    return engine, spoken


def test_an_automatic_read_that_lands_under_the_autopilot_is_not_spoken(capture):  # noqa: F811
    engine, spoken = _speaking_engine(drives=True)
    engine.take_screenshot_analysis(automatic=True)
    assert spoken == [] and engine.ui.advice_calls == []
    assert any("not spoken" in line for line in engine.ui.logs)


def test_the_users_own_analyze_button_still_answers(capture):  # noqa: F811
    engine, spoken = _speaking_engine(drives=True)
    engine.take_screenshot_analysis()
    assert engine.ui.advice_calls == [("KEEP: two lands and a two-drop.", "Visual Analysis")]
    assert spoken == ["KEEP: two lands and a two-drop."]


def test_an_automatic_read_while_coaching_is_spoken(capture):  # noqa: F811
    engine, spoken = _speaking_engine(drives=False)
    engine.take_screenshot_analysis(automatic=True)
    assert spoken == ["KEEP: two lands and a two-drop."]
