"""Clickable Arena standings on the coach sidebar."""

import os

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QWidget

from arenamcp.android_link import MIRROR_PATH, game_device
from arenamcp.ladder import LADDERS, RANK_HISTORY_LIMIT, RankLog, rank_text, ranked_ladder
from arenamcp.match_history import MatchHistory
from arenamcp.watcher import DEFAULT_LOG_PATH, _normalize_log_path

from .companion_widgets import SummaryButton


class RankStrip(QWidget):
    history_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._reader = RankLog()
        self.buttons = {}
        self.recent = {ladder: [] for ladder in LADDERS}
        self._history_stamp = None
        for ladder in LADDERS:
            button = SummaryButton(f"{ladder}\nUnavailable")
            button.setFixedHeight(4 * button.fontMetrics().lineSpacing() + 16)
            button.setCursor(Qt.PointingHandCursor)
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
            recent = self.recent[ladder]
            marks = [{"win": "W", "loss": "L", "draw": "D"}.get(r.result, "?") for r in recent]
            metrics = button.fontMetrics()
            available = button.width() - 28
            results = " ".join(marks)
            if metrics.horizontalAdvance(results) > available:
                results = "".join(marks)
            history = f"Newest → oldest\n{results}" if results else "No recent matches"
            if results and metrics.horizontalAdvance(results) > available:
                split = (len(marks) + 1) // 2
                first, last = results[:split], results[split:]
                newest = "Newest" if metrics.horizontalAdvance(f"Newest {first}") <= available else "New"
                oldest = "Oldest" if metrics.horizontalAdvance(f"{last} Oldest") <= available else "Old"
                history = f"{newest} {first}\n{last} {oldest}"
            button.setText(f"{ladder}\n{rank}\n{history}")
            button.setToolTip(
                f"Show up to {RANK_HISTORY_LIMIT} recent ranked {ladder} matches, newest first."
            )

    def refresh(self):
        path = (
            MIRROR_PATH
            if game_device() == "android"
            else _normalize_log_path(os.environ.get("MTGA_LOG_PATH") or DEFAULT_LOG_PATH)
        )
        more = self._reader.read(path)
        if not more:
            from arenamcp import match_history

            try:
                stat = match_history.HISTORY_FILE.stat()
                stamp = (str(match_history.HISTORY_FILE), stat.st_mtime_ns, stat.st_size)
            except OSError:
                stamp = (str(match_history.HISTORY_FILE), None)
            if stamp != self._history_stamp:
                self._history_stamp = stamp
                history = MatchHistory()
                records = list(reversed(history.get_recent(history.total_games)))
                self.recent = {
                    name: [r for r in records if ranked_ladder(r.format_name) == name][:RANK_HISTORY_LIMIT]
                    for name in LADDERS
                }
            self._render()
        self._timer.start(10 if more else 3000)
