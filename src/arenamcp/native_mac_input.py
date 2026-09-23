"""Window capture and input for the native macOS Arena client."""

from __future__ import annotations

import ctypes
import io
import math
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageStat


class DesktopUnavailable(RuntimeError):
    """The game cannot currently receive desktop input."""


class _ProcessSerialNumber(ctypes.Structure):
    _fields_ = [("high", ctypes.c_uint32), ("low", ctypes.c_uint32)]


class _ForegroundProcess:
    """Query focus synchronously without NSWorkspace's main-run-loop cache."""

    def __init__(self) -> None:
        self._services = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        self._services.GetFrontProcess.argtypes = [ctypes.POINTER(_ProcessSerialNumber)]
        self._services.GetFrontProcess.restype = ctypes.c_int16
        self._services.GetProcessPID.argtypes = [
            ctypes.POINTER(_ProcessSerialNumber),
            ctypes.POINTER(ctypes.c_int),
        ]
        self._services.GetProcessPID.restype = ctypes.c_int32

    def pid(self) -> int:
        serial = _ProcessSerialNumber()
        process_id = ctypes.c_int()
        if self._services.GetFrontProcess(ctypes.byref(serial)) != 0:
            raise DesktopUnavailable("Cannot verify the foreground application; waiting before input.")
        if self._services.GetProcessPID(ctypes.byref(serial), ctypes.byref(process_id)) != 0:
            raise DesktopUnavailable("Cannot verify the foreground process; waiting before input.")
        return process_id.value


@dataclass(frozen=True)
class GameWindow:
    window_id: int
    pid: int
    bounds: tuple[float, float, float, float]

    def screen_point(self, point: tuple[float, float]) -> tuple[float, float]:
        left, top, width, height = self.bounds
        return left + point[0] * width, top + point[1] * height


@dataclass
class DesktopFrame:
    window: GameWindow
    image: Image.Image
    captured_at: float

    def png(self) -> bytes:
        buffer = io.BytesIO()
        self.image.save(buffer, format="PNG")
        return buffer.getvalue()


@dataclass(frozen=True)
class DesktopAction:
    kind: str
    reason: str
    confidence: float
    point: tuple[float, float] | None = None
    end: tuple[float, float] | None = None
    key: str = ""
    text: str = ""
    amount: int = 0

    @classmethod
    def from_dict(cls, payload: Any) -> DesktopAction:
        if not isinstance(payload, dict):
            raise ValueError("Expected one JSON action object")
        kind = payload.get("kind")
        if kind not in {"click", "double_click", "drag", "move", "key", "text", "scroll", "wait", "stop"}:
            raise ValueError("Unsupported desktop action")
        confidence = payload.get("confidence")
        if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("Action confidence must be between 0 and 1")
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Action must explain its visible target")

        def coordinate(name: str) -> tuple[float, float]:
            value = payload.get(name)
            if not isinstance(value, list) or len(value) != 2:
                raise ValueError(f"{name} must contain two normalized coordinates")
            if any(
                type(axis) not in (int, float) or not math.isfinite(axis) or not 0 < axis < 1
                for axis in value
            ):
                raise ValueError(f"{name} must be strictly inside the game window")
            return float(value[0]), float(value[1])

        point = coordinate("point") if kind in {"click", "double_click", "drag", "move", "scroll"} else None
        end = coordinate("end") if kind == "drag" else None
        key = payload.get("key", "")
        if kind == "key" and key not in MAC_KEY_CODES:
            raise ValueError("Unsupported game key")
        text = payload.get("text", "")
        if kind == "text" and (
            not isinstance(text, str) or not text.isascii() or not text.isdigit() or len(text) > 3
        ):
            raise ValueError("Text input must be a numeric game choice of at most three digits")
        amount = payload.get("amount", 0)
        if kind == "scroll" and (type(amount) is not int or amount == 0 or not -6 <= amount <= 6):
            raise ValueError("Scroll amount must be a nonzero integer between -6 and 6")
        return cls(kind, reason[:500], float(confidence), point, end, key, text, amount)


MAC_KEY_CODES = {"enter": 36, "tab": 48, "space": 49, "backspace": 51, "escape": 53, "delete": 117}


