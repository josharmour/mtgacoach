"""All-in attacks when no defensive line survives (bug_20261006_180436).

At 3 life facing a 3/3 flying Thopter and ~12 damage through our best blocks,
the planner held every blocker back and attacked with nothing.
"""

from __future__ import annotations

from copy import deepcopy

from tests.bug_180436_states import attack_state

from arenamcp.board_assessment import ROLE_AGGRESSOR, assess
from arenamcp.combat_strategy import losing_attackers
from arenamcp.game_plan import GamePlan, validate_plan


def test_dead_next_attack_whatever_we_do_means_all_in():
    assessment = assess(attack_state(3))
    assert assessment.all_in and assessment.role == ROLE_AGGRESSOR
    assert any(flag.startswith("ALL-IN") for flag in assessment.flags)


def test_a_healthy_life_total_is_not_all_in():
    assessment = assess(attack_state(20))
    assert not assessment.all_in


def test_plan_validator_forces_aggressor_when_all_in():
    state = attack_state(3)
    plan = GamePlan(role="control/stabilize", role_reason="survive the crackback and hold all blockers")
    validate_plan(plan, assess(state), state)
    assert plan.role == ROLE_AGGRESSOR
    assert any("all-in" in issue for issue in plan.issues)


def test_losing_attack_guard_steps_aside_when_all_in():
    state = attack_state(3)
    attackers = state["decision_context"]["legal_attacker_ids"]
    assert losing_attackers(deepcopy(state), attackers) == {}


def test_bridge_replaces_an_empty_attack_with_every_legal_attacker():
    from arenamcp.autopilot_bridge import _BridgeSubmitMixin

    state = attack_state(3)
    action = _BridgeSubmitMixin._all_in_attack_action(None, state, [])
    assert action is not None and len(action.attacker_names) == len(
        state["decision_context"]["legal_attacker_ids"]
    )
    assert _BridgeSubmitMixin._all_in_attack_action(None, attack_state(20), []) is None
