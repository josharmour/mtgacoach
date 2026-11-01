"""Saved Greaves/Notary report: exact action/target lists, reconstructed T11 board."""

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import build_pending_decision
from arenamcp.equipment_opportunities import equipment_opportunity_note, equipment_pass_concern
from arenamcp.game_plan import decision_claim_notes
from arenamcp.mac_bridge_adapter import serialize_action


@pytest.fixture
def saved(monkeypatch):
    monkeypatch.setattr("arenamcp.decisions._default_ability_description", lambda aid: ("{0}", "Equip {0}"))
    monkeypatch.setattr("arenamcp.action_planner._ability_rules_text", lambda aid: "Equip {0}")
    return json.loads((Path(__file__).parent / "fixtures/greaves_20261031.json").read_text())


def decision(saved, gid="222"):
    request = saved["requests"][gid]
    cards = {c["grp_id"]: c["name"] for c in saved["state"]["battlefield"] + saved["state"]["hand"]}
    return build_pending_decision(
        {
            "has_pending": True,
            "request_type": "ActionsAvailable",
            "game_state_id": request["gameStateId"],
            "msg_id": request["msgId"],
            "actions": request["actionsAvailableReq"]["actions"],
        },
        resolve_name=lambda gid: cards.get(gid, ""),
    )


def test_saved_request_offers_all_three_fresh_notaries_and_zero_cost_equip(saved):
    state = saved["state"]
    gear = next(c for c in state["battlefield"] if c["instance_id"] == 698)
    assert "Equip {o0}" in gear["oracle_text"] and not gear.get("attached_to_id")
    bodies = [c for c in state["battlefield"] if c["instance_id"] in {689, 696, 697}]
    assert len(bodies) == 3 and all(not c["is_tapped"] and c["summoning_sickness"] for c in bodies)
    assert all(c["controller_seat_id"] == 2 for c in bodies)
    assert all(
        c["is_tapped"]
        for c in state["battlefield"]
        if c["controller_seat_id"] == 2 and "Land" in c["type_line"]
    )
    note = equipment_opportunity_note(decision(saved), state)
    assert all(f"id:{iid}" in note for iid in (689, 696, 697))
    assert "equip {0}" in note and "Before combat" in note
    assert "extra colorless cannot pay green costs" in note and "own targeting" in note


def test_native_bridge_preserves_arena_target_hints_into_typed_options(saved):
    raw = saved["requests"]["222"]["actionsAvailableReq"]["actions"][3]

    def dumped(value):
        if isinstance(value, list):
            return {"$items": [dumped(v) for v in value]}
        if isinstance(value, dict):
            return {k: dumped(v) for k, v in value.items()}
        return value

    shaped = serialize_action(dumped(raw))
    assert {t["targetInstanceId"] for t in shaped["targets"][0]["targets"]} == {672, 689, 696, 697}
    saved["requests"]["222"]["actionsAvailableReq"]["actions"][3] = shaped
    assert "id:696" in equipment_opportunity_note(decision(saved), saved["state"])


def cast_state(saved):
    state = deepcopy(saved["state"])
    gear = next(c for c in state["battlefield"] if c["instance_id"] == 698)
    state["battlefield"].remove(gear)
    gear["instance_id"] = 681
    state["hand"].append(gear)
    return state


def test_real_cast_plan_is_not_rewritten_as_haste_impossible(saved):
    assert not decision_claim_notes(saved["cast_reason"], cast_state(saved), cast_ids=[681])


@pytest.mark.parametrize("case", ["not_cast", "missing", "no_haste", "opponent", "unrelated"])
def test_haste_exception_requires_real_named_source(saved, case):
    state = cast_state(saved)
    source = state["hand"][-1]
    ids = [681]
    text = saved["cast_reason"]
    if case == "not_cast":
        ids = []
    elif case == "missing":
        state["hand"].remove(source)
    elif case == "no_haste":
        source["oracle_text"] = "Equipped creature gets +1/+1. Equip {0}"
    elif case == "opponent":
        state["hand"].remove(source)
        source["controller_seat_id"] = 1
        state["battlefield"].append(source)
    else:
        text = "The Notary Hobbits can tap for mana and attack this turn."
    assert any("summoning sick" in note for note in decision_claim_notes(text, state, cast_ids=ids))


