"""Match History: game results and clearly labelled legacy model notes.

Historical mixed ratings remain in storage but are never presented as performance.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
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


def _canonical_queue(format_name: str) -> str:
    """Bucket a recorded Arena queue name into one comparable format token.

    Queues that differ in format must never pool their games: the twenty-game
    floor and the published rate are per format (real case 2026-11-12: one
    deck with 1 decided Standard Ranked game and 19 Historic Ranked games used
    to publish a pooled 100% rate that attached to Standard candidates).
    Unknown queue names stay their own bucket so nothing silently mixes.
    """
    folded = " ".join((format_name or "").replace("_", " ").lower().split())
    if folded in {
        "ladder",
        "standard ranked",
        "traditional standard ranked",
        "standard ladder",
        "tradladder",
    }:
        return "standard"
    if folded in {"brawl ladder", "brawl ranked", "competitive brawl"}:
        return "brawl"
    return "other:" + folded


def autopilot_deck_results(records) -> list[dict]:
    """Aggregate per-deck results for games the autopilot played.

    Only records where ``autopilot`` is True count (advisor-mode games are the
    player's own play). Games are grouped by deck identity — falling back to
    the deck name when a record predates identity tracking — and by Arena
    queue bucket, so a deck's twenty-game floor and its published rate are
    always measured within one format. Only decided games (win/loss) are
    counted. A bucket's ``win_rate`` is None until it has at least
    MIN_GAMES_FOR_WIN_RATE decided games.
    """
    groups: dict[tuple[str, str], dict] = {}
    legacy_keys: set[tuple[str, str]] = set()
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
        recorded_format = (getattr(rec, "format_name", "") or "").strip()
        bucket_key = (key, _canonical_queue(recorded_format))
        if not identity:
            legacy_keys.add(bucket_key)
        group = groups.setdefault(bucket_key, {"name": name, "wins": 0, "losses": 0, "formats": set()})
        if name and not group["name"]:
            group["name"] = name
        if recorded_format:
            group["formats"].add(recorded_format)
        group["wins" if result == "win" else "losses"] += 1

    paired: list[tuple[tuple[str, str], dict]] = []
    for bucket_key, group in groups.items():
        games = group["wins"] + group["losses"]
        paired.append(
            (
                bucket_key,
                {
                    "name": group["name"] or "Unnamed deck",
                    "games": games,
                    "wins": group["wins"],
                    "losses": group["losses"],
                    # Which Arena queues these games came from (e.g. "Standard
                    # Ranked", "Brawl Ranked") so consumers can require an
                    # unambiguous format match before attaching a rate.
                    "formats": sorted(group["formats"]),
                    # No percentage below the sample-size floor.
                    "win_rate": (group["wins"] / games) if games >= MIN_GAMES_FOR_WIN_RATE else None,
                },
            )
        )

    # Records written before identity tracking carry only a deck name. Fold a
    # legacy bucket into an identified deck when the display name matches
    # exactly one identified deck IN THE SAME QUEUE BUCKET, so one deck never
    # shows as two rows — but never merge two different identities on a name
    # alone and never pool games across queue buckets.
    by_name_and_queue: dict[tuple[str, str], list[tuple[tuple[str, str], dict]]] = {}
    for bucket_key, entry in paired:
        by_name_and_queue.setdefault((entry["name"], bucket_key[1]), []).append((bucket_key, entry))
    for legacy_key in legacy_keys:
        legacy_pair = next((p for p in paired if p[0] == legacy_key), None)
        if legacy_pair is None:
            continue
        peers = by_name_and_queue[(legacy_pair[1]["name"], legacy_key[1])]
        if len(peers) < 2:
            continue  # unique display name in this queue: nothing to fold into
        identified = [p for p in peers if p[0] != legacy_key]
        if len(identified) == 1:
            target = identified[0][1]
            target["wins"] += legacy_pair[1]["wins"]
            target["losses"] += legacy_pair[1]["losses"]
            target["formats"] = sorted(set(target["formats"]) | set(legacy_pair[1]["formats"]))
            target["games"] = target["wins"] + target["losses"]
            target["win_rate"] = (
                (target["wins"] / target["games"]) if target["games"] >= MIN_GAMES_FOR_WIN_RATE else None
            )
            paired.remove(legacy_pair)

    entries = [entry for _, entry in paired]
    entries.sort(key=lambda e: (-e["games"], e["name"]))
    return entries


def _format_deck_result(entry: dict) -> str:
    line = f"{entry['name']} · {entry['games']} games · {entry['wins']}W–{entry['losses']}L"
    if entry["win_rate"] is not None:
        line += f" · {entry['win_rate'] * 100:.0f}% win rate"
    elif entry["games"] > 0:
        pct = entry["wins"] / entry["games"] * 100
        line += f" · {pct:.0f}% (small sample)"
    return line


def _parse_record_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def filter_records(
    records,
    *,
    window_days: int | None = None,
    format_filter: str = "all",
    now: datetime | None = None,
) -> list:
    """Filter MatchRecords by time window (days) and canonical format bucket."""
    ref_now = now or datetime.now(timezone.utc)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=timezone.utc)
    cutoff = (ref_now - timedelta(days=window_days)) if window_days is not None else None
    fmt = (format_filter or "all").strip().lower()
    out = []
    for rec in records:
        if cutoff is not None:
            ts = _parse_record_ts(getattr(rec, "timestamp", "") or "")
            if ts is None or ts < cutoff:
                continue
        if fmt != "all":
            bucket = _canonical_queue(getattr(rec, "format_name", "") or "")
            if fmt == "other":
                if bucket in ("standard", "brawl"):
                    continue
            elif bucket != fmt:
                continue
        out.append(rec)
    return out


def compute_performance_summary(records) -> dict:
    """Compute W-L, win rate, current streak, average turns, and top opponent color matchups."""
    wins = 0
    losses = 0
    turns_sum = 0
    turns_count = 0
    streak_char = ""
    streak_len = 0
    vs_colors: dict[str, list[int]] = {}
    for rec in records:
        res = (getattr(rec, "result", "") or "").lower()
        if res not in ("win", "loss"):
            continue
        is_win = res == "win"
        if is_win:
            wins += 1
        else:
            losses += 1
        ch = "W" if is_win else "L"
        if not streak_char:
            streak_char = ch
            streak_len = 1
        elif streak_char == ch and streak_len > 0:
            streak_len += 1
        else:
            # Streak ended at first mismatch (records are newest-first)
            streak_len = -abs(streak_len)
        t = int(getattr(rec, "turns", 0) or 0)
        if t > 0:
            turns_sum += t
            turns_count += 1
        colors = "".join(sorted(set(getattr(rec, "opponent_colors_seen", None) or [])))
        if colors:
            slot = vs_colors.setdefault(colors, [0, 0])
            slot[0] += 1 if is_win else 0
            slot[1] += 0 if is_win else 1
    total = wins + losses
    matchups = sorted(
        (
            {
                "colors": c,
                "wins": wl[0],
                "losses": wl[1],
                "games": wl[0] + wl[1],
                "win_rate": wl[0] / (wl[0] + wl[1]),
            }
            for c, wl in vs_colors.items()
        ),
        key=lambda m: (-m["games"], -m["win_rate"], m["colors"]),
    )
    return {
        "wins": wins,
        "losses": losses,
        "total": total,
        "win_rate": (wins / total) if total else None,
        "avg_turns": (turns_sum / turns_count) if turns_count else None,
        "streak": f"{streak_char}{abs(streak_len)}" if streak_char else "",
        "matchups": matchups[:6],
    }


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

        filters = QHBoxLayout()
        self.window_filter = QComboBox()
        self.window_filter.addItem("All time", None)
        self.window_filter.addItem("Today (24h)", 1)
        self.window_filter.addItem("Last 7 days", 7)
        self.window_filter.addItem("Last 30 days", 30)
        self.window_filter.currentIndexChanged.connect(self.refresh)
        self.format_filter = QComboBox()
        self.format_filter.addItem("All formats", "all")
        self.format_filter.addItem("Standard", "standard")
        self.format_filter.addItem("Brawl", "brawl")
        self.format_filter.addItem("Other", "other")
        self.format_filter.currentIndexChanged.connect(self.refresh)
        filters.addWidget(self.window_filter)
        filters.addWidget(self.format_filter)
        filters.addStretch(1)
        lay.addLayout(filters)

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

        self._matchups_caption = QLabel("OPPONENT COLOR MATCHUPS")
        self._matchups_caption.setProperty("role", "caption")
        self._matchups_caption.hide()
        lay.addWidget(self._matchups_caption)
        self._matchups = QLabel("")
        self._matchups.setWordWrap(True)
        self._matchups.setProperty("role", "muted")
        self._matchups.hide()
        lay.addWidget(self._matchups)

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
            # The per-deck section follows the same ladder and UI filters as the
            # table above it, drawing from the FULL filtered history (no row
            # limit) so the 20-game sample floor stays reachable.
            window_days = self.window_filter.currentData() if hasattr(self, "window_filter") else None
            fmt_filter = self.format_filter.currentData() if hasattr(self, "format_filter") else "all"
            filtered = filter_records(all_records, window_days=window_days, format_filter=fmt_filter)
            if self._ladder:
                deck_records = [rec for rec in filtered if ranked_ladder(rec.format_name) == self._ladder]
                records = deck_records[:RANK_HISTORY_LIMIT]
            else:
                deck_records = filtered
                records = filtered[:_MAX_ROWS]
        except Exception:
            deck_records = []
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
            self._update_summary(records, deck_records)
            self._update_deck_results(deck_records)
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

        self._update_summary(records, deck_records)
        self._update_deck_results(deck_records)

    def _update_deck_results(self, records) -> None:
        """Per-deck autopilot results, drawn from the ladder-filtered full
        history (not the row-display limit) so the 20-game sample floor is
        reachable and the section never mixes queues."""
        entries = autopilot_deck_results(records)
        self._decks.setText("\n".join(_format_deck_result(entry) for entry in entries))
        has_entries = bool(entries)
        self._decks.setVisible(has_entries)
        self._decks_caption.setVisible(has_entries)

        stats = compute_performance_summary(records)
        matchups = stats["matchups"]
        if hasattr(self, "_matchups"):
            if matchups:
                text = " · ".join(
                    f"vs {m['colors']}: {m['wins']}W–{m['losses']}L ({m['win_rate'] * 100:.0f}%)"
                    for m in matchups
                )
                self._matchups.setText(text)
            self._matchups.setVisible(bool(matchups))
            self._matchups_caption.setVisible(bool(matchups))

    def _update_summary(self, records, all_filtered=None) -> None:
        pool = all_filtered if all_filtered is not None else records
        stats = compute_performance_summary(pool)
        wins = stats["wins"]
        losses = stats["losses"]
        win_rate = (stats["win_rate"] * 100) if stats["win_rate"] is not None else 0.0

        extras = []
        if stats["streak"]:
            extras.append(f"Streak {stats['streak']}")
        if stats["avg_turns"] is not None:
            extras.append(f"Avg {stats['avg_turns']:.1f} turns")
        extra_str = (" · " + " · ".join(extras)) if extras else ""
        shown_part = (
            f"{len(records)} of {len(pool)} shown" if len(pool) > len(records) else f"{len(records)} shown"
        )
        summary = f"{shown_part} · {wins}W / {losses}L ({win_rate:.0f}% win rate){extra_str}"
        if self._ladder:
            summary = (
                f"Ranked {self._ladder} · {summary}"
                if records
                else f"No ranked {self._ladder} matches recorded yet."
            )
        self._summary.setText(summary)
