"""User-initiated screen capture permission setup; background capture stays silent."""

import os
import sys
from pathlib import Path

from PySide6.QtWidgets import QMessageBox, QWidget

from arenamcp.screen_capture import request_screen_capture_access, require_screen_capture_access

from .runtime import open_url

SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"


def _ask(parent, title, text, action):
    box = QMessageBox(parent if isinstance(parent, QWidget) else None)
    box.setWindowTitle(title)
    box.setText(text)
    accept = box.addButton(action, QMessageBox.AcceptRole)
    box.addButton("Not now", QMessageBox.RejectRole)
    box.exec()
    return box.clickedButton() is accept


def _application_path():
    if os.environ.get("MTGACOACH_DEV_CHECKOUT"):
        return Path(os.environ.get("MTGACOACH_DEV_APP", Path.home() / "Applications/MTGA Coach (Dev).app"))
    return next((path for path in Path(sys.executable).parents if path.suffix == ".app"), None)


def ensure_screen_capture_permission(parent=None):
    """Offer setup at feature use, and let reports continue when access is declined."""
    try:
        require_screen_capture_access()
        return True
    except PermissionError:
        pass
    if not _ask(
        parent,
        "Allow Arena screenshots?",
        "Coach needs macOS Screen & System Audio Recording permission to capture Arena. "
        "Bug reports can still be saved without the Arena screenshot.",
        "Allow screenshots…",
    ):
        return False
    if request_screen_capture_access():
        return True
    app = _application_path()
    target = f"“{app.stem}”" if app else "the app that launches Coach"
    details = (
        f"Enable {target} in Screen & System Audio Recording. "
        "If it is already enabled, fully quit and reopen Coach after your match."
    )
    if app:
        details += f"\n\nIf it is missing, click + and add:\n{app}"
    if _ask(parent, "Finish screen recording setup", details, "Open System Settings"):
        open_url(SETTINGS_URL)
    return False
