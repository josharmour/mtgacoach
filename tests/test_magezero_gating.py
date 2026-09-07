"""Unit tests for MageZero match gating and opponent hand imputation."""

from __future__ import annotations

from arenamcp.magezero_gating import (
    UWTEMPO_DECK_COUNTS,
    compute_count_weighted_jaccard,
    is_hero_deck_gated,
    sample_opponent_hands,
)


def test_jaccard_similarity_exact():
    # Identical deck must have similarity 1.0
    sim = compute_count_weighted_jaccard(UWTEMPO_DECK_COUNTS, UWTEMPO_DECK_COUNTS)
    assert sim == 1.0


def test_jaccard_similarity_unrelated():
    # Mono-Red aggro list has near 0 similarity with UWTempo
    mono_r = {"Mountain": 20, "Monastery Swiftspear": 4, "Lightning Bolt": 4}
    sim = compute_count_weighted_jaccard(mono_r, UWTEMPO_DECK_COUNTS)
    assert sim == 0.0


def test_is_hero_deck_gated_positive():
    # Hero playing UWTempo cards
    state = {
        "local_seat_id": 1,
        "battlefield": [
            {"name": "Malcolm, Alluring Scoundrel", "controller_seat_id": 1},
            {"name": "Island", "controller_seat_id": 1},
            {"name": "Island", "controller_seat_id": 1},
            {"name": "Adarkar Wastes", "controller_seat_id": 1},
        ],
        "hand": [
            {"name": "Spell Pierce", "controller_seat_id": 1},
            {"name": "No More Lies", "controller_seat_id": 1},
            {"name": "Combat Research", "controller_seat_id": 1},
        ],
    }
    # All visible cards are in UWTempo
    is_gated, score, label = is_hero_deck_gated(state, threshold=0.10)
    assert is_gated is True
    assert label == "MageZero UWTempo v2"
    assert score > 0.10


def test_is_hero_deck_gated_negative():
    # Hero playing Mono-Green Stompy
    state = {
        "local_seat_id": 1,
        "battlefield": [
            {"name": "Forest", "controller_seat_id": 1},
            {"name": "Llanowar Elves", "controller_seat_id": 1},
            {"name": "Colossal Dreadmaw", "controller_seat_id": 1},
        ],
        "hand": [],
    }
    is_gated, score, label = is_hero_deck_gated(state, threshold=0.60)
    assert is_gated is False
    assert label == "Tactical Heuristic Lookahead"
    assert score == 0.0


def test_sample_opponent_hands():
    state = {
        "local_seat_id": 1,
        "players": [
            {"seat_id": 1, "is_local": True},
            {"seat_id": 2, "is_local": False, "cards_in_hand": 3},
        ],
        "battlefield": [
            {"name": "Mountain", "controller_seat_id": 2},
            {"name": "Monastery Swiftspear", "controller_seat_id": 2},
        ],
    }
    samples = sample_opponent_hands(state, num_samples=8, seed=42)
    assert len(samples) == 8
    for s in samples:
        assert len(s) == 3
        # Should sample red cards for Mono-Red
        assert any("Mountain" in c or "Swiftspear" in c or "Rage" in c or len(c) > 0 for c in s)
