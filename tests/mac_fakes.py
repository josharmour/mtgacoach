"""Shared fakes for the native-macOS screen input (auto-queue's clicks)."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image

from arenamcp.native_mac_input import DesktopFrame, GameWindow, NativeMacInput


def make_frame(*, window=None, color="green", size=(1440, 900)) -> DesktopFrame:
    """A fresh capture of a 720x450-point Arena window at (100, 50)."""
    return DesktopFrame(
        window or GameWindow(42, 123, (100, 50, 720, 450)),
        Image.new("RGB", size, color),
        time.monotonic(),
    )


def command(kind="click", **kwargs) -> dict:
    """A confident desktop action payload for ``DesktopAction.from_dict``."""
    return {
        "kind": kind,
        "point": [0.5, 0.6],
        "reason": "Select the visible card",
        "confidence": 0.95,
        **kwargs,
    }


def fake_native_controller() -> NativeMacInput:
    """A NativeMacInput whose Quartz events, focus and hit tests are recorded mocks."""
    controller = NativeMacInput.__new__(NativeMacInput)
    controller._quartz = SimpleNamespace(
        kCGEventMouseMoved=5,
        kCGEventLeftMouseDown=1,
        kCGEventLeftMouseUp=2,
        kCGEventLeftMouseDragged=6,
    )
    controller.check_permissions = Mock()
    controller._check_focus = Mock()
    controller._check_point = Mock()
    controller._mouse = Mock()
    controller.capture = Mock(side_effect=make_frame)
    return controller
