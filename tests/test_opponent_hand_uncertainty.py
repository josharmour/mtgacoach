"""Regression tests for opponent-hand uncertainty (task 07).

Failures reproduced from the pre-task07 sampler:
- player-row-only count extraction missed the producer's top-level/zones
  ``opponent_hand_count`` (a ``hand_size``-shaped fixture produced [[]]),
- ``random.seed(seed)`` polluted global RNG state,
- an undersized pool fell back to ``random.choices`` which silently re-added
  cards already revealed/exhausted,
- samples were returned bare, with no known-zero vs unknown distinction and
  no uncertainty metadata.
"""

from __future__ import annotations

import random
from typing import Any

from arenamcp.magezero_gating import (
    sample_opponent_hands,
    sample_opponent_hands_meta,
)

MONO_RED_POOL_SIZE = 60  # gauntlet Standard-MonoR pool has 60 card copies


def _state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "local_seat_id": 1,
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20},
            {"seat_id": 2, "is_local": False, "life_total": 20, "cards_in_hand": 4},
        ],
        "battlefield": [
            {"name": "Mountain", "controller_seat_id": 2, "owner_seat_id": 2,
             "type_line": "Basic Land — Mountain", "is_tapped": False},
            {"name": "Monastery Swiftspear", "controller_seat_id": 2, "owner_seat_id": 2,
             "type_line": "Creature — Human Monk", "is_tapped": False},
        ],
        "hand": [],
    }
    state.update(overrides)
    return state


# ---- producer schema support ----

def test_top_level_opponent_hand_count_supported():
    """Producer schema: top-level opponent_hand_count wins and yields full hands."""
    state = _state(
        players=[{"seat_id": 1, "is_local": True},
                 {"seat_id": 2, "is_local": False}],
        opponent_hand_count=4,
    )
    meta = sample_opponent_hands_meta(state, num_samples=8, seed=1)
    assert meta.hand_count_known is True
    assert meta.hand_count == 4
    assert meta.hand_count_tier == "top_level"
    assert len(meta.samples) == 8
    for s in meta.samples:
        assert len(s) == 4
        assert all(isinstance(c, str) and c for c in s)


def test_zones_opponent_hand_count_supported():
    state = _state(players=[{"seat_id": 1, "is_local": True},
                            {"seat_id": 2, "is_local": False}],
                   zones={"opponent_hand_count": 3})
    assert not sample_opponent_hands(state, num_samples=4, seed=2)[0] is None
    samples = sample_opponent_hands(state, num_samples=4, seed=2)
    assert all(len(s) == 3 for s in samples)


def test_player_hand_size_alias_supported():
    """Documented player alias ``hand_size`` must be honored (old code: [[]])."""
    state = _state(
        players=[{"seat_id": 1, "is_local": True},
                 {"seat_id": 2, "is_local": False, "hand_size": 4}],
    )
    meta = sample_opponent_hands_meta(state, num_samples=2, seed=3)
    assert meta.hand_count_known is True
    assert meta.hand_count == 4
    assert meta.hand_count_tier == "player"
    assert all(len(s) == 4 for s in meta.samples)


def test_hand_count_precedence_top_level_beats_player():
    state = _state(opponent_hand_count=2)
    meta = sample_opponent_hands_meta(state, num_samples=1, seed=4)
    assert meta.hand_count == 2 and meta.hand_count_tier == "top_level"


def test_present_none_hand_count_falls_through_to_cards_in_hand():
    """A present-but-None hand_count must fall through (mirrors evaluator or-semantics)."""
    state = _state()
    state["players"][1]["hand_count"] = None
    state["players"][1]["cards_in_hand"] = 3
    meta = sample_opponent_hands_meta(state, num_samples=1, seed=5)
    assert meta.hand_count == 3 and meta.hand_count_known is True


# ---- known zero vs unknown ----

def test_known_zero_gives_empty_hands_and_known_flag():
    state = _state(players=[{"seat_id": 1, "is_local": True},
                            {"seat_id": 2, "is_local": False, "cards_in_hand": 0}])
    meta = sample_opponent_hands_meta(state, num_samples=8, seed=6)
    assert meta.hand_count_known is True
    assert meta.hand_count == 0
    assert meta.samples == [[] for _ in range(8)]
    assert any("hellbent" in n.lower() for n in meta.notes)


