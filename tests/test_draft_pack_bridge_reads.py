"""get_draft_pack skips the bridge's draft and deck-editor reads during a match.

2026-10-18 22:15 (Linux/Proton): the coach loop called get_draft_state and
get_deck_editor on every pass mid-match; on BepInEx each is a reflection batch
on Arena's main thread, queued behind the in-game reads, and the coach stalled
for ~17 s at a time. No draft or deck editor can be open during a game.
"""

from __future__ import annotations

from types import SimpleNamespace

from arenamcp import gre_bridge, server


class Bridge:
    connected = True

    def __init__(self):
        self.calls = []

    def get_draft_state(self):
        self.calls.append("get_draft_state")
        return None

    def get_deck_editor(self):
        self.calls.append("get_deck_editor")
        return None


def run(monkeypatch, match_id, turn, result=None):
    bridge = Bridge()
    monkeypatch.setattr(gre_bridge, "get_bridge", lambda: bridge)
    monkeypatch.setattr(server, "watcher", object())
    monkeypatch.setattr(server, "get_completed_match_for_navigation", lambda: {})
    monkeypatch.setattr(
        server,
        "game_state",
        SimpleNamespace(
            match_id=match_id, turn_info=SimpleNamespace(turn_number=turn), last_game_result=result
        ),
    )
    server.get_draft_pack()
    return bridge.calls


def test_no_bridge_draft_reads_mid_match(monkeypatch):
    assert run(monkeypatch, "m-1", 7) == []


def test_draft_screens_are_read_between_matches(monkeypatch):
    assert run(monkeypatch, None, 0) == ["get_draft_state", "get_deck_editor"]
    assert run(monkeypatch, "m-1", 9, result="win") == ["get_draft_state", "get_deck_editor"]
