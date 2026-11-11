"""Match History: game results and clearly labelled legacy model notes.

Historical mixed ratings remain in storage but are never presented as performance.
"""

from __future__ import annotations

import sys
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

from arenamcp.ladder import RANK_HISTORY_LIMIT, ranked_ladder
from arenamcp.match_history import MatchHistory

from . import theme

_COLUMNS = ["Result", "Notes", "Opponent", "Format", "Colors", "Turns", "When"]
_MAX_ROWS = 20
WIDE_LAYOUT_PX = 520
_USE_TABLES = sys.platform != "darwin"  # QTBUG-149612: Cocoa selected-cell cache ownership

# A win rate is only shown once a deck has this many decided autopilot games;
# smaller samples get no percentage (noise, not signal).
MIN_GAMES_FOR_WIN_RATE = 20


def autopilot_deck_results(records) -> list[dict]:
    """Aggregate per-deck results for games the autopilot played.

    Only records where ``autopilot`` is True count (advisor-mode games are the
    player's own play). Games are grouped by deck identity — falling back to
    the deck name when a record predates identity tracking — and only decided
    games (win/loss) are counted. A deck's ``win_rate`` is None until it has
    at least MIN_GAMES_FOR_WIN_RATE decided games.
    """
    groups: dict[str, dict] = {}
    legacy_keys: set[str] = set()
    for rec in records:
        if getattr(rec, "autopilot", None) is not True:
            continue
        result = (rec.result or "").lower()
        if result not in ("win", "loss"):
            continue
        identity = getattr(rec, "deck_identity", "") or ""
        name = getattr(rec, "deck_name", "") or ""
        key = identity or name
        if not key:
            continue
        if not identity:
            legacy_keys.add(key)
        group = groups.setdefault(key, {"name": name, "wins": 0, "losses": 0})
        if name and not group["name"]:
            group["name"] = name
        group["wins" if result == "win" else "losses"] += 1

    entries = []
    for group in groups.values():
        games = group["wins"] + group["losses"]
        entries.append(
            {
                "name": group["name"] or "Unnamed deck",
                "games": games,
                "wins": group["wins"],
                "losses": group["losses"],
                # No percentage below the sample-size floor.
                "win_rate": (group["wins"] / games) if games >= MIN_GAMES_FOR_WIN_RATE else None,
            }
        )

    # Records written before identity tracking carry only a deck name. Fold a
    # legacy bucket into an identified deck when the display name matches
    # exactly one identified deck, so one deck never shows as two rows — but
    # never merge two different identities on a name alone.
    by_name: dict[str, list[dict]] = {}
    for entry in entries:
        by_name.setdefault(entry["name"], []).append(entry)
    for key in legacy_keys:
        legacy_entry = next((e for e in entries if e["name"] == groups[key]["name"]), None)
        if legacy_entry is None or len(by_name[legacy_entry["name"]]) < 2:
            continue  # unique display name: nothing to fold into
        identified = [e for e in by_name[legacy_entry["name"]] if e is not legacy_entry]
        if len(identified) == 1:
            target = identified[0]
            target["wins"] += legacy_entry["wins"]
            target["losses"] += legacy_entry["losses"]
            target["games"] = target["wins"] + target["losses"]
            target["win_rate"] = (
                (target["wins"] / target["games"])
                if target["games"] >= MIN_GAMES_FOR_WIN_RATE
                else None
            )
            entries.remove(legacy_entry)

    entries.sort(key=lambda e: (-e["games"], e["name"]))
    return entries


def _format_deck_result(entry: dict) -> str:
    line = f"{entry['name']} · {entry['games']} games · {entry['wins']}W–{entry['losses']}L"
    if entry["win_rate"] is not None:
        line += f" · {entry['win_rate'] * 100:.0f}% win rate"
    return line


def _fmt_ts(ts: str) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")
    except Exception:
        return ts or ""


def _result_tone(result: str) -> str:
    return {"win": "good", "loss": "bad"}.get(result, "muted")


