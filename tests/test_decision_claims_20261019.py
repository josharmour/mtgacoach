"""Claims in a decision's reasoning that the board contradicts (ledger #84, 2026-10-19).

- 11:23:15 / 11:23:50 (match b2a6f83a): "Casting Giant Cindermaw gives haste trample
  pressure alongside Slickshot (6 damage this combat vs 7 life)", "Casting the second
  Giant Cindermaw gives two hasty trampling attackers". Giant Cindermaw: "Trample /
  Players can't gain life." No haste.
- 11:40:47 (match a358cce5 T9): "Casting Fleeting Effigy deals 2 face damage, grows
  Slickshot via the noncreature-spell trigger". Effigy is a creature spell.
- 11:44:02 (match a358cce5): "The last mana funds Hired Claw, adding a second attacker
  whose Lizard ping chips the opponent" in our main phase: a Claw cast now is
  summoning sick and makes no attack trigger this turn.

The plan's keyword check (#23) reads decision reasoning too; the planner logs the
contradiction and the stored reasoning carries the facts. No model call.
"""

from __future__ import annotations

import logging
from copy import deepcopy

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.game_plan import decision_claim_notes

US, THEM = 1, 2

CARDS = {
    "Giant Cindermaw": {"type_line": "Creature — Dinosaur Beast", "mana_cost": "{2}{R}", "power": 4,
                        "toughness": 3, "oracle_text": "Trample\nPlayers can't gain life.",
                        "card_types": ["CardType_Creature"]},
    "Fleeting Effigy": {"type_line": "Creature — Elemental", "mana_cost": "{R}", "power": 2, "toughness": 2,
                        "oracle_text": "Haste\nAt the beginning of your end step, return this creature to its "
                        "owner's hand.\n{o2oR}: This creature gets +2/+0 until end of turn.",
                        "card_types": ["CardType_Creature"]},
    "Hired Claw": {"type_line": "Creature — Lizard Mercenary", "mana_cost": "{R}", "power": 1, "toughness": 2,
                   "oracle_text": "Whenever you attack with one or more Lizards, this creature deals 1 damage to "
                   "target opponent.\n{o1oR}: Put a +1/+1 counter on this creature. Activate only if an "
                   "opponent lost life this turn and only once each turn.", "card_types": ["CardType_Creature"]},
    "Slickshot Show-Off": {"type_line": "Creature — Bird Wizard", "mana_cost": "{1}{R}", "power": 1,
                           "toughness": 2, "oracle_text": "Flying, haste\nWhenever you cast a noncreature "
                           "spell, this creature gets +2/+0 until end of turn.",
                           "card_types": ["CardType_Creature"]},
    "Shock": {"type_line": "Instant", "mana_cost": "{R}", "oracle_text": "Shock deals 2 damage to any target.",
              "card_types": ["CardType_Instant"]},
    "Mountain": {"type_line": "Basic Land — Mountain", "oracle_text": "({T}: Add {R}.)",
                 "card_types": ["CardType_Land"]},
    "Ajani's Pridemate": {"type_line": "Creature — Cat Soldier", "mana_cost": "{1}{W}", "power": 2,
                          "toughness": 2, "oracle_text": "Whenever you gain life, put a +1/+1 counter on this "
                          "creature.", "card_types": ["CardType_Creature"]},
}  # fmt: skip


def card(iid: int, name: str, seat: int = US, **extra) -> dict:
    return {"instance_id": iid, "name": name, "owner_seat_id": seat, "controller_seat_id": seat,
            **deepcopy(CARDS[name]), **extra}  # fmt: skip


def state(battlefield: list[dict], hand: list[dict], *, turn: int = 9, active: int = US,
          phase: str = "Phase_Main1") -> dict:  # fmt: skip
    lands = [card(500 + k, "Mountain", turn_entered_battlefield=1) for k in range(4)]
    return {
        "local_seat_id": US,
        "turn": {"turn_number": turn, "active_player": active, "priority_player": active, "phase": phase,
                 "step": ""},
        "players": [{"seat_id": US, "is_local": True, "life_total": 14},
                    {"seat_id": THEM, "is_local": False, "life_total": 7}],
        "battlefield": [*lands, *battlefield],
        "hand": hand,
        "graveyard": [],
        "stack": [],
    }  # fmt: skip


SLICKSHOT = card(301, "Slickshot Show-Off", turn_entered_battlefield=6)
PRIDEMATE = card(401, "Ajani's Pridemate", THEM, turn_entered_battlefield=5)


