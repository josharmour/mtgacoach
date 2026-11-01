"""Open #345 reproduction, from 2b141bfa T15 OptionalAction349/446.

The monitor packet supplies the exact menu/reason; raw state350 proves life16->13,
839 untapped, first mana payment state424/T17. This minimal snapshot supplies
known source rules explicitly to isolate value checking from the missing-source
trace. It is not a full reconstructed historical snapshot.
"""

import json
from copy import deepcopy

import pytest
from tests.test_tactical_timing_backlog import card
from tests.test_typed_decision_context import _CapturingBackend

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.optional_prompts import life_payment_decline, prompt_note

REASON = (
    "Paying 3 life lets the land enter untapped for immediate green mana toward the stalled ramp, "
    "and 16 life is safe against their 6-power clock."
)
LAND_RULES = "As this land enters, you may pay 3 life. If you don't, it enters tapped.\n{oT}: Add {oG}."


def payment_case():
    meta = {
        "sourceId": 9022,
        "mechanics": [],
        "recipients": [],
        "promptCardIds": [839],
        "promptCards": [],
        "promptText": "Pay 3 life?",
    }
    decision = PendingDecision(
        (349, 446),
        "OptionalAction",
        (
            DecisionOption("optional:accept", "Accept the optional effect", meta=meta),
            DecisionOption("optional:decline", "Decline the optional effect", meta=meta),
        ),
        prompt_text="Pay 3 life?",
    )
    land = card(839, "Tanglespan Bridgeworks", LAND_RULES, "Land")
    state = {
        "match_id": "2b141bfa-b412-4dce-89e5-3551c5af2062",
        "local_seat_id": 1,
        "hand_complete": True,
        "hand_count": 4,
        "graveyard": [],
        "exile": [],
        "command": [],
        "turn": {"turn_number": 15, "phase": "Main1", "active_player": 1},
        "players": [
            {"seat_id": 1, "life_total": 16, "is_local": True, "mana_pool": {}},
            {"seat_id": 2, "life_total": 18},
        ],
        "battlefield": [
            card(i, "Forest", "{T}: Add {G}.", "Basic Land — Forest", is_tapped=True)
            for i in (469, 473, 500, 704, 729)
        ]
        + [
            card(
                449,
                "Ba Sing Se",
                "{T}: Add {G}.\n{2}{G}, {T}: Earthbend 2. Activate only as a sorcery.",
                "Land",
                is_tapped=True,
            ),
            card(
                502,
                "Nykthos, Shrine to Nyx",
                "{T}: Add {C}.\n{2}, {T}: Choose a color. Add an amount of mana of that color equal to your devotion to that color.",
                "Legendary Land",
                is_tapped=True,
            ),
            *[
                card(
                    i,
                    "The Notary Hobbits",
                    "{T}: Add {C} for each Halfling you control.",
                    "Creature — Halfling Advisor",
                    summoning_sickness=True,
                    turn_entered_battlefield=15,
                    power=1,
                    toughness=1,
                )
                for i in (828, 837, 838)
            ],
            card(688, "Massacre Girl", "Menace", "Creature", seat=2, power=4, toughness=4),
            card(824, "Starscape Cleric", "Flying", "Creature", seat=2, power=2, toughness=1),
        ],
        "hand": [
            card(251, "Vorinclex, Voice of Hunger", "Trample", "Legendary Creature", "{6}{G}{G}"),
            card(827, "Vaultborn Tyrant", "Trample", "Creature", "{5}{G}{G}"),
            card(
                249,
                "Emrakul, the Exigent Doom",
                '{3}, Exile this card from your hand: Target land gains "{T}: Add {C}{C}" until this card is cast from exile.',
                "Legendary Creature",
                "{10}",
            ),
        ],
        "stack": [],
        "decision_context": {
            "type": "optional_action",
            "raw": {"gameStateId": 349, "msgId": 446},
            "prompt_cards": [land],
            "prompt": "Pay 3 life?",
        },
    }
    state["hand"].append(
        card(
            250,
            "Bridgeworks Battle",
            "Target creature you control gets +2/+2 until end of turn. It fights up to one target creature you don't control.",
            "Sorcery",
            "{2}{G}",
        )
    )
    return decision, state


