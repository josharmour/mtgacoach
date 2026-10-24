"""Ledger #226(a)/(b): planner micro-wins — the single-option fast path and answer reuse.

2026-10-23 audit: 13% of typed decisions offered exactly one legal option and
still spent ~1.8 s of model time naming it; ~14% of model answers were
discarded ("request changed during planning") when only the game-state id had
moved and the options were identical — ~480 s of model time re-earning the
same answer.
"""

from __future__ import annotations

import json

from tests.race_boards_20261019 import CARDS
from tests.test_burn_timing_20261019 import _board, _card, _menu, _option
from tests.test_decision_layer_20261019 import _Model, _planner, _reply

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision, TargetSlot

LAND = {
    "type_line": "Basic Land — Mountain",
    "card_types": ["CardType_Land"],
    "oracle_text": "({T}: Add {R}.)",
}


def _our_main(turn=6, phase="Phase_Main1", step=""):
    """Our main 1: two Mountains, Hired Claw in hand, no creatures anywhere."""
    hand = [_card(227, "Hired Claw", 2), _card(228, "Mountain", 2, **LAND)]
    return _board(
        2,
        turn,
        2,
        phase,
        step,
        ours=[_land(300, 2), _land(301, 2)],
        theirs=[_land(401, 1, True, "Swamp")],
        hand=hand,
        life=20,
        their_life=17,
        lands_played=1,
    )


def _land(iid, seat, tapped=False, name="Mountain"):
    return _card(iid, name, seat, is_tapped=tapped, **LAND)


# --- #226(a): one legal option, no model ------------------------------------------------------------


def test_single_action_window_never_reaches_the_model():
    model = _Model(_reply("idx:0", "cast it"))
    planner = _planner(model)
    state = _our_main()
    decision = PendingDecision(
        request_id=(9, 9),
        request_type="ActionsAvailable",
        options=(
            DecisionOption(
                "idx:0",
                "Cast Hired Claw [OK]",
                payable=True,
                meta={"actionType": "ActionType_Cast", "instanceId": 227},
            ),
        ),
        min_select=1,
        max_select=1,
    )
    assert planner.plan_decision_options(decision, state) == ["idx:0"]
    assert model.efforts == []


def test_single_target_and_pass_only_windows_keep_their_model_call():
    """Deliberate exclusions: cast-or-decline and the pass reasoning are real content."""
    state = _our_main()
    state["hand"].append(_card(230, "Lightning Strike", 2))
    # The controller gate needs to know who owns the target: put it on their side.
    state["battlefield"].append(_card(307, "Enigma Drake", 1))
    model = _Model(_reply("tgt:307", "the only target"))
    planner = _planner(model)
    decision = PendingDecision(
        request_id=(301, 402),
        request_type="SelectTargets",
        options=(DecisionOption("tgt:307", "Target #307", meta={"instanceId": 307}),),
        min_select=1,
        max_select=1,
        can_cancel=True,
        source_label="Lightning Strike",
        slots=(TargetSlot(1, 1, 1, 0, (307,)),),
    )
    assert planner.plan_decision_options(decision, state) == ["tgt:307"]
    # SelectTargets is out of the fast path's families: the model decided.
    assert len(model.efforts) == 1
    assert planner.get_last_decision_trace().get("policy") != "single_option"
    pass_only = PendingDecision(
        request_id=(9, 10),
        request_type="ActionsAvailable",
        options=(DecisionOption("pass", "Pass"),),
        min_select=1,
        max_select=1,
    )
    model2 = _Model(_reply("pass", "hold everything"))
    planner2 = _planner(model2)
    assert planner2.plan_decision_options(pass_only, state) == ["pass"]
    assert len(model2.efforts) == 1


def test_the_fast_path_skips_an_unpayable_or_unvalidatable_single_option():
    model = _Model(_reply("idx:0", "cast"))
    planner = _planner(model)
    state = _our_main()
    unpayable = PendingDecision(
        request_id=(9, 9),
        request_type="ActionsAvailable",
        options=(DecisionOption("idx:0", "Cast Hired Claw [OK]", payable=False),),
        min_select=1,
        max_select=1,
    )
    planner.plan_decision_options(unpayable, state)
    # An unpayable lone option never enters the fast path: the deterministic
    # filters (and, when one survives them, the model) own that choice.
    assert planner.get_last_decision_trace().get("policy") != "single_option"


# --- #226(b): an identical re-presented window reuses the answer ------------------------------------


def test_represented_window_with_identical_options_reuses_the_answer():
    import dataclasses

    model = _Model(_reply("idx:0", "Develop the board with Hired Claw."))
    planner = _planner(model)
    state = _our_main()
    first_menu = _menu(state, (227, True))
    first = planner.plan_decision_options(first_menu, state)
    assert first == [_option(first_menu, 227)]
    # Arena re-presents the request with a bumped game-state id: same options.
    second_menu = dataclasses.replace(first_menu, request_id=(999, 1000))
    assert planner.plan_decision_options(second_menu, state) == first
    assert len(model.efforts) == 1
    assert planner.get_last_decision_trace()["policy"] == "reused_answer"


def test_a_different_menu_or_turn_or_step_asks_again():
    answers = [_reply("idx:0", "Claw."), _reply("idx:0", "Claw."), _reply("idx:0", "Claw.")]
    model = _Model(answers)
    planner = _planner(model)
    state = _our_main()
    menu = _menu(state, (227, True))
    assert planner.plan_decision_options(menu, state) == [_option(menu, 227)]

    # A second castable card: a different menu is a different question.
    wider_state = _our_main()
    wider_state["hand"].append(_card(229, "Slickshot Show-Off", 2))
    wider = _menu(wider_state, (227, True), (229, True))
    planner.plan_decision_options(wider, wider_state)
    assert len(model.efforts) == 2

    # A new turn: the board may have changed under the same menu.
    next_turn = _our_main(turn=7)
    planner.plan_decision_options(_menu(next_turn, (227, True)), next_turn)
    assert len(model.efforts) == 3

    # A later step of the same turn: mana emptied, combat happened (#153).
    later_step = _our_main(phase="Phase_Main2")
    planner.plan_decision_options(_menu(later_step, (227, True)), later_step)
    assert len(model.efforts) == 4


def test_only_model_answers_are_cached():
    """A deterministic policy's pick must recompute against the live board."""
    from tests.test_llm_down_fallback import _Backend

    from arenamcp.backends.proxy import BackendError

    planner = ActionPlanner(_Backend(error=BackendError("down")), timeout=5, land_drop_first=False)
    state = _our_main()
    menu = _menu(state, (227, True))
    first = planner.plan_decision_options(menu, state)
    again = planner.plan_decision_options(_menu(state, (227, True)), state)
    assert first == again
    assert planner.get_last_decision_trace().get("policy") != "reused_answer"


def test_expired_or_illegal_cache_entries_are_ignored():
    model = _Model([_reply("idx:0", "Claw."), _reply("idx:0", "Claw again.")])
    planner = _planner(model)
    state = _our_main()
    menu = _menu(state, (227, True))
    planner.plan_decision_options(menu, state)
    entry = planner._answer_cache
    entry["at"] -= planner._ANSWER_REUSE_TTL_S + 1.0
    planner.plan_decision_options(_menu(state, (227, True)), state)
    assert len(model.efforts) == 2
