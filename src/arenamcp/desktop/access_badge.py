"""Status-bar chip showing the user's access: subscriber badge or a live free-trial countdown.

The subscription check runs off the UI thread (it is a network call); the countdown re-renders every
minute from the last known expiry, so it keeps ticking while offline. Clicking the chip opens the
subscribe page when access has lapsed and Setup & Repair otherwise.
"""

from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QLabel, QWidget

from arenamcp.settings import get_settings
from arenamcp.subscription import SubscriptionStatus, check_subscription, describe_access, open_subscribe_page

from . import theme

logger = logging.getLogger(__name__)

RECHECK_MS = 30 * 60 * 1000  # server status
TICK_MS = 60 * 1000  # countdown text


class AccessBadge(QLabel):
    """Clickable chip: "★ Subscriber", "Trial · 4d 6h left", "Trial ended — Subscribe", ..."""

    setup_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("chip", True)
        self.setTextFormat(Qt.RichText)
        self.setCursor(Qt.PointingHandCursor)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="access-badge")
        self._pending: Future | None = None
        self._status: SubscriptionStatus | None = None
        self._needs_subscription = False

        self._tick = QTimer(self)
        self._tick.timeout.connect(self._render)
        self._tick.start(TICK_MS)
        self._recheck = QTimer(self)
        self._recheck.timeout.connect(lambda: self.refresh(force=True))
        self._recheck.start(RECHECK_MS)
        self._poll = QTimer(self)  # collects the background check's result on the UI thread
        self._poll.setInterval(250)
        self._poll.timeout.connect(self._collect)

        self._render()
        self.refresh(force=True)

    def refresh(self, force: bool = False) -> None:
        """Re-check the subscription in the background (force bypasses the 24h cache)."""
        key = (get_settings().get("license_key") or "").strip()
        if not key or (self._pending is not None and not self._pending.done()):
            self._render()
            return
        self._pending = self._pool.submit(check_subscription, key, force)
        self._poll.start()

    def _collect(self) -> None:
        if self._pending is None or not self._pending.done():
            return
        self._poll.stop()
        try:
            self._status = self._pending.result()
        except Exception as e:  # noqa: BLE001 - a failed check must never break the window
            logger.debug(f"Access check failed: {e}")
        self._pending = None
        self._render()

    def _render(self) -> None:
        settings = get_settings()
        text, tone, tip, needs = describe_access(
            (settings.get("license_key") or "").strip(), self._status, settings.get("trial_expires_at"))
        self._needs_subscription = needs
        self.setText(theme.span(text, tone, weight=600))
        self.setToolTip(tip)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.LeftButton:
            if self._needs_subscription:
                open_subscribe_page()
            self.setup_requested.emit()
        super().mousePressEvent(event)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