def test_unused_immediate_land_mana_does_not_justify_three_life(monkeypatch):
    decision, state = payment_case()
    # Known source text reaches the prompt. The existing low-life floor alone
    # cannot reject this payment:13 remaining is above the visible6power.
    assert "Tanglespan Bridgeworks" in prompt_note(decision, state)
    backend = _CapturingBackend(json.dumps({"option_ids": ["optional:accept"], "reasoning": REASON}))
    planner = ActionPlanner(backend=backend)
    monkeypatch.setattr(planner, "_line_prompt_context", lambda *a: (None, "", {}))
    monkeypatch.setattr(planner, "_grounded_plan_block", lambda *a, **kw: "")
    assert planner.plan_decision_options(decision, state) == ["optional:decline"]
    assert not backend.prompts
    assert "before our next untap" in planner._last_decision_reasoning


@pytest.mark.parametrize(
    "change",
    [
        "cheap_spell",
        "cheap_channel",
        "ready_land",
        "ready_hobbit",
        "floating",
        "partial_hand",
        "unknown_hand_count",
        "opponent_turn",
        "pending_spell",
        "unknown_zone",
        "unknown_controller",
        "alternate_cost",
        "unknown_rules",
        "grave_flashback",
        "exile_card",
        "untap_engine",
        "attack_draw",
        "attack_treasure",
        "mana_sink",
    ],
)
def test_concrete_or_unpriced_before_untap_uses_keep_payment_open(change):
    decision, state = payment_case()
    if change == "cheap_spell":
        state["hand"][0] = card(
            251, "Protection", "Target permanent gains hexproof until end of turn.", "Instant", "{G}"
        )
    elif change == "cheap_channel":
        state["hand"][0] = card(
            251, "Channel creature", "Channel — {G}, Discard this card: Create a token.", "Creature", "{6}{G}"
        )
    elif change == "ready_land":
        state["battlefield"][0]["is_tapped"] = False
    elif change == "ready_hobbit":
        state["battlefield"][7].update(summoning_sickness=False, turn_entered_battlefield=13)
    elif change == "floating":
        state["players"][0]["mana_pool"] = {"Green": 1}
    elif change == "partial_hand":
        state["hand_complete"] = False
    elif change == "unknown_hand_count":
        state.pop("hand_count")
    elif change == "opponent_turn":
        state["turn"]["active_player"] = 2
    elif change == "pending_spell":
        state["stack"] = [card(900, "Draw spell", "Draw two cards.", "Instant")]
    elif change == "unknown_zone":
        state.pop("exile")
    elif change == "unknown_controller":
        state["battlefield"][0].pop("controller_seat_id")
    elif change == "alternate_cost":
        state["hand"][0]["oracle_text"] = "Convoke"
    elif change == "unknown_rules":
        state["battlefield"][7]["oracle_text"] = ""
    elif change == "grave_flashback":
        state["graveyard"] = [card(900, "Flashback", "Flashback {G}", "Instant", "{3}{G}")]
    elif change == "exile_card":
        state["exile"] = [card(900, "Exiled offer", "", "Instant", "{G}")]
    else:
        rules = {
            "untap_engine": "Untap all permanents you control during each other player's untap step.",
            "attack_draw": "Whenever this creature attacks, draw a card.",
            "attack_treasure": "Whenever this creature attacks, create a Treasure token.",
            "mana_sink": "{G}: Scry 1.",
        }[change]
        state["battlefield"].append(card(901, "Potential benefit", rules, "Creature", power=1, toughness=1))
    assert life_payment_decline(decision, state) == ""


