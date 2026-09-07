#!/usr/bin/env python3
"""Task 11 — the legacy renderer must not assert missing facts.

Golden fixtures built from synthetic MageZero decision rows assert that a
rendered training record contains NO affirmative false fact for a field the
MZ log does not establish (stack contents, active/priority player, card
costs/CMC, opponent hand size, battlefield entry turns), while the legal
menu is rendered exactly once and the chosen answer index is unchanged.

Production live-state formatting is unchanged: with legacy_render unset the
formatter keeps its previous opinionated behaviour (asserted by the
production par).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for p in (str(REPO / "src"), str(REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)

from tools.training import build_magezero_bridge as BRIDGE  # noqa: E402
from tools.training import gate_play_decisions as G  # noqa: E402

# Codes that may NOT appear in a golden prompt for a missing field.
FORBIDDEN_FABRICATIONS = ("all", "[ok,x=0]", "[ok, x=0]")


def _row(**overrides) -> dict:
    row = {
        "game_id": "gameL11.log:Thread-1:001",
        "turn": 5,
        "phase": "PRECOMBAT_MAIN",
        "active_life": 18,
        "opp_life": 14,
        "hand": ["Flowfarm Verge"],
        "battlefield_self": [{"name": "Skrelv, Defector Mite", "tapped": False}],
        "battlefield_opp": [{"name": "Plains", "tapped": False}],
        "menu": ["Pass", "Play Land: Flowfarm Verge"],
        "chosen": "Pass",
        "mcts_counts": {"Pass": 30, "Play Land: Flowfarm Verge": 609},
        "actor": "PlayerB",
        "outcome": "won",
        "session": "s11",
        "decision_kind": "priority",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# 1. build_game_state represents missing facts as unknown/omitted
# ---------------------------------------------------------------------------


def test_game_state_carries_no_fabricated_facts():
    gs = BRIDGE.build_game_state(_row())
    # no empty-stack assertion is attributable: the declaration is present
    assert gs["_render_unknown"]["stack"] is True
    assert gs["_render_unknown"]["active_player"] is True
    assert gs["_render_unknown"]["opponent_hand_size"] is True
    assert gs["_render_unknown"]["turn_entered_battlefield"] is True
    # a MISSING stack is represented as unknown, not asserted as empty
    assert gs["stack"] == []
    # no local priority is established
    assert gs["turn"]["active_player"] is None
    assert gs["turn"]["priority_player"] is None
    # opponent hand size is omitted, not asserted as 0
    assert "hand_size" not in gs["players"][1]


def test_battlefield_cards_carry_no_fresh_etb_fact():
    gs = BRIDGE.build_game_state(_row())
    for card in gs["battlefield"]:
        assert "turn_entered_battlefield" not in card, card
        assert card.get("_etb_turn_known") is not True


def test_hand_card_unknown_cost_is_none_not_zero():
    gs = BRIDGE.build_game_state(_row(hand=["Floodfarm Verge"]))
    card = gs["hand"][0]
    assert card["mana_cost"] is None
    assert "cmc" not in card


# ---------------------------------------------------------------------------
# 2. Golden rendered record — no affirmative false facts for missing fields
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def golden_record():
    record, reason = BRIDGE.build_record(_row())
    assert record is not None, reason
    return record


def test_golden_prompt_present(golden_record):
    assert golden_record["user"].strip() != ""


def test_golden_no_stack_fabrication(golden_record):
    # with an unknown stack, the timing line must not assert an empty stack
    user = golden_record["user"]
    assert not any(s in user.lower() for s in FORBIDDEN_FABRICATIONS)
    assert "Stack:" not in user and "STACK" not in user


def test_golden_no_active_player_fabrication(golden_record):
    user = golden_record["user"]
    assert "T5 UNKNOWN" in user
    assert "T5 YOU" not in user and "T5 OPP" not in user
    assert "Pri:UNKNOWN" in user


def test_golden_no_castability_fabrication(golden_record):
    # the hand card's cost conclusion must derive from a resolved card source
    # (never a fabricated zero CMC): an unknown-cost non-land renders CAST?,
    # a resolved card renders its true affordance tag — never [OK,X=0]
    user = golden_record["user"]
    assert "[OK" not in user and "CAST?" not in user
    assert "[S,HOLD]" in user


def test_golden_no_opp_hand_fabrication(golden_record):
    # opponent hand size is not established: explicit UNKNOWN, never zero
    assert "Opp hand: UNKNOWN" in golden_record["user"]
    assert "Opp hand: 0" not in golden_record["user"]


def test_golden_no_fresh_etb_fabrication(golden_record):
    user = golden_record["user"]
    assert "SS" not in user  # summoning-sickness flag needs entry-turn facts
    assert "Land: " in user
    assert "Land: UNKNOWN (active player unknown)" in user


def test_golden_menu_and_answer_unchanged(golden_record):
    user, meta = golden_record["user"], golden_record["meta"]
    assert user.count("Legal: (pick by number)") == 1
    assert "1. Pass" in user and "2. Play Land: Flowfarm Verge" in user
    assert meta["answer_pick"] == 1
    assert json.loads(golden_record["response"]) == {"actions": [{"pick": 1}]}


# ---------------------------------------------------------------------------
# 3. Known values still render as facts (empty/zero ≠ unknown)
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
# 4. Production-formatter invariance (default stays byte-identical)
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


def test_bridge_strict_renderer_fails_closed_on_mz_state():
    """Strict opinion refuses the MZ state instead of rendering it."""
    import tools.training.build_magezero_bridge as B

    gs = B.build_game_state(_row())
    engine = _engine()
    with pytest.raises(AssertionError):
        engine._format_game_context(gs, legacy_render=False)


def test_annotate_mode_mana_line_on_mz_state():
    engine = _engine()
    gs = BRIDGE.build_game_state(_row())
    out = engine._format_game_context(gs, legacy_render=True)
    assert "Mana: " in out
    # Skrelv's mana ability is unresolvable — pool total is supported by no
    # evidence, so the assumed label must be present
    assert "(assumed pool — sources may be missing)" in out
