"""A pinger whose ping equals its power is held when it would only feed a blocker (#78, a #45 gap).

2026-10-19 T10 (match abde2a07, 11:31:28): Hired Claw 1/2 ("Whenever you
attack with one or more Lizards, this creature deals 1 damage to target
opponent") attacked alone past an untapped Creakwood Safewright 4/4, Lys
Alana Dignitary 2/3 and Thornweald Archer. The losing-attack guard let it go
twice over:
- ``_doomed`` priced the attack as "1 own damage for a 1-damage ping", a
  deficit of 0, and holds only above 0. The ping fires whether or not the
  Claw is blocked, and every later attack fires it again: what the attack
  costs is the creature, not one hit;
- its own {1}{R} +1/+1 counter, affordable with three open Mountains, counted
  as a trick that "could save it", though a 2/3 is still eaten by the 4/4.
Then (11:31:39) the planner spent {1}{R} on that counter before blocks, the
4/4 blocked the 2/3, and the Claw died. The line search's best line did not
attack. The board is the match's Player.log at the DeclareAttackers request.
"""

from __future__ import annotations

import copy
import logging

from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
from arenamcp.combat_strategy import losing_attackers, pump_before_blocks_wasted
from arenamcp.decisions import build_pending_decision
from arenamcp.play_safety import filter_play_options, unsafe_play_reason

US, THEM = 1, 2
CLAW_TEXT = (
    "Whenever you attack with one or more Lizards, this creature deals 1 damage to target opponent.\n"
    "{1}{R}: Put a +1/+1 counter on this creature. Activate only if an opponent lost life this turn and "
    "only once each turn."
)
EMBERHEART_TEXT = (
    "Haste\nProwess (Whenever you cast a noncreature spell, this creature gets +1/+1 until end of turn.)\n"
    "Valiant — Whenever this creature becomes the target of a spell or ability you control for the first "
    "time each turn, exile the top card of your library. Until end of turn, you may play that card."
)
SLICKSHOT_TEXT = (
    "Flying, haste\nWhenever you cast a noncreature spell, this creature gets +2/+0 until end of turn.\n"
    "Plot {1}{R} (You may pay {1}{R} and exile this card from your hand. Cast it as a sorcery on a later "
    "turn without paying its mana cost. Plot only as a sorcery.)"
)
SAFEWRIGHT_TEXT = (
    "This creature enters with three -1/-1 counters on it.\nAt the beginning of your end step, if there is "
    "an Elf card in your graveyard and this creature has a -1/-1 counter on it, remove a -1/-1 counter from "
    "this creature."
)
DIGNITARY_TEXT = (
    "As an additional cost to cast this spell, behold an Elf or pay {2}.\n{T}: Add {G}{G}. Activate only if "
    "there is an Elf card in your graveyard."
)
# The ability as the planner's typed menu offers it (cost, effect).
PUMP_COST = "{1}{R}"
PUMP_EFFECT = (
    "Put a +1/+1 counter on this creature. Activate only if an opponent lost life this turn and only once "
    "each turn."
)
CLAW_PUMP = {
    "actionType": "ActionType_Activate",
    "grpId": 91676,
    "instanceId": 282,
    "abilityGrpId": 174025,
    "manaCost": [{"color": "Generic", "count": 1}, {"color": "Red", "count": 1}],
    "hasAutoTap": True,
}


def creature(iid, name, power, toughness, seat, text="", *, type_line="Creature", **extra):
    return {
        "instance_id": iid,
        "name": name,
        "type_line": type_line,
        "card_types": ["CardType_Creature"],
        "power": power,
        "toughness": toughness,
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "oracle_text": text,
        "is_tapped": False,
        **extra,
    }


def land(iid, name, seat, *, tapped=False, color="R"):
    return {
        "instance_id": iid,
        "name": name,
        "type_line": f"Basic Land — {name}",
        "card_types": ["CardType_Land"],
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "oracle_text": f"({{T}}: Add {{{color}}}.)",
        "is_tapped": tapped,
    }


def safewright():
    return creature(
        321,
        "Creakwood Safewright",
        4,
        4,
        THEM,
        SAFEWRIGHT_TEXT,
        type_line="Creature — Elf Warrior",
        counters={"CounterType_M1M1": 1},
    )


def dignitary():
    return creature(
        324, "Lys Alana Dignitary", 2, 3, THEM, DIGNITARY_TEXT, type_line="Creature — Elf Advisor"
    )


