"""2026-10-20: auto-queue left VICTORY screens alone after three ignored clicks.

10:05:48-10:05:59 and 10:13:37-10:13:48 the centre click on VICTORY had no
visible effect three times, navigation paused until toggled, and both screens
were clicked through by hand (OnExitMatchScene 10:09:26, 10:14:16). Arena's
result screen ignores input until MatchSceneManager.AnimateMatchEnd has
finished; the Mac bridge now waits for that and leaves the screen itself.
"""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image
from tests.test_auto_queue import arm, navigator, proposal, step

from arenamcp import auto_queue

NOT_READY = {"ok": True, "is_open": True, "exit_ready": False, "click_ready": False}
READY = {"ok": True, "is_open": True, "exit_ready": True, "click_ready": True}
CLOSED = {"ok": True, "is_open": False, "exit_ready": False, "click_ready": False}
VICTORY = proposal("dismiss_result", "results", label="VICTORY", result_title="VICTORY", result_visible=True)


def result_bridge(states, leave=None):
    return SimpleNamespace(
        connected=True,
        client_runtime="il2cpp-macos",
        get_survey=Mock(return_value={"ok": True, "is_open": False}),
        dismiss_survey=Mock(),
        get_match_end=Mock(side_effect=list(states)),
        leave_match=Mock(return_value=leave or {"ok": True}),
    )


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr("arenamcp.auto_queue.sys.platform", "darwin")


def test_bridge_waits_for_the_result_screen_then_leaves_it_without_a_click(mac):
    nav, controller, backend, _, statuses = navigator()
    nav._bridge = result_bridge([NOT_READY, NOT_READY, READY, CLOSED])
    arm(nav)
    step(nav)
    step(nav)
    nav._bridge.leave_match.assert_not_called()
    assert statuses[-1] == "Waiting for Arena's result screen to accept input"
    step(nav)
    nav._bridge.leave_match.assert_called_once_with()
    assert statuses[-1] == "Leaving the result screen"
    # No screenshot, model call or click was needed for the result screen.
    controller.capture.assert_not_called()
    backend.complete_with_image.assert_not_called()
    assert not nav.paused_reason
    # The screen closed: screenshots take the next screen, told the result was left.
    step(nav)
    controller.execute.assert_called_once()
    request = json.loads(backend.complete_with_image.call_args.args[1])
    assert request["awaiting_result_dismissal"] is True
    assert request["previous_actions"] == [{"action": "leave_match", "label": "result screen (bridge)"}]
    assert nav.get_debug_info()["bridge_leaves"] == 1


def test_a_screen_still_closing_after_leave_is_not_left_again(mac):
    nav, controller, _, _, statuses = navigator()
    nav._bridge = result_bridge([READY, READY, NOT_READY])
    arm(nav)
    step(nav)
    step(nav)
    step(nav)
    nav._bridge.leave_match.assert_called_once_with()
    assert statuses[-1] == "Leaving the result screen"
    controller.capture.assert_not_called()


def test_waiting_for_the_result_screen_is_bounded_then_screenshots_take_over(mac):
    nav, controller, _, _, _ = navigator()
    nav._bridge = result_bridge([NOT_READY] * 3)
    arm(nav)
    step(nav)
    controller.capture.assert_not_called()
    nav._result_seen_at -= auto_queue._RESULT_READY_WAIT_S + 1
    step(nav)
    controller.execute.assert_called_once()
    assert nav.get_debug_info()["bridge_result_done"] is True
    # Once given up the bridge is not asked again for this navigation.
    step(nav)
    assert nav._bridge.get_match_end.call_count == 2
    nav._bridge.leave_match.assert_not_called()


def test_bridge_leaves_are_bounded_then_screenshots_take_over(mac):
    nav, controller, _, _, _ = navigator()
    nav._bridge = result_bridge([READY] * 5)
    arm(nav)
    for _ in range(3):
        step(nav)
        nav._bridge_left_at -= auto_queue._BRIDGE_LEAVE_SETTLE_S + 1
    assert nav._bridge.leave_match.call_count == 3
    controller.capture.assert_not_called()
    step(nav)
    assert nav._bridge.leave_match.call_count == 3
    controller.execute.assert_called_once()


@pytest.mark.parametrize("answer", [{"ok": True, "waiting": True}, {"ok": True, "survey_skipped": True}])
def test_a_leave_arena_did_not_take_yet_is_not_counted(mac, answer):
    nav, _, _, _, statuses = navigator()
    nav._bridge = result_bridge([READY, READY], leave=answer)
    arm(nav)
    step(nav)
    step(nav)
    assert nav._bridge.leave_match.call_count == 2
    assert nav.get_debug_info()["bridge_leaves"] == 0
    assert statuses[-1] == "Waiting for Arena's result screen to accept input"


