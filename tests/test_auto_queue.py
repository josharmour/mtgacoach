"""Opt-in match-boundary navigation without live input or network requests."""

import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp.auto_queue import AutoQueueNavigator, parse_queue_action
from test_native_mac_autopilot import make_frame


def proposal(action="start_queue", screen="deck", **changes):
    data = {
        "screen": screen,
        "action": action,
        "label": "Play",
        "point": [0.5, 0.7],
        "confidence": 0.98,
        "reason": "Use the visible most recent queue with the current deck",
        "recent_index": 0,
        "recent_selected": True,
        "deck_selected": True,
        "free_entry": True,
        "result_visible": False,
        "queue_name": "Standard Ranked",
        "deck_name": "Current deck",
    }
    data.update(changes)
    return json.dumps(data)


def navigator():
    state = {"match_id": "ended"}
    controller = Mock()
    controller.capture.side_effect = make_frame
    controller.execute.return_value = True
    backend = SimpleNamespace(complete_with_image=Mock(return_value=proposal()))
    statuses = []
    nav = AutoQueueNavigator(
        backend=backend, controller=controller, get_game_state=lambda: state, status_fn=statuses.append
    )
    return nav, controller, backend, state, statuses


def arm(nav):
    nav.set_enabled(True)
    assert nav.note_match_end({"match_id": "ended"}, confirmed_match_end=True)


def step(nav):
    nav._step(nav._generation, nav._abort)


def test_default_off_and_enable_does_not_start_initial_queue_or_call_model():
    nav, controller, backend, state, _ = navigator()
    assert not nav.process_tick(state)
    nav.set_enabled(True)
    assert not nav.process_tick(state)
    assert not nav.note_match_end({"match_id": "ended", "last_game_result": "win"})
    assert not nav.process_tick(state)
    controller.capture.assert_not_called()
    backend.complete_with_image.assert_not_called()


def test_match_scope_required_and_duplicate_completion_cannot_rearm():
    nav, *_ = navigator()
    nav.set_enabled(True)
    assert not nav.note_match_end({"match_id": "ended", "match_end_scope": "game"})
    assert nav.note_match_end({"match_id": "ended", "match_end_scope": "match"})
    assert not nav.note_match_end({"match_id": "ended"}, confirmed_match_end=True)
    assert not nav.process_tick({"match_id": "next"})
    assert not nav.active
    assert not nav.note_match_end({"match_id": "ended"}, confirmed_match_end=True)
    assert nav.note_match_end({"match_id": "next", "match_complete": True})


def test_claim_recently_played_and_queue_use_visible_labels_one_input_per_step():
    nav, controller, backend, _, _ = navigator()
    arm(nav)
    sequence = [
        proposal("claim", "reward", label="Claim"),
        proposal("open_play", "home", label="Play"),
        proposal("open_recent", "play", label="Recently Played"),
        proposal("select_recent", "recent", label="Standard Ranked"),
        proposal(),
        proposal("wait", "queue", point=None),
    ]
    for index, response in enumerate(sequence):
        backend.complete_with_image.return_value = response
        before = controller.execute.call_count
        step(nav)
        assert controller.execute.call_count - before == (0 if index == 5 else 1)
    assert controller.execute.call_count == 5
    assert backend.complete_with_image.call_count == 6
    assert all(call.kwargs["request_timeout_s"] == 8 for call in backend.complete_with_image.call_args_list)
    assert nav.get_debug_info()["stage"] == "queue"


def test_already_selected_recent_queue_can_use_play_directly_without_changing_deck():
    nav, controller, _, _, _ = navigator()
    arm(nav)
    step(nav)
    controller.execute.assert_called_once()
    assert nav.get_debug_info()["recent_actions"] == [{"action": "start_queue", "label": "Play"}]


@pytest.mark.parametrize(
    "changes",
    [
        {"screen": "match"},
        {"screen": "sideboard"},
        {"screen": "home"},
        {"action": "purchase"},
        {"label": "Buy"},
        {"label": "Pay 1000 Gold"},
        {"deck_selected": False},
        {"recent_selected": False},
        {"free_entry": False},
        {"free_entry": "true"},
        {"confidence": 0.8},
        {"point": [1.5, 0.2]},
    ],
)
def test_invalid_or_unverified_queue_action_is_rejected(changes):
    with pytest.raises(ValueError):
        parse_queue_action(proposal(**changes))


@pytest.mark.parametrize("label", ["Premier Draft", "Traditional Sealed", "Buy Entry"])
def test_paid_or_limited_recent_tile_is_rejected(label):
    with pytest.raises(ValueError):
        parse_queue_action(proposal("select_recent", "recent", label=label))


def test_only_first_most_recent_tile_and_free_earned_reward_can_be_selected():
    with pytest.raises(ValueError):
        parse_queue_action(proposal("select_recent", "recent", recent_index=1, label="Standard"))
    with pytest.raises(ValueError):
        parse_queue_action(proposal("claim", "reward", label="Claim", free_entry=False))


def test_new_match_during_model_response_prevents_input():
    nav, controller, backend, state, _ = navigator()
    arm(nav)

    def response(*args, **kwargs):
        state["match_id"] = "next"
        return proposal()

    backend.complete_with_image.side_effect = response
    step(nav)
    controller.execute.assert_not_called()
    assert not nav.process_tick(state)
    assert not nav.active


