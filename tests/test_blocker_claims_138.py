"""#138 retained e917434f T5/T7 wording; no tactical-optimality assertion.

standalone.log 2026-10-19 13:19:50.495 / 13:20:24.739 / 13:20:26.598 PDT.
Player-session-f404e237ec.log states 110/171 (lines 301495/302086):
local seat2 Slickshot290; opposing Scapegoat285 and Pyromancer313.
Scapegoat had passed suspected to opposing Slickshot295 at T5; its lack of
flying/reach, not a guessed suspected state, establishes the mismatch.
"""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.game_plan import _claim_corrections, corrected_decision_reasoning, decision_claim_notes

SLICKSHOT = dict(
    instance_id=290,
    name="Slickshot Show-Off",
    controller_seat_id=2,
    owner_seat_id=2,
    type_line="Creature — Bird Wizard",
    power=1,
    toughness=2,
    oracle_text="Flying, haste\nWhenever you cast a noncreature spell, this creature gets +2/+0 until end of turn.",
)
PYROMANCER = dict(
    instance_id=313,
    name="Viashino Pyromancer",
    controller_seat_id=1,
    owner_seat_id=1,
    type_line="Creature — Lizard Wizard",
    power=2,
    toughness=1,
    oracle_text="When this creature enters, it deals 2 damage to target player or planeswalker.",
)
SCAPEGOAT = dict(
    instance_id=285,
    name="Frantic Scapegoat",
    controller_seat_id=1,
    owner_seat_id=1,
    type_line="Creature — Goat",
    power=1,
    toughness=1,
    is_tapped=True,
    oracle_text="Haste\nWhen this creature enters, suspect it. (It has menace and can't block.)\nWhenever one or more other creatures you control enter, if this creature is suspected, you may suspect one of the other creatures. If you do, this creature is no longer suspected.",
)
CAST = (
    "Kill the Viashino Pyromancer blocker so Slickshot attacks freely, and the cast trigger grows it to 3/2."
)
TARGET = "Kills the 2/1 Viashino Pyromancer blocker so Slickshot attacks freely, and the cast trigger grows it to 3/2."
GOAT_CAST = "Cast Burst Lightning on Frantic Scapegoat: it removes their only future blocker, grows Slickshot to a 3/2 hasty flier for an unblocked 3-damage attack this turn, and keeps the kicker option for later reach."
GOAT_TARGET = "Kills their only future blocker (Frantic Scapegoat) so my grown 3/2 hasty flying Slickshot attacks unblocked this turn."


def state(blocker=PYROMANCER):
    creatures = [deepcopy(SLICKSHOT), deepcopy(blocker)]
    if blocker is SCAPEGOAT:
        creatures.append(
            dict(
                SLICKSHOT,
                instance_id=295,
                owner_seat_id=1,
                controller_seat_id=1,
                is_tapped=True,
                is_suspected=True,
            )
        )
    return dict(
        local_seat_id=2,
        turn=dict(turn_number=5 if blocker is SCAPEGOAT else 7, active_player=2),
        battlefield=creatures,
        hand=[],
        stack=[],
    )


@pytest.mark.parametrize(
    "reason,blocker",
    [(CAST, PYROMANCER), (TARGET, PYROMANCER), (GOAT_TARGET, SCAPEGOAT), (GOAT_CAST, SCAPEGOAT)],
)
def test_retained_positive_blocker_claim_is_corrected(reason, blocker):
    notes = decision_claim_notes(reason, state(blocker))
    assert any(f"{blocker['name']} cannot block Slickshot Show-Off" in note for note in notes)
    corrected = corrected_decision_reasoning(reason, state(blocker))
    assert reason not in corrected
    assert "no flying or reach" in corrected


def test_correction_reaches_plan_text_and_keeps_independent_fact():
    snapshot = state()
    text = CAST + " Save another burn spell for reach."
    corrected, notes = _claim_corrections(
        text,
        ours=snapshot["battlefield"][:1],
        theirs=snapshot["battlefield"][1:],
        hand=[],
        battlefield=snapshot["battlefield"],
        our_turn=True,
    )
    assert notes
    assert CAST not in corrected
    assert "Save another burn spell for reach." in corrected


def test_selected_removal_target_stands_and_rejected_reason_is_preserved():
    planner = ActionPlanner(Mock())
    planner._last_decision_reasoning = TARGET
    planner._last_decision_trace = {}
    decision = PendingDecision((1, 2), "SelectTargets", (DecisionOption("tgt:313", "Viashino Pyromancer"),))
    chosen = ["tgt:313"]
    planner._note_false_claims(state(), decision, chosen)
    assert chosen == ["tgt:313"]
    assert planner._last_decision_trace["uncorrected_reasoning"] == TARGET
    assert "no flying or reach" in planner._last_decision_reasoning


@pytest.mark.parametrize(
    "text",
    [
        "Kill Viashino Pyromancer for its damage; Slickshot attacks freely.",
        "Viashino Pyromancer cannot block Slickshot.",
        "Viashino Pyromancer is not a blocker for Slickshot.",
        "If given reach, Viashino Pyromancer is a blocker for Slickshot.",
        "Viashino Pyromancer is their blocker against Hired Claw; Slickshot flies over it.",
        "Kill Viashino Pyromancer so Slickshot attacks freely.",
    ],
)
def test_negative_and_conditional_controls(text):
    assert decision_claim_notes(text, state()) == []


@pytest.mark.parametrize(
    "field,value",
    [("keywords", ["reach"]), ("keywords", ["flying"]), ("granted_abilities", "Reach"), ("oracle_text", "")],
)
def test_legal_or_unknown_blocker_is_not_corrected(field, value):
    snapshot = state()
    snapshot["battlefield"][1][field] = value
    assert decision_claim_notes(CAST, snapshot) == []


def test_possible_external_keyword_grant_stays_unknown():
    snapshot = state()
    snapshot["battlefield"].append(
        dict(
            name="Reach anthem",
            type_line="Enchantment",
            oracle_text="Creatures you control have reach.",
            controller_seat_id=1,
        )
    )
    assert decision_claim_notes(CAST, snapshot) == []


def test_ground_attacker_and_ambiguous_duplicate_blocker_are_not_corrected():
    snapshot = state()
    snapshot["battlefield"][0]["oracle_text"] = "Haste"
    assert decision_claim_notes(CAST, snapshot) == []
    snapshot = state()
    snapshot["battlefield"].append(dict(PYROMANCER, instance_id=400, keywords=["reach"]))
    assert decision_claim_notes(CAST, snapshot) == []


@pytest.mark.parametrize(
    "text",
    [
        "Viashino Pyromancer is a blocker for Slickshot.",
        "Viashino Pyromancer is their only blocker against Slickshot.",
    ],
)
def test_direct_positive_blocker_description(text):
    assert decision_claim_notes(text, state())


@pytest.mark.parametrize("side", [0, 1])
def test_unknown_duplicate_creature_identity_abstains(side):
    snapshot = state()
    snapshot["battlefield"].append(dict(snapshot["battlefield"][side], instance_id=500, oracle_text=""))
    assert decision_claim_notes(CAST, snapshot) == []
