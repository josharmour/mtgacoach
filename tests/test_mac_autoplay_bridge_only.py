"""Native macOS autoplay is bridge-only (2026-10-18).

The screenshot-and-click gameplay engine is gone. On a native Mac without the
injected bridge library the coach still starts the GRE autopilot, which hands
each window back to the player (MANUAL REQUIRED) with the relaunch hint, while
the coach keeps advising from the log.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionType, GameAction
from arenamcp.autopilot import AutopilotEngine
from arenamcp.autopilot_models import AutopilotState
from arenamcp.standalone import StandaloneCoach


class _OfflineBridge:
    connected = False

    def connect(self) -> bool:
        return False


@pytest.fixture
def native_mac_without_bridge(monkeypatch):
    monkeypatch.setenv("MTGACOACH_GAME_DEVICE", "desktop")
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("arenamcp.platform_integration.bridge_capable", lambda: False)
    monkeypatch.setattr("arenamcp.platform_integration.mac_bridge_installed", lambda: False)
    monkeypatch.setattr("arenamcp.gre_bridge._bridge", None)  # no port-held hint
    monkeypatch.setattr("arenamcp.autopilot.get_bridge", _OfflineBridge)
    monkeypatch.setattr(
        "arenamcp.native_mac_input.NativeMacInput", Mock(side_effect=AssertionError("no screen input"))
    )


def _coach(monkeypatch) -> tuple[StandaloneCoach, Mock, list[str]]:
    backend_factory = Mock(return_value=Mock(name="backend"))
    monkeypatch.setattr("arenamcp.coach.create_backend", backend_factory)
    coach = StandaloneCoach.__new__(StandaloneCoach)
    coach._mcp = SimpleNamespace(get_game_state=lambda: {}, poll_log=lambda: None)
    coach._coach = SimpleNamespace(_ensure_game_plan_mgr=lambda: None)
    notices: list[str] = []
    coach.ui = Mock()
    coach.ui.advice.side_effect = lambda text, label: notices.append(f"{label}:{text}")
    coach.settings = {}
    coach._backend_name = "online"
    coach._model_name = "text-model"
    coach._autopilot = None
    coach._autopilot_enabled = False
    coach._autopilot_dry_run = False
    coach._autopilot_afk = False
    # Never file a real bug report or send an emote from a test.
    coach._auto_bug_report_bridge_fallback = Mock()
    coach._oops_report = Mock()
    coach.speak_advice = Mock()
    return coach, backend_factory, notices


def test_native_mac_without_bridge_starts_the_gre_engine(monkeypatch, native_mac_without_bridge):
    coach, backend_factory, _ = _coach(monkeypatch)
    coach._init_autopilot()
    assert type(coach._autopilot) is AutopilotEngine
    # One text-model backend: no separate vision model or endpoint.
    backend_factory.assert_called_once_with("online", model="text-model")
    assert any("Autopilot initialized (GRE bridge)" in call.args[0] for call in coach.ui.log.call_args_list)


def test_native_mac_without_bridge_hands_the_window_back_and_keeps_coaching(
    monkeypatch, native_mac_without_bridge
):
    coach, _, notices = _coach(monkeypatch)
    coach._init_autopilot()
    engine = coach._autopilot
    engine._config.bridge_reconnect_wait = 0
    coach._autopilot_enabled = True
    state = {
        "match_id": "m",
        "turn": {"turn_number": 3, "phase": "Phase_Main1", "step": "Step_PreCombatMain"},
        "players": [{"seat_id": 1, "is_local": True}],
        "pending_decision": "Priority",
    }

    result = engine._execute_action(GameAction(action_type=ActionType.PLAY_LAND, card_name="Forest"), state)

    assert not result.success
    manual = [notice for notice in notices if "MANUAL REQUIRED" in notice]
    assert manual, notices
    assert "Bridge offline." in manual[-1]
    assert "quit MTGA and reopen the coach" in manual[-1]
    assert "DYLD_INSERT_LIBRARIES" in manual[-1]
    assert engine.state == AutopilotState.PAUSED
    # The window is the player's, so the coach advises it with the model.
    assert coach._autopilot_drives(state) is False


def test_control_status_reports_off_paused_and_on():
    coach = StandaloneCoach.__new__(StandaloneCoach)
    coach._autopilot = SimpleNamespace(state=AutopilotState.IDLE)
    coach._autopilot_enabled = False
    assert coach._autopilot_control_status() == "AP:OFF"
    coach._autopilot_enabled = True
    assert coach._autopilot_control_status() == "AP:ON"
    coach._autopilot.state = AutopilotState.PAUSED
    assert coach._autopilot_control_status() == "AP:PAUSED"
