"""The #56 face-to-creature redirect defers to a death trigger the line search can't see (#126).

2026-10-19 13:14:12-16 (turn 3; Player.log selectTargetsReq sourceId 290,
gameStateId 51): Boltwave was on the stack and Burst Lightning was cast in
response, unkicked, "goes face ... killing the 1/1 Inspector only feeds its
death trigger and gives a Clue". The target step's model picked the opponent
(tgt:1). The #56 redirect ("The searched line aims Burst Lightning at Callous
Inspector") moved it to Callous Inspector #284 1/1 ("When this creature dies,
it deals 1 damage to you. Create a Clue token."). The line search models no
death triggers or Clues, so its preference for that kill is blind to what the
kill gives back.
"""

from __future__ import annotations

import json
import logging
import time

import pytest

from arenamcp import action_planner as planner_module
from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision, TargetSlot
from arenamcp.play_safety import death_trigger_benefit

ME, THEM = 2, 1
INSPECTOR_ID, CLAW_ID, BURST_ID, BOLTWAVE_ID = 284, 280, 290, 289
INSPECTOR_TEXT = (
    "Menace (This creature can't be blocked except by two or more creatures.)\n"
    "When this creature dies, it deals 1 damage to you. Create a Clue token. (It's an artifact with "
    '"{2}, Sacrifice this token: Draw a card.")'
)
CLAW_TEXT = (
    "Whenever you attack with one or more Lizards, this creature deals 1 damage to target opponent.\n"
    "{1}{R}: Put a +1/+1 counter on this creature. Activate only if an opponent lost life this turn and "
    "only once each turn."
)
BURST_TEXT = (
    "Kicker {4} (You may pay an additional {4} as you cast this spell.)\n"
    "Burst Lightning deals 2 damage to any target. If this spell was kicked, it deals 4 damage instead."
)
BOLTWAVE_TEXT = "Boltwave deals 3 damage to each opponent."
SAID_13_14_15 = (
    "Unkicked Burst Lightning goes face for 2 chip damage per the burn-clock plan; killing the 1/1 "
    "Inspector only feeds its death trigger and gives a Clue."
)


class _Backend:
    def __init__(self, reply):
        self.reply = reply

    def available(self):
        return True

    def complete(self, system, user, *args, **kwargs):
        return self.reply


def _card(iid, name, seat, type_line, text="", **extra):
    return {
        "instance_id": iid,
        "name": name,
        "type_line": type_line,
        "oracle_text": text,
        "controller_seat_id": seat,
        "owner_seat_id": seat,
        "is_tapped": False,
        **extra,
    }


def burst_window(inspector_text: str = INSPECTOR_TEXT) -> tuple[dict, PendingDecision]:
    """gsid 51: Burst Lightning #290 above Boltwave, targets the players, Hired Claw and the Inspector."""
    state = {
        "match_id": "2026-10-19-13:13",
        "local_seat_id": ME,
        "turn": {"turn_number": 3, "active_player": ME, "priority_player": ME, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": THEM, "life_total": 20, "is_local": False},
            {"seat_id": ME, "life_total": 20, "is_local": True},
        ],
        "battlefield": [
            _card(279, "Mountain", ME, "Basic Land — Mountain", "({T}: Add {R}.)", is_tapped=True),
            _card(287, "Mountain", ME, "Basic Land — Mountain", "({T}: Add {R}.)", is_tapped=True),
            _card(
                CLAW_ID,
                "Hired Claw",
                ME,
                "Creature — Lizard Mercenary",
                CLAW_TEXT,
                power=1,
                toughness=2,
                card_types=["CardType_Creature"],
            ),
            _card(283, "Swamp", THEM, "Basic Land — Swamp", "({T}: Add {B}.)", is_tapped=True),
            _card(
                INSPECTOR_ID,
                "Callous Inspector",
                THEM,
                "Creature — Human Soldier",
                inspector_text,
                power=1,
                toughness=1,
                card_types=["CardType_Creature"],
                summoning_sickness=True,
                keywords=["menace"],
            ),
        ],
        "hand": [],
        "graveyard": [],
        "exile": [],
        "stack": [
            _card(BOLTWAVE_ID, "Boltwave", ME, "Sorcery", BOLTWAVE_TEXT),
            _card(BURST_ID, "Burst Lightning", ME, "Instant", BURST_TEXT, mana_cost="{R}"),
        ],
        "decision_context": {"type": "target_selection", "source_id": BURST_ID},
        "_bridge_request_payload": {"sourceId": BURST_ID},
    }
    ids = (1, 2, CLAW_ID, INSPECTOR_ID)
    decision = PendingDecision(
        request_id=(51, 90),
        request_type="SelectTargets",
        options=tuple(
            DecisionOption(f"tgt:{iid}", f"Target #{iid}", meta={"instanceId": iid}) for iid in ids
        ),
        min_select=1,
        max_select=1,
        can_cancel=True,
        source_label="Burst Lightning",
        slots=(TargetSlot(1, 1, 1, 0, ids),),
    )
    return state, decision


