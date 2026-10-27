"""Deck editing over the Mac bridge (feature #204): edit piles, craft deficits.

The editor works on the deck record the Decks tab already holds (bridge
``get_decks`` shape) plus the collection from ``get_collection``. Saves go
through ``session.request_update_deck``; crafting deficits goes through
``session.request_craft`` only after the user confirms the wildcard spend in
``_confirm_craft`` — the adapter re-checks ``confirm`` on the wire, so no path
spends wildcards without that dialog.

The pure helpers (``deficit``, ``craft_plan``, ``wildcard_cost``) carry the
rules and are unit-tested without Qt.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import theme

logger = logging.getLogger(__name__)

PILES = ("main", "sideboard", "command_zone", "companions")
EDITABLE_PILES = ("main", "sideboard")
_PILE_LABELS = {
    "main": "Main",
    "sideboard": "Sideboard",
    "command_zone": "Commander",
    "companions": "Companion",
}
_COLUMNS = ["Card", "Pile", "In deck", "Owned"]
_RARITY_RANK = {"mythic": 3, "rare": 2, "uncommon": 1, "common": 0}

_card_db: Any = None


def _database() -> Any:
    """The unified card database, or False when it cannot be loaded."""
    global _card_db
    if _card_db is None:
        try:
            from arenamcp.card_db import get_card_database

            _card_db = get_card_database()
        except Exception as error:  # no local database: show ids, never crash
            logger.debug("Card database unavailable: %s", error)
            _card_db = False
    return _card_db


def card_name(grp_id: int) -> str:
    """Printed name for an Arena grpId, or ``#id`` when no database is loaded."""
    db = _database()
    if not db:
        return f"#{grp_id}"
    info = db.get_card_by_arena_id(int(grp_id))
    return str(getattr(info, "name", "") or f"#{grp_id}")


def card_rarity(grp_id: int) -> str:
    """Arena rarity of a printing ('' when unknown); Arena charges the lowest."""
    db = _database()
    if not db:
        return ""
    info = db.get_card_by_arena_id(int(grp_id))
    return str(getattr(info, "rarity", "") or "").lower()


def pile_counts(piles: dict[str, Any]) -> dict[str, dict[int, int]]:
    """Per pile, grp_id -> count from a bridge deck record."""
    out: dict[str, dict[int, int]] = {}
    for pile in PILES:
        counts: dict[int, int] = {}
        for card in (piles or {}).get(pile) or []:
            grp_id, count = int(card.get("grp_id") or 0), int(card.get("count") or 0)
            if grp_id > 0 and count > 0:
                counts[grp_id] = counts.get(grp_id, 0) + count
        out[pile] = counts
    return out


def deficit(piles: dict[str, Any], collection: dict[int, int] | None) -> dict[int, int]:
    """Copies of each printing the collection cannot cover across all piles.

    A printing needed by two piles counts once at its highest requirement —
    the same physical card moves between piles in Arena.
    """
    owned = collection or {}
    needed: dict[int, int] = {}
    for counts in pile_counts(piles).values():
        for grp_id, count in counts.items():
            needed[grp_id] = max(needed.get(grp_id, 0), count)
    return {
        grp_id: count - int(owned.get(grp_id, 0))
        for grp_id, count in needed.items()
        if count > int(owned.get(grp_id, 0))
    }


def craft_plan(shortfall: dict[int, int]) -> list[dict[str, Any]]:
    """The wildcard spend request rows for a shortfall, rarest first."""
    plan = []
    for grp_id in sorted(shortfall, key=lambda g: (-_RARITY_RANK.get(card_rarity(g), -1), g)):
        plan.append({"grp_id": grp_id, "count": int(shortfall[grp_id]), "rarity": card_rarity(grp_id)})
    return plan


