"""London-mulligan bottoming: ties broken by what a card does, never by its place in the hand.

2026-10-19, Mono-Red Burn (standalone.log):

* 09:30 (match 92c09c7a): keeping 6 of Slickshot Show-Off, Mountain, Hired
  Claw, Boltwave, Fleeting Effigy, Mountain, Shock. Keeping Shock, Boltwave
  or Effigy all scored 19.0; the tie key (list position) bottomed Shock, the
  keep reason said "(bottom Shock)", and the bottom step's model followed it
  "consistent with my earlier keep decision".
* 08:47 (match 7efad315): Mountain, Burst Lightning x2, Boltwave, Slickshot,
  Mountain, Shock. The guard said bottom Shock; the model bottomed a Burst
  Lightning (which does all Shock does, and kicks) and the bottom guard,
  within its 1.0 score tolerance, let it stand.
"""

from __future__ import annotations

import json
import logging

import pytest

from arenamcp import mulligan_policy as mp
from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision

SHOCK = ("Shock", "Instant", "{R}", "common", "Shock deals 2 damage to any target.")
BURST = (
    "Burst Lightning",
    "Instant",
    "{R}",
    "common",
    "Kicker {4} (You may pay an additional {4} as you cast this spell.)\n"
    "Burst Lightning deals 2 damage to any target. If this spell was kicked, it deals 4 damage instead.",
)
BOLTWAVE = ("Boltwave", "Sorcery", "{R}", "uncommon", "Boltwave deals 3 damage to each opponent.")
SLICKSHOT = (
    "Slickshot Show-Off",
    "Creature — Bird Wizard",
    "{1}{R}",
    "rare",
    "Flying, haste\nWhenever you cast a noncreature spell, this creature gets +2/+0 until end of turn.",
)
CLAW = ("Hired Claw", "Creature — Lizard Mercenary", "{R}", "rare", "Whenever you attack with one or more Lizards…")
EFFIGY = (
    "Fleeting Effigy",
    "Creature — Elemental",
    "{R}",
    "uncommon",
    "Haste\nAt the beginning of your end step, return this creature to its owner's hand.",
)
MOUNTAIN = ("Mountain", "Basic Land — Mountain", "", "common", "({T}: Add {R}.)")

HAND_92C09C7A = [SLICKSHOT, MOUNTAIN, CLAW, BOLTWAVE, EFFIGY, MOUNTAIN, SHOCK]
HAND_7EFAD315 = [MOUNTAIN, BURST, BURST, BOLTWAVE, SLICKSHOT, MOUNTAIN, SHOCK]


def _state(cards, mulligans=1):
    hand = [
        {
            "instance_id": 339 + index,
            "grp_id": 9000 + index,
            "name": name,
            "type_line": type_line,
            "mana_cost": cost,
            "rarity": rarity,
            "oracle_text": text,
            "owner_seat_id": 2,
            "controller_seat_id": 2,
        }
        for index, (name, type_line, cost, rarity, text) in enumerate(cards)
    ]
    return {
        "match_id": "92c09c7a",
        "local_seat_id": 2,
        "pending_decision": "Mulligan",
        "decision_context": {"type": "mulligan"},
        "players": [
            {"seat_id": 2, "is_local": True, "life_total": 20, "mulligan_count": mulligans},
            {"seat_id": 1, "life_total": 20},
        ],
        "turn": {"turn_number": 0, "active_player": 1},
        "hand": hand,
        "battlefield": [],
    }


def _ids(state):
    return [card["instance_id"] for card in state["hand"]]


def _names(state, ids):
    return mp.card_names(state, ids)


@pytest.mark.parametrize("cards", [HAND_92C09C7A, HAND_7EFAD315], ids=["92c09c7a", "7efad315"])
@pytest.mark.parametrize("order", ["logged", "reversed"])
def test_the_tie_bottoms_the_face_only_burn_in_any_order(cards, order):
    state = _state(cards if order == "logged" else list(reversed(cards)))
    assert _names(state, mp.bottom_choice(state, _ids(state), 1)) == "Boltwave"


def test_roles_rank_instant_removal_over_creatures_over_face_burn():
    role = {card["name"]: mp.hand_card(card).role for card in _state(HAND_7EFAD315 + [EFFIGY])["hand"]}
    assert role["Burst Lightning"] > role["Shock"] > role["Fleeting Effigy"] > role["Boltwave"]
    assert role["Mountain"] == 0.0


def test_the_keep_reason_names_no_bottom_card():
    choice, reason = mp.mulligan_verdict(mp.situation(_state(HAND_92C09C7A)))
    assert choice == "keep"
    assert "bottom" not in reason.lower()


def _bottom_decision(state):
    return PendingDecision(
        request_id=(6, 21),
        request_type="Group",
        options=tuple(
            DecisionOption(f"grp:{c['instance_id']}", f"Bottom {c['name']}", meta={"instance_id": c["instance_id"]})
            for c in state["hand"]
        ),
        min_select=1,
        max_select=1,
        source_label="LondonMulligan",
    )


class _Backend:
    def __init__(self, answer):
        self.answer = answer
        self.prompts = []

    def complete(self, system, user, *args, **kwargs):
        self.prompts.append(user)
        return self.answer


def _model_bottoms(state, name, caplog):
    target = next(f"grp:{c['instance_id']}" for c in state["hand"] if c["name"] == name)
    backend = _Backend(json.dumps({"option_ids": [target], "reasoning": f"bottom the redundant {name}"}))
    planner = ActionPlanner(backend, timeout=5, land_drop_first=False)
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        chosen = planner.plan_decision_options(_bottom_decision(state), state)
    return [_names(state, [int(c[4:])]) for c in chosen], backend


def test_bottoming_a_burst_lightning_over_shock_is_replaced(caplog):
    state = _state(HAND_7EFAD315)
    chosen, backend = _model_bottoms(state, "Burst Lightning", caplog)
    assert chosen == ["Boltwave"]
    assert "Burst Lightning is strictly better than the kept Shock" in caplog.text
    assert "cheap instant-speed removal beats burn that only hits players" in backend.prompts[0]


def test_the_bottom_prompt_suggests_the_face_only_burn(caplog):
    _chosen, backend = _model_bottoms(_state(HAND_92C09C7A), "Boltwave", caplog)
    assert "Land/curve suggestion: bottom Boltwave." in backend.prompts[0]
    assert "Mulligan bottom guard" not in caplog.text


@pytest.mark.parametrize("name", ["Fleeting Effigy", "Shock"])
def test_trading_one_kind_of_card_for_another_stays_the_models_call(name, caplog):
    # Bottoming the Effigy or Shock keeps Boltwave: a creature or instant
    # removal against face burn is a judgement call within the tolerance,
    # not a strictly worse choice (the tie-break and prompt steer it instead).
    chosen, _backend = _model_bottoms(_state(HAND_92C09C7A), name, caplog)
    assert chosen == [name]
    assert "Mulligan bottom guard" not in caplog.text


def test_dominance_compares_spells_only():
    state = _state(HAND_7EFAD315)
    ids = _ids(state)
    by_name = {}
    for card in state["hand"]:
        by_name.setdefault(card["name"], card["instance_id"])
    assert mp.dominated_bottom(state, ids, [by_name["Burst Lightning"]]) == ("Burst Lightning", "Shock")
    assert mp.dominated_bottom(state, ids, [by_name["Boltwave"]]) is None