def test_top_level_floating_mana_and_haste_are_not_ignored():
    decision, state = payment_case()
    state["floating_mana"] = {"G": 1}
    assert not life_payment_decline(decision, state)
    state.pop("floating_mana")
    state["battlefield"][7]["oracle_text"] += "\nHaste"
    assert not life_payment_decline(decision, state)


def test_unidentified_entering_source_abstains_but_exact_prompt_face_resolves(monkeypatch):
    from arenamcp import card_db, match_context

    decision, state = payment_case()
    unresolved = deepcopy(state["decision_context"]["prompt_cards"][0])
    unresolved.pop("type_line")
    state["decision_context"]["prompt_cards"] = [unresolved]
    assert not life_payment_decline(decision, state)
    unresolved["grp_id"] = 90825
    unresolved["oracle_text"] = ""
    monkeypatch.setattr(card_db, "_card_db", object())
    monkeypatch.setattr(
        match_context,
        "_local_card",
        lambda *args: {"type_line": "Land", "name": "Tanglespan Bridgeworks", "oracle_text": LAND_RULES},
    )
    assert "before our next untap" in life_payment_decline(decision, state)


def test_low_life_guard_still_applies_with_a_useful_spell():
    decision, state = payment_case()
    state["hand"][0] = card(251, "Useful instant", "Draw a card.", "Instant", "{G}")
    state["players"][0]["life_total"] = 8
    assert "within reach" in life_payment_decline(decision, state)


def test_already_resolved_self_entry_token_trigger_does_not_invent_future_mana():
    decision, state = payment_case()
    for permanent in state["battlefield"][7:10]:
        permanent["oracle_text"] = (
            "When The Notary Hobbits enter, if they're not a token, create two tokens that are copies of them, except the tokens aren't legendary.\n{T}: Add {C} for each Halfling you control."
        )
    assert "before our next untap" in life_payment_decline(decision, state)


def test_wrong_payment_prompt_and_missing_hand_rules_abstain():
    from dataclasses import replace

    decision, state = payment_case()
    assert not life_payment_decline(replace(decision, prompt_text="Pay 2 life?"), state)
    state["hand"][0]["oracle_text"] = ""
    assert not life_payment_decline(decision, state)


def test_battlefield_only_mana_ability_in_graveyard_cannot_spend_the_new_mana():
    decision, state = payment_case()
    state["graveyard"] = [
        card(
            696,
            "Rofellos, Llanowar Emissary",
            "{T}: Add {G} for each Forest you control.",
            "Legendary Creature — Elf Druid",
            "{G}{G}",
        )
    ]
    assert "before our next untap" in life_payment_decline(decision, state)
    state["battlefield"].append(
        card(900, "Grave permission", "You may activate abilities of cards in your graveyard.", "Enchantment")
    )
    assert not life_payment_decline(decision, state)


def test_modified_cast_cost_and_unknown_grave_owner_are_not_ignored():
    decision, state = payment_case()
    state["hand"][0]["modified_cost"] = "{G}"
    assert not life_payment_decline(decision, state)
    state["hand"][0].pop("modified_cost")
    state["graveyard"] = [{"instance_id": 900, "name": "Unknown", "type_line": "Instant"}]
    assert not life_payment_decline(decision, state)


def test_known_static_life_threshold_can_justify_entry_payment():
    decision, state = payment_case()
    state["battlefield"].append(
        card(
            900,
            "Threshold benefit",
            "As long as you have 13 or less life, creatures you control get +2/+2.",
            "Enchantment",
        )
    )
    assert not life_payment_decline(decision, state)


def test_unpriced_attack_keyword_resource_abstains():
    decision, state = payment_case()
    state["battlefield"].append(
        card(900, "Investigator", "Whenever this creature attacks, investigate.", "Creature")
    )
    assert not life_payment_decline(decision, state)
