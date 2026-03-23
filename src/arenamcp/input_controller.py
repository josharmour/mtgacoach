"""Input Simulation for Autopilot Mode.

Backend abstraction with fallback chain:
  1. SendInputBackend  — ctypes SendInput (zero deps, best for DirectX/Unity)
  2. DirectInputBackend — pydirectinput-rgx (DirectInput scan codes)
  3. PyAutoGuiBackend   — pyautogui (legacy fallback)

Safety features (bounds checking, focus verification, delays) live in
InputController, not in backends.
"""

import ctypes
import ctypes.wintypes
import logging
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Win32 helpers (shared by InputController and ScreenMapper)
# ---------------------------------------------------------------------------

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    # Make the process DPI aware so coordinates match physical pixels
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1) # PROCESS_SYSTEM_DPI_AWARE
    except Exception:
        user32.SetProcessDPIAware()
else:
    user32 = None
    kernel32 = None

# --- Window management ---

_MTGA_TITLES = ["MTGA", "Magic: The Gathering Arena"]


def find_mtga_hwnd() -> Optional[int]:
    """Find the MTGA window handle using FindWindowW."""
    if not _IS_WINDOWS:
        return None
    for title in _MTGA_TITLES:
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            return hwnd
    # Fallback: enumerate windows looking for partial match
    return _enum_find_mtga()


def _enum_find_mtga() -> Optional[int]:
    """Enumerate all top-level windows to find MTGA (fallback)."""
    if not _IS_WINDOWS:
        return None

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.wintypes.BOOL,
        ctypes.wintypes.HWND,
        ctypes.wintypes.LPARAM,
    )
    result = [None]

    def callback(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            for t in _MTGA_TITLES:
                if t.lower() in title.lower():
                    result[0] = hwnd
                    return False  # stop enumerating
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return result[0]


def get_client_rect(hwnd: int) -> Optional[tuple[int, int, int, int]]:
    """Get the client area of a window as (left, top, width, height).

    Uses GetClientRect + ClientToScreen to get absolute screen coordinates
    of the client area (excludes title bar and borders).
    """
    if not _IS_WINDOWS:
        return None

    rect = ctypes.wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None

    # Convert client (0,0) to screen coordinates
    point = ctypes.wintypes.POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(point)):
        return None

    return (point.x, point.y, rect.right - rect.left, rect.bottom - rect.top)


def force_foreground(hwnd: int) -> bool:
    """Reliably bring a window to the foreground.

    Uses AttachThreadInput trick to bypass Windows' SetForegroundWindow
    restrictions when the calling process is not the foreground window.
    """
    if not _IS_WINDOWS:
        return False

    try:
        current_hwnd = user32.GetForegroundWindow()
        if current_hwnd == hwnd:
            return True  # Already foreground

        current_tid = user32.GetWindowThreadProcessId(current_hwnd, None)
        target_tid = user32.GetWindowThreadProcessId(hwnd, None)

        if current_tid != target_tid:
            user32.AttachThreadInput(current_tid, target_tid, True)
            try:
                # Restore if minimized
                SW_RESTORE = 9
                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, SW_RESTORE)

                user32.SetForegroundWindow(hwnd)
                user32.BringWindowToTop(hwnd)
            finally:
                user32.AttachThreadInput(current_tid, target_tid, False)
        else:
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)
            user32.SetForegroundWindow(hwnd)

        # Windows may delay focus switch — poll briefly
        for _ in range(5):
            if user32.GetForegroundWindow() == hwnd:
                return True
            time.sleep(0.02)
        return user32.GetForegroundWindow() == hwnd
    except Exception as e:
        logger.error(f"force_foreground failed: {e}")
        return False


# ---------------------------------------------------------------------------
# SendInput ctypes structures (for SendInputBackend)
# ---------------------------------------------------------------------------

