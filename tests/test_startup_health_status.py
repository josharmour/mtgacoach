from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp.backend_health import BackendHealth
from arenamcp.standalone import StandaloneCoach
from arenamcp.standalone_startup import _StartupMixin


class _ProbeBackend:
    """The live boot check: ProxyBackend.probe_inference sends a 1-token completion."""

    _base_url = "https://gateway.test/v1"
    model = "glm-5.3-flash"

    def __init__(self, ok: bool) -> None:
        self.ok = ok

    def probe_inference(self, timeout: float = 8.0):
        if self.ok:
            return True, "glm-5.3-flash inference OK, 210ms"
        return False, "glm-5.3-flash: inference probe failed after 5000ms: Connection error"


@pytest.mark.parametrize("boot_ok", [True, False], ids=["boot-ok", "boot-down"])
def test_inference_probe_then_failure_and_recovery_update_the_banner(boot_ok):
    BackendHealth.reset_instance()
    try:
        coach = StandaloneCoach.__new__(StandaloneCoach)
        coach.ui = Mock()
        coach._coach = SimpleNamespace(_backend=_ProbeBackend(boot_ok))
        coach._startup_finished = False

        def last_status():
            return coach.ui.emit_startup_status.call_args.args[0]

        def health_states():
            return [c.args[0]["state"] for c in coach.ui.emit_backend_health.call_args_list]

        coach._probe_backend_health_at_startup()
        phases = [c.args[0]["phase"] for c in coach.ui.emit_startup_status.call_args_list]
        assert "checking_connection" in phases
        coach._startup_complete()
        if boot_ok:
            assert last_status()["phase"] == "ready" and last_status()["ready"]
            # #215: mid-match trouble is transient. The startup card stays
            # ready; the degraded state travels on the backend_health event
            # (rendered as a small status line), not as a new error phase.
            BackendHealth.instance().record_failure("Inference authentication failed", status_code=401)
            assert last_status()["phase"] == "ready" and last_status()["ready"]
            assert health_states()[-1] == "degraded"
            BackendHealth.instance().record_failure("first token timeout")
            BackendHealth.instance().record_failure("stream stalled")
            assert last_status()["phase"] == "ready" and last_status()["ready"]
            assert health_states()[-1] == "down"
        else:
            assert last_status()["phase"] == "error"
            assert "Connection error" in last_status()["message"]
            assert not last_status()["ready"]

        BackendHealth.instance().record_success()
        assert last_status()["ready"] and last_status()["phase"] == "ready"
        assert health_states()[-1] == "ok"
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