def planner_after_the_cast(reasoning: str = SAID_13_14_15) -> ActionPlanner:
    reply = json.dumps(
        {"option_ids": ["tgt:1"], "reasoning": reasoning, "target_controllers": {"tgt:1": "opponent"}}
    )
    planner = ActionPlanner(_Backend(reply), timeout=5, land_drop_first=False)
    # The cast step remembered the searched line's target for this Burst Lightning (13:14:10).
    planner._cast_intent = {
        "card": "Burst Lightning",
        "said": "With Boltwave resolving for 3 face damage, the last {R} goes upstairs with Burst Lightning",
        "line_target": "Callous Inspector",
        "line_target_id": INSPECTOR_ID,
        "at": time.monotonic(),
    }
    return planner


@pytest.fixture(autouse=True)
def _not_lethal(monkeypatch):
    monkeypatch.setattr(planner_module, "_lethal_this_turn", lambda state: False)


def test_the_face_pick_stands_against_a_line_blind_to_the_clue(caplog):
    state, decision = burst_window()
    planner = planner_after_the_cast()
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        assert planner.plan_decision_options(decision, state) == ["tgt:1"]
    assert "Not retargeting: the searched line aims Burst Lightning at Callous Inspector" in caplog.text
    assert "create a clue token" in caplog.text and "(the model cited it)" in caplog.text
    assert "Retargeting:" not in caplog.text
    deferred = planner.get_last_decision_trace()["line_over_face_deferred"]
    assert deferred["line"] == f"tgt:{INSPECTOR_ID}" and deferred["model_cited"] is True


def test_the_trigger_alone_defers_even_when_the_model_did_not_cite_it(caplog):
    state, decision = burst_window()
    planner = planner_after_the_cast("Burst Lightning to the face keeps the burn clock on schedule.")
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        assert planner.plan_decision_options(decision, state) == ["tgt:1"]
    assert "Not retargeting" in caplog.text and "(the model cited it)" not in caplog.text


@pytest.mark.parametrize(
    "text",
    [
        "Menace",  # #56 as it was: no death trigger, the line's kill stands
        "Menace\nWhen this creature dies, it deals 1 damage to you.",  # a trigger that only hurts them
    ],
)
def test_without_a_beneficial_death_trigger_the_line_target_still_wins(text, caplog):
    state, decision = burst_window(text)
    planner = planner_after_the_cast()
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        assert planner.plan_decision_options(decision, state) == [f"tgt:{INSPECTOR_ID}"]
    assert "Retargeting: The searched line aims Burst Lightning at Callous Inspector" in caplog.text


@pytest.mark.parametrize(
    "card, benefit",
    [
        ({"name": "Callous Inspector", "oracle_text": INSPECTOR_TEXT}, True),
        (
            {
                "name": "Callous Inspector",
                "oracle_text": "Menace\nWhen Callous Inspector dies, it deals 1 damage to you and you investigate.",
            },
            True,
        ),
        (
            {"name": "Doomed Traveler", "oracle_text": "When this creature dies, create a 1/1 white Spirit."},
            True,
        ),
        ({"name": "Sanctum Seeker", "oracle_text": "Undying (When this creature dies, ...)"}, True),
        (
            {"name": "Spark Elemental", "oracle_text": "When this creature dies, it deals 2 damage to you."},
            False,
        ),
        (
            {
                "name": "Unflinching Hortimancer",
                "oracle_text": "Ward {1}\nWhenever you gain life, put a +1/+1 counter on this creature.",
            },
            False,
        ),
        # An aura's or another creature's death trigger is not this creature's own.
        (
            {
                "name": "Bear",
                "oracle_text": "When enchanted creature dies, return this card to its owner's hand.",
            },
            False,
        ),
        ({"name": "Bear", "oracle_text": "Whenever another creature you control dies, draw a card."}, False),
        ({"name": "Bear", "oracle_text": ""}, False),
    ],
)
def test_death_trigger_benefit_reads_only_its_own_paying_trigger(card, benefit):
    assert bool(death_trigger_benefit(card)) is benefit