@pytest.mark.parametrize("change", ["disable", "new_match"])
def test_state_change_during_capture_prevents_model_request(change):
    nav, controller, backend, state, _ = navigator()
    arm(nav)

    def capture():
        if change == "disable":
            nav.set_enabled(False)
        else:
            state["match_id"] = "next"
        return make_frame()

    controller.capture.side_effect = capture
    step(nav)
    backend.complete_with_image.assert_not_called()
    controller.execute.assert_not_called()
    assert not nav.active


def test_visible_gameplay_hands_off_even_before_new_match_id_is_logged():
    nav, controller, backend, state, _ = navigator()
    arm(nav)
    step(nav)  # This cycle successfully submitted Play to enter matchmaking.
    backend.complete_with_image.return_value = proposal("wait", "match", point=None)
    step(nav)
    assert not nav.active
    assert not nav.process_tick(state)
    assert backend.complete_with_image.call_count == 2
    controller.execute.assert_called_once()


def test_ended_board_misclassified_as_gameplay_cannot_handoff_or_click():
    nav, controller, backend, state, _ = navigator()
    arm(nav)
    backend.complete_with_image.return_value = proposal("wait", "match", point=None)
    for _ in range(3):
        step(nav)
    assert nav.active and nav.paused_reason
    assert nav.process_tick(state)
    controller.execute.assert_not_called()


def test_result_overlay_takes_precedence_even_after_queue_started():
    nav, controller, backend, _, _ = navigator()
    arm(nav)
    step(nav)
    backend.complete_with_image.return_value = proposal("wait", "match", point=None, result_visible=True)
    step(nav)
    assert nav.active
    assert controller.execute.call_count == 1


def test_defeat_click_to_continue_is_supported_without_new_game_handoff():
    nav, controller, backend, _, _ = navigator()
    arm(nav)
    backend.complete_with_image.return_value = proposal(
        "continue", "results", label="Click to Continue", result_visible=True
    )
    step(nav)
    assert nav.active
    controller.execute.assert_called_once()
    assert nav.get_debug_info()["stage"] == "results"


def test_debug_snapshot_retains_navigation_evidence_without_serializing_image_or_backend():
    nav, controller, _, _, _ = navigator()
    assert nav.get_debug_screenshot() is None
    arm(nav)
    step(nav)
    debug = nav.get_debug_info()
    assert debug["enabled"] and debug["active"]
    assert debug["ended_match_id"] == "ended"
    assert debug["stage"] == "deck"
    assert debug["last_proposal"]["action"] == "start_queue"
    assert debug["recent_actions"] == [{"action": "start_queue", "label": "Play"}]
    assert debug["failures"] == 0
    assert debug["worker_running"] is False
    json.dumps(debug)
    assert nav.get_debug_screenshot().startswith(b"\x89PNG")
    assert controller.capture.call_count == 2


def test_disable_during_model_response_prevents_input():
    nav, controller, backend, _, _ = navigator()
    arm(nav)

    def response(*args, **kwargs):
        nav.set_enabled(False)
        return proposal()

    backend.complete_with_image.side_effect = response
    step(nav)
    controller.execute.assert_not_called()
    assert not nav.active


def test_changed_screen_or_disabled_at_final_capture_prevents_stale_click():
    nav, controller, _, _, _ = navigator()
    arm(nav)
    controller.capture.side_effect = [make_frame(color="green"), make_frame(color="red")]
    step(nav)
    controller.execute.assert_not_called()

    def disable():
        nav.set_enabled(False)
        return make_frame()

    controller.capture.side_effect = [make_frame(), disable()]
    step(nav)
    controller.execute.assert_not_called()


def test_repeat_and_model_failure_budgets_pause_without_turning_setting_off():
    nav, controller, _, _, _ = navigator()
    arm(nav)
    for _ in range(4):
        step(nav)
    assert controller.execute.call_count == 3
    assert nav.paused_reason and nav.enabled
    nav, controller, backend, _, _ = navigator()
    arm(nav)
    backend.complete_with_image.return_value = "invalid JSON"
    for _ in range(3):
        step(nav)
    controller.execute.assert_not_called()
    assert nav.paused_reason and nav.enabled


def test_failed_input_is_bounded_instead_of_spinning_requests():
    nav, controller, _, _, _ = navigator()
    arm(nav)
    controller.execute.return_value = False
    for _ in range(3):
        step(nav)
    assert nav.paused_reason
    assert nav._next_poll > 0


def test_process_tick_is_nonblocking_and_singleflight():
    nav, controller, backend, state, _ = navigator()
    arm(nav)
    entered, release = threading.Event(), threading.Event()

    def response(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return proposal()

    backend.complete_with_image.side_effect = response
    try:
        assert nav.process_tick(state)
        assert entered.wait(3)
        assert nav.process_tick(state)
        assert backend.complete_with_image.call_count == 1
        controller.execute.assert_not_called()
        nav.set_enabled(False)
    finally:
        release.set()
        if nav._worker:
            nav._worker.join(timeout=3)
    controller.execute.assert_not_called()


def test_foreground_unavailable_waits_without_model_request():
    from arenamcp.native_mac_input import DesktopUnavailable

    nav, controller, backend, _, statuses = navigator()
    arm(nav)
    controller.capture.side_effect = DesktopUnavailable("Bring Arena to the foreground")
    step(nav)
    backend.complete_with_image.assert_not_called()
    assert statuses[-1] == "Bring Arena to the foreground"


def test_other_platform_without_injected_controller_is_explicitly_unsupported(monkeypatch):
    monkeypatch.setattr("arenamcp.auto_queue.sys.platform", "win32")
    nav, _, backend, _, _ = navigator()
    nav._controller = None
    arm(nav)
    step(nav)
    assert "macOS" in nav.paused_reason
    backend.complete_with_image.assert_not_called()
