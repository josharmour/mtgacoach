"""Review 2026-10-20 of 0803998 (#198): their beginning-of-combat counters gated the root's whole
landfall step, so at their Step_BeginCombat / Step_DeclareAttack the line search lost the Ordeal of
Nylea counter and the lands it fetches (#96/#97) and called a lethal attack survivable.

Board: ``tests/race_boards_20261019.ORDEAL_T10`` (match 18b1601a, their land drop already made).
"""

from __future__ import annotations

from copy import deepcopy

import pytest
from tests.race_boards_20261019 import ORDEAL_T10

from arenamcp.board_model import build_board_model
from arenamcp.line_search import search_lines


@pytest.mark.parametrize(
    ("phase", "step"),
    [("Phase_Main1", ""), ("Phase_Combat", "Step_BeginCombat"), ("Phase_Combat", "Step_DeclareAttack")],
)
def test_their_landfall_counts_at_every_step_before_their_attack(phase, step):
    state = deepcopy(ORDEAL_T10)
    for player in state["players"]:
        if not player["is_local"]:
            player["lands_played"] = 1
    state["turn"] = {**state["turn"], "phase": phase, "step": step}
    result = search_lines(build_board_model(state), survival=True, lethal_now=False, hard_ms=1e9)
    assert result.now_life is not None and result.now_life <= 0  # their attack kills us, as it did


@pytest.mark.parametrize(
    ("event", "before", "after"),
    [
        ("at the beginning of your upkeep", 1, 0),
        ("at the beginning of each upkeep", 2, 1),
        ("at the beginning of each player's upkeep", 2, 1),
        ("at the beginning of each opponent's upkeep", 1, 1),
        ("at the beginning of your end step", 1, 1),
    ],
)
def test_after_their_upkeep_only_their_own_upkeep_drain_is_spent(event, before, after):
    """Review 2026-10-20: after their upkeep, a drain on every upkeep (or on ours) still hits us on
    our upkeep; only their own upkeep's drain is spent."""
    from arenamcp.line_search_moves import drain_triggers

    assert drain_triggers(event, {}) == before
    assert drain_triggers(event, {}, after_upkeep=True) == after
