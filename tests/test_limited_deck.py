import json
from copy import deepcopy
from unittest.mock import Mock

import pytest

from arenamcp.draft_advisor import DraftAdvisor
from arenamcp.draftstate import DraftState, create_draft_handler
from arenamcp.limited_deck import deck_choice_summary, fallback_deck, validate_deck
from arenamcp.standalone_deck import _DeckAnalysisMixin


def pool():
    cards = [
        {
            "grp_id": index,
            "name": f"Green creature {index}",
            "mana_cost": "{1}{G}",
            "type_line": "Creature",
            "oracle_text": "Vigilance",
        }
        for index in range(1, 24)
    ]
    cards.extend(
        [
            {
                "grp_id": 90,
                "name": "Expensive red dragon",
                "mana_cost": "{5}{R}{R}",
                "type_line": "Creature",
                "oracle_text": "Flying",
            }
        ]
        * 2
    )
    cards.append(
        {"grp_id": 100, "name": "Forest", "type_line": "Basic Land — Forest", "oracle_text": "{T}: Add {G}."}
    )
    return cards


def model_build():
    return {
        "main_deck": [{"grp_id": index, "count": 1} for index in range(1, 24)],
        "basic_lands": {"G": 17},
        "plan": "Green creature pressure with a low curve.",
        "cuts": [{"grp_id": 90, "reason": "off-color and too expensive for this creature curve"}],
    }


def test_counted_build_speaks_named_cuts_and_exact_forty():
    result = validate_deck(model_build(), pool(), source="card_rules")
    assert result["total_cards"] == 40
    assert result["spell_count"] == 23
    assert result["land_count"] == 17
    assert result["cuts"][0]["count"] == 2
    assert "Cut 2 Expensive red dragon" in result["spoken_advice"]
    assert "17 Forests" in result["spoken_advice"]
    assert "drafted pool" in result["spoken_advice"]
    assert "Cut 1 Forest" not in result["spoken_advice"]


@pytest.mark.parametrize("failure", ["wrong_total", "extra_copy", "unknown_card", "no_reason", "all_lands"])
def test_invalid_model_build_is_rejected(failure):
    payload = model_build()
    if failure == "wrong_total":
        payload["basic_lands"]["G"] = 18
    elif failure == "extra_copy":
        payload["main_deck"][0]["count"] = 2
    elif failure == "unknown_card":
        payload["main_deck"][0]["grp_id"] = 999
    elif failure == "no_reason":
        payload["cuts"] = []
    else:
        payload["main_deck"] = []
        payload["basic_lands"] = {"G": 40}
    with pytest.raises(ValueError):
        validate_deck(payload, pool(), source="card_rules")


def test_unrated_pool_still_gets_named_cuts_and_land_counts():
    result = fallback_deck(pool())
    assert result["total_cards"] == 40
    assert result["cuts"][0]["name"] == "Expensive red dragon"
    assert result["cuts"][0]["count"] == 2
    assert result["basic_lands"]["G"] == 17


def test_deck_explanation_uses_selected_cards_and_states_a_real_weakness():
    build = fallback_deck(pool())
    explanation = deck_choice_summary(build, pool())
    assert explanation.startswith("I built green, led by Green creature")
    assert "with 23 creature or token spells, 23 of them costing three or less" in explanation
    assert "curve out with early creatures" in explanation
    assert "few permanent answers" in explanation
    assert "Expensive red dragon" not in explanation
    assert "GIH" not in explanation and "%" not in explanation
    assert len(explanation.split()) <= 100


def test_deck_explanation_preserves_accepted_model_strategy():
    build = validate_deck(model_build(), pool(), source="card_rules")
    explanation = deck_choice_summary(build, pool())
    assert "Its plan: Green creature pressure with a low curve, " in explanation
    assert "Green creature 1" in explanation


def test_deck_explanation_does_not_invent_details_for_unknown_cards():
    assert "couldn't verify" in deck_choice_summary({"main_deck": [{"grp_id": 999, "count": 23}]}, pool())


