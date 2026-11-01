"""Recorded Oct 27 recurrences of #108/#117/#321/#323/#324.

Sources: overnight-claims-{22eef035,0412,c836bb26}.json,
overnight-108-recurrences.jsonl, overnight-bloodletter-4602e43a.json.
No alternative winning target or live outcome is inferred from these fixtures.
"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests.test_claim_descriptions_20261019 import CLAW, EMBERHEART, SLICKSHOT, _state
from tests.test_false_lethal_claim import _facts, _planner

from arenamcp.action_planner import ActionPlanner, SecondLookError
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.game_plan import corrected_decision_reasoning, decision_claim_notes
from arenamcp.session_facts import damage_attribution_notes, rules_block


@pytest.mark.parametrize(
    "text, creature",
    [
        (
            "Curve out with Emberheart Challenger — haste plus prowess/valiant growth makes it the better engine to stack burn triggers on than Slickshot this turn.",
            EMBERHEART,
        ),
        (
            "Opponent is at 3 life; Emberheart Challenger is a permanent hasty prowess body that attacks for 2 now (leaving them at 1) and stays as a growing threat, whereas Fleeting Effigy bounces back anyway and can be replayed next turn.",
            EMBERHEART,
        ),
        (
            "Effigy's haste lets it attack immediately and its {2}{R} pump closes from 6 life via the replay loop; Hired Claw lacks haste and needs an attack trigger first.",
            CLAW,
        ),
        (
            "Exploring my own Hired Claw digs toward burn/hasty creatures and may permanently grow my only attacker; the effect is helpful so it targets my creature.",
            CLAW,
        ),
    ],
)
def test_recorded_description_is_not_a_false_claim(text, creature):
    state = _state([], [creature, SLICKSHOT])
    assert decision_claim_notes(text, state, cast_ids=[creature["instance_id"]]) == []


def test_removal_target_does_not_acquire_the_fliers_haste_claim():
    target = dict(
        instance_id=42,
        name="Emergency Phytomedic",
        type_line="Creature",
        oracle_text="Reach",
        controller_seat_id=2,
    )
    state = _state([target], [SLICKSHOT, dict(instance_id=43, name="Shock", type_line="Instant")])
    text = "Cast Slickshot now so the follow-up Shock both removes Emergency Phytomedic and pumps it to a hasty 3/2 flier, converting this turn into a real growth-and-pressure turn instead of banking everything."
    assert decision_claim_notes(text, state, cast_ids=[SLICKSHOT["instance_id"]]) == []


def test_false_hasty_body_is_removed_while_independent_intent_survives():
    text = "With 3 mana left after Hazard grew both attackers, deploying Hired Claw adds a third hasty body and a recurring attack ping, advancing the lethal-by-T9 pressure line. Keep Shock for their blocker."
    state = _state([], [CLAW])
    assert "Hired Claw has no haste" in decision_claim_notes(text, state, cast_ids=[395])
    corrected = corrected_decision_reasoning(text, state, cast_ids=[395])
    assert "third hasty" not in corrected
    assert corrected.startswith("Keep Shock for their blocker.")
    assert "Hired Claw has no haste" in corrected


@pytest.mark.parametrize("request_type", ["CastingTimeOptions", "SelectTargets"])
def test_pending_burst_is_not_declared_nonlethal_by_precast_board_math(monkeypatch, request_type):
    text = "Opponent is at 2 life, so kicked Burst Lightning to their face wins the game outright."
    planner = _planner(monkeypatch, text, _facts(opp_life=2))
    planner._last_decision_reasoning = text
    planner._last_decision_trace = {}
    planner._correct_false_lethal_claim({"turn": {}}, SimpleNamespace(request_type=request_type))
    assert planner._last_decision_reasoning == text
    assert "false_lethal_claim" not in planner._last_decision_trace


def test_unresolved_stack_is_not_proof_of_nonlethal(monkeypatch):
    text = "The pending Shock ends the game now."
    planner = _planner(monkeypatch, text, _facts(opp_life=2))
    planner._last_decision_reasoning = text
    planner._last_decision_trace = {}
    planner._correct_false_lethal_claim({"stack": [{"name": "Shock"}]})
    assert planner._last_decision_reasoning == text


BLOODLETTER = dict(
    instance_id=340,
    name="Bloodletter of Aclazotz",
    controller_seat_id=1,
    owner_seat_id=1,
    type_line="Creature — Vampire Demon",
    power=2,
    toughness=4,
    oracle_text="Flying\nIf an opponent would lose life during your turn, they lose twice that much life instead. (Damage causes loss of life.)",
)
FALSE_MULTIPLIER = "Bloodletter doubles losses on my turn, so Strike to the face deals 6 (7→1), making all future burn lethal-range; killing the tapped Slasher advances less than closing."


def bloodletter_state(**changes):
    state = dict(
        local_seat_id=2, turn=dict(active_player=2), battlefield=[deepcopy(BLOODLETTER)], hand=[], stack=[]
    )
    state.update(changes)
    return state


def test_opposing_bloodletter_is_grounded_before_the_choice_and_false_claim_is_removed():
    state = bloodletter_state()
    assert "controlled by the opponent" in rules_block(state)
    notes = decision_claim_notes(FALSE_MULTIPLIER, state)
    assert len(notes) == 1 and "does not double" in notes[0]
    corrected = corrected_decision_reasoning(FALSE_MULTIPLIER, state)
    assert "deals 6" not in corrected and "does not double" in corrected


@pytest.mark.parametrize(
    "controller, owner, active, should_flag",
    [
        (1, 1, 2, True),
        (1, 2, 2, True),
        (2, 1, 2, False),
        (2, 2, 1, True),
        (None, 1, 2, False),
        (1, 1, None, False),
    ],
)
def test_multiplier_uses_current_control_and_turn(controller, owner, active, should_flag):
    state = bloodletter_state(turn=dict(active_player=active))
    state["battlefield"][0].update(controller_seat_id=controller, owner_seat_id=owner)
    assert bool(damage_attribution_notes(FALSE_MULTIPLIER, state)) == should_flag


@pytest.mark.parametrize(
    "text",
    [
        "Bloodletter does not double our damage on my turn.",
        "Bloodletter doubles their damage on their turn.",
        "If we steal Bloodletter, it would double our burn.",
    ],
)
def test_multiplier_does_not_correct_true_or_conditional_claims(text):
    assert damage_attribution_notes(text, bloodletter_state()) == []


@pytest.mark.parametrize("hidden", ["is_phased_out", "face_down"])
def test_inactive_or_hidden_multiplier_is_not_used_as_evidence(hidden):
    state = bloodletter_state()
    state["battlefield"][0][hidden] = True
    assert not damage_attribution_notes(FALSE_MULTIPLIER, state)


def target_decision():
    return PendingDecision(
        request_id=(271, 366),
        request_type="SelectTargets",
        options=(
            DecisionOption("tgt:1", "Opponent (7 life)", meta={"player_seat": 1}),
            DecisionOption("tgt:337", "Unstoppable Slasher", meta={"instanceId": 337}),
        ),
    )


@pytest.mark.parametrize("high", [False, True])
def test_false_multiplier_cannot_survive_second_look_even_with_a_changed_target(high):
    planner = ActionPlanner(Mock())
    planner._last_decision_trace = {}
    decision, state = target_decision(), bloodletter_state()

    def concerns(chosen, data):
        return planner._second_look_concerns(decision, state, chosen, data["reasoning"])

    with pytest.raises(SecondLookError):
        planner._second_look(
            decision,
            state,
            {"reasoning": FALSE_MULTIPLIER},
            ["tgt:1"],
            concerns,
            ask=lambda *args, **kwargs: "response",
            parse=lambda raw: ({"reasoning": FALSE_MULTIPLIER}, ["tgt:337"]),
            message="Choose a target.",
            answered_high=high,
        )
    assert planner._last_decision_trace["second_look"]["final"] == "fallback"


def test_corrected_damage_evaluation_can_keep_the_face_target():
    planner = ActionPlanner(Mock())
    planner._last_decision_trace = {}
    decision, state = target_decision(), bloodletter_state()
    corrected = "Strike deals ordinary 3 damage to the opponent, leaving 4; choose face for reach."
    result = planner._second_look(
        decision,
        state,
        {"reasoning": FALSE_MULTIPLIER},
        ["tgt:1"],
        lambda chosen, data: planner._second_look_concerns(decision, state, chosen, data["reasoning"]),
        ask=lambda *args, **kwargs: "response",
        parse=lambda raw: ({"reasoning": corrected}, ["tgt:1"]),
        message="Choose a target.",
        answered_high=False,
    )
    assert result == ({"reasoning": corrected}, ["tgt:1"])


def test_exemplar_light_is_not_the_light_prefix_in_lightning():
    from arenamcp.play_safety import claimed_kills, strip_kill_claims

    exemplar = dict(name="Exemplar of Light", power=7, toughness=7)
    soldier = dict(name="Soldier", power=1, toughness=1)
    text = (
        "All-in race: killing the Soldier with Burst Lightning lets Emberheart and Hired Claw both connect."
    )
    assert claimed_kills(text, [exemplar, soldier]) == [soldier]
    assert strip_kill_claims(text, [exemplar]) == text
    assert claimed_kills("Burst Lightning kills Exemplar of Light.", [exemplar]) == [exemplar]


def test_selected_cast_is_subject_of_unnamed_hasty_description():
    text = "Curve out with the cheapest hasty attacker on an empty board; its Lizard ping engine starts chipping immediately."
    state = _state([], [CLAW])
    assert decision_claim_notes(text, state, cast_ids=[395]) == ["Hired Claw has no haste"]
    assert "hasty attacker" not in corrected_decision_reasoning(text, state, cast_ids=[395])
    assert decision_claim_notes(text, state) == []  # no selected subject: do not guess
