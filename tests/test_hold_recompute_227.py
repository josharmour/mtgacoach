"""Ledger #227: the hold policy recomputes against the post-resolution board.

Match 2c397f62 T14 (2026-10-23 20:18-20:19): the line cast Lightning Strike
AND Tectonic Hazard to kill their 5/4 Enigma Drake. Strike resolved (3 damage
marked); the very next ActionsAvailable re-held Hazard with the verbatim
pre-cast rationale "it kills none of their creatures now and pumps nothing" —
false after the Strike — and passed. The Drake survived at full size and won
the race.

The log's object snapshots miss spell damage, so the planner records the
damage its own target steps dealt this turn and the hold checks see it.
"""

from __future__ import annotations

from tests.race_boards_20261019 import CARDS  # noqa: F401  (fixture parity with the burn-timing file)
from tests.test_burn_timing_20261019 import _board, _card, _land, _menu, _option, _targets
from tests.test_decision_layer_20261019 import _Model, _planner, _reply

from arenamcp.decision_memory import burn_kills_nothing

DRAKE = {
    "type_line": "Creature — Drake",
    "card_types": ["CardType_Creature"],
    "power": 5,
    "toughness": 4,
    "keywords": ["flying"],
    "oracle_text": "Flying",
}


def _t14_main():
    """Our main 1, turn 14: two Mountains open; their Enigma Drake (5/4) flies.

    The snapshot shows NO damage on the Drake — as it did live after our
    Strike resolved (the log missed the spell's damage mark).
    """
    drake = _card(338, "Enigma Drake", 1, **DRAKE)
    hand = [_card(222, "Tectonic Hazard", 2), _card(223, "Lightning Strike", 2)]
    return _board(
        2,
        14,
        2,
        "Phase_Main1",
        "",
        ours=[_land(300, 2), _land(301, 2)],
        theirs=[drake, _land(401, 1, True, "Swamp")],
        hand=hand,
        life=15,
        their_life=7,
        lands_played=1,
    )


def _strike_target_state():
    state = _t14_main()
    state["stack"] = [_card(400, "Lightning Strike", 2)]
    return state


def test_hazard_kills_nothing_before_our_strike_so_it_is_held():
    state = _t14_main()
    hazard = state["hand"][0]
    assert burn_kills_nothing(str(hazard["oracle_text"]), state, castable=hazard)
    model = _Model(_reply("pass", "nothing to do"))
    planner = _planner(model)
    assert planner.plan_decision_options(_menu(state, (222, True)), state) == ["pass"]
    assert model.efforts == []
    assert "kills none of their creatures now" in planner.get_decision_reasoning(["pass"])


def test_after_our_strike_resolves_the_hold_yields_and_hazard_goes():
    """Strike resolved on the Drake this turn: Hazard's 1 now kills it (#227)."""
    model = _Model(_reply("tgt:338", "Strike the Drake; Hazard finishes it."))
    planner = _planner(model)
    # The target step of the Strike (the spell is on the stack).
    assert planner.plan_decision_options(_targets("Lightning Strike", [338]), _strike_target_state()) == [
        "tgt:338"
    ]
    assert planner._resolved_burn_marks().get("damage", {}).get(338) == 3

    # The very next window: same snapshot shape, Drake still undamaged on paper.
    next_state = _t14_main()
    menu = _menu(next_state, (222, True))
    model.answers[None] = [_reply(_option(menu, 222), "Hazard kills the wounded Drake now.")]
    chosen = planner.plan_decision_options(menu, next_state)
    assert chosen == [_option(menu, 222)], planner.get_decision_reasoning(chosen)
    # SelectTargets is out of the fast path's families by design, so both the
    # target step and the Hazard window asked the model.
    assert len(model.efforts) == 2
    assert "kills none of their creatures now" not in planner.get_decision_reasoning(chosen)
    assert "false_kill_claim" not in planner.get_last_decision_trace()


def test_the_mark_expires_with_the_turn():
    model = _Model(_reply("tgt:338", "Strike."))
    planner = _planner(model)
    assert planner.plan_decision_options(_targets("Lightning Strike", [338]), _strike_target_state()) == [
        "tgt:338"
    ]
    target_calls = len(model.efforts)
    next_turn = _t14_main()
    next_turn["turn"]["turn_number"] = 15
    menu = _menu(next_turn, (222, True))
    assert planner.plan_decision_options(menu, next_turn) == ["pass"]
    # The expired mark re-holds deterministically: no extra model call.
    assert len(model.efforts) == target_calls


def test_our_own_creatures_are_never_marked():
    """The patch only touches their side of the battlefield."""
    planner = _planner(_Model(_reply("tgt:10", "our own creature")))
    state = _t14_main()
    state["battlefield"].append(_card(10, "Hired Claw", 2))
    state["stack"] = [_card(400, "Lightning Strike", 2)]
    planner.plan_decision_options(_targets("Lightning Strike", [10]), state)
    marked = planner._state_with_our_burn_marks(state)
    claw = next(c for c in marked["battlefield"] if c["instance_id"] == 10)
    assert "damage" not in claw


def test_strike_mark_comes_from_the_cards_oracle():
    """The recorded damage is what Lightning Strike deals (3)."""
    planner = _planner(_Model(_reply("tgt:338", "Strike.")))
    planner.plan_decision_options(_targets("Lightning Strike", [338]), _strike_target_state())
    marks = planner._resolved_burn_marks()
    assert marks["turn"] == 14
    assert marks["damage"] == {338: 3}
