from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests.fakes import FakeBridge, make_engine

import arenamcp.autopilot as autopilot_module
from arenamcp.action_planner import ActionType, GameAction
from arenamcp.autopilot import AutopilotState
from arenamcp.autopilot_models import ClickResult


def _make_engine(state_fn, bridge: FakeBridge, notifications: list[str] | None = None):
    engine = make_engine(
        bridge,
        game_state=state_fn,
        dry_run=True,
        verification_timeout=0.01,
        post_action_delay=0.0,
        ui_advice_fn=(lambda text, label: notifications.append(f"{label}:{text}"))
        if notifications is not None
        else None,
    )
    return engine, engine._planner


@pytest.mark.parametrize(
    ("request_type", "option_id", "submit_method", "submit_args"),
    [
        ("Mulligan", "mull:keep", "submit_mulligan", (True,)),
        ("ActionsAvailable", "pass", "submit_pass", ()),
    ],
)
def test_process_trigger_submits_live_bridge_decision(
    monkeypatch, request_type, option_id, submit_method, submit_args
):
    pending = {
        "ok": True,
        "has_pending": True,
        "request_type": request_type,
        "actions": [],
        "can_pass": request_type == "ActionsAvailable",
    }
    bridge = FakeBridge(pending)
    submit = Mock(return_value=True)
    monkeypatch.setattr(bridge, submit_method, submit)
    state = {
        "_bridge_connected": True,
        "_bridge_has_pending": True,
        "_bridge_request_type": request_type,
        "pending_decision": request_type,
        "turn": {"turn_number": 1},
        "players": [{"seat_id": 1, "is_local": True}],
    }
    engine, planner = _make_engine(lambda: dict(state), bridge)
    engine._config.dry_run = False
    planner.plan_decision_options = Mock(return_value=[option_id])

    assert engine.process_trigger(state, "decision_required") is True

    submit.assert_called_once_with(*submit_args)
    assert engine._actions_executed == 1
    assert engine._state == AutopilotState.IDLE
    assert not engine._lock.locked()


def test_get_game_state_clears_stale_pending_when_bridge_is_idle(monkeypatch):
    bridge = FakeBridge({"ok": True, "has_pending": False})
    state = {
        "turn": {"turn_number": 3, "phase": "Phase_Main1", "step": "Step_PreCombatMain"},
        "players": [{"seat_id": 1, "is_local": True}],
        "pending_decision": "Select Targets",
        "decision_context": {"type": "target_selection"},
        "legal_actions": ["Select target: Alpha Myr"],
        "legal_actions_raw": [{"actionType": "ActionType_SelectTarget"}],
        "_bridge_connected": True,
        "_bridge_game_state_id": 77,
    }
    engine, _ = _make_engine(lambda: dict(state), bridge)

    fresh = engine._get_game_state()

    assert fresh["pending_decision"] is None
    assert fresh["decision_context"] is None
    assert fresh["legal_actions"] == []
    assert fresh["_bridge_has_pending"] is False


@pytest.mark.parametrize("submitted", [True, False])
def test_empty_planner_pass_returns_bridge_submission_result(monkeypatch, submitted):
    bridge = FakeBridge({"ok": True, "has_pending": True, "request_type": "ActionsAvailable"})
    state = {
        "_bridge_connected": True,
        "_bridge_has_pending": True,
        "_bridge_request_type": "ActionsAvailable",
        "turn": {"turn_number": 1},
    }
    engine, planner = _make_engine(lambda: dict(state), bridge)
    engine._config.dry_run = False
    monkeypatch.setattr(engine, "_try_typed_decision_path", lambda *args: None)
    execute = Mock(return_value=ClickResult(submitted))
    monkeypatch.setattr(engine, "_execute_action", execute)

    assert engine.process_trigger(state, "decision_required") is submitted

    assert planner.plan_calls == 1
    assert execute.call_args.args[0].action_type == ActionType.PASS_PRIORITY
    assert not engine._lock.locked()


@pytest.mark.parametrize(
    "log_decision",
    [{}, {"pending_decision": "Priority", "decision_context": {"type": "actions_available"}}],
    ids=["no_log_data", "despite_log_data"],
)
def test_process_trigger_refuses_when_bridge_idle(log_decision):
    """Arbiter doctrine (fable-improvements.md item 4): a connected, idle
    bridge is authoritative; log-derived decisions are stale. The old
    behavior (proceed on log data) planned and spoke against ghost
    decisions the client had already consumed (live 2026-06-09)."""
    bridge = FakeBridge({"ok": True, "has_pending": False})
    engine, planner = _make_engine(lambda: {}, bridge)

    handled = engine.process_trigger(
        {
            "turn": {
                "turn_number": 4,
                "active_player": 1,
                "priority_player": 1,
                "phase": "Phase_Main1",
                "step": "Step_PreCombatMain",
            },
            "players": [{"seat_id": 1, "is_local": True}],
            "_bridge_connected": True,
            **log_decision,
        },
        "priority_gained",
    )

    # Consumed as stale: suppress fallback planning against the idle bridge.
    assert handled is True
    assert planner.plan_calls == 0
    assert engine.state == AutopilotState.IDLE


