"""Declare attackers only on a Sneak line with payment available after the attack."""

from __future__ import annotations

import pytest
from tests.test_sneak_all_in_20261019 import _declare_step

from arenamcp import combat_strategy, sneak
from arenamcp.autopilot_bridge import _BridgeSubmitMixin


def _state():
    state = _declare_step()
    state["players"][0]["life_total"] = 60  # the independent forced-loss override does not apply
    state["hand"] = []
    return state


def _lands(state):
    return [
        c
        for c in state["battlefield"]
        if c["controller_seat_id"] == state["local_seat_id"] and c.get("type_line") == "Basic Land — Plains"
    ]


def _declare(state):
    return _BridgeSubmitMixin._all_in_attack_action(None, state, [])


def test_taxed_commander_can_sneak_after_the_attack_without_its_normal_cast_price():
    state = _state()
    _lands(state)[0]["is_tapped"] = True
    (cast,) = sneak.payable_sneak_casts(state)
    assert cast.zone == "command" and cast.cost == "{2}{W}" and cast.hard_mana == 5
    assert sum(not c["is_tapped"] for c in _lands(state)) == 4
    assert combat_strategy._sneak_lethal(state)
    action = _declare(state)
    assert action and "Sneak line is lethal" in action.reasoning
    assert len(action.attacker_names) == len(state["decision_context"]["legal_attacker_ids"])


@pytest.mark.parametrize(
    "unavailable", ["tapped", "wrong_color", "restricted", "attacking", "float", "hand_land"]
)
def test_unpayable_lethal_forecast_does_not_force_an_attack(unavailable):
    state = _state()
    lands = _lands(state)
    for land in lands:
        land["is_tapped"] = True
    if unavailable == "wrong_color":
        for land in lands:
            land.update(is_tapped=False, type_line="Basic Land — Forest", oracle_text="({T}: Add {G}.)")
    elif unavailable == "restricted":
        for land in lands:
            land.update(
                is_tapped=False,
                type_line="Land",
                oracle_text="{T}: Add {W}.\nSpend this mana only to cast artifact spells.",
            )
    elif unavailable == "attacking":
        for card in state["battlefield"]:
            if card["instance_id"] in state["decision_context"]["legal_attacker_ids"]:
                card["oracle_text"] = str(card.get("oracle_text") or "") + "\n{T}: Add {W}."
    elif unavailable == "float":
        state["players"][0]["mana_pool"] = {"White": 5}
    elif unavailable == "hand_land":
        state["hand"] = [dict(lands[0], instance_id=1234567, is_tapped=False)]
    assert not sneak.payable_sneak_casts(state)
    assert not combat_strategy._sneak_lethal(state)
    assert _declare(state) is None


@pytest.mark.parametrize(
    "invalid",
    ["absent", "fraction", "contradiction", "higher_tax", "unsupported_symbol", "extra_cost", "zone_lock"],
)
def test_unknown_or_unpayable_command_price_abstains(invalid):
    state = _state()
    card = state["command"][0]
    if invalid == "absent":
        state["commander_casts"] = {}
    elif invalid == "fraction":
        state["commander_casts"] = {card["grp_id"]: 1.4}
    elif invalid == "contradiction":
        card["commander_casts"] = 0
    elif invalid == "higher_tax":
        state["commander_casts"] = {card["grp_id"]: 3}
    elif invalid == "unsupported_symbol":
        card["oracle_text"] = card["oracle_text"].replace("{oW}", "{oW}{X}")
    elif invalid == "extra_cost":
        card["oracle_text"] += "\nAs an additional cost to cast this spell, sacrifice a creature."
    elif invalid == "zone_lock":
        state["battlefield"].append(
            {
                "instance_id": 987654,
                "name": "Witness",
                "type_line": "Artifact",
                "controller_seat_id": 2,
                "oracle_text": "Your opponents can't cast spells from anywhere other than their hands.",
            }
        )
    assert not sneak.payable_sneak_casts(state)
    assert _declare(state) is None


def test_native_hand_sneak_uses_printed_cost_even_with_previous_command_casts():
    state = _state()
    card = state["command"].pop()
    state["hand"] = [card]
    for land in _lands(state)[1:]:
        land["is_tapped"] = True
    (cast,) = sneak.payable_sneak_casts(state)
    assert cast.zone == "hand" and cast.cost == "{W}" and cast.tax == 0
    assert combat_strategy._sneak_lethal(state)
    assert _declare(state)


def test_main_one_hold_can_still_plan_for_a_future_land_drop():
    state = _state()
    state["turn"].update(phase="Phase_Main1", step="")
    for land in _lands(state):
        land["is_tapped"] = True
    state["hand"] = [dict(_lands(state)[0], instance_id=1234567, is_tapped=False)]
    assert sneak.sneak_main_hold(state)
    state.pop("decision_context")
    assert sneak.sneak_main_hold(state, declare_step=True)  # the concession guard also considers this out
    assert not sneak.sneak_main_hold(state, declare_step=True, force_declaration=True)


