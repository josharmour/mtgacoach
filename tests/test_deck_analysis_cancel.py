"""2026-10-11 10:52: a cancelled deck analysis must stop, and the game plan must not wait on it for ever."""

from __future__ import annotations

import threading
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
    assert isinstance(threading.Event(), threading.Event)
