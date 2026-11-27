"""Tests for cloud_sync client and Web-to-Arena deck import (bridge + clipboard fallback)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from arenamcp import cloud_sync
from arenamcp.standalone_decks import _DeckLibraryMixin


class _FakeCardDB:
    def __init__(self):
        self._by_id = {
            101: SimpleNamespace(name="Lightning Strike", arena_id=101, rarity="common", colors=["R"]),
            102: SimpleNamespace(name="Lightning Strike", arena_id=102, rarity="common", colors=["R"]),
            201: SimpleNamespace(name="Slickshot Show-Off", arena_id=201, rarity="rare", colors=["R"]),
            301: SimpleNamespace(name="Mountain", arena_id=301, rarity="common", colors=[]),
        }
        self._by_name = {
            "Lightning Strike": self._by_id[101],
            "Slickshot Show-Off": self._by_id[201],
            "Mountain": self._by_id[301],
        }

    def get_card_by_arena_id(self, gid: int):
        return self._by_id.get(int(gid))

    def get_card_by_name(self, name: str):
        return self._by_name.get(name)


def test_resolve_deck_piles_prefers_owned_printings_and_flags_unresolved() -> None:
    db = _FakeCardDB()
    deck = {
        "main": [
            {"name": "Lightning Strike", "count": 4},
            {"name": "Slickshot Show-Off", "count": 4},
            {"name": "Unknown Card", "count": 2},
        ],
        "sideboard": [],
        "commander": [],
        "companion": [],
    }
    # Player owns printing 102 of Lightning Strike (not 101)
    piles, unresolved = cloud_sync.resolve_deck_piles_to_grp_ids(
        deck, collection={102: 4, 201: 1}, database=db
    )
    assert piles["main"] == [{"grp_id": 102, "count": 4}, {"grp_id": 201, "count": 4}]
    assert unresolved == ["Unknown Card"]


def test_push_sync_payload_serializes_collection_decks_and_matches() -> None:
    db = _FakeCardDB()
    captured = []

    def fake_opener(method, url, body, headers):
        captured.append((method, url, json.loads(body.decode("utf-8")), headers))
        return 200, json.dumps({"ok": True, "synced": {"matches": 1, "decks": 1}})

    deck_rec = SimpleNamespace(
        deck_id="deck-1",
        name="Red Deck",
        format="Standard",
        main=[(102, 4), (301, 20)],
        sideboard=[],
        command_zone=[],
        companions=[],
        complete=True,
        snapshot=False,
        identity=lambda: "ident-red",
    )
    res = cloud_sync.push_sync_payload(
        matches=[{"match_id": "m1", "result": "win", "format_name": "Standard Ranked"}],
        collection_payload={
            "ok": True,
            "cards": {102: 4},
            "wildcards": {"rare": 3},
            "gold": 500,
            "gems": 100,
        },
        decks={"deck-1": deck_rec},
        database=db,
        license_key="sk-test",
        base_url="https://example.test",
        opener=fake_opener,
    )
    assert res["ok"] is True
    assert len(captured) == 1
    method, url, payload, headers = captured[0]
    assert method == "POST"
    assert url == "https://example.test/v1/sync/push"
    assert headers["Authorization"] == "Bearer sk-test"
    assert payload["collection"]["cards_by_name"] == {"Lightning Strike": 4}
    assert payload["decks"][0]["name"] == "Red Deck"
    assert any(c["name"] == "Lightning Strike" for c in payload["catalog_entries"])


def test_cloud_sync_disabled_skips_background_upload(monkeypatch) -> None:
    monkeypatch.setattr(cloud_sync, "is_cloud_sync_enabled", lambda: False)
    called = []
    monkeypatch.setattr(cloud_sync, "push_sync_payload", lambda **kw: called.append(kw))
    cloud_sync.sync_match_async({"match_id": "m1", "result": "win"})
    assert called == []


class _FakeBridge:
    client_runtime = "il2cpp-macos"
    connected = True

    def __init__(self, *, create_ok: bool = True):
        self.calls: list[tuple[str, dict]] = []
        self._created = False
        self._create_ok = create_ok

    def draft_command(self, action: str, timeout: float = 12.0, **kwargs):
        self.calls.append((action, kwargs))
        if action == "get_collection":
            return {"ok": True, "cards": {102: 4, 201: 1, 301: 20}, "wildcards": {"rare": 4}}
        if action == "get_decks":
            decks = [
                {
                    "deck_id": "existing-1",
                    "name": "Starter Red",
                    "format": "Standard",
                    "main": [{"grp_id": 301, "count": 60}],
                    "sideboard": [],
                    "command_zone": [],
                    "companions": [],
                }
            ]
            if self._created:
                decks.append(
                    {
                        "deck_id": "new-deck-99",
                        "name": "Web Aggro",
                        "format": "Standard",
                        "main": [{"grp_id": 301, "count": 60}],
                        "sideboard": [],
                        "command_zone": [],
                        "companions": [],
                    }
                )
            return {"ok": True, "decks": decks}
        if action == "create_deck":
            if not self._create_ok:
                return {"ok": False, "error": "Arena refused clone"}
            self._created = True
            return {"ok": True}
        if action == "update_deck":
            return {"ok": True}
        if action == "craft":
            raise AssertionError("import_synced_deck_async must NEVER auto-craft wildcards!")
        return {"ok": False}


class _FakeCoach(_DeckLibraryMixin):
    def __init__(self, bridge):
        self._bridge = bridge
        self.emitted: list[tuple[str, tuple]] = []

    def _deck_library_bridge(self):
        return self._bridge

    def _deck_library_emit(self, name: str, *args):
        self.emitted.append((name, args))


def test_import_synced_deck_bridge_creates_and_updates_without_autocraft(monkeypatch) -> None:
    db = _FakeCardDB()
    monkeypatch.setattr(cloud_sync, "_load_card_db", lambda: db)
    acks = []
    monkeypatch.setattr(
        cloud_sync, "ack_arena_deck_sync", lambda *a, **kw: acks.append((a, kw)) or {"ok": True}
    )
    monkeypatch.setattr("arenamcp.desktop.deck_editor._database", lambda: db)

    # Run thread synchronously in test
    class _SyncThread:
        def __init__(self, target=None, **kwargs):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr("threading.Thread", _SyncThread)

    bridge = _FakeBridge(create_ok=True)
    coach = _FakeCoach(bridge)
    coach.import_synced_deck_async(
        {
            "id": "web-deck-1",
            "name": "Web Aggro",
            "format": "standard",
            "main": [
                {"name": "Lightning Strike", "count": 4},
                {"name": "Slickshot Show-Off", "count": 4},
                {"name": "Mountain", "count": 20},
            ],
            "sideboard": [],
            "commander": [],
            "companion": [],
        }
    )

    actions = [c[0] for c in bridge.calls]
    assert "create_deck" in actions
    assert "update_deck" in actions
    assert "craft" not in actions
    assert acks and acks[0][0] == ("web-deck-1", "synced")
    assert coach.emitted[0][0] == "emit_deck_op"
    res = coach.emitted[0][1][0]
    assert res["ok"] is True
    assert res["mode"] == "bridge"
    assert res["arena_deck_id"] == "new-deck-99"
    # Slickshot Show-Off needs 4, owns 1 -> missing 3 copies flagged for user confirmation
    assert res["missing_count"] == 3


def test_import_synced_deck_falls_back_to_clipboard_when_bridge_offline(monkeypatch) -> None:
    acks = []
    monkeypatch.setattr(
        cloud_sync, "ack_arena_deck_sync", lambda *a, **kw: acks.append((a, kw)) or {"ok": True}
    )

    class _SyncThread:
        def __init__(self, target=None, **kwargs):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr("threading.Thread", _SyncThread)

    coach = _FakeCoach(bridge=None)
    coach.import_synced_deck_async(
        {
            "id": "web-deck-2",
            "name": "Clipboard Deck",
            "format": "standard",
            "main": [{"name": "Lightning Strike", "count": 4}, {"name": "Mountain", "count": 20}],
            "sideboard": [],
            "commander": [],
            "companion": [],
        }
    )
    res = coach.emitted[0][1][0]
    assert res["ok"] is True
    assert res["mode"] == "clipboard"
    assert "4 Lightning Strike" in res["export_text"]


def test_import_synced_deck_preserves_failed_ack_when_update_deck_fails(monkeypatch) -> None:
    db = _FakeCardDB()
    monkeypatch.setattr(cloud_sync, "_load_card_db", lambda: db)
    acks = []
    monkeypatch.setattr(
        cloud_sync, "ack_arena_deck_sync", lambda *a, **kw: acks.append((a, kw)) or {"ok": True}
    )

    class _SyncThread:
        def __init__(self, target=None, **kwargs):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr("threading.Thread", _SyncThread)

    bridge = _FakeBridge(create_ok=True)
    orig_cmd = bridge.draft_command

    def _fail_update(action: str, timeout: float = 12.0, **kwargs):
        if action == "update_deck":
            return {"ok": False, "error": "UpdateDeck rejected"}
        return orig_cmd(action, timeout=timeout, **kwargs)

    bridge.draft_command = _fail_update
    coach = _FakeCoach(bridge)
    coach.import_synced_deck_async(
        {
            "id": "web-deck-fail",
            "name": "Web Aggro",
            "format": "standard",
            "main": [{"name": "Mountain", "count": 20}],
            "sideboard": [],
            "commander": [],
            "companion": [],
        }
    )
    assert len(acks) == 1
    assert acks[0][0] == ("web-deck-fail", "failed")
    assert acks[0][1]["error"] == "UpdateDeck rejected"
    assert coach.emitted[0][1][0]["mode"] == "clipboard"


def test_backfill_from_local_pushes_history_ranks_and_decks(monkeypatch) -> None:
    db = _FakeCardDB()
    monkeypatch.setattr(cloud_sync, "_load_card_db", lambda: db)
    captured = []

    def fake_opener(method, url, body, headers):
        captured.append(json.loads(body.decode("utf-8")))
        return 200, json.dumps({"ok": True, "synced": {"matches": 1, "decks": 1}})

    class _FakeHistory:
        def get_recent(self, limit):
            return [
                SimpleNamespace(
                    match_id="hist-1",
                    timestamp="2026-11-26T10:00:00Z",
                    result="win",
                    format_name="Ladder",
                    deck_identity="id1",
                    deck_name="Red",
                    local_deck_colors=["R"],
                    opponent_colors_seen=["U"],
                    turns=5,
                    autopilot=True,
                    local_rank_tier=2,
                )
            ]

    monkeypatch.setattr("arenamcp.match_history.MatchHistory", _FakeHistory)
    monkeypatch.setattr(
        cloud_sync, "_latest_rank_snapshot", lambda: {"constructedClass": "Diamond", "constructedLevel": 2}
    )
    monkeypatch.setattr(
        "arenamcp.deck_library.load_log_decks",
        lambda: {
            "d1": SimpleNamespace(
                deck_id="d1",
                name="Log Red",
                format="Standard",
                main=[(102, 4), (301, 20)],
                sideboard=[],
                command_zone=[],
                companions=[],
                complete=True,
                snapshot=False,
                identity=lambda: "id1",
            )
        },
    )
    res = cloud_sync.backfill_from_local(
        license_key="sk-local", base_url="https://example.test", opener=fake_opener
    )
    assert res["ok"] is True
    assert len(captured) == 1
    assert captured[0]["matches"][0]["match_id"] == "hist-1"
    assert captured[0]["ranks"]["constructedClass"] == "Diamond"
    assert captured[0]["decks"][0]["name"] == "Log Red"