def archer():
    return creature(
        337,
        "Thornweald Archer",
        2,
        1,
        THEM,
        "Reach\nDeathtouch",
        type_line="Creature — Elf Archer",
        keywords=["reach", "deathtouch"],
        turn_entered_battlefield=9,
    )


def turn_10_board(theirs=None, *, opponent_life=15) -> dict:
    """11:31:24: Slickshot cast with two Mountains, three open; their 4/4, 2/3 and reach deathtoucher untapped."""
    theirs = [safewright(), dignitary(), archer()] if theirs is None else theirs
    ours = [
        creature(282, "Hired Claw", 1, 2, US, CLAW_TEXT, type_line="Creature — Lizard Mercenary"),
        *(
            creature(
                iid,
                "Emberheart Challenger",
                2,
                2,
                US,
                EMBERHEART_TEXT,
                type_line="Creature — Mouse Warrior",
                keywords=["haste"],
            )
            for iid in (330, 333)
        ),
        creature(
            343,
            "Slickshot Show-Off",
            1,
            2,
            US,
            SLICKSHOT_TEXT,
            type_line="Creature — Bird Wizard",
            keywords=["flying", "haste"],
            turn_entered_battlefield=10,
        ),
    ]
    return {
        "turn": {
            "turn_number": 10,
            "active_player": US,
            "priority_player": US,
            "phase": "Phase_Combat",
            "step": "Step_DeclareAttack",
        },
        "local_seat_id": US,
        "opponent_seat_id": THEM,
        "players": [
            {"seat_id": US, "life_total": 20, "is_local": True, "mana_pool": {}},
            {"seat_id": THEM, "life_total": opponent_life, "is_local": False, "mana_pool": {}},
        ],
        "battlefield": [
            *ours,
            land(281, "Mountain", US, tapped=True),
            land(290, "Mountain", US, tapped=True),
            *(land(iid, "Mountain", US) for iid in (305, 329, 342)),
            *theirs,
            land(279, "Forest", THEM, color="G"),
            land(299, "Forest", THEM, tapped=True, color="G"),
            land(285, "Swamp", THEM, color="B"),
            land(320, "Forest", THEM, tapped=True, color="G"),
        ],
        "hand": [],
        "graveyard": [],
        "exile": [],
        "stack": [],
        "zones": {"opponent_hand_count": 3},
        "decision_context": {
            "type": "declare_attackers",
            "legal_attackers": [
                "Hired Claw",
                "Emberheart Challenger",
                "Emberheart Challenger",
                "Slickshot Show-Off",
            ],
            "legal_attacker_ids": [282, 330, 333, 343],
        },
    }


def after_the_ping(board: dict | None = None) -> dict:
    """11:31:34: the Claw attacks alone and its ping resolved (opponent at 14); no blocks yet."""
    state = copy.deepcopy(board or turn_10_board())
    state["players"][1]["life_total"] -= 1
    claw = next(card for card in state["battlefield"] if card["instance_id"] == 282)
    claw.update(is_attacking=True, is_tapped=True)
    state["decision_context"] = None
    return state


def claw_of(state: dict) -> dict:
    return next(card for card in state["battlefield"] if card["instance_id"] == 282)


# --- the attack -------------------------------------------------------------------


def test_hired_claw_alone_into_the_untapped_four_four_is_held():
    losing = losing_attackers(turn_10_board(), [282])
    assert set(losing) == {282}
    reason = losing[282]
    assert "Creakwood Safewright 4/4 can block Hired Claw 1/2, kill it and survive" in reason
    assert "its attack trigger is worth only 1" in reason


def test_planner_holds_the_claw_and_marks_the_hold(caplog):
    state = turn_10_board()
    plan = ActionPlan(
        actions=[
            GameAction(
                ActionType.DECLARE_ATTACKERS,
                attacker_names=["Hired Claw"],
                attacker_instance_ids=[282],
                target_names=["Opponent"],
            )
        ],
        overall_strategy="Chip with Hired Claw's free attack ping while preserving the prowess crew.",
    )
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        ActionPlanner.__new__(ActionPlanner)._check_losing_attacks(plan, state, state["decision_context"])
    assert "Losing-attack guard: not attacking with Hired Claw [282]" in caplog.text
    action = plan.actions[0]
    assert action.attacker_names == [] and plan.fallback_reason == "planner_losing_attack"
    # #75: the bridge must not refill the emptied declaration.
    assert action.guard_hold is True


