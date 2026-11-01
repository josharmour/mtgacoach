"""Exact retained missed relay, reconstructed relevant public state (not a full replay)."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.equipment_opportunities import equipment_opportunity_note, equipment_pass_concern

FIXTURE = json.loads((Path(__file__).parent / "fixtures/greaves_relay_252bd136.json").read_text())


def case(index=0):
    record = deepcopy(FIXTURE["records"][index])
    data = record["decision"]
    data["options"] = tuple(DecisionOption(**o) for o in data["options"])
    data["request_id"] = tuple(data["request_id"])
    state = deepcopy(FIXTURE["state"])
    state["turn"] = record["turn"]
    return PendingDecision(**data), state, record["reason"]


@pytest.mark.parametrize("index", [0, 1])
def test_exact_no_tap_payoff_pass_gets_concrete_ugin_relay_recheck(index):
    decision, state, reason = case(index)
    concern = equipment_pass_concern(decision, state, ["pass"], reason)
    assert "9 colorless" in concern and "Ugin, Eye of the Storms" in concern
    assert all(f"id:{i}" in concern for i in (665, 672, 673))
    note = equipment_opportunity_note(decision, state)
    assert "9 colorless" in note and "7" in note


def test_grounded_recheck_changes_pass_to_current_equip_only(monkeypatch):
    from unittest.mock import Mock

    from arenamcp.action_planner import ActionPlanner

    monkeypatch.setattr("arenamcp.action_planner._ability_rules_text", lambda _aid: "Equip {0}")
    decision, state, reason = case()
    backend = Mock()
    backend.complete.side_effect = [
        json.dumps({"option_ids": ["pass"], "reasoning": reason}),
        json.dumps(
            {
                "option_ids": ["idx:2"],
                "reasoning": "Equip a fresh Notary, then bank its three colorless toward Ugin before moving Greaves.",
            }
        ),
    ]
    planner = ActionPlanner(backend, separate_strategy_planning=True)
    assert planner.plan_decision_options(decision, state) == ["idx:2"]
    assert backend.complete.call_count == 2
    assert "9 colorless" in backend.complete.call_args_list[0].args[1]
    assert "Ugin" in planner._last_decision_trace["second_look"]["concerns"][0]


def test_repeated_false_payoff_is_not_saved_as_accepted_memory(monkeypatch):
    from unittest.mock import Mock

    from arenamcp.action_planner import ActionPlanner

    monkeypatch.setattr("arenamcp.action_planner._ability_rules_text", lambda _aid: "Equip {0}")
    decision, state, reason = case()
    backend = Mock()
    backend.complete.return_value = json.dumps({"option_ids": ["pass"], "reasoning": reason})
    planner = ActionPlanner(backend, separate_strategy_planning=True)
    assert planner.plan_decision_options(decision, state) == ["pass"]
    assert backend.complete.call_count == 2
    assert planner._last_decision_trace["equipment_payoff_claim"]["uncorrected_reasoning"] == reason
    assert "did not justify" in planner._last_decision_reasoning


@pytest.mark.parametrize(
    "change",
    [
        "one_tapped",
        "opponent_controls",
        "phased",
        "no_ability",
        "extra_activation_cost",
        "removed_ability",
        "no_raw_cast",
        "colored_cost",
        "more_expensive",
        "additional_cost",
        "suppressed",
        "no_targets",
        "no_haste",
        "paid_equip",
        "stack",
        "combat",
    ],
)
def test_no_funded_relay_claim_when_evidence_or_payment_fails(change):
    decision, state, _ = case()
    bodies = [c for c in state["battlefield"] if c["grp_id"] == 103511]
    gear = next(c for c in state["battlefield"] if c["grp_id"] == 19737)
    ugin = next(a for a in state["_bridge_actions"] if a.get("instanceId") == 247)
    if change == "one_tapped":
        bodies[0]["is_tapped"] = True
    elif change == "opponent_controls":
        bodies[0]["controller_seat_id"] = 2
    elif change == "phased":
        bodies[0]["is_phased_out"] = True
    elif change == "no_ability":
        bodies[0]["oracle_text"] = ""
    elif change == "extra_activation_cost":
        bodies[0]["oracle_text"] = "{T}, Pay 2 life: Add {C} for each Halfling you control."
    elif change == "removed_ability":
        bodies[0]["removed_abilities"] = ["205210"]
    elif change == "no_raw_cast":
        state["_bridge_actions"] = []
    elif change == "colored_cost":
        ugin["manaCost"] = [
            {"color": ["ManaColor_Generic"], "count": 6},
            {"color": ["ManaColor_Green"], "count": 1},
        ]
    elif change == "more_expensive":
        ugin["manaCost"][0]["count"] = 10
    elif change == "additional_cost":
        state["hand"][0]["oracle_text"] = "As an additional cost to cast this spell, sacrifice a creature."
    elif change == "suppressed":
        state["battlefield"].append(
            {"oracle_text": "Activated abilities of creatures can't be activated.", "controller_seat_id": 2}
        )
    elif change == "no_targets":
        decision.find("idx:2").meta["targets"] = []
    elif change == "no_haste":
        gear["oracle_text"] = "Equipped creature has shroud. Equip {0}"
    elif change == "paid_equip":
        decision.find("idx:2").meta["manaCost"] = [{"count": 1}]
    elif change == "stack":
        state["stack"] = [{"instance_id": 999}]
    elif change == "combat":
        state["turn"]["phase"] = "Phase_Combat"
    assert "COLORLESS RELAY:" not in equipment_opportunity_note(decision, state)


def test_relay_recomputes_after_tap_and_uses_only_actual_offered_banking():
    from dataclasses import replace

    from arenamcp.decisions import build_pending_decision
    from arenamcp.mana import haste_relay_mana_sources

    decision, state, _ = case()
    gear = next(c for c in state["battlefield"] if c["grp_id"] == 19737)
    bodies = sorted(
        (c for c in state["battlefield"] if c["grp_id"] == 103511), key=lambda c: c["instance_id"]
    )
    gear["attached_to_id"] = 665
    mana = {
        "actionType": "ActionType_Activate_Mana",
        "instanceId": 665,
        "grpId": 103511,
        "abilityGrpId": 205210,
    }
    poll = {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "actions": [mana, {"actionType": "ActionType_Pass"}],
    }
    current = build_pending_decision(
        poll, resolve_name=lambda _: "The Notary Hobbits", mana_source_ids=haste_relay_mana_sources(state)
    )
    assert current.find("idx:0").meta["hasteRelayMana"]
    with_bank = replace(decision, options=decision.options + (current.find("idx:0"),))
    note = equipment_opportunity_note(with_bank, state)
    assert "9 colorless" in note
    bodies[0]["is_tapped"] = True
    state["players"][0]["mana_pool"] = {"C": 3}
    note = equipment_opportunity_note(decision, state)
    assert "6 colorless" in note and "plus 3 already banked" in note
    assert "id:665] taps" not in note
    # No fabricated mana option before equipping or when Arena does not offer it.
    gear["attached_to_id"] = None
    assert not haste_relay_mana_sources(state)


def test_grounded_tradeoff_to_pass_is_preserved():
    from arenamcp.equipment_opportunities import correct_equipment_payoff_claim

    decision, state, _ = case()
    reason = "The Ugin relay is available, but keep these creatures untapped to block a known flash threat."
    data = {"reasoning": reason}
    assert not equipment_pass_concern(decision, state, ["pass"], reason)
    assert correct_equipment_payoff_claim(decision, state, ["pass"], data, {}) is data


def test_cost_quotes_do_not_create_selectable_casts_or_survive_missing_fresh_evidence():
    from arenamcp.equipment_relay import relay_cast_costs
    from arenamcp.offered_casts import bind_offered_casts

    decision, state, _ = case()
    quotes = relay_cast_costs(state)
    assert any(a["instanceId"] == 247 for a in quotes)
    bound, filtered = bind_offered_casts(decision, state)
    assert [o.option_id for o in bound.options] == ["idx:2", "pass"]
    assert not any(a.get("instanceId") == 247 for a in filtered["_bridge_actions"])
    filtered["_equipment_relay_costs"] = quotes
    assert "9 colorless" in equipment_opportunity_note(bound, filtered)
    assert relay_cast_costs({"hand": state["hand"], "_equipment_relay_costs": quotes}) == []


def test_exact_raw_packet_has_only_inactive_sick_mana_actions_before_equip():
    req = FIXTURE["raw_requests"][0]["actionsAvailableReq"]
    inactive = {
        a.get("instanceId")
        for a in req["inactiveActions"]
        if a.get("actionType") == "ActionType_Activate_Mana"
    }
    assert {665, 672, 673} <= inactive
    assert not any(
        a.get("instanceId") in {665, 672, 673} and a.get("actionType") == "ActionType_Activate_Mana"
        for a in req["actions"]
    )
    ugin = next(a for a in req["actions"] if a.get("instanceId") == 247)
    assert ugin["manaCost"] == [{"color": ["ManaColor_Generic"], "count": 7}]


def test_no_current_castable_spell_is_not_itself_a_false_relay_claim():
    decision, state, _ = case()
    reason = "No spell is castable now; the Ugin relay exists, but keep the bodies untapped for blocking."
    assert not equipment_pass_concern(decision, state, ["pass"], reason)
