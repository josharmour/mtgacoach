"""Tests for Task 06: Use only the hero's verified deck."""

import pytest
from collections import Counter

from arenamcp.format_profile import FormatProfile
from arenamcp.magezero_gating import (
    UWTEMPO_DECK_COUNTS,
    compute_count_weighted_jaccard,
    extract_hero_deck,
    extract_hero_deck_cards,
    is_hero_deck_gated,
)
from arenamcp.model_zoo import _DEFAULT_UWTEMPO_MANIFEST, ModelZooClient


def test_current_training_deck_fixture_matches_exported_manifest(monkeypatch):
    """Acceptance 1: The current UWTempo training deck fixture matches the exported manifest with Jaccard 1.0."""
    manifest_counts = _DEFAULT_UWTEMPO_MANIFEST["deck_counts"]
    sim = compute_count_weighted_jaccard(UWTEMPO_DECK_COUNTS, manifest_counts)
    assert sim == 1.0, f"Expected 1.0 Jaccard similarity, got {sim}"

    # Also test through ModelZooClient.select with full deck
    profile = FormatProfile(
        family="constructed",
        variant="standard",
        deck_size=60,
        singleton=False,
    )
    full_deck = []
    for card, count in UWTEMPO_DECK_COUNTS.items():
        full_deck.extend([card] * count)

    from test_model_zoo import _v2_manifest
    from dataclasses import replace
    from arenamcp.model_zoo import ModelSpec
    from arenamcp.magezero_client import MageZeroClient
    monkeypatch.setattr(MageZeroClient, "get_active_endpoint", lambda: "http://fixture")
    spec = replace(ModelSpec.from_manifest(_v2_manifest()), is_resident=True, promotion_status="certified")
    with ModelZooClient._lock:
        ModelZooClient._active_host = "http://fixture"
        ModelZooClient._models_by_host["http://fixture"] = [spec]

    selection = ModelZooClient.select(profile, full_deck, refresh=False)
    assert selection is not None
    assert selection.similarity >= 0.99
    assert "UWTempo" in selection.label


def test_opponent_island_malcolm_cannot_activate_rl_for_hero_forest():
    """Acceptance 2: Opponent cards on battlefield/graveyard do NOT leak into hero deck identity."""
    # Hero only controls and owns a Forest.
    # Opponent controls/owns an Island and Malcolm, Alluring Scoundrel (which match UWTempo).
    state = {
        "local_seat_id": 1,
        "players": [
            {"seat_id": 1, "is_local": True},
            {"seat_id": 2, "is_local": False},
        ],
        "battlefield": [
            {
                "name": "Forest",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
            },
            {
                "name": "Island",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
            },
            {
                "name": "Malcolm, Alluring Scoundrel",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
            },
        ],
        "graveyard": [
            {
                "name": "No More Lies",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
            }
        ],
        "hand": [],
    }

    extraction = extract_hero_deck(state)
    # Only the hero Forest should be extracted!
    assert extraction.cards == ["Forest"]
    assert "Island" not in extraction.cards
    assert "Malcolm, Alluring Scoundrel" not in extraction.cards

    is_gated, score, label = is_hero_deck_gated(state)
    assert is_gated is False
    assert label == "Tactical Heuristic Lookahead"
    assert score == 0.0


def test_stolen_permanent_does_not_leak_into_deck():
    """Hero controlling an opponent's permanent (e.g. Act of Treason) does not count as hero's deck."""
    state = {
        "local_seat_id": 1,
        "battlefield": [
            {
                "name": "Forest",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
            },
            {
                "name": "Malcolm, Alluring Scoundrel",
                "owner_seat_id": 2,  # Owned by opponent!
                "controller_seat_id": 1,  # Temporarily controlled by hero
            },
        ],
    }
    extraction = extract_hero_deck(state)
    assert extraction.cards == ["Forest"]


def test_two_generic_matching_cards_alone_do_not_establish_match():
    """Acceptance 3: Seeing only 2 basic lands (Island + Plains) does NOT activate UWTempo RL."""
    state = {
        "local_seat_id": 1,
        "battlefield": [
            {"name": "Island", "owner_seat_id": 1, "controller_seat_id": 1},
            {"name": "Plains", "owner_seat_id": 1, "controller_seat_id": 1},
        ],
        "hand": [],
    }
    extraction = extract_hero_deck(state)
    assert extraction.is_full_deck is False
    assert set(extraction.cards) == {"Island", "Plains"}

    is_gated, score, label = is_hero_deck_gated(state)
    assert is_gated is False
    assert label == "Tactical Heuristic Lookahead"

    # Also check ModelZooClient.select directly
    profile = FormatProfile(family="constructed", variant="standard", deck_size=60)
    selection = ModelZooClient.select(profile, ["Island", "Plains"])
    assert selection is None


def test_unknown_cards_and_format_produce_explicit_compatibility_result():
    """Acceptance 4: Unknown format variant or unknown cards produce explicit incompatibility."""
    # 1. Unknown cards in registered deck
    state_unknown_cards = {
        "local_seat_id": 1,
        "deck_cards": [9999999] * 40,  # invalid/unknown GRP IDs
    }
    extraction = extract_hero_deck(state_unknown_cards)
    assert extraction.is_compatible is False
    assert "unknown_cards_in_deck" in extraction.compatibility_reason

    # 2. Incompatible format (e.g. Brawl / Commander)
    state_brawl = {
        "local_seat_id": 1,
        "format": {"family": "brawl", "singleton": True, "deck_size": 100},
        "deck_cards": list(UWTEMPO_DECK_COUNTS.keys()) * 3,
    }
    is_gated, score, label = is_hero_deck_gated(state_brawl)
    assert is_gated is False
    assert label == "Tactical Heuristic Lookahead"
