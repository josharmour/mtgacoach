"""Historical performance tab: recent matches with W/L and a best-effort score.

Reads the persisted MatchHistory (~/.arenamcp/match_history/history.json),
written by the coach at game end. Each row shows W/L plus a coach rating
(1-10) that is backfilled after post-match analysis; rows with no analysis
degrade to W/L-only (score shows a dash).
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from arenamcp.match_history import MatchHistory

_COLUMNS = ["Result", "Score", "Opponent", "Format", "Colors", "Turns", "When"]
_MAX_ROWS = 20

_GREEN = QColor(40, 160, 90)
_RED = QColor(215, 80, 80)
_DIM = QColor(150, 150, 150)


def _fmt_ts(ts: str) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%m-%d %H:%M")
    except Exception:
        return ts or ""


class PerformanceTab(QWidget):
    """Shows the last few completed matches and how each one scored."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._build_ui()
        # The coach writes match history from a SEPARATE process, so the UI
        # must re-read the file rather than trust any cached in-memory list.
        # Poll while the tab is visible so a just-finished game shows up
        # without requiring a manual Refresh click.
        self._timer = QTimer(self)
        self._timer.setInterval(3000)
        self._timer.timeout.connect(self.refresh)
        self.refresh()

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(8)

        title = QLabel("Recent Matches — Performance")
        title.setStyleSheet("font-size: 17px; font-weight: 700;")
        lay.addWidget(title)

        header = QHBoxLayout()
        self._summary = QLabel("")
        self._summary.setStyleSheet("font-size: 13px;")
        refresh = QPushButton("Refresh")
        refresh.setToolTip("Re-read match history from disk")
        refresh.clicked.connect(self.refresh)
        header.addWidget(self._summary)
        header.addStretch()
        header.addWidget(refresh)
        lay.addLayout(header)

        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.SingleSelection)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        lay.addWidget(self._table, 1)

        why = QLabel("WHY THIS SCORE")
        why.setStyleSheet("font-size: 11px; font-weight: 700; color: #9aa;")
        lay.addWidget(why)
        self._detail = QLabel("Select a match to see why its score was given.")
        self._detail.setWordWrap(True)
        self._detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._detail.setStyleSheet(
            "font-size: 13px; color: #ddd;"
            "background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.10);"
            "padding: 8px; border-radius: 4px;"
        )
        lay.addWidget(self._detail)

    def _on_selection_changed(self) -> None:
        """Show the selected row's score explanation in the detail label."""
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            self._detail.setText("Click a row to see why its score was given.")
            return
        row = rows[0].row()
        if not (0 <= row < len(self._row_records)):
            self._detail.setText("")
            return
        rec = self._row_records[row]
        result = (rec.result or "").upper()
        opponent = rec.opponent_name or "opponent"
        rating = getattr(rec, "coach_rating", None)
        reason = (getattr(rec, "coach_score_reason", "") or "").strip()
        if rating is None:
            self._detail.setText(f"{result} vs {opponent} — no score (post-match analysis not run)")
            return
        text = f"{result} vs {opponent} — score {int(rating)}/10"
        if reason:
            text += f"\n{reason}"
        self._detail.setText(text)

    def showEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().showEvent(event)
        self._timer.start()
        self.refresh()

    def hideEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().hideEvent(event)
        self._timer.stop()

    def _selected_match_id(self):
        """match_id of the currently selected row, or None (first-call safe)."""
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return None
        row = rows[0].row()
        records = getattr(self, "_row_records", [])
        if 0 <= row < len(records):
            return getattr(records[row], "match_id", None)
        return None

    def refresh(self) -> None:
        try:
            # Fresh instance every time: re-reads match_history from disk so
            # results written by the coach subprocess are always visible.
            history = MatchHistory()
            records = list(reversed(history.get_recent(_MAX_ROWS)))
        except Exception:
            records = []

        # Preserve the selected row's explanation across the 3s auto-refresh so
        # the "why this score" stays visible instead of clearing every poll.
        selected_match_id = self._selected_match_id()

        self._table.setRowCount(0)
        self._row_records: list = []
        wins = losses = 0
        rated: list[int] = []

        for rec in records:
            row = self._table.rowCount()
            self._table.insertRow(row)
            result = (rec.result or "").lower()

            if result == "win":
                wins += 1
                rtext, color = "W", _GREEN
            elif result == "loss":
                losses += 1
                rtext, color = "L", _RED
            else:
                rtext, color = (result or "?").upper(), _DIM

            item_r = QTableWidgetItem(rtext)
            item_r.setForeground(color)
            item_r.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(row, 0, item_r)

            rating = getattr(rec, "coach_rating", None)
            reason = (getattr(rec, "coach_score_reason", "") or "").strip()
            if rating is not None:
                rated.append(int(rating))
                item_s = QTableWidgetItem(f"{int(rating)}/10")
                item_s.setTextAlignment(Qt.AlignCenter)
            else:
                item_s = QTableWidgetItem("—")
                item_s.setTextAlignment(Qt.AlignCenter)
                item_s.setForeground(_DIM)
            self._table.setItem(row, 1, item_s)

            self._table.setItem(row, 2, QTableWidgetItem(rec.opponent_name or ""))
            self._table.setItem(row, 3, QTableWidgetItem(rec.format_name or ""))
            self._table.setItem(row, 4, QTableWidgetItem("".join(rec.opponent_colors_seen)))
            self._table.setItem(row, 5, QTableWidgetItem(str(rec.turns or 0)))
            self._table.setItem(row, 6, QTableWidgetItem(_fmt_ts(rec.timestamp)))

            # Hover anywhere on the row shows the score + why.
            tip = (
                f"{rtext} · score {int(rating)}/10" + (f"\n{reason}" if reason else "")
                if rating is not None
                else f"{rtext} · no score (post-match analysis not run)"
            )
            for cc in range(self._table.columnCount()):
                it = self._table.item(row, cc)
                if it is not None:
                    it.setToolTip(tip)
            self._row_records.append(rec)

        if selected_match_id is not None:
            for r in range(self._table.rowCount()):
                if getattr(self._row_records[r], "match_id", None) == selected_match_id:
                    self._table.selectRow(r)
                    break

        total = wins + losses
        win_rate = (wins / total * 100) if total else 0.0
        avg = round(sum(rated) / len(rated), 1) if rated else None

        summary = f"{len(records)} shown · {wins}W / {losses}L ({win_rate:.0f}% win rate)"
        if avg is not None:
            summary += f" · avg coach score {avg}/10"
        self._summary.setText(summary)
