"""Pay one mana source at a time when Arena offers no Auto Pay.

2026-10-18 21:40 (Linux/Proton): Shivan Devastator at X=5 (5 generic + R, all
six sources) arrived as a PayCostsReq with paymentActions and no Auto Pay; the
autopilot could only ask for Auto Pay and went MANUAL REQUIRED.
"""

from __future__ import annotations

from arenamcp.mana_payment import choose_payment_source


def source(iid, colors, treasure=False, ability=1000):
    return {"instance_id": iid, "ability_grp_id": ability, "colors": colors, "treasure": treasure}


def context(requirements, sources, modeled=True):
    return {"mana_requirements": requirements, "payment_sources": sources, "requirements_modeled": modeled}


def test_colored_requirement_is_paid_before_generic_from_the_narrowest_source():
    sources = [source(1, ["R", "G"]), source(2, ["R"]), source(3, ["G"]), source(4, ["B"])]
    assert choose_payment_source(context({"generic": 5, "R": 1}, sources)) == {
        "instance_id": 2,
        "ability_grp_id": 1000,
        "color": "R",
    }


def test_the_scarcest_color_is_paid_first():
    sources = [source(1, ["B", "G"]), source(2, ["G"]), source(3, ["G"])]
    step = choose_payment_source(context({"B": 1, "G": 1}, sources))
    assert step["color"] == "B" and step["instance_id"] == 1


def test_treasures_are_spent_last():
    sources = [source(9, ["W", "U", "B", "R", "G"], treasure=True), source(5, ["W", "U"])]
    assert choose_payment_source(context({"generic": 1}, sources))["instance_id"] == 5
    assert choose_payment_source(context({"U": 1}, sources))["instance_id"] == 5


def test_unplannable_costs_are_left_to_the_player():
    sources = [source(1, ["G"])]
    assert choose_payment_source(context({"R": 1}, sources)) is None  # nothing makes red
    assert choose_payment_source(context({"generic": 2}, sources, modeled=False)) is None  # hybrid
    assert choose_payment_source(context({"Any": 1}, sources)) is None
    assert choose_payment_source(context({"generic": 1}, [])) is None


def test_log_capture_lists_each_source_its_colors_and_treasures():
    from arenamcp.gamestate import GameState
    from arenamcp.gamestate_decisions import _handle_pay_costs

    req = {
        "manaCost": [
            {"color": ["ManaColor_Generic"], "count": 2, "objectId": 582},
            {"color": ["ManaColor_Green"], "count": 1, "objectId": 582},
        ],
        "paymentActions": {
            "actions": [
                {
                    "actionType": "ActionType_Activate_Mana",
                    "instanceId": 536,
                    "abilityGrpId": 1131,
                    "manaPaymentOptions": [
                        {
                            "mana": [
                                {"color": "ManaColor_Red", "specs": [{"type": "ManaSpecType_Predictive"}]}
                            ]
                        },
                        {
                            "mana": [
                                {"color": "ManaColor_Green", "specs": [{"type": "ManaSpecType_Predictive"}]}
                            ]
                        },
                    ],
                },
                {
                    "actionType": "ActionType_Activate_Mana",
                    "instanceId": 578,
                    "abilityGrpId": 183,
                    "manaPaymentOptions": [
                        {
                            "mana": [
                                {"color": "ManaColor_White", "specs": [{"type": "ManaSpecType_FromTreasure"}]}
                            ]
                        }
                    ],
                },
            ]
        },
    }
    state = GameState()
    _handle_pay_costs(state, {"payCostsReq": req})
    ctx = state.decision_context
    assert ctx["payment_sources"] == [
        {"instance_id": 536, "ability_grp_id": 1131, "colors": ["G", "R"], "treasure": False},
        {"instance_id": 578, "ability_grp_id": 183, "colors": ["W"], "treasure": True},
    ]
    assert ctx["requirements_modeled"] is True
    assert choose_payment_source(ctx)["instance_id"] == 536


def test_mac_adapter_submits_a_copy_holding_only_the_chosen_color():
    from arenamcp.mac_bridge_adapter import MESSAGING, MacBridgeAdapter

    red = {
        "$c": "ManaPaymentOption",
        "$h": 31,
        "mana_": {"$items": [{"color_": {"e": "ManaColor_Red", "v": 4}}]},
    }
    green = {
        "$c": "ManaPaymentOption",
        "$h": 32,
        "mana_": {"$items": [{"color_": {"e": "ManaColor_Green", "v": 5}}]},
    }
    action = {
        "$c": "Wotc.Mtgo.Gre.External.Messaging.Action",
        "$h": 20,
        "instanceId_": 536,
        "abilityGrpId_": 1131,
        "manaPaymentOptions_": {"$items": [red, green]},
    }
    request = {
        "$c": "GreClient.Rules.PayCostsRequest",
        "$h": 7,
        "PaymentActions": {"$c": "ActionsAvailableRequest", "$h": 10, "Actions": {"$items": [action]}},
    }
    sent = []

    def send(command, timeout):
        sent.append(command)
        if len(sent) == 1:
            return {"ok": True, "results": [request, None]}
        return {"ok": True, "results": []}

    adapter = MacBridgeAdapter(send, "il2cpp-macos")
    reply = adapter.handle(
        {
            "action": "submit_payment_action",
            "instance_id": 536,
            "ability_grp_id": 1131,
            "color": "ManaColor_Green",
        }
    )
    assert reply == {"ok": True, "submitted_type": "PaymentAction", "instance_id": 536}
    ops = sent[-1]["ops"]
    assert ops[0]["op"] == "pending" and ops[1] == {"op": "expect_pending", "target": {"h": 7}}
    assert ops[2] == {"op": "new", "class": MESSAGING + "Action", "args": [{"h": 20}], "depth": 0}
    assert ops[3]["op"] == "get" and ops[3]["member"] == "ManaPaymentOptions"
    assert ops[4]["method"] == "Clear" and ops[5]["method"] == "Add" and ops[5]["args"] == [{"h": 32}]
    assert ops[6]["target"] == {"h": 10} and ops[6]["method"] == "SubmitAction"
    assert ops[6]["args"] == [{"ref": 2}, {"bool": False}]
