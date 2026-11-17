"""An opt-in workspace for sourced decks and collection / wildcard comparisons."""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
)

from arenamcp.deck_candidates import (
    RARITIES,
    arena_export,
    bo1_variant,
    compare_collection,
    cost_order_key,
    fleet_autopilot_results,
    load_candidates,
    local_autopilot_results,
    pricing_database,
    rank_candidates,
    split_export,
)

from .runtime import open_url

logger = logging.getLogger(__name__)


class _Result(QObject):
    ready = Signal(list, str)


class _LoadDecks(QRunnable):
    def __init__(self, collection, wildcards, deck=None):
        super().__init__()
        self.result = _Result()
        self.collection, self.wildcards = dict(collection), dict(wildcards)
        self.deck = deck

    def run(self):
        warning = ""
        try:
            if self.deck is None:
                # Rank by results where the coach has them: this machine's
                # recorded games (P1) plus the fleet's shared match-bundle
                # corpus (W1). Both sources keep their recorded queue names,
                # so a rate only attaches on an unambiguous format match.
                results = []
                try:
                    results.extend(dict(row, sample_source="local") for row in local_autopilot_results())
                except Exception as local_error:
                    warning = "Local match history unavailable."
                    logger.warning("local autopilot results unavailable: %s", local_error)
                try:
                    results.extend(dict(row, sample_source="fleet") for row in fleet_autopilot_results())
                except Exception as fleet_error:  # noqa: BLE001 - degraded, not fatal
                    # A missing fleet corpus must never blank the suggestion
                    # table: viable candidates still load and rank on this
                    # machine's measured rates alone. Surface the degraded
                    # source explicitly instead of inventing fleet data or
                    # failing silently.
                    source_note = (
                        "showing this machine's rates only" if not warning else "no measured rates available"
                    )
                    warning = f"{warning} Fleet results unavailable ({fleet_error}); {source_note}.".strip()
                    logger.warning("fleet autopilot results unavailable: %s", fleet_error)
                decks = rank_candidates(load_candidates(self.collection, self.wildcards), results)
            else:
                database = pricing_database()
                self.deck["export"] = arena_export(self.deck, database=database)
                self.deck["cost"] = compare_collection(self.deck, self.collection, self.wildcards, database)
                bo1 = bo1_variant(self.deck, database=database)
                if bo1 is not None:
                    self.deck["bo1_export"] = arena_export(bo1, database=database)
                    self.deck["bo1_cost"] = compare_collection(bo1, self.collection, self.wildcards, database)
                decks = [self.deck]
            message = "" if decks else "No complete decklists available from the source."
            if warning:
                message = f"{warning}\n{message}" if message else warning
            self.result.ready.emit(decks, message)
        except Exception as error:
            self.result.ready.emit([], f"Could not load candidate decks: {error}")