def frame_changed(before: DesktopFrame, after: DesktopFrame, action: DesktopAction) -> bool:
    """Reject stale screen layouts while tolerating small ambient animations."""
    if before.window != after.window:
        return True
    images = [frame.image.convert("RGB").resize((320, 180)) for frame in (before, after)]
    difference = ImageChops.difference(*images)
    if sum(ImageStat.Stat(difference).mean) / 3 > 25:
        return True
    for point in (action.point, action.end):
        if point is None:
            continue
        horizontal, vertical = point[0] * 320, point[1] * 180
        crop = difference.crop(
            (max(0, horizontal - 10), max(0, vertical - 8), min(320, horizontal + 10), min(180, vertical + 8))
        )
        if sum(ImageStat.Stat(crop).mean) / 3 > 25:
            return True
    return False


class NativeMacInput:
    """Use Quartz screen points, independent of the screenshot's Retina scale."""

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise RuntimeError("Native Mac input requires macOS")
        try:
            import ApplicationServices
            import Quartz
        except ImportError as exc:
            raise RuntimeError(
                "Install mtgacoach's macOS dependencies (pyobjc-framework-Cocoa, "
                "pyobjc-framework-Quartz and pyobjc-framework-ApplicationServices)"
            ) from exc
        self._quartz = Quartz
        self._accessibility = ApplicationServices
        self._system_element = ApplicationServices.AXUIElementCreateSystemWide()
        ApplicationServices.AXUIElementSetMessagingTimeout(self._system_element, 0.5)
        self._foreground = _ForegroundProcess()

    def check_permissions(self, *, request: bool = False) -> None:
        missing = []
        for preflight, acquire, label in (
            ("CGPreflightPostEventAccess", "CGRequestPostEventAccess", "Accessibility"),
            ("CGPreflightScreenCaptureAccess", "CGRequestScreenCaptureAccess", "Screen Recording"),
        ):
            if not getattr(self._quartz, preflight)():
                if request:
                    getattr(self._quartz, acquire)()
                missing.append(label)
        if missing:
            raise DesktopUnavailable(
                "Allow "
                + " and ".join(missing)
                + " for the coach's Python/app in System Settings → Privacy & Security, then restart the coach."
            )

    def _windows(self) -> list[dict]:
        return (
            self._quartz.CGWindowListCopyWindowInfo(
                self._quartz.kCGWindowListOptionOnScreenOnly, self._quartz.kCGNullWindowID
            )
            or []
        )

    def _game_window(self) -> GameWindow:
        for info in self._windows():
            if info.get("kCGWindowOwnerName") != "MTGA" or info.get("kCGWindowLayer") != 0:
                continue
            bounds = info.get("kCGWindowBounds") or {}
            width, height = float(bounds.get("Width", 0)), float(bounds.get("Height", 0))
            if width >= 400 and height >= 300:
                return GameWindow(
                    int(info["kCGWindowNumber"]),
                    int(info["kCGWindowOwnerPID"]),
                    (float(bounds["X"]), float(bounds["Y"]), width, height),
                )
        raise DesktopUnavailable("Open the native Steam Arena client and keep its game window visible.")

    def _check_focus(self, window: GameWindow) -> None:
        if self._foreground.pid() != window.pid:
            raise DesktopUnavailable("Autoplay is waiting: bring Arena to the foreground.")
        if self._game_window() != window:
            raise DesktopUnavailable("Arena's window moved; waiting for a fresh screenshot.")

    def capture(self) -> DesktopFrame:
        self.check_permissions()
        window = self._game_window()
        self._check_focus(window)
        with tempfile.TemporaryDirectory(prefix="mtgacoach-capture-") as directory:
            path = Path(directory) / "arena.png"
            result = subprocess.run(
                ["/usr/sbin/screencapture", "-x", "-o", "-l", str(window.window_id), str(path)],
                capture_output=True,
                timeout=5,
                check=False,
            )
            if result.returncode or not path.is_file():
                raise DesktopUnavailable(
                    "Arena capture failed. Check Screen Recording permission and keep Arena visible."
                )
            with Image.open(path) as original:
                screenshot = original.convert("RGB")
            screenshot.thumbnail((1600, 1000))
        self._check_focus(window)
        return DesktopFrame(window, screenshot, time.monotonic())

    def _check_point(self, window: GameWindow, point: tuple[float, float]) -> None:
        """Hit-test input ownership, including transparent click-through overlays."""
        self._check_focus(window)
        horizontal, vertical = window.screen_point(point)
        error, element = self._accessibility.AXUIElementCopyElementAtPosition(
            self._system_element, horizontal, vertical, None
        )
        if error or element is None:
            raise DesktopUnavailable("Cannot verify the input target; check Accessibility permission.")
        error, process_id = self._accessibility.AXUIElementGetPid(element, None)
        if error:
            raise DesktopUnavailable("Cannot verify which application owns the input target.")
        if process_id != window.pid:
            raise DesktopUnavailable("Another window covers the input target; uncover Arena to continue.")

    def _mouse(self, event_type: int, position: tuple[float, float], clicks: int = 1) -> None:
        quartz = self._quartz
        event = quartz.CGEventCreateMouseEvent(None, event_type, position, quartz.kCGMouseButtonLeft)
        if event is None:
            raise DesktopUnavailable("macOS could not create a mouse event")
        quartz.CGEventSetIntegerValueField(event, quartz.kCGMouseEventClickState, clicks)
        quartz.CGEventPost(quartz.kCGHIDEventTap, event)

    def execute(self, frame: DesktopFrame, action: DesktopAction, aborted: threading.Event) -> bool:
        if aborted.is_set() or action.kind in {"wait", "stop"}:
            return False
        self.check_permissions()
        self._check_focus(frame.window)
        if time.monotonic() - frame.captured_at > 2:
            raise DesktopUnavailable("Screenshot expired before input; recapturing Arena.")
        for point in (action.point, action.end):
            if point is not None:
                self._check_point(frame.window, point)
        quartz = self._quartz
        position = frame.window.screen_point(action.point) if action.point else None
        if position is not None:
            self._mouse(quartz.kCGEventMouseMoved, position)
            clicks_card = action.kind in {"click", "double_click", "drag"}
            if aborted.wait(0.3 if clicks_card else 0.06):
                return False
            self._check_point(frame.window, action.point)
            if clicks_card:
                # Arena expands/reflows the hand on hover. The screenshot checked
                # before moving the pointer no longer proves which card is here.
                hovered = self.capture()
                if aborted.is_set():
                    return False
                if frame_changed(frame, hovered, action):
                    raise DesktopUnavailable("Arena's UI changed on hover; observing again before input.")
        if action.kind == "move":
            return True
        if action.kind in {"click", "double_click", "drag"}:
            for click_index in range(1, 3 if action.kind == "double_click" else 2):
                if aborted.is_set():
                    return False
                self._check_point(frame.window, action.point)
                self._mouse(quartz.kCGEventLeftMouseDown, position, click_index)
                try:
                    if action.kind == "drag":
                        for step in range(1, 13):
                            if aborted.wait(0.025):
                                return False
                            fraction = step / 12
                            intermediate = tuple(
                                start + (finish - start) * fraction
                                for start, finish in zip(action.point, action.end, strict=True)
                            )
                            self._check_point(frame.window, intermediate)
                            position = frame.window.screen_point(intermediate)
                            self._mouse(quartz.kCGEventLeftMouseDragged, position)
                    else:
                        # Short presses were intermittently missed by native Arena.
                        aborted.wait(0.12)
                finally:
                    self._mouse(quartz.kCGEventLeftMouseUp, position, click_index)
                if aborted.wait(0.12):
                    return False
            return True
        if action.kind in {"key", "text"}:
            keycode = MAC_KEY_CODES[action.key] if action.kind == "key" else 0
            pressed = quartz.CGEventCreateKeyboardEvent(None, keycode, True)
            released = quartz.CGEventCreateKeyboardEvent(None, keycode, False)
            if pressed is None or released is None:
                raise DesktopUnavailable("macOS could not create a keyboard event")
            if action.kind == "text":
                for event in (pressed, released):
                    quartz.CGEventKeyboardSetUnicodeString(event, len(action.text), action.text)
            self._check_focus(frame.window)
            if aborted.is_set():
                return False
            quartz.CGEventPost(quartz.kCGHIDEventTap, pressed)
            try:
                aborted.wait(0.04)
            finally:
                quartz.CGEventPost(quartz.kCGHIDEventTap, released)
            return True
        if action.kind == "scroll":
            event = quartz.CGEventCreateScrollWheelEvent(
                None, quartz.kCGScrollEventUnitLine, 1, action.amount
            )
            if event is None:
                raise DesktopUnavailable("macOS could not create a scroll event")
            self._check_point(frame.window, action.point)
            if aborted.is_set():
                return False
            quartz.CGEventPost(quartz.kCGHIDEventTap, event)
            return True
        return False
