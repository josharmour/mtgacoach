"""Activation-to-selection continuity; equivalent requests, not a full game replay.

G08 audit + retained log: 8b7bed14 T12 Main2, requests (398,557)/(399,560),
Greaves source 724 / ability 2152. The actual candidate list was not retained.
The small legal menu below reconstructs the mechanism, not the whole board.
"""

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.activation_intent import activation_intent_note
from arenamcp.decisions import build_pending_decision

REASON = (
    "Equipping Greaves for free gives a summoning-sick Notary Hobbits haste so it can tap "
    "for mana this turn, helping fund Tooth and Nail; animating tapped Mutavault does nothing."
)


@pytest.fixture(autouse=True)
def local_ability_text(monkeypatch):
    monkeypatch.setattr("arenamcp.decisions._default_ability_description", lambda aid: ("Equip", "Equip {0}"))
    monkeypatch.setattr("arenamcp.action_planner._ability_rules_text", lambda aid: "Equip {0}.")


def game():
    return {
        "match_id": "8b7bed14-4181-42f2-b1da-e888689a7b8c",
        "local_seat_id": 1,
        "turn": {"turn_number": 12, "active_player": 1, "priority_player": 1, "phase": "Phase_Main2"},
        "players": [{"seat_id": 1, "is_local": True, "life_total": 9}, {"seat_id": 2, "life_total": 10}],
        "battlefield": [
            {
                "instance_id": 724,
                "name": "Lightning Greaves",
                "type_line": "Artifact — Equipment",
                "controller_seat_id": 1,
                "oracle_text": "Equipped creature has haste and shroud. Equip {0}.",
            },
            {
                "instance_id": 734,
                "name": "The Notary Hobbits",
                "type_line": "Legendary Creature",
                "controller_seat_id": 1,
                "power": 1,
                "toughness": 1,
            },
            {
                "instance_id": 738,
                "name": "Vorinclex",
                "type_line": "Legendary Creature",
                "controller_seat_id": 1,
                "power": 6,
                "toughness": 6,
            },
        ],
    }


def request(state, kind="SelectTargets", *, source=724, ability=2152):
    names = {c["instance_id"]: c["name"] for c in state["battlefield"]}
    payload = {"sourceId": source, "targetSelections": [{"targetingAbilityGrpId": ability}]}
    poll = {
        "has_pending": True,
        "request_type": kind,
        "game_state_id": 399,
        "msg_id": 560,
        "source_card": "Lightning Greaves",
        "source_instance_id": source,
        "request_payload": payload,
        "target_candidates": [
            {"instanceId": 734, "legalAction": "Select", "targetIdx": 1},
            {"instanceId": 738, "legalAction": "Select", "targetIdx": 1},
            {"instanceId": 999, "legalAction": "Illegal", "targetIdx": 1},
        ],
    }
    if kind == "ActionsAvailable":
        poll.update(
            game_state_id=398,
            msg_id=557,
            actions=[
                {"actionType": "Activate", "instanceId": 724, "abilityGrpId": 2152},
                {"actionType": "Pass"},
            ],
        )
    elif kind in ("SelectN", "Search"):
        poll.update(select_n_ids=[734, 738], select_n_min=1, select_n_max=1)
    elif kind == "CastingTimeOptions":
        poll.update(
            actions=[
                {
                    "actionType": "CastingTimeOption",
                    "choiceKind": "modal",
                    "childIndex": 0,
                    "min": 1,
                    "max": 1,
                    "sourceId": source,
                    "abilityGrpId": ability,
                }
            ]
        )
    state.update(_bridge_request_type=kind, _bridge_request_payload=payload)
    return build_pending_decision(
        poll,
        resolve_name=lambda gid: "",
        resolve_instance=names.get,
        resolve_zone=lambda iid: "Battlefield",
        resolve_card=lambda iid: {},
        resolve_prompt=lambda pid: "",
        activation_unaffordable=lambda action: False,
    )


class Backend:
    def __init__(self):
        self.prompts = []

    def complete(self, system, user, *args, **kwargs):
        self.prompts.append(user)
        return json.dumps({"option_ids": ["tgt:734"], "reasoning": "Keep the mana relay intent."})


def test_equipment_target_prompt_preserves_activation_and_current_phase():
    state = game()
    planner = ActionPlanner(Backend(), separate_strategy_planning=True)
    action = request(state, "ActionsAvailable")
    assert action.selection_is_valid(["idx:0"])
    planner._remember_cast_intent(action, state, ["idx:0"], REASON)
    target = request(state)
    assert "tgt:999" not in target.option_ids()
    assert planner._llm_decision_options(target, state) == ["tgt:734"]
    prompt = planner._backend.prompts[-1]
    assert "ACTIVATION INTENT" in prompt
    assert REASON in prompt
    assert "Phase_Main2" in prompt
    assert "only the current legal options" in prompt
    assert prompt.index("ACTIVATION INTENT") < prompt.index("OPTIONS:")
    assert planner._last_decision_trace["activation_intent"] in prompt


def test_activation_does_not_populate_cast_line_target_memory():
    state = game()
    planner = ActionPlanner(None)
    planner._remember_cast_intent(request(state, "ActionsAvailable"), state, ["idx:0"], REASON)
    assert planner._cast_intent is None


