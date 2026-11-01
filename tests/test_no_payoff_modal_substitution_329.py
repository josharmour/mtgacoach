"""#329: a no-kill modal rewrite needs a supported cast payoff.

The #65 rewrite keeps a modal damage spell's cast by switching its targeted
mode to a one-sided untargeted mode when the target would survive. That is
only justified while the cast feeds a cast trigger into this turn's combat.
Without such a payoff the selected mode must be left alone for ordinary
target/cancellation evaluation; the untargeted mode's chip damage is not a
reason to rewrite the choice.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from tests.test_modal_no_kill_switch_20261019 import IROH, MODE_TEXT, modes, with_payoff
from tests.test_self_cancel_memory_20261019 import (
    LOCAL,
    OPP,
    board_state,
    creature,
    make_iroh_engine,
    modes_poll,
)

from arenamcp.cast_triggers import cast_trigger_attackers, untargeted_mode_instead

PUMP_TEXT = "Whenever you cast a noncreature spell, Hired Claw gets +2/+0 until end of turn."


@pytest.fixture
def iroh_engine(monkeypatch):
    return make_iroh_engine(monkeypatch)


def no_payoff(*, phase="Phase_Main2"):
    """The #329 board: two vanilla attackers and a 5/5 that survives 4 damage."""
    state = board_state(stack=True)
    state["turn"]["phase"] = phase
    state["battlefield"] = [
        creature(101, "Hired Claw", LOCAL, 1, 2),
        creature(102, "Giant Cindermaw", LOCAL, 4, 3),
        creature(103, "Overlord", OPP, 5, 5),
    ]
    return state


def test_no_payoff_leaves_the_selected_mode():
    state = no_payoff()
    assert cast_trigger_attackers(state) == []
    assert untargeted_mode_instead(IROH, ["idx:1"], state) is None


def test_main_one_without_a_cast_trigger_still_leaves_the_mode():
    state = no_payoff(phase="Phase_Main1")
    assert cast_trigger_attackers(state) == []
    assert untargeted_mode_instead(IROH, ["idx:1"], state) is None


def test_a_cast_trigger_that_cannot_attack_is_not_payoff():
    state = no_payoff(phase="Phase_Main1")
    state["battlefield"][0].update(oracle_text=PUMP_TEXT, keywords=["prowess"], is_tapped=True)
    assert cast_trigger_attackers(state) == []
    assert untargeted_mode_instead(IROH, ["idx:1"], state) is None


def test_a_summoning_sick_cast_trigger_is_not_payoff():
    state = no_payoff(phase="Phase_Main1")
    state["battlefield"][0].update(oracle_text=PUMP_TEXT, keywords=["prowess"], turn_entered_battlefield=8)
    assert cast_trigger_attackers(state) == []
    assert untargeted_mode_instead(IROH, ["idx:1"], state) is None


def test_payoff_still_yields_to_a_real_kill():
    state = with_payoff(board_state())
    state["battlefield"].append(creature(400, "Hired Claw", OPP, 1, 2))
    assert cast_trigger_attackers(state)
    assert untargeted_mode_instead(IROH, ["idx:1"], state) is None


def test_payoff_still_rejects_a_sweeper_that_hits_ours():
    state = with_payoff(board_state())
    sweeper = modes("CARDNAME deals 2 damage to each creature.", MODE_TEXT[21773])
    assert cast_trigger_attackers(state)
    assert untargeted_mode_instead(sweeper, ["idx:1"], state) is None


def test_payoff_still_leaves_an_already_untargeted_mode():
    state = with_payoff(board_state())
    assert cast_trigger_attackers(state)
    assert untargeted_mode_instead(IROH, ["idx:0"], state) is None


def _enrich_modes(monkeypatch):
    import arenamcp.card_db as card_db

    monkeypatch.setattr(
        card_db,
        "get_card_database",
        lambda: SimpleNamespace(get_ability_text=lambda grp: MODE_TEXT.get(grp)),
    )


def test_the_autopilot_submits_the_selected_mode_without_payoff(iroh_engine, monkeypatch, caplog):
    _enrich_modes(monkeypatch)
    engine, bridge, planner = iroh_engine  # its planner picks "idx:1" (4 damage)
    bridge.get_pending_actions.return_value = modes_poll()
    with caplog.at_level(logging.WARNING, logger="arenamcp.autopilot"):
        assert engine._try_typed_decision_path(no_payoff(), "decision_required")
    (index,) = bridge.submit_action_by_index.call_args.args
    assert index == 1
    assert bridge.submit_action_by_index.call_args.kwargs["expected"]["grpId"] == 21773
    bridge.cancel_action.assert_not_called()
    assert "Modal switch" not in caplog.text
    planner.plan_decision_options.assert_called_once()
    decision = planner.plan_decision_options.call_args.args[0]
    assert any("4 damage" in option.label for option in decision.options)


def test_the_autopilot_submits_the_targeted_mode_when_it_kills(iroh_engine, monkeypatch, caplog):
    _enrich_modes(monkeypatch)
    state = with_payoff(board_state(stack=True))
    state["battlefield"].append(creature(400, "Hired Claw", OPP, 1, 2))
    engine, bridge, planner = iroh_engine
    bridge.get_pending_actions.return_value = modes_poll()
    with caplog.at_level(logging.WARNING, logger="arenamcp.autopilot"):
        assert engine._try_typed_decision_path(state, "decision_required")
    (index,) = bridge.submit_action_by_index.call_args.args
    assert index == 1
    assert bridge.submit_action_by_index.call_args.kwargs["expected"]["grpId"] == 21773
    bridge.cancel_action.assert_not_called()
    assert "Modal switch" not in caplog.text
    planner.plan_decision_options.assert_called_once()
    decision = planner.plan_decision_options.call_args.args[0]
    assert any("4 damage" in option.label for option in decision.options)