if _IS_WINDOWS:
    INPUT_MOUSE = 0
    INPUT_KEYBOARD = 1

    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_ABSOLUTE = 0x8000

    KEYEVENTF_SCANCODE = 0x0008
    KEYEVENTF_KEYUP = 0x0002

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", ctypes.wintypes.LONG),
            ("dy", ctypes.wintypes.LONG),
            ("mouseData", ctypes.wintypes.DWORD),
            ("dwFlags", ctypes.wintypes.DWORD),
            ("time", ctypes.wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", ctypes.wintypes.WORD),
            ("wScan", ctypes.wintypes.WORD),
            ("dwFlags", ctypes.wintypes.DWORD),
            ("time", ctypes.wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class _INPUT_UNION(ctypes.Union):
        _fields_ = [
            ("mi", MOUSEINPUT),
            ("ki", KEYBDINPUT),
        ]

    class INPUT(ctypes.Structure):
        _fields_ = [
            ("type", ctypes.wintypes.DWORD),
            ("union", _INPUT_UNION),
        ]

    # Key name -> scan code mapping (common keys for MTGA)
    _SCAN_CODES = {
        "space": 0x39,
        "enter": 0x1C,
        "return": 0x1C,
        "escape": 0x01,
        "esc": 0x01,
        "tab": 0x0F,
        "z": 0x2C,
        "x": 0x2D,
        "c": 0x2E,
        "q": 0x10,
        "w": 0x11,
        "e": 0x12,
        "1": 0x02,
        "2": 0x03,
        "3": 0x04,
        "4": 0x05,
        "5": 0x06,
    }


# ---------------------------------------------------------------------------
# Click result
# ---------------------------------------------------------------------------

@dataclass
class ClickResult:
    """Result of an input action."""
    success: bool
    x: int = 0
    y: int = 0
    description: str = ""
    error: str = ""

    def __str__(self) -> str:
        if self.success:
            return f"OK: {self.description} at ({self.x}, {self.y})"
        return f"FAIL: {self.description} - {self.error}"


# ---------------------------------------------------------------------------
# Backend ABC
# ---------------------------------------------------------------------------

class InputBackend(ABC):
    """Abstract base for input simulation backends."""

    name: str = "abstract"

    @abstractmethod
    def click(self, x: int, y: int) -> bool:
        """Click at absolute screen coordinates."""

    @abstractmethod
    def double_click(self, x: int, y: int) -> bool:
        """Double-click at absolute screen coordinates."""

    @abstractmethod
    def move_to(self, x: int, y: int, duration: float) -> bool:
        """Move mouse to absolute screen coordinates."""

    @abstractmethod
    def press_key(self, key: str) -> bool:
        """Press and release a keyboard key."""

    @abstractmethod
    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float) -> bool:
        """Drag from one point to another."""


# ---------------------------------------------------------------------------
# Backend 1: SendInput (ctypes, zero dependencies)
# ---------------------------------------------------------------------------

class SendInputBackend(InputBackend):
    """Uses ctypes SendInput with MOUSEEVENTF_ABSOLUTE and scan codes.

    This is the preferred backend for DirectX/Unity games like MTGA because
    it generates hardware-level input events that games actually process,
    unlike pyautogui's SetCursorPos + mouse_event approach.
    """

    name = "SendInput"

    def __init__(self):
        if not _IS_WINDOWS:
            raise RuntimeError("SendInputBackend requires Windows")
        # Get screen dimensions for absolute coordinate normalization
        self._screen_w = user32.GetSystemMetrics(0)
        self._screen_h = user32.GetSystemMetrics(1)
        logger.info(
            f"SendInputBackend initialized (screen: {self._screen_w}x{self._screen_h})"
        )

    def _abs_coords(self, x: int, y: int) -> tuple[int, int]:
        """Convert screen pixels to normalized 0-65535 range for MOUSEEVENTF_ABSOLUTE."""
        nx = int(x * 65535 / self._screen_w)
        ny = int(y * 65535 / self._screen_h)
        return nx, ny

    def _send_mouse(self, dx: int, dy: int, flags: int) -> bool:
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.union.mi.dx = dx
        inp.union.mi.dy = dy
        inp.union.mi.mouseData = 0
        inp.union.mi.dwFlags = flags
        inp.union.mi.time = 0
        inp.union.mi.dwExtraInfo = None
        return user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp)) == 1

    def _send_key(self, scan_code: int, key_up: bool = False) -> bool:
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.union.ki.wVk = 0
        inp.union.ki.wScan = scan_code
        inp.union.ki.dwFlags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if key_up else 0)
        inp.union.ki.time = 0
        inp.union.ki.dwExtraInfo = None
        return user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp)) == 1

    def move_to(self, x: int, y: int, duration: float) -> bool:
        nx, ny = self._abs_coords(x, y)
        return self._send_mouse(
            nx, ny, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
        )

    def click(self, x: int, y: int) -> bool:
        nx, ny = self._abs_coords(x, y)
        # Move + click in sequence
        self._send_mouse(nx, ny, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE)
        time.sleep(0.02)
        self._send_mouse(nx, ny, MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_LEFTDOWN)
        time.sleep(0.02)
        return self._send_mouse(nx, ny, MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_LEFTUP)

    def double_click(self, x: int, y: int) -> bool:
        self.click(x, y)
        time.sleep(0.05)
        return self.click(x, y)

    def press_key(self, key: str) -> bool:
        scan = _SCAN_CODES.get(key.lower())
        if scan is None:
            logger.warning(f"SendInput: no scan code for key '{key}'")
            return False
        self._send_key(scan, key_up=False)
        time.sleep(0.02)
        return self._send_key(scan, key_up=True)

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float) -> bool:
        nx1, ny1 = self._abs_coords(x1, y1)
        nx2, ny2 = self._abs_coords(x2, y2)

        # Move to start
        self._send_mouse(nx1, ny1, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE)
        time.sleep(0.05)

        # Press
        self._send_mouse(nx1, ny1, MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_LEFTDOWN)

        # Interpolate movement
        steps = max(5, int(duration / 0.02))
        for i in range(1, steps + 1):
            t = i / steps
            cx = int(nx1 + (nx2 - nx1) * t)
            cy = int(ny1 + (ny2 - ny1) * t)
            self._send_mouse(cx, cy, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE)
            time.sleep(duration / steps)

        # Release
        return self._send_mouse(nx2, ny2, MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_LEFTUP)


