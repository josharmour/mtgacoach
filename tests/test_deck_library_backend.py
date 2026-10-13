"""Decks window backend: the bridge deck read, the pipe commands, and the offline playbook build."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import Mock

import pytest

from arenamcp.mac_bridge_adapter import MacBridgeAdapter
from arenamcp.pipe_adapter import PipeAdapter
from arenamcp.standalone_decks import _DeckLibraryMixin


def guid_fields(hex_id: str) -> dict:
    import uuid

    raw = uuid.UUID(hex_id)
    a, b, c = raw.time_low, raw.time_mid, raw.time_hi_version
    rest = raw.bytes[8:]
    out = {"_a": a, "_b": b, "_c": c}
    out.update({f"_{letter}": byte for letter, byte in zip("defghijk", rest, strict=True)})
    return out


def pile(name: str, cards: list[tuple[int, int]]) -> dict:
    return {
        "key": {"e": name, "v": 1},
        "value": {"$c": "List", "$h": 0, "$items": [{"Id": g, "Quantity": q} for g, q in cards]},
    }


class ProbeStub:
    """Answers reflect_batch ops the way the client would for two cached decks."""

    def __init__(self) -> None:
        self.batches: list[list[dict]] = []

    def send(self, command: dict, timeout: float | None) -> dict:
        ops = command["ops"]
        self.batches.append(ops)
        results: list[Any] = []
        for op in ops:
            if op["op"] == "static":
                results.append({"$c": "WrapperController", "$h": 1, "$unity": True})
            elif op.get("member") == "DecksManager":
                results.append({"$c": "DecksManager", "$h": 2})
            elif op.get("method") == "GetAllCachedDecks":
                results.append(
                    {
                        "$c": "List",
                        "$h": 3,
                        "$items": [{"$c": "Client_Deck", "$h": 10}, {"$c": "Client_Deck", "$h": 11}],
                    }
                )
            elif op.get("member") == "Summary":
                deck = op["target"]["h"]
                hex_id = (
                    "07ef9d14c1124cf4b4e549e735d7d101" if deck == 10 else "b7eda01297b0495ebeb941fd976eb679"
                )
                results.append(
                    {
                        "$c": "Client_DeckSummary",
                        "$h": deck * 10,
                        "DeckId": guid_fields(hex_id),
                        "Name": f"Deck {deck}",
                    }
                )
            elif op.get("member") == "Format":
                results.append("HistoricBrawl")
            elif op.get("member") == "Contents":
                results.append({"$c": "Client_DeckContents", "$h": 50})
            elif op.get("member") == "Piles":
                results.append(
                    {
                        "$c": "Dictionary",
                        "$h": 60,
                        "_entries": {
                            "$items": [
                                pile("Main", [(100, 4), (200, 20)]),
                                pile("CommandZone", [(999, 1)]),
                                {"key": {"e": "Invalid"}, "value": None},
                            ]
                        },
                    }
                )
            else:
                results.append(None)
        return {"ok": True, "results": results}


def test_bridge_reads_every_cached_deck_with_its_piles():
    probe = ProbeStub()
    adapter = MacBridgeAdapter(probe.send)
    response = adapter.handle({"action": "get_decks"}, timeout=5.0)
    assert response["ok"]
    assert [d["deck_id"] for d in response["decks"]] == [
        "07ef9d14-c112-4cf4-b4e5-49e735d7d101",
        "b7eda012-97b0-495e-beb9-41fd976eb679",
    ]
    deck = response["decks"][0]
    assert deck["name"] == "Deck 10" and deck["format"] == "HistoricBrawl"
    assert deck["main"] == [{"grp_id": 100, "count": 4}, {"grp_id": 200, "count": 20}]
    assert deck["command_zone"] == [{"grp_id": 999, "count": 1}]
    first = probe.batches[0]
    assert first[0] == {"op": "static", "class": "WrapperController", "member": "Instance", "depth": 0}
    assert first[2]["method"] == "GetAllCachedDecks"
    assert any(op.get("member") == "Piles" and op.get("max_items") == 256 for op in probe.batches[1])


def test_bridge_reports_when_the_wrapper_is_unloaded_mid_match():
    def send(command, timeout):
        return {"ok": True, "results": [None, None, None]}

    response = MacBridgeAdapter(send).handle({"action": "get_decks"}, timeout=5.0)
    assert response["ok"] is False and "outside a match" in response["error"]


class FakePlaybook:
    identity = "abc"
    catalog = {100: {"name": "Bolt"}}
    data = {
        "archetype": "Burn",
        "spoken_summary": "Burn them.",
        "mechanisms": [],
        "card_roles": {},
        "phases": {},
    }


class FakeCoach:
    def __init__(self, cached=None) -> None:
        self.cached = cached
        self.prepared: list[tuple[str, bool]] = []

    def _cached_playbook(self, identity, state):
        return self.cached

    def prepare_playbook(self, state, backend=None, *, refresh=False, progress=None):
        self.prepared.append((state["deck_name"], refresh))
        if progress:
            progress("discovery")
            progress("compile")
        return FakePlaybook()


class Library(_DeckLibraryMixin):
    def __init__(self, coach, bridge=None, log_decks=None) -> None:
        self._coach = coach
        self._bridge = bridge
        self._backend_name = "online"
        self.model_name = None
        self.ui = Mock()
        self._log = log_decks or {}

    def _deck_library_bridge(self):
        return self._bridge

    def _deck_library_backend(self):
        return object()


@pytest.fixture
def library(monkeypatch):
    from arenamcp import deck_library

    def make(coach, bridge=None, log_decks=None):
        lib = Library(coach, bridge, log_decks)
        monkeypatch.setattr(deck_library, "load_log_decks", lambda paths=None: dict(lib._log))
        return lib

    return make


def test_listing_merges_bridge_and_log_and_reports_cache_status(library):
    from arenamcp.deck_library import DeckRecord

    bridge = Mock()
    bridge.draft_command.return_value = {
        "ok": True,
        "decks": [
            {"deck_id": "a", "name": "Burn", "format": "Standard", "main": [{"grp_id": 100, "count": 60}]}
        ],
    }
    lib = library(FakeCoach(cached=FakePlaybook()), bridge, {"b": DeckRecord("b", "Seen in log only")})
    decks, note = lib.collect_decks()
    assert set(decks) == {"a", "b"} and note == ""
    rows = {row["deck_id"]: row for row in lib.deck_payloads()}
    assert rows["a"]["strategy"]["status"] == "ready" and rows["a"]["strategy"]["archetype"] == "Burn"
    assert rows["a"]["strategy"]["report"]["summary"] == "Burn them."
    assert rows["b"]["strategy"]["status"] == "incomplete"
    bridge.draft_command.assert_called_once_with("get_decks", timeout=12.0)

    offline = library(FakeCoach(cached=None), None, {"b": DeckRecord("b", "Log deck", main=[(1, 60)])})
    _decks, note = offline.collect_decks()
    assert "bridge offline" in note
    assert offline.deck_payloads()[0]["strategy"] == {"status": "none"}


def test_analysis_runs_outside_the_match_and_streams_its_stages(library):
    from arenamcp.deck_library import DeckRecord

    coach = FakeCoach(cached=None)
    lib = library(coach, None, {"a": DeckRecord("a", "Burn", main=[(100, 60)])})
    lib.collect_decks()
    payload = lib.analyze_deck_record(lib._deck_library["a"], refresh=True)
    assert payload["status"] == "ready" and payload["archetype"] == "Burn"
    assert coach.prepared == [("Burn", True)]
    stages = [call.args[0]["stage"] for call in lib.ui.emit_deck_analysis.call_args_list]
    assert stages == ["discovery", "compile"]
    assert lib.analyze_deck_record(DeckRecord("x", "No cards"))["status"] == "error"


def test_pipe_commands_drive_the_library_and_emit_events():
    adapter = PipeAdapter()
    events: list[dict] = []
    adapter._emit = events.append  # type: ignore[method-assign]
    coach = Mock()
    adapter._coach = coach
    adapter._dispatch({"cmd": "list_decks"})
    coach.list_decks_async.assert_called_once()
    adapter._dispatch({"cmd": "analyze_deck", "deck_id": "a", "refresh": True})
    coach.analyze_deck_async.assert_called_once_with("a", refresh=True)
    adapter._dispatch({"cmd": "analyze_all_decks"})
    coach.analyze_all_decks_async.assert_called_once()
    adapter.emit_deck_list([{"deck_id": "a"}], "note")
    adapter.emit_deck_analysis({"deck_id": "a", "status": "ready"})
    assert events[-2] == {"type": "deck_list", "decks": [{"deck_id": "a"}], "error": "note"}
    assert events[-1] == {"type": "deck_analysis", "deck_id": "a", "status": "ready"}

    adapter._coach = object()  # no library support
    adapter._dispatch({"cmd": "analyze_deck", "deck_id": "a"})
    assert events[-1]["status"] == "error"


def test_prepare_playbook_builds_and_caches_without_publishing(monkeypatch, tmp_path):
    from arenamcp.coach import CoachEngine
    from test_deck_strategy import deck_case

    state, catalog, response = deck_case()
    monkeypatch.setattr("arenamcp.match_context._local_card", lambda gid, epoch: catalog[gid])
    coach = CoachEngine(backend=Mock())
    monkeypatch.setattr(
        coach, "_playbook_cache_path", staticmethod(lambda identity: tmp_path / f"{identity}.json")
    )
    backend = Mock()
    backend.complete.side_effect = ["notes about the deck", json.dumps(response)]
    coach._deck_playbook = "live-match-playbook"
    stages: list[str] = []
    playbook = coach.prepare_playbook(state, backend=backend, progress=stages.append)
    assert playbook.data["archetype"] == response["archetype"]
    assert stages == ["discovery", "compile", "done"]
    assert coach._deck_playbook == "live-match-playbook"  # the live match keeps its own
    assert list(tmp_path.glob("*.json"))
    discovery_kwargs = backend.complete.call_args_list[0].kwargs
    assert discovery_kwargs["reasoning_effort"] == "high" and discovery_kwargs["max_tokens"] >= 8000
    # Second call: cache hit, no model call.
    again = coach.prepare_playbook(state, backend=Mock(), progress=stages.append)
    assert again.identity == playbook.identity and stages[-1] == "cache"
    with pytest.raises(ValueError):
        coach.prepare_playbook({"deck_cards": []}, backend=backend)
