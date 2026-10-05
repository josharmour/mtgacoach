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
        backend=backend,
        controller=controller,
        get_game_state=lambda: state,
        status_fn=statuses.append,
        refine_target=lambda frame, data, action: action,
    )
    return nav, controller, backend, state, statuses


ENDED = {"match_id": "ended", "event_id": "Ladder", "deck_name": "Current deck"}


def arm(nav):
    nav.set_enabled(True)
    assert nav.note_match_end(dict(ENDED), confirmed_match_end=True)


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
    parse_queue_action(proposal(), expected_deck="Current deck")
    with pytest.raises(ValueError):
        parse_queue_action(proposal(**changes), expected_deck="Current deck")


@pytest.mark.parametrize("label", ["Premier Draft", "Traditional Sealed", "Buy Entry"])
def test_paid_or_limited_recent_tile_is_rejected(label):
    with pytest.raises(ValueError):
        parse_queue_action(proposal("select_recent", "recent", label=label), expected_deck="Current deck")


def test_only_the_ended_matchs_deck_tile_and_free_earned_reward_can_be_selected():
    # 2026-10-04 23:15:20: the leftmost tile (another deck and event) was
    # taken for the most recent one. Position is not trusted; the deck is.
    expected = "The Notary Hobbits Digital"
    for action, label in (("select_recent", "Brawl"), ("start_queue", "Play")):
        tile = proposal(action, "recent", label=label, deck_name="Michelangelo, Weirdness to 11")
        with pytest.raises(ValueError):
            parse_queue_action(tile, expected_deck=expected)
        with pytest.raises(ValueError):
            parse_queue_action(proposal(action, "recent", label=label, deck_name=expected))
        for visible in (expected, "the notary hobbits digital", "The Notary Hobbits Dig…"):
            parse_queue_action(
                proposal(action, "recent", label=label, deck_name=visible), expected_deck=expected
            )
    with pytest.raises(ValueError):
        parse_queue_action(
            proposal("select_recent", "recent", label="Brawl", deck_name="The"), expected_deck=expected
        )
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
    assert nav.active and not nav.paused_reason
    assert nav.get_debug_info()["result_waits"] == 3
    assert nav.get_debug_info()["failures"] == 0
    # Give the result animation time to finish, but bound a permanently
    # misclassified/stuck screen without clicking or handing gameplay off.
    for _ in range(9):
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


@pytest.mark.parametrize("title", ["VICTORY", "DEFEAT", "DRAW"])
def test_report_result_wait_dismisses_overlay_then_requeues_and_hands_off(title):
    nav, controller, backend, state, _ = navigator()
    arm(nav)
    # The old board remains visible while the match-end animation runs.
    backend.complete_with_image.return_value = proposal("wait", "match", point=None)
    for _ in range(3):
        step(nav)
    assert not nav.paused_reason
    controller.execute.assert_not_called()
    # Replay the observed results/wait/DEFEAT response. The title itself
    # proves the dismissible overlay; no separate Continue button is needed.
    backend.complete_with_image.return_value = proposal(
        "wait", "results", label=title, point=None, result_visible=True
    )
    step(nav)
    controller.execute.assert_called_once()
    assert controller.execute.call_args.args[1].point == (0.5, 0.5)
    assert nav.get_debug_info()["recent_actions"] == [{"action": "dismiss_result", "label": title}]
    assert nav.get_debug_info()["result_waits"] == 0
    for response in [
        proposal("open_play", "home"),
        proposal("select_recent", "recent", label="Brawl"),
        proposal(),
        proposal("wait", "queue", point=None),
    ]:
        backend.complete_with_image.return_value = response
        step(nav)
    assert controller.execute.call_count == 4
    assert nav.get_debug_info()["queue_started"]
    state["match_id"] = "next"
    assert not nav.process_tick(state)
    assert not nav.active


@pytest.mark.parametrize(
    "changes",
    [
        {"screen": "reward"},
        {"result_visible": False},
        {"result_visible": "true"},
        {"result_visible": None},
        {"label": "Play"},
        {"confidence": 0.89},
    ],
)
def test_result_dismissal_requires_a_confident_result_title_on_a_visible_overlay(changes):
    data = dict(label="DEFEAT", point=None, result_visible=True)
    data.update(changes)
    screen = data.pop("screen", "results")
    with pytest.raises(ValueError):
        parse_queue_action(proposal("dismiss_result", screen, **data))


def test_results_without_visible_banner_only_waits_and_remains_bounded():
    nav, controller, backend, _, _ = navigator()
    arm(nav)
    backend.complete_with_image.return_value = proposal(
        "wait", "results", label="DEFEAT", point=None, result_visible=False
    )
    for _ in range(12):
        step(nav)
    controller.execute.assert_not_called()
    assert nav.paused_reason


