"""Native Mac autoplay decides from the logs; vision only operates the committed play.

Live 2026-09-20 22:33:54: the vision model, deciding from pixels, proposed
"Play a Plains" and double-clicked Spellbook Vendor. The Player.log already
knew the legal, payable actions — the play is now chosen there.
"""

from __future__ import annotations

import json
import time
from unittest.mock import Mock

from PIL import Image

from arenamcp.action_planner import ActionPlan, ActionType, GameAction
from arenamcp.autopilot_models import AutopilotConfig, AutopilotState
from arenamcp.native_mac_autopilot import NativeMacAutopilot
from arenamcp.native_mac_input import DesktopFrame, GameWindow

LEGAL = ["Cast Optimistic Scavenger [OK]", "Play Land: Plains", "Pass"]


def _frame():
    return DesktopFrame(GameWindow(42, 123, (100, 50, 720, 450)), Image.new("RGB", (1440, 900), "green"), time.monotonic())


def _engine(monkeypatch, planned: GameAction | None, *, state=None, config=None, planner_error=None):
    monkeypatch.setattr("arenamcp.gre_bridge.get_bridge", Mock(side_effect=AssertionError("No bridge allowed")))
    state = state if state is not None else {"match_id": "m", "legal_actions": list(LEGAL)}
    controller = Mock()
    controller.capture.side_effect = lambda: _frame()
    controller.execute.return_value = True
    backend = Mock()
    backend.complete_with_image.return_value = json.dumps(
        {"kind": "double_click", "point": [0.4, 0.8], "reason": "Cast Optimistic Scavenger", "confidence": 0.95}
    )
    planner = Mock()
    if planner_error:
        planner.plan_actions.side_effect = planner_error
    else:
        planner.plan_actions.return_value = ActionPlan(actions=[planned] if planned else [])
    engine = NativeMacAutopilot(
        backend=backend,
        controller=controller,
        get_game_state=lambda: state,
        config=config or AutopilotConfig(),
        planner=planner,
    )
    engine._ground_action = Mock(side_effect=lambda frame, action: action)
    return engine, controller, backend, planner, state


def _prompt(backend) -> dict:
    return json.loads(backend.complete_with_image.call_args.args[1])


def test_committed_play_is_sent_to_vision(monkeypatch):
    play = GameAction(ActionType.CAST_SPELL, card_name="Optimistic Scavenger")
    engine, controller, backend, planner, state = _engine(monkeypatch, play)
    assert engine.process_trigger(state, "desktop_poll")
    assert "Optimistic Scavenger" in _prompt(backend)["committed_play"]
    planner.plan_actions.assert_called_once()
    assert planner.plan_actions.call_args.args[2] == LEGAL
    controller.execute.assert_called_once()


def test_planned_pass_uses_space_without_vision(monkeypatch):
    engine, controller, backend, _, state = _engine(monkeypatch, GameAction(ActionType.PASS_PRIORITY))
    assert engine.process_trigger(state, "desktop_poll")
    backend.complete_with_image.assert_not_called()
    action = controller.execute.call_args.args[1]
    assert action.kind == "key" and action.key == "space"


def test_plain_priority_context_still_uses_space(monkeypatch):
    # Real priority windows carry {"type": "actions_available"} (45/105 bug
    # reports); the 2026-09-22 live session sent all three planned passes to
    # vision because of it.
    state = {"match_id": "m", "legal_actions": list(LEGAL),
             "decision_context": {"type": "actions_available", "num_actions": 3}}
    engine, controller, backend, _, _ = _engine(monkeypatch, GameAction(ActionType.PASS_PRIORITY), state=state)
    assert engine.process_trigger(state, "desktop_poll")
    backend.complete_with_image.assert_not_called()
    assert controller.execute.call_args.args[1].key == "space"


def test_pass_inside_a_decision_dialog_still_uses_vision(monkeypatch):
    state = {"match_id": "m", "legal_actions": list(LEGAL), "decision_context": {"type": "select_targets"}}
    engine, controller, backend, _, _ = _engine(monkeypatch, GameAction(ActionType.PASS_PRIORITY), state=state)
    engine.process_trigger(state, "desktop_poll")
    backend.complete_with_image.assert_called_once()


def test_plan_is_reused_within_one_decision_window(monkeypatch):
    play = GameAction(ActionType.CAST_SPELL, card_name="Optimistic Scavenger")
    engine, _, _, planner, state = _engine(monkeypatch, play)
    engine.process_trigger(state, "desktop_poll")
    engine._next_poll = 0
    engine.process_trigger(state, "desktop_poll")
    assert planner.plan_actions.call_count == 1
    state["legal_actions"] = ["Pass"]
    engine._next_poll = 0
    engine.process_trigger(state, "desktop_poll")
    assert planner.plan_actions.call_count == 2


def test_planner_failure_falls_back_to_vision_decision(monkeypatch):
    engine, controller, backend, _, state = _engine(monkeypatch, None, planner_error=RuntimeError("gateway down"))
    assert engine.process_trigger(state, "desktop_poll")
    assert "committed_play" not in _prompt(backend)
    controller.execute.assert_called_once()


def test_no_legal_actions_skips_planner(monkeypatch):
    state = {"match_id": "m", "legal_actions": [], "turn": {"priority_player": 2, "active_player": 2},
             "players": [{"seat_id": 1, "is_local": True}]}
    engine, _, _, planner, _ = _engine(monkeypatch, GameAction(ActionType.PASS_PRIORITY), state=state)
    engine.process_trigger(state, "desktop_poll")
    planner.plan_actions.assert_not_called()


def test_afk_mode_ignores_non_pass_commitments(monkeypatch):
    play = GameAction(ActionType.CAST_SPELL, card_name="Optimistic Scavenger")
    engine, _, backend, _, state = _engine(monkeypatch, play, config=AutopilotConfig(afk_mode=True))
    engine.process_trigger(state, "desktop_poll")
    assert "committed_play" not in _prompt(backend)


def test_land_only_mode_keeps_land_commitment(monkeypatch):
    engine, _, backend, _, state = _engine(
        monkeypatch, GameAction(ActionType.PLAY_LAND, card_name="Plains"), config=AutopilotConfig(land_drop_mode=True)
    )
    engine.process_trigger(state, "desktop_poll")
    assert "Plains" in _prompt(backend)["committed_play"]


def test_repeated_space_that_does_not_advance_pauses(monkeypatch):
    engine, controller, _, _, state = _engine(monkeypatch, GameAction(ActionType.PASS_PRIORITY))
    for _ in range(4):
        engine._next_poll = 0
        engine.process_trigger(state, "desktop_poll")
    assert controller.execute.call_count == 3
    assert engine.state == AutopilotState.PAUSED
