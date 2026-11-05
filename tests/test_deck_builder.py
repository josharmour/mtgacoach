from unittest.mock import Mock

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from arenamcp.desktop.deck_builder import DeckBuilderDialog


class Session(QObject):
    collectionChanged = Signal(dict)

    def __init__(self):
        super().__init__()
        self.request_collection = Mock()


def candidate(owned=False, affordable=True):
    return {
        "name": "Test list",
        "source": "Test source",
        "source_rank": 1,
        "meta_share": "10%",
        "export": "Deck\n60 Island",
        "cost": {
            "owned": owned,
            "affordable": affordable,
            "missing": {} if owned else {"Spell": 2},
            "spend": {} if owned else {"rare": 2},
            "unknown": [],
        },
    }


def test_filter_and_import_do_not_spend_wildcards(qapp):
    session = Session()
    dialog = DeckBuilderDialog(session)
    dialog._loaded([candidate(owned=True), candidate(), candidate(affordable=False)], "")
    assert dialog.table.rowCount() == 1
    dialog.mode.setCurrentIndex(1)
    assert dialog.table.rowCount() == 2
    dialog.mode.setCurrentIndex(2)
    assert dialog.table.rowCount() == 3
    dialog.table.selectRow(0)
    dialog.copy_button.click()
    assert QApplication.clipboard().text() == "Deck\n60 Island"
    assert "No wildcards have been spent" in dialog.status.text()
    dialog.close()


def test_collection_failure_allows_retry(qapp):
    session = Session()
    dialog = DeckBuilderDialog(session)
    dialog.refresh_button.click()
    session.request_collection.assert_called_once()
    session.collectionChanged.emit({"ok": False, "error": "Return home"})
    assert dialog.refresh_button.isEnabled()
    assert "Return home" in dialog.status.text()
    dialog.close()


def test_popup_explains_reentry_and_opens_builder(qapp, monkeypatch):
    from arenamcp.deck_recommendation import ADVICE_LABEL
    from arenamcp.desktop.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_start_session", lambda self: None)
    win = MainWindow()
    try:
        win._show_decks_view = Mock()
        win.decks_tab.open_builder = Mock()
        win._on_deck_invitation("Would you like help building a new deck?", ADVICE_LABEL)
        popup = win._deck_invitation
        assert "Decks → Build a stronger deck" in popup.informativeText()
        next(b for b in popup.buttons() if popup.buttonRole(b) == QMessageBox.AcceptRole).click()
        win.decks_tab.open_builder.assert_called_once()
        win._show_decks_view.assert_called_once()
    finally:
        win.close()


def test_popup_acceptance_enters_real_builder_and_reads_collection(qapp, monkeypatch):
    from arenamcp.deck_recommendation import ADVICE_LABEL
    from arenamcp.desktop.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_start_session", lambda self: None)
    win = MainWindow()
    try:
        win._session.set_auto_queue = Mock()
        win._session.request_collection = Mock()
        win._session.request_deck_list = Mock()
        win._session.adviceReceived.emit("Would you like help building a new deck?", ADVICE_LABEL)
        popup = win._deck_invitation
        next(b for b in popup.buttons() if popup.buttonRole(b) == QMessageBox.AcceptRole).click()
        assert win._stack.currentIndex() == 3
        assert win.decks_tab._builder.isVisible()
        win._session.set_auto_queue.assert_called_once_with(False)
        win._session.request_collection.assert_called_once()
        win._session.collectionChanged.emit({"ok": False, "error": "bridge offline"})
        assert "bridge offline" in win.decks_tab._builder.status.text()
        assert win.decks_tab._builder.refresh_button.isEnabled()
    finally:
        win.close()


def test_popup_decline_leaves_queue_and_builder_alone(qapp, monkeypatch):
    from arenamcp.deck_recommendation import ADVICE_LABEL
    from arenamcp.desktop.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_start_session", lambda self: None)
    win = MainWindow()
    try:
        win._session.set_auto_queue = Mock()
        win._session.request_collection = Mock()
        win._session.adviceReceived.emit("Would you like help building a new deck?", ADVICE_LABEL)
        popup = win._deck_invitation
        next(b for b in popup.buttons() if popup.buttonRole(b) == QMessageBox.RejectRole).click()
        assert not hasattr(win.decks_tab, "_builder")
        win._session.set_auto_queue.assert_not_called()
        win._session.request_collection.assert_not_called()
    finally:
        win.close()
