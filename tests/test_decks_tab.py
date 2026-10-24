"""Decks page: rows, strategy status, the report, and the commands it sends."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal

from arenamcp.desktop.decks_tab import DecksTab, report_html

REPORT = {
    "archetype": "Mono-green ramp",
    "summary": "Ramp into Halflings and swing.",
    "primary_plan": "Dorks into Notary Hobbits, then quadratic mana.",
    "backup_plan": "Beat down with big creatures.",
    "mechanisms": [
        {
            "id": "m1",
            "effect": "Halfling mana",
            "cards": ["Notary Hobbits"],
            "requires": ["3 halflings"],
            "avoid": ["overextending"],
        }
    ],
    "decision_rules": [
        {
            "id": "r1",
            "decisions": ["combat"],
            "prefer": "hold dorks back",
            "when": "engine online",
            "unless": "lethal",
        }
    ],
    "key_cards": [{"name": "Craterhoof", "role": "finisher"}],
    "phases": {"early": "ramp", "late": "overrun"},
    "commanders": [{"name": "Hei Bai", "deployment": "turn 4", "preserve_or_reuse": "recast freely"}],
}


class FakeSession(QObject):
    deckListChanged = Signal(list, str)
    deckAnalysisChanged = Signal(dict)
    collectionChanged = Signal(dict)
    deckOpResult = Signal(dict)

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[tuple] = []

    def request_deck_list(self) -> None:
        self.requests.append(("list",))

    def request_collection(self) -> None:
        self.requests.append(("collection",))

    def request_update_deck(self, deck_id: str, piles: dict) -> None:
        self.requests.append(("update", deck_id, piles))

    def request_create_deck(self, source_deck_id: str, name: str) -> None:
        self.requests.append(("create", source_deck_id, name))

    def request_craft(self, cards: list) -> None:
        self.requests.append(("craft", cards))

    def analyze_deck(self, deck_id: str, *, refresh: bool = False) -> None:
        self.requests.append(("analyze", deck_id, refresh))

    def analyze_all_decks(self) -> None:
        self.requests.append(("all",))


def decks():
    return [
        {
            "deck_id": "a",
            "name": "Hobbits",
            "format": "HistoricBrawl",
            "cards": 100,
            "complete": True,
            "source": "bridge",
            "last_played": '"2026-10-04T23:22:09"',
            "strategy": {"status": "ready", "archetype": "Mono-green ramp", "report": REPORT},
        },
        {
            "deck_id": "b",
            "name": "Mono Red",
            "format": "Standard",
            "cards": 60,
            "complete": True,
            "source": "log",
            "last_played": "",
            "strategy": {"status": "none"},
        },
        {
            "deck_id": "c",
            "name": "Unknown list",
            "format": "Alchemy",
            "cards": 0,
            "complete": False,
            "source": "log",
            "last_played": "",
            "strategy": {"status": "incomplete", "detail": "card list not seen yet"},
        },
    ]


def test_rows_show_strategy_status_and_the_report_on_selection(qapp):
    session = FakeSession()
    tab = DecksTab(session=session)
    tab.refresh()
    assert session.requests == [("list",)]
    session.deckListChanged.emit(decks(), "bridge offline")
    assert tab._table.rowCount() == 3
    assert tab._table.item(0, 3).text() == "Mono-green ramp"
    assert tab._table.item(1, 3).text() == "No strategy yet"
    assert tab._table.item(2, 2).text() == "?"
    assert "3 decks, 2 with card lists, 1 with a strategy" in tab._summary.text()
    assert tab.all_btn.isEnabled()
    tab._table.selectRow(0)
    assert "HOW IT POPS OFF" in tab._detail.text() and "Notary Hobbits" in tab._detail.text()
    assert tab.rebuild_btn.isEnabled() and tab.strategy_btn.isEnabled()
    tab._table.selectRow(2)
    assert not tab.strategy_btn.isEnabled() and "Card list unknown" in tab._detail.text()


def test_strategy_button_sends_the_command_and_streams_progress(qapp):
    session = FakeSession()
    tab = DecksTab(session=session)
    session.deckListChanged.emit(decks(), "")
    tab._table.selectRow(1)
    tab.strategy_btn.click()
    assert session.requests[-1] == ("analyze", "b", False)
    assert tab._table.item(1, 3).text().startswith("Building")
    assert not tab.strategy_btn.isEnabled()
    session.deckAnalysisChanged.emit({"deck_id": "b", "status": "building", "stage": "discovery"})
    assert "Thinking" in tab._table.item(1, 3).text()
    session.deckAnalysisChanged.emit(
        {"deck_id": "b", "status": "ready", "archetype": "Burn", "report": {**REPORT, "archetype": "Burn"}}
    )
    assert tab._table.item(1, 3).text() == "Burn"
    assert "Burn" in tab._detail.text() and tab.rebuild_btn.isEnabled()
    session.deckAnalysisChanged.emit({"deck_id": "b", "status": "error", "error": "server down"})
    assert tab._table.item(1, 3).text().startswith("Failed")
    tab.all_btn.click()
    assert session.requests[-1] == ("all",)


def test_report_html_covers_every_section_and_escapes():
    html = report_html({**REPORT, "summary": "<b>raw</b>"}, "Hobbits")
    for heading in (
        "IN SHORT",
        "HOW IT POPS OFF",
        "ENGINES AND COMBOS",
        "KEY CARDS",
        "GAME PLAN BY PHASE",
        "COMMANDER",
        "WHEN TO BREAK",
    ):
        assert heading in html
    assert "&lt;b&gt;raw&lt;/b&gt;" in html and "<b>raw</b>" not in html
    assert report_html({})  # an empty report still renders a placeholder
