"""A plan may cast a second copy of a card in hand on a later turn (ledger #164).

2026-10-19 14:46-14:48 (Brawl b7337658): five Hare Apparents in hand; every plan
turn after the first said "no cast" because the name lookup keeps only the
first copy and the second cast was dropped as "already cast earlier in the plan".
"""

from __future__ import annotations

from copy import deepcopy

from tests.test_strategic_plan import _brawl_plan

from arenamcp.board_assessment import assess
from arenamcp.game_plan import validate_plan


def _with_two_explorers():
    from tests.strategic_states import BRAWL_T7

    state = deepcopy(BRAWL_T7)
    explorer = next(c for c in state["hand"] if c.get("name") == "Icetill Explorer")
    twin = dict(explorer, instance_id=int(explorer.get("instance_id") or 0) + 9000)
    state["hand"].append(twin)
    return state


def test_a_second_copy_is_cast_on_a_later_turn():
    state = _with_two_explorers()
    plan = _brawl_plan(
        [
            {"turn": "T", "land": "Forest", "cast": ["Icetill Explorer"], "attack": "none"},
            {"turn": "T+1", "cast": ["Icetill Explorer"], "attack": "none"},
        ]
    )
    validate_plan(plan, assess(state), state)
    t, t1 = plan.turn_plan
    assert t["cast"] == ["Icetill Explorer"]
    assert t1["cast"] == ["Icetill Explorer"]
    assert not any("already cast earlier in the plan" in issue for issue in plan.issues)


def test_a_third_cast_with_two_copies_is_still_dropped():
    state = _with_two_explorers()
    plan = _brawl_plan(
        [
            {"turn": "T", "land": "Forest", "cast": ["Icetill Explorer"], "attack": "none"},
            {"turn": "T+1", "cast": ["Icetill Explorer"], "attack": "none"},
            {"turn": "T+2", "cast": ["Icetill Explorer"], "attack": "none"},
        ]
    )
    validate_plan(plan, assess(state), state)
    assert "T+2: dropped Icetill Explorer (already cast earlier in the plan)" in plan.issues
