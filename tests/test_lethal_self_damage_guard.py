from copy import deepcopy

import pytest

from arenamcp.action_planner import DECLINE_DECISION, ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.self_harm_guard import false_power_survival_claim


def position():
    return {
        "local_seat_id": 2,
        "players": [{"seat_id": 2, "is_local": True}, {"seat_id": 1}],
        "battlefield": [
            {
                "instance_id": 287,
                "name": "Slickshot Show-Off",
                "controller_seat_id": 2,
                "type_line": "Creature",
                "power": 5,
                "toughness": 2,
                "oracle_text": "Whenever you cast a noncreature spell, Slickshot Show-Off gets +2/+0 until end of turn.",
            }
        ],
        "stack": [
            {
                "instance_id": 304,
                "name": "Burst Lightning",
                "controller_seat_id": 2,
                "oracle_text": "Burst Lightning deals 2 damage to any target. If this spell was kicked, it deals 4 damage instead.",
            },
            {
                "instance_id": 303,
                "name": "Shock",
                "controller_seat_id": 1,
                "targeting": [287],
                "oracle_text": "Shock deals 2 damage to any target.",
            },
        ],
        "decision_context": {"source_id": 304},
    }


def choose(state, reason="The pump saves our Slickshot so it survives Shock.", ids=(287, 1)):
    planner = ActionPlanner.__new__(ActionPlanner)
    planner._last_decision_trace = {}
    planner._last_decision_target_controllers = {"tgt:287": "self"}
    planner._last_decision_unusual_targets = {"tgt:287": reason}
    decision = PendingDecision(
        (110, 164),
        "SelectTargets",
        tuple(DecisionOption(f"tgt:{i}", str(i)) for i in ids),
        source_label="Burst Lightning",
    )
    return planner._gate_harmful_llm_target_picks(decision, state, ["tgt:287"]), planner._last_decision_trace


def test_confident_false_survival_cannot_authorize_own_lethal_damage():
    picked, trace = choose(position())
    assert picked == ["tgt:1"]
    assert "lethal" in trace["self_harm_guard"]["tgt:287"]


def test_no_safe_target_declines_instead_of_killing_our_creature():
    assert choose(position(), ids=(287,))[0] == [DECLINE_DECISION]


@pytest.mark.parametrize("change", ["death", "nonlethal", "indestructible", "beneficial"])
def test_grounded_intentional_self_targeting_survives(change):
    state = position()
    reason = "Intentionally trigger its death ability to draw a card."
    if change == "death":
        state["battlefield"][0]["oracle_text"] = "When this creature dies, draw a card."
    elif change == "nonlethal":
        state["battlefield"][0]["toughness"] = 4
    elif change == "indestructible":
        state["battlefield"][0]["oracle_text"] = "Indestructible"
    else:
        state["stack"][0]["oracle_text"] = "Target creature gets +3/+3 until end of turn."
    assert choose(state, reason)[0] == ["tgt:287"]


def test_invented_death_payoff_does_not_bypass_guard():
    assert choose(position(), "Trigger its death ability to draw a card.")[0] == ["tgt:1"]


def test_survival_contradiction_requires_linked_threat_and_affirmative_claim():
    s = position()
    wrong = "Pumping Slickshot to 7/2 saves it from Shock."
    assert false_power_survival_claim(s, wrong)
    assert not false_power_survival_claim(s, "Pumping Slickshot does not save it from Shock.")
    unlinked = deepcopy(s)
    unlinked["stack"][1]["targeting"] = [999]
    assert not false_power_survival_claim(unlinked, wrong)


def test_damage_trigger_payoff_requires_real_rules_and_explicit_intent():
    state = position()
    state["battlefield"][0]["oracle_text"] = "Whenever this creature is dealt damage, draw a card."
    assert choose(state, "Intentionally trigger its damage ability to draw a card.")[0] == ["tgt:287"]
    assert choose(state, "The pump saves it.")[0] == ["tgt:1"]


@pytest.mark.parametrize("kind", ["marked_damage", "modified_toughness"])
def test_current_toughness_and_marked_damage_determine_lethality(kind):
    state = position()
    card = state["battlefield"][0]
    card["toughness"] = 4
    card["damage" if kind == "marked_damage" else "modified_toughness"] = 2
    assert choose(state)[0] == ["tgt:1"]


def test_good_arena_highlight_does_not_override_proven_lethal_self_damage(monkeypatch):
    monkeypatch.setattr(ActionPlanner, "_target_highlights", staticmethod(lambda decision, state: {287: "Good"}))
    assert choose(position())[0] == ["tgt:1"]
