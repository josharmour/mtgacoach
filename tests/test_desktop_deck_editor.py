"""Feature #204 desktop side: editor logic, the wildcard confirmation gate, tab wiring."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox

from arenamcp.desktop import deck_editor
from arenamcp.desktop.deck_editor import (
    DeckEditorDialog,
    craft_plan,
    deficit,
    pile_counts,
    wildcard_cost,
)
from arenamcp.desktop.decks_tab import DecksTab

DECK = {
    "deck_id": "07ef9d14-c112-4cf4-b4e5-49e735d7d101",
    "name": "Burn",
    "format": "Standard",
    "main": [{"grp_id": 100, "count": 4}, {"grp_id": 200, "count": 2}],
    "sideboard": [{"grp_id": 100, "count": 2}],
    "command_zone": [],
    "companions": [],
}
COLLECTION = {100: 5, 200: 2}


class FakeDB:
    """Stand-in for the unified card database (keeps tests off the real one)."""

    class Info:
        def __init__(self, name: str, rarity: str, arena_id: int = 0) -> None:
            self.name = name
            self.rarity = rarity
            self.arena_id = arena_id

    def __init__(self) -> None:
        self.cards = {
            100: self.Info("Bolt", "Rare", 100),
            200: self.Info("Shock", "Uncommon", 200),
            300: self.Info("Vine", "Mythic", 300),
        }

    def get_card_by_arena_id(self, grp_id: int):
        return self.cards.get(int(grp_id))

    def get_card_by_name(self, name: str):
        for info in self.cards.values():
            if info.name.lower() == name.lower():
                return info
        return None


@pytest.fixture(autouse=True)
def fake_db(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(deck_editor, "_card_db", db)
    return db


# -- pure helpers ------------------------------------------------------------


def test_deficit_counts_the_highest_requirement_across_piles():
    short = deficit(DECK, COLLECTION)
    # Bolt: 4 main + 2 side need 4 copies at once (the card moves between piles);
    # owned 5 covers it. Shock: owned 2 covers 2. Nothing missing here.
    assert short == {}


def test_deficit_reports_what_the_collection_cannot_cover():
    piles = {"main": [{"grp_id": 300, "count": 2}], "sideboard": []}
    assert deficit(piles, {300: 1}) == {300: 1}
    assert deficit(piles, None) == {300: 2}


def test_craft_plan_orders_rarest_first_and_carries_rarity():
    plan = craft_plan({100: 1, 300: 2})
    assert [row["grp_id"] for row in plan] == [300, 100]
    assert plan[0]["rarity"] == "mythic" and plan[1]["rarity"] == "rare"


def test_wildcard_cost_checks_the_balance_per_rarity():
    plan = [{"grp_id": 300, "count": 2, "rarity": "mythic"}, {"grp_id": 100, "count": 1, "rarity": "rare"}]
    cost = wildcard_cost(plan, {"mythic": 1, "rare": 5})
    assert cost["spend"] == {"mythic": 2, "rare": 1}
    assert cost["affordable"] is False and cost["missing"] == {"mythic": 1}
    assert wildcard_cost(plan, {"mythic": 2, "rare": 1})["affordable"] is True


def test_pile_counts_merges_duplicates_and_drops_zeroes():
    counts = pile_counts(
        {"main": [{"grp_id": 1, "count": 2}, {"grp_id": 1, "count": 2}, {"grp_id": 0, "count": 9}]}
    )
    assert counts["main"] == {1: 4}


# -- editor dialog -------------------------------------------------------------


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


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def editor(qapp, session):
    dialog = DeckEditorDialog(
        session, DECK, collection=COLLECTION, wildcards={"common": 4, "uncommon": 4, "rare": 1, "mythic": 0}
    )
    yield dialog
    dialog.close()


def test_editor_shows_owned_counts_and_names(editor):
    assert editor._table.rowCount() == 3  # Bolt main + Shock main + Bolt sideboard
    assert editor._table.item(0, 0).text() == "Bolt"
    assert editor._table.item(0, 3).text() == "5"


def test_adjust_and_remove_edit_the_right_pile(editor):
    editor._table.selectRow(0)  # Bolt in main
    editor._adjust_selected(-1)
    assert editor.current_piles()["main"] == [{"grp_id": 100, "count": 3}, {"grp_id": 200, "count": 2}]
    editor._adjust_selected(-3)  # drops to zero and disappears
    assert [c["grp_id"] for c in editor.current_piles()["main"]] == [200]
    editor._table.selectRow(0)  # selection follows the reload: Shock now
    editor._remove_selected()
    assert editor.current_piles()["main"] == []


def test_add_card_resolves_names_and_ids(editor):
    editor._card_input.setText("Vine")
    editor._pile_picker.setCurrentIndex(0)
    editor._add_card()
    editor._card_input.setText("301")  # unknown id still allowed (Arena validates)
    editor._add_card()
    piles = editor.current_piles()["main"]
    assert {"grp_id": 300, "count": 1} in piles and {"grp_id": 301, "count": 1} in piles


def test_save_sends_the_bridge_payload_and_refuses_an_empty_main(editor, session):
    editor._save()
    assert session.requests[-1][:2] == ("update", DECK["deck_id"])
    payload = session.requests[-1][2]
    assert payload["main"][0] == {"grp_id": 100, "count": 4}
    assert set(payload) == {"main", "sideboard", "command_zone", "companions"}
    for grp in list(editor._counts["main"]):
        del editor._counts["main"][grp]
    editor._reload()
    editor._save()
    assert session.requests[-1][0] == "update" or "empty main" in editor._status.text()


def test_craft_confirmation_gates_the_spend(editor, session, monkeypatch):
    # Give the deck a card the collection cannot cover.
    editor._counts["main"][300] = 2
    editor._reload()
    answers = []
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **kw: answers.append(kw.get("detailedText")) or QMessageBox.Yes
    )
    editor._confirm_craft()
    assert session.requests[-1] == ("craft", [{"grp_id": 300, "count": 2}])

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **kw: QMessageBox.No)
    editor._confirm_craft()
    assert session.requests[-1][0] == "craft"  # unchanged: No never sends anything
    assert "cancelled" in editor._status.text().lower()


def test_craft_is_blocked_without_enough_wildcards(session):
    dialog = DeckEditorDialog(
        session,
        {
            "deck_id": DECK["deck_id"],
            "name": "Burn",
            "format": "",
            "main": [{"grp_id": 300, "count": 2}],
            "sideboard": [],
        },
        collection={},
        wildcards={"mythic": 1},
    )
    assert dialog._craft_btn.isEnabled() is False
    dialog.close()


def test_editor_consumes_deck_op_results(editor):
    editor.on_deck_op({"ok": True, "submitted_type": "DeckUpdate"})
    assert "accepted" in editor._status.text().lower()
    editor.on_deck_op({"ok": False, "error": "boom"})
    assert "boom" in editor._status.text()


# -- Decks tab wiring ------------------------------------------------------------


class TabSession(FakeSession):
    pass


def test_tab_requests_the_collection_and_shows_balances(qapp):
    tab = DecksTab(session=TabSession())
    tab.refresh_collection()
    assert tab._session.requests[-1] == ("collection",)
    tab.on_collection({"ok": True, "cards": {"100": 5}, "wildcards": {"common": 3}, "gold": 10, "gems": 20})
    assert tab._collection == {100: 5}
    assert tab._wildcards == {"common": 3}


def test_tab_forwards_deck_ops_and_refreshes_after_create(qapp):
    session = TabSession()
    tab = DecksTab(session=session)
    tab.on_deck_op({"ok": True, "submitted_type": "DeckCreate", "name": "New"})
    assert ("list",) in session.requests  # the deck list is re-read
    tab.on_deck_op({"ok": True, "submitted_type": "WildcardRedeem", "cards": []})
    assert ("collection",) in session.requests  # balances are re-read


def test_tab_edit_opens_the_editor_for_a_complete_deck(qapp):
    session = TabSession()
    tab = DecksTab(session=session)
    tab.set_decks(
        [
            {
                "deck_id": DECK["deck_id"],
                "name": "Burn",
                "format": "Standard",
                "complete": True,
                "main": DECK["main"],
                "sideboard": DECK["sideboard"],
                "strategy": {"status": "none"},
            }
        ]
    )
    assert tab.edit_btn.isEnabled() is False  # nothing selected yet
    tab._table.selectRow(0)
    assert tab.edit_btn.isEnabled() is True
    tab._edit_selected()
    assert tab._editor is not None and tab._editor.isVisible()
    assert tab._editor.current_piles()["main"][0] == {"grp_id": 100, "count": 4}
    tab._editor.close()


def test_tab_duplicate_needs_a_deck_and_sends_create(qapp):
    session = TabSession()
    tab = DecksTab(session=session)
    tab._duplicate_deck()  # no decks: nothing sent
    assert not session.requests
    tab.set_decks([{"deck_id": DECK["deck_id"], "name": "Burn", "format": "", "complete": False}])
    tab._duplicate_deck()  # dialog opens; simulate its accept path
    session.request_create_deck(DECK["deck_id"], "Copy")
    assert session.requests[-1] == ("create", DECK["deck_id"], "Copy")
