from types import SimpleNamespace
from unittest.mock import Mock

from arenamcp.backend_health import BackendHealth, HealthState
from arenamcp.standalone import StandaloneCoach
from arenamcp.standalone_startup import _StartupMixin


def test_inconclusive_probe_then_inference_failure_and_recovery_update_banner(monkeypatch):
    BackendHealth.reset_instance()
    try:
        coach = StandaloneCoach.__new__(StandaloneCoach)
        coach.ui = Mock()
        coach._coach = SimpleNamespace(_backend=SimpleNamespace(_base_url="https://gateway.test/v1"))
        coach._startup_finished = False

        def probe(_backend):
            BackendHealth.instance().record_probe_warning("Model list restricted")
            return HealthState.UNKNOWN, "Model list restricted"

        monkeypatch.setattr("arenamcp.standalone.check_gateway_health", probe)
        coach._probe_backend_health_at_startup()
        coach._startup_complete()
        assert coach.ui.emit_startup_status.call_args.args[0]["phase"] == "connection_warning"
        BackendHealth.instance().record_failure("Inference authentication failed", status_code=401)
        failure = coach.ui.emit_startup_status.call_args.args[0]
        assert failure["phase"] == "error"
        assert "authentication" in failure["message"]
        BackendHealth.instance().record_success()
        recovered = coach.ui.emit_startup_status.call_args.args[0]
        assert recovered["ready"] and recovered["phase"] == "ready"
    finally:
        BackendHealth.reset_instance()


def test_running_arena_waits_for_bridge_and_never_relaunches_it(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("arenamcp.standalone_startup.time.monotonic", lambda: now[0])
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("arenamcp.android_link.game_device", lambda: "desktop")
    monkeypatch.setattr("arenamcp.desktop.runtime.is_mtga_running", lambda: True)
    launch = Mock(side_effect=AssertionError("must not restart an active game"))
    monkeypatch.setattr("arenamcp.desktop.runtime.launch_mtga", launch)
    monkeypatch.setattr("arenamcp.platform_integration.mac_bridge_installed", lambda: True)
    runtime = _StartupMixin()
    runtime.ui = Mock()
    bridge = SimpleNamespace(connected=False)
    runtime._bridge_poller = SimpleNamespace(_bridge=bridge, connected=False)
    runtime._publish_arena_connection_status()
    assert "Connecting" in runtime.ui.status.call_args.args[1]
    now[0] += 11
    runtime._publish_arena_connection_status()
    assert "finish your match" in runtime.ui.status.call_args.args[1]
    bridge.connected = True
    now[0] += 6
    runtime._publish_arena_connection_status()
    runtime.ui.status.assert_called_with("ARENA", "")
    launch.assert_not_called()
