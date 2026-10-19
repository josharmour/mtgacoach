"""Zero-upside attacks under trick exposure, and engine trades into burnable blockers (ledger #120, #113).

Boards from the matches' Player.log (2026-10-19):

- #120, match 2c67eed8 T10 (gsid 241, 12:50:07): we just cast Giant Cindermaw and Emberheart
  Challenger (all five Mountains tapped, hand empty); they are at 11 with an untapped Youthful
  Valkyrie 1/3 flying, two of four lands untapped and two cards in hand (one was Shock). Emberheart
  2/2 attacked alone ("Force the block; free pressure"): the Valkyrie blocked, nothing died, nothing
  got through. Their trick on that block would have killed it for free.
- #113, match ba060a28 T4 (gsid 71, 12:33:27): Emberheart Challenger 2/2 (prowess, valiant) attacked
  into an untapped Vampire Gourmand 2/2 with both our Mountains tapped and two Shocks in hand; they
  traded. Either Shock kills the Gourmand next turn and keeps the engine.
"""

from __future__ import annotations

from copy import deepcopy

from tests import race_boards_20261019 as rb

from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
from arenamcp.combat_strategy import losing_attackers

GOURMAND = {
    "type_line": "Creature — Vampire Rogue", "mana_cost": "{1}{B}", "power": 2, "toughness": 2,
    "card_types": ["CardType_Creature"],
    "oracle_text": "Whenever this creature attacks, you may sacrifice another creature. If you do, draw a card and "
    "this creature can't be blocked this turn.",
}  # fmt: skip
CINDERMAW = {
    "type_line": "Creature — Dinosaur Beast", "mana_cost": "{2}{R}", "power": 4, "toughness": 3,
    "keywords": ["trample"], "card_types": ["CardType_Creature"], "oracle_text": "Trample\nPlayers can't gain life.",
}  # fmt: skip
SWAMP = {"type_line": "Basic Land — Swamp", "oracle_text": "({T}: Add {B}.)", "card_types": ["CardType_Land"]}


def _add(state: dict, iid: int, name: str, seat: int, base: dict, **extra) -> dict:
    card = {
        "instance_id": iid,
        "name": name,
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        **deepcopy(base),
    }
    card.update(extra)
    state["battlefield"].append(card)
    return card


def _t10(*, their_open: int = 2, their_hand: int = 2, slickshot: bool = False) -> dict:
    """2c67eed8 T10 at the attack (local seat 2 in the game, 1 here)."""
    lands = [(279, "Mountain"), (286, "Plains"), (298, "Mountain"), (318, "Mountain")]
    state = rb.board(
        turn=10, life=19, their_life=11, lands_played=1, opponent_hand=their_hand,
        ours=[(iid, "Mountain", True, turn) for iid, turn in ((281, 2), (293, 4), (303, 6), (333, 8), (339, 10))]
        + [(344, "Emberheart Challenger", False, 10)],
        theirs=[(iid, name, index >= their_open, 1 + 2 * index) for index, (iid, name) in enumerate(lands)]
        + [(335, "Youthful Valkyrie", False, 9)],
        hand=[], phase="Phase_Combat", step="Step_DeclareAttack",
    )  # fmt: skip
    _add(state, 340, "Giant Cindermaw", rb.LOCAL, CINDERMAW, is_tapped=False, turn_entered_battlefield=10)
    if slickshot:
        state["battlefield"].append(
            rb.card(355, "Slickshot Show-Off", rb.LOCAL, is_tapped=False, turn_entered_battlefield=10)
        )
    return state


def test_a_lone_attacker_their_untapped_blocker_absorbs_is_held_under_trick_exposure():
    held = losing_attackers(_t10(), [344], strategy_text="Force the block; free pressure, crackback is safe.")
    assert held == {
        344: "Youthful Valkyrie 1/3 can block Emberheart Challenger 2/2 for free: neither dies and nothing gets "
        "through, while their 2 open mana and 2 cards in hand make a trick on that block a kill"
    }


def test_without_their_trick_exposure_the_attack_stands():
    assert losing_attackers(_t10(their_open=0), [344]) == {}  # every land tapped
    assert losing_attackers(_t10(their_hand=0), [344]) == {}  # nothing in hand


