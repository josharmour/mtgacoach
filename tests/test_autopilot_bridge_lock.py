from types import SimpleNamespace

import arenamcp.autopilot as autopilot_module
from arenamcp.action_planner import ActionPlan, ActionType, GameAction
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine, AutopilotState
from arenamcp.input_controller import ClickResult


class _DummyPlanner:
    def __init__(self):
        self._timeout = 0.1
        self._backend = object()
        self.plan_calls = 0

    def plan_actions(self, *args, **kwargs) -> ActionPlan:
        self.plan_calls += 1
        return ActionPlan(actions=[], overall_strategy="noop")

    def get_recent_diagnostics(self) -> list[dict]:
        return []


class _DummyMapper:
    window_rect = (0, 0, 100, 100)
    cache_size = 0

    def refresh_window(self):
        return self.window_rect

    def get_button_coord(self, name: str):
        return None


class _DummyController:
    def focus_mtga_window(self) -> None:
        return None

    def wait(self, *args, **kwargs) -> None:
        return None

    def press_key(self, key: str, description: str) -> ClickResult:
        return ClickResult(True, 0, 0, description or key)

    def click(self, x: int, y: int, description: str, window_rect) -> ClickResult:
        return ClickResult(True, x, y, description)


class _DummyBridge:
    def __init__(self, pending_response: dict | None = None, connected: bool = True):
        self.connected = connected
        self.pending_response = pending_response or {"ok": True, "has_pending": False}

    def connect(self) -> bool:
        return self.connected

    def get_pending_actions(self) -> dict:
        return self.pending_response

    def auto_respond(self) -> bool:
        return False

    def submit_pass(self) -> bool:
        return False

    def cancel_action(self) -> bool:
        return False

    def submit_mulligan(self, keep: bool) -> bool:
        return False

    def submit_choose_starting_player(self, seat_id: int) -> bool:
        return False

    def submit_action_by_match(self, **kwargs) -> bool:
        return False

    def submit_action_by_index(self, *args, **kwargs) -> bool:
        return False

    def submit_attackers(self, attackers) -> bool:
        return False

    def submit_attackers_raw(self, attackers):
        return None

    def submit_blockers(self, assignments) -> bool:
        return False

    def submit_targets(self, target_instance_id: int) -> bool:
        return False

    def submit_selection(self, ids: list[int]) -> bool:
        return False


def _make_engine(monkeypatch, state_fn, bridge: _DummyBridge, notifications: list[str] | None = None) -> tuple[AutopilotEngine, _DummyPlanner]:
    planner = _DummyPlanner()
    mapper = _DummyMapper()
    controller = _DummyController()
    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: bridge)
    engine = AutopilotEngine(
        planner=planner,
        mapper=mapper,
        controller=controller,
        get_game_state=state_fn,
        config=AutopilotConfig(
            dry_run=True,
            verify_after_action=True,
            verification_timeout=0.01,
            post_action_delay=0.0,
        ),
        ui_advice_fn=(lambda text, label: notifications.append(f"{label}:{text}")) if notifications is not None else None,
    )
    return engine, planner


def test_get_game_state_clears_stale_pending_when_bridge_is_idle(monkeypatch):
    bridge = _DummyBridge({"ok": True, "has_pending": False})
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
    engine, _ = _make_engine(monkeypatch, lambda: dict(state), bridge)

    fresh = engine._get_game_state()

    assert fresh["pending_decision"] is None
    assert fresh["decision_context"] is None
    assert fresh["legal_actions"] == []
    assert fresh["_bridge_has_pending"] is False


def test_process_trigger_refuses_when_bridge_idle_and_no_log_data(monkeypatch):
    """When bridge says idle AND the trigger has no log-derived data, refuse."""
    bridge = _DummyBridge({"ok": True, "has_pending": False})
    engine, planner = _make_engine(monkeypatch, lambda: {}, bridge)

    handled = engine.process_trigger(
        {
            "turn": {"turn_number": 4, "active_player": 1, "priority_player": 1, "phase": "Phase_Main1", "step": "Step_PreCombatMain"},
            "players": [{"seat_id": 1, "is_local": True}],
            "_bridge_connected": True,
        },
        "priority_gained",
    )

    assert handled is False
    assert planner.plan_calls == 0
    assert engine.state == AutopilotState.IDLE


