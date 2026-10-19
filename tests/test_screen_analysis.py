"""The desktop's "Analyze screen" command (pipe action ``analyze_screen``).

Since 6b32e95 (2026-08-31) deleted input_controller.find_mtga_hwnd, the
analysis called ``capture_mtga_png(bbox=bbox)`` without the required ``hwnd``:
every request died in a TypeError that the broad ``except`` turned into a
"Screenshot error" line, so the feature never reached a model.
"""

from __future__ import annotations

import io
from unittest.mock import create_autospec

import pytest
from PIL import Image


def _png(color=(40, 90, 160)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buffer, format="PNG")
    return buffer.getvalue()


class _UI:
    def __init__(self) -> None:
        self.logs: list[str] = []
        self.advice_calls: list[tuple[str, str]] = []

    def log(self, message: str) -> None:
        self.logs.append(message)

    def subtask(self, _label: str) -> None:
        pass

    def advice(self, text: str, source: str) -> None:
        self.advice_calls.append((text, source))


class _Backend:
    def __init__(self) -> None:
        self.images: list[bytes] = []

    def complete_with_image(self, system_prompt: str, user_message: str, png: bytes) -> str:
        self.images.append(png)
        return "KEEP: two lands and a two-drop."


def _engine():
    from arenamcp.standalone_diagnostics import _DiagnosticsMixin
    from arenamcp.standalone_windows import _StandaloneWindowsMixin

    class Engine(_DiagnosticsMixin):
        _is_garbled = staticmethod(_StandaloneWindowsMixin._is_garbled)

        def __init__(self) -> None:
            self._screenshot_analysis_in_progress = False
            self.backend_name = "online"
            self._vision_mapper = None
            self._backend = _Backend()
            self._coach = type("Coach", (), {"_backend": self._backend})()
            self._auto_speak = False
            self.ui = _UI()

        def get_game_state(self) -> dict:
            return {}

    return Engine()


@pytest.fixture
def capture(monkeypatch):
    from arenamcp import screen_capture
    from arenamcp.desktop import window_tracking

    # autospec keeps the fake's signature identical to the real function, so a
    # call that the real capture would reject fails here too.
    fake = create_autospec(screen_capture.capture_mtga_png, return_value=_png())
    monkeypatch.setattr(screen_capture, "capture_mtga_png", fake)
    monkeypatch.setattr(screen_capture, "find_mtga_hwnd", lambda: 4242)
    monkeypatch.setattr(window_tracking, "get_mtga_window_rect", lambda *a, **k: (10, 20, 300, 200))
    return fake


def test_analyze_screen_captures_arena_and_asks_the_model(capture):
    engine = _engine()

    engine.take_screenshot_analysis()

    assert not [line for line in engine.ui.logs if "Screenshot error" in line], engine.ui.logs
    capture.assert_called_once_with(4242, bbox=(10, 20, 310, 220))
    assert len(engine._backend.images) == 1
    assert engine.ui.advice_calls == [("KEEP: two lands and a two-drop.", "Visual Analysis")]
    assert engine._screenshot_analysis_in_progress is False


def test_analyze_screen_without_an_arena_window_still_captures(capture, monkeypatch):
    from arenamcp import screen_capture
    from arenamcp.desktop import window_tracking

    monkeypatch.setattr(screen_capture, "find_mtga_hwnd", lambda: None)
    monkeypatch.setattr(window_tracking, "get_mtga_window_rect", lambda *a, **k: None)
    engine = _engine()

    engine.take_screenshot_analysis()

    capture.assert_called_once_with(None, bbox=None)
    assert engine.ui.advice_calls


def test_find_mtga_hwnd_is_none_off_windows(monkeypatch):
    from arenamcp import screen_capture

    monkeypatch.setattr(screen_capture, "_IS_WINDOWS", False)
    assert screen_capture.find_mtga_hwnd() is None


@pytest.mark.parametrize(
    "bbox",
    [(10, 10, 10, 20), (20, 10, 10, 20), (None, 10, 20, 20)],
    ids=["zero-width", "left-past-right", "missing-edge"],
)
def test_screen_capture_bbox_validation(monkeypatch: pytest.MonkeyPatch, bbox) -> None:
    """An invalid bbox is never handed to ImageGrab; the full-screen grab runs instead."""
    from PIL import Image, ImageGrab

    from arenamcp import screen_capture

    # The kill switch is module-global: never let this test (or an earlier
    # one) leave capture disabled for the rest of the worker.
    monkeypatch.setattr(screen_capture, "_CAPTURE_DEAD", False)
    calls: list[dict] = []

    def mock_grab(*args, **kwargs):
        calls.append({"args": args, **kwargs})
        if args or "bbox" in kwargs:
            raise AssertionError(f"ImageGrab.grab got the invalid bbox {bbox}")
        return Image.new("RGB", (8, 8), (200, 120, 40))

    monkeypatch.setattr(ImageGrab, "grab", mock_grab)

    res = screen_capture.capture_mtga_png(hwnd=None, bbox=bbox)

    assert calls == [{"args": ()}]
    assert res is not None and res.startswith(b"\x89PNG")
    assert screen_capture._CAPTURE_DEAD is False