# ---------------------------------------------------------------------------
# Backend 2: pydirectinput-rgx (DirectInput scan codes)
# ---------------------------------------------------------------------------

class DirectInputBackend(InputBackend):
    """Wraps pydirectinput-rgx for DirectInput scan code input.

    Better game compatibility than pyautogui. Requires:
        pip install pydirectinput-rgx
    """

    name = "DirectInput"

    def __init__(self):
        try:
            import pydirectinput
            pydirectinput.PAUSE = 0.02
            self._pdi = pydirectinput
            logger.info("DirectInputBackend initialized")
        except ImportError:
            raise RuntimeError(
                "pydirectinput not installed: pip install pydirectinput-rgx"
            )

    def move_to(self, x: int, y: int, duration: float) -> bool:
        try:
            self._pdi.moveTo(x, y, duration=duration)
            return True
        except Exception as e:
            logger.error(f"DirectInput moveTo failed: {e}")
            return False

    def click(self, x: int, y: int) -> bool:
        try:
            self._pdi.click(x, y)
            return True
        except Exception as e:
            logger.error(f"DirectInput click failed: {e}")
            return False

    def double_click(self, x: int, y: int) -> bool:
        try:
            self._pdi.click(x, y, clicks=2, interval=0.05)
            return True
        except Exception as e:
            logger.error(f"DirectInput double_click failed: {e}")
            return False

    def press_key(self, key: str) -> bool:
        try:
            self._pdi.press(key)
            return True
        except Exception as e:
            logger.error(f"DirectInput press_key failed: {e}")
            return False

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float) -> bool:
        try:
            self._pdi.moveTo(x1, y1, duration=0.05)
            self._pdi.mouseDown()
            self._pdi.moveTo(x2, y2, duration=duration)
            self._pdi.mouseUp()
            return True
        except Exception as e:
            logger.error(f"DirectInput drag failed: {e}")
            return False


# ---------------------------------------------------------------------------
# Backend 3: pyautogui (legacy fallback)
# ---------------------------------------------------------------------------

