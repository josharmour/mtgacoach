"""Windows must observe result readiness and closure before submitting Ladder."""

from unittest.mock import Mock

from tests.test_auto_queue import arm, navigator, step
from tests.test_auto_queue_result_bridge_20261020 import CLOSED, NOT_READY, READY, result_bridge


def test_windows_animation_does_not_consume_navigation_retries_or_queue(monkeypatch):
    monkeypatch.setattr("arenamcp.auto_queue.sys.platform", "win32")
    nav, controller, backend, _, statuses = navigator()
    bridge = result_bridge([NOT_READY] * 8 + [READY, READY, CLOSED])
    bridge.client_runtime = "bepinex"
    bridge.return_to_home = Mock(return_value=True)
    bridge.queue_match = Mock(return_value=True)
    nav._bridge = bridge
    arm(nav)

    for _ in range(8):
        step(nav)
        assert nav._stage == "results"
        assert nav._failures == 0
    bridge.leave_match.assert_not_called()
    bridge.return_to_home.assert_not_called()
    bridge.queue_match.assert_not_called()
    assert statuses[-1] == "Waiting for Arena's result screen to accept input"

    step(nav)  # The real exit callback is now present.
    bridge.leave_match.assert_called_once()
    assert nav._stage == "results"
    step(nav)  # Method accepted, but the result scene is still visible.
    bridge.return_to_home.assert_not_called()
    bridge.queue_match.assert_not_called()

    step(nav)  # Result scene closed; Home is independently confirmed.
    bridge.return_to_home.assert_called_once()
    assert nav._stage == "home"
    bridge.queue_match.assert_not_called()
    step(nav)
    bridge.queue_match.assert_called_once_with(event="Ladder")
    assert nav._stage == "queue"
    controller.execute.assert_not_called()
    backend.complete_with_image.assert_not_called()


def test_windows_old_bridge_without_readiness_keeps_legacy_path(monkeypatch):
    monkeypatch.setattr("arenamcp.auto_queue.sys.platform", "win32")
    nav, _, _, _, _ = navigator()
    bridge = result_bridge([None])
    bridge.client_runtime = "bepinex"
    bridge.return_to_home = Mock(return_value=False)
    bridge.queue_match = Mock()
    nav._bridge = bridge
    arm(nav)
    step(nav)
    bridge.return_to_home.assert_called_once()
    bridge.leave_match.assert_not_called()
    bridge.queue_match.assert_not_called()
    assert nav._stage == "results"
