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


@pytest.mark.parametrize("kind", ["Planeswalker", "Battle"])
def test_legacy_ids_cannot_confirm_player_recipient_with_alternate_visible(kind):
    state = _state()
    state["battlefield"].append(
        {
            "name": "Other recipient",
            "instance_id": 893113,
            "controller_seat_id": 2,
            "card_types": [f"CardType_{kind}"],
            "type_line": kind,
        }
    )
    assert _declare(state) is None
    ctx = state["decision_context"]
    ctx["raw_attackers"] = [
        {
            "attackerInstanceId": iid,
            "legalDamageRecipients": [
                {"type": "DamageRecType_Player", "playerSystemSeatId": 2},
                {f"{kind.lower()}InstanceId": 893113},
            ],
        }
        for iid in ctx["legal_attacker_ids"]
    ]
    assert _declare(state)


@pytest.mark.parametrize(
    "effect",
    [
        "No more than one creature can attack each combat.",
        "No more than two creatures can attack you each combat.",
    ],
)
def test_individually_legal_attackers_do_not_override_a_joint_attack_limit(effect):
    from tests.bug_180436_states import attack_state

    from arenamcp.combat_targets import all_out_declaration_allowed

    for state in (_state(), attack_state(3)):
        opponent = next(p["seat_id"] for p in state["players"] if not p.get("is_local"))
        state["battlefield"].append(
            {
                "instance_id": 891111,
                "type_line": "Artifact",
                "controller_seat_id": opponent,
                "oracle_text": effect,
            }
        )
        assert all_out_declaration_allowed(state, 1)
        assert not all_out_declaration_allowed(state, 3)
        assert _declare(state) is None
    state = _state()
    state["battlefield"].append(
        {
            "instance_id": 891111,
            "type_line": "Artifact",
            "controller_seat_id": state["local_seat_id"],
            "oracle_text": "No more than two creatures can attack you each combat.",
        }
    )
    assert _declare(state)


@pytest.mark.parametrize(
    "field,value",
    [
        ("hasRestrictions", True),
        ("hasRequirements", True),
        ("AttackWarnings", [{"type": "AttackWarningType_CannotBeAttackedByMoreThanOne"}]),
        ("attackWarnings", [{"type": "AttackWarningType_UnpayableEffectCost"}]),
        ("manaCost", [{"count": 1}]),
        ("hasRestrictions", False),
    ],
)
def test_gre_log_and_windows_bridge_preserve_joint_declaration_evidence(field, value):
    from arenamcp.gamestate import GameState
    from arenamcp.gamestate_decisions import _handle_decision_message
    from arenamcp.gre_bridge import _apply_bridge_attackers

    state = _state()
    legal = state["decision_context"]["legal_attacker_ids"]
    rows = [
        {
            "attackerInstanceId": i,
            "legalDamageRecipients": [{"type": "DamageRecType_Player", "playerSystemSeatId": 2}],
        }
        for i in legal
    ]
    request = {"qualifiedAttackers": rows, field: value}
    parsed = GameState()
    _handle_decision_message(parsed, "GREMessageType_DeclareAttackersReq", {"declareAttackersReq": request})
    state["decision_context"] = parsed.decision_context
    assert bool(_declare(state)) == (value is False)
    state["decision_context"] = {}
    _apply_bridge_attackers(state, {"request_type": "DeclareAttackersRequest", "request_payload": request})
    assert bool(_declare(state)) == (value is False)


@pytest.mark.parametrize(
    "field,value",
    [
        ("HasRestrictions", True),
        ("HasRequirements", True),
        ("ManaRequirements", [{"count_": 1}]),
        ("AttackWarnings", [{"Type": {"e": "UnpayableEffectCost", "v": 4}}]),
        ("AttackWarnings", {"$n": 1, "$items": []}),
        ("AttackWarnings", {"$n": False, "$items": []}),
        ("ManaRequirements", {"$n": 0, "$items": [], "$error": "partial"}),
        ("ManaRequirements", []),
        ("HasRestrictions", False),
    ],
)
def test_macos_native_shaper_preserves_an_attack_limit_without_an_extra_call(field, value):
    from tests.test_mac_bridge_adapter import MSG, FakeGame, adapter_for, enum, listing

    from arenamcp.gre_bridge import _apply_bridge_attackers

    state = _state()
    native_recipient = {
        "$c": MSG + "DamageRecipient",
        "$h": 31,
        "type_": enum("Player", 1),
        "playerSystemSeatId_": 2,
    }
    offered = [
        {
            "$c": MSG + "Attacker",
            "$h": i,
            "attackerInstanceId_": i,
            "legalDamageRecipients_": listing(native_recipient),
        }
        for i in state["decision_context"]["legal_attacker_ids"]
    ]
    game = FakeGame(
        {
            "$c": "GreClient.Rules.DeclareAttackerRequest",
            "$h": 5,
            "QualifiedAttackers": listing(*offered),
            field: listing(*value) if isinstance(value, list) else value,
        }
    )
    response = adapter_for(game).handle({"action": "get_pending_actions"})
    assert bool(response.get("attack_declaration_needs_validation")) == bool(value)
    _apply_bridge_attackers(state, response)
    assert bool(_declare(state)) == (not bool(value))


