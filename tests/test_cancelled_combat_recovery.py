from unittest.mock import Mock

import pytest
from tests.fakes import make_engine


def setup():
    engine = make_engine()
    state = {
        "match_id": "m",
        "turn": {"turn_number": 12, "phase": "Combat", "step": "DeclareBlock"},
        "decision_context": {"type": "declare_blockers"},
    }
    pending = {"has_pending": True, "request_type": "DeclareBlockers", "game_state_id": 323}
    engine._gre_bridge = Mock()
    engine._gre_bridge.get_pending_actions.return_value = pending
    engine._plan_window_identity = Mock(return_value=(323, "DeclareBlockers", -1))
    engine._live_window_identity = Mock(return_value=(323, "DeclareBlockers", -1))
    engine._safe_default_blocks = Mock(side_effect=lambda s, p, ok: ok("fresh blocks"))
    engine._safe_default_attacks = Mock()
    return engine, state


def test_two_cancelled_plans_recover_using_fresh_solver_instead_of_old_plan():
    engine, state = setup()
    engine._note_cancelled_combat(state)
    assert not engine._recover_cancelled_combat(state, "decision_required")
    engine._note_cancelled_combat(state)
    assert engine._recover_cancelled_combat(state, "decision_required")
    engine._safe_default_blocks.assert_called_once()
    assert engine._cancelled_combat is None


@pytest.mark.parametrize(
    "change",
    ["turn", "match", "request", "manual", "handoff", "stopped", "dry_run", "expired", "poll_change"],
)
def test_recovery_never_crosses_window_or_control_boundaries(change):
    engine, state = setup()
    engine._note_cancelled_combat(state)
    engine._note_cancelled_combat(state)
    if change == "turn":
        state["turn"]["turn_number"] += 1
    elif change == "match":
        state["match_id"] = "other"
    elif change == "request":
        engine._live_window_identity.return_value = (324, "SelectTargets", -1)
    elif change == "manual":
        engine.in_manual_play_cooldown = lambda: True
    elif change == "handoff":
        engine.is_window_given_up = lambda _: True
    elif change == "expired":
        key, count, timestamp = engine._cancelled_combat
        engine._cancelled_combat = (key, count, timestamp - 11)
    elif change == "poll_change":
        engine._gre_bridge.get_pending_actions.return_value["game_state_id"] = 324
    elif change == "stopped":
        engine._abort_event.set()
    else:
        engine._config.dry_run = True
    assert not engine._recover_cancelled_combat(state, "decision_required")
    engine._safe_default_blocks.assert_not_called()


def test_attack_cancellation_keeps_existing_planning_path():
    engine, state = setup()
    state["decision_context"]["type"] = "declare_attackers"
    engine._note_cancelled_combat(state)
    engine._note_cancelled_combat(state)
    assert not engine._recover_cancelled_combat(state, "decision_required")
    engine._safe_default_blocks.assert_not_called()
    engine._safe_default_attacks.assert_not_called()