class DeckBuilderDialog(QDialog):
    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Build a stronger deck")
        self.resize(880, 640)
        self._session = session
        self._collection = None
        self._wildcards = {}
        self._candidates = []
        self._visible = []
        self._worker = None
        self._checking_paste = False
        self._source_warning = ""
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Find a deck you can build with your cards or wildcards, then import it into Arena. "
            "Find this again in Decks → Build a stronger deck.\n"
            "Auto-queue is turned off when you enter this workspace; turn it back on when ready to play.\n"
            "Suggestions: Standard and Brawl decks from MTGGoldfish — every archetype the source "
            "lists plus its budget lists, ordered cheapest to craft first. Where the coach's own "
            "autopilot has at least 20 recorded games with a matching deck, its measured Arena win "
            "rate is shown; decks without data are never given one. "
            "Other sources can be checked by pasting a deck export below."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        actions = QHBoxLayout()
        self.refresh_button = QPushButton("Read collection and find decks")
        self.refresh_button.clicked.connect(self.refresh)
        self.mode = QComboBox()
        self.mode.addItems(["Owned cards only", "Within my wildcard balance", "All candidates"])
        self.mode.currentIndexChanged.connect(self._render)
        self.order = QComboBox()
        self.order.setAccessibleName("Deck ordering")
        self.order.addItems(["Fewest wildcards first", "Highest measured win rate first"])
        self.order.currentIndexChanged.connect(self._render)
        self.list_scope = QComboBox()
        self.list_scope.setAccessibleName("List scope")
        self.list_scope.addItems(["Best-of-1 (60-card main deck)", "Full list (with Bo3 sideboard)"])
        self.list_scope.currentIndexChanged.connect(self._render)
        source = QPushButton("Untapped Arena decks ↗")
        source.clicked.connect(lambda: open_url("https://mtga.untapped.gg/constructed/standard/decks"))
        for widget in (self.refresh_button, self.mode, self.order, self.list_scope, source):
            actions.addWidget(widget)
        layout.addLayout(actions)
        self.balance_label = QLabel("")
        self.balance_label.setWordWrap(True)
        layout.addWidget(self.balance_label)
        self.status = QLabel("Return to Arena's home screen to read your collection and wildcard balances.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            [
                "Deck / source order",
                "Meta share",
                "Missing copies",
                "Wildcard estimate",
                "Arena autopilot",
                "Source / retrieved",
            ]
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._select)
        layout.addWidget(self.table, 1)
        self.details = QLabel("")
        self.details.setWordWrap(True)
        self.details.setTextFormat(Qt.PlainText)
        layout.addWidget(self.details)
        self.paste = QTextEdit()
        self.paste.setPlaceholderText(
            "Paste an Arena deck export from your chosen source here to check its cost."
        )
        self.paste.setMaximumHeight(100)
        layout.addWidget(self.paste)
        self.check_button = QPushButton("Check pasted deck against my collection")
        self.check_button.clicked.connect(self._check_pasted)
        layout.addWidget(self.check_button)
        footer = QHBoxLayout()
        self.source_button = QPushButton("View selected source ↗")
        self.source_button.clicked.connect(self._open_source)
        self.copy_button = QPushButton("Copy deck for Arena import")
        self.copy_button.clicked.connect(self._copy)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        for widget in (self.source_button, self.copy_button, close):
            footer.addWidget(widget)
        layout.addLayout(footer)
        if session is not None:
            session.collectionChanged.connect(self._on_collection)
        self._select()

    def refresh(self):
        if self._session is None or self._worker is not None:
            return
        self.refresh_button.setEnabled(False)
        self.check_button.setEnabled(False)
        self.status.setText("Reading collection… Arena must be at its home screen.")
        self._session.request_collection()

    def _on_collection(self, payload):
        if self.refresh_button.isEnabled() or self._worker is not None:
            return
        if payload.get("ok") is not True:
            self.refresh_button.setEnabled(True)
            self.check_button.setEnabled(True)
            self.status.setText(
                f"Collection unavailable: {payload.get('error', 'return to Arena home and retry')}."
            )
            return
        self._collection = {int(k): int(v) for k, v in (payload.get("cards") or {}).items()}
        self._wildcards = dict(payload.get("wildcards") or {})
        balance_summary = ", ".join(f"{int(self._wildcards.get(r) or 0)} {r}" for r in RARITIES)
        self.balance_label.setText(f"Your wildcard balance: {balance_summary}")
        self.status.setText("Finding source decklists and comparing your cards…")
        self._worker = _LoadDecks(self._collection, self._wildcards)
        self._worker.result.ready.connect(self._loaded)
        QThreadPool.globalInstance().start(self._worker)

    def _loaded(self, candidates, error):
        self._worker = None
        self.refresh_button.setEnabled(True)
        self.check_button.setEnabled(True)
        if self._checking_paste:
            self._candidates.extend(candidates)
            self.mode.setCurrentIndex(2)
        else:
            self._candidates = candidates
            self._source_warning = error if candidates else ""
        self._render()
        if self._checking_paste and candidates:
            self.table.selectRow(next(i for i, deck in enumerate(self._visible) if deck is candidates[-1]))
        self._checking_paste = False
        if error and not candidates:
            self.status.setText(error + " You can still paste a source deck export to check its cost.")

    def _use_bo1(self, deck: dict) -> bool:
        return self.list_scope.currentIndex() == 0 and "bo1_cost" in deck and "bo1_export" in deck

    def _active_cost(self, deck: dict) -> dict:
        return deck["bo1_cost"] if self._use_bo1(deck) else deck["cost"]

    def _active_export(self, deck: dict) -> str:
        return deck["bo1_export"] if self._use_bo1(deck) else deck["export"]

    def _shortfall_for(self, cost: dict) -> dict[str, int]:
        explicit = cost.get("shortfall_wildcards")
        if isinstance(explicit, dict):
            return {r: int(explicit[r]) for r in RARITIES if int(explicit.get(r) or 0) > 0}
        if self._collection is None:
            return {}
        spend = cost.get("spend") or {}
        return {
            r: int(spend.get(r) or 0) - int(self._wildcards.get(r) or 0)
            for r in RARITIES
            if int(spend.get(r) or 0) > int(self._wildcards.get(r) or 0)
        }

    def _active_order_key(self, deck: dict) -> tuple:
        return cost_order_key({"cost": self._active_cost(deck), "source_rank": deck.get("source_rank", 999)})

    def _render(self):
        mode = self.mode.currentIndex()
        filtered = [
            d
            for d in self._candidates
            if mode == 2 or bool(self._active_cost(d).get("owned" if mode == 0 else "affordable"))
        ]
        if filtered or not self._candidates:
            if self.order.currentIndex() == 0:
                self._visible = sorted(filtered, key=self._active_order_key)
            else:
                self._visible = sorted(
                    filtered,
                    key=lambda d: (
                        0
                        if isinstance(d.get("arena"), dict) and d["arena"].get("win_rate") is not None
                        else 1,
                        -(float((d.get("arena") or {}).get("win_rate") or 0.0)),
                        self._active_order_key(d),
                    ),
                )
            fallback = False
        else:
            # Never show an empty table: when no candidate fits the filter,
            # show every candidate nearest first (fewest wildcards to craft)
            # and say so, instead of leaving the customer with nothing.
            self._visible = sorted(self._candidates, key=self._active_order_key)
            fallback = True
        self.table.setRowCount(0)
        for deck in self._visible:
            row = self.table.rowCount()
            self.table.insertRow(row)
            cost = self._active_cost(deck)
            price = (
                ", ".join(f"{cost['spend'].get(r, 0)} {r}" for r in RARITIES if cost["spend"].get(r))
                or "None"
            )
            if self._use_bo1(deck):
                price += " (Bo1 main)"
            if cost.get("affordable") is False and not cost.get("unknown"):
                short = self._shortfall_for(cost)
                price += (
                    " · short " + ", ".join(f"{n} {r}" for r, n in short.items())
                    if short
                    else " · exceeds balance"
                )
            if cost["unknown"]:
                price += " · unresolved cards"
            arena = deck.get("arena")
            unit = {"fleet": "matches", "local": "games"}.get(deck.get("arena_sample"), "results")
            arena_text = f"{arena['games']} {unit} · {arena['win_rate']:.0%} win rate" if arena else "—"
            for col, text in enumerate(
                [
                    f"{deck.get('source_rank', '—')}. {deck['name']}",
                    deck.get("meta_share") or ("Budget list" if deck.get("budget") else "Not supplied"),
                    str(sum(cost["missing"].values())),
                    price,
                    arena_text,
                    deck["source"] + "\n" + (deck.get("checked_at") or "Local entry")[:10],
                ]
            ):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                self.table.setItem(row, col, item)
        self.table.resizeRowsToContents()
        if fallback:
            self.status.setText(
                f"0 decks match '{self.mode.currentText()}' — No candidate fits this filter yet; "
                f"showing all {len(self._visible)} candidates nearest first (fewest wildcards to craft)."
            )
        else:
            count_word = "deck matches" if len(self._visible) == 1 else "decks match"
            self.status.setText(
                f"{len(self._visible)} {count_word} this filter. "
                "Try All candidates or paste another deck if none fit."
            )
        if self._source_warning:
            self.status.setText(self.status.text() + "\n" + self._source_warning)
        self._select()

    def _selected(self):
        row = self.table.currentRow()
        return self._visible[row] if 0 <= row < len(self._visible) else None

    def _select(self):
        deck = self._selected()
        self.copy_button.setEnabled(deck is not None)
        self.source_button.setEnabled(bool(deck and deck.get("url")))
        if deck:
            cost = self._active_cost(deck)
            parts = [
                f"[{deck.get('format', 'Pasted')}]",
                f"Source: {deck['source']}.",
                f"Checked: {deck.get('checked_at', 'locally')}.",
            ]
            if self._use_bo1(deck):
                parts.append("Scope: Best-of-1 main deck (sideboard omitted).")
            arena = deck.get("arena")
            if arena:
                source, unit = {
                    "fleet": ("Shared fleet", "matches"),
                    "local": ("This machine", "games"),
                }.get(deck.get("arena_sample"), ("Recorded autopilot", "results"))
                parts.append(
                    f"{source}: {arena['games']} autopilot {unit} with this archetype, "
                    f"{arena['win_rate']:.0%} win rate."
                )
            if cost.get("affordable") is False:
                short = self._shortfall_for(cost)
                if short:
                    parts.append(
                        "Not enough wildcards: short " + ", ".join(f"{n} {r}" for r, n in short.items()) + "."
                    )
                else:
                    parts.append("Not enough wildcards for this list under your current balance.")
            parts.append("Arena verifies format legality and final wildcard cost at import.")
            if cost["unknown"]:
                parts.append("Unresolved: " + ", ".join(cost["unknown"]))
            text = " ".join(parts)
        else:
            text = "Select a deck to review its source and cost."
        self.details.setText(text)

    def _check_pasted(self):
        if self._worker is not None:
            return
        if self._collection is None:
            self.status.setText("Read your collection before checking a deck's cost.")
            return
        deck = split_export(self.paste.toPlainText())
        try:
            deck["export"] = arena_export(deck)
        except ValueError as error:
            self.status.setText(str(error))
            return
        deck.update(name="Pasted deck", source="User-provided export; performance statistics not supplied")
        self._checking_paste = True
        self.refresh_button.setEnabled(False)
        self.check_button.setEnabled(False)
        self.status.setText("Checking the pasted deck against your collection…")
        self._worker = _LoadDecks(self._collection, self._wildcards, deck)
        self._worker.result.ready.connect(self._loaded)
        QThreadPool.globalInstance().start(self._worker)

    def _open_source(self):
        deck = self._selected()
        if deck and deck.get("url"):
            open_url(deck["url"])

    def _copy(self):
        deck = self._selected()
        if deck:
            QApplication.clipboard().setText(self._active_export(deck))
            cost = self._active_cost(deck)
            if cost.get("affordable") is False:
                short = self._shortfall_for(cost)
                short_text = (
                    "short " + ", ".join(f"{n} {r}" for r, n in short.items()) if short else "exceeds balance"
                )
                bo1_hint = (
                    " or switch to Best-of-1" if (not self._use_bo1(deck) and "bo1_export" in deck) else ""
                )
                self.status.setText(
                    f"Deck copied — NOT ENOUGH WILDCARDS ({short_text}). "
                    f"Arena Craft All will fail unless you have those wildcards{bo1_hint}. "
                    "No wildcards have been spent by Coach."
                )
            else:
                self.status.setText(
                    "Deck copied. In Arena, open Decks → Import. Review the list and any missing cards; "
                    "use Craft All only if you accept Arena's wildcard cost. No wildcards have been spent by Coach."
                )
