"""Regression tests for evaluation cache semantics (task 03).

The old cache signature contained only zone *lengths* and life totals: swapping
a Forest for an Island at the same hand size reproduced reuse of the same
payload object. These tests exercise that failure mode: equal snapshots must
reuse work, and semantic changes (card swap at equal size, mana change, tap
change, controller change, different match) must recompute.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from arenamcp.mcts_evaluator import MCTSEvaluator


def _base_state() -> dict[str, Any]:
    """Hero has a Forest on board and a land + spell in hand at Main1."""
    return {
        "local_seat_id": 1,
        "match_id": "match-A",
        "turn": {"turn_number": 3, "phase": "Phase_Main1", "active_player": 1, "priority_player": 1},
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20, "lands_played": 0,
             "mana_pool": {"U": 1, "G": 1}},
            {"seat_id": 2, "is_local": False, "life_total": 20, "cards_in_hand": 4},
        ],
        "battlefield": [
            {"instance_id": 1, "name": "Forest", "controller_seat_id": 1, "owner_seat_id": 1,
             "type_line": "Basic Land — Forest", "is_tapped": False},
            {"instance_id": 2, "name": "Llanowar Elves", "controller_seat_id": 1, "owner_seat_id": 1,
             "type_line": "Creature — Elf Druid", "is_tapped": False, "power": 1, "toughness": 1,
             "oracle_text": "{T}: Add {G}."},
        ],
        "hand": [
            {"instance_id": 3, "name": "Giant Growth", "type_line": "Instant", "mana_cost": "{G}"},
            {"instance_id": 4, "name": "Forest", "type_line": "Basic Land — Forest"},
        ],
    }


@pytest.fixture(autouse=True)
def _clean_cache():
    MCTSEvaluator.reset_cache()
    yield
    MCTSEvaluator.reset_cache()


def test_equal_snapshot_reuses_cached_payload_object():
    """The same snapshot must reuse the cached payload object (not a copy)."""
    state = _base_state()
    first = MCTSEvaluator.evaluate(state)
    second = MCTSEvaluator.evaluate(state)
    assert first is second


def test_same_size_card_swap_recomputes():
    """Forest->Island battlefield swap at identical zone sizes must recompute."""
    state = _base_state()
    first = MCTSEvaluator.evaluate(state)

    swapped = _base_state()
    swapped["battlefield"][0]["name"] = "Island"
    swapped["battlefield"][0]["type_line"] = "Basic Land — Island"

    second = MCTSEvaluator.evaluate(swapped)
    assert second is not first
    assert second.eval_source == first.eval_source  # still a real evaluation


def test_same_size_hand_change_recomputes():
    """Hand content change at identical hand size must recompute."""
    state = _base_state()
    first = MCTSEvaluator.evaluate(state)

    changed = _base_state()
    changed["hand"][0]["name"] = "Lightning Bolt"
    changed["hand"][0]["type_line"] = "Instant"
    changed["hand"][0]["mana_cost"] = "{R}"

    second = MCTSEvaluator.evaluate(changed)
    assert second is not first


def test_mana_change_recomputes():
    """Floated mana change must invalidate the cached payload."""
    state = _base_state()
    first = MCTSEvaluator.evaluate(state)

    more_mana = _base_state()
    more_mana["players"][0]["mana_pool"] = {"U": 3, "G": 1}

    second = MCTSEvaluator.evaluate(more_mana)
    assert second is not first


def test_tap_change_recomputes():
    """Tapping the Elves changes mana availability and must recompute."""
    state = _base_state()
    first = MCTSEvaluator.evaluate(state)

    tapped = _base_state()
    tapped["battlefield"][1]["is_tapped"] = True

    second = MCTSEvaluator.evaluate(tapped)
    assert second is not first


def test_controller_change_recomputes():
    """Stolen permanent (controller flip) must recompute."""
    state = _base_state()
    first = MCTSEvaluator.evaluate(state)

    stolen = _base_state()
    stolen["battlefield"][1]["controller_seat_id"] = 2

    second = MCTSEvaluator.evaluate(stolen)
    assert second is not first


def test_turn_change_recomputes():
    state = _base_state()
    first = MCTSEvaluator.evaluate(state)

    later = _base_state()
    later["turn"] = dict(later["turn"], turn_number=4)

    second = MCTSEvaluator.evaluate(later)
    assert second is not first


def test_phase_change_recomputes():
    state = _base_state()
    first = MCTSEvaluator.evaluate(state)

    combat = _base_state()
    combat["turn"] = dict(combat["turn"], phase="Phase_DeclareAttack")

    second = MCTSEvaluator.evaluate(combat)
    assert second is not first


def test_different_match_recomputes():
    """A new match must not inherit the previous match's cached payload."""
    state = _base_state()
    first = MCTSEvaluator.evaluate(state)

    next_match = _base_state()
    next_match["match_id"] = "match-B"

    second = MCTSEvaluator.evaluate(next_match)
    assert second is not first