def test_a_cindermaw_called_hasty_is_corrected():
    board = state([SLICKSHOT, PRIDEMATE], [card(220, "Giant Cindermaw"), card(221, "Giant Cindermaw")])
    first = (
        "Casting Giant Cindermaw gives haste trample pressure alongside Slickshot (6 damage this combat vs "
        "7 life) and locks out any lifegain."
    )
    assert "Giant Cindermaw has no haste" in decision_claim_notes(first, board, cast_ids=[220])
    second = (
        "Casting the second Giant Cindermaw gives two hasty trampling attackers totaling 8 power against "
        "their 6 life, closing the race before their token engine scales."
    )
    assert "Giant Cindermaw has no haste" in decision_claim_notes(second, board, cast_ids=[221])


def test_an_effigy_cast_triggers_no_noncreature_ability():
    board = state([SLICKSHOT, PRIDEMATE], [card(230, "Fleeting Effigy"), card(231, "Shock")])
    text = (
        "Casting Fleeting Effigy deals 2 face damage, grows Slickshot via the noncreature-spell trigger, "
        "and leaves a hasty body while returning to hand later."
    )
    notes = decision_claim_notes(text, board, cast_ids=[230])
    assert notes == [
        "Fleeting Effigy is a creature spell: casting it triggers no noncreature-spell ability (prowess)"
    ]
    # Shock is the noncreature spell the sentence sequences after it: no claim about Effigy.
    later = "Cast Fleeting Effigy, then Shock to trigger Slickshot's noncreature-spell pump."
    assert decision_claim_notes(later, board, cast_ids=[230]) == []


def test_a_claw_cast_now_makes_no_attack_trigger_this_turn():
    board = state([SLICKSHOT, card(240, "Fleeting Effigy", turn_entered_battlefield=9)],
                  [card(241, "Hired Claw")])  # fmt: skip
    text = (
        "The last mana funds Hired Claw, adding a second attacker whose Lizard ping chips the opponent "
        "toward burn-range lethal."
    )
    assert decision_claim_notes(text, board, cast_ids=[241]) == [
        "Hired Claw is summoning sick: it can't attack or activate its own {T}/{Q} abilities this turn"
    ]
    # A Claw that came down on an earlier turn pings; a later turn's attack is no claim about now.
    old_claw = state([SLICKSHOT, card(242, "Hired Claw", turn_entered_battlefield=7)], [])
    assert decision_claim_notes("Attack with Hired Claw so its ping chips 1.", old_claw) == []
    assert decision_claim_notes("Cast Hired Claw now; it attacks next turn.", board, cast_ids=[241]) == []
    # After combat, or on their turn, attack talk is about later turns.
    main2 = state([SLICKSHOT], [card(241, "Hired Claw")], phase="Phase_Main2")
    assert decision_claim_notes(text, main2, cast_ids=[241]) == []


def test_true_claims_pass():
    board = state([SLICKSHOT, PRIDEMATE], [card(230, "Fleeting Effigy"), card(231, "Shock")])
    for text, cast in (
        ("Casting Fleeting Effigy gives a hasty 2/2 attacker this turn.", [230]),
        ("Shock the Pridemate; the noncreature spell pumps Slickshot for the attack.", [231]),
        ("Slickshot has flying and haste, so it swings now.", []),
    ):
        assert decision_claim_notes(text, board, cast_ids=cast) == [], text


def test_the_planner_logs_and_annotates_without_changing_the_pick(caplog):
    planner = ActionPlanner.__new__(ActionPlanner)
    planner._last_decision_reasoning = (
        "Casting Giant Cindermaw gives haste trample pressure alongside Slickshot (6 damage this combat "
        "vs 7 life) and locks out any lifegain"
    )
    planner._last_decision_trace = {}
    board = state([SLICKSHOT, PRIDEMATE], [card(220, "Giant Cindermaw")])
    decision = PendingDecision(
        request_id=(1, 2),
        request_type="ActionsAvailable",
        options=(
            DecisionOption("idx:0", "Cast Giant Cindermaw", True, {"actionType": "ActionType_Cast",
                                                                   "instanceId": 220, "grpId": 94052}),
            DecisionOption("pass", "Pass"),
        ),
    )  # fmt: skip
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        planner._note_false_claims(board, decision, ["idx:0"])
    assert "typed-decision reasoning contradicts the board (Giant Cindermaw has no haste)" in caplog.text
    assert planner._last_decision_reasoning.endswith(
        "locks out any lifegain. [checked: Giant Cindermaw has no haste]"
    )
    assert planner._last_decision_trace["false_claims"] == ["Giant Cindermaw has no haste"]
    # Nothing to say: nothing changes.
    planner._last_decision_reasoning = "Cast Giant Cindermaw to add a big trampler for next turn."
    planner._note_false_claims(board, decision, ["idx:0"])
    assert planner._last_decision_reasoning == "Cast Giant Cindermaw to add a big trampler for next turn."
