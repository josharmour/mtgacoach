"""Decks page: every constructed deck, with a "Deck strategy" button that builds its playbook.

The coach keeps one Oracle-grounded playbook per deck (archetype, how it
wins, the engines, the decision rules). Until 2026-10-11 it was built only
in the background when a match started with an unknown deck. This page
lists the decks the bridge or Player.log has seen, shows which have a
strategy on disk, and builds the missing ones before a match, so the
expensive analysis never runs while the first turns are being played.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
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

from . import theme

_COLUMNS = ["Deck", "Format", "Cards", "Strategy", "Last played"]

_STAGE_TEXT = {
    "cache": "Loaded",
    "discovery": "Thinking: engines and game-changers…",
    "compile": "Writing the playbook…",
    "done": "Ready",
}


def _status_text(strategy: dict[str, Any]) -> tuple[str, str]:
    """(label, tone) for the Strategy column."""
    status = str(strategy.get("status") or "unknown")
    if status == "ready":
        return (str(strategy.get("archetype") or "Ready")[:48], "good")
    if status == "building":
        return (_STAGE_TEXT.get(str(strategy.get("stage") or ""), "Building…"), "accent")
    if status == "incomplete":
        return ("Card list unknown", "muted")
    if status == "error":
        return (f"Failed: {strategy.get('error', '')}"[:60], "bad")
    if status == "none":
        return ("No strategy yet", "warn")
    return ("", "muted")


def _fmt_when(value: str) -> str:
    text = str(value or "").strip('"')
    return text[:16].replace("T", " ") if text else ""


def _section(title: str, body: str) -> str:
    return theme.block(theme.span(title, "muted", size="caption", weight=700), gap=2) + theme.block(body, gap=8)


def report_html(report: dict[str, Any], name: str = "") -> str:
    """The playbook as rich text: how the deck pops off, then the engines and rules."""
    parts = []
    if name:
        parts.append(theme.block(theme.span(name, None, size="title", weight=700), gap=2))
    if report.get("archetype"):
        parts.append(theme.block(theme.span(report["archetype"], "accent", weight=600), gap=8))
    if report.get("summary"):
        parts.append(_section("IN SHORT", theme.span(report["summary"])))
    if report.get("primary_plan"):
        parts.append(_section("HOW IT POPS OFF", theme.span(report["primary_plan"])))
    if report.get("backup_plan"):
        parts.append(_section("WHEN THAT IS DISRUPTED", theme.span(report["backup_plan"])))
    mechanisms = report.get("mechanisms") or []
    if mechanisms:
        items = []
        for m in mechanisms:
            cards = ", ".join(m.get("cards") or [])
            line = theme.span(m.get("effect", ""))
            if cards:
                line += "<br>" + theme.span("Cards: " + cards, "muted", size="caption")
            if m.get("requires"):
                line += "<br>" + theme.span("Needs: " + "; ".join(m["requires"]), "muted", size="caption")
            if m.get("avoid"):
                line += "<br>" + theme.span("Avoid: " + "; ".join(m["avoid"]), "warn", size="caption")
            items.append(f"<li>{line}</li>")
        parts.append(_section("ENGINES AND COMBOS", "<ul>" + "".join(items) + "</ul>"))
    key_cards = report.get("key_cards") or []
    if key_cards:
        rows = "".join(
            f"<li>{theme.span(c.get('name', ''), None, weight=600)}: {theme.span(c.get('role', ''))}</li>"
            for c in key_cards
        )
        parts.append(_section("KEY CARDS", "<ul>" + rows + "</ul>"))
    phases = report.get("phases") or {}
    if phases:
        rows = "".join(
            f"<li>{theme.span(label, None, weight=600)}: {theme.span(phases.get(key, ''))}</li>"
            for key, label in (("early", "Early"), ("mid", "Mid"), ("late", "Late"), ("recovery", "Recovery"))
            if phases.get(key)
        )
        parts.append(_section("GAME PLAN BY PHASE", "<ul>" + rows + "</ul>"))
    commanders = report.get("commanders") or []
    if commanders:
        rows = "".join(
            f"<li>{theme.span(c.get('name', ''), None, weight=600)}: {theme.span(c.get('deployment', ''))}"
            + (f"<br>{theme.span('Keep or recast: ' + c.get('preserve_or_reuse', ''), 'muted', size='caption')}" if c.get("preserve_or_reuse") else "")
            + "</li>"
            for c in commanders
        )
        parts.append(_section("COMMANDER", "<ul>" + rows + "</ul>"))
    rules = report.get("decision_rules") or []
    if rules:
        rows = "".join(
            f"<li>{theme.span(', '.join(r.get('decisions') or []) + ': ', 'muted', size='caption')}"
            f"{theme.span(r.get('prefer', ''))}"
            + (f"<br>{theme.span('When: ' + r.get('when', ''), 'muted', size='caption')}" if r.get("when") else "")
            + (f"<br>{theme.span('Unless: ' + r.get('unless', ''), 'muted', size='caption')}" if r.get("unless") else "")
            + "</li>"
            for r in rules
        )
        parts.append(_section("WHEN TO BREAK THE USUAL RULES", "<ul>" + rows + "</ul>"))
    return "".join(parts) or theme.span("No strategy yet.", "muted")


class DecksTab(QWidget):
    """Lists the decks the coach can see and builds their strategies on demand."""

    def __init__(self, session: Any = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._session = session
        self._decks: list[dict[str, Any]] = []
        self._reports: dict[str, dict[str, Any]] = {}
        self._names: dict[str, str] = {}
        self._build_ui()
        if session is not None:
            session.deckListChanged.connect(self.set_decks)
            session.deckAnalysisChanged.connect(self.on_analysis)

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 0, 2, 8)
        lay.setSpacing(8)

        header = QHBoxLayout()
        self._summary = QLabel("Refresh to list your decks. Arena must be running; the bridge reads every deck, "
                               "Player.log only the ones you have opened or played.")
        self._summary.setWordWrap(True)
        self._summary.setProperty("role", "muted")
        header.addWidget(self._summary, 1)
        self.refresh_btn = QPushButton("Refresh decks")
        self.refresh_btn.setToolTip("Read the deck list from Arena (bridge) and Player.log")
        self.refresh_btn.clicked.connect(self.refresh)
        header.addWidget(self.refresh_btn, 0, Qt.AlignTop)
        lay.addLayout(header)

        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.SingleSelection)
        self._table.setShowGrid(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for column in (1, 2, 4):
            self._table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        lay.addWidget(self._table, 1)

        actions = QHBoxLayout()
        self.strategy_btn = QPushButton("Deck strategy")
        self.strategy_btn.setToolTip("Build (or show) this deck's playbook: how it wins, its engines, its decision rules")
        self.strategy_btn.clicked.connect(lambda: self._analyze_selected(refresh=False))
        self.rebuild_btn = QPushButton("Rebuild")
        self.rebuild_btn.setToolTip("Throw away the cached strategy and analyse the deck again")
        self.rebuild_btn.clicked.connect(lambda: self._analyze_selected(refresh=True))
        self.all_btn = QPushButton("Build all missing")
        self.all_btn.setToolTip("Build a strategy for every deck that has none, one at a time")
        self.all_btn.clicked.connect(self._analyze_all)
        for button in (self.strategy_btn, self.rebuild_btn, self.all_btn):
            button.setEnabled(False)
            actions.addWidget(button)
        actions.addStretch(1)
        lay.addLayout(actions)

        self._detail = QLabel(theme.span("Select a deck.", "muted"))
        self._detail.setWordWrap(True)
        self._detail.setTextFormat(Qt.RichText)
        self._detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._detail.setProperty("card", True)
        self._detail.setContentsMargins(8, 6, 8, 6)
        self._detail.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        lay.addWidget(self._detail)

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        if self._session is not None:
            self._summary.setText("Reading decks…")
            self._session.request_deck_list()

    def selected_deck_id(self) -> str | None:
        rows = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        if not rows:
            return None
        row = rows[0].row()
        return self._decks[row]["deck_id"] if 0 <= row < len(self._decks) else None

    def set_decks(self, decks: list[dict[str, Any]], note: str = "") -> None:
        selected = self.selected_deck_id()
        self._decks = [dict(d) for d in decks if isinstance(d, dict)]
        for deck in self._decks:
            self._names[deck["deck_id"]] = deck.get("name", "")
            report = (deck.get("strategy") or {}).get("report")
            if report:
                self._reports[deck["deck_id"]] = report
        self._table.setRowCount(len(self._decks))
        for row, deck in enumerate(self._decks):
            self._set_row(row, deck)
        complete = sum(1 for d in self._decks if d.get("complete"))
        ready = sum(1 for d in self._decks if (d.get("strategy") or {}).get("status") == "ready")
        summary = f"{len(self._decks)} decks, {complete} with card lists, {ready} with a strategy."
        if note:
            summary += f" {note}"
        if not self._decks:
            summary = "No decks found. " + (note or "Is Arena running?")
        self._summary.setText(summary)
        self.all_btn.setEnabled(any(
            d.get("complete") and (d.get("strategy") or {}).get("status") in ("none", "error") for d in self._decks
        ))
        if selected is not None:
            for row, deck in enumerate(self._decks):
                if deck["deck_id"] == selected:
                    self._table.selectRow(row)
                    break
        self._on_selection_changed()

    def _set_row(self, row: int, deck: dict[str, Any]) -> None:
        label, tone = _status_text(deck.get("strategy") or {})
        cells = [
            deck.get("name", ""),
            deck.get("format", ""),
            str(deck.get("cards") or "") if deck.get("complete") else "?",
            label,
            _fmt_when(deck.get("last_played", "")),
        ]
        for column, text in enumerate(cells):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            if column == 3:
                item.setForeground(theme.qcolor(tone))
            if column == 0:
                item.setToolTip(f"{deck.get('source', '')} · {deck.get('deck_id', '')}")
            self._table.setItem(row, column, item)

    def _deck_at(self, deck_id: str) -> tuple[int, dict[str, Any]] | None:
        for row, deck in enumerate(self._decks):
            if deck["deck_id"] == deck_id:
                return row, deck
        return None

    def on_analysis(self, payload: dict[str, Any]) -> None:
        deck_id = str(payload.get("deck_id") or "")
        found = self._deck_at(deck_id)
        status = {k: v for k, v in payload.items() if k not in ("deck_id", "name", "type")}
        if payload.get("report"):
            self._reports[deck_id] = payload["report"]
        if found is None:
            if payload.get("status") == "error":
                self._summary.setText(f"Deck strategy failed: {payload.get('error', '')}")
            return
        row, deck = found
        deck["strategy"] = status
        self._set_row(row, deck)
        if self.selected_deck_id() == deck_id:
            self._on_selection_changed()  # buttons follow the new status

    def _on_selection_changed(self) -> None:
        deck_id = self.selected_deck_id()
        found = self._deck_at(deck_id) if deck_id else None
        if found is None:
            self._detail.setText(theme.span("Select a deck.", "muted"))
            self.strategy_btn.setEnabled(False)
            self.rebuild_btn.setEnabled(False)
            return
        _row, deck = found
        strategy = deck.get("strategy") or {}
        building = strategy.get("status") == "building"
        self.strategy_btn.setEnabled(bool(deck.get("complete")) and not building)
        self.rebuild_btn.setEnabled(strategy.get("status") == "ready")
        self._show_deck(deck)

    def _show_deck(self, deck: dict[str, Any]) -> None:
        strategy = deck.get("strategy") or {}
        report = self._reports.get(deck["deck_id"])
        if report:
            self._detail.setText(report_html(report, deck.get("name", "")))
            return
        label, tone = _status_text(strategy)
        detail = str(strategy.get("detail") or "")
        if strategy.get("status") == "none":
            detail = "Press Deck strategy to analyse how this deck wins. It takes a minute or two and is kept on disk."
        text = theme.block(theme.span(deck.get("name", ""), None, size="title", weight=700), gap=4) + theme.block(
            theme.span(label, tone) + (f"<br>{theme.span(detail, 'muted')}" if detail else "")
        )
        self._detail.setText(text)

    def _analyze_selected(self, *, refresh: bool) -> None:
        deck_id = self.selected_deck_id()
        if deck_id is None or self._session is None:
            return
        found = self._deck_at(deck_id)
        if found is not None:
            row, deck = found
            deck["strategy"] = {"status": "building", "stage": ""}
            if refresh:
                self._reports.pop(deck_id, None)
            self._set_row(row, deck)
            self._show_deck(deck)
            self.strategy_btn.setEnabled(False)
            self.rebuild_btn.setEnabled(False)
        self._session.analyze_deck(deck_id, refresh=refresh)

    def _analyze_all(self) -> None:
        if self._session is None:
            return
        for row, deck in enumerate(self._decks):
            if deck.get("complete") and (deck.get("strategy") or {}).get("status") in ("none", "error"):
                deck["strategy"] = {"status": "building", "stage": ""}
                self._set_row(row, deck)
        self.all_btn.setEnabled(False)
        self._session.analyze_all_decks()


__all__ = ["DecksTab", "report_html"]