@pytest.mark.parametrize("change", ["new_match", "disabled", "changed_screen"])
def test_normalized_result_dismissal_rechecks_match_and_screen_before_click(change):
    nav, controller, backend, state, _ = navigator()
    arm(nav)
    backend.complete_with_image.return_value = proposal(
        "wait", "results", label="DEFEAT", point=None, result_visible=True
    )

    def fresh_capture():
        if change == "new_match":
            state["match_id"] = "next"
        elif change == "disabled":
            nav.set_enabled(False)
        return make_frame(color="red" if change == "changed_screen" else "green")

    # Apply state changes at the second capture, after the observation.
    count = 0

    def capture():
        nonlocal count
        count += 1
        return make_frame(color="green") if count == 1 else fresh_capture()

    controller.capture.side_effect = capture
    step(nav)
    controller.execute.assert_not_called()


@pytest.mark.parametrize("screen", ["results", "match"])
@pytest.mark.parametrize("kind", ["wait", "continue", "open_play", "dismiss_result"])
def test_result_title_dismisses_without_any_button_or_model_coordinates(screen, kind):
    data, action = parse_queue_action(
        proposal(
            kind,
            screen,
            label="Click anywhere",  # An instruction, not a visible button.
            result_title="DEFEAT",
            result_visible=True,
            point=[0.97, 0.98],  # Ignore a guessed button position behind the overlay.
        )
    )
    assert data["screen"] == "results"
    assert data["action"] == "dismiss_result"
    assert data["label"] == "DEFEAT"
    assert action.point == (0.5, 0.5)


def test_result_click_is_not_reported_as_closed_until_next_screen_is_observed():
    nav, controller, backend, _, statuses = navigator()
    arm(nav)
    backend.complete_with_image.return_value = proposal(
        "continue", "results", label=None, result_title="DEFEAT", result_visible=True, point=None
    )
    step(nav)
    assert nav.get_debug_info()["awaiting_result_dismissal"]
    assert "waiting for it to close" in statuses[-1]
    # The first click can start an animation; that isn't evidence of Home yet.
    backend.complete_with_image.return_value = proposal("wait", "match", point=None)
    step(nav)
    controller.execute.assert_called_once()
    assert nav.get_debug_info()["awaiting_result_dismissal"]
    backend.complete_with_image.return_value = proposal("open_play", "home")
    step(nav)
    request = json.loads(backend.complete_with_image.call_args.args[1])
    assert request["awaiting_result_dismissal"] is True
    assert not nav.get_debug_info()["awaiting_result_dismissal"]
    assert "Result screen closed; continuing to Recently Played" in statuses
    assert controller.execute.call_count == 2


def test_rejected_label_is_preserved_and_given_to_next_observation_for_repair():
    nav, controller, backend, _, _ = navigator()
    arm(nav)
    rejected = proposal("open_play", "home", label="Play Brawl")
    backend.complete_with_image.return_value = rejected
    step(nav)
    controller.execute.assert_not_called()
    debug = nav.get_debug_info()
    assert debug["last_proposal"] == json.loads(rejected)
    assert "Play Brawl" in debug["last_rejection"]["error"]
    assert "expected one of ['play']" in debug["last_rejection"]["error"]
    backend.complete_with_image.return_value = proposal("open_play", "home")
    step(nav)
    request = json.loads(backend.complete_with_image.call_args.args[1])
    assert request["previous_rejection"] == debug["last_rejection"]
    assert request["supported_actions"]["open_play"] == {"screens": ["home"], "labels": ["play"]}
    controller.execute.assert_called_once()
    assert nav.get_debug_info()["last_rejection"] is None


def test_toggle_retries_the_paused_finished_match_without_a_new_completion():
    nav, controller, backend, _, statuses = navigator()
    arm(nav)
    backend.complete_with_image.return_value = proposal("open_play", "home", label="Play Brawl")
    for _ in range(3):
        step(nav)
    assert nav.paused_reason
    old_generation, old_abort = nav._generation, nav._abort
    nav.set_enabled(False)
    nav.set_enabled(True)
    assert nav.active and not nav.paused_reason
    assert old_abort.is_set() and nav._generation != old_generation
    assert nav.get_debug_info()["failures"] == 0
    assert statuses[-1] == "Retrying navigation for the finished match"
    backend.complete_with_image.return_value = proposal("open_play", "home")
    nav._step(old_generation, old_abort)
    controller.execute.assert_not_called()
    step(nav)
    controller.execute.assert_called_once()