@pytest.mark.parametrize("menu", ["subset", "wrong_recipient", "missing_recipient", "extra_blocker_capacity"])
def test_declaration_can_only_spend_damage_from_real_player_attackers(menu):
    state = _state()
    ctx = state["decision_context"]
    if menu == "subset":
        ctx["legal_attacker_ids"] = ctx["legal_attacker_ids"][:1]
    elif menu == "extra_blocker_capacity":
        state["battlefield"].append(
            {
                "instance_id": 451923,
                "owner_seat_id": 2,
                "controller_seat_id": 2,
                "oracle_text": "Creatures you control can block an additional creature each combat.",
            }
        )
    else:
        ctx["raw_attackers"] = [
            {"attackerInstanceId": i, "legalDamageRecipients": [{"playerSystemSeatId": 2}]}
            for i in ctx["legal_attacker_ids"]
        ]
        ctx["raw_attackers"][0]["legalDamageRecipients"] = (
            [{"planeswalkerInstanceId": 23456}] if menu == "wrong_recipient" else []
        )
    assert _declare(state) is None


def test_one_unblocked_double_striker_loses_both_hits_when_returned():
    from arenamcp.sneak import _declared_sneak_floor

    state = _state()
    state["decision_context"]["legal_attacker_ids"] = [1]
    attacker = {"instance_id": 1, "power": 1, "toughness": 1, "keywords": ["double strike"]}
    assert _declared_sneak_floor(state, 2, [attacker], []) == 0
    attacker.update(power=4, toughness=4, keywords=["double strike", "trample"])
    blocker = {"instance_id": 2, "power": 1, "toughness": 1}
    assert _declared_sneak_floor(state, 2, [attacker], [blocker]) is None


@pytest.mark.parametrize(
    "unavailable", ["pain_land", "one_spell_per_turn", "taxed_spells", "reserved_attack_cost"]
)
def test_declaration_keeps_unsupported_costs_and_spending_restrictions_out(unavailable):
    state = _state()
    lands = _lands(state)
    if unavailable == "pain_land":
        for land in lands[1:]:
            land.update(type_line="Basic Land — Wastes", oracle_text="({T}: Add {C}.)")
        lands[0].update(
            type_line="Land",
            oracle_text="Whenever this land becomes tapped, it deals 1 damage to you.\n{T}: Add one mana of any color.",
        )
        state["players"][0]["life_total"] = 1
    else:
        effect = {
            "one_spell_per_turn": "Each player can't cast more than one spell each turn.",
            "taxed_spells": "Creature spells cost {2} more to cast.",
            "reserved_attack_cost": "Creatures can't attack you unless their controller pays {2} for each creature they control that's attacking you.",
        }[unavailable]
        state["battlefield"].append(
            {"instance_id": 787878, "name": "Restriction", "controller_seat_id": 2, "oracle_text": effect}
        )
    assert not sneak.payable_sneak_casts(state)
    assert not combat_strategy._sneak_lethal(state)


@pytest.mark.parametrize("effect", ["infect", "life_on_entry", "early_casualties"])
def test_entry_power_alone_cannot_force_a_lethal_declaration(effect):
    state = _state()
    if effect == "infect":
        state["command"][0]["oracle_text"] += "\nInfect"
        state["command"][0]["keywords"] = ["infect"]
    elif effect == "life_on_entry":
        state["battlefield"].append(
            {
                "instance_id": 457839,
                "controller_seat_id": 2,
                "type_line": "Creature",
                "power": 1,
                "toughness": 1,
                "is_tapped": True,
                "oracle_text": "Whenever another creature enters, you gain 1 life.",
            }
        )
    else:
        card = next(
            c
            for c in state["battlefield"]
            if c["controller_seat_id"] == 2
            and c.get("is_tapped") is False
            and "Creature" in str(c.get("card_types"))
        )
        card["keywords"] = [*card.get("keywords", []), "first strike"]
    assert sneak.payable_sneak_casts(state)
    assert _declare(state) is None


@pytest.mark.parametrize(
    "recipient",
    [
        {"type": "DamageRecType_Player", "playerSystemSeatId": 2},
        {"playerSystemSeatId": 2},
        {"type": "DamageRecType_Planeswalker", "playerSystemSeatId": 2, "planeswalkerInstanceId": 4637},
        {"type": "DamageRecType_Player", "playerSystemSeatId": 2.3},
        {"type": "DamageRecType_Player", "playerSystemSeatId": 2, "battleInstanceId": 4637},
    ],
)
def test_only_an_unambiguous_offered_opponent_supports_the_declaration(recipient):
    state = _state()
    ctx = state["decision_context"]
    ctx["raw_attackers"] = [
        {"attackerInstanceId": i, "legalDamageRecipients": [recipient]} for i in ctx["legal_attacker_ids"]
    ]
    assert bool(_declare(state)) == (
        recipient in ({"type": "DamageRecType_Player", "playerSystemSeatId": 2}, {"playerSystemSeatId": 2})
    )


def test_zero_printed_hand_sneak_needs_no_mana_source():
    state = _state()
    card = state["command"].pop()
    card["oracle_text"] = card["oracle_text"].replace("{oW}", "{o0}")
    state["hand"] = [card]
    for land in _lands(state):
        land["is_tapped"] = True
    (cast,) = sneak.payable_sneak_casts(state)
    assert cast.zone == "hand" and cast.cost == "{0}"
    assert _declare(state)
