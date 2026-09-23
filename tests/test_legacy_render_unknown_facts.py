#!/usr/bin/env python3
"""Task 11 — the legacy renderer must not assert missing facts.

With ``legacy_render=True`` the formatter renders unknown fields as unknown
(never as fabricated facts) while known values, including empty/zero, still
render as facts; ``legacy_render=False`` fails hard on declared-unknown state.

Production live-state formatting is unchanged: with legacy_render unset the
formatter keeps its previous opinionated behaviour.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for p in (str(REPO / "src"), str(REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ---------------------------------------------------------------------------
# Known values still render as facts (empty/zero ≠ unknown)
# ---------------------------------------------------------------------------


def _gs(players=None, stack=None, active=None):
    return {
        "players": players
        or [
            {"seat_id": 1, "is_local": True, "life_total": 15, "lands_played": 0,
             "hand_size": 2},
            {"seat_id": 2, "is_local": False, "life_total": 12, "lands_played": 0,
             "hand_size": 0},
        ],
        "turn": {"turn_number": 3, "phase": "Phase_Main1", "step": "",
                 "active_player": active if active is not None else 1,
                 "priority_player": 1},
        "battlefield": [],
        "hand": [],
        "stack": stack if stack is not None else [{"name": "Counterspell"}],
        "graveyard": [],
        "legal_actions": [],
    }


def test_known_zero_hand_renders_as_zero_fact():
    engine = _engine()
    out = engine._format_game_context(_gs(), legacy_render=True)
    assert "Opp hand: 0 card(s)" in out


def test_missing_hand_renders_as_unknown():
    players = _gs()["players"]
    del players[1]["hand_size"]
    engine = _engine()
    out = engine._format_game_context(_gs(players=players), legacy_render=True)
    assert "Opp hand: UNKNOWN" in out


def test_production_opinion_renders_no_opp_hand_line():
    """Without legacy_render the formatter unchanged: no Opp hand line at all."""
    engine = _engine()
    out = engine._format_game_context(_gs())
    assert "Opp hand" not in out
    out2 = engine._format_game_context(_gs(), legacy_render=True)
    assert "Opp hand: 0 card(s)" in out2


def test_nonempty_stack_renders_under_legacy_mode():
    engine = _engine()
    out = engine._format_game_context(_gs(stack=[{"name": "Counterspell"}]), legacy_render=True)
    assert "STACK (top resolves first):" in out and "Counterspell" in out


def test_strict_mode_refuses_unknown_active_player():
    engine = _engine()
    gs = _gs()
    gs["_render_unknown"] = {"active_player": True}
    gs["turn"]["active_player"] = None
    with pytest.raises(AssertionError):
        engine._format_game_context(gs, legacy_render=False)


def test_strict_mode_allows_established_state():
    engine = _engine()
    out = engine._format_game_context(_gs(), legacy_render=False)
    assert "T3 YOUR" in out


# ---------------------------------------------------------------------------
# Production-formatter invariance (default stays byte-identical)
# ---------------------------------------------------------------------------


def _engine():
    from arenamcp.coach import CoachEngine

    return CoachEngine.__new__(CoachEngine)


def test_production_render_is_byte_identical_with_and_without_marker():
    engine = _engine()
    gs = _gs()
    marked = dict(gs)
    marked["_render_unknown"] = {"stack": True, "active_player": True,
                                 "opponent_hand_size": True,
                                 "turn_entered_battlefield": True}
    a = engine._format_game_context(gs)
    b = engine._format_game_context(marked)
    assert a == b  # marker alone must not change the production output