class PerformanceTab(QWidget):
    """Shows the last few completed matches and their recorded results."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ladder: str | None = None
        self._row_records: list = []
        self._cards = None
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

        # Per-deck autopilot results (hidden until at least one autopilot
        # game with a known deck is recorded).
        self._decks_caption = QLabel("AUTOPILOT RESULTS BY DECK")
        self._decks_caption.setProperty("role", "caption")
        self._decks_caption.hide()
        lay.addWidget(self._decks_caption)
        self._decks = QLabel("")
        self._decks.setWordWrap(True)
        self._decks.setProperty("role", "muted")
        self._decks.hide()
        lay.addWidget(self._decks)

        if not _USE_TABLES:
            from .match_history_cards import MatchHistoryCards

            self._cards = MatchHistoryCards(self)
            self._cards.selected.connect(self._show_record)
            lay.addWidget(self._cards, 1)
        else:
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

        why = QLabel("RECORDED GAME")
        why.setProperty("role", "caption")
        lay.addWidget(why)
        self._detail = QLabel("Select a game to review its recorded result.")
        self._detail.setWordWrap(True)
        self._detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._detail.setProperty("card", True)
        self._detail.setContentsMargins(8, 6, 8, 6)
        lay.addWidget(self._detail)

    # ------------------------------------------------------------------
    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        if self._cards is not None:
            return
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
        if self._cards is not None:
            return self._cards.selected_row()
        rows = self._visible_table().selectionModel().selectedRows()
        return rows[0].row() if rows else None

    def _on_selection_changed(self, table: QTableWidget) -> None:
        """Show the recorded result and quarantine any legacy model note."""
        rows = table.selectionModel().selectedRows()
        if not rows:
            self._detail.setText("Select a game to review its recorded result.")
            return
        self._show_record(rows[0].row())

    def _show_record(self, row: int) -> None:
        if not (0 <= row < len(self._row_records)):
            self._detail.setText("")
            return
        rec = self._row_records[row]
        result = (rec.result or "").upper()
        opponent = rec.opponent_name or "opponent"
        reason = (getattr(rec, "coach_score_reason", "") or "").strip()
        text = f"{result} vs {opponent}"
        if reason:
            text += "\nLegacy model note (unverified; not a performance assessment):\n" + reason
        else:
            text += "\nNo saved model note. This does not indicate missing gameplay or poor play."
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

    def set_ladder(self, ladder: str | None) -> None:
        self._ladder = ladder
        if self.isVisible():
            self.refresh()  # Otherwise showEvent refreshes when the page opens.

    def refresh(self) -> None:
        try:
            # Fresh instance every time: re-reads match_history from disk so
            # results written by the coach subprocess are always visible.
            history = MatchHistory()
            all_records = list(reversed(history.get_recent(history.total_games)))
            if self._ladder:
                records = [
                    rec
                    for rec in all_records
                    if ranked_ladder(rec.format_name) == self._ladder
                ][:RANK_HISTORY_LIMIT]
            else:
                records = all_records[:_MAX_ROWS]
        except Exception:
            all_records = []
            records = []

        # Preserve the selected row's explanation across the 3s auto-refresh so
        # the "why this score" stays visible instead of clearing every poll.
        selected_match_id = self._selected_match_id()

        if self._cards is not None:
            self._row_records = records
            self._cards.set_records(records)
            row = self._cards.selected_row()
            if row is not None:
                self._show_record(row)
            else:
                self._detail.setText("Select a game to review its recorded result.")
            self._update_summary(records)
            self._update_deck_results(all_records)
            return

        for table in (self._table, self._compact):
            table.setRowCount(0)
        self._row_records = []
        wins = losses = 0
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
            score_text = "Legacy note" if reason else "—"
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

            # Legacy model opinions are not grades or verified findings.
            tip = f"{rtext} · legacy model note (unverified)" if reason else f"{rtext} · recorded result"
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

        self._update_summary(records)
        self._update_deck_results(all_records)

    def _update_deck_results(self, records) -> None:
        """Per-deck autopilot results, drawn from the full history (not the
        row-display limit) so the 20-game sample floor is reachable."""
        entries = autopilot_deck_results(records)
        self._decks.setText("\n".join(_format_deck_result(entry) for entry in entries))
        has_entries = bool(entries)
        self._decks.setVisible(has_entries)
        self._decks_caption.setVisible(has_entries)

    def _update_summary(self, records) -> None:
        wins = sum((r.result or "").lower() == "win" for r in records)
        losses = sum((r.result or "").lower() == "loss" for r in records)
        total = wins + losses
        win_rate = (wins / total * 100) if total else 0.0

        summary = f"{len(records)} shown · {wins}W / {losses}L ({win_rate:.0f}% win rate)"
        if self._ladder:
            summary = (
                f"Ranked {self._ladder} · {summary}"
                if records
                else f"No ranked {self._ladder} matches recorded yet."
            )
        self._summary.setText(summary)
