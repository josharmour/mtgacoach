"""Tests for CoachEngine._plan_framing_instruction — when the hierarchical
game plan / win condition is recited aloud vs kept as silent background.

Rule: recite on the opponent's turn OR when the win strategy just changed;
otherwise (our own turn, unchanged plan) keep it silent and give only the play.
"""

from __future__ import annotations

from arenamcp.coach import CoachEngine


f = CoachEngine._plan_framing_instruction
PLAN = "GAME PLAN: win=Overwhelm with tokens | path=Develop board"


def _recites(s: str) -> bool:
    return "Lead with the concrete recommended move FIRST" in s


def _silent(s: str) -> bool:
    return "SILENT background only" in s


def test_no_plan_returns_empty():
    assert f("", our_turn=True, plan_changed=False) == ""
    assert f("", our_turn=False, plan_changed=True) == ""


def test_our_turn_unchanged_plan_is_silent():
    s = f(PLAN, our_turn=True, plan_changed=False)
    assert _silent(s) and not _recites(s)
    assert PLAN in s  # still present as background context


def test_opponent_turn_recites():
    s = f(PLAN, our_turn=False, plan_changed=False)
    assert _recites(s) and not _silent(s)


def test_plan_changed_recites_even_on_our_turn():
    s = f(PLAN, our_turn=True, plan_changed=True)
    assert _recites(s) and not _silent(s)


def test_recite_leads_with_move_not_plan_name():
    # The recite instruction must put the MOVE first (fixes the wordy
    # "Plan: ...; win: ..." preamble that buried the actual action).
    s = f(PLAN, our_turn=False, plan_changed=False)
    assert "Lead with the concrete recommended move FIRST" in s