def test_equip_plan_exception_does_not_erase_postcombat_attack_check(saved):
    state = saved["state"]
    state["turn"]["phase"] = "Phase_Main2"
    assert any(
        "attack declaration" in n
        for n in decision_claim_notes("Equip Greaves so Notary Hobbits can attack now.", state)
    )
    note = equipment_opportunity_note(decision(saved, "236"), state)
    assert "Combat has passed" in note


@pytest.mark.parametrize(
    "reason",
    [
        "No castable spell and no useful Greaves target; attacking with Vaultborn Tyrant is the best play, so pass to combat.",
        "No castable spell and Greaves on a summoning-sick Notary token gains nothing this turn; pass to combat and attack with Vaultborn Tyrant.",
    ],
)
def test_recorded_pass_gets_grounded_second_look(saved, reason):
    backend = Mock()
    backend.complete.side_effect = [
        json.dumps({"option_ids": ["pass"], "reasoning": reason}),
        json.dumps(
            {
                "option_ids": ["idx:3"],
                "reasoning": "Equip Greaves to one fresh Hobbit for haste and shroud before combat.",
            }
        ),
    ]
    planner = ActionPlanner(backend, separate_strategy_planning=True)
    assert planner.plan_decision_options(decision(saved), saved["state"]) == ["idx:3"]
    assert backend.complete.call_count == 2
    assert "id:696" in backend.complete.call_args_list[0].args[1]
    assert "EQUIPMENT OPPORTUNITY" in planner._last_decision_trace["second_look"]["concerns"][0]


@pytest.mark.parametrize(
    "case",
    [
        "paid",
        "unpayable",
        "no_targets",
        "cold",
        "tapped",
        "opponent_turn",
        "combat",
        "stack",
        "enemy_targets",
    ],
)
def test_opportunity_requires_free_current_activation_and_eligible_fresh_targets(saved, case):
    state = saved["state"]
    request = decision(saved)
    option = request.find("idx:3")
    if case == "paid":
        option = replace(option, label="Activate: Lightning Greaves [Equip {2}]")
    elif case == "unpayable":
        option = replace(option, payable=False)
    elif case == "no_targets":
        option.meta.pop("targets")
    elif case == "cold":
        for slot in option.meta["targets"]:
            for target in slot["targets"]:
                target["highlight"] = "HighlightType_Cold"
    elif case in {"tapped", "enemy_targets"}:
        for card in state["battlefield"]:
            if card["instance_id"] in {689, 696, 697}:
                card["is_tapped" if case == "tapped" else "controller_seat_id"] = (
                    True if case == "tapped" else 1
                )
    elif case == "opponent_turn":
        state["turn"]["active_player"] = 1
    elif case == "combat":
        state["turn"]["phase"] = "Phase_Combat"
    else:
        state["stack"] = [{"instance_id": 999}]
    request = replace(
        request, options=tuple(option if o.option_id == option.option_id else o for o in request.options)
    )
    assert not equipment_opportunity_note(request, state)


def test_valid_reason_to_pass_is_not_overridden(saved):
    assert not equipment_pass_concern(
        decision(saved),
        saved["state"],
        ["pass"],
        "Hold equip to preserve our own targeted protection and avoid exposing it to a response.",
    )


@pytest.mark.parametrize(
    "text",
    [
        "Equip Greaves for the Notary Hobbits next turn. The Notary Hobbits can attack now.",
        "Greaves can give the Notary Hobbits haste. The Notary Hobbits can attack before equipping.",
    ],
)
def test_grant_plan_does_not_exempt_an_independent_false_claim(saved, text):
    assert any("summoning sick" in n for n in decision_claim_notes(text, saved["state"]))


@pytest.mark.parametrize("case", ["shroud", "tax", "not_creature", "missing"])
def test_conflicting_or_missing_target_facts_do_not_invent_opportunity(saved, case):
    request = decision(saved)
    state = saved["state"]
    if case == "tax":
        request.find("idx:3").meta["manaCost"] = [{"count": 1}]
    else:
        for card in state["battlefield"]:
            if card["instance_id"] in {689, 696, 697}:
                if case == "shroud":
                    card["keywords"] = ["shroud"]
                elif case == "not_creature":
                    card["card_types"] = ["Enchantment"]
                else:
                    card["instance_id"] += 1000
    assert not equipment_opportunity_note(request, state)
