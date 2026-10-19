"""A coach restart with our own spell on the stack is not a manual play (#62).

2026-10-19 11:14:17 the coach cast Iroh's Demonstration; at 11:14:20 it was
restarted (the #61 hotfix). At 11:14:30 the new process saw the spell on our
stack, found no submission of its own to explain it and stood down for 20 s
("manual play detected (["iroh's demonstration"]) — advise-only for 20s"); the
decision watchdog had to declare attackers with 20 s left on Arena's clock.
"""

from __future__ import annotations

import logging
import time

from tests.fakes import make_engine

LOCAL = 2


def state(names, turn=8):
    return {
        "local_seat_id": LOCAL,
        "turn": {"turn_number": turn},
        "stack": [{"name": n, "controller_seat_id": LOCAL} for n in names],
    }


def test_our_spell_already_on_the_stack_at_startup_is_inherited(caplog):
    engine = make_engine()
    with caplog.at_level(logging.INFO, logger="arenamcp.autopilot"):
        assert engine._detect_manual_play(state(["Iroh's Demonstration"])) is False
    assert not engine.in_manual_play_cooldown()
    assert "since before this coach started" in caplog.text


def test_the_log_catching_up_in_the_same_turn_is_inherited_too():
    # The first look can come before the log has replayed the cast.
    engine = make_engine()
    assert engine._detect_manual_play(state([])) is False
    assert engine._detect_manual_play(state(["Iroh's Demonstration"])) is False
    assert not engine.in_manual_play_cooldown()


def test_after_the_grace_a_new_spell_is_a_manual_play_again():
    engine = make_engine()
    engine._detect_manual_play(state(["Iroh's Demonstration"]))
    engine._started_at = time.monotonic() - 31
    assert engine._detect_manual_play(state(["Iroh's Demonstration", "Shock"])) is True
    assert engine.in_manual_play_cooldown()


def test_a_new_turn_after_startup_is_watched_at_once():
    engine = make_engine()
    engine._detect_manual_play(state([], turn=8))
    assert engine._detect_manual_play(state(["Shock"], turn=10)) is True
