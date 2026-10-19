"""Typed-decision prompts say what a creature's tapped state and damage mean (2026-10-19, Mono-Red Burn).

08:08:10 (match a74bbc90): Lightning Strike was offered two targets named
"Virulent Emissary" with no other facts. It hit #288 — tapped from attacking
the turn before (Player.log gs 153) — "clearing the ground blocker so Hired
Claw can attack"; the untapped #312 blocked Claw (gs 202). 09:18:32 (match
f9e0ec3b T8): Burst Lightning's 2 "kills neither Overseer" with Inspiring
Overseer a 3/2 (gs 147-200). 09:21:14 (T12): Burst Lightning "pumps Slickshot
for this turn's combat" with Slickshot tapped (it entered tapped under
Authority of the Consuls). The board said only "[T]".
"""

from __future__ import annotations

import json

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision, TargetSlot
from arenamcp.gamestate import GameState
from arenamcp.play_safety import creature_state_facts, lethal_damage

LOCAL, OPP = 2, 1
STRIKE = "Lightning Strike deals 3 damage to any target."


def _creature(iid, name, seat, power, toughness, oracle="", **extra):
    return {
        "instance_id": iid,
        "name": name,
        "type_line": "Creature — Phyrexian Cleric",
        "card_types": ["CardType_Creature"],
        "oracle_text": oracle,
        "power": power,
        "toughness": toughness,
        "controller_seat_id": seat,
        "owner_seat_id": seat,
        "is_tapped": False,
        "turn_entered_battlefield": 5,
        **extra,
    }


def _mountain(iid, tapped):
    return {
        "instance_id": iid,
        "name": "Mountain",
        "type_line": "Basic Land — Mountain",
        "oracle_text": "({T}: Add {R}.)",
        "card_types": ["CardType_Land"],
        "controller_seat_id": LOCAL,
        "owner_seat_id": LOCAL,
        "is_tapped": tapped,
    }


def strike_state():
    """Our T10 main phase with Lightning Strike on the stack, as the 08:08 log had it."""
    return {
        "local_seat_id": LOCAL,
        "turn": {"turn_number": 10, "active_player": LOCAL, "priority_player": LOCAL, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": OPP, "life_total": 14, "is_local": False},
            {"seat_id": LOCAL, "life_total": 16, "is_local": True},
        ],
        "battlefield": [
            _creature(288, "Virulent Emissary", OPP, 1, 1, "Deathtouch", is_tapped=True),
            _creature(312, "Virulent Emissary", OPP, 1, 1, "Deathtouch"),
            _creature(282, "Hired Claw", LOCAL, 1, 2, "Whenever you attack with one or more Lizards, ..."),
            _mountain(283, True),
            _mountain(302, True),
            _mountain(225, False),
        ],
        "hand": [],
        "graveyard": [],
        "stack": [
            {
                "instance_id": 223,
                "grp_id": 97423,
                "name": "Lightning Strike",
                "type_line": "Instant",
                "oracle_text": STRIKE,
                "controller_seat_id": LOCAL,
            }
        ],
        "_bridge_request_payload": {"sourceId": 223},
        "decision_context": {
            "type": "target_selection",
            "source_id": 223,
            "source_card": "Lightning Strike",
            "source_oracle_text": STRIKE,
        },
    }


def strike_decision():
    ids = (1, 2, 288, 312, 282)
    return PendingDecision(
        request_id=(180, 181),
        request_type="SelectTargets",
        options=tuple(DecisionOption(f"tgt:{iid}", f"Target #{iid}") for iid in ids),
        min_select=1,
        max_select=1,
        can_cancel=True,
        source_label="Lightning Strike",
        slots=(TargetSlot(1, 1, 1, 0, ids),),
    )


class _Backend:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def complete(self, system, user, *args, **kwargs):
        self.prompts.append(user)
        return self.reply


def _prompt(state, decision) -> str:
    planner = ActionPlanner.__new__(ActionPlanner)
    planner._timeout = 1.0
    planner._backend = _Backend('{"option_ids": ["tgt:312"], "reasoning": "kill the untapped blocker"}')
    planner._llm_decision_options(decision, state)
    return planner._backend.prompts[0]


def _option_facts(prompt: str, option_id: str) -> dict:
    line = next(line for line in prompt.splitlines() if line.startswith(f"- {option_id}:"))
    return json.loads(line[line.index("{") :])