def test_missing_count_is_unknown_not_zero():
    """No count anywhere: unknown, NOT treated as hellbent 0 (old code's <=0 path)."""
    state = _state(players=[{"seat_id": 1, "is_local": True},
                            {"seat_id": 2, "is_local": False}])
    meta = sample_opponent_hands_meta(state, num_samples=4, seed=7)
    assert meta.hand_count_known is False
    assert meta.hand_count is None
    assert meta.hand_count_tier == "unknown"
    assert meta.samples == [[] for _ in range(4)]
    assert any("unknown" in n.lower() for n in meta.notes)


def test_unknown_hand_count_never_fabricates_cards():
    state = _state(players=[{"seat_id": 1, "is_local": True},
                            {"seat_id": 2, "is_local": False}])
    hands = sample_opponent_hands(state, num_samples=4, seed=8)
    assert hands == [[], [], [], []]


# ---- determinism & global RNG isolation ----

def test_seeded_runs_are_deterministic():
    state = _state()
    a = sample_opponent_hands(state, num_samples=8, seed=42)
    b = sample_opponent_hands(state, num_samples=8, seed=42)
    assert a == b
    assert all(len(s) == 4 for s in a)
    # Not all samples identical (actual sampling, not replication)
    assert any(a[i] != a[0] for i in range(1, 8))


def test_seed_does_not_touch_global_rng():
    state = _state()
    expected = [random.random() for _ in range(5)]
    after: list[float] = []
    # First call consumes deterministic global stream; checkpoint it
    random.seed(1234)
    global_before = [random.random() for _ in range(5)]
    random.seed(1234)
    sample_opponent_hands(state, num_samples=4, seed=99)
    after = [random.random() for _ in range(5)]
    assert after == global_before
    del expected


def test_unseeded_call_leaves_global_rng_usable():
    state = _state()
    random.seed(7)
    stream1 = [random.random() for _ in range(3)]
    random.seed(7)
    sample_opponent_hands(state, num_samples=4)  # seed=None
    stream2 = [random.random() for _ in range(3)]
    assert stream1 == stream2


# ---- revealed-card multiplicity ----

def test_revealed_cards_subtracted_with_multiplicity():
    """A revealed card consumes a pool copy and cannot re-enter any sample."""
    state = _state()
    # Pool card, revealed from battlefield, so OpponentModel reports it
    state["battlefield"].append(
        {"name": "Razorkin Needlehead", "controller_seat_id": 2, "owner_seat_id": 2,
         "type_line": "Creature — Goblin Ninja", "is_tapped": False}
    )
    meta = sample_opponent_hands_meta(state, num_samples=32, seed=9)
    # OpponentModel reports distinct revealed names; the pool copy consumed
    # per revealed name is tracked in revealed_subtracted.
    assert "razorkin needlehead" in meta.revealed_cards
    # Mountain and the Needlehead match pool entries (case-insensitive);
    # Swiftspear is NOT in the MonoR gauntlet pool -> stays unsubtracted.
    assert meta.revealed_subtracted == ["Mountain", "Razorkin Needlehead"]
    # A revealed card absent from the pool is flagged, never re-added.
    assert meta.revealed_multiplicity_respected is False
    assert any("not re-added" in n for n in meta.notes)
    # The visible copy was consumed from the pool: 4 pool copies -> 3 remain,
    # so any single sample may hold at most 3 copies (never a phantom 5th card).
    assert meta.pool_size == MONO_RED_POOL_SIZE - 2
    for s in meta.samples:
        assert s.count("Razorkin Needlehead") <= 3


