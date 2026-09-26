"""Bounded attack search.

2026-09-24 (real match): with 7 possible attackers, optimal_attacks scored
~9M block assignments (two block searches per attacker subset) and held
the coaching thread for 69 seconds; the user had to declare attackers by
hand. The search now caps the assignments it scores across subsets.
"""

import time
from itertools import combinations

import arenamcp.combat_solver as solver


def _c(iid: int, power: int, toughness: int, text: str = "") -> dict:
    return {"instance_id": iid, "name": f"c{iid}", "power": power, "toughness": toughness, "oracle_text": text}


OURS = [_c(1, 13, 13, "Flying, trample"), _c(2, 6, 6, "Trample"), _c(3, 1, 4)] + [_c(i, 1, 1) for i in range(4, 8)]
THEIRS = [_c(11, 3, 3), _c(12, 2, 1), _c(13, 3, 3), _c(14, 2, 2), _c(15, 6, 6), _c(16, 1, 1)]
SPARE = [_c(8, 0, 3, "Reach"), _c(9, 1, 2)]


def test_seven_attacker_board_is_bounded(monkeypatch):
    solver._MEMO.clear()
    scored = []
    real = solver._search_blocks

    def counting(*args, **kwargs):
        plan = real(*args, **kwargs)
        scored.append(plan.evaluated if plan else 0)
        return plan

    monkeypatch.setattr(solver, "_search_blocks", counting)
    started = time.perf_counter()
    plan = solver.optimal_attacks(OURS, THEIRS, 20, 11, THEIRS, SPARE)
    assert plan is not None
    assert sum(scored) <= 200_000
    assert time.perf_counter() - started < 10.0  # was 69s live


def _exhaustive(candidates, blockers, opp_life, your_life, crack, spare):
    """Every subset with the original rules: highest score, lowest bitmask on ties."""
    best, best_mask = None, 0
    n = len(candidates)
    for size in range(n + 1):
        for subset in combinations(range(n), size):
            attacking = [candidates[i] for i in subset]
            held = [candidates[i] for i in range(n) if i not in subset]
            blocks = solver.optimal_blocks(attacking, blockers, opp_life)
            crackback = solver.optimal_blocks(crack, held + spare, your_life)
            plan = solver.AttackPlan(
                attacker_ids=[c["instance_id"] for c in attacking],
                damage_through=blocks.damage_through if blocks else sum(c["power"] for c in attacking),
                worst_case_crackback=crackback.damage_through if crackback else sum(c["power"] for c in crack),
                attackers_lost_material=blocks.attackers_killed_material if blocks else 0,
                blockers_killed_material=blocks.blockers_lost_material if blocks else 0,
            )
            plan.score = solver._score_attack_plan(plan, your_life, opp_life)
            mask = sum(1 << i for i in subset)
            if best is None or plan.score > best.score or (plan.score == best.score and mask < best_mask):
                best, best_mask = plan, mask
    return best


def test_small_boards_match_exhaustive_search():
    boards = [
        (OURS[:3], THEIRS[:3], 20, 20),
        (OURS[2:6], THEIRS[:2], 4, 9),
        (OURS[:4], THEIRS[3:], 13, 3),
        (OURS[3:7], [], 6, 20),
    ]
    for candidates, blockers, opp_life, your_life in boards:
        got = solver.optimal_attacks(candidates, blockers, opp_life, your_life, blockers, SPARE)
        want = _exhaustive(candidates, blockers, opp_life, your_life, blockers, SPARE)
        assert (got.attacker_ids, got.score) == (want.attacker_ids, want.score)


def test_repeat_solves_of_one_board_are_shared(monkeypatch):
    solver._MEMO.clear()
    calls = []
    real = solver._search_attacks
    monkeypatch.setattr(solver, "_search_attacks", lambda *a: calls.append(1) or real(*a))
    first = solver.optimal_attacks(OURS, THEIRS, 20, 11, THEIRS, SPARE)
    first.attacker_names.append("mutated by a caller")
    second = solver.optimal_attacks(OURS, THEIRS, 20, 11, THEIRS, SPARE)
    assert len(calls) == 1
    assert "mutated by a caller" not in second.attacker_names
    # A different board is a different solve.
    solver.optimal_attacks(OURS, THEIRS, 20, 10, THEIRS, SPARE)
    assert len(calls) == 2


def test_deadline_stops_the_search(monkeypatch):
    solver._MEMO.clear()
    clock = iter(range(0, 1000, 5))  # every clock read is 5 seconds later
    monkeypatch.setattr(solver.time, "monotonic", lambda: next(clock))
    scored = []
    real = solver.evaluate_attack
    monkeypatch.setattr(solver, "evaluate_attack", lambda *a: scored.append(1) or real(*a))
    plan = solver.optimal_attacks(OURS, THEIRS, 20, 11, THEIRS, SPARE)
    assert plan is not None  # the first subset (no attack) always gets scored
    assert len(scored) == 1