@pytest.mark.parametrize(
    "facts,allowed",
    [
        ({}, False),
        ({"hasRestrictions": False}, False),
        ({"hasRestrictions": False, "hasRequirements": False, "attackWarnings": [], "manaCost": []}, True),
        (
            {
                "hasRestrictions": False,
                "hasRequirements": False,
                "attackWarnings": [],
                "manaRequirements": [{"count": 1}],
            },
            False,
        ),
    ],
)
def test_windows_bridge_with_individual_offers_needs_explicit_joint_evidence(monkeypatch, facts, allowed):
    import sys

    from arenamcp.gre_bridge import _apply_bridge_attackers

    state = _state()
    ids = state["decision_context"]["legal_attacker_ids"]
    rows = [
        {"attackerInstanceId": i, "legalDamageRecipients": [{"type": "Player", "playerSystemSeatId": 2}]}
        for i in ids
    ]
    poll = {
        "request_type": "DeclareAttackersRequest",
        "attackers": rows,
        "request_payload": {
            "requestType": "DeclareAttackers",
            "requestClass": "DeclareAttackerRequest",
            **facts,
        },
    }
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "win32")
        _apply_bridge_attackers(state, poll)
    assert bool(_declare(state)) == allowed


def test_empty_windows_attack_poll_cannot_force_a_previous_menu(monkeypatch):
    import sys

    from arenamcp.gre_bridge import _apply_bridge_attackers

    state = _state()
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "win32")
        _apply_bridge_attackers(state, {"request_type": "DeclareAttackersRequest"})
    assert _declare(state) is None


@pytest.mark.parametrize("life,expected", [(10, False), (11, True), (24, True), (25, False)])
def test_both_combat_totals_use_only_the_offered_attacker_subset(life, expected):
    from tests.test_all_in_labels_20261020 import _declare_step_board_order_differs

    state, legal = _declare_step_board_order_differs()
    state["players"][0]["life_total"] = 60
    state["players"][1]["life_total"] = life
    hold = sneak.sneak_main_hold(state, declare_step=True, force_declaration=True)
    assert bool(hold) == expected
    if hold:
        assert len(legal) == 14 and (hold.through, hold.total) == (10, 24)
    assert bool(_declare(state)) == expected


@pytest.mark.parametrize(
    "bad", ["unknown_id", "duplicate", "fraction", "missing_menu", "not_ready", "empty", "raw_extra"]
)
def test_uncertain_offered_subset_still_withholds_a_forced_declaration(bad):
    from tests.test_all_in_labels_20261020 import _declare_step_board_order_differs

    state, legal = _declare_step_board_order_differs()
    state["players"][0]["life_total"] = 60
    ctx = state["decision_context"]
    if bad == "unknown_id":
        legal.append(123456789)
    elif bad == "duplicate":
        legal.append(legal[-1])
    elif bad == "fraction":
        legal[0] = float(legal[0])
    elif bad == "missing_menu":
        ctx.pop("legal_attacker_ids")
    elif bad == "empty":
        legal.clear()
    elif bad == "not_ready":
        card = next(c for c in state["battlefield"] if c["instance_id"] == legal[0])
        card["is_tapped"] = True
    elif bad == "raw_extra":
        ctx["raw_attackers"] = [
            {"attackerInstanceId": i, "legalDamageRecipients": [{"type": "Player", "playerSystemSeatId": 2}]}
            for i in [*legal, 123456789]
        ]
    assert not sneak.sneak_main_hold(state, declare_step=True, force_declaration=True)
    assert _declare(state) is None


@pytest.mark.parametrize(
    "extra,allowed",
    [
        ({"playerSystemSeatId": 2}, False),
        ({"playerSystemSeatId": 3}, False),
        ({"playerSystemSeatId": 2, "planeswalkerInstanceId": 123456}, False),
        ({"playerSystemSeatId": 1, "teamId": 3}, False),
        ({"type": "DamageRecType_Player", "planeswalkerInstanceId": 123456}, False),
        ({}, False),
        ({"type": "DamageRecType_Planeswalker", "planeswalkerInstanceId": 123456}, True),
    ],
)
def test_legal_subset_requires_the_normal_bridge_to_resolve_opponent_uniquely(extra, allowed):
    from tests.test_all_in_labels_20261020 import _declare_step_board_order_differs

    state, legal = _declare_step_board_order_differs()
    state["players"][0]["life_total"] = 60
    state["decision_context"]["raw_attackers"] = [
        {"attackerInstanceId": i, "legalDamageRecipients": [{"type": "Player", "playerSystemSeatId": 2}]}
        for i in legal
    ]
    state["decision_context"]["raw_attackers"][0]["legalDamageRecipients"].append(extra)
    assert bool(_declare(state)) == allowed
