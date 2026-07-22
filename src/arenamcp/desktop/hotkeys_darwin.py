"""Native macOS Global Hotkey Listener using PyObjC NSEvent.

Monitors global and local KeyDown events for registered hotkeys on macOS.
Primary target keys: F3 (VLM Analyze), F6 (PTT), F12 (AP Toggle).
Falls back gracefully if PyObjC (AppKit) is unavailable or if event monitoring fails.
"""

import logging
import sys
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

# macOS Virtual Key Codes for Function Keys (Carbon/AppKit)
MACOS_KEY_CODES: Dict[str, int] = {
    "F1": 122,
    "F2": 120,
    "F3": 99,
    "F4": 118,
    "F5": 96,
    "F6": 97,
    "F7": 98,
    "F8": 100,
    "F9": 101,
    "F10": 109,
    "F11": 103,
    "F12": 111,
}

HOTKEY_PURPOSES: Dict[str, str] = {
    "F3": "VLM Analyze",
    "F6": "PTT (Push-To-Talk)",
    "F12": "AP Toggle (Autopilot)",
}

# Attempt PyObjC AppKit import safely
_pyobjc_available = False
NSEvent = None
NSEventMaskKeyDown = None

if sys.platform == "darwin":
    try:
        from AppKit import NSEvent
        try:
            from AppKit import NSEventMaskKeyDown
        except ImportError:
            try:
                from AppKit import NSKeyDownMask as NSEventMaskKeyDown
            except ImportError:
                NSEventMaskKeyDown = 1 << 10  # Fallback to keydown bitmask constant
        _pyobjc_available = True
    except (ImportError, Exception) as _import_err:
        logger.info("PyObjC (AppKit) not available for Darwin global hotkeys: %s", _import_err)
        _pyobjc_available = False


class DarwinHotkeyListener:
    """macOS Global Hotkey Listener using PyObjC NSEvent.addGlobalMonitorForEventsMatchingMask.
    
    Implements native background global hotkey interception for F3, F6, F12, etc.
    Gracefully falls back when PyObjC is not installed or when accessibility permissions are missing.
    """

    def __init__(self) -> None:
        self._callbacks: Dict[int, Callable[[], None]] = {}
        self._key_names: Dict[int, str] = {}
        self._global_monitor = None
        self._local_monitor = None
        self._active = False

    @property
    def is_available(self) -> bool:
        """Return True if PyObjC environment is present on macOS."""
        return _pyobjc_available and sys.platform == "darwin"

    @property
    def is_active(self) -> bool:
        """Return True if event monitoring is active."""
        return self._active

    def register(self, key_name: str, callback: Callable[[], None]) -> bool:
        """Register a hotkey callback (e.g. 'F3', 'F6', 'F12').

        Args:
            key_name: Key name string (case-insensitive, e.g. "F3").
            callback: Parameterless function to invoke when triggered.

        Returns:
            True if key was mapped and registered; False otherwise.
        """
        if not self.is_available:
            return False

        code = MACOS_KEY_CODES.get(key_name.upper())
        if code is None:
            logger.warning("Unsupported macOS function key: %s", key_name)
            return False

        self._callbacks[code] = callback
        self._key_names[code] = key_name.upper()

        if not self._active:
            self.start()

        purpose = HOTKEY_PURPOSES.get(key_name.upper(), "")
        desc = f" [{purpose}]" if purpose else ""
        logger.info("Registered Darwin hotkey %s (keycode %d)%s", key_name.upper(), code, desc)
        return True

    def unregister(self, key_name: str) -> None:
        """Unregister a specific hotkey."""
        code = MACOS_KEY_CODES.get(key_name.upper())
        if code is not None:
            self._callbacks.pop(code, None)
            self._key_names.pop(code, None)
        if not self._callbacks:
            self.stop()

    def unregister_all(self) -> None:
        """Unregister all hotkeys and stop event monitoring."""
        self.stop()
        self._callbacks.clear()
        self._key_names.clear()

    def start(self) -> bool:
        """Start global and local event monitors.

        Returns:
            True if monitor started successfully; False if unavailable or failed.
        """
        if not self.is_available or self._active:
            return self._active

        try:
            def _global_handler(event) -> None:
                try:
                    keycode = event.keyCode()
                    if keycode in self._callbacks:
                        logger.debug(
                            "Darwin global hotkey triggered: %s (keycode %d)",
                            self._key_names.get(keycode, "unknown"),
                            keycode,
                        )
                        self._callbacks[keycode]()
                except Exception as err:
                    logger.error("Error in Darwin global hotkey callback: %s", err)

            self._global_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                NSEventMaskKeyDown, _global_handler
            )

            # Also register local monitor for when application is focused
            if hasattr(NSEvent, "addLocalMonitorForEventsMatchingMask_handler_"):
                def _local_handler(event):
                    try:
                        keycode = event.keyCode()
                        if keycode in self._callbacks:
                            logger.debug(
                                "Darwin local hotkey triggered: %s (keycode %d)",
                                self._key_names.get(keycode, "unknown"),
                                keycode,
                            )
                            self._callbacks[keycode]()
                    except Exception as err:
                        logger.error("Error in Darwin local hotkey callback: %s", err)
                    return event

                self._local_monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                    NSEventMaskKeyDown, _local_handler
                )

            self._active = True
            logger.info("Darwin global hotkey monitoring started.")
            return True
        except Exception as err:
            logger.warning("Failed to start Darwin global hotkey monitor: %s", err)
            self._active = False
            return False

    def stop(self) -> None:
        """Stop global and local event monitors."""
        if not self._active:
            return

        if self._global_monitor and NSEvent:
            try:
                NSEvent.removeMonitor_(self._global_monitor)
            except Exception as err:
                logger.debug("Error removing Darwin global monitor: %s", err)
            self._global_monitor = None

        if self._local_monitor and NSEvent:
            try:
                NSEvent.removeMonitor_(self._local_monitor)
            except Exception as err:
                logger.debug("Error removing Darwin local monitor: %s", err)
            self._local_monitor = None

        self._active = False
        logger.info("Darwin global hotkey monitoring stopped.")
