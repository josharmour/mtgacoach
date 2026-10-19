"""Window capture and input for the native macOS Arena client."""

from __future__ import annotations

import ctypes
import io
import logging
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

logger = logging.getLogger(__name__)

# The keyboard, mouse and trackpad event types that mean a person is at the Mac.
# kCGAnyInputEventType also counts kCGEventNull, which apps post to keep the
# display awake: on 2026-10-18 `pmset -g assertions` showed MTGA holding
# "com.apple.iohideventsystem.queue.tickle.nxevent ... process:MTGA", and
# sampled HID-state idle for kCGAnyInputEventType (= kCGEventNull) reset every
# ~10 s while keyDown idle was 4353 s and mouseMoved 318 s. With Arena running
# the "any input" idle never reaches 20 s, so Arena was never brought forward:
# auto-queue sat on the Victory screen 19:02:12-19:16:09 with no /usr/bin/open
# fallback in the unified log and no MTGA activation until it was clicked.
_USER_INPUT_EVENTS = (
    "kCGEventKeyDown",
    "kCGEventKeyUp",
    "kCGEventFlagsChanged",
    "kCGEventMouseMoved",
    "kCGEventLeftMouseDown",
    "kCGEventLeftMouseDragged",
    "kCGEventRightMouseDown",
    "kCGEventRightMouseDragged",
    "kCGEventOtherMouseDown",
    "kCGEventOtherMouseDragged",
    "kCGEventScrollWheel",
)


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
    """One left click at a normalized point of the game window.

    Auto-queue's post-match navigation is the only screen input left; the
    gameplay autopilot acts through the GRE bridge (docs/DECISIONS.md
    2026-10-18), so the other input kinds it used are gone.
    """

    kind: str
    reason: str
    confidence: float
    point: tuple[float, float]

    @classmethod
    def from_dict(cls, payload: Any) -> DesktopAction:
        if not isinstance(payload, dict):
            raise ValueError("Expected one JSON action object")
        kind = payload.get("kind")
        if kind != "click":
            raise ValueError("Unsupported desktop action")
        confidence = payload.get("confidence")
        if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("Action confidence must be between 0 and 1")
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Action must explain its visible target")
        value = payload.get("point")
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError("point must contain two normalized coordinates")
        if any(
            type(axis) not in (int, float) or not math.isfinite(axis) or not 0 < axis < 1 for axis in value
        ):
            raise ValueError("point must be strictly inside the game window")
        return cls(kind, reason[:500], float(confidence), (float(value[0]), float(value[1])))


def frame_changed(before: DesktopFrame, after: DesktopFrame, action: DesktopAction) -> bool:
    """Reject stale screen layouts while tolerating small ambient animations."""
    if before.window != after.window:
        return True
    images = [frame.image.convert("RGB").resize((320, 180)) for frame in (before, after)]
    difference = ImageChops.difference(*images)
    if sum(ImageStat.Stat(difference).mean) / 3 > 25:
        return True
    horizontal, vertical = action.point[0] * 320, action.point[1] * 180
    crop = difference.crop(
        (max(0, horizontal - 10), max(0, vertical - 8), min(320, horizontal + 10), min(180, vertical + 8))
    )
    return sum(ImageStat.Stat(crop).mean) / 3 > 25