def test_required_colorless_mana_cannot_be_paid_with_forests():
    cards = pool()
    cards[0]["mana_cost"] = "{1}{C}"
    with pytest.raises(ValueError, match="mana base"):
        validate_deck(model_build(), cards, source="card_rules")


def test_model_failure_keeps_the_counted_fallback():
    cards = pool()
    fallback = {**fallback_deck(cards), "pool_cards": cards, "pool_size": len(cards)}
    backend = Mock()
    backend.complete.return_value = '{"main_deck":[]}'
    assert DraftAdvisor(backend).recommend_deck(fallback) == fallback


def test_completed_course_recovers_full_pool_and_enters_deck_building():
    state = DraftState(is_active=True, picked_cards=[1])
    handle = create_draft_handler(state)
    handle(
        "course",
        {
            "Course": {
                "InternalEventName": "PickTwoDraft_FRA",
                "CurrentModule": "DeckSelect",
                "CardPool": [1, 1, 2, 3],
            }
        },
    )
    assert state.picked_cards == [1, 1, 2, 3]
    assert state.last_completed_pool == [1, 1, 2, 3]
    assert state.is_active is False
    assert state.is_building is True


def test_deck_builder_speaks_once_and_shows_detailed_cuts():
    coach = _DeckAnalysisMixin()
    coach._mcp = Mock()
    coach.ui = Mock()
    coach.speak_advice = Mock()
    cards = pool()
    snapshot = {"is_building": True, "pool_signature": ["FRA", [1, 1, 2]]}
    coach._mcp.get_draft_pack.return_value = snapshot
    coach._mcp.analyze_draft_pool.return_value = {
        **fallback_deck(cards),
        "pool_size": len(cards),
        "pool_cards": cards,
    }
    coach._advise_draft_build(deepcopy(snapshot))
    coach._advise_draft_build(deepcopy(snapshot))
    coach.speak_advice.assert_called_once()
    assert "Cut 2 Expensive red dragon" in coach.speak_advice.call_args.args[0]
    assert coach.speak_advice.call_args.kwargs["blocking"] is False
    assert coach.ui.advice.call_args.args[1] == "DECK"


def test_deck_model_uses_actual_copy_counts():
    cards = pool()
    backend = Mock()
    backend.complete.return_value = json.dumps(model_build())
    result = DraftAdvisor(backend).recommend_deck({"pool_cards": cards})
    assert result["total_cards"] == 40
    message = json.loads(backend.complete.call_args.args[1])
    assert next(card for card in message["pool"] if card["grp_id"] == 90)["count"] == 2


def test_repeated_event_metadata_does_not_erase_completed_draft_pool():
    state = DraftState(is_building=True, event_name="PickTwoDraft_FRA", picked_cards=[1, 1, 2])
    create_draft_handler(state)("event", {"EventName": "PickTwoDraft_FRA"})
    assert state.is_building is True
    assert state.picked_cards == [1, 1, 2]


def test_stale_build_is_not_spoken_after_match_starts():
    coach = _DeckAnalysisMixin()
    coach._mcp = Mock()
    coach.ui = Mock()
    coach.speak_advice = Mock()
    coach._mcp.analyze_draft_pool.return_value = {"pool_size": 42, "spoken_advice": "Stale cuts"}
    coach._mcp.get_draft_pack.return_value = {"is_building": False}
    coach._advise_draft_build({"is_building": True, "pool_signature": ["FRA", [1, 2]]})
    coach.speak_advice.assert_not_called()
    coach.ui.advice.assert_not_called()


def test_new_match_clears_deck_building_without_erasing_pool(monkeypatch):
    from arenamcp import server

    state = DraftState(is_building=True, picked_cards=[1, 1, 2])
    monkeypatch.setattr(server, "draft_state", state)
    server._deactivate_draft_state("new match")
    assert state.is_building is False
    assert state.picked_cards == [1, 1, 2]
