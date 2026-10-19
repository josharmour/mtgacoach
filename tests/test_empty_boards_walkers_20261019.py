"""Ledger #91 (2026-10-19 match c8ea2b6c T13, standalone.log 11:52:25): "empty boards" with Elspeth out.

The board facts read "AGGRESSOR — empty boards; low curve (avg MV 1.5) —
curve out and pressure" while the opponent's Elspeth, Storm Slayer (loyalty
3, a token doubler making two Soldiers a turn) was on the battlefield: the
no-creatures branch of the role ignored planeswalkers (a gap in the #42 fix,
which only added them to the board text of the other branches).
"""

from __future__ import annotations

from copy import deepcopy

from tests import race_boards_20261019 as rb

from arenamcp import board_assessment as ba

ELSPETH = {
    "instance_id": 357, "grp_id": 95526, "name": "Elspeth, Storm Slayer",
    "type_line": "Legendary Planeswalker — Elspeth", "mana_cost": "{3}{W}{W}",
    "card_types": ["CardType_Planeswalker"], "counters": {"Loyalty": 3},
    "owner_seat_id": rb.OPPONENT, "controller_seat_id": rb.OPPONENT, "turn_entered_battlefield": 11,
    "oracle_text": "If one or more tokens would be created under your control, twice that many of those tokens "
    "are created instead.\n+1: Create a 1/1 white Soldier creature token.\n0: Put a +1/+1 counter on each "
    "creature you control. Those creatures gain flying until your next turn.\n−3: Destroy target creature an "
    "opponent controls with mana value 3 or greater.",
}  # fmt: skip


def _t13(*walkers: dict) -> dict:
    """T13 on their turn: no creatures on either side, 17 vs 12 life, five Mountains and a Shock."""
    source = rb.board(
        turn=13, life=17, their_life=12, active=rb.OPPONENT, opponent_hand=3,
        ours=[(401 + i, "Mountain", False, 2 + 2 * i) for i in range(5)],
        theirs=[(501 + i, "Plains", False, 1 + 2 * i) for i in range(6)],
        hand=[(373, "Shock")],
    )  # fmt: skip
    source["battlefield"] += [deepcopy(w) for w in walkers]
    return source


def _assess(source: dict):
    ba._CACHE.clear()
    try:
        return ba.assess(deepcopy(source))
    finally:
        ba._CACHE.clear()


def test_their_planeswalker_is_not_an_empty_board():
    found = _assess(_t13(ELSPETH))
    assert "empty boards" not in found.role_reason, found.role_reason
    assert "their planeswalkers Elspeth, Storm Slayer (loyalty 3)" in found.role_reason
    assert found.role == ba.ROLE_AGGRESSOR and "pressure the planeswalker" in found.role_reason


def test_our_planeswalker_is_named_too():
    ours = {**deepcopy(rb.card(601, "Ajani Resolute", rb.LOCAL)), "counters": {"Loyalty": 5},
            "turn_entered_battlefield": 12}  # fmt: skip
    found = _assess(_t13(ours))
    assert "empty boards" not in found.role_reason, found.role_reason
    assert "our planeswalkers Ajani Resolute (loyalty 5)" in found.role_reason
    assert found.role == ba.ROLE_CONTROL


def test_no_creatures_and_no_planeswalkers_are_still_empty_boards():
    found = _assess(_t13())
    assert found.role_reason.startswith("empty boards"), found.role_reason
