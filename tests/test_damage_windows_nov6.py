"""Public GRE options and battlefield objects from Armour's turns 12 and 22."""

import json
from pathlib import Path
from unittest.mock import Mock

from arenamcp.action_planner import ActionPlanner
from arenamcp.damage_decisions import combat_contradiction, hit_reward, strategy_facts
from arenamcp.decisions import DecisionOption, PendingDecision, TargetSlot
from arenamcp.sweep_sequence import before_selected_sweep, fallback_sweep, repeat_sweep


def window(number):
    entry = json.loads((Path(__file__).parent / "fixtures/nov6_damage_windows.json").read_text())[str(number)]
    raw = entry["decision"]
    if raw:
        raw["options"] = tuple(DecisionOption(**o) for o in raw["options"])
        raw["slots"] = tuple(TargetSlot(**o) for o in raw["slots"])
    return (PendingDecision(**raw) if raw else None), entry["state"]


def test_recorded_power_trigger_does_not_fund_six_lands():
    decision, state = window(800)
    planner = ActionPlanner(backend=Mock())
    planner._llm_decision_options = Mock(return_value=["tgt:1671"])
    assert planner.plan_decision_options(decision, state) == ["tgt:1371"]
    assert "library" in planner._last_decision_reasoning
    assert planner.targeting_fallback_choice(decision, state) == ["tgt:1371"]


def test_target_guard_preserves_lethal_and_does_not_guess_power_or_transplant_an_intent():
    decision, state = window(800)
    planner = ActionPlanner(backend=Mock())
    state["players"][0]["life_total"] = 6
    assert planner._avoid_hit_reward(decision, state, ["tgt:1671"]) == ["tgt:1"]
    state["decision_context"]["targets"][0]["prompt"]["parameters"] = []
    assert planner._avoid_hit_reward(decision, state, ["tgt:1671"]) == ["tgt:1671"]
    assert not hit_reward(
        {"name": "A", "oracle_text": "Whenever another creature is dealt damage, draw a card."}
    )


def test_power_trigger_shows_cast_reason_only_on_its_own_window():
    import time

    decision, state = window(800)
    planner = ActionPlanner(backend=Mock())
    intent = {
        "at": time.monotonic(),
        "card": "Pyrogoyf",
        "window": (state["match_id"], 1, 22),
        "gsid": 797,
        "grp_id": 99698,
        "said": "ETB pings Tatyova",
        "line_target": "",
    }
    planner._cast_intent = intent
    assert "ETB pings Tatyova" in planner._cast_intent_note(decision, state)
    assert not planner._cast_intent_note(decision, {**state, "match_id": "other"})
    state["decision_context"]["source_parent_instance_id"] = 888
    assert not planner._cast_intent_note(decision, state)


def test_visible_zombies_can_block_now_and_attack_next_turn():
    _, state = window(857)
    why = combat_contradiction(state, "All their creatures are tapped so no crackback.")
    assert "8 visibly untapped" in why and "19 total power" in why
    assert why in strategy_facts(state)
    assert not combat_contradiction(state, "Leave a blocker back.")


def test_sweep_uses_three_damage_and_preserves_friendly_bodies_or_lethal():
    d, state = window(296)
    choice = ["idx:0", "idx:0", "idx:1"]
    planner = ActionPlanner(backend=Mock())
    planner._llm_decision_options = Mock(return_value=choice)
    assert planner.plan_decision_options(d, state) == ["idx:0"] * 3
    assert "Voice of Victory" in planner._last_decision_reasoning
    assert fallback_sweep(d, state)[0] == ["idx:0"] * 3
    boo = next(c for c in state["battlefield"] if c["name"] == "Boo")
    boo["toughness"] = 3
    assert repeat_sweep(d, state, choice) is None
    boo["toughness"] = 1
    state["players"][0]["life_total"] = 2
    assert repeat_sweep(d, state, choice) is None


def test_use_doomed_body_before_selected_sweep():
    d, state = window(295)
    planner = ActionPlanner(backend=Mock())

    def choose(*_):
        planner._last_decision_reasoning = "Three damage each creature modes kill Voice."
        return ["idx:2"]

    planner._llm_decision_options = choose
    assert planner.plan_decision_options(d, state) == ["idx:5"]
    line = planner._pending_loyalty_removal[2]
    assert (line.fodder, line.target) == (810, 797)
    assert "last loyalty" in line.reason
    assert before_selected_sweep(d, state, ["idx:0"]) is None
    assert before_selected_sweep(d, state, ["idx:2"], "Destroy their artifact") is None
