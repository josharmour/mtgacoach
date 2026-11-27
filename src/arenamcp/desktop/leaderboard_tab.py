"""In-app Community Leaderboard, Winning Decks discovery, and Web-to-Arena deck sync."""

from __future__ import annotations

import logging
import sys
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from arenamcp import cloud_sync

from . import theme
from .runtime import open_url

logger = logging.getLogger(__name__)

_USE_TABLES = sys.platform != "darwin" and QApplication.platformName() != "cocoa"


class _AsyncSignals(QObject):
    finished = Signal(dict)


class _Worker(QRunnable):
    def __init__(self, fn) -> None:
        super().__init__()
        self.signals = _AsyncSignals()
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn()
            if not isinstance(result, dict):
                result = {"ok": False, "error": "Unexpected response"}
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        self.signals.finished.emit(result)


def _format_pct(value: Any) -> str:
    if value is None:
        return "—"
    return f"{round(float(value) * 100, 1)}%"


def _format_craft_summary(deck: dict[str, Any]) -> str:
    cinfo = deck.get("craftability")
    if not isinstance(cinfo, dict) or not cinfo.get("has_collection"):
        return "Sync collection on Decks tab to see cost"
    if cinfo.get("owned"):
        return "Owned (0 wildcards)"
    spend = cinfo.get("spend") or {}
    parts = [
        f"{spend[k]}{k[0].upper()}"
        for k in ("mythic", "rare", "uncommon", "common")
        if int(spend.get(k) or 0) > 0
    ]
    cost = " ".join(parts) or "0"
    if cinfo.get("affordable"):
        return f"Buildable ({cost})"
    if cinfo.get("unknown"):
        return f"Missing {cinfo.get('missing_total', 0)} (unpriced cards)"
    return f"Short {cinfo.get('extra_wildcards_needed', 0)} WC ({cost})"


