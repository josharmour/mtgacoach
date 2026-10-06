import json
from unittest.mock import Mock

from arenamcp.draft_advisor import DraftAdvisor
from arenamcp.draftstate import DraftState, create_draft_handler
from arenamcp.limited_deck import fallback_deck, reconcile_logged_deck
from arenamcp.limited_rules import rules_profile, synergy_evidence


def card(identity, text, kind="Creature", cost="{1}{G}"):
    return {
        "grp_id": identity,
        "name": f"Card {identity}",
        "oracle_text": text,
        "type_line": kind,
        "mana_cost": cost,
    }


def test_token_spell_counts_as_a_body_but_conditional_payoff_does_not():
    spell = card(1, "Create a 4/4 green Beast creature token with trample.", "Sorcery", "{4}{G}")
    payoff = card(2, "Whenever you scry, create a 1/1 Thopter artifact creature token.", "Enchantment")
    assert rules_profile(spell)["unconditional_body"] is True
    assert rules_profile(payoff)["unconditional_body"] is False
    assert rules_profile(payoff)["body"] is True


def test_draw_is_not_scry_and_loyalty_is_not_a_plus_one_counter():
    payoff = card(2, "Whenever you scry or surveil, create a Thopter token.")
    assert synergy_evidence(card(1, "Draw a card."), payoff) == []
    assert synergy_evidence(card(1, "Scry 2."), payoff)[0]["mechanic"] == "scry"
    counter_payoff = card(4, "Whenever you put a +1/+1 counter on a creature, draw a card.")
    assert synergy_evidence(card(3, "Put a loyalty counter on target planeswalker."), counter_payoff) == []


def test_opponent_actions_and_restricted_entries_are_not_generic_synergies():
    source = card(1, "Scry 2.")
    assert synergy_evidence(source, card(2, "Whenever an opponent scries, draw a card.")) == []
    assert (
        synergy_evidence(source, card(3, "Whenever a red creature enters under your control, draw a card."))
        == []
    )


def test_unsubstantiated_named_synergy_is_stripped_even_for_owned_cards():
    backend = Mock()
    backend.complete.return_value = json.dumps(
        {
            "picks": [{"grp_id": 1, "reason": "Drawing triggers the scry payoff.", "synergy_with": [2]}],
            "plan": "Scry tokens.",
        }
    )
    pack = {
        "cards": [card(1, "Draw a card.")],
        "picked_cards": [card(2, "Whenever you scry, create a Thopter.")],
        "picks_per_pack": 1,
    }
    result = DraftAdvisor(backend).recommend(pack, {"spoken_advice": "Use grounded fallback"})
    # 2026-10-06: the claim is dropped and flagged; the pick itself is still judged.
    pick = result["recommendations"][0]
    assert pick["synergy_with"] == [] and pick["unsupported_synergy"] == ["Card 2"]
    assert pick["reason"].endswith("(synergy unverified)")


def test_token_producers_keep_slots_over_excess_empty_board_buffs():
    pool = [card(identity, "Vigilance") for identity in range(1, 11)]
    pool += [
        card(identity, "Create a 3/3 green Beast creature token.", "Sorcery", "{2}{G}")
        for identity in range(11, 18)
    ]
    pool += [
        card(identity, "Target creature gets +2/+2 until end of turn.", "Instant", "{G}")
        for identity in range(18, 33)
    ]
    build = fallback_deck(pool)
    kept = {entry["grp_id"] for entry in build["main_deck"]}
    assert set(range(11, 18)).issubset(kept)
    assert build["total_cards"] == 40


def test_nested_course_list_does_not_mix_a_constructed_deck_with_draft_pool():
    state = DraftState()
    handle = create_draft_handler(state)
    handle(
        "courses",
        {
            "Courses": [
                {
                    "InternalEventName": "Standard",
                    "CurrentModule": "CreateMatch",
                    "CardPool": [],
                    "CourseDeck": {"MainDeck": [{"cardId": 999, "quantity": 60}]},
                },
                {
                    "InternalEventName": "PickTwoDraft_FRA",
                    "CourseId": "current",
                    "CurrentModule": "DeckSelect",
                    "CardPool": [1, 1, 2],
                    "CourseDeckSummary": {"DeckId": "draft-deck"},
                    "CourseDeck": {"MainDeck": [{"cardId": 1, "quantity": 2}]},
                },
            ]
        },
    )
    assert state.picked_cards == [1, 1, 2]
    assert state.editor_main_deck == {1: 2}
    handle("deck", {"DeckId": "other-deck", "MainDeck": [{"cardId": 9, "quantity": 60}]})
    assert state.editor_main_deck == {1: 2}
    handle(
        "deck",
        {"DeckId": "draft-deck", "MainDeck": [{"cardId": 1, "quantity": 1}, {"cardId": 2, "quantity": 1}]},
    )
    assert state.editor_main_deck == {1: 1, 2: 1}


def test_saved_deck_reconciliation_does_not_repeat_already_made_cuts():
    pool = [card(identity, "Vigilance") for identity in range(1, 24)]
    pool.append(card(90, "Flying", cost="{5}{R}{R}"))
    build = fallback_deck(pool)
    current = [{**entry, "type_line": "Creature"} for entry in build["main_deck"]]
    current.append({"grp_id": 100, "name": "Forest", "type_line": "Basic Land — Forest", "count": 17})
    result = reconcile_logged_deck({**build, "editor_cards": current})
    assert result["remaining_cuts"] == {}
    assert "already matches" in result["spoken_advice"]
    current.append({"grp_id": 90, "name": "Off-color Dragon", "type_line": "Creature", "count": 2})
    result = reconcile_logged_deck({**build, "editor_cards": current})
    assert result["remaining_cuts"] == {90: 2}
    assert "Cut 2 Off-color Dragon" in result["spoken_advice"]
    assert "last logged deck has 42" in result["spoken_advice"]