def test_a_blocker_busy_with_it_lets_the_rest_through():
    # T12's attack: the Valkyrie blocks one of Emberheart and the flying Slickshot; the other connects.
    assert losing_attackers(_t10(slickshot=True), [344, 355]) == {}


def _t4(
    *, hand=("Shock", "Shock", "Mountain", "Mountain", "Mountain"), our_open: int = 0, attacker=None
) -> dict:
    """ba060a28 T4 at the attack (local seat 1 in the game and here)."""
    state = rb.board(
        turn=4, life=20, their_life=17, lands_played=1, opponent_hand=5,
        ours=[(286, "Mountain", our_open < 1, 2), (296, "Mountain", our_open < 2, 4)],
        theirs=[],
        hand=[(700 + index, name) for index, name in enumerate(hand)],
        phase="Phase_Combat", step="Step_DeclareAttack",
    )  # fmt: skip
    if attacker is None:
        state["battlefield"].append(
            rb.card(297, "Emberheart Challenger", rb.LOCAL, is_tapped=False, turn_entered_battlefield=4)
        )
    else:
        _add(state, 297, attacker[0], rb.LOCAL, attacker[1], is_tapped=False, turn_entered_battlefield=4)
    for iid in (284, 291):
        _add(state, iid, "Swamp", rb.OPPONENT, SWAMP, is_tapped=True, turn_entered_battlefield=1)
    _add(state, 292, "Vampire Gourmand", rb.OPPONENT, GOURMAND, is_tapped=False, turn_entered_battlefield=3)
    return state


def test_an_engine_creature_does_not_trade_with_a_blocker_burn_in_hand_kills():
    held = losing_attackers(_t4(), [297], strategy_text="Aggro burn: chip damage with prowess attacker")
    assert held == {
        297: "Vampire Gourmand 2/2 would trade with Emberheart Challenger 2/2, which our spells grow, while "
        "Shock in hand kills Vampire Gourmand on its own: keep the engine"
    }


def test_the_even_trade_stands_without_burn_for_the_blocker_or_without_an_engine():
    assert losing_attackers(_t4(hand=("Mountain", "Mountain")), [297]) == {}
    bear = ("Grizzly Bears", rb.CARDS["Grizzly Bears"])
    assert losing_attackers(_t4(attacker=bear), [297]) == {}
    # Trading into a bigger body (a 3/2 Shock also kills) is still a trade worth making.
    bigger = _t4()
    next(card for card in bigger["battlefield"] if card["instance_id"] == 292)["power"] = 3
    assert losing_attackers(bigger, [297]) == {}


def test_with_mana_up_for_the_shock_the_attack_stands():
    # Shock castable now answers the block (or keeps them from blocking): the existing trick rule.
    assert losing_attackers(_t4(our_open=1), [297]) == {}


def _held_plan(state: dict, iid: int, reasoning: str) -> ActionPlan:
    plan = ActionPlan(
        actions=[
            GameAction(
                ActionType.DECLARE_ATTACKERS,
                attacker_names=["Emberheart Challenger"],
                attacker_instance_ids=[iid],
                attacker_targets={"Emberheart Challenger": "Opponent"},
                reasoning=reasoning,
            )
        ],
        overall_strategy=reasoning,
    )
    ActionPlanner(None, timeout=1, land_drop_first=False)._check_losing_attacks(
        plan, state, {"type": "declare_attackers"}
    )
    return plan


def test_the_planner_says_why_it_holds_them():
    bounce = _held_plan(_t10(), 344, "Force the block; free pressure, crackback is safe.")
    assert bounce.actions[0].attacker_names == []
    assert bounce.voice_advice == "Not attacking with Emberheart Challenger: it would be blocked for nothing."
    engine = _held_plan(_t4(), 297, "Aggro burn: chip damage with prowess attacker, save Shocks for reach.")
    assert engine.actions[0].attacker_names == []
    assert engine.voice_advice == (
        "Not attacking with Emberheart Challenger: our burn can kill that blocker instead of a trade."
    )