class LeaderboardTab(QWidget):
    """In-app leaderboard, community decks, Discord link, and Web-to-Arena sync."""

    def __init__(self, session: Any = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._session = session
        self._entries: list[dict[str, Any]] = []
        self._pending_decks: list[dict[str, Any]] = []
        self._selected_entry: dict[str, Any] | None = None
        self._discord_url: str = ""
        self._last_craft_plan: list[dict[str, int]] = []
        self._last_craft_deck_name: str = ""
        self._workers: set[_Worker] = set()
        self._row_buttons: dict[int, QPushButton] = {}
        self._lb_seq = 0
        self._build_ui()
        if session is not None:
            session.deckOpResult.connect(self.on_deck_op)

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 0, 2, 8)
        lay.setSpacing(8)

        # Top summary + profile / Discord buttons
        head = QHBoxLayout()
        self.standing_label = QLabel("Loading community standings…")
        self.standing_label.setWordWrap(True)
        self.standing_label.setProperty("role", "muted")
        head.addWidget(self.standing_label, 1)

        self.open_profile_btn = QPushButton("Open My Profile ↗")
        self.open_profile_btn.setToolTip(
            "Sign in to mtgacoach.com with one click to view stats & Deck Builder"
        )
        self.open_profile_btn.clicked.connect(self.open_web_profile)
        head.addWidget(self.open_profile_btn)

        self.discord_btn = QPushButton("Join Discord ↗")
        self.discord_btn.setToolTip("Open the mtgacoach Discord community (Patrons get private rooms)")
        self.discord_btn.clicked.connect(self._open_discord)
        self.discord_btn.hide()
        head.addWidget(self.discord_btn)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        head.addWidget(self.refresh_btn)
        lay.addLayout(head)

        # Handle & opt-in row
        opt_row = QHBoxLayout()
        self.handle_input = QLineEdit()
        self.handle_input.setPlaceholderText("Public leaderboard handle (2–32 chars)")
        self.handle_input.setMaxLength(32)
        self.opt_in_check = QCheckBox("Show me on leaderboard")
        self.save_settings_btn = QPushButton("Save handle")
        self.save_settings_btn.clicked.connect(self.save_profile_settings)
        opt_row.addWidget(self.handle_input, 1)
        opt_row.addWidget(self.opt_in_check)
        opt_row.addWidget(self.save_settings_btn)
        lay.addLayout(opt_row)

        # Pending web-planned decks banner
        self.pending_frame = QFrame()
        self.pending_frame.setProperty("card", True)
        p_lay = QHBoxLayout(self.pending_frame)
        p_lay.setContentsMargins(8, 6, 8, 6)
        self.pending_label = QLabel("")
        self.pending_label.setWordWrap(True)
        self.pending_build_btn = QPushButton("Build in Arena")
        self.pending_build_btn.setToolTip("Create this deck in Arena via bridge (or copy export)")
        self.pending_build_btn.clicked.connect(self._import_first_pending)
        self.pending_copy_btn = QPushButton("Copy export")
        self.pending_copy_btn.clicked.connect(self._copy_first_pending)
        p_lay.addWidget(self.pending_label, 1)
        p_lay.addWidget(self.pending_build_btn)
        p_lay.addWidget(self.pending_copy_btn)
        self.pending_frame.hide()
        lay.addWidget(self.pending_frame)

        # Filters
        filters = QHBoxLayout()
        self.view_combo = QComboBox()
        self.view_combo.setAccessibleName("Leaderboard view")
        self.view_combo.addItem("Highest Rank", "rank")
        self.view_combo.addItem("Best Win Rate (20+ games)", "winrate")
        self.view_combo.addItem("Top Public Decks", "decks")
        self.view_combo.currentIndexChanged.connect(self.refresh_leaderboard_only)

        self.format_combo = QComboBox()
        self.format_combo.setAccessibleName("Leaderboard format")
        for label, val in (
            ("All formats", "all"),
            ("Standard", "standard"),
            ("Brawl", "brawl"),
            ("Historic", "historic"),
            ("Alchemy", "alchemy"),
            ("Limited", "limited"),
        ):
            self.format_combo.addItem(label, val)
        self.format_combo.currentIndexChanged.connect(self.refresh_leaderboard_only)

        self.mode_combo = QComboBox()
        self.mode_combo.setAccessibleName("Play mode")
        self.mode_combo.addItem("All games", "all")
        self.mode_combo.addItem("Autopilot only", "autopilot")
        self.mode_combo.addItem("Manual play", "manual")
        self.mode_combo.currentIndexChanged.connect(self.refresh_leaderboard_only)

        self.window_combo = QComboBox()
        self.window_combo.setAccessibleName("Time window")
        self.window_combo.addItem("Last 30 days", "30d")
        self.window_combo.addItem("Last 7 days", "7d")
        self.window_combo.addItem("All time", "all")
        self.window_combo.currentIndexChanged.connect(self.refresh_leaderboard_only)

        for widget in (self.view_combo, self.format_combo, self.mode_combo, self.window_combo):
            filters.addWidget(widget)
        lay.addLayout(filters)

        # Results list (Cocoa-safe card list vs QTableWidget)
        self._table: QTableWidget | None = None
        if _USE_TABLES:
            self._table = QTableWidget(0, 6)
            self._table.setHorizontalHeaderLabels(
                ["#", "Player / Deck", "Rank / Format", "Win Rate", "Games", "Extra"]
            )
            self._table.verticalHeader().setVisible(False)
            self._table.setEditTriggers(QTableWidget.NoEditTriggers)
            self._table.setSelectionBehavior(QTableWidget.SelectRows)
            self._table.setSelectionMode(QTableWidget.SingleSelection)
            self._table.setShowGrid(False)
            self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
            self._table.itemSelectionChanged.connect(self._on_table_selection)
            lay.addWidget(self._table, 1)
        else:
            self._row_group = QButtonGroup(self)
            self._row_group.setExclusive(True)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            body = QWidget()
            self._rows_layout = QVBoxLayout(body)
            self._rows_layout.setAlignment(Qt.AlignTop)
            scroll.setWidget(body)
            lay.addWidget(scroll, 1)

        # Selected deck actions + craft prompt bar
        deck_actions = QHBoxLayout()
        self.detail_label = QLabel("")
        self.detail_label.setWordWrap(True)
        self.detail_label.setProperty("role", "muted")
        self.build_selected_btn = QPushButton("Build selected deck in Arena")
        self.build_selected_btn.clicked.connect(self._import_selected_deck)
        self.build_selected_btn.hide()
        self.copy_selected_btn = QPushButton("Copy Arena export")
        self.copy_selected_btn.clicked.connect(self._copy_selected_deck)
        self.copy_selected_btn.hide()
        self.craft_missing_btn = QPushButton("Craft missing…")
        self.craft_missing_btn.clicked.connect(self._confirm_craft_missing)
        self.craft_missing_btn.hide()
        deck_actions.addWidget(self.detail_label, 1)
        deck_actions.addWidget(self.build_selected_btn)
        deck_actions.addWidget(self.copy_selected_btn)
        deck_actions.addWidget(self.craft_missing_btn)
        lay.addLayout(deck_actions)

    # ------------------------------------------------------------------
    def _run_async(self, fn, slot) -> None:
        worker = _Worker(fn)
        self._workers.add(worker)

        def _done(res: dict) -> None:
            self._workers.discard(worker)
            slot(res)

        worker.signals.finished.connect(_done)
        QThreadPool.globalInstance().start(worker)

    def refresh(self) -> None:
        self.refresh_leaderboard_only()
        self._refresh_profile_and_pending()

    def _refresh_profile_and_pending(self) -> None:
        def _load() -> dict:
            prof = cloud_sync.fetch_profile_me()
            pending = cloud_sync.fetch_pending_arena_decks()
            return {"ok": True, "profile": prof, "pending": pending}

        self._run_async(_load, self._on_profile_loaded)

    def _on_profile_loaded(self, payload: dict) -> None:
        prof_wrap = payload.get("profile") or {}
        if prof_wrap.get("ok") is True:
            prof = prof_wrap.get("profile") or {}
            self.handle_input.setText(str(prof.get("display_name") or ""))
            self.opt_in_check.setChecked(bool(prof.get("leaderboard_opt_in")))
            discord_info = prof_wrap.get("discord") or {}
            url = str(discord_info.get("invite_url") or "").strip()
            if url:
                self._discord_url = url
                self.discord_btn.show()
        self._set_pending_decks(payload.get("pending") or [])

    def _set_pending_decks(self, decks: list[dict[str, Any]]) -> None:
        self._pending_decks = [d for d in decks if isinstance(d, dict)]
        if not self._pending_decks:
            self.pending_frame.hide()
            return
        first = self._pending_decks[0]
        extra = f" (+{len(self._pending_decks) - 1} more)" if len(self._pending_decks) > 1 else ""
        self.pending_label.setText(
            f"Deck ready from mtgacoach.com: {first.get('name', 'Deck')} "
            f"({first.get('format', 'standard')}){extra}"
        )
        self.pending_frame.show()

    def refresh_leaderboard_only(self) -> None:
        tab = str(self.view_combo.currentData() or "rank")
        fmt = str(self.format_combo.currentData() or "all")
        mode = str(self.mode_combo.currentData() or "all")
        window = str(self.window_combo.currentData() or "30d")
        self._lb_seq += 1
        seq = self._lb_seq
        self.standing_label.setText("Refreshing leaderboard…")
        self._run_async(
            lambda: cloud_sync.fetch_leaderboard(tab=tab, fmt=fmt, mode=mode, window=window),
            lambda res, s=seq: self._on_leaderboard_loaded(res, seq=s),
        )

    def _on_leaderboard_loaded(self, payload: dict, *, seq: int | None = None) -> None:
        if seq is not None and seq != self._lb_seq:
            return
        if payload.get("ok") is not True:
            self.standing_label.setText(f"Leaderboard unavailable: {payload.get('error') or 'offline'}")
            return
        url = str(payload.get("discord_invite_url") or "").strip()
        if url:
            self._discord_url = url
            self.discord_btn.show()
        standing = payload.get("my_standing")
        if isinstance(standing, dict):
            if standing.get("position"):
                self.standing_label.setText(
                    f"Your standing: #{standing['position']} as {standing.get('handle', '')} "
                    f"· {standing.get('rank_label', 'Unranked')}"
                )
            elif not standing.get("leaderboard_opt_in", True):
                self.standing_label.setText(
                    f"Your rank: {standing.get('rank_label', 'Unranked')} · Hidden from leaderboard (check 'Show me on leaderboard' below to appear)."
                )
            else:
                self.standing_label.setText(
                    f"Your rank: {standing.get('rank_label', 'Unranked')} · Play more games to rank in this view."
                )
        else:
            self.standing_label.setText("Community standings loaded.")

        self._entries = [e for e in (payload.get("entries") or []) if isinstance(e, dict)]
        self._render_entries(str(payload.get("tab") or "rank"))

    def _render_entries(self, tab: str) -> None:
        self._selected_entry = None
        self.build_selected_btn.hide()
        self.copy_selected_btn.hide()
        if tab == "decks" and self._entries:
            self.detail_label.setText("Select a deck above to copy or build it in Arena.")
        elif not self._entries:
            self.detail_label.setText("No qualifying entries in this view yet.")
        else:
            self.detail_label.setText("")

        if self._table is not None:
            headers = (
                ["#", "Deck", "Format", "Win Rate", "Author", "Wildcard Cost"]
                if tab == "decks"
                else ["#", "Player", "Rank", "Win Rate", "Record", "Top Deck"]
            )
            self._table.setHorizontalHeaderLabels(headers)
            self._table.setRowCount(len(self._entries))
            for row, item in enumerate(self._entries):
                if tab == "decks":
                    cells = [
                        f"#{item.get('position', row + 1)}",
                        str(item.get("name") or ""),
                        str(item.get("format") or ""),
                        _format_pct(item.get("display_win_rate")),
                        f"{item.get('author_name') or 'Player'}{' ★' if item.get('author_is_patron') else ''}",
                        _format_craft_summary(item),
                    ]
                else:
                    cells = [
                        f"#{item.get('position', row + 1)}",
                        f"{item.get('handle') or ''}{' ★' if item.get('is_patron') else ''}",
                        str(item.get("rank_label") or "Unranked"),
                        _format_pct(item.get("win_rate")),
                        f"{item.get('wins', 0)}W-{item.get('losses', 0)}L",
                        str(item.get("top_deck") or "—"),
                    ]
                for col, text in enumerate(cells):
                    cell = QTableWidgetItem(text)
                    cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
                    if item.get("is_me"):
                        cell.setForeground(theme.qcolor("accent"))
                    self._table.setItem(row, col, cell)
            return

        # Cocoa card buttons
        for btn in self._row_buttons.values():
            self._row_group.removeButton(btn)
            self._rows_layout.removeWidget(btn)
            btn.hide()
            btn.deleteLater()
        self._row_buttons.clear()
        for idx, item in enumerate(self._entries):
            if tab == "decks":
                text = (
                    f"#{item.get('position', idx + 1)} · {item.get('name', '')} ({item.get('format', '')})\n"
                    f"{_format_pct(item.get('display_win_rate'))} "
                    f"({item.get('display_wins', 0)}W-{item.get('display_losses', 0)}L) · "
                    f"{_format_craft_summary(item)}"
                )
            else:
                text = (
                    f"#{item.get('position', idx + 1)} · {item.get('handle', '')}"
                    f"{' ★' if item.get('is_patron') else ''} · {item.get('rank_label', 'Unranked')}\n"
                    f"{_format_pct(item.get('win_rate'))} ({item.get('wins', 0)}W-{item.get('losses', 0)}L)"
                    + (f" · {item.get('top_deck')}" if item.get("top_deck") else "")
                )
            btn = QPushButton(text.replace("&", "&&"))
            btn.setCheckable(True)
            btn.setAccessibleName(text)
            btn.toggled.connect(lambda checked, i=idx: checked and self._select_entry(i))
            self._row_group.addButton(btn)
            self._rows_layout.addWidget(btn)
            self._row_buttons[idx] = btn

    def _on_table_selection(self) -> None:
        if self._table is None:
            return
        rows = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        if rows:
            self._select_entry(rows[0].row())

    def _select_entry(self, idx: int) -> None:
        if not (0 <= idx < len(self._entries)):
            return
        entry = self._entries[idx]
        self._selected_entry = entry
        is_deck = "main" in entry or "export_text" in entry
        self.build_selected_btn.setVisible(is_deck)
        self.copy_selected_btn.setVisible(is_deck)
        if is_deck:
            self.detail_label.setText(f"{entry.get('name', '')} · {_format_craft_summary(entry)}")

    def save_profile_settings(self) -> None:
        handle = self.handle_input.text().strip()
        opt_in = self.opt_in_check.isChecked()
        self.save_settings_btn.setEnabled(False)
        self._run_async(
            lambda: cloud_sync.update_profile_settings(display_name=handle, leaderboard_opt_in=opt_in),
            self._on_settings_saved,
        )

    def _on_settings_saved(self, res: dict) -> None:
        self.save_settings_btn.setEnabled(True)
        if res.get("ok") is True:
            self.standing_label.setText("Saved leaderboard settings.")
            self.refresh_leaderboard_only()
        else:
            self.standing_label.setText(f"Could not save handle: {res.get('error', 'error')}")

    def open_web_profile(self, *, section: str = "") -> None:
        self.open_profile_btn.setEnabled(False)
        self._run_async(
            lambda: cloud_sync.create_profile_login_url(section=section),
            lambda res: self._on_login_url(res, section=section),
        )

    def _on_login_url(self, res: dict, *, section: str = "") -> None:
        self.open_profile_btn.setEnabled(True)
        url = str(res.get("login_url") or "").strip()
        if not url.startswith(("https://", "http://")):
            base = cloud_sync._website_base()
            url = f"{base}/profile" + (f"#{section}" if section else "")
        open_url(url)

    def _open_discord(self) -> None:
        if self._discord_url.startswith(("https://", "http://")):
            open_url(self._discord_url)

    def _import_first_pending(self) -> None:
        if self._pending_decks:
            self._trigger_deck_import(self._pending_decks[0])

    def _copy_first_pending(self) -> None:
        if self._pending_decks:
            self._copy_deck_export(self._pending_decks[0])

    def _import_selected_deck(self) -> None:
        if self._selected_entry:
            self._trigger_deck_import(self._selected_entry)

    def _copy_selected_deck(self) -> None:
        if self._selected_entry:
            self._copy_deck_export(self._selected_entry)

    def _copy_deck_export(self, deck: dict[str, Any]) -> None:
        text = cloud_sync.build_arena_export_text(deck)
        if not text:
            self.detail_label.setText("No exportable card list for this deck.")
            return
        QApplication.clipboard().setText(text)
        self.detail_label.setText(f"Copied '{deck.get('name', 'Deck')}' — paste in Arena → Decks → Import.")

    def _trigger_deck_import(self, deck: dict[str, Any]) -> None:
        if self._session is None:
            self._copy_deck_export(deck)
            return
        self.detail_label.setText(f"Sending '{deck.get('name', 'Deck')}' to Arena…")
        self._session.request_import_synced_deck(deck)

    def on_deck_op(self, payload: dict[str, Any]) -> None:
        if payload.get("submitted_type") != "SyncedDeckImport":
            return
        self._last_craft_plan = []
        self.craft_missing_btn.hide()
        if payload.get("ok") is not True:
            self.detail_label.setText(f"Deck sync failed: {payload.get('error', 'unknown error')}")
            return
        mode = payload.get("mode")
        name = str(payload.get("name") or "Deck")
        if mode == "bridge":
            missing = int(payload.get("missing_count") or 0)
            msg = f"Built '{name}' in Arena!"
            if missing > 0:
                msg += f" ({missing} missing copies — click Craft missing to spend wildcards.)"
                self._last_craft_plan = list(payload.get("craft_plan") or [])
                self._last_craft_deck_name = name
                if self._last_craft_plan:
                    self.craft_missing_btn.setText(f"Craft missing ({missing})…")
                    self.craft_missing_btn.show()
            self.detail_label.setText(msg)
        else:
            export_text = str(payload.get("export_text") or "")
            if export_text:
                QApplication.clipboard().setText(export_text)
            self.detail_label.setText(f"Copied '{name}' to clipboard — open Arena → Decks → Import.")
        self._refresh_profile_and_pending()

    def _confirm_craft_missing(self) -> None:
        if not self._last_craft_plan or self._session is None:
            return
        counts: dict[str, int] = {}
        for row in self._last_craft_plan:
            rarity = str(row.get("rarity") or "unknown")
            counts[rarity] = counts.get(rarity, 0) + int(row.get("count") or 0)
        summary = ", ".join(f"{n} {r}" for r, n in counts.items() if n > 0)
        answer = QMessageBox.question(
            self,
            "Confirm wildcard crafting",
            f"Craft missing cards for '{self._last_craft_deck_name}'?\n\nWildcards to spend: {summary}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self._session.request_craft(self._last_craft_plan)
            self.craft_missing_btn.hide()
            self.detail_label.setText("Wildcard craft request sent to Arena.")


__all__ = ["LeaderboardTab"]
