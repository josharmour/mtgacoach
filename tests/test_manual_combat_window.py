"""bug_20261008_172415: a combat window handed to the user is not re-planned.

17:23:15 the declare-attackers submission failed (manual required); the user
attacked by hand. 17:24:06 the log's stale decision was auto-cleared, the GRE's
DeclareAttackersReq was re-captured with a new bridge game-state id, the
backstop forced decision_required and the autopilot planned the same window
again — "window closed before submission" 4 s later.
"""

from __future__ import annotations

from unittest.mock import Mock

from tests.fra_quickdraft_states import state

import arenamcp.autopilot as autopilot_module
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine, AutopilotState


class _DummyBridge:
    connected = False

    def connect(self):
        return False


def _engine(monkeypatch) -> AutopilotEngine:
    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: _DummyBridge())
    planner = Mock()
    planner.plan_actions.side_effect = AssertionError("the window must not be planned again")
    planner._timeout = 5.0
    return AutopilotEngine(planner=planner, get_game_state=lambda: {}, config=AutopilotConfig(dry_run=True))


def test_window_key_names_the_combat_decision():
    window = state("G2_T18_ATTACK")
    key = AutopilotEngine._combat_window_key(window)
    assert key == (window["match_id"], 18, "Step_DeclareAttack", "declare_attackers")
    assert AutopilotEngine._combat_window_key(state("G2_T8_MAIN1")) is None
    assert AutopilotEngine._combat_window_key(None) is None


def test_a_manual_required_combat_window_is_not_replanned(monkeypatch):
    engine = _engine(monkeypatch)
    window = state("G2_T18_ATTACK")
    window["_bridge_game_state_id"] = 557
    engine._pause_for_manual("Bridge couldn't handle declare_attackers", window)
    assert engine._manual_window == AutopilotEngine._combat_window_key(window)

    # The re-captured request: same turn and step, a fresh bridge state id.
    again = state("G2_T18_ATTACK")
    again["_bridge_game_state_id"] = 558
    engine._given_up_semantics = None
    engine._given_up_window_sig = None
    assert engine.process_trigger(again, "decision_required") is True
    assert engine._state == AutopilotState.IDLE
    engine._planner.plan_actions.assert_not_called()


def test_the_next_combat_step_is_planned_normally(monkeypatch):
    engine = _engine(monkeypatch)
    engine._manual_window = AutopilotEngine._combat_window_key(state("G2_T18_ATTACK"))
    later = state("G2_T18_ATTACK")
    later["turn"]["turn_number"] = 20
    assert AutopilotEngine._combat_window_key(later) != engine._manual_window