def test_same_name_targets_differ_by_tapped_state_and_say_what_kills_them():
    prompt = _prompt(strike_state(), strike_decision())
    tapped, untapped = _option_facts(prompt, "tgt:288"), _option_facts(prompt, "tgt:312")
    assert tapped == {
        "controller_seat_id": OPP,
        "owner_seat_id": OPP,
        "pt": "1/1",
        "status": "tapped: can't block until their next untap",
        "dies_to_damage": 1,
    }
    assert untapped["status"] == "untapped" and untapped["pt"] == "1/1"
    ours = _option_facts(prompt, "tgt:282")
    assert ours["status"] == "untapped" and ours["dies_to_damage"] == 2
    # Players carry no creature facts.
    assert "status" not in _option_facts(prompt, "tgt:1")


def test_board_block_spells_out_tapped_and_lethal_damage():
    prompt = _prompt(strike_state(), strike_decision())
    tapped_line = next(line for line in prompt.splitlines() if "Virulent Emissary #1 1/1 [T" in line)
    assert tapped_line.endswith("— tapped: can't block until their next untap; dies to 1 damage")
    untapped_line = next(line for line in prompt.splitlines() if "Virulent Emissary #2 1/1" in line)
    assert untapped_line.endswith("— dies to 1 damage")
    claw_line = next(line for line in prompt.splitlines() if line.strip().startswith("Hired Claw 1/2"))
    assert "—" not in claw_line  # our untapped, undamaged creature: no note


def test_our_tapped_creature_is_marked_unable_to_attack():
    state = strike_state()
    state["battlefield"].append(
        _creature(
            423,
            "Slickshot Show-Off",
            LOCAL,
            1,
            2,
            "Flying\nHaste",
            is_tapped=True,
            turn_entered_battlefield=10,
        )
    )
    prompt = _prompt(state, strike_decision())
    line = next(line for line in prompt.splitlines() if "Slickshot Show-Off 1/2" in line)
    assert line.endswith("— tapped: can't attack or block until your next untap")


def test_lethal_damage_counts_counters_marked_damage_and_indestructible():
    # Inspiring Overseer with Giada's +1/+1 counter: GRE reports 3/2 (gs 147).
    overseer = _creature(374, "Inspiring Overseer", OPP, 3, 2, "Flying", counters={"P1P1": 1})
    assert lethal_damage(overseer) == 2
    assert lethal_damage({**overseer, "damage": 1}) == 1
    assert creature_state_facts({**overseer, "damage": 1}, mine=False) == {
        "pt": "3/2",
        "status": "untapped",
        "marked_damage": 1,
        "dies_to_damage": 1,
    }
    god = _creature(500, "Indestructible God", OPP, 5, 6, "Indestructible")
    assert lethal_damage(god) is None
    assert creature_state_facts(god, mine=False)["dies_to_damage"] == "never (indestructible)"
    assert lethal_damage({"name": "?", "toughness": None}) is None


def test_board_block_marks_damage_and_indestructible():
    state = strike_state()
    state["battlefield"] += [
        _creature(374, "Inspiring Overseer", OPP, 3, 2, "Flying", damage=1),
        _creature(500, "Indestructible God", OPP, 5, 6, "Indestructible"),
    ]
    prompt = _prompt(state, strike_decision())
    overseer = next(line for line in prompt.splitlines() if "Inspiring Overseer 3/2" in line)
    assert overseer.endswith("— 1 damage marked; dies to 1 damage")
    god = next(line for line in prompt.splitlines() if "Indestructible God 5/6" in line)
    assert god.endswith("— indestructible: damage can't kill it")


def test_log_parse_keeps_marked_damage_until_cleanup():
    game = GameState()
    full = {"instanceId": 288, "grpId": 98524, "type": "GameObjectType_Card", "zoneId": 28, "ownerSeatId": 1}
    full |= {"controllerSeatId": 1, "cardTypes": ["CardType_Creature"], "power": {"value": 1}}
    full |= {"toughness": {"value": 1}}
    game._update_game_object({**full, "damage": 3})
    assert game.game_objects[288].damage == 3
    assert game.game_objects[288].to_dict()["damage"] == 3
    game._update_game_object({"instanceId": 288, "isTapped": True})  # a partial update keeps it
    assert game.game_objects[288].damage == 3
    game._update_game_object(full)  # cleanup resends the full object without damage
    assert game.game_objects[288].damage == 0
    assert "damage" not in game.game_objects[288].to_dict()