class PyAutoGuiBackend(InputBackend):
    """Wraps pyautogui as final fallback.

    Uses SetCursorPos + mouse_event which some games ignore.
    """

    name = "PyAutoGui"

    def __init__(self):
        try:
            import pyautogui
            pyautogui.FAILSAFE = True
            pyautogui.PAUSE = 0.05
            self._pyautogui = pyautogui
            logger.info("PyAutoGuiBackend initialized (FAILSAFE=True)")
        except ImportError:
            raise RuntimeError("pyautogui not installed: pip install pyautogui")

    def move_to(self, x: int, y: int, duration: float) -> bool:
        try:
            self._pyautogui.moveTo(x, y, duration=duration)
            return True
        except Exception as e:
            logger.error(f"PyAutoGui moveTo failed: {e}")
            return False

    def click(self, x: int, y: int) -> bool:
        try:
            self._pyautogui.click(x, y)
            return True
        except Exception as e:
            logger.error(f"PyAutoGui click failed: {e}")
            return False

    def double_click(self, x: int, y: int) -> bool:
        try:
            self._pyautogui.doubleClick(x, y)
            return True
        except Exception as e:
            logger.error(f"PyAutoGui double_click failed: {e}")
            return False

    def press_key(self, key: str) -> bool:
        try:
            self._pyautogui.press(key)
            return True
        except Exception as e:
            logger.error(f"PyAutoGui press_key failed: {e}")
            return False

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float) -> bool:
        try:
            self._pyautogui.moveTo(x1, y1, duration=0.05)
            self._pyautogui.drag(
                x2 - x1, y2 - y1, duration=duration
            )
            return True
        except Exception as e:
            logger.error(f"PyAutoGui drag failed: {e}")
            return False


# ---------------------------------------------------------------------------
# InputController — safety layer around the active backend
# ---------------------------------------------------------------------------

def _create_backend() -> InputBackend:
    """Try backends in order: SendInput -> DirectInput -> PyAutoGui."""
    errors = []

    # 1. SendInput (ctypes, zero deps)
    try:
        return SendInputBackend()
    except Exception as e:
        errors.append(f"SendInput: {e}")

    # 2. DirectInput (pydirectinput-rgx)
    try:
        return DirectInputBackend()
    except Exception as e:
        errors.append(f"DirectInput: {e}")

    # 3. PyAutoGui (fallback)
    try:
        return PyAutoGuiBackend()
    except Exception as e:
        errors.append(f"PyAutoGui: {e}")

    logger.error(f"No input backend available: {errors}")
    return None