def test_resuming_after_user_started_a_new_match_never_clicks_the_old_target():
    nav, controller, backend, state, _ = navigator()
    arm(nav)
    nav.set_enabled(False)
    state["match_id"] = "next"
    nav.set_enabled(True)
    assert not nav.process_tick(state)
    backend.complete_with_image.assert_not_called()
    controller.execute.assert_not_called()
    nav.set_enabled(False)
    nav.set_enabled(True)
    assert not nav.active  # Handoff consumed the old completion.


def test_loading_after_defeat_does_not_wait_thirty_seconds_or_prove_matchmaking(monkeypatch):
    nav, controller, backend, _, statuses = navigator()
    arm(nav)
    monkeypatch.setattr("arenamcp.auto_queue.time.monotonic", lambda: 100.0)
    backend.complete_with_image.return_value = proposal(
        "wait", "queue", label="Waiting for the Server...", point=None
    )
    step(nav)
    assert nav._next_poll == 103.0
    assert not nav.get_debug_info()["queue_observed"]
    assert statuses[-1] == "Waiting for Arena to finish loading"
    # A loading screen cannot authorize a visual handoff to the ended board.
    backend.complete_with_image.return_value = proposal("wait", "match", point=None)
    step(nav)
    assert nav.active
    controller.execute.assert_not_called()


def test_actual_matchmaking_can_wait_and_prove_a_subsequent_visual_handoff(monkeypatch):
    nav, controller, backend, _, _ = navigator()
    arm(nav)
    monkeypatch.setattr("arenamcp.auto_queue.time.monotonic", lambda: 100.0)
    backend.complete_with_image.return_value = proposal("wait", "queue", point=None, matchmaking_visible=True)
    step(nav)
    assert nav._next_poll == 130.0
    assert nav.get_debug_info()["queue_observed"]
    backend.complete_with_image.return_value = proposal("wait", "match", point=None)
    step(nav)
    assert not nav.active
    controller.execute.assert_not_called()


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


def refining_navigator(refined):
    """A navigator whose refinement pass hits the (fake) model, like production."""
    nav, controller, backend, state, statuses = navigator()
    nav._refine_target = nav._refine_with_model
    backend.complete_with_image.side_effect = [proposal(point=[0.897, 0.846]), refined]
    controller.capture.side_effect = lambda: make_frame(size=(1600, 935))
    return nav, controller, backend


def test_click_is_regrounded_on_a_zoomed_crop_before_input():
    from io import BytesIO

    from PIL import Image

    from arenamcp.auto_queue import REFINE_PROMPT

    nav, controller, backend = refining_navigator(json.dumps({"found": True, "point": [0.68, 0.55]}))
    arm(nav)
    step(nav)
    refine_call = backend.complete_with_image.call_args_list[1]
    assert refine_call.args[0] == REFINE_PROMPT
    assert json.loads(refine_call.args[1])["target_label"] == "Play"
    assert Image.open(BytesIO(refine_call.args[2])).size == (480, 280)
    # The crop is clamped to the right edge: left=1120, top=651.
    point = controller.execute.call_args.args[1].point
    assert point == pytest.approx(((1120 + 0.68 * 480) / 1600, (651 + 0.55 * 280) / 935))


def test_target_missing_from_zoomed_crop_prevents_input_and_is_fed_back():
    nav, controller, backend = refining_navigator(json.dumps({"found": False, "point": None}))
    arm(nav)
    step(nav)
    controller.execute.assert_not_called()
    rejection = nav.get_debug_info()["last_rejection"]
    assert "Could not confirm 'Play'" in rejection["error"]
    assert nav._failures == 1


@pytest.mark.parametrize(
    "refined",
    ["not json", "[BACKEND ERROR] timeout", json.dumps({"found": True, "point": [480, 140]})],
)
def test_failed_refinement_keeps_the_full_frame_estimate(refined):
    nav, controller, _ = refining_navigator(refined)
    arm(nav)
    step(nav)
    assert controller.execute.call_args.args[1].point == (0.897, 0.846)


def test_result_dismissal_skips_refinement():
    nav, controller, backend, _, _ = navigator()
    nav._refine_target = nav._refine_with_model
    arm(nav)
    backend.complete_with_image.return_value = proposal(
        "dismiss_result", "results", label="VICTORY", result_title="VICTORY", result_visible=True
    )
    step(nav)
    assert backend.complete_with_image.call_count == 1
    assert controller.execute.call_args.args[1].point == (0.5, 0.5)


