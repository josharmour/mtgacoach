"""Match History page: recent matches with W/L and a best-effort score.

Reads the persisted MatchHistory (~/.arenamcp/match_history/history.json),
written by the coach at game end. Each row shows W/L plus a coach rating
(1-10) that is backfilled after post-match analysis; rows with no analysis
degrade to W/L-only (score shows a dash).

At sidebar width the page shows two-line rows; from ``WIDE_LAYOUT_PX`` up it
switches to the full seven-column table. Both views hold the same rows.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from arenamcp.match_history import MatchHistory

from . import theme

_COLUMNS = ["Result", "Score", "Opponent", "Format", "Colors", "Turns", "When"]
_MAX_ROWS = 20
WIDE_LAYOUT_PX = 520


def _fmt_ts(ts: str) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%m-%d %H:%M")
    except Exception:
        return ts or ""


def _result_tone(result: str) -> str:
    return {"win": "good", "loss": "bad"}.get(result, "muted")


class PerformanceTab(QWidget):
    """Shows the last few completed matches and how each one scored."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._row_records: list = []
        self._build_ui()
        # The coach writes match history from a SEPARATE process, so the UI
        # must re-read the file rather than trust any cached in-memory list.
        # Poll while the tab is visible so a just-finished game shows up
        # without requiring a manual Refresh click.
        self._timer = QTimer(self)
        self._timer.setInterval(3000)
        self._timer.timeout.connect(self.refresh)
        theme.on_theme_changed(self.refresh)
        self.refresh()

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 0, 2, 8)
        lay.setSpacing(8)

        header = QHBoxLayout()
        self._summary = QLabel("")
        self._summary.setWordWrap(True)
        self._summary.setProperty("role", "muted")
        refresh = QPushButton("Refresh")
        refresh.setToolTip("Re-read match history from disk")
        refresh.clicked.connect(self.refresh)
        header.addWidget(self._summary, 1)
        header.addWidget(refresh, 0, Qt.AlignTop)
        lay.addLayout(header)

        # Wide: seven columns.
        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.SingleSelection)
        self._table.setShowGrid(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._table.itemSelectionChanged.connect(lambda: self._on_selection_changed(self._table))

        # Narrow: result badge + two-line details.
        self._compact = QTableWidget(0, 2)
        self._compact.horizontalHeader().setVisible(False)
        self._compact.verticalHeader().setVisible(False)
        self._compact.setEditTriggers(QTableWidget.NoEditTriggers)
        self._compact.setSelectionBehavior(QTableWidget.SelectRows)
        self._compact.setSelectionMode(QTableWidget.SingleSelection)
        self._compact.setShowGrid(False)
        self._compact.setWordWrap(True)
        self._compact.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._compact.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._compact.itemSelectionChanged.connect(lambda: self._on_selection_changed(self._compact))

        self._views = QStackedWidget()
        self._views.addWidget(self._compact)
        self._views.addWidget(self._table)
        self._views.setCurrentWidget(self._table)
        lay.addWidget(self._views, 1)

        why = QLabel("WHY THIS SCORE")
        why.setProperty("role", "caption")
        lay.addWidget(why)
        self._detail = QLabel("Select a match to see why its score was given.")
        self._detail.setWordWrap(True)
        self._detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._detail.setProperty("card", True)
        self._detail.setContentsMargins(8, 6, 8, 6)
        lay.addWidget(self._detail)

    # ------------------------------------------------------------------
    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        wide = self.width() >= WIDE_LAYOUT_PX
        target = self._table if wide else self._compact
        if self._views.currentWidget() is not target:
            row = self._selected_row()
            self._views.setCurrentWidget(target)
            if row is not None:
                target.selectRow(row)

    def _visible_table(self) -> QTableWidget:
        return self._views.currentWidget()

    def _selected_row(self) -> int | None:
        rows = self._visible_table().selectionModel().selectedRows()
        return rows[0].row() if rows else None

    def _on_selection_changed(self, table: QTableWidget) -> None:
        """Show the selected row's score explanation in the detail label."""
        rows = table.selectionModel().selectedRows()
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
        row = self._selected_row()
        if row is not None and 0 <= row < len(self._row_records):
            return getattr(self._row_records[row], "match_id", None)
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

        for table in (self._table, self._compact):
            table.setRowCount(0)
        self._row_records = []
        wins = losses = 0
        rated: list[int] = []
        muted = theme.qcolor("muted")

        for rec in records:
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._compact.insertRow(row)
            result = (rec.result or "").lower()

            if result == "win":
                wins += 1
                rtext = "W"
            elif result == "loss":
                losses += 1
                rtext = "L"
            else:
                rtext = (result or "?").upper()
            result_color = theme.qcolor(_result_tone(result))

            rating = getattr(rec, "coach_rating", None)
            reason = (getattr(rec, "coach_score_reason", "") or "").strip()
            score_text = f"{int(rating)}/10" if rating is not None else "—"
            if rating is not None:
                rated.append(int(rating))
            when = _fmt_ts(rec.timestamp)
            colors = "".join(rec.opponent_colors_seen)
            turns = rec.turns or 0

            item_r = QTableWidgetItem(rtext)
            item_r.setForeground(result_color)
            item_r.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(row, 0, item_r)

            item_s = QTableWidgetItem(score_text)
            item_s.setTextAlignment(Qt.AlignCenter)
            if rating is None:
                item_s.setForeground(muted)
            self._table.setItem(row, 1, item_s)

            self._table.setItem(row, 2, QTableWidgetItem(rec.opponent_name or ""))
            self._table.setItem(row, 3, QTableWidgetItem(rec.format_name or ""))
            self._table.setItem(row, 4, QTableWidgetItem(colors))
            self._table.setItem(row, 5, QTableWidgetItem(str(turns)))
            self._table.setItem(row, 6, QTableWidgetItem(when))

            badge = QTableWidgetItem(f"{rtext}\n{score_text}")
            badge.setForeground(result_color)
            badge.setTextAlignment(Qt.AlignCenter)
            self._compact.setItem(row, 0, badge)
            line_one = " · ".join(x for x in (rec.opponent_name or "opponent", rec.format_name or "") if x)
            line_two = " · ".join(x for x in (colors, f"{turns} turns", when) if x)
            self._compact.setItem(row, 1, QTableWidgetItem(f"{line_one}\n{line_two}"))

            # Hover anywhere on the row shows the score + why.
            tip = (
                f"{rtext} · score {int(rating)}/10" + (f"\n{reason}" if reason else "")
                if rating is not None
                else f"{rtext} · no score (post-match analysis not run)"
            )
            for table in (self._table, self._compact):
                for cc in range(table.columnCount()):
                    it = table.item(row, cc)
                    if it is not None:
                        it.setToolTip(tip)
            self._row_records.append(rec)

        self._compact.resizeRowsToContents()

        if selected_match_id is not None:
            for r, rec in enumerate(self._row_records):
                if getattr(rec, "match_id", None) == selected_match_id:
                    self._visible_table().selectRow(r)
                    break

        total = wins + losses
        win_rate = (wins / total * 100) if total else 0.0
        avg = round(sum(rated) / len(rated), 1) if rated else None

        summary = f"{len(records)} shown · {wins}W / {losses}L ({win_rate:.0f}% win rate)"
        if avg is not None:
            summary += f" · avg coach score {avg}/10"
        self._summary.setText(summary)
