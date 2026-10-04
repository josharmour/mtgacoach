"""Tests for the deck-minus-seen library model injected into advice prompts.

The compact summary must be injected on every advice path (not just when a
tutor is in hand) so the coach can reason about draw odds and remaining outs
— see the 2026-07-16 "logs are the eyes and ears" working model.
"""

from types import SimpleNamespace

import pytest

from arenamcp.standalone import StandaloneCoach

MOUNTAIN, BOLT, OTHER, TUTOR = 100, 200, 300, 400

CARD_INFO = {
    MOUNTAIN: {"name": "Mountain", "type_line": "Basic Land — Mountain"},
    BOLT: {
        "name": "Lightning Bolt",
        "type_line": "Instant",
        "mana_cost": "{R}",
        "oracle_text": "Lightning Bolt deals 3 damage to any target.",
    },
    OTHER: {"name": "Grizzly Bears", "type_line": "Creature — Bear", "mana_cost": "{1}{G}"},
    TUTOR: {
        "name": "Demonic Tutor",
        "type_line": "Sorcery",
        "mana_cost": "{1}{B}",
        "oracle_text": "Search your library for a card and put it into your hand.",
    },
}


@pytest.fixture(autouse=True)
def local_card_facts(monkeypatch):
    monkeypatch.setattr("arenamcp.match_context._local_card", lambda grp, epoch: CARD_INFO.get(grp, {}))


class _Stub:
    """Borrows the real StandaloneCoach methods without its heavy __init__."""

    _compute_library_summary = StandaloneCoach._compute_library_summary
    _compute_tutor_library_targets = StandaloneCoach._compute_tutor_library_targets
    _has_tutor_in_hand = StandaloneCoach._has_tutor_in_hand
    _inject_library_summary_if_needed = StandaloneCoach._inject_library_summary_if_needed

    def __init__(self):
        self._mcp = SimpleNamespace(get_card_info=lambda grp: CARD_INFO.get(grp, {"name": f"Unknown({grp})"}))


def _hand_card(grp_id, seat=1):
    info = CARD_INFO.get(grp_id, {})
    return {
        "grp_id": grp_id,
        "owner_seat_id": seat,
        "name": info.get("name", "?"),
        "type_line": info.get("type_line", ""),
        "oracle_text": info.get("oracle_text", ""),
    }


def _game_state(hand=None, battlefield=None):
    # 60-card deck: 24 Mountain, 4 Bolt, 32 Bears
    deck = [MOUNTAIN] * 24 + [BOLT] * 4 + [OTHER] * 32
    return {
        "deck_cards": deck,
        "players": [{"is_local": True, "seat_id": 1}],
        "hand": hand or [],
        "battlefield": battlefield or [],
        "graveyard": [],
        "exile": [],
        "stack": [],
        "command": [],
    }


def test_compact_summary_subtracts_seen_and_shows_odds():
    stub = _Stub()
    gs = _game_state(
        hand=[_hand_card(MOUNTAIN), _hand_card(MOUNTAIN), _hand_card(BOLT)],
        battlefield=[_hand_card(MOUNTAIN), _hand_card(MOUNTAIN)],
    )
    gs["zones"] = {"library_count": 55, "library_count_source": "log_zone_membership"}
    summary = stub._compute_library_summary(gs, detailed=False)
    # 60 - 5 seen = 55 remaining; Mountain 24-4=20, Bolt 4-1=3, Bears 32
    assert summary.startswith("MY LIBRARY (55 cards left")
    assert "32x Grizzly Bears" in summary
    assert "20x Mountain" in summary
    assert "3x Lightning Bolt" in summary
    # Draw odds: 20/55 = 36%
    assert "(36.4% per random draw)" in summary
    # Compact form must not leak oracle text into every prompt
    assert "deals 3 damage" not in summary


def test_unknown_observed_count_retains_inferred_inventory_without_exact_odds():
    summary = _Stub()._compute_library_summary(_game_state(), detailed=False)
    assert "60 cards left by starting-deck-minus-visible estimate" in summary
    assert "observed count UNKNOWN" in summary
    assert "24x Mountain" in summary
    assert "4x Lightning Bolt" in summary
    assert "per random draw" not in summary


def test_compact_summary_ignores_opponent_cards():
    stub = _Stub()
    gs = _game_state(battlefield=[_hand_card(MOUNTAIN, seat=2)])
    summary = stub._compute_library_summary(gs, detailed=False)
    assert summary.startswith("MY LIBRARY (60 cards left")


def test_detailed_summary_keeps_oracle_text():
    stub = _Stub()
    summary = stub._compute_library_summary(_game_state(), detailed=True)
    assert "MY LIBRARY (60 cards left" in summary
    assert "deals 3 damage" in summary  # non-basics carry oracle text


def test_inject_uses_compact_form_without_tutor():
    stub = _Stub()
    gs = _game_state(hand=[_hand_card(BOLT)])
    stub._inject_library_summary_if_needed(gs)
    assert gs["library_summary"].startswith("MY LIBRARY (")


def test_tutor_in_hand_retains_all_draw_inventory_and_full_target_rules():
    stub = _Stub()
    gs = _game_state(hand=[_hand_card(TUTOR)])
    stub._inject_library_summary_if_needed(gs)
    assert "4x Lightning Bolt" in gs["library_summary"]
    assert "24x Mountain" in gs["library_summary"]
    assert "deals 3 damage" in gs["deck_reference"]


def test_missing_decklist_injects_explicit_unknown_not_empty():
    stub = _Stub()
    gs = _game_state()
    gs["deck_cards"] = []
    stub._inject_library_summary_if_needed(gs)
    assert "count UNKNOWN" in gs["library_summary"]
    assert "starting deck inventory unavailable" in gs["library_summary"]
    assert "Missing inventory does not mean empty" in gs["library_summary"]
    assert "deck_reference" not in gs
