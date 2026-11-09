"""Accessible match buttons without Cocoa's synthesized table-cell cache.

Qt 6.12.0 is affected by QTBUG-149612. Keep the Mac history out of the
QAbstractItemView accessibility path until a fixed Qt distribution is qualified.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget


class MatchHistoryCards(QScrollArea):
    selected = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self._body = QWidget()
        self._layout = QVBoxLayout(self._body)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.addStretch()
        self.setWidget(self._body)
        self._ids = []
        self._selected_id = None
        self.buttons = {}

    def selected_row(self):
        return self._ids.index(self._selected_id) if self._selected_id in self._ids else None

    def _choose(self, match_id):
        self._selected_id = match_id
        for key, button in self.buttons.items():
            button.setChecked(key == match_id)
        row = self.selected_row()
        if row is not None:
            self.selected.emit(row)

    def set_records(self, records):
        from .performance_tab import _fmt_ts

        self._ids = [r.match_id for r in records]
        for key in list(self.buttons):
            if key not in self._ids:
                button = self.buttons.pop(key)
                self._layout.removeWidget(button)
                button.hide()
                button.deleteLater()
        if self._selected_id not in self._ids:
            self._selected_id = None
        for index, rec in enumerate(records):
            button = self.buttons.get(rec.match_id)
            if button is None:
                button = QPushButton(self._body)
                button.setCheckable(True)
                button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Minimum)
                button.setMinimumWidth(0)
                layout = QVBoxLayout(button)
                label = QLabel(button)
                label.setWordWrap(True)
                label.setTextFormat(Qt.PlainText)
                label.setAttribute(Qt.WA_TransparentForMouseEvents)
                layout.addWidget(label)
                button.clicked.connect(lambda checked=False, key=rec.match_id: self._choose(key))
                self.buttons[rec.match_id] = button
            # Reuse widgets across polls so focus and accessible identities survive.
            if self._layout.indexOf(button) != index:
                self._layout.removeWidget(button)
                self._layout.insertWidget(index, button)
            text = (
                f"{(rec.result or '?').upper()} · {rec.opponent_name or 'opponent'}\n"
                f"{rec.format_name or 'Unknown format'} · {rec.turns or 0} turns · {_fmt_ts(rec.timestamp)}"
            )
            button.findChild(QLabel).setText(text)
            button.setAccessibleName(text)
            button.setToolTip("Select to review the recorded result; legacy model notes are unverified.")
            button.setChecked(rec.match_id == self._selected_id)
