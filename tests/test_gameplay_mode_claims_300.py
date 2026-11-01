"""#300 gameplay: retained Charm mode request136/181, independent of draft300.

Board136 has Loot6481/4 vs Kinnan6552/2. The offered effect adds a counter
before Loot deals2 damage; no reciprocal damage. No choice-optimality claim.
"""

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision

FIXTURE = json.loads((Path(__file__).parent / "fixtures/gameplay_300_charm_mode.json").read_text())
REASON = FIXTURE["reasoning"]


def decision():
    raw = deepcopy(FIXTURE["pending_decision"])
    raw["request_id"] = tuple(raw["request_id"])
    raw["slots"] = tuple(raw["slots"])
    raw["options"] = tuple(DecisionOption(**option) for option in raw["options"])
    return PendingDecision(**raw)


def state():
    return dict(
        local_seat_id=2,
        match_id="3d69785b",
        turn=deepcopy(FIXTURE["turn"]),
        players=[dict(seat_id=2, is_local=True, life_total=24), dict(seat_id=1, life_total=25)],
        battlefield=[
            dict(
                instance_id=648,
                grp_id=93819,
                name="Loot, Exuberant Explorer",
                controller_seat_id=2,
                type_line="Legendary Creature — Beast Noble",
                power=1,
                toughness=4,
            ),
            dict(
                instance_id=655,
                grp_id=96284,
                name="Kinnan, Bonder Prodigy",
                controller_seat_id=1,
                type_line="Legendary Creature — Human Druid",
                power=2,
                toughness=2,
            ),
        ],
        hand=[],
        stack=[],
    )


def test_retained_mode_rationale_through_typed_planner():
    prompts = []

    class Backend:
        def complete(self, system, user, **kwargs):
            prompts.append(user)
            return json.dumps(dict(option_ids=["idx:0"], reasoning=REASON))

    planner = ActionPlanner(Backend(), timeout=1)
    snapshot = state()
    original = deepcopy(snapshot)
    assert planner.plan_decision_options(decision(), snapshot) == ["idx:0"]
    assert "one-way damage" in planner._last_decision_reasoning
    assert "fight mode has no profitable target" not in planner._last_decision_reasoning
    assert "has no profitable target" in planner._last_decision_reasoning
    assert "Charm's search mode fetches a land or creature" in planner._last_decision_reasoning
    assert planner._last_decision_trace["uncorrected_reasoning"] == REASON
    assert planner._last_decision_trace["reasoning"] == planner._last_decision_reasoning
    assert "counter before" in prompts[0]
    assert "no reciprocal damage" in prompts[0]
    assert snapshot == original


def check(text, current=None):
    planner = ActionPlanner(Mock())
    planner._last_decision_reasoning = text
    planner._last_decision_trace = {}
    planner._note_false_claims(state(), current or decision(), ["idx:0"])
    return planner._last_decision_reasoning, planner._last_decision_trace


@pytest.mark.parametrize(
    "text",
    [
        "Search is best for ramp; there is no profitable damage target.",
        "Prey Upon's fight mode would trade Loot for Kinnan.",
        "Prey Upon's fight mode deals reciprocal damage.",
        "The fight mode of Prey Upon deals reciprocal damage.",
        "If fight mode were available, we could use it.",
        "Fight mode would take reciprocal damage if it were offered.",
        "This mode is not a fight; damage only goes one way.",
        "Fight mode is not what this effect does.",
    ],
)
def test_unrelated_conditional_and_negated_fight_claims_survive(text):
    assert check(text) == (text, {})


@pytest.mark.parametrize(
    "text",
    [
        "Fight mode makes Loot take 2 damage back.",
        "The fight mode causes reciprocal damage.",
        "Mode 2 is a fight.",
    ],
)
def test_explicit_fight_or_reciprocal_claim_is_replaced_with_ordered_rules(text):
    corrected, trace = check(text)
    assert "counter before" in corrected
    assert "no reciprocal damage" in corrected
    assert "take 2 damage back" not in corrected
    assert trace["uncorrected_reasoning"] == text


@pytest.mark.parametrize(
    "change",
    [
        "actual_fight",
        "mixed_fight",
        "missing_rules",
        "bad_index",
        "other_child",
        "other_source",
        "stale_source",
        "multiple_modes",
    ],
)
def test_unknown_actual_fight_or_ambiguous_menu_does_not_rewrite(change):
    current = decision()
    options = list(current.options)
    if change == "actual_fight":
        options[1] = replace(
            options[1], label="Mode 2: Target creature you control fights target creature you do not control."
        )
    elif change == "mixed_fight":
        options[2] = replace(
            options[2], label="Mode 3: Target creature you control fights target creature you do not control."
        )
    elif change == "missing_rules":
        options[2] = replace(options[2], label="Mode 3")
    elif change == "bad_index":
        options[1] = replace(options[1], meta={**options[1].meta, "optionIndex": None})
    elif change == "other_child":
        options[2] = replace(options[2], meta={**options[2].meta, "childIndex": 1})
    elif change == "other_source":
        options[2] = replace(options[2], meta={**options[2].meta, "sourceId": 999})
    elif change == "stale_source":
        current = replace(current, source_instance_id=999)
    elif change == "multiple_modes":
        current = replace(current, max_select=2)
    current = replace(current, options=tuple(options))
    assert check(REASON, current) == (REASON, {})


def test_target_slot_polarity_is_preserved_for_counter_then_damage():
    from arenamcp.decisions import TargetSlot

    current = PendingDecision(
        (137, 182),
        "SelectTargets",
        (),
        slots=(
            TargetSlot(0, 1, 1, 0, (648,)),
            TargetSlot(1, 1, 1, 0, (655,)),
        ),
    )
    oracle = decision().options[1].label.split(": ", 1)[1]
    controllers = {648: 2, 655: 1}
    assert ActionPlanner._unclassified_pick_reason(current, 648, True, controllers, oracle)
    assert ActionPlanner._unclassified_pick_reason(current, 655, False, controllers, oracle)
    assert check(REASON, current) == (REASON, {})