def test_same_named_different_source_does_not_inherit_activation_intent():
    state = game()
    planner = ActionPlanner(Backend(), separate_strategy_planning=True)
    planner._remember_cast_intent(request(state, "ActionsAvailable"), state, ["idx:0"], REASON)
    state["battlefield"].append(dict(state["battlefield"][0], instance_id=725))
    target = request(state, source=725)
    planner._llm_decision_options(target, state)
    assert "ACTIVATION INTENT" not in planner._backend.prompts[-1]


def remembered():
    state = game()
    planner = ActionPlanner(None)
    planner._remember_cast_intent(request(state, "ActionsAvailable"), state, ["idx:0"], REASON)
    return state, planner


@pytest.mark.parametrize("mismatch", ["ability", "turn", "match", "active", "request", "source", "age"])
def test_stale_or_different_activation_never_lends_its_reason(mismatch):
    state, planner = remembered()
    decision = request(state)
    intent = planner._activation_intent
    if mismatch == "ability":
        state["_bridge_request_payload"]["targetSelections"][0]["targetingAbilityGrpId"] = 9999
    elif mismatch == "turn":
        state["turn"]["turn_number"] += 1
    elif mismatch == "match":
        state["match_id"] = "a different game"
    elif mismatch == "active":
        state["turn"]["active_player"] = 2
    elif mismatch == "request":
        decision = replace(decision, request_id=(397, 556))
    elif mismatch == "source":
        state["_bridge_request_payload"] = {}
    else:
        intent = replace(intent, at=intent.at - 61)
    assert activation_intent_note(intent, decision, state) == ""


def test_ability_object_resolves_through_its_parent_and_rejects_other_parent():
    state, planner = remembered()
    decision = request(state, source=9000)
    state["stack"] = [{"instance_id": 9000, "parent_instance_id": 724, "name": "Equip ability"}]
    assert REASON in activation_intent_note(planner._activation_intent, decision, state)
    state["stack"][0]["parent_instance_id"] = 725
    assert not activation_intent_note(planner._activation_intent, decision, state)


@pytest.mark.parametrize("kind", ["SelectN", "Search"])
def test_activation_reason_survives_legal_non_target_selection(kind):
    state, planner = remembered()
    decision = request(state, kind)
    assert decision.selection_is_valid(["sel:734"])
    assert REASON in activation_intent_note(planner._activation_intent, decision, state)
    planner._remember_cast_intent(decision, state, ["sel:734"], "Choose the available card")
    assert planner._activation_intent is not None


@pytest.mark.parametrize("finish", ["target", "cancel", "pass", "other_action"])
def test_completed_or_cancelled_activation_does_not_leak_into_later_requests(finish):
    state, planner = remembered()
    decision = request(state)
    if finish == "target":
        chosen = ["tgt:734"]
    elif finish == "cancel":
        chosen = ["__decline__"]
    else:
        decision = request(state, "ActionsAvailable")
        if finish == "other_action":
            option = replace(decision.options[0], meta={"actionType": "ActionType_Play", "instanceId": 888})
            decision = replace(decision, options=(option,))
            chosen = ["idx:0"]
        else:
            chosen = ["pass"]
    planner._remember_cast_intent(decision, state, chosen, "complete")
    assert planner._activation_intent is None


def test_illegal_or_unpayable_activation_is_not_remembered():
    state = game()
    decision = request(state, "ActionsAvailable")
    planner = ActionPlanner(None)
    planner._remember_cast_intent(decision, state, ["idx:99"], REASON)
    assert planner._activation_intent is None
    option = replace(decision.options[0], payable=False)
    planner._remember_cast_intent(replace(decision, options=(option,)), state, ["idx:0"], REASON)
    assert planner._activation_intent is None


def test_missing_old_target_does_not_rewrite_current_legal_menu():
    state, planner = remembered()
    decision = request(state)
    decision = replace(decision, options=tuple(o for o in decision.options if o.option_id == "tgt:738"))
    before = deepcopy(decision)
    note = activation_intent_note(planner._activation_intent, decision, state)
    assert "no longer legal" in note
    assert decision == before
    assert not decision.selection_is_valid(["tgt:734"])
    assert decision.selection_is_valid(["tgt:738"])


def test_activation_mode_selection_preserves_the_purpose_for_following_targets():
    state, planner = remembered()
    mode = request(state, "CastingTimeOptions")
    assert mode.selection_is_valid(["idx:0"])
    assert REASON in activation_intent_note(planner._activation_intent, mode, state)
    planner._remember_cast_intent(mode, state, ["idx:0"], "Select the activation's mode")
    target = replace(request(state), request_id=(400, 561))
    assert REASON in activation_intent_note(planner._activation_intent, target, state)


def test_prompt_distinguishes_original_timing_from_current_timing():
    state = game()
    state["turn"]["phase"] = "Phase_Main1"
    planner = ActionPlanner(None)
    planner._remember_cast_intent(request(state, "ActionsAvailable"), state, ["idx:0"], REASON)
    state["turn"]["phase"] = "Phase_Main2"
    target = request(state)
    note = activation_intent_note(planner._activation_intent, target, state)
    assert "during Phase_Main1" in note
    assert "Current timing: Phase_Main2" in note
