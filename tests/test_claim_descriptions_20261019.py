"""Card descriptions are not this-turn claims (ledger #108, #117; standalone.log 2026-10-19).

The decision-reasoning claim check (#84, ``game_plan.decision_claim_notes``) flagged the model
describing a card's own abilities:

- #108, 12:43:26: "Emberheart Challenger is the planned on-curve hasty attacker with Prowess/Valiant
  upside" -> "casting it triggers no noncreature-spell ability"; 12:50:07 (match 2c67eed8 T10):
  "casting the hasty Emberheart Challenger adds a second attacker and prowess engine"; 13:00:04:
  "Slickshot is my only card and a hasty prowess engine". Nothing claims the cast triggers anything.
- #117, 12:52:10 (2c67eed8 T18): "the free-value Hired Claw — its attack ping chips for 1" on a Claw
  cast that turn -> "summoning sick"; also 13:13:51 "develops a recurring ping attacker" and 13:15:35
  "starts its ping/pump engine".

The true claims stay flagged (tests/test_decision_claims_20261019.py keeps the original ones).
"""

from __future__ import annotations

from copy import deepcopy

import pytest
from tests import race_boards_20261019 as rb

from arenamcp.game_plan import decision_claim_notes

US, THEM = 1, 2
NO_TRIGGER = "{} is a creature spell: casting it triggers no noncreature-spell ability (prowess)"
SICK = "{} is summoning sick: it can't attack or activate its own {{T}}/{{Q}} abilities this turn"


def _card(iid: int, name: str, seat: int = US, **extra) -> dict:
    base = deepcopy(rb.CARDS[name])
    return {
        "instance_id": iid,
        "name": name,
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        **base,
        **extra,
    }


def _state(battlefield: list[dict], hand: list[dict], *, phase: str = "Phase_Main1") -> dict:
    lands = [_card(500 + k, "Mountain", turn_entered_battlefield=1) for k in range(5)]
    return {
        "local_seat_id": US,
        "turn": {"turn_number": 10, "active_player": US, "priority_player": US, "phase": phase, "step": ""},
        "players": [
            {"seat_id": US, "is_local": True, "life_total": 18},
            {"seat_id": THEM, "is_local": False, "life_total": 11},
        ],
        "battlefield": [*lands, *battlefield],
        "hand": hand,
        "graveyard": [],
        "stack": [],
    }


EMBERHEART = _card(338, "Emberheart Challenger")
SLICKSHOT = _card(355, "Slickshot Show-Off")
CLAW = _card(395, "Hired Claw")
VALKYRIE = _card(335, "Youthful Valkyrie", THEM, turn_entered_battlefield=9)


# --- #108: prowess named, not triggered -----------------------------------------------------------


@pytest.mark.parametrize(
    "text, cast",
    [
        (
            "Emberheart Challenger is the planned on-curve hasty attacker with Prowess/Valiant upside, better "
            "than a second Slickshot.",
            EMBERHEART,
        ),
        (
            "With 2 mana left after Cindermaw, casting the hasty Emberheart Challenger adds a second attacker "
            "and prowess engine, advancing the aggressive clock.",
            EMBERHEART,
        ),
        (
            "Slickshot is my only card and a hasty prowess engine; deploying it now starts the pressure clock "
            "against Ajani's lifegain.",
            SLICKSHOT,
        ),
        (
            "Cast Emberheart Challenger now; its prowess grows it with every burn spell after this.",
            EMBERHEART,
        ),
    ],
)
def test_naming_a_creatures_own_prowess_is_no_cast_trigger_claim(text, cast):
    board = _state([VALKYRIE], [cast, _card(700, "Mountain")])
    assert decision_claim_notes(text, board, cast_ids=[cast["instance_id"]]) == []


@pytest.mark.parametrize(
    "text",
    [
        "Casting Emberheart Challenger triggers Slickshot's noncreature-spell pump for the attack.",
        "Casting Emberheart Challenger grows Slickshot via prowess before combat.",
    ],
)
def test_a_creature_cast_said_to_trigger_prowess_is_still_flagged(text):
    board = _state([_card(301, "Slickshot Show-Off", turn_entered_battlefield=6), VALKYRIE], [EMBERHEART])
    assert decision_claim_notes(text, board, cast_ids=[338]) == [NO_TRIGGER.format("Emberheart Challenger")]


# --- #117: the card's ping described, not claimed for this turn --------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "With no survivable line against their lethal flyers, the only play is the free-value Hired Claw — its "
        "attack ping chips for 1 and it costs just {R}, strictly better than passing.",
        "With only 1 mana this turn, deploying Hired Claw develops a recurring ping attacker; Boltwave keeps "
        "for next turn alongside the second land.",
        "With Bagginses killed and 2 mana left, deploying Hired Claw develops a body and starts its ping/pump "
        "engine toward the burn-out plan.",
        "Deploy Hired Claw: Hired Claw's ping adds reach to the burn plan.",
    ],
)
def test_describing_a_fresh_claws_ping_is_no_attack_claim(text):
    assert decision_claim_notes(text, _state([], [CLAW]), cast_ids=[395]) == []


@pytest.mark.parametrize(
    "text",
    [
        "Cast Hired Claw — its attack ping chips them for 1 this turn.",
        "Cast Hired Claw and attack with it so the Lizard ping chips 1.",
        "The last mana funds Hired Claw, adding a second attacker whose Lizard ping chips the opponent toward "
        "burn-range lethal.",
    ],
)
def test_a_fresh_claw_planned_to_attack_or_ping_this_turn_is_still_flagged(text):
    assert decision_claim_notes(text, _state([], [CLAW]), cast_ids=[395]) == [SICK.format("Hired Claw")]


def test_a_creature_already_sick_on_the_board_planned_to_attack_is_still_flagged():
    fresh = _card(344, "Emberheart Challenger", turn_entered_battlefield=10, keywords=[], oracle_text="")
    board = _state([fresh, VALKYRIE], [])
    assert decision_claim_notes("Attack with Emberheart Challenger to force the block.", board) == [
        SICK.format("Emberheart Challenger")
    ]


@pytest.mark.parametrize("attack_owner", ["my", "my own", "our", "their"])
def test_effect_on_another_attack_is_not_sick_creature_attacking(attack_owner):
    fresh = _card(344, "Hired Claw", turn_entered_battlefield=10, keywords=[], oracle_text="")
    fresh["name"] = "Kogla, the Titan Ape"
    board = _state([fresh], [])
    text = (
        "Lethal is already on the board; Heroic Intervention answers nothing and "
        f"Kogla's bounce only weakens {attack_owner} attack, so move to combat."
    )
    assert decision_claim_notes(text, board) == []
    assert decision_claim_notes("Kogla attacks this turn.", board) == [SICK.format(fresh["name"])]


@pytest.mark.parametrize(
    "reason",
    [
        "Tap the summoning-sick Camera Launcher to crew Aetherhive, preserving Lagac as an attacker.",
        "Camera Launcher crews Aetherhive this turn.",
        "Camera Launcher: tap it to crew Aetherhive.",
    ],
)
def test_sick_creature_can_pay_crew_cost(reason):
    fresh = _card(344, "Hired Claw", turn_entered_battlefield=10, keywords=[], oracle_text="")
    fresh["name"] = "Camera Launcher"
    state = _state([fresh], [])
    assert decision_claim_notes(reason, state) == []
    assert decision_claim_notes("Camera Launcher attacks this turn.", state) == [
        SICK.format("Camera Launcher")
    ]