def test_process_trigger_pauses_for_unmapped_bridge_interaction(monkeypatch):
    bridge = FakeBridge(
        {
            "ok": True,
            "has_pending": True,
            "request_type": "MysteryReq",
            "request_class": "MysteryRequest",
        }
    )
    notifications: list[str] = []
    engine, planner = _make_engine(lambda: {}, bridge, notifications)

    handled = engine.process_trigger(
        {
            "turn": {
                "turn_number": 4,
                "active_player": 1,
                "priority_player": 1,
                "phase": "Phase_Main1",
                "step": "Step_PreCombatMain",
            },
            "players": [{"seat_id": 1, "is_local": True}],
            "_bridge_connected": True,
            "_bridge_request_type": "MysteryReq",
            "_bridge_request_class": "MysteryRequest",
            "pending_decision": "Manual Required",
            "decision_context": {"type": "unmapped_interaction"},
        },
        "decision_required",
    )

    assert handled is False
    assert planner.plan_calls == 0
    assert engine.state == AutopilotState.PAUSED
    assert any("MANUAL REQUIRED" in item for item in notifications)


def _make_bridge_only_engine(monkeypatch, bridge: FakeBridge):
    """Build an engine in real bridge-only mode (dry_run=False)."""
    notifications: list[str] = []
    bug_calls: list[tuple[str, dict]] = []

    # Run the dispatched bug-report thread synchronously so assertions are
    # deterministic. The autopilot dispatches via threading.Thread(...).start().
    class _SyncThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}

        def start(self) -> None:
            if self._target is not None:
                self._target(*self._args, **self._kwargs)

    monkeypatch.setattr(autopilot_module.threading, "Thread", _SyncThread)

    engine = make_engine(
        bridge,
        verify_after_action=False,
        verification_timeout=0.01,
        post_action_delay=0.0,
        ui_advice_fn=lambda text, label: notifications.append(f"{label}:{text}"),
        bug_report_fn=lambda reason, extra: bug_calls.append((reason, extra)),
    )
    return engine, notifications, bug_calls


def test_unavailable_bridge_is_manual_required_with_a_bug_report(monkeypatch):
    """Bridge disconnected → MANUAL REQUIRED and an auto-filed bridge bug."""
    bridge = FakeBridge(connected=False)
    engine, notifications, bug_calls = _make_bridge_only_engine(monkeypatch, bridge)

    state = {
        "turn": {"turn_number": 3, "phase": "Phase_Main1", "step": "Step_PreCombatMain"},
        "players": [{"seat_id": 1, "is_local": True}],
        "_bridge_connected": False,
    }
    action = GameAction(
        action_type=ActionType.PLAY_LAND,
        card_name="Forest",
    )

    result = engine._execute_action(action, state)

    assert result.success is False
    assert any("MANUAL REQUIRED" in n for n in notifications)
    assert len(bug_calls) == 1
    reason, extra = bug_calls[0]
    assert "bridge_unavailable" in reason
    assert extra["auto_fallback_bug"]["reason_tag"] == "bridge_unavailable"
    assert extra["auto_fallback_bug"]["card_name"] == "Forest"


def test_failed_bridge_submit_is_manual_required_with_a_bug_report(monkeypatch):
    """Bridge connected but submit returns False → MANUAL REQUIRED + bug report."""
    bridge = FakeBridge(
        {
            "ok": True,
            "has_pending": True,
            "request_type": "ActionsAvailableReq",
            "request_class": "ActionsAvailableRequest",
        },
        connected=True,
    )
    engine, notifications, bug_calls = _make_bridge_only_engine(monkeypatch, bridge)

    state = {
        "turn": {"turn_number": 3, "phase": "Phase_Main1", "step": "Step_PreCombatMain"},
        "players": [{"seat_id": 1, "is_local": True}],
        "_bridge_connected": True,
        "_bridge_request_type": "ActionsAvailableReq",
        "_bridge_request_class": "ActionsAvailableRequest",
        "_bridge_has_pending": True,
    }
    action = GameAction(
        action_type=ActionType.CAST_SPELL,
        card_name="Shock",
        gre_action_ref=SimpleNamespace(
            instance_id=9001, grp_id=1003, ability_grp_id=0, action_type="ActionType_Cast"
        ),
    )

    result = engine._execute_action(action, state)

    assert result.success is False
    assert any("MANUAL REQUIRED" in n for n in notifications)
    assert len(bug_calls) == 1
    reason, extra = bug_calls[0]
    assert "bridge_submit_failed" in reason
    assert extra["auto_fallback_bug"]["reason_tag"] == "bridge_submit_failed"
    assert extra["auto_fallback_bug"]["card_name"] == "Shock"
    assert extra["auto_fallback_bug"]["bridge"]["connected"] is True


