"""2026-10-11 10:52: a cancelled deck analysis must stop, and the game plan must not wait on it for ever."""

from __future__ import annotations

import time
from unittest.mock import Mock

import pytest

from arenamcp.coach import CoachEngine


def _state():
    return {
        "deck_cards": [100] * 60,
        "commander_grp_ids": [],
        "players": [{"seat_id": 1, "is_local": True}],
        "local_seat_id": 1,
        "match_id": "m1",
    }


def test_cancelled_analysis_stops_instead_of_retrying_for_the_match(monkeypatch, tmp_path):
    coach = CoachEngine(backend=Mock())
    monkeypatch.setattr(
        "arenamcp.match_context._local_card",
        lambda gid, epoch: {"name": "Card", "type_line": "Creature", "oracle_text": "Flying"},
    )
    monkeypatch.setattr(
        coach, "_playbook_cache_path", staticmethod(lambda identity: tmp_path / f"{identity}.json")
    )
    monkeypatch.setattr(coach, "_await_model", lambda *a, **k: None)
    sleeps: list[float] = []
    monkeypatch.setattr("arenamcp.coach.time.sleep", lambda s: sleeps.append(s))
    backend = Mock()

    def cancelled(*args, **kwargs):
        kwargs["cancel_event"].set()  # the newer analysis cancels this one mid-call
        return "[BACKEND ERROR] request cancelled (cancelled)"

    backend.complete.side_effect = cancelled
    with pytest.raises(ValueError, match="cancelled"):
        coach.prepare_playbook(_state(), backend=backend)
    assert backend.complete.call_count == 1
    assert sleeps == []


def test_plan_waits_for_the_deck_analysis_only_briefly():
    coach = CoachEngine(backend=Mock())
    assert coach._deck_analysis_blocks_plan() is False
    coach.begin_deck_analysis("abc")
    assert coach._deck_strategy_pending and coach._deck_analysis_blocks_plan() is True
    coach._deck_strategy_pending_since = time.monotonic() - 60
    assert coach._deck_analysis_blocks_plan() is False
    coach.clear_deck_strategy()
    assert coach._deck_analysis_blocks_plan() is False


def test_autopilot_game_plan_gate_waits_only_briefly_for_deck_analysis():
    """#205: the autopilot's plan gate must be the coach's bounded one.

    Suspending background plan work for as long as ``_deck_strategy_pending``
    (a 74 s playbook build with one validation retry) left the first match on
    a changed deck list with no game-plan attempt on turns 3-5 at all.
    """
    from arenamcp.standalone import StandaloneCoach

    engine = CoachEngine(backend=Mock())
    standalone = StandaloneCoach.__new__(StandaloneCoach)
    standalone._coach = engine
    gate = standalone._game_plan_background_suspended
    assert gate() is False
    engine.begin_deck_analysis("abc")
    assert gate() is True  # the analysis just started: give it a moment
    engine._deck_strategy_pending_since = time.monotonic() - 60
    assert gate() is False  # the plan forms while the playbook keeps building
    standalone._coach = None
    assert gate() is False


def test_autopilot_init_wires_the_bounded_game_plan_gate():
    """Guard against reintroducing the unbounded ``_deck_strategy_pending`` lambda."""
    import inspect

    from arenamcp.standalone import StandaloneCoach

    source = inspect.getsource(StandaloneCoach._init_autopilot)
    assert "_game_plan_background_suspended" in source
    assert "background_suspended_fn = lambda" not in source
