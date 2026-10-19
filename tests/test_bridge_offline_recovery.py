"""Bridge-offline recovery: autopilot waits for the plugin to reconnect
instead of cascading every action into MANUAL REQUIRED, and the bridge
warns once (actionably) when no plugin ever connects.

Live failure 2026-06-07 21:46: BepInEx wasn't injected (Steam launch
options lost WINEDLLOVERRIDES), the bridge stayed offline all match, and
every autopilot action failed "Bridge offline" with zero guidance.
"""

import logging
import time

import pytest
from tests.fakes import VirtualClock, make_engine

import arenamcp.autopilot as autopilot_module
from arenamcp.action_planner import ActionType, GameAction
from arenamcp.autopilot import AutopilotEngine
from arenamcp.autopilot_models import ClickResult
from arenamcp.gre_bridge import GREBridge


@pytest.fixture(autouse=True)
def _desktop_bridge_environment(monkeypatch):
    monkeypatch.setenv("MTGACOACH_GAME_DEVICE", "desktop")
    monkeypatch.setattr("arenamcp.platform_integration.mac_bridge_installed", lambda: False)


@pytest.fixture
def clock(monkeypatch):
    """The reconnect wait sleeps 0.25 s per attempt; run it on virtual time."""
    clock = VirtualClock()
    monkeypatch.setattr(autopilot_module, "time", clock)
    return clock


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


def _make_engine(bridge, **config_overrides) -> AutopilotEngine:
    options = {
        "bridge_reconnect_wait": 1.0,
        "bridge_reconnect_wait_cooldown": 60.0,
        "post_action_delay": 0.0,
        **config_overrides,
    }
    return make_engine(bridge, _DummyPlanner(), **options)


def test_wait_succeeds_when_plugin_reconnects(clock):
    bridge = _FlippingBridge(succeed_after=2)
    engine = _make_engine(bridge)
    assert engine._wait_for_bridge_reconnect() is True
    assert bridge.connected


def test_wait_cooldown_prevents_stacked_waits(clock):
    bridge = _FlippingBridge(succeed_after=10_000)  # never connects
    engine = _make_engine(bridge, bridge_reconnect_wait=0.3)
    start = clock.now
    assert engine._wait_for_bridge_reconnect() is False
    assert clock.now - start >= 0.25
    attempts = bridge.connect_calls

    # Second call must bail immediately — a dead plugin shouldn't add
    # seconds of latency to every subsequent action.
    start = clock.now
    assert engine._wait_for_bridge_reconnect() is False
    assert clock.now == start
    assert bridge.connect_calls == attempts


def test_wait_disabled_in_dry_run():
    bridge = _FlippingBridge(succeed_after=1)
    engine = _make_engine(bridge, dry_run=True)
    assert engine._wait_for_bridge_reconnect() is False
    assert bridge.connect_calls == 0


def test_execute_action_retries_bridge_after_reconnect(monkeypatch):
    bridge = _FlippingBridge(succeed_after=10_000)
    engine = _make_engine(bridge)

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
    monkeypatch.setattr("arenamcp.platform_integration.bridge_capable", lambda: True)
    bridge = GREBridge()
    bridge._server_socket = object()
    bridge._server_started_at = time.monotonic() - 60.0
    with caplog.at_level(logging.WARNING, logger="arenamcp.gre_bridge"):
        bridge._maybe_warn_no_plugin()
        bridge._maybe_warn_no_plugin()
    warnings = [r for r in caplog.records if "plugin is not loading" in r.getMessage()]
    assert len(warnings) == 1


def test_no_plugin_log_mode_instead_of_warning_when_bridge_impossible(caplog, monkeypatch):
    monkeypatch.setattr("arenamcp.platform_integration.bridge_capable", lambda: False)
    bridge = GREBridge()
    bridge._server_socket = object()
    bridge._server_started_at = time.monotonic() - 60.0
    with caplog.at_level(logging.INFO, logger="arenamcp.gre_bridge"):
        bridge._maybe_warn_no_plugin()
    assert not any(
        "plugin is not loading" in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    )
    assert any("log-only" in r.getMessage() for r in caplog.records)


def test_installed_mac_bridge_warns_instead_of_claiming_log_only(caplog, monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("arenamcp.platform_integration.bridge_capable", lambda: False)
    monkeypatch.setattr("arenamcp.platform_integration.mac_bridge_installed", lambda: True)
    bridge = GREBridge()
    bridge._server_socket = object()
    bridge._server_started_at = time.monotonic() - 60.0
    with caplog.at_level(logging.INFO, logger="arenamcp.gre_bridge"):
        bridge._maybe_warn_no_plugin()
        bridge._maybe_warn_no_plugin()
    warnings = [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "quit MTGA and reopen the coach" in warnings[0]
    assert "DYLD_INSERT_LIBRARIES" in warnings[0]
    assert "BepInEx" not in warnings[0]
    assert not any("log-only" in record.getMessage() for record in caplog.records)


def test_android_bridge_warns_even_when_mac_bridge_is_not_installed(caplog, monkeypatch):
    monkeypatch.setenv("MTGACOACH_GAME_DEVICE", "android")
    monkeypatch.setattr("arenamcp.platform_integration.bridge_capable", lambda: False)
    bridge = GREBridge()
    bridge._server_socket = object()
    bridge._server_started_at = time.monotonic() - 60.0
    with caplog.at_level(logging.WARNING, logger="arenamcp.gre_bridge"):
        bridge._maybe_warn_no_plugin()
    assert "adb reverse" in caplog.text
    assert "relaunch MTGA on the phone" in caplog.text
    assert "DYLD_INSERT_LIBRARIES" not in caplog.text


def test_mac_reconnect_timeout_reports_native_bridge_recovery(caplog, monkeypatch, clock):
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("arenamcp.platform_integration.bridge_capable", lambda: False)
    engine = _make_engine(_FlippingBridge(10_000), bridge_reconnect_wait=0.01)
    with caplog.at_level(logging.WARNING, logger="arenamcp.autopilot"):
        assert engine._wait_for_bridge_reconnect() is False
    assert "native macOS bridge" in caplog.text
    assert "DYLD_INSERT_LIBRARIES" in caplog.text
    assert "WINEDLLOVERRIDES" not in caplog.text


def test_no_plugin_warning_suppressed_after_any_connection(caplog):
    bridge = GREBridge()
    bridge._server_socket = object()
    bridge._server_started_at = time.monotonic() - 60.0
    bridge._ever_connected = True
    with caplog.at_level(logging.WARNING, logger="arenamcp.gre_bridge"):
        bridge._maybe_warn_no_plugin()
    assert not bridge._no_plugin_warned
    assert not any("plugin is not loading" in r.getMessage() for r in caplog.records)


def test_no_plugin_warning_waits_for_grace_period(caplog):
    bridge = GREBridge()
    bridge._server_socket = object()
    bridge._server_started_at = time.monotonic()  # just started
    with caplog.at_level(logging.WARNING, logger="arenamcp.gre_bridge"):
        bridge._maybe_warn_no_plugin()
    assert not bridge._no_plugin_warned