def test_stack_card_identity_recomputes():
    """Stack content change (not just size) must recompute."""
    state = _base_state()
    state["stack"] = [
        {"instance_id": 9, "name": "Spell Pierce", "controller_seat_id": 2,
         "owner_seat_id": 2, "type_line": "Instant"}
    ]
    first = MCTSEvaluator.evaluate(state)

    other_spell = _base_state()
    other_spell["stack"] = [
        {"instance_id": 9, "name": "Fading Hope", "controller_seat_id": 2,
         "owner_seat_id": 2, "type_line": "Instant"}
    ]
    second = MCTSEvaluator.evaluate(other_spell)
    assert second is not first


def test_opp_hand_count_change_recomputes():
    """Opponent information change must recompute."""
    state = _base_state()
    first = MCTSEvaluator.evaluate(state)

    fewer = _base_state()
    fewer["players"][1]["cards_in_hand"] = 2

    second = MCTSEvaluator.evaluate(fewer)
    assert second is not first


def test_graveyard_identity_counts():
    """Graveyard card identity participates (cards drive gating/encoding)."""
    state = _base_state()
    state["graveyard"] = [{"instance_id": 7, "name": "Fading Hope",
                           "type_line": "Instant"}]
    first = MCTSEvaluator.evaluate(state)

    changed = _base_state()
    changed["graveyard"] = [{"instance_id": 7, "name": "Make Disappear",
                             "type_line": "Instant"}]
    second = MCTSEvaluator.evaluate(changed)
    assert second is not first


def test_tap_change_same_state_unchanged_reuses():
    """Non-semantic metadata changes must NOT invalidate (no phony misses)."""
    state = _base_state()
    first = MCTSEvaluator.evaluate(state)

    noisy = _base_state()
    noisy["battlefield"][0]["sometransientfield"] = "x"  # unknown fields ignored
    noisy["players"][1]["extra"] = 123

    second = MCTSEvaluator.evaluate(noisy)
    assert second is first


def test_ttl_expiry_recomputes(monkeypatch: pytest.MonkeyPatch):
    """After TTL expiry, equal snapshots recompute rather than reuse forever."""
    monkeypatch.setattr(MCTSEvaluator, "CACHE_TTL_SECONDS", 0.05)
    MCTSEvaluator.reset_cache()
    state = _base_state()
    first = MCTSEvaluator.evaluate(state)
    time.sleep(0.08)
    second = MCTSEvaluator.evaluate(state)
    assert second is not first


