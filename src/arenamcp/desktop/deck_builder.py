"""An opt-in workspace for sourced decks and collection / wildcard comparisons."""

from __future__ import annotations

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

from arenamcp.deck_candidates import RARITIES, arena_export, compare_collection, load_candidates

from .runtime import open_url


class _Result(QObject):
    ready = Signal(list, str)


class _LoadDecks(QRunnable):
    def __init__(self, collection, wildcards, deck=None):
        super().__init__()
        self.result = _Result()
        self.collection, self.wildcards = dict(collection), dict(wildcards)
        self.deck = deck

    def run(self):
        try:
            if self.deck is None:
                decks = load_candidates(self.collection, self.wildcards)
            else:
                from arenamcp.card_db import get_card_database

                self.deck["cost"] = compare_collection(
                    self.deck, self.collection, self.wildcards, get_card_database()
                )
                decks = [self.deck]
            self.result.ready.emit(decks, "" if decks else "No complete decklists available from the source.")
        except Exception as error:
            self.result.ready.emit([], f"Could not load candidate decks: {error}")


class DeckBuilderDialog(QDialog):
    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Build a stronger deck")
        self.resize(760, 620)
        self._session = session
        self._collection = None
        self._wildcards = {}
        self._candidates = []
        self._visible = []
        self._worker = None
        self._checking_paste = False
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Find a deck you can build with your cards or wildcards, then import it into Arena. "
            "Find this again in Decks → Build a stronger deck.\n"
            "Auto-queue is turned off when you enter this workspace; turn it back on when ready to play.\n"
            "Suggestions: Standard decks from MTGGoldfish, ordered by paper metagame popularity. "
            "For Arena win rates and other formats, explore Untapped and paste a deck export below. "
            "Some Untapped statistics require a subscription."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        actions = QHBoxLayout()
        self.refresh_button = QPushButton("Read collection and find decks")
        self.refresh_button.clicked.connect(self.refresh)
        self.mode = QComboBox()
        self.mode.addItems(["Owned cards only", "Within my wildcard balance", "All candidates"])
        self.mode.currentIndexChanged.connect(self._render)
        source = QPushButton("Untapped Arena decks ↗")
        source.clicked.connect(lambda: open_url("https://mtga.untapped.gg/constructed/standard/decks"))
        for widget in (self.refresh_button, self.mode, source):
            actions.addWidget(widget)
        layout.addLayout(actions)
        self.status = QLabel("Return to Arena's home screen to read your collection and wildcard balances.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Deck / source order", "Meta share", "Missing copies", "Wildcard estimate"]
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
        self._render()
        if self._checking_paste and candidates:
            self.table.selectRow(len(self._visible) - 1)
        self._checking_paste = False
        if error:
            self.status.setText(error + " You can still paste a source deck export to check its cost.")

    def _render(self):
        mode = self.mode.currentIndex()
        self._visible = [
            d for d in self._candidates if mode == 2 or d["cost"]["owned" if mode == 0 else "affordable"]
        ]
        self.table.setRowCount(0)
        for deck in self._visible:
            row = self.table.rowCount()
            self.table.insertRow(row)
            cost = deck["cost"]
            price = (
                ", ".join(f"{cost['spend'].get(r, 0)} {r}" for r in RARITIES if cost["spend"].get(r))
                or "None"
            )
            if cost["unknown"]:
                price += " · unresolved cards"
            for col, text in enumerate(
                [
                    f"{deck.get('source_rank', '—')}. {deck['name']}",
                    deck.get("meta_share", "Not supplied"),
                    str(sum(cost["missing"].values())),
                    price,
                ]
            ):
                self.table.setItem(row, col, QTableWidgetItem(text))
        self.status.setText(
            f"{len(self._visible)} decks match this filter. "
            "Try All candidates or paste another deck if none fit."
        )
        self._select()

    def _selected(self):
        row = self.table.currentRow()
        return self._visible[row] if 0 <= row < len(self._visible) else None

    def _select(self):
        deck = self._selected()
        self.copy_button.setEnabled(deck is not None)
        self.source_button.setEnabled(bool(deck and deck.get("url")))
        self.details.setText(
            f"Source: {deck['source']}. Checked: {deck.get('checked_at', 'locally')}. "
            "Arena verifies format legality and final wildcard cost at import. "
            + ("Unresolved: " + ", ".join(deck["cost"]["unknown"]) if deck["cost"]["unknown"] else "")
            if deck
            else "Select a deck to review its source and cost."
        )

    def _check_pasted(self):
        if self._worker is not None:
            return
        if self._collection is None:
            self.status.setText("Read your collection before checking a deck's cost.")
            return
        deck = {"mainboard": [], "sideboard": []}
        pile = "mainboard"
        for line in self.paste.toPlainText().splitlines():
            if line.strip().lower() == "sideboard":
                pile = "sideboard"
            elif line.strip().lower() in ("commander", "companion"):
                self.status.setText(
                    "This checker supports main deck and sideboard exports; commander/companion imports are not supported yet."
                )
                return
            elif line.strip().lower() != "deck" and line.strip():
                deck[pile].append(line)
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
            QApplication.clipboard().setText(deck["export"])
            self.status.setText(
                "Deck copied. In Arena, open Decks → Import. Review the list and any missing cards; "
                "use Craft All only if you accept Arena's wildcard cost. No wildcards have been spent by Coach."
            )