def test_a_failed_leave_counts_toward_the_bound(mac):
    nav, _, _, _, _ = navigator()
    nav._bridge = result_bridge([READY] * 3, leave={"ok": False, "error": "timeout"})
    arm(nav)
    for _ in range(3):
        step(nav)
    assert nav.get_debug_info()["bridge_leaves"] == 3
    assert not nav._awaiting_result_dismissal


def test_no_result_screen_or_no_bridge_support_uses_screenshots(mac):
    nav, controller, _, _, _ = navigator()
    nav._bridge = result_bridge([CLOSED])
    arm(nav)
    step(nav)
    controller.execute.assert_called_once()
    nav, controller, _, _, _ = navigator()
    unsupported = result_bridge([None])
    nav._bridge = unsupported
    arm(nav)
    step(nav)
    controller.execute.assert_called_once()
    nav, controller, _, _, _ = navigator()
    offline = result_bridge([READY])
    offline.connected = False
    nav._bridge = offline
    arm(nav)
    step(nav)
    offline.get_match_end.assert_not_called()
    controller.execute.assert_called_once()


def test_a_new_match_before_leaving_hands_off_instead(mac):
    nav, _, _, _, _ = navigator()
    nav._bridge = result_bridge([READY])
    arm(nav)
    nav._get_game_state = lambda: {"match_id": "next"}
    nav._bridge.get_match_end.side_effect = lambda: READY
    nav._leave_result_via_bridge(nav._bridge, nav._generation, nav._abort)
    nav._bridge.leave_match.assert_not_called()
    assert not nav.active


def test_toggling_a_retry_starts_the_bridge_result_budget_again(mac):
    nav, _, _, _, _ = navigator()
    nav._bridge = result_bridge([READY] * 3)
    arm(nav)
    nav._bridge_leaves, nav._bridge_result_done = 3, True
    nav._pause("Arena navigation stopped making progress")
    nav.set_enabled(False)
    nav.set_enabled(True)
    assert nav.get_debug_info()["bridge_leaves"] == 0
    assert nav.get_debug_info()["bridge_result_done"] is False


def test_ignored_result_clicks_retry_by_themselves_without_the_bridge():
    # No bridge: the screenshot path's three ignored clicks pause, then look again.
    nav, controller, backend, _, statuses = navigator()
    backend.complete_with_image.return_value = VICTORY
    arm(nav)
    for _ in range(4):
        step(nav)
    assert controller.execute.call_count == 3
    assert nav.paused_reason and nav.retry_scheduled
    assert "automatic retry 1/3" in statuses[-1]
    nav._retry_at = 1.0
    nav.process_tick({"match_id": "ended"})
    nav._worker.join(5)
    assert controller.execute.call_count == 4
    assert not nav.paused_reason


def test_other_ignored_clicks_still_wait_for_the_user():
    nav, controller, _, _, _ = navigator()
    arm(nav)
    for _ in range(4):
        step(nav)
    assert controller.execute.call_count == 3
    assert nav.paused_reason and not nav.retry_scheduled


def test_a_click_that_changed_nothing_keeps_its_screens(tmp_path, monkeypatch):
    monkeypatch.setattr(auto_queue, "MISSED_CLICK_DIR", tmp_path)
    monkeypatch.setattr(auto_queue, "MISSED_CLICK_KEEP", 1)
    nav, controller, _, _, _ = navigator()
    # The screen as the user sees it, other apps' overlays included.
    controller.capture_on_screen = lambda frame: Image.new("RGB", (16, 9), "blue")
    arm(nav)
    step(nav)
    step(nav)
    saved = sorted(path.name for path in tmp_path.glob("*.png"))
    assert [name.split("_Play_")[1] for name in saved] == ["after.png", "before.png", "screen.png"]
    assert all("_start_queue_" in name for name in saved)
    assert (tmp_path / saved[0]).read_bytes().startswith(b"\x89PNG")
    step(nav)
    assert len(list(tmp_path.glob("*.png"))) == 3


def test_a_missing_on_screen_capture_still_keeps_the_game_screens(tmp_path, monkeypatch):
    monkeypatch.setattr(auto_queue, "MISSED_CLICK_DIR", tmp_path)
    nav, controller, _, _, _ = navigator()
    controller.capture_on_screen = Mock(side_effect=RuntimeError("screen capture failed"))
    arm(nav)
    step(nav)
    step(nav)
    assert len(list(tmp_path.glob("*_before.png"))) == len(list(tmp_path.glob("*_after.png"))) == 1
    assert not list(tmp_path.glob("*_screen.png"))


def test_screens_that_cannot_be_saved_never_break_navigation(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setattr(auto_queue, "MISSED_CLICK_DIR", blocker / "auto_queue")
    nav, controller, _, _, _ = navigator()
    arm(nav)
    step(nav)
    step(nav)
    assert controller.execute.call_count == 2
    assert not list(tmp_path.glob("**/*.png"))
