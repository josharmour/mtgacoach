"""Ledger #192: the block search answers a 51-creature declare-blockers window in time.

~/.arenamcp/standalone.log 2026-10-19 15:52:14-15:52:34 (match 41b07b15 T22): "Big board (51
creatures on the battlefield): the combat solver declares", then "Coaching loop stalled for
15s" with the coaching thread in autopilot_bridge._safe_default_blocks ->
combat_strategy.safe_default_blocks -> combat_solver.optimal_blocks -> _search_blocks ->
_resolve_attacker -> _damage_order; the blocks came at 15:52:33 ("Hare Apparent blocks Myrel,
Shield of Argive" x3), the loop resumed after ~19 s. Myrel 3/4 attacked alone into 17 legal
blockers: 2^17 assignments under safe_default_blocks' 400000 cap, each a fight of up to 17
blockers (4.9 s on an idle M-series Mac, 19 s live).

Interchangeable blockers (twelve Rabbits, four Hares) now make one assignment per permutation
class on a big board, and every block search has a wall-clock deadline with a best-so-far
answer (or the greedy plan when better), logged once.
"""

from __future__ import annotations

import logging
import time

import pytest
from tests.myrel_boards_20261019 import HARES, MYREL, t22_declare_blockers

import arenamcp.combat_solver as solver
from arenamcp.combat_strategy import safe_default_blocks

pytestmark = pytest.mark.usefixtures("unlimited_search_deadline")


def _bodies(state):
    attackers = [c for c in state["battlefield"] if c.get("is_attacking")]
    legal = set(state["decision_context"]["legal_blocker_ids"])
    blockers = [c for c in state["battlefield"] if c["instance_id"] in legal]
    return attackers, blockers


def test_the_51_creature_window_declares_in_well_under_two_seconds(caplog):
    solver._MEMO.clear()
    state = t22_declare_blockers()
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="arenamcp.combat_solver"):
        choice = safe_default_blocks(state)
    assert time.monotonic() - started < 1.0
    assert "deadline" not in caplog.text  # searched to the end, not cut
    blocks, reason = choice
    # Two or more Hares kill Myrel 3/4; it kills one of them. No Rabbit is thrown in.
    assert set(blocks.values()) == {MYREL}
    assert set(blocks) <= set(HARES) and len(blocks) >= 2
    assert reason.endswith("0 damage at 25 life (no-block 3)")


def test_the_rabbits_from_different_hares_are_one_class():
    attackers, blockers = _bodies(t22_declare_blockers())
    options = [[None, 0]] * len(blockers)
    groups = solver._symmetric_groups(blockers, options)
    # Four Hares, one Human, twelve Rabbits: parent ability, creator and turn entered differ.
    assert sorted(len(group) for group in groups) == [1, 4, 12]
    assert solver._canonical_count(groups, options) == 5 * 2 * 13


def test_the_canonical_search_scores_what_the_exhaustive_one_does(monkeypatch):
    attackers, blockers = _bodies(t22_declare_blockers())
    blockers = blockers[:13]  # 2^13 assignments: exhaustive is still quick
    canonical = solver._search_blocks(attackers, blockers, 25, max_options=400_000)
    monkeypatch.setattr(solver, "BIG_BLOCK_SEARCH", 10**9)
    exhaustive = solver._search_blocks(attackers, blockers, 25, max_options=400_000)
    assert canonical.evaluated < exhaustive.evaluated
    assert (canonical.score, canonical.damage_through) == (exhaustive.score, exhaustive.damage_through)
    assert sorted(canonical.assignments.values()) == sorted(exhaustive.assignments.values())


def test_distinct_blockers_by_the_dozen_stop_at_the_deadline(monkeypatch, caplog):
    """No two bodies alike: the canonical search cannot help, the deadline does (logged once)."""
    solver._MEMO.clear()
    attacker = {"instance_id": 1, "name": "Myrel", "power": 3, "toughness": 4, "oracle_text": ""}
    blockers = [
        {
            "instance_id": 100 + i,
            "name": f"Body {i}",
            "power": 1 + i % 3,
            "toughness": 1 + i // 3,
            "oracle_text": "",
        }
        for i in range(17)
    ]
    monkeypatch.setattr(solver, "BLOCK_DEADLINE_S", 0.05)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="arenamcp.combat_solver"):
        plan = solver.optimal_blocks(
            [attacker], blockers, 20, max_options=400_000, caller="safe_default_blocks"
        )
    assert time.monotonic() - started < 1.0
    warnings = [r.getMessage() for r in caplog.records if "deadline" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].startswith(
        "block search (safe_default_blocks) hit its 0.05s deadline with 1 attackers x 17"
    )
    # The answer is never worse than the greedy plan.
    greedy = solver._greedy_block_plan([attacker], blockers, 20, blocker_allowed_attackers=None)
    assert plan.score >= greedy.score
