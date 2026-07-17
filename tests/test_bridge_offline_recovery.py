"""Bridge-offline recovery: autopilot waits for the plugin to reconnect
instead of cascading every action into MANUAL REQUIRED, and the bridge
warns once (actionably) when no plugin ever connects.

Live failure 2026-06-07 21:46: BepInEx wasn't injected (Steam launch
options lost WINEDLLOVERRIDES), the bridge stayed offline all match, and
every autopilot action failed "Bridge offline" with zero guidance.
"""

import logging
import time

import arenamcp.autopilot as autopilot_module
from arenamcp.action_planner import ActionType, GameAction
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine
from arenamcp.gre_bridge import GREBridge
from arenamcp.input_controller import ClickResult


class _FlippingBridge:
    """Disconnected bridge whose connect() succeeds after N attempts."""

    def __init__(self, succeed_after: int):
        self.connected = False
        self.connect_calls = 0
        self._succeed_after = succeed_after

    def connect(self) -> bool:
        self.connect_calls += 1
        if self.connect_calls >= self._succeed_after:
            self.connected = True
        return self.connected


class _DummyPlanner:
    _timeout = 0.1
    _backend = object()

    def plan_actions(self, *args, **kwargs):
        raise AssertionError("planner should not be called")

    def get_recent_diagnostics(self):
        return []


class _DummyMapper:
    window_rect = (0, 0, 100, 100)
    cache_size = 0

    def refresh_window(self):
        return self.window_rect

    def get_button_coord(self, name):
        return None


class _DummyController:
    def focus_mtga_window(self):
        return None


def _make_engine(monkeypatch, bridge, **config_overrides) -> AutopilotEngine:
    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: bridge)
    config_kwargs = {
        "dry_run": False,
        "bridge_only_when_connected": True,
        "bridge_reconnect_wait": 1.0,
        "bridge_reconnect_wait_cooldown": 60.0,
        "post_action_delay": 0.0,
    }
    config_kwargs.update(config_overrides)
    config = AutopilotConfig(**config_kwargs)
    return AutopilotEngine(
        planner=_DummyPlanner(),
        mapper=_DummyMapper(),
        controller=_DummyController(),
        get_game_state=lambda: {},
        config=config,
    )


def test_wait_succeeds_when_plugin_reconnects(monkeypatch):
    bridge = _FlippingBridge(succeed_after=2)
    engine = _make_engine(monkeypatch, bridge)
    assert engine._wait_for_bridge_reconnect() is True
    assert bridge.connected


def test_wait_cooldown_prevents_stacked_waits(monkeypatch):
    bridge = _FlippingBridge(succeed_after=10_000)  # never connects
    engine = _make_engine(monkeypatch, bridge, bridge_reconnect_wait=0.3)
    start = time.monotonic()
    assert engine._wait_for_bridge_reconnect() is False
    first_duration = time.monotonic() - start
    assert first_duration >= 0.25

    # Second call must bail immediately — a dead plugin shouldn't add
    # seconds of latency to every subsequent action.
    start = time.monotonic()
    assert engine._wait_for_bridge_reconnect() is False
    assert time.monotonic() - start < 0.1


def test_wait_disabled_in_dry_run(monkeypatch):
    bridge = _FlippingBridge(succeed_after=1)
    engine = _make_engine(monkeypatch, bridge, dry_run=True)
    assert engine._wait_for_bridge_reconnect() is False
    assert bridge.connect_calls == 0


def test_execute_action_retries_bridge_after_reconnect(monkeypatch):
    bridge = _FlippingBridge(succeed_after=10_000)
    engine = _make_engine(monkeypatch, bridge)

    attempts = []

    def fake_try_gre_bridge(action, game_state):
        attempts.append(action.action_type)
        if len(attempts) == 1:
            return None  # first try: bridge offline
        return ClickResult(True, 0, 0, "pass", "GRE bridge")

    monkeypatch.setattr(engine, "_try_gre_bridge", fake_try_gre_bridge)
    monkeypatch.setattr(engine, "_wait_for_bridge_reconnect", lambda: True)

    action = GameAction(action_type=ActionType.PASS_PRIORITY)
    result = engine._execute_action(action, {})
    assert result.success
    assert len(attempts) == 2


def test_no_plugin_warning_fires_once(caplog, monkeypatch):
    # Pin a bridge-capable install: on a native Mac (bridge_capable False)
    # the warning is intentionally replaced by an informational log-mode line.
    monkeypatch.setattr(
        "arenamcp.platform_integration.bridge_capable", lambda: True
    )
    bridge = GREBridge()
    bridge._server_socket = object()
    bridge._server_started_at = time.monotonic() - 60.0
    with caplog.at_level(logging.WARNING, logger="arenamcp.gre_bridge"):
        bridge._maybe_warn_no_plugin()
        bridge._maybe_warn_no_plugin()
    warnings = [
        r for r in caplog.records if "plugin is not loading" in r.getMessage()
    ]
    assert len(warnings) == 1


def test_no_plugin_log_mode_instead_of_warning_when_bridge_impossible(
    caplog, monkeypatch
):
    monkeypatch.setattr(
        "arenamcp.platform_integration.bridge_capable", lambda: False
    )
    bridge = GREBridge()
    bridge._server_socket = object()
    bridge._server_started_at = time.monotonic() - 60.0
    with caplog.at_level(logging.INFO, logger="arenamcp.gre_bridge"):
        bridge._maybe_warn_no_plugin()
    assert not any(
        "plugin is not loading" in r.getMessage()
        for r in caplog.records if r.levelno >= logging.WARNING
    )
    assert any("log-only" in r.getMessage() for r in caplog.records)


def test_no_plugin_warning_suppressed_after_any_connection(caplog):
    bridge = GREBridge()
    bridge._server_socket = object()
    bridge._server_started_at = time.monotonic() - 60.0
    bridge._ever_connected = True
    with caplog.at_level(logging.WARNING, logger="arenamcp.gre_bridge"):
        bridge._maybe_warn_no_plugin()
    assert not bridge._no_plugin_warned
    assert not any(
        "plugin is not loading" in r.getMessage() for r in caplog.records
    )


def test_no_plugin_warning_waits_for_grace_period(caplog):
    bridge = GREBridge()
    bridge._server_socket = object()
    bridge._server_started_at = time.monotonic()  # just started
    with caplog.at_level(logging.WARNING, logger="arenamcp.gre_bridge"):
        bridge._maybe_warn_no_plugin()
    assert not bridge._no_plugin_warned
