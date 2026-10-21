"""Regression tests for evaluation cache semantics (task 03).

The old cache signature contained only zone *lengths* and life totals: swapping
a Forest for an Island at the same hand size reproduced reuse of the same
payload object. These tests exercise that failure mode: equal snapshots must
reuse work, and semantic changes (card swap at equal size, mana change, tap
change, controller change, different match) must recompute.
"""

from __future__ import annotations

from collections.abc import Callable
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
            {
                "seat_id": 1,
                "is_local": True,
                "life_total": 20,
                "lands_played": 0,
                "mana_pool": {"U": 1, "G": 1},
            },
            {"seat_id": 2, "is_local": False, "life_total": 20, "cards_in_hand": 4},
        ],
        "battlefield": [
            {
                "instance_id": 1,
                "name": "Forest",
                "controller_seat_id": 1,
                "owner_seat_id": 1,
                "type_line": "Basic Land — Forest",
                "is_tapped": False,
            },
            {
                "instance_id": 2,
                "name": "Llanowar Elves",
                "controller_seat_id": 1,
                "owner_seat_id": 1,
                "type_line": "Creature — Elf Druid",
                "is_tapped": False,
                "power": 1,
                "toughness": 1,
                "oracle_text": "{T}: Add {G}.",
            },
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


def _spell(name: str, iid: int) -> dict[str, Any]:
    return {
        "instance_id": iid,
        "name": name,
        "controller_seat_id": 2,
        "owner_seat_id": 2,
        "type_line": "Instant",
    }


def _nothing(state: dict[str, Any]) -> None:
    pass


def _set(path: str, value: Any) -> Callable[[dict[str, Any]], None]:
    """Mutator that sets ``state[a][b]...`` (list indexes as ints) in place."""
    keys = [int(k) if k.isdigit() else k for k in path.split(".")]

    def apply(state: dict[str, Any]) -> None:
        target = state
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = value

    return apply


def _swap_forest_for_island(state: dict[str, Any]) -> None:
    state["battlefield"][0].update(name="Island", type_line="Basic Land — Island")


def _swap_hand_spell(state: dict[str, Any]) -> None:
    state["hand"][0].update(name="Lightning Bolt", type_line="Instant", mana_cost="{R}")


def _two_spells_on_stack(state: dict[str, Any]) -> None:
    state["stack"] = [_spell("Spell Pierce", 9), _spell("Fading Hope", 10)]


def _forest_without_id(state: dict[str, Any]) -> None:
    state["hand"].append({"name": "Forest", "type_line": "Basic Land — Forest"})


# (setup applied before the first evaluation, in-place change before the second)
SEMANTIC_CHANGES = {
    # Same zone sizes, different cards: the original failure mode.
    "battlefield_card_swap": (_nothing, _swap_forest_for_island),
    "hand_card_swap": (_nothing, _swap_hand_spell),
    "stack_card_identity": (
        _set("stack", [_spell("Spell Pierce", 9)]),
        _set("stack.0.name", "Fading Hope"),
    ),
    "graveyard_card_identity": (
        _set("graveyard", [{"instance_id": 7, "name": "Fading Hope", "type_line": "Instant"}]),
        _set("graveyard.0.name", "Make Disappear"),
    ),
    # The stack is the one order-sensitive zone.
    "stack_order": (_two_spells_on_stack, lambda state: state["stack"].reverse()),
    # Mana and board facts.
    "floating_mana": (_nothing, _set("players.0.mana_pool", {"U": 3, "G": 1})),
    "tapped_elves": (_nothing, _set("battlefield.1.is_tapped", True)),
    "stolen_elves": (_nothing, _set("battlefield.1.controller_seat_id", 2)),
    "attacking_flag": (_nothing, _set("battlefield.1.is_attacking", True)),
    "summoning_sickness": (_nothing, _set("battlefield.1.is_summoning_sick", True)),
    "oracle_text": (_nothing, _set("battlefield.1.oracle_text", "Flying. Haste.")),
    "hero_life": (_nothing, _set("players.0.life_total", 19)),
    # Turn and match identity.
    "turn_number": (_nothing, _set("turn.turn_number", 4)),
    "phase": (_nothing, _set("turn.phase", "Phase_Main2")),
    "different_match": (_nothing, _set("match_id", "match-B")),
    # Opponent information: the top-level count wins over the player row, and
    # 0 is a real count, distinct from missing.
    "opponent_hand_count": (_nothing, _set("players.1.cards_in_hand", 2)),
    "top_level_opponent_hand_count": (_set("opponent_hand_count", 4), _set("opponent_hand_count", 1)),
    "opponent_hand_count_zero_vs_missing": (_nothing, _set("opponent_hand_count", 0)),
    # pending_decision is deep-frozen, so an in-place edit of the same object counts.
    "pending_decision_in_place": (
        _set("pending_decision", {"type": "choose", "options": ["A"]}),
        lambda state: state["pending_decision"]["options"].append("B"),
    ),
    # Same-name cards with mixed/missing instance ids keep their multiplicity
    # (the old sorted raw tuples raised TypeError comparing None with int).
    "mixed_missing_instance_ids": (
        _forest_without_id,
        lambda state: state["hand"].append(
            {"name": "Forest", "instance_id": 7, "type_line": "Basic Land — Forest", "is_tapped": False}
        ),
    ),
}


@pytest.mark.parametrize("setup, change", SEMANTIC_CHANGES.values(), ids=SEMANTIC_CHANGES.keys())
def test_semantic_change_recomputes(setup, change):
    state = _base_state()
    setup(state)
    first = MCTSEvaluator.evaluate(state)
    change(state)
    second = MCTSEvaluator.evaluate(state)
    assert second is not first
    assert second.eval_source == first.eval_source  # still a real evaluation


@pytest.mark.parametrize(
    "odd_entry",
    [
        {"name": "Forest", "type_line": "Basic Land — Forest"},  # no instance_id
        "Forest",  # a bare name instead of a card dict
    ],
    ids=["missing_instance_id", "bare_string"],
)
def test_odd_card_entries_do_not_crash(odd_entry):
    state = _base_state()
    state["hand"].append(odd_entry)
    assert MCTSEvaluator.evaluate(state).branches


def test_equal_snapshot_reuses_cached_payload_object():
    """The same snapshot must reuse the cached payload object (not a copy)."""
    state = _base_state()
    first = MCTSEvaluator.evaluate(state)
    second = MCTSEvaluator.evaluate(state)
    assert first is second


def test_unknown_fields_do_not_invalidate():
    """Non-semantic metadata changes must NOT invalidate (no phony misses)."""
    state = _base_state()
    first = MCTSEvaluator.evaluate(state)

    noisy = _base_state()
    noisy["battlefield"][0]["sometransientfield"] = "x"  # unknown fields ignored
    noisy["players"][1]["extra"] = 123

    second = MCTSEvaluator.evaluate(noisy)
    assert second is first


def test_cached_result_matches_a_recompute_after_a_format_change():
    state = _base_state()
    state["format_profile"] = {"family": "constructed", "variant": "standard"}
    first = MCTSEvaluator.evaluate(state)
    state["format_profile"] = {"family": "limited", "variant": "draft"}
    cached = MCTSEvaluator.evaluate(state)
    forced = MCTSEvaluator.evaluate(state, force=True)
    assert first.format_summary != forced.format_summary
    assert cached.format_summary == forced.format_summary


def test_cached_result_matches_a_recompute_when_the_hand_count_falls_back():
    """A null hand_count falls back to cards_in_hand, and the signature follows."""
    state = _base_state()
    state["players"][1]["hand_count"] = None
    first = MCTSEvaluator.evaluate(state)
    state["players"][1]["cards_in_hand"] = 1
    cached = MCTSEvaluator.evaluate(state)
    forced = MCTSEvaluator.evaluate(state, force=True)
    assert first.root_win_probability != forced.root_win_probability
    assert cached.root_win_probability == forced.root_win_probability


def test_ttl_expiry_recomputes():
    """After TTL expiry, equal snapshots recompute rather than reuse forever."""
    state = _base_state()
    first = MCTSEvaluator.evaluate(state)
    MCTSEvaluator._last_payload_at -= MCTSEvaluator.CACHE_TTL_SECONDS + 1.0
    second = MCTSEvaluator.evaluate(state)
    assert second is not first


def test_ttl_disabled_reuses(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(MCTSEvaluator, "CACHE_TTL_SECONDS", 0.0)
    state = _base_state()
    first = MCTSEvaluator.evaluate(state)
    MCTSEvaluator._last_payload_at -= 3600.0
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
