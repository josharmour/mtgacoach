"""Tests to verify raw Card<ID> token resolution and distribution legal action formatting."""

from arenamcp.coach import CoachEngine
from arenamcp.rules_engine import RulesEngine


def _make_coach() -> CoachEngine:
    class _Stub:
        timeout_s = 5.0

        def complete(self, *a, **k):
            return ""

    return CoachEngine(backend=_Stub())


def test_postprocess_resolves_raw_card_id_token():
    """Verify _postprocess_advice replaces raw Card205168 tokens with the actual card name."""
    coach = _make_coach()
    game_state = {
        "local_seat_id": 1,
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20},
            {"seat_id": 2, "is_local": False, "life_total": 20},
        ],
        "turn": {
            "turn_number": 1,
            "active_player": 1,
            "priority_player": 1,
            "phase": "Phase_Main1",
        },
        "decision_context": {
            "type": "distribution",
            "source_card": "Gandalf, Friend of the Shire",
            "total": 0,
        },
        "battlefield": [
            {
                "instance_id": 205168,
                "name": "Gandalf, Friend of the Shire",
                "type_line": "Legendary Creature — Avatar Wizard",
            }
        ],
        "hand": [],
    }

    advice = "Distribute 0 from Card205168 then attack with Gandalf."
    sanitized = coach._postprocess_advice(advice, game_state)

    assert "Card205168" not in sanitized
    assert "Gandalf, Friend of the Shire" in sanitized


def test_rules_engine_distribution_legal_action_formatting():
    """Verify RulesEngine formats distribution actions without 'Distribute 0'."""
    game_state = {
        "local_seat_id": 1,
        "decision_context": {
            "type": "distribution",
            "source_card": "Gandalf, Friend of the Shire",
            "total": 0,
        },
    }

    actions = RulesEngine.get_legal_actions(game_state)
    assert any("Distribute damage/counters from Gandalf" in a for a in actions)
    assert not any("Distribute 0" in a for a in actions)


def test_scryfall_api_fallback_when_bulk_not_ready(monkeypatch):
    """Verify ScryfallCache attempts live API fallback even when _bulk_data_ready is False."""
    from arenamcp.scryfall import ScryfallCache

    # Create ScryfallCache instance with _bulk_data_ready = False
    cache = ScryfallCache.__new__(ScryfallCache)
    cache._arena_index = {}
    cache._bulk_data_ready = False
    cache._not_found_cache = set()
    cache._name_cache = {}
    cache._last_api_call = 0.0

    # Mock _fetch_from_api to verify it is invoked
    called_ids = []

    def mock_fetch(arena_id):
        called_ids.append(arena_id)
        return {
            "name": "Smite the Deathless",
            "oracle_text": "Deals 3 damage",
            "type_line": "Instant",
            "mana_cost": "{1}{R}",
            "cmc": 2.0,
            "colors": ["R"],
            "arena_id": arena_id,
            "scryfall_uri": "https://scryfall.com",
        }

    monkeypatch.setattr(cache, "_fetch_from_api", mock_fetch)

    res = cache.get_card_by_arena_id(12345)
    assert res is not None
    assert res.name == "Smite the Deathless"
    assert 12345 in called_ids