class InputController:
    """Wraps an InputBackend with MTGA-specific safety features.

    Safety features:
    - FAILSAFE: mouse to corner aborts (pyautogui backend)
    - Bounds checking against MTGA window rectangle
    - Focus verification before actions
    - Configurable minimum delay between clicks
    - All actions are logged
    """

    def __init__(
        self,
        min_click_delay: float = 0.2,
        move_duration: float = 0.15,
        dry_run: bool = False,
    ):
        self._min_click_delay = min_click_delay
        self._move_duration = move_duration
        self._dry_run = dry_run
        self._last_click_time = 0.0
        self._mtga_hwnd: Optional[int] = None

        # Create backend via fallback chain
        self._backend: Optional[InputBackend] = _create_backend()

        if self._backend:
            logger.info(
                f"InputController initialized: backend={self._backend.name}, "
                f"dry_run={dry_run}, min_delay={min_click_delay}s"
            )
        else:
            logger.error("InputController: no input backend available")

    @property
    def available(self) -> bool:
        """Whether an input backend is available."""
        return self._backend is not None

    @property
    def backend_name(self) -> str:
        """Name of the active input backend."""
        return self._backend.name if self._backend else "none"

    def _get_mtga_hwnd(self) -> Optional[int]:
        """Get (cached) MTGA window handle."""
        # Re-validate cached handle
        if self._mtga_hwnd and _IS_WINDOWS:
            if not user32.IsWindow(self._mtga_hwnd):
                self._mtga_hwnd = None
        if not self._mtga_hwnd:
            self._mtga_hwnd = find_mtga_hwnd()
        return self._mtga_hwnd

    def focus_mtga_window(self) -> bool:
        """Bring MTGA window to the foreground using ctypes."""
        hwnd = self._get_mtga_hwnd()
        if not hwnd:
            logger.warning("Cannot focus: MTGA window not found")
            return False
        result = force_foreground(hwnd)
        if result:
            time.sleep(0.1)
        else:
            logger.warning("force_foreground returned False")
        return result

    def _enforce_delay(self) -> None:
        """Enforce minimum delay between clicks."""
        elapsed = time.time() - self._last_click_time
        remaining = self._min_click_delay - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _is_in_bounds(
        self, x: int, y: int, window_rect: tuple[int, int, int, int]
    ) -> bool:
        """Check if coordinates are within the MTGA window bounds."""
        left, top, width, height = window_rect
        return left <= x <= left + width and top <= y <= top + height

    def click(
        self,
        x: int,
        y: int,
        description: str = "",
        window_rect: Optional[tuple[int, int, int, int]] = None,
    ) -> ClickResult:
        """Click at absolute screen coordinates."""
        if not self._backend:
            return ClickResult(False, x, y, description, "no input backend")

        if window_rect and not self._is_in_bounds(x, y, window_rect):
            error = f"({x}, {y}) outside MTGA window {window_rect}"
            logger.warning(f"Bounds check failed: {error}")
            return ClickResult(False, x, y, description, error)

        self._enforce_delay()

        if self._dry_run:
            logger.info(f"[DRY RUN] Click ({x}, {y}): {description}")
            self._last_click_time = time.time()
            return ClickResult(True, x, y, f"[DRY] {description}")

        try:
            self._backend.move_to(x, y, duration=self._move_duration)
            ok = self._backend.click(x, y)
            self._last_click_time = time.time()
            if ok:
                logger.info(f"Clicked ({x}, {y}): {description}")
                return ClickResult(True, x, y, description)
            return ClickResult(False, x, y, description, "backend click returned False")
        except Exception as e:
            logger.error(f"Click failed at ({x}, {y}): {e}")
            return ClickResult(False, x, y, description, str(e))

    def click_card_in_hand(
        self,
        x: int,
        y: int,
        card_name: str,
        window_rect: Optional[tuple[int, int, int, int]] = None,
    ) -> ClickResult:
        """Click a card in hand with hover-to-expand behavior."""
        if not self._backend:
            return ClickResult(False, x, y, card_name, "no input backend")

        if window_rect and not self._is_in_bounds(x, y, window_rect):
            return ClickResult(False, x, y, card_name, f"({x}, {y}) outside MTGA window")

        self._enforce_delay()

        if self._dry_run:
            logger.info(f"[DRY RUN] Click hand card ({x}, {y}): {card_name}")
            self._last_click_time = time.time()
            return ClickResult(True, x, y, f"[DRY] Hand: {card_name}")

        try:
            self._backend.move_to(x, y, duration=self._move_duration)
            time.sleep(0.3)  # Wait for card expansion animation
            ok = self._backend.click(x, y)
            self._last_click_time = time.time()
            if ok:
                logger.info(f"Clicked hand card ({x}, {y}): {card_name}")
                return ClickResult(True, x, y, f"Hand: {card_name}")
            return ClickResult(False, x, y, card_name, "backend click returned False")
        except Exception as e:
            logger.error(f"Hand card click failed: {e}")
            return ClickResult(False, x, y, card_name, str(e))

    def drag_card_from_hand(
        self,
        from_x: int,
        from_y: int,
        to_x: int,
        to_y: int,
        card_name: str,
        window_rect: Optional[tuple[int, int, int, int]] = None,
    ) -> ClickResult:
        """Drag a card from hand to the battlefield.

        Hovers over the card first to trigger the expansion animation,
        then drags it to the target position (e.g., land row on battlefield).
        """
        if not self._backend:
            return ClickResult(False, from_x, from_y, card_name, "no input backend")

        if window_rect:
            if not self._is_in_bounds(from_x, from_y, window_rect):
                return ClickResult(False, from_x, from_y, card_name, f"({from_x}, {from_y}) outside MTGA window")
            if not self._is_in_bounds(to_x, to_y, window_rect):
                return ClickResult(False, to_x, to_y, card_name, f"({to_x}, {to_y}) outside MTGA window")

        self._enforce_delay()

        if self._dry_run:
            logger.info(
                f"[DRY RUN] Drag hand card ({from_x},{from_y})->({to_x},{to_y}): {card_name}"
            )
            self._last_click_time = time.time()
            return ClickResult(True, from_x, from_y, f"[DRY] Drag hand: {card_name}")

        try:
            # Hover first to trigger card expansion animation
            self._backend.move_to(from_x, from_y, duration=self._move_duration)
            time.sleep(0.3)  # Wait for card expansion

            # Drag from hand to battlefield
            ok = self._backend.drag(
                from_x, from_y, to_x, to_y,
                duration=self._move_duration * 3,  # Slower drag for reliability
            )
            self._last_click_time = time.time()
            if ok:
                logger.info(
                    f"Dragged hand card ({from_x},{from_y})->({to_x},{to_y}): {card_name}"
                )
                return ClickResult(True, from_x, from_y, f"Drag hand: {card_name}")
            return ClickResult(False, from_x, from_y, card_name, "backend drag returned False")
        except Exception as e:
            logger.error(f"Hand card drag failed: {e}")
            return ClickResult(False, from_x, from_y, card_name, str(e))

    def double_click(
        self,
        x: int,
        y: int,
        description: str = "",
        window_rect: Optional[tuple[int, int, int, int]] = None,
    ) -> ClickResult:
        """Double-click at absolute screen coordinates."""
        if not self._backend:
            return ClickResult(False, x, y, description, "no input backend")

        if window_rect and not self._is_in_bounds(x, y, window_rect):
            return ClickResult(False, x, y, description, f"({x}, {y}) outside MTGA window")

        self._enforce_delay()

        if self._dry_run:
            logger.info(f"[DRY RUN] Double-click ({x}, {y}): {description}")
            self._last_click_time = time.time()
            return ClickResult(True, x, y, f"[DRY] {description}")

        try:
            self._backend.move_to(x, y, duration=self._move_duration)
            ok = self._backend.double_click(x, y)
            self._last_click_time = time.time()
            if ok:
                logger.info(f"Double-clicked ({x}, {y}): {description}")
                return ClickResult(True, x, y, description)
            return ClickResult(False, x, y, description, "backend double_click returned False")
        except Exception as e:
            logger.error(f"Double-click failed: {e}")
            return ClickResult(False, x, y, description, str(e))

    def drag(
        self,
        from_x: int,
        from_y: int,
        to_x: int,
        to_y: int,
        description: str = "",
        window_rect: Optional[tuple[int, int, int, int]] = None,
    ) -> ClickResult:
        """Drag from one point to another."""
        if not self._backend:
            return ClickResult(False, from_x, from_y, description, "no input backend")

        if window_rect:
            if not self._is_in_bounds(from_x, from_y, window_rect):
                return ClickResult(False, from_x, from_y, description, "Start outside window")
            if not self._is_in_bounds(to_x, to_y, window_rect):
                return ClickResult(False, to_x, to_y, description, "End outside window")

        self._enforce_delay()

        if self._dry_run:
            logger.info(
                f"[DRY RUN] Drag ({from_x},{from_y})->({to_x},{to_y}): {description}"
            )
            self._last_click_time = time.time()
            return ClickResult(True, from_x, from_y, f"[DRY] {description}")

        try:
            ok = self._backend.drag(
                from_x, from_y, to_x, to_y,
                duration=self._move_duration * 2,
            )
            self._last_click_time = time.time()
            if ok:
                logger.info(
                    f"Dragged ({from_x},{from_y})->({to_x},{to_y}): {description}"
                )
                return ClickResult(True, from_x, from_y, description)
            return ClickResult(False, from_x, from_y, description, "backend drag returned False")
        except Exception as e:
            logger.error(f"Drag failed: {e}")
            return ClickResult(False, from_x, from_y, description, str(e))

    def press_key(self, key: str, description: str = "") -> ClickResult:
        """Press a keyboard key."""
        if not self._backend:
            return ClickResult(False, 0, 0, description, "no input backend")

        if self._dry_run:
            logger.info(f"[DRY RUN] Press key '{key}': {description}")
            return ClickResult(True, 0, 0, f"[DRY] Key: {key}")

        try:
            ok = self._backend.press_key(key)
            if ok:
                logger.info(f"Pressed key '{key}': {description}")
                return ClickResult(True, 0, 0, f"Key: {key} - {description}")
            return ClickResult(False, 0, 0, description, f"backend press_key returned False for '{key}'")
        except Exception as e:
            logger.error(f"Key press failed: {e}")
            return ClickResult(False, 0, 0, description, str(e))

    def wait(self, seconds: float, reason: str = "") -> None:
        """Wait with logging."""
        if reason:
            logger.debug(f"Waiting {seconds:.1f}s: {reason}")
        time.sleep(seconds)