def test_execute_action_classifies_stale_play_land_as_planner_action_stale(monkeypatch):
    """Planner picks play_land but bridge has no Play action → stale state.

    Skipped for a re-plan rather than MANUAL REQUIRED (2026-09-24: that
    escalation auto-passed priority and threw away main phase 1), and never
    an auto-filed bug — a self-inflicted state mismatch (issues #136 #137
    #139 #140). The plan loop blocks the skipped play for the window, so
    planning it again hits "Blocked action repeated" instead of looping.
    """
    bridge = FakeBridge(
        {
            "ok": True,
            "has_pending": True,
            "request_type": "ActionsAvailableReq",
            "request_class": "ActionsAvailableRequest",
        },
        connected=True,
    )
    engine, notifications, bug_calls = _make_bridge_only_engine(monkeypatch, bridge)

    # Bridge has 6 actions — none of type "Play". This is the post-land-drop
    # state where the user already played their land for the turn.
    state = {
        "turn": {"turn_number": 3, "phase": "Phase_Main1", "step": "Step_PreCombatMain"},
        "players": [{"seat_id": 1, "is_local": True}],
        "_bridge_connected": True,
        "_bridge_request_type": "ActionsAvailable",
        "_bridge_request_class": "ActionsAvailableRequest",
        "_bridge_has_pending": True,
        "_bridge_actions": [
            {"actionType": "Cast", "grpId": 1001, "instanceId": 100},
            {"actionType": "Activate_Mana", "instanceId": 200},
            {"actionType": "Activate_Mana", "instanceId": 201},
            {"actionType": "Pass"},
            {"actionType": "FloatMana"},
            {"actionType": "Activate", "grpId": 2002, "instanceId": 300},
        ],
    }
    action = GameAction(
        action_type=ActionType.PLAY_LAND,
        card_name="Forest",
    )

    result = engine._execute_action(action, state)

    assert result.success is True
    assert "stale-skip" in result.error
    assert not any("MANUAL REQUIRED" in n for n in notifications)
    # Critical: no auto-bug filed for stale state mismatches
    assert bug_calls == [], f"expected no auto-bug for planner_action_stale, got {bug_calls}"


def test_execute_action_files_bug_for_real_submit_failure_with_play_action_present(monkeypatch):
    """Same shape as above but bridge HAS a Play action → real submit failure.

    Confirms the stale-vs-real classifier doesn't accidentally suppress
    legitimate bridge bugs.
    """
    bridge = FakeBridge(
        {
            "ok": True,
            "has_pending": True,
            "request_type": "ActionsAvailableReq",
            "request_class": "ActionsAvailableRequest",
        },
        connected=True,
    )
    engine, notifications, bug_calls = _make_bridge_only_engine(monkeypatch, bridge)

    state = {
        "turn": {"turn_number": 3, "phase": "Phase_Main1", "step": "Step_PreCombatMain"},
        "players": [{"seat_id": 1, "is_local": True}],
        "_bridge_connected": True,
        "_bridge_request_type": "ActionsAvailable",
        "_bridge_request_class": "ActionsAvailableRequest",
        "_bridge_has_pending": True,
        # Bridge HAS a Play action — so a play_land miss here is a real
        # bridge problem, not a stale-state mismatch.
        "_bridge_actions": [
            {"actionType": "Play", "grpId": 100652, "instanceId": 165},
            {"actionType": "Pass"},
        ],
    }
    action = GameAction(
        action_type=ActionType.PLAY_LAND,
        card_name="Forest",
    )

    result = engine._execute_action(action, state)

    assert result.success is False
    assert any("MANUAL REQUIRED" in n for n in notifications)
    assert len(bug_calls) == 1
    reason, extra = bug_calls[0]
    assert "bridge_submit_failed" in reason
    assert extra["auto_fallback_bug"]["reason_tag"] == "bridge_submit_failed"


def test_verify_action_blocks_repeated_bridge_action_when_state_id_stalls(monkeypatch):
    bridge = FakeBridge(
        {
            "ok": True,
            "has_pending": True,
            "request_type": "ActionsAvailableReq",
            "request_class": "ActionsAvailableRequest",
            "actions": [],
        }
    )
    state = {
        "turn": {
            "turn_number": 5,
            "active_player": 1,
            "priority_player": 1,
            "phase": "Phase_Main1",
            "step": "Step_PreCombatMain",
        },
        "players": [{"seat_id": 1, "is_local": True}],
        "battlefield": [],
        "hand": [{"instance_id": 9001, "name": "Shock"}],
        "stack": [],
        "pending_decision": "Priority",
        "decision_context": {"type": "actions_available"},
        "_bridge_connected": True,
        "_bridge_game_state_id": 123,
        "_bridge_request_type": "ActionsAvailableReq",
        "_bridge_request_class": "ActionsAvailableRequest",
    }
    engine, _ = _make_engine(lambda: dict(state), bridge)
    action = GameAction(
        action_type=ActionType.CAST_SPELL,
        card_name="Shock",
        gre_action_ref=SimpleNamespace(instance_id=9001, grp_id=1003, ability_grp_id=0),
    )

    verified = engine._verify_action(action, dict(state))

    assert verified is False
    assert engine._is_action_blocked(action, state) is True
