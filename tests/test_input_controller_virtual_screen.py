from __future__ import annotations

import arenamcp.input_controller as input_controller


class _FakeUser32:
    def __init__(self, metrics: dict[int, int]) -> None:
        self._metrics = metrics

    def GetSystemMetrics(self, index: int) -> int:
        return self._metrics[index]


def _ensure_windows_mouse_constants(monkeypatch) -> None:
    """Inject MOUSEEVENTF_* constants on non-Windows hosts.

    The constants live inside ``if _IS_WINDOWS:`` in input_controller, so on
    Linux they don't exist as module attributes. Tests that exercise the
    SendInput code path need them present regardless of host OS.
    """
    monkeypatch.setattr(input_controller, "MOUSEEVENTF_MOVE", 0x0001, raising=False)
    monkeypatch.setattr(input_controller, "MOUSEEVENTF_ABSOLUTE", 0x8000, raising=False)
    monkeypatch.setattr(input_controller, "MOUSEEVENTF_VIRTUALDESK", 0x4000, raising=False)


def test_sendinput_backend_uses_virtual_desktop_origin(monkeypatch) -> None:
    _ensure_windows_mouse_constants(monkeypatch)
    monkeypatch.setattr(input_controller, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        input_controller,
        "user32",
        _FakeUser32(
            {
                0: 1920,
                1: 1080,
                76: 0,
                77: -2138,
                78: 4300,
                79: 3866,
            }
        ),
    )

    backend = input_controller.SendInputBackend()

    assert backend._abs_coords(0, -2138) == (0, 0)
    assert backend._abs_coords(4299, 1727) == (65535, 65535)


def test_sendinput_absolute_flags_include_virtual_desktop(monkeypatch) -> None:
    _ensure_windows_mouse_constants(monkeypatch)
    monkeypatch.setattr(input_controller, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        input_controller,
        "user32",
        _FakeUser32(
            {
                0: 1920,
                1: 1080,
                76: 0,
                77: 0,
                78: 1920,
                79: 1080,
            }
        ),
    )

    backend = input_controller.SendInputBackend()

    absolute_move = input_controller.MOUSEEVENTF_MOVE | input_controller.MOUSEEVENTF_ABSOLUTE

    assert backend._absolute_flags(absolute_move) == (
        absolute_move | input_controller.MOUSEEVENTF_VIRTUALDESK
    )