def test_ttl_disabled_reuses(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(MCTSEvaluator, "CACHE_TTL_SECONDS", 0.0)
    MCTSEvaluator.reset_cache()
    state = _base_state()
    first = MCTSEvaluator.evaluate(state)
    second = MCTSEvaluator.evaluate(state)
    assert first is second


def test_reset_clears_cache():
    """reset_cache clears signature, payload and timestamp."""
    state = _base_state()
    first = MCTSEvaluator.evaluate(state)
    MCTSEvaluator.reset_cache()
    assert MCTSEvaluator._last_sig is None
    assert MCTSEvaluator._last_payload is None
    assert MCTSEvaluator._last_payload_at == 0.0
    second = MCTSEvaluator.evaluate(state)
    assert second is not first  # recomputed, not reused


def test_force_bypasses_cache():
    state = _base_state()
    first = MCTSEvaluator.evaluate(state)
    second = MCTSEvaluator.evaluate(state, force=True)
    assert second is not first


def test_semantic_changes_are_not_phony_hits():
    """Multiple semantic changes back-to-back each produce fresh payloads."""
    a = MCTSEvaluator.evaluate(_base_state())
    b = MCTSEvaluator.evaluate({**_base_state(), "match_id": "match-Z"})
    c = MCTSEvaluator.evaluate({**_base_state(),
                                "players": [{"seat_id": 1, "is_local": True,
                                             "life_total": 19, "lands_played": 0,
                                             "mana_pool": {"U": 1, "G": 1}},
                                            {"seat_id": 2, "is_local": False,
                                             "life_total": 20, "cards_in_hand": 4}]})
    assert len({id(a), id(b), id(c)}) == 3


# ----- Checkpoint-1 review remediation -----

def test_mixed_missing_instance_ids_do_not_crash():
    """Same-name cards with mixed/missing instance IDs must evaluate cleanly.

    Regression: the old sorted raw tuples raised TypeError comparing
    NoneType/int when one Forest carried an instance_id and another did not.
    Multiplicity must be preserved (three Forests remain three).
    """
    state = _base_state()
    state["hand"].append({"name": "Forest", "type_line": "Basic Land — Forest"})
    first = MCTSEvaluator.evaluate(state)  # must not raise
    state["hand"].append({"name": "Forest", "instance_id": 7,
                          "type_line": "Basic Land — Forest", "is_tapped": False})
    second = MCTSEvaluator.evaluate(state)
    assert second is not first


def test_pending_decision_in_place_mutation_invalidates():
    """The signature snapshots pending_decision, so in-place edits invalidate."""
    state = _base_state()
    state["pending_decision"] = {"type": "choose", "options": ["A"]}
    first = MCTSEvaluator.evaluate(state)
    state["pending_decision"]["options"].append("B")  # in-place, same object
    second = MCTSEvaluator.evaluate(state)
    assert second is not first


def test_attacking_flag_invalidates():
    state = _base_state()
    first = MCTSEvaluator.evaluate(state)
    state["battlefield"][1]["is_attacking"] = True
    assert MCTSEvaluator.evaluate(state) is not first


def test_oracle_text_invalidates():
    state = _base_state()
    first = MCTSEvaluator.evaluate(state)
    state["battlefield"][1]["oracle_text"] = "Flying. Haste."
    assert MCTSEvaluator.evaluate(state) is not first


def test_stack_order_invalidates():
    """Stack is order-semantic: reversing it must invalidate the cache."""
    state = _base_state()
    state["stack"] = [
        {"name": "Spell Pierce", "instance_id": 9, "type_line": "Instant"},
        {"name": "Fading Hope", "instance_id": 10, "type_line": "Instant"},
    ]
    first = MCTSEvaluator.evaluate(state)
    state["stack"].reverse()
    assert MCTSEvaluator.evaluate(state) is not first


def test_top_level_opponent_hand_count_takes_precedence():
    """Signature mirrors evaluation precedence: top-level count wins over the
    player row, and changing it recomputes even when both fields are present."""
    state = _base_state()
    state["opponent_hand_count"] = 4  # agrees with player row cards_in_hand=4
    first = MCTSEvaluator.evaluate(state)
    state["opponent_hand_count"] = 1
    second = MCTSEvaluator.evaluate(state)
    assert second is not first


def test_opponent_hand_count_zero_vs_missing_distinct():
    """0 is a real count and must differ from missing/unknown."""
    zero = {**_base_state(), "opponent_hand_count": 0}
    missing = _base_state()
    a = MCTSEvaluator.evaluate(zero)
    b = MCTSEvaluator.evaluate(missing)
    assert a is not b
