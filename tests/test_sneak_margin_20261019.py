"""A Sneak far better than the normal cast is held for and cast after blocks (ledger #179, 2026-10-19).

Match c0af235f T10 (15:20:14-15:20:25; ``tests.valkyrie_boards_20261019``, Player.log
gsid 237): after three Hare Apparents, two Plains open and Leonardo, Big
Brother in the command zone. Arena offered his normal cast for {1}{W}
(Oketra's Monument's discount); Sneak was {W}. Master's Sneak hold
(``sneak.sneak_main_hold``, #156) only kept the mana for a LETHAL Sneak; the
Sneak line was 23 of their 27, so nothing stopped the hard cast ("joins the
21-creature board immediately, giving a big attacker next turn"). He came in
summoning sick; the attack that followed dealt 1. A Sneak after their blocks
returning the unblocked Warrior would have put a 21/3 into them (27 -> 6).

Now the hold, and the cast after blocks, also apply when the Sneak beats the
normal cast by at least half their life, at the same or a lower cost. The
#156 cases that leave a smaller margin to the model (be1a945f at 30 and 40
life: +14) are unchanged.
"""

from __future__ import annotations

from tests import valkyrie_boards_20261019 as vb

from arenamcp import sneak
from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision

LEO = 237  # Leonardo's command-zone instance id at T10


def _main1_menu() -> PendingDecision:
    """Arena's T10 menu at gsid 237: the normal cast for {1}{W}, two Plains, pass."""
    cast, plains, other, _pass = vb.T10_MENU_ACTIONS
    return PendingDecision(
        (237, 300), "ActionsAvailable",
        (DecisionOption("idx:0", "Cast Leonardo, Big Brother", True, cast),
         DecisionOption("idx:1", "Action: Activate_Mana", None, plains),
         DecisionOption("idx:2", "Action: Activate_Mana", None, other),
         DecisionOption("pass", "Pass")),
        can_pass=True,
    )  # fmt: skip


def _sneak_menu() -> PendingDecision:
    """Our declare-blockers priority with the Sneak cast offered ({W}, no tax yet)."""
    meta = {"actionType": "ActionType_Cast", "grpId": 100471, "instanceId": LEO, "alternative": "Sneak"}
    return PendingDecision(
        (256, 330), "ActionsAvailable",
        (DecisionOption("idx:0", "Cast Leonardo, Big Brother [via Sneak {W}]", True, meta),
         DecisionOption("pass", "Pass")),
        can_pass=True,
    )  # fmt: skip


def test_main_one_holds_the_sneak_mana_for_a_sneak_far_better_than_the_hard_cast():
    state = vb.t10_main1()
    (cast,) = sneak.sneak_casts(state)
    assert (cast.cost, cast.hard_cost, cast.zone) == ("{W}", "{2}{W}", "command")
    hold = sneak.sneak_main_hold(state)
    # Their three blockers take three of our six able attackers: 3 through; the Sneak returns a
    # 1/1 and Leonardo comes in with 21 creatures around: 23 of their 27, not lethal.
    assert (hold.through, hold.power, hold.total, hold.life, hold.keep) == (3, 21, 23, 27, 1)
    assert not hold.lethal and hold.gain == 20
    kept, why = sneak.hold_filter(_main1_menu(), state)
    assert kept.option_ids() == {"idx:1", "idx:2", "pass"}
    assert why == (
        "keep {W} open for the Sneak (attack with everything, then Sneak Leonardo, Big Brother ({W}) after "
        "blocks: 3 through their best blocks - the returned attacker + 21 = 23 against their 27 life, 20 more "
        "than casting it normally ({2}{W}), which comes in summoning sick); withholding Cast Leonardo, Big "
        "Brother"
    )


def test_the_normal_cast_is_withheld_even_when_the_mana_covers_both():
    # Three Plains open (one more than the game had): the {1}{W} cast leaves the {W}, but the card
    # cast normally is no longer there to Sneak.
    state = vb.t10_main1()
    next(c for c in state["battlefield"] if c["instance_id"] == 552)["is_tapped"] = False
    kept, why = sneak.hold_filter(_main1_menu(), state)
    assert "idx:0" not in kept.option_ids() and why.startswith("keep {W} open for the Sneak")


def test_a_smaller_margin_or_a_dearer_sneak_is_not_held():
    state = vb.t10_main1()
    state["players"][1]["life_total"] = 45  # +20 is less than half of 45
    assert sneak.sneak_main_hold(state) is None
    assert sneak.hold_filter(_main1_menu(), state)[1] == ""
    (cast,) = sneak.sneak_casts(vb.t10_main1())
    assert sneak.beats_hard_cast(cast, 20, 27, hard_mana=2)
    assert sneak.beats_hard_cast(cast, 20, 27, hard_mana=1)  # the same cost
    assert not sneak.beats_hard_cast(cast, 20, 27, hard_mana=0)  # the Sneak would cost more
    assert not sneak.beats_hard_cast(cast, 13, 27, hard_mana=2)  # 13 < 13.5


def test_after_blocks_the_sneak_is_cast_for_twenty_more_damage():
    state = vb.t10_declare_blockers()
    found = sneak.sneak_now(state)
    # The game's blocks: the Warrior was the one unblocked attacker; it goes back, Leonardo comes in.
    assert (found.through, found.power, found.total, found.life) == (1, 21, 21, 27)
    assert not found.lethal and found.gain == 20 and found.beats_hard_cast
    assert found.returned["instance_id"] == 576
    picked, why = sneak.sneak_cast_pick(_sneak_menu(), state)
    assert picked == ["idx:0"]
    assert why == (
        "Sneak: Sneak Leonardo, Big Brother ({W}) returning Warrior: 1 unblocked - 1 + 21 = 21 against their "
        "27 life; 20 more damage than casting it normally ({2}{W}), at least half their life."
    )
    planner = ActionPlanner.__new__(ActionPlanner)
    planner._sneak_notes = []
    _kept, picked = planner._sneak_policy(_sneak_menu(), state)
    assert picked == ["idx:0"] and planner._last_decision_trace["policy"] == "sneak_margin"


def test_after_blocks_a_smaller_margin_is_left_to_the_model():
    state = vb.t10_declare_blockers()
    state["players"][1]["life_total"] = 45
    assert sneak.sneak_cast_pick(_sneak_menu(), state) == ([], "")
