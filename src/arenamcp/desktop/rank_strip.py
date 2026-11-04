"""Clickable Arena standings on the coach sidebar."""

import os

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget

from arenamcp.android_link import MIRROR_PATH, game_device
from arenamcp.ladder import LADDERS, RankLog, rank_text
from arenamcp.watcher import DEFAULT_LOG_PATH, _normalize_log_path


class RankStrip(QWidget):
    history_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._reader = RankLog()
        self.buttons = {}
        for ladder in LADDERS:
            button = QPushButton(f"{ladder}\nUnavailable")
            button.setCursor(Qt.PointingHandCursor)
            button.setToolTip(f"Show the last five ranked {ladder} matches")
            button.clicked.connect(lambda checked=False, name=ladder: self.history_requested.emit(name))
            layout.addWidget(button, 1)
            self.buttons[ladder] = button
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.refresh)

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self._timer.start(0)

    def hideEvent(self, event):  # noqa: N802
        super().hideEvent(event)
        self._timer.stop()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._render()

    def _render(self):
        for ladder, button in self.buttons.items():
            rank = rank_text(self._reader.snapshot, ladder)
            if self.width() < 280:
                rank = rank.replace(" · ", "\n")
            button.setText(f"{ladder}\n{rank}")

    def refresh(self):
        path = (
            MIRROR_PATH
            if game_device() == "android"
            else _normalize_log_path(os.environ.get("MTGA_LOG_PATH") or DEFAULT_LOG_PATH)
        )
        more = self._reader.read(path)
        if not more:
            self._render()
        self._timer.start(10 if more else 3000)