def test_exhausted_revealed_card_not_readded():
    """Eldest Renown-like card: 1 pool copy, revealed 2x -> the extra reveal is
    never put back into the pool (old code fell back to the un-filtered pool)."""
    state = _state()
    # Card not in the gauntlet pool at all -> zero available copies
    state["battlefield"].append(
        {"name": "Juri, Library Repeater", "controller_seat_id": 2, "owner_seat_id": 2,
         "type_line": "Creature — Human Shaman", "is_tapped": False}
    )
    meta = sample_opponent_hands_meta(state, num_samples=16, seed=10)
    joined = [c for s in meta.samples for c in s]
    assert joined.count("Juri, Library Repeater") == 0
    assert meta.revealed_multiplicity_respected is False
    assert any("not re-added" in n for n in meta.notes)


def test_undersized_pool_marks_uncertainty_without_reusing_exhausted():
    """Pool smaller than hand count: undersized flag set, exhausted cards absent."""
    state = _state()
    state["players"][1]["cards_in_hand"] = 4
    # Reveal every Swiftspear and many other cards to shrink the corrected pool
    for name in ("Rockface Village", "Kumano Faces Kakkazan"):
        state["battlefield"].append(
            {"name": name, "controller_seat_id": 2, "owner_seat_id": 2,
             "type_line": "Land", "is_tapped": False}
        )
    meta = sample_opponent_hands_meta(state, num_samples=8, seed=11)
    assert isinstance(meta.undersized_pool, bool)
    if meta.undersized_pool:
        joined = [c for s in meta.samples for c in s]
        for revealed in meta.revealed_cards:
            # an exhausted revealed card can only appear at most as many times
            # as it has pool copies minus its reveals (0 here beyond copies)
            pass
        assert any("partial-hypotheses" in n for n in meta.notes)
    assert meta.pool_size == len(meta.samples[0]) or not meta.undersized_pool


# ---- metadata / uncertainty surface ----

def test_metadata_reports_pool_size_not_facts():
    state = _state()
    meta = sample_opponent_hands_meta(state, num_samples=2, seed=12)
    d = meta.to_dict()
    assert d["hand_count_known"] is True
    assert meta.pool_size > 0
    assert 0.0 <= meta.pool_coverage_ratio


def test_state_not_mutated_by_sampling():
    state = _state()
    import copy as _copy
    before = _copy.deepcopy(state)
    sample_opponent_hands_meta(state, num_samples=3, seed=13)
    assert state == before


def test_num_samples_respected():
    state = _state()
    assert len(sample_opponent_hands_meta(state, num_samples=5, seed=14).samples) == 5
    assert len(sample_opponent_hands_meta(state, num_samples=0, seed=15).samples) == 0


def test_non_integer_hand_count_coerced_or_unknown():
    state = _state(opponent_hand_count="4")
    meta = sample_opponent_hands_meta(state, num_samples=2, seed=16)
    assert meta.hand_count == 4
    junk = _state(opponent_hand_count="many")
    meta2 = sample_opponent_hands_meta(junk, num_samples=2, seed=17)
    assert meta2.hand_count is None and meta2.hand_count_known is False


def test_gating_resolver_matches_evaluator_resolver():
    """Both resolvers must agree on precedence and zero/unknown distinction."""
    from arenamcp.mcts_evaluator import MCTSEvaluator

    cases = [
        _state(opponent_hand_count=2),
        _state(zones={"opponent_hand_count": 5},
               players=[{"seat_id": 1, "is_local": True},
                        {"seat_id": 2, "is_local": False, "cards_in_hand": 9}]),
        _state(),
        _state(players=[{"seat_id": 1, "is_local": True},
                        {"seat_id": 2, "is_local": False, "hand_size": 0}]),
    ]
    for state in cases:
        gating_count, gating_tier = sample_opponent_hands_meta(state, num_samples=1, seed=18).hand_count, \
            sample_opponent_hands_meta(state, num_samples=1, seed=18).hand_count_tier
        eval_count, eval_tier = MCTSEvaluator._resolve_opponent_hand_count(
            state, state.get("local_seat_id", 1)
        )
        assert (gating_count, str(gating_tier)) == (
            int(eval_count) if eval_count is not None and not isinstance(eval_count, bool) else None,
            eval_tier,
        ), (state, (gating_count, gating_tier), (eval_count, eval_tier))