def wildcard_cost(plan: list[dict[str, Any]], wildcards: dict[str, int]) -> dict[str, Any]:
    """Per-rarity spend against the balance, plus whether it is affordable."""
    spend: dict[str, int] = {}
    for row in plan:
        rarity = str(row.get("rarity") or "")
        if rarity:
            spend[rarity] = spend.get(rarity, 0) + int(row["count"])
    balance = {rarity: int((wildcards or {}).get(rarity) or 0) for rarity in _RARITY_RANK}
    missing = {rarity: spend[rarity] - balance[rarity] for rarity in spend if spend[rarity] > balance[rarity]}
    return {"spend": spend, "balance": balance, "affordable": not missing, "missing": missing}


class DeckEditorDialog(QDialog):
    """Edit one deck's piles against the collection; craft what is missing."""

    def __init__(
        self,
        session: Any,
        deck: dict[str, Any],
        collection: dict[int, int] | None = None,
        wildcards: dict[str, int] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._deck = dict(deck)
        self._collection: dict[int, int] = dict(collection or {})
        self._wildcards: dict[str, int] = dict(wildcards or {})
        # The editable truth: absolute counts per editable pile.
        base = pile_counts(self._deck)
        self._counts: dict[str, dict[int, int]] = {
            pile: dict(base.get(pile) or {}) for pile in EDITABLE_PILES
        }
        self.setWindowTitle(f"Edit deck — {self._deck.get('name') or 'Unnamed'}")
        self.setMinimumWidth(560)
        self._build_ui()
        self._reload()

    # -- UI ------------------------------------------------------------------
    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        self._summary = QLabel("")
        self._summary.setWordWrap(True)
        lay.addWidget(self._summary)

        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.SingleSelection)
        self._table.setShowGrid(False)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3):
            self._table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        lay.addWidget(self._table, 1)

        add_row = QHBoxLayout()
        self._pile_picker = QComboBox()
        for pile in EDITABLE_PILES:
            self._pile_picker.addItem(_PILE_LABELS[pile], pile)
        self._card_input = QLineEdit()
        self._card_input.setPlaceholderText("Card name or Arena grpId")
        add_button = QPushButton("Add")
        add_button.clicked.connect(self._add_card)
        add_row.addWidget(self._pile_picker)
        add_row.addWidget(self._card_input, 1)
        add_row.addWidget(add_button)
        lay.addLayout(add_row)

        actions = QHBoxLayout()
        minus_btn = QPushButton("-1")
        minus_btn.clicked.connect(lambda: self._adjust_selected(-1))
        plus_btn = QPushButton("+1")
        plus_btn.clicked.connect(lambda: self._adjust_selected(1))
        remove_btn = QPushButton("Remove row")
        remove_btn.clicked.connect(self._remove_selected)
        self._craft_btn = QPushButton("Craft missing...")
        self._craft_btn.setToolTip("Redeem wildcards for the copies the collection cannot cover")
        self._craft_btn.clicked.connect(self._confirm_craft)
        self.save_btn = QPushButton("Save changes")
        self.save_btn.setToolTip("Write the piles back to Arena through the bridge")
        self.save_btn.clicked.connect(self._save)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        for button in (minus_btn, plus_btn, remove_btn):
            actions.addWidget(button)
        actions.addStretch(1)
        actions.addWidget(self._craft_btn)
        actions.addWidget(self.save_btn)
        actions.addWidget(close_btn)
        lay.addLayout(actions)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        lay.addWidget(self._status)

    # -- state ---------------------------------------------------------------
    def current_piles(self) -> dict[str, list[dict[str, int]]]:
        """The edited deck as a bridge-shaped piles payload."""
        piles: dict[str, list[dict[str, int]]] = {}
        for pile in PILES:
            counts = (
                self._counts[pile]
                if pile in EDITABLE_PILES
                else {int(c["grp_id"]): int(c["count"]) for c in (self._deck.get(pile) or [])}
            )
            piles[pile] = [
                {"grp_id": grp_id, "count": count} for grp_id, count in sorted(counts.items()) if count > 0
            ]
        return piles

    def _shortfall(self) -> dict[int, int]:
        return deficit(self.current_piles(), self._collection)

    def _reload(self) -> None:
        rows: list[tuple[str, int]] = []
        for pile in EDITABLE_PILES:
            rows.extend((pile, grp_id) for grp_id in sorted(self._counts[pile]))
        shortfall = self._shortfall()
        self._table.setRowCount(len(rows))
        for row_index, (pile, grp_id) in enumerate(rows):
            count = self._counts[pile].get(grp_id, 0)
            owned = int(self._collection.get(grp_id, 0))
            cells = [card_name(grp_id), _PILE_LABELS[pile], str(count), str(owned)]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if column == 2 and count > owned:
                    item.setForeground(theme.qcolor("warn"))
                elif column == 3 and owned == 0:
                    item.setForeground(theme.qcolor("muted"))
                if column == 0:
                    item.setToolTip(f"grpId {grp_id}")
                self._table.setItem(row_index, column, item)
            self._table.item(row_index, 0).setData(Qt.UserRole, (pile, grp_id))
        total = sum(card["count"] for pile in self.current_piles().values() for card in pile)
        missing_total = sum(shortfall.values())
        header = f"{self._deck.get('name', '')} · {self._deck.get('format', '')} · {total} cards"
        if missing_total:
            cost = wildcard_cost(craft_plan(shortfall), self._wildcards)
            header += f" · {missing_total} missing from the collection"
            if cost["affordable"]:
                header += " · affordable with current wildcards"
                self._craft_btn.setEnabled(True)
            else:
                header += " · not enough wildcards"
                self._craft_btn.setEnabled(False)
            self._craft_btn.setText(f"Craft missing ({missing_total})...")
        else:
            self._craft_btn.setEnabled(False)
            self._craft_btn.setText("Craft missing...")
        self._summary.setText(theme.span(header, "muted"))

    # -- edits -----------------------------------------------------------------
    def _selected_row(self) -> tuple[str, int] | None:
        row = self._table.currentRow()
        if row < 0:
            return None
        item = self._table.item(row, 0)
        data = item.data(Qt.UserRole) if item else None
        return (str(data[0]), int(data[1])) if data else None

    def _add_card(self) -> None:
        text = self._card_input.text().strip()
        if not text:
            return
        grp_id = self._resolve_card(text)
        if grp_id is None:
            self._status.setText(theme.span(f"Could not resolve a card from “{text}”.", "warn"))
            return
        pile = str(self._pile_picker.currentData() or "main")
        counts = self._counts[pile]
        counts[grp_id] = counts.get(grp_id, 0) + 1
        self._card_input.clear()
        self._reload()

    def _resolve_card(self, text: str) -> int | None:
        if text.isdigit():
            return int(text)
        db = _database()
        if not db:
            return None
        finder = getattr(db, "get_card_by_name", None)
        info = finder(text) if callable(finder) else None
        grp_id = int(getattr(info, "arena_id", 0) or 0)
        return grp_id or None

    def _adjust_selected(self, delta: int) -> None:
        selected = self._selected_row()
        if selected is None:
            return
        pile, grp_id = selected
        counts = self._counts[pile]
        counts[grp_id] = max(0, counts.get(grp_id, 0) + delta)
        if counts[grp_id] == 0:
            del counts[grp_id]
        self._reload()

    def _remove_selected(self) -> None:
        selected = self._selected_row()
        if selected is None:
            return
        self._counts[selected[0]].pop(selected[1], None)
        self._reload()

    # -- save and craft ----------------------------------------------------------
    def _save(self) -> None:
        piles = self.current_piles()
        if not piles["main"]:
            self._status.setText(theme.span("The main pile is empty; Arena would reject the deck.", "warn"))
            return
        deck_id = str(self._deck.get("deck_id") or "")
        if not deck_id:
            self._status.setText(
                theme.span("This deck has no bridge identity; open it in Arena once first.", "warn")
            )
            return
        self.save_btn.setEnabled(False)
        self._status.setText(theme.span("Saving through the bridge…", "muted"))
        self._session.request_update_deck(deck_id, piles)

    def on_deck_op(self, payload: dict[str, Any]) -> None:
        """Feedback from the bridge for the last save/craft (session.deckOpResult)."""
        ok = payload.get("ok") is True
        verb = "Craft" if payload.get("submitted_type") == "WildcardRedeem" else "Save"
        text = (
            f"{verb} accepted by Arena."
            if ok
            else f"{verb} failed: {payload.get('error') or 'unknown error'}"
        )
        self._status.setText(theme.span(text, "good" if ok else "bad"))
        if ok and payload.get("submitted_type") == "WildcardRedeem":
            # The spend lands asynchronously; the Decks tab refreshes the
            # collection and this dialog's Owned column on the next read.
            self.save_btn.setEnabled(True)
            return
        if ok:
            for pile in EDITABLE_PILES:
                self._deck[pile] = list(self.current_piles()[pile])
            self.save_btn.setEnabled(True)

    def _confirm_craft(self) -> None:
        """Ask before spending a single wildcard; never craft without this."""
        shortfall = self._shortfall()
        plan = craft_plan(shortfall)
        if not plan:
            return
        cost = wildcard_cost(plan, self._wildcards)
        lines = [f"{row['count']}× {card_name(row['grp_id'])} ({row['rarity'] or 'rarity?'})" for row in plan]
        spend = ", ".join(f"{count} {rarity}" for rarity, count in sorted(cost["spend"].items()))
        balance = ", ".join(
            f"{cost['balance'][rarity]} {rarity}" for rarity in ("common", "uncommon", "rare", "mythic")
        )
        detail = "\n".join(lines) + f"\n\nSpends: {spend}\nBalance now: {balance}"
        answer = QMessageBox.question(
            self,
            "Spend wildcards?",
            theme.span(f"Craft {sum(int(r['count']) for r in plan)} card(s) with wildcards?", weight=600),
            detailedText=detail,
            defaultButton=QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            self._status.setText(theme.span("Craft cancelled — no wildcards spent.", "muted"))
            return
        cards = [{"grp_id": row["grp_id"], "count": row["count"]} for row in plan]
        self._craft_btn.setEnabled(False)
        self._status.setText(theme.span("Redeeming wildcards…", "muted"))
        self._session.request_craft(cards)


class NewDeckDialog(QDialog):
    """Clone an existing deck under a new name (duplicate-then-edit)."""

    def __init__(self, session: Any, decks: list[dict[str, Any]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._session = session
        self.setWindowTitle("New deck from existing")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(theme.span("Clone a deck, then edit the copy.", "muted")))
        self._picker = QComboBox()
        for deck in decks:
            self._picker.addItem(
                f"{deck.get('name', '')} ({deck.get('format', '')})", str(deck.get("deck_id") or "")
            )
        lay.addWidget(self._picker)
        self._name = QLineEdit()
        self._name.setPlaceholderText("New deck name")
        lay.addWidget(self._name)
        row = QHBoxLayout()
        create_btn = QPushButton("Create")
        create_btn.clicked.connect(self._create)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        row.addStretch(1)
        row.addWidget(create_btn)
        row.addWidget(cancel_btn)
        lay.addLayout(row)

    def _create(self) -> None:
        source = str(self._picker.currentData() or "")
        name = self._name.text().strip()
        if source and name:
            self.accept()
            self._session.request_create_deck(source, name)


__all__ = [
    "DeckEditorDialog",
    "NewDeckDialog",
    "card_name",
    "card_rarity",
    "craft_plan",
    "deficit",
    "pile_counts",
    "wildcard_cost",
]
