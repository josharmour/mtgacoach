import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp.auto_queue import AutoQueueNavigator
from arenamcp.recent_queue_attempt import reserve
from arenamcp.standalone_auto_queue import _AutoQueueMixin


def setup():
    target = dict(
        ok=True, event_id="Traditional_Explorer", deck_id="deck-a", deck_name="Recent deck", format="Explorer"
    )
    bridge = SimpleNamespace(
        connected=True,
        client_runtime="il2cpp-macos",
        automation_blocker=lambda **kw: None,
        get_survey=Mock(return_value={"ok": True, "is_open": False}),
        get_recent_match=Mock(return_value=target),
        queue_recent_match=Mock(return_value={"ok": True}),
        return_to_home=Mock(return_value=True),
    )
    state = {}
    backend, controller = Mock(), Mock()
    nav = AutoQueueNavigator(
        backend=backend, controller=controller, bridge=bridge, get_game_state=lambda: state
    )
    nav.set_enabled(True)
    return nav, bridge, state, backend, controller


def step(nav):
    nav._step(nav._generation, nav._abort)


def test_cold_start_uses_recent_and_never_requests_desktop_focus_or_model():
    nav, bridge, state, backend, controller = setup()
    assert nav.start_recent_match(state)
    step(nav)
    bridge.queue_recent_match.assert_called_once()
    assert nav._expected_queue == {"event_id": "Traditional_Explorer", "deck_name": "Recent deck"}
    assert nav._stage == "queue"
    step(nav)
    bridge.queue_recent_match.assert_called_once()
    controller.capture.assert_not_called()
    controller.execute.assert_not_called()
    backend.complete_with_image.assert_not_called()


def test_repeat_resolves_new_featured_entry_not_previous_deck_or_event():
    nav, bridge, state, *_ = setup()
    assert nav.start_recent_match(state)
    step(nav)
    state["match_id"] = "match-1"
    assert not nav.process_tick(state)
    assert nav.note_match_end({"match_id": "match-1", "completed_at": time.time()}, confirmed_match_end=True)
    bridge.get_recent_match.return_value = dict(
        ok=True, event_id="Play", deck_id="deck-b", deck_name="New recent", format="Standard"
    )
    step(nav)  # results
    step(nav)  # new Home model
    assert bridge.queue_recent_match.call_count == 2
    assert bridge.queue_recent_match.call_args.args[0]["deck_id"] == "deck-b"
    assert nav._last_event_id == "Play"


def test_disconnection_and_reconnect_do_not_repeat_uncertain_submission():
    nav, bridge, state, *_ = setup()
    nav.start_recent_match(state)
    bridge.queue_recent_match.side_effect = OSError("connection lost after write")
    step(nav)
    nav.set_enabled(False)
    nav.set_enabled(True)
    step(nav)
    assert nav._stage == "queue" and nav._queue_started
    assert bridge.queue_recent_match.call_count == 1


def test_engine_reload_retains_pending_cold_start_without_fake_match():
    nav, bridge, state, *_ = setup()
    nav.start_recent_match(state)
    step(nav)
    record = nav.export_for_reload()
    assert record["ended_match_id"] is None
    second, second_bridge, *_ = setup()
    assert second.restore_after_reload(record)
    step(second)
    second_bridge.queue_recent_match.assert_not_called()
    assert second._quest_deck_id == "deck-a"


def test_missing_recent_stops_visibly_with_no_fallback():
    nav, bridge, state, *_ = setup()
    bridge.get_recent_match.return_value = {"ok": False, "error": "Recent Match has no available entry"}
    nav.start_recent_match(state)
    step(nav)
    assert "no available entry" in nav.paused_reason
    bridge.queue_recent_match.assert_not_called()


def test_fresh_handshake_required_before_navigation():
    nav, bridge, state, *_ = setup()
    nav.start_recent_match(state)
    bridge.automation_blocker = lambda **kw: "Bridge disconnected"
    step(nav)
    bridge.get_recent_match.assert_not_called()
    bridge.queue_recent_match.assert_not_called()


def test_cold_start_no_log_completion_required_but_live_decision_blocks():
    nav, *_ = setup()
    runtime = _AutoQueueMixin()
    assert runtime._arm_auto_queue_after_restart(nav, {})
    other, *_ = setup()
    assert not runtime._arm_auto_queue_after_restart(
        other, {"match_id": "active", "pending_decision": {"type": "actions"}}
    )
    assert not other.active


def test_persisted_attempt_survives_restart_but_completed_match_permits_next(tmp_path):
    path = tmp_path / "attempt.json"
    target = {"event_id": "Ladder", "deck_id": "one"}
    reserve("arena-lifetime-A", target, path=path)
    with pytest.raises(ValueError, match="already pending"):
        reserve("arena-lifetime-A", target, path=path)
    reserve("arena-lifetime-A", target, completed_at=time.time() + 1, path=path)
    reserve("arena-lifetime-B", target, path=path)


def test_corrupt_attempt_record_fails_closed(tmp_path):
    path = tmp_path / "attempt.json"
    path.write_text("invalid")
    with pytest.raises(ValueError):
        reserve("arena-A", {"event_id": "Play", "deck_id": "one"}, path=path)


def test_queue_confirmation_requires_exact_deck_id():
    nav, bridge, state, *_ = setup()
    nav.start_recent_match(state)
    step(nav)
    nav._queue_selection = lambda: {
        "recorded_at": time.time(),
        "event_id": "Traditional_Explorer",
        "deck_name": "Recent deck",
        "deck_id": "wrong",
    }
    assert nav._joined_wrong_queue()


def test_native_repeat_keeps_result_animation_readiness_before_recent_queue():
    from tests.test_auto_queue_result_bridge_20261020 import CLOSED, READY

    nav, bridge, state, _, controller = setup()
    bridge.get_match_end = Mock(side_effect=[READY, CLOSED])
    bridge.leave_match = Mock(return_value={"ok": True})
    state["match_id"] = "ended"
    nav.note_match_end({"match_id": "ended", "completed_at": time.time()}, confirmed_match_end=True)
    step(nav)
    bridge.leave_match.assert_called_once()
    bridge.queue_recent_match.assert_not_called()
    step(nav)
    bridge.queue_recent_match.assert_called_once()
    bridge.return_to_home.assert_not_called()
    controller.capture.assert_not_called()


def test_loading_home_retries_without_falling_back_to_a_different_queue():
    nav, bridge, state, backend, controller = setup()
    nav.start_recent_match(state)
    bridge.get_recent_match.return_value = {"ok": False, "error": "identity mismatch: IsLoading is true"}
    for _ in range(12):
        step(nav)
    assert not nav.paused_reason
    assert not nav._queue_started
    bridge.queue_recent_match.assert_not_called()
    controller.capture.assert_not_called()
    backend.complete_with_image.assert_not_called()


def test_turning_auto_queue_off_during_target_read_prevents_submission():
    nav, bridge, state, *_ = setup()
    nav.start_recent_match(state)
    target = bridge.get_recent_match.return_value

    def disable():
        nav.set_enabled(False)
        return target

    bridge.get_recent_match.side_effect = disable
    step(nav)
    bridge.queue_recent_match.assert_not_called()
