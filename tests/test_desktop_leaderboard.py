"""Headless Qt tests for LeaderboardTab and Web-to-Arena synced deck UI."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from arenamcp.desktop.leaderboard_tab import LeaderboardTab


class _FakeSession(QObject):
    deckOpResult = Signal(dict)

    def __init__(self) -> None:
        super().__init__()
        self.imported: list[dict] = []
        self.crafted: list[list] = []

    def request_import_synced_deck(self, deck: dict) -> None:
        self.imported.append(deck)

    def request_craft(self, cards: list) -> None:
        self.crafted.append(cards)


def test_leaderboard_tab_renders_standings_decks_and_clipboard_import(qapp) -> None:
    session = _FakeSession()
    tab = LeaderboardTab(session=session)

    # Render leaderboard payload directly
    tab._on_leaderboard_loaded(
        {
            "ok": True,
            "tab": "rank",
            "discord_invite_url": "https://discord.gg/test-invite",
            "my_standing": {"position": 2, "handle": "MePlayer", "rank_label": "Diamond 1"},
            "entries": [
                {
                    "position": 1,
                    "handle": "TopPlayer",
                    "is_patron": True,
                    "is_me": False,
                    "rank_label": "Mythic #5",
                    "win_rate": 0.68,
                    "wins": 34,
                    "losses": 16,
                    "top_deck": "Boros Convoke",
                },
                {
                    "position": 2,
                    "handle": "MePlayer",
                    "is_patron": False,
                    "is_me": True,
                    "rank_label": "Diamond 1",
                    "win_rate": 0.61,
                    "wins": 22,
                    "losses": 14,
                    "top_deck": "Mono Red",
                },
            ],
        }
    )
    assert "#2 as MePlayer" in tab.standing_label.text()
    assert not tab.discord_btn.isHidden()

    # Pending web deck banner appears and sends import command
    tab._set_pending_decks(
        [
            {
                "id": "w-1",
                "name": "Web Burn",
                "format": "standard",
                "main": [{"name": "Mountain", "count": 20}],
            }
        ]
    )
    assert not tab.pending_frame.isHidden()
    tab.pending_build_btn.click()
    assert len(session.imported) == 1
    assert session.imported[0]["name"] == "Web Burn"

    # Clipboard fallback copies export_text to system clipboard
    session.deckOpResult.emit(
        {
            "ok": True,
            "submitted_type": "SyncedDeckImport",
            "mode": "clipboard",
            "name": "Web Burn",
            "export_text": "Deck\n20 Mountain",
        }
    )
    assert QApplication.clipboard().text() == "Deck\n20 Mountain"
    assert "Copied 'Web Burn'" in tab.detail_label.text()


def test_leaderboard_tab_requires_confirmation_before_crafting(qapp, monkeypatch) -> None:
    session = _FakeSession()
    tab = LeaderboardTab(session=session)
    monkeypatch.setattr(tab, "_refresh_profile_and_pending", lambda: None)

    session.deckOpResult.emit(
        {
            "ok": True,
            "submitted_type": "SyncedDeckImport",
            "mode": "bridge",
            "name": "Boros Aggro",
            "missing_count": 2,
            "craft_plan": [{"grp_id": 201, "count": 2, "rarity": "rare"}],
        }
    )
    assert not tab.craft_missing_btn.isHidden()
    assert session.crafted == []

    # Rejecting confirmation does NOT craft
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **kw: QMessageBox.No)
    tab.craft_missing_btn.click()
    assert session.crafted == []

    # Accepting confirmation sends craft command
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **kw: QMessageBox.Yes)
    tab.craft_missing_btn.click()
    assert session.crafted == [[{"grp_id": 201, "count": 2, "rarity": "rare"}]]
