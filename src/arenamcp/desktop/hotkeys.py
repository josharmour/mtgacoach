import contextlib
import logging
import os
import sys
from collections.abc import Callable

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QWidget

logger = logging.getLogger(__name__)


class HotkeyManager(QObject):
    def __init__(self, parent=None):
        super().__init__(parent if isinstance(parent, QObject) else None)
        self._callbacks: dict[str, Callable] = {}
        self._shortcuts: dict[str, QShortcut] = {}
        self._darwin_listener = None

        if sys.platform == "darwin":
            try:
                from arenamcp.desktop.hotkeys_darwin import DarwinHotkeyListener

                listener = DarwinHotkeyListener()
                if listener.is_available:
                    self._darwin_listener = listener
            except Exception:
                logger.debug("DarwinHotkeyListener unavailable; using QShortcut fallback")
                self._darwin_listener = None

    def register(self, key: str, callback: Callable):
        self._callbacks[key] = callback
        if sys.platform == "darwin" and self._darwin_listener:
            try:
                if self._darwin_listener.register(key, callback):
                    return
            except Exception as e:
                logger.warning("Darwin hotkey '%s' failed (%s); using QShortcut fallback", key, e)
        self._register_qshortcut(key, callback)

    def _register_qshortcut(self, key: str, callback: Callable):
        qt_key = key
        if len(qt_key) >= 2 and qt_key[0] in "fF" and qt_key[1:].isdigit():
            qt_key = qt_key.upper()
        parent = self.parent()
        if isinstance(parent, QWidget):
            shortcut = QShortcut(QKeySequence(qt_key), parent)
            shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            shortcut.activated.connect(callback)
            self._shortcuts[key] = shortcut
        else:
            logger.warning("Hotkey '%s' not registered: parent is not a QWidget", key)

    def unregister_all(self):
        if self._darwin_listener:
            with contextlib.suppress(Exception):
                self._darwin_listener.unregister_all()
        for shortcut in self._shortcuts.values():
            shortcut.setEnabled(False)
        self._shortcuts.clear()
        self._callbacks.clear()