class NativeMacInput:
    """Use Quartz screen points, independent of the screenshot's Retina scale."""

    def __init__(self, *, raise_when_idle_s: float | None = None) -> None:
        """``raise_when_idle_s``: bring Arena to the front once the user has not
        touched keyboard or mouse for this long, instead of waiting for them to
        (auto-queue only; None keeps the old wait)."""
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
        self._raise_when_idle_s = raise_when_idle_s
        # Why the last focus check could not proceed ("" once Arena is in
        # front); auto-queue's still-waiting log line reports it.
        self.focus_wait = ""

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
        front = self._foreground.pid()
        if front != window.pid and not self._bring_forward_when_idle(window, front):
            idle = getattr(self, "_raise_when_idle_s", None)
            raise DesktopUnavailable(
                "Autoplay is waiting: bring Arena to the foreground"
                + (f" (or leave the keyboard and mouse for {idle:.0f}s)." if idle else ".")
            )
        self.focus_wait = ""
        if self._game_window() != window:
            raise DesktopUnavailable("Arena's window moved; waiting for a fresh screenshot.")

    def _idle_seconds(self) -> float:
        """Seconds since the last keyboard, mouse or trackpad input (not display-wake tickles)."""
        quartz = self._quartz
        kinds = [getattr(quartz, name) for name in _USER_INPUT_EVENTS if hasattr(quartz, name)]
        state = quartz.kCGEventSourceStateHIDSystemState
        if not kinds:
            return float(quartz.CGEventSourceSecondsSinceLastEventType(state, quartz.kCGAnyInputEventType))
        return min(float(quartz.CGEventSourceSecondsSinceLastEventType(state, kind)) for kind in kinds)

    @staticmethod
    def _app_name(pid: int) -> str:
        try:
            from AppKit import NSRunningApplication

            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            return str(app.localizedName() or "") if app is not None else ""
        except Exception:
            return ""

    def _bring_forward_when_idle(self, window: GameWindow, front_pid: int | None = None) -> bool:
        """Activate Arena when the user has left keyboard and mouse alone; True once it is in front.

        Auto-queue's clicks are real HID events that move the pointer, so they
        wait while someone is using another app. 2026-10-13 00:09-00:14: with
        Auto-Queue on and the coach's terminal in front, the Victory and Reward
        screens sat until Arena was clicked by hand. Each outcome is kept in
        ``focus_wait`` so a long wait says why it is still waiting.
        """
        name = self._app_name(front_pid) if front_pid is not None else ""
        front = f"{name or 'another app'} (pid {front_pid})" if front_pid is not None else "another app"
        threshold = getattr(self, "_raise_when_idle_s", None)
        if not threshold:
            self.focus_wait = f"Arena is behind {front}"
            return False
        idle = self._idle_seconds()
        if idle < threshold:
            self.focus_wait = (
                f"Arena is behind {front}; keyboard/mouse used {idle:.0f}s ago "
                f"(Arena is brought forward after {threshold:.0f}s idle)"
            )
            return False
        from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication

        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(window.pid)
        if app is None:
            self.focus_wait = f"Arena (pid {window.pid}) is not a running application"
            return False
        app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
        if self._wait_for_front(window, 0.8):
            logger.info("Brought Arena forward from %s after %.0fs without keyboard/mouse input", front, idle)
            return True
        # Cooperative activation can refuse a background request; LaunchServices
        # activates the running instance (it never starts a second Arena).
        bundle = app.bundleURL()
        if bundle is not None:
            subprocess.run(["/usr/bin/open", str(bundle.path())], capture_output=True, timeout=5, check=False)
        if self._wait_for_front(window, 1.5):
            logger.info("Brought Arena forward from %s with LaunchServices after %.0fs idle", front, idle)
            return True
        self.focus_wait = f"macOS refused to bring Arena forward from {front} after {idle:.0f}s idle"
        return False

    def _wait_for_front(self, window: GameWindow, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while True:
            if self._foreground.pid() == window.pid:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)

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

    def _mouse(self, event_type: int, position: tuple[float, float]) -> None:
        quartz = self._quartz
        event = quartz.CGEventCreateMouseEvent(None, event_type, position, quartz.kCGMouseButtonLeft)
        if event is None:
            raise DesktopUnavailable("macOS could not create a mouse event")
        quartz.CGEventSetIntegerValueField(event, quartz.kCGMouseEventClickState, 1)
        quartz.CGEventPost(quartz.kCGHIDEventTap, event)

    def execute(self, frame: DesktopFrame, action: DesktopAction, aborted: threading.Event) -> bool:
        if aborted.is_set():
            return False
        self.check_permissions()
        self._check_focus(frame.window)
        if time.monotonic() - frame.captured_at > 2:
            raise DesktopUnavailable("Screenshot expired before input; recapturing Arena.")
        self._check_point(frame.window, action.point)
        quartz = self._quartz
        position = frame.window.screen_point(action.point)
        self._mouse(quartz.kCGEventMouseMoved, position)
        if aborted.wait(0.3):
            return False
        self._check_point(frame.window, action.point)
        # Arena expands/reflows controls on hover. The screenshot checked
        # before moving the pointer no longer proves what is under it.
        hovered = self.capture()
        if aborted.is_set():
            return False
        if frame_changed(frame, hovered, action):
            raise DesktopUnavailable("Arena's UI changed on hover; observing again before input.")
        if aborted.is_set():
            return False
        self._check_point(frame.window, action.point)
        self._mouse(quartz.kCGEventLeftMouseDown, position)
        try:
            # Short presses were intermittently missed by native Arena.
            aborted.wait(0.12)
        finally:
            self._mouse(quartz.kCGEventLeftMouseUp, position)
        return not aborted.wait(0.12)
