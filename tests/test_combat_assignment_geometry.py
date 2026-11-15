"""Assignment geometry caches contain no card data or changed search policy."""

from dataclasses import asdict
from itertools import product

import pytest
from tests.test_combat_call_key_scope import body

from arenamcp import combat_solver as cs
from arenamcp.combat_loss import LossState


def test_rows_preserve_product_touch_order_and_masks():
    options = ((None, 0, 2), (None, 1, 0), (None, 2))
    rows = cs._assignment_rows(options)
    assert [row[0] for row in rows] == list(product(*options))
    for choice, groups in rows:
        expected = {}
        for index, attacker in enumerate(choice):
            if attacker is not None:
                expected.setdefault(attacker, []).append(index)
        assert groups == tuple(
            (a, tuple(bs), (a << len(options)) | sum(1 << b for b in bs)) for a, bs in expected.items()
        )


@pytest.mark.parametrize("text", ["", "Menace", "First strike", "Infect", "Lifelink", "Trample"])
@pytest.mark.parametrize("recovery", [False, True])
def test_compiled_matches_legacy_enumeration_and_callback_sequence(monkeypatch, text, recovery):
    attackers = [body(1, text, 5), body(2, "Double strike"), body(3, "Flying")]
    blockers = [body(4, "Reach"), body(5, "Lifelink"), body(6, "Deathtouch")]
    calls = []
    kwargs = dict(
        defender=LossState(9, 1),
        deadline_s=1e9,
        recovery_credit=(lambda lost: calls.append(lost) or len(lost)) if recovery else None,
    )
    actual = cs._search_blocks(attackers, blockers, 6, **kwargs)
    actual_calls = list(calls)
    calls.clear()
    monkeypatch.setattr(cs, "_ASSIGNMENT_COMPILE_LIMIT", 0)
    expected = cs._search_blocks(attackers, blockers, 6, **kwargs)
    assert asdict(actual) == asdict(expected)
    assert calls == actual_calls


def test_cache_total_row_bound_and_card_independence(monkeypatch):
    with cs._ASSIGNMENT_LOCK:
        cs._ASSIGNMENT_CACHE.clear()
        cs._assignment_row_count = 0
    monkeypatch.setattr(cs, "_ASSIGNMENT_ROW_LIMIT", 10)
    for i in range(20):
        cs._assignment_rows(((None, i), (None, i + 1)))
        assert cs._assignment_row_count <= 10
        assert cs._assignment_row_count == sum(map(len, cs._ASSIGNMENT_CACHE.values()))
    assert all(isinstance(key, tuple) for key in cs._ASSIGNMENT_CACHE)
    with cs._ASSIGNMENT_LOCK:
        cs._ASSIGNMENT_CACHE.clear()
        cs._assignment_row_count = 0


def test_cold_geometry_time_counts_toward_existing_deadline(monkeypatch):
    now = [0.0]
    original = cs._assignment_rows

    def cold(options):
        result = original(options)
        now[0] += 5.0
        return result

    monkeypatch.setattr(cs.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(cs, "_assignment_rows", cold)
    attackers = [body(i + 1) for i in range(4)]
    blockers = [body(i + 10) for i in range(4)]
    plan = cs._search_blocks(attackers, blockers, deadline_s=1.0)
    assert plan.evaluated == 255


@pytest.mark.parametrize(
    "outer",
    [
        None,
        {},
        {"controller_seat_id": 0},
        {"controller_seat_id": 2},
        {"_source_controller_seat_id": 1, "controller_seat_id": 2},
        {"_source_controller_seat_id": "", "controller_seat_id": "2"},
    ],
)
@pytest.mark.parametrize(
    "inner",
    [
        None,
        {},
        {"controller_seat_id": 0},
        {"controller_seat_id": 3},
        {"_source_controller_seat_id": 4, "controller_seat_id": 3},
    ],
)
def test_controller_key_preserves_all_original_precedence(outer, inner):
    card = dict(outer or {})
    card["_card"] = inner
    expected = None
    for obj in (card, inner if isinstance(inner, dict) else {}):
        for field in ("_source_controller_seat_id", "controller_seat_id"):
            if obj.get(field):
                expected = obj[field]
                break
        if expected:
            break
    assert cs._controller_key(card) == expected


def test_private_keyword_alias_is_canonical_function():
    assert cs._has is cs.has_combat_keyword


@pytest.mark.parametrize("limit", [1, 50000])
@pytest.mark.parametrize("callback", [False, True])
def test_assignment_only_mode_preserves_selection_fallback_and_callbacks(limit, callback):
    attackers = [body(1, "Trample", 5), body(2, "First strike")]
    blockers = [body(3, "Lifelink"), body(4, "Deathtouch")]
    calls = []
    options = dict(
        max_options=limit,
        deadline_s=1e9,
        recovery_credit=(lambda lost: calls.append(lost) or len(lost)) if callback else None,
    )
    full = cs._search_blocks(attackers, blockers, **options)
    expected_calls = list(calls)
    calls.clear()
    minimal = cs._search_blocks(attackers, blockers, _only_assignments=True, **options)
    assert minimal.assignments == full.assignments
    assert minimal.evaluated == full.evaluated
    assert calls == expected_calls
    assert (
        isinstance(minimal, cs.BlockPlan)
        if callback or limit == 1
        else isinstance(minimal, cs._BlockAssignments)
    )


def test_assignment_only_deadline_keeps_full_greedy_comparison(monkeypatch):
    now = [0]

    def clock():
        now[0] += 1
        return now[0]

    monkeypatch.setattr(cs.time, "monotonic", clock)
    attackers = [body(i + 1) for i in range(4)]
    blockers = [body(i + 10) for i in range(4)]
    full = cs._search_blocks(attackers, blockers, deadline_s=0)
    now[0] = 0
    minimal = cs._search_blocks(attackers, blockers, deadline_s=0, _only_assignments=True)
    assert isinstance(minimal, cs.BlockPlan)
    assert asdict(minimal) == asdict(full)