def test_click_without_visible_effect_is_reported_to_the_next_observation():
    nav, controller, backend, _, _ = navigator()
    arm(nav)
    step(nav)
    controller.execute.assert_called_once()
    step(nav)
    request = json.loads(backend.complete_with_image.call_args.args[1])
    assert request["previous_click_had_no_visible_effect"] == {"action": "start_queue", "label": "Play"}

    nav, controller, backend, _, _ = navigator()
    arm(nav)
    step(nav)
    controller.capture.side_effect = lambda: make_frame(color="red")
    step(nav)
    request = json.loads(backend.complete_with_image.call_args.args[1])
    assert request["previous_click_had_no_visible_effect"] is None


def test_unknown_deck_pauses_instead_of_choosing_a_recently_played_tile():
    nav, controller, backend, _, _ = navigator()
    nav.set_enabled(True)
    assert nav.note_match_end({"match_id": "ended", "event_id": "Ladder"}, confirmed_match_end=True)
    step(nav)
    controller.execute.assert_not_called()
    assert "did not show which deck" in nav.paused_reason


def test_expected_queue_from_the_log_is_given_to_the_model():
    nav, _, backend, _, _ = navigator()
    arm(nav)
    step(nav)
    request = json.loads(backend.complete_with_image.call_args.args[1])
    assert request["expected_queue"] == {"event_id": "Ladder", "deck_name": "Current deck"}


def test_refinement_cannot_hop_to_another_tiles_control():
    # Observed 23:15:20: estimate (0.272, 0.886) refined to (0.173, 0.922).
    nav, controller, backend = refining_navigator(json.dumps({"found": True, "point": [0.02, 0.6]}))
    backend.complete_with_image.side_effect = [
        proposal(point=[0.272, 0.886]),
        json.dumps({"found": True, "point": [0.17, 0.62]}),
    ]
    arm(nav)
    step(nav)
    controller.execute.assert_not_called()
    assert "moved" in nav.get_debug_info()["last_rejection"]["error"]


def queued_navigator(selection):
    nav, controller, backend, state, statuses = navigator()
    nav._queue_selection = lambda: selection
    arm(nav)
    step(nav)  # clicks start_queue
    assert controller.execute.call_count == 1
    backend.complete_with_image.return_value = proposal("wait", "queue", point=None, matchmaking_visible=True)
    return nav, controller, backend


def test_log_confirms_the_queue_the_click_joined():
    import time

    nav, _, backend = queued_navigator(
        {"event_id": "Ladder", "deck_name": "Current deck", "recorded_at": time.time() + 1}
    )
    step(nav)
    assert not nav.paused_reason
    assert nav._queue_confirmed


def test_log_showing_a_different_deck_or_event_pauses_navigation():
    import time

    for selection in (
        {"event_id": "Play_Brawl_Historic", "deck_name": "Current deck"},
        {"event_id": "Ladder", "deck_name": "Michelangelo, Weirdness to 11"},
    ):
        nav, controller, backend = queued_navigator({**selection, "recorded_at": time.time() + 1})
        calls = backend.complete_with_image.call_count
        step(nav)
        assert "Leave that queue" in nav.paused_reason
        assert backend.complete_with_image.call_count == calls
        assert controller.execute.call_count == 1


def test_a_selection_from_before_the_click_is_not_evidence():
    nav, _, _ = queued_navigator({"event_id": "Other", "deck_name": "Old deck", "recorded_at": 1.0})
    step(nav)
    assert not nav.paused_reason


def test_same_deck_in_another_known_queue_is_rejected():
    # The same deck can be on a Competitive Brawl tile and a Brawl tile.
    tile = proposal("start_queue", "recent", deck_name="The Notary Hobbits Digital")
    expected = {"expected_deck": "The Notary Hobbits Digital", "expected_event": "Brawl_Ladder"}
    with pytest.raises(ValueError):
        parse_queue_action(proposal(**{**json.loads(tile), "queue_name": "Brawl"}), **expected)
    parse_queue_action(proposal(**{**json.loads(tile), "queue_name": "Competitive  Brawl"}), **expected)
    # Unmapped events rely on the deck name alone.
    parse_queue_action(
        proposal(**{**json.loads(tile), "queue_name": "Bot Match"}),
        expected_deck="The Notary Hobbits Digital",
        expected_event="AIBotMatch",
    )


def test_bot_match_requeue_requires_the_bot_match_tile():
    tile = json.loads(proposal("start_queue", "recent", deck_name="Mono-White Auras"))
    expected = {"expected_deck": "Mono-White Auras", "expected_event": "AIBotMatch"}
    parse_queue_action(proposal(**{**tile, "queue_name": "Bot Match"}), **expected)
    with pytest.raises(ValueError):
        parse_queue_action(proposal(**{**tile, "queue_name": "Brawl"}), **expected)
