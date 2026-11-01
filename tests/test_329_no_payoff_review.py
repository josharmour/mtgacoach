"""Bounded #329 source-path review, not a full historical replay.

Reconstructs the report's no-kill/no-cast-trigger property. It does not assert
that pass was strategically optimal or that a chosen target was illegal.
"""

from tests.test_modal_no_kill_switch_20261019 import IROH
from tests.test_self_cancel_memory_20261019 import board_state, creature

from arenamcp.autopilot import AutopilotEngine
from arenamcp.cast_triggers import cast_trigger_attackers


def _no_payoff():
    state = board_state(stack=True)
    state["turn"]["phase"] = "Phase_Main2"  # no remaining combat-damage synergy
    local = state["local_seat_id"]
    opponent = next(p["seat_id"] for p in state["players"] if not p["is_local"])
    state["battlefield"] = [
        creature(101, "Hired Claw", local, 1, 2),
        creature(102, "Giant Cindermaw", local, 4, 3),
        creature(103, "Overlord", opponent, 5, 5),
    ]
    return state


def test_no_payoff_mode_is_not_automatically_substituted_for_targeted_choice():
    state = _no_payoff()
    assert cast_trigger_attackers(state) == []
    # Leave the proposed mode for ordinary target/cancel evaluation; do not
    # assert that the only permitted strategy is cancelling the whole spell.
    assert AutopilotEngine._untargeted_mode_for_no_kill(IROH, state, ["idx:1"]) == ["idx:1"]


def test_real_prowess_payoff_keeps_untargeted_mode():
    state = _no_payoff()
    state["turn"]["phase"] = "Phase_Main1"
    own = state["battlefield"][0]
    own.update(name="Emberheart Challenger", keywords=["prowess"])
    assert cast_trigger_attackers(state)
    assert AutopilotEngine._untargeted_mode_for_no_kill(IROH, state, ["idx:1"]) == ["idx:0"]