def test_a_pump_that_wins_the_only_fight_keeps_the_attack():
    # Against the 2/3 alone, the {1}{R} counter makes a 2/3 that survives the block.
    assert losing_attackers(turn_10_board([dignitary()]), [282]) == {}
    # Without the mana for it, the 2/3 eats the 1/2.
    board = turn_10_board([dignitary()])
    for card in board["battlefield"]:
        if card["instance_id"] in (305, 329, 342):
            card["is_tapped"] = True
    assert set(losing_attackers(board, [282])) == {282}


def test_a_lethal_ping_still_attacks():
    # At 1 life the ping alone wins: the attack is lethal whatever blocks.
    assert losing_attackers(turn_10_board(opponent_life=1), [282]) == {}


def test_nobody_to_eat_it_still_attacks():
    assert losing_attackers(turn_10_board([archer()]), [282]) == {}  # the 1/2 kills the 2/1 it trades with


# --- the pump before blocks ---------------------------------------------------------


def test_the_pump_before_blocks_is_withheld_while_the_four_four_still_eats_it():
    state = after_the_ping()
    reason = pump_before_blocks_wasted(state, claw_of(state), PUMP_EFFECT, PUMP_COST)
    assert reason.startswith("pumping the attacking Hired Claw to 2/3 before blocks saves nothing")
    assert "Creakwood Safewright 4/4 can still block it, kill it and survive" in reason
    assert unsafe_play_reason(state, claw_of(state), "ActionType_Activate", CLAW_PUMP) == reason


def test_the_typed_menu_drops_the_pump_and_keeps_the_pass():
    state = after_the_ping()
    menu = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "ActionsAvailable",
            "can_pass": True,
            "actions": [CLAW_PUMP, {"actionType": "ActionType_Pass"}],
        },
        resolve_name=lambda grp: "Hired Claw",
    )
    assert [option.label for option in menu.options][0].startswith("Activate: Hired Claw")
    kept = filter_play_options(menu, state)
    assert [option.label for option in kept.options] == ["Pass"]


def test_arenas_this_wording_is_the_same_self_pump():
    # The live menu: "Activate: Hired Claw [{1}{R}: Put a +1/+1 counter on this. Activate only if ...]".
    state = after_the_ping()
    effect = "Put a +1/+1 counter on this. Activate only if an opponent lost life this turn and only once each turn."
    assert "saves nothing" in pump_before_blocks_wasted(state, claw_of(state), effect, PUMP_COST)
    claw = claw_of(state)
    claw["oracle_text"] = CLAW_TEXT.replace("on this creature", "on this")
    assert "saves nothing" in unsafe_play_reason(state, claw, "ActionType_Activate", CLAW_PUMP)
    # A pump of some other creature is a target choice made later: not judged here.
    assert (
        pump_before_blocks_wasted(state, claw, "Target creature gets +1/+1 until end of turn.", "{1}") == ""
    )


def test_a_repeatable_pump_is_judged_with_every_activation_the_mana_pays_for():
    state = after_the_ping()
    firebreath = "This creature gets +1/+1 until end of turn."
    # Three open Mountains: three activations make a 4/5 the 4/4 no longer eats.
    assert pump_before_blocks_wasted(state, claw_of(state), firebreath, "{R}") == ""
    # One open Mountain: a 2/3, still eaten.
    for card in state["battlefield"]:
        if card["instance_id"] in (329, 342):
            card["is_tapped"] = True
    assert "to 2/3 before blocks saves nothing" in pump_before_blocks_wasted(
        state, claw_of(state), firebreath, "{R}"
    )
    # The attack guard counts the activations too: with three open the 4/5 is no clear loser.
    board = turn_10_board()
    claw_of(board)["oracle_text"] = (
        CLAW_TEXT.splitlines()[0] + "\n{R}: This creature gets +1/+1 until end of turn."
    )
    assert losing_attackers(board, [282]) == {}


def test_the_pump_stays_when_it_saves_the_claw_or_blocks_are_in():
    # Only the 2/3 can block: the counter makes the Claw survive it, so pumping now deters the block.
    state = after_the_ping(turn_10_board([dignitary()]))
    assert pump_before_blocks_wasted(state, claw_of(state), PUMP_EFFECT, PUMP_COST) == ""
    # After blocks the pump decides a known fight: not this rule's call.
    state = after_the_ping()
    state["turn"]["step"] = "Step_DeclareBlock"
    next(card for card in state["battlefield"] if card["instance_id"] == 321)["is_blocking"] = True
    assert pump_before_blocks_wasted(state, claw_of(state), PUMP_EFFECT, PUMP_COST) == ""
    # A creature that is not attacking is not judged.
    state = turn_10_board()
    assert pump_before_blocks_wasted(state, claw_of(state), PUMP_EFFECT, PUMP_COST) == ""
