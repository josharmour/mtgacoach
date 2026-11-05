"""Permission preflight must never invoke a capture/request API when denied."""

from unittest.mock import Mock

import pytest

from arenamcp import screen_capture
from arenamcp.autopilot_bug_capture import capture_mtga_screenshot


@pytest.fixture
def preflight(monkeypatch):
    check = Mock(return_value=False)
    core = Mock(CGPreflightScreenCaptureAccess=check)
    monkeypatch.setattr(screen_capture, "_IS_MACOS", True)
    monkeypatch.setattr(screen_capture.ctypes, "CDLL", Mock(return_value=core))
    return check


def test_denied_capture_never_looks_up_or_grabs_a_window(preflight, monkeypatch, tmp_path):
    locator, grab = Mock(), Mock()
    monkeypatch.setattr("arenamcp.desktop.window_tracking.get_mtga_window_rect", locator)
    monkeypatch.setattr("PIL.ImageGrab.grab", grab)
    for _ in range(2):
        with pytest.raises(PermissionError, match="this Coach build"):
            capture_mtga_screenshot(tmp_path, "report")
    locator.assert_not_called()
    grab.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_access_is_rechecked_after_user_grants_it(preflight):
    with pytest.raises(PermissionError):
        screen_capture.require_screen_capture_access()
    preflight.return_value = True
    screen_capture.require_screen_capture_access()
    assert preflight.call_count == 2


def test_unavailable_permission_api_fails_without_capture(monkeypatch):
    monkeypatch.setattr(screen_capture, "_IS_MACOS", True)
    monkeypatch.setattr(screen_capture.ctypes, "CDLL", Mock(side_effect=OSError("unavailable")))
    with pytest.raises(PermissionError):
        screen_capture.require_screen_capture_access()


def test_denied_vision_capture_can_recover_later(preflight, monkeypatch):
    grab = Mock()
    monkeypatch.setattr("PIL.ImageGrab.grab", grab)
    monkeypatch.setattr(screen_capture, "_CAPTURE_DEAD", False)
    assert screen_capture.capture_mtga_png(None) is None
    assert not screen_capture._CAPTURE_DEAD
    grab.assert_not_called()


def test_non_mac_platform_does_not_load_coregraphics(monkeypatch):
    load = Mock()
    monkeypatch.setattr(screen_capture, "_IS_MACOS", False)
    monkeypatch.setattr(screen_capture.ctypes, "CDLL", load)
    screen_capture.require_screen_capture_access()
    load.assert_not_called()


@pytest.mark.parametrize("allowed", [True, False])
def test_explicit_request_rechecks_current_process_access(monkeypatch, allowed):
    core = Mock(CGPreflightScreenCaptureAccess=Mock(return_value=allowed))
    monkeypatch.setattr(screen_capture, "_IS_MACOS", True)
    monkeypatch.setattr(screen_capture.ctypes, "CDLL", Mock(return_value=core))
    assert screen_capture.request_screen_capture_access() is allowed
    core.CGRequestScreenCaptureAccess.assert_called_once()
    core.CGPreflightScreenCaptureAccess.assert_called_once()