def test_process_trigger_refuses_when_bridge_idle_despite_log_data(monkeypatch):
    """Arbiter doctrine (fable-improvements.md item 4): a connected, idle
    bridge is authoritative; log-derived decisions are stale. The old
    behavior (proceed on log data) planned and spoke against ghost
    decisions the client had already consumed (live 2026-06-09)."""
    bridge = _DummyBridge({"ok": True, "has_pending": False})
    # Use dry_run so execution doesn't need real screen coordinates
    engine, planner = _make_engine(monkeypatch, lambda: {}, bridge)
    engine._config = AutopilotConfig(
        dry_run=True,
        verify_after_action=False,
        verification_timeout=0.01,
        post_action_delay=0.0,
    )

    handled = engine.process_trigger(
        {
            "turn": {"turn_number": 4, "active_player": 1, "priority_player": 1, "phase": "Phase_Main1", "step": "Step_PreCombatMain"},
            "players": [{"seat_id": 1, "is_local": True}],
            "_bridge_connected": True,
            "pending_decision": "Priority",
            "decision_context": {"type": "actions_available"},
        },
        "priority_gained",
    )

    assert handled is False
    assert planner.plan_calls == 0
    assert engine.state == AutopilotState.IDLE


def test_process_trigger_pauses_for_unmapped_bridge_interaction(monkeypatch):
    bridge = _DummyBridge(
        {
            "ok": True,
            "has_pending": True,
            "request_type": "MysteryReq",
            "request_class": "MysteryRequest",
        }
    )
    notifications: list[str] = []
    engine, planner = _make_engine(monkeypatch, lambda: {}, bridge, notifications)

    handled = engine.process_trigger(
        {
            "turn": {"turn_number": 4, "active_player": 1, "priority_player": 1, "phase": "Phase_Main1", "step": "Step_PreCombatMain"},
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


class _RecordingController:
    """Spy controller that records every input call so we can assert no mouse use."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def focus_mtga_window(self) -> None:
        self.calls.append("focus_mtga_window")
        return None

    def wait(self, *args, **kwargs) -> None:
        return None

    def press_key(self, key: str, description: str) -> ClickResult:
        self.calls.append(f"press_key:{key}")
        return ClickResult(True, 0, 0, description or key)

    def click(self, x: int, y: int, description: str, window_rect) -> ClickResult:
        self.calls.append(f"click:{description}")
        return ClickResult(True, x, y, description)

    def double_click(self, x: int, y: int, description: str, window_rect) -> ClickResult:
        self.calls.append(f"double_click:{description}")
        return ClickResult(True, x, y, description)

    def click_card_in_hand(self, *args, **kwargs) -> ClickResult:
        self.calls.append("click_card_in_hand")
        return ClickResult(True, 0, 0, "card")


def _make_bridge_only_engine(monkeypatch, bridge: _DummyBridge):
    """Build an engine in real bridge-only mode (dry_run=False)."""
    planner = _DummyPlanner()
    mapper = _DummyMapper()
    controller = _RecordingController()
    notifications: list[str] = []
    bug_calls: list[tuple[str, dict]] = []

    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: bridge)

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

    engine = AutopilotEngine(
        planner=planner,
        mapper=mapper,
        controller=controller,
        get_game_state=lambda: {},
        config=AutopilotConfig(
            dry_run=False,
            bridge_only_when_connected=True,
            verify_after_action=False,
            verification_timeout=0.01,
            post_action_delay=0.0,
        ),
        ui_advice_fn=lambda text, label: notifications.append(f"{label}:{text}"),
        bug_report_fn=lambda reason, extra: bug_calls.append((reason, extra)),
    )
    return engine, controller, notifications, bug_calls


def test_execute_action_no_mouse_when_bridge_unavailable(monkeypatch):
    """Bridge disconnected → manual required + bug report, no mouse fallback."""
    bridge = _DummyBridge(connected=False)
    engine, controller, notifications, bug_calls = _make_bridge_only_engine(
        monkeypatch, bridge
    )

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
    assert controller.calls == [], f"expected zero mouse calls, got {controller.calls}"
    assert any("MANUAL REQUIRED" in n for n in notifications)
    assert len(bug_calls) == 1
    reason, extra = bug_calls[0]
    assert "bridge_unavailable" in reason
    assert extra["auto_fallback_bug"]["reason_tag"] == "bridge_unavailable"
    assert extra["auto_fallback_bug"]["card_name"] == "Forest"


def test_execute_action_no_mouse_when_bridge_submit_fails(monkeypatch):
    """Bridge connected but submit returns False → manual required + bug report."""
    bridge = _DummyBridge(
        {
            "ok": True,
            "has_pending": True,
            "request_type": "ActionsAvailableReq",
            "request_class": "ActionsAvailableRequest",
        },
        connected=True,
    )
    engine, controller, notifications, bug_calls = _make_bridge_only_engine(
        monkeypatch, bridge
    )

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
    assert controller.calls == [], f"expected zero mouse calls, got {controller.calls}"
    assert any("MANUAL REQUIRED" in n for n in notifications)
    assert len(bug_calls) == 1
    reason, extra = bug_calls[0]
    assert "bridge_submit_failed" in reason
    assert extra["auto_fallback_bug"]["reason_tag"] == "bridge_submit_failed"
    assert extra["auto_fallback_bug"]["card_name"] == "Shock"
    assert extra["auto_fallback_bug"]["bridge"]["connected"] is True


def test_execute_action_classifies_stale_play_land_as_planner_action_stale(monkeypatch):
    """Planner picks play_land but bridge has no Play action → stale state.

    Should still surface MANUAL REQUIRED, but must NOT auto-file a bug —
    this is a self-inflicted state mismatch (lands_played already 1, etc.),
    not a real bridge failure. See issues #136 #137 #139 #140.
    """
    bridge = _DummyBridge(
        {
            "ok": True,
            "has_pending": True,
            "request_type": "ActionsAvailableReq",
            "request_class": "ActionsAvailableRequest",
        },
        connected=True,
    )
    engine, controller, notifications, bug_calls = _make_bridge_only_engine(
        monkeypatch, bridge
    )

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

    assert result.success is False
    assert controller.calls == [], f"expected zero mouse calls, got {controller.calls}"
    assert any("MANUAL REQUIRED" in n for n in notifications)
    # Critical: no auto-bug filed for stale state mismatches
    assert bug_calls == [], (
        f"expected no auto-bug for planner_action_stale, got {bug_calls}"
    )


def test_execute_action_files_bug_for_real_submit_failure_with_play_action_present(monkeypatch):
    """Same shape as above but bridge HAS a Play action → real submit failure.

    Confirms the stale-vs-real classifier doesn't accidentally suppress
    legitimate bridge bugs.
    """
    bridge = _DummyBridge(
        {
            "ok": True,
            "has_pending": True,
            "request_type": "ActionsAvailableReq",
            "request_class": "ActionsAvailableRequest",
        },
        connected=True,
    )
    engine, controller, notifications, bug_calls = _make_bridge_only_engine(
        monkeypatch, bridge
    )

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
    bridge = _DummyBridge(
        {
            "ok": True,
            "has_pending": True,
            "request_type": "ActionsAvailableReq",
            "request_class": "ActionsAvailableRequest",
            "actions": [],
        }
    )
    state = {
        "turn": {"turn_number": 5, "active_player": 1, "priority_player": 1, "phase": "Phase_Main1", "step": "Step_PreCombatMain"},
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
    engine, _ = _make_engine(monkeypatch, lambda: dict(state), bridge)
    action = GameAction(
        action_type=ActionType.CAST_SPELL,
        card_name="Shock",
        gre_action_ref=SimpleNamespace(instance_id=9001, grp_id=1003, ability_grp_id=0),
    )

    verified = engine._verify_action(action, dict(state))

    assert verified is False
    assert engine._is_action_blocked(action, state) is True
