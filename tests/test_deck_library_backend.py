"""Decks window backend: the bridge deck read, the pipe commands, and the offline playbook build."""

from __future__ import annotations

import json
import threading
from typing import Any
from unittest.mock import Mock

import pytest

from arenamcp.coach import _playbook_call_options
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
        self.prepared: list = []

    def _cached_playbook(self, identity, state, modes=None):
        return self.cached

    def _cancel_idle_playbook_builds(self, reason):
        pass

    def save_deck_strategy(self, playbook):
        self.saved = playbook

    def prepare_playbook(
        self, state, backend=None, *, refresh=False, progress=None, cancel=None, in_match=None
    ):
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
    assert coach.saved.data["archetype"] == "Burn"
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
    from tests.deck_fakes import deck_case

    from arenamcp.coach import CoachEngine

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


def test_saved_deck_strategy_survives_restart_without_the_analysis_cache(monkeypatch, tmp_path, caplog):
    import hashlib
    import logging

    from tests.deck_fakes import deck_case

    from arenamcp.coach import CoachEngine
    from arenamcp.deck_strategy import deck_identity

    state, catalog, response = deck_case()
    monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", tmp_path)
    monkeypatch.setattr("arenamcp.match_context._local_card", lambda gid, epoch: catalog[gid])
    backend = Mock()
    backend.complete.side_effect = ["notes about the deck", json.dumps(response)]
    coach = CoachEngine(backend=backend)
    playbook = coach.prepare_playbook(state)
    coach.save_deck_strategy(playbook)
    coach._playbook_cache_path(playbook.identity).unlink()

    restarted = CoachEngine(backend=Mock())
    with caplog.at_level(logging.INFO):
        assert restarted.analyze_deck(state)
    stored = coach._saved_strategy_path(playbook.identity).read_bytes()
    assert hashlib.sha256(stored).hexdigest() in caplog.text
    assert restarted._deck_playbook.data == playbook.data
    restarted._backend.complete.assert_not_called()
    changed = {**state, "deck_cards": state["deck_cards"] + [state["deck_cards"][0]]}
    assert restarted._cached_playbook(deck_identity(changed), changed) is None


def test_pregame_prepares_only_selected_deck_and_skips_cached_version(library, monkeypatch):
    from arenamcp.deck_library import DeckRecord

    coach = FakeCoach()
    records = {k: DeckRecord(k, k, main=[(gid, 60)]) for k, gid in [("a", 100), ("b", 200)]}
    lib = library(coach, log_decks=records)
    monkeypatch.setattr("arenamcp.server.get_last_queue_selection", lambda: {"deck_id": "b"})
    lib._prepare_selected_deck(threading.Event())
    assert coach.prepared == [("b", False)]
    coach.cached = FakePlaybook()
    lib._prepare_selected_deck(threading.Event())
    assert coach.prepared == [("b", False)]


def test_match_start_cancels_the_prep_worker_even_for_the_same_deck(library):
    """Q3 rework 2026-11 (verifier rejection of 46862f6): a match beginning stops
    the idle prep worker at once even when it is building the very deck the match
    uses — live play never waits on a background thinking build."""
    from arenamcp.deck_library import DeckRecord

    lib = library(FakeCoach())
    record = DeckRecord("a", "First", main=[(100, 60)])
    lib._auto_deck_strategy = True
    lib._deck_prep_identity = record.identity()
    lib._deck_prep_thread = Mock()
    lib._deck_prep_thread.is_alive.return_value = True
    lib._deck_prep_cancel = threading.Event()
    # Same deck as the running build: still stopped at once.
    lib._poll_deck_preparation(False, record.game_state())
    assert lib._deck_prep_cancel.is_set()


def test_idle_poll_starts_only_one_worker_and_wakes_for_a_different_selection(library, monkeypatch):
    lib = library(FakeCoach())
    lib._auto_deck_strategy = True
    selection = {"deck_id": "one"}
    monkeypatch.setattr("arenamcp.server.get_last_queue_selection", lambda: selection)
    entered = threading.Event()
    releases = []

    def prepare(cancel):
        releases.append(cancel)
        entered.set()
        cancel.wait(2)

    monkeypatch.setattr(lib, "_prepare_selected_deck", prepare)
    lib._poll_deck_preparation(True)
    assert entered.wait(1)
    lib._poll_deck_preparation(True)
    assert len(releases) == 1
    selection["deck_id"] = "two"
    lib._poll_deck_preparation(True)
    assert releases[0].is_set()
    lib._deck_prep_thread.join(1)
    entered.clear()
    lib._poll_deck_preparation(True)
    assert entered.wait(1)
    lib._poll_deck_preparation(False)
    lib._deck_prep_thread.join(1)
    assert len(releases) == 2 and releases[1].is_set()


# -- Q3 (2026-11): idle builds think; in-match builds stay compact -----------


def test_hosted_idle_builds_think_while_in_match_builds_stay_compact():
    """2026-10-31 20:29:18 standalone.log: a hosted discovery during a match ran
    at reasoning_effort 'none', max_tokens 1536 with a 60 s cap. Idle builds now
    think at high effort with the full budget and a 300 s cap."""

    class HostedBackend:
        model = "current"

    in_match = _playbook_call_options(HostedBackend(), True)
    assert in_match["enable_thinking"] is False
    assert in_match["reasoning_effort"] == "none"
    assert in_match["max_tokens"] == 1536
    assert in_match["request_timeout_s"] == 60.0

    idle = _playbook_call_options(HostedBackend(), True, outside_match=True)
    assert idle["enable_thinking"] is True
    assert idle["reasoning_effort"] == "high"
    assert idle["max_tokens"] == 12000
    assert idle["request_timeout_s"] == 300.0

    # Backends that already think are unchanged, and so is the compile pass.
    assert _playbook_call_options(object(), True) == _playbook_call_options(
        object(), True, outside_match=True
    )
    assert _playbook_call_options(HostedBackend(), False) == _playbook_call_options(
        HostedBackend(), False, outside_match=True
    )


def test_a_compact_playbook_is_rebuilt_by_the_next_idle_pass(monkeypatch, tmp_path):
    """2026-10-11 10:50:54 standalone.log: 'The Notary Hobbits Digital' was built
    in-match at compact settings; the next idle pass must replace it with a
    thinking build instead of reusing it."""
    from tests.deck_fakes import deck_case

    from arenamcp.coach import CoachEngine, _playbook_prompt_fingerprint

    state, catalog, response = deck_case()
    monkeypatch.setattr("arenamcp.match_context._local_card", lambda gid, epoch: catalog[gid])
    coach = CoachEngine(backend=Mock())
    monkeypatch.setattr(
        coach, "_playbook_cache_path", staticmethod(lambda identity: tmp_path / f"{identity}.json")
    )

    def building_backend():
        backend = Mock()
        backend.complete.side_effect = ["notes about the deck", json.dumps(response)]
        return backend

    playbook = coach.prepare_playbook(state, backend=building_backend())
    path = tmp_path / f"{playbook.identity}.json"
    payload = json.loads(path.read_text())
    assert payload["mode"] == "idle"

    # Demote the entry to what an in-match build leaves behind.
    payload["mode"] = "match"
    payload["prompt"] = _playbook_prompt_fingerprint("match")
    path.write_text(json.dumps(payload))
    coach._playbook_cache.clear()

    rebuild = building_backend()
    again = coach.prepare_playbook(state, backend=rebuild)
    assert rebuild.complete.call_count == 2  # rebuilt with thinking, not reused
    assert json.loads(path.read_text())["mode"] == "idle"
    assert again.identity == playbook.identity

    # A reader that accepts any build mode still reuses the entry with no model call.
    assert coach._cached_playbook(playbook.identity, state) is not None


class SweepCoach(FakeCoach):
    """Per-deck cache awareness for the idle sweep."""

    def __init__(self, cached_ids=()) -> None:
        super().__init__()
        self.cached_ids = set(cached_ids)
        self.prepared: list[str] = []

    def _cached_playbook(self, identity, state, modes=None):
        return FakePlaybook() if identity in self.cached_ids else None

    def prepare_playbook(
        self, state, backend=None, *, refresh=False, progress=None, cancel=None, in_match=None
    ):
        self.prepared.append(state["deck_name"])
        return FakePlaybook()


def test_idle_sweep_builds_each_uncached_deck_newest_first(library):
    """Q3 2026-11: builds used to start only when a match began (37-89 s in on
    the Mac), so early turns ran on generic heuristics. The idle sweep warms
    every complete deck without a thinking playbook, newest first, skipping
    decks that already have one and snapshot-only decks."""
    from arenamcp.deck_library import DeckRecord

    records = {
        "old": DeckRecord("old", "Ancient", main=[(100, 60)], last_played="2026-09-01"),
        "new": DeckRecord("new", "Fresh", main=[(200, 60)], last_played="2026-10-11"),
        "mid": DeckRecord("mid", "Middle", main=[(300, 60)], last_played="2026-10-02"),
        "cached": DeckRecord("cached", "Warmed", main=[(400, 60)], last_played="2026-10-05"),
        "snap": DeckRecord("snap", "Snapshot", main=[(500, 60)], last_played="2026-10-06", snapshot=True),
        "thin": DeckRecord("thin", "NoCards"),
    }
    coach = SweepCoach(cached_ids={records["cached"].identity()})
    lib = library(coach, log_decks=records)
    # Cold Home session: no Recent Match selection and nobody has opened Decks yet.
    lib._build_missing_playbooks(threading.Event())
    assert coach.prepared == ["Fresh", "Middle", "Ancient"]


def test_idle_sweep_stops_when_a_match_starts(library):
    """Q3 2026-11: the sweep shares the model server with live play; the moment
    a match begins the remaining decks wait for the next idle window."""
    from arenamcp.deck_library import DeckRecord

    records = {
        "a": DeckRecord("a", "First", main=[(100, 60)], last_played="2026-10-11"),
        "b": DeckRecord("b", "Second", main=[(200, 60)], last_played="2026-10-01"),
    }

    class MatchStartingCoach(SweepCoach):
        def __init__(self) -> None:
            super().__init__()
            self.owner = None

        def prepare_playbook(
            self, state, backend=None, *, refresh=False, progress=None, cancel=None, in_match=None
        ):
            self.prepared.append(state["deck_name"])
            self.owner._draft_event_in_match = lambda: True
            return FakePlaybook()

    coach = MatchStartingCoach()
    lib = library(coach, log_decks=records)
    coach.owner = lib
    lib.collect_decks()
    lib._build_missing_playbooks(threading.Event())
    assert coach.prepared == ["First"]  # 'Second' waits for the next idle window


def test_match_start_stops_the_running_idle_build_mid_call(library):
    """Q3 rework 2026-11 (verifier rejection of 46862f6): the sweep's currently
    running build stops mid-call when a match begins — including when the match
    uses the same deck — instead of finishing and moving on to the next deck."""
    from arenamcp.deck_library import DeckRecord

    records = {
        "a": DeckRecord("a", "First", main=[(100, 60)], last_played="2026-10-11"),
        "b": DeckRecord("b", "Second", main=[(200, 60)], last_played="2026-10-01"),
    }

    class MidCallMatchCoach(SweepCoach):
        """Two-pass build honouring cancel; the match starts inside pass one."""

        def __init__(self) -> None:
            super().__init__()
            self.owner = None
            self.compiled: list[str] = []

        def prepare_playbook(
            self, state, backend=None, *, refresh=False, progress=None, cancel=None, in_match=None
        ):
            if cancel is not None and cancel.is_set():
                raise ValueError("Deck analysis yielded or was superseded")
            self.prepared.append(state["deck_name"])
            # Mid-call: a match begins with this very deck.
            self.owner._poll_deck_preparation(False, state)
            if cancel is not None and cancel.is_set():
                raise ValueError("Deck analysis yielded or was superseded")
            self.compiled.append(state["deck_name"])
            return FakePlaybook()

    coach = MidCallMatchCoach()
    lib = library(coach, log_decks=records)
    coach.owner = lib
    lib._auto_deck_strategy = True
    lib._deck_prep_thread = Mock()
    lib._deck_prep_thread.is_alive.return_value = True
    lib._deck_prep_cancel = threading.Event()
    lib.collect_decks()
    lib._build_missing_playbooks(lib._deck_prep_cancel)
    assert coach.prepared == ["First"]
    assert coach.compiled == []  # the running build never reached its compile pass


def test_match_boundaries_cancel_idle_builds_but_not_in_match_builds():
    """Q3 rework 2026-11 (verifier rejection of 46862f6): clear_deck_strategy and
    begin_deck_analysis stop pending outside-match builds — this deck's included —
    while an in-match build keeps its protected lifecycle."""
    from arenamcp.coach import CoachEngine

    def pending(coach, identity, mode):
        cancel = threading.Event()
        coach._playbook_cancels[identity] = cancel
        coach._playbook_cancel_modes[identity] = mode
        return cancel

    coach = CoachEngine(backend=Mock())
    idle_same = pending(coach, "a" * 16, "idle")
    match_other = pending(coach, "b" * 16, "match")
    coach.begin_deck_analysis("a" * 16)
    assert idle_same.is_set()  # same-deck idle build stops at once
    assert match_other.is_set()  # a different deck's build is superseded

    coach = CoachEngine(backend=Mock())
    match_same = pending(coach, "c" * 16, "match")
    coach.begin_deck_analysis("c" * 16)
    assert not match_same.is_set()  # in-match build for the same deck is preserved

    coach = CoachEngine(backend=Mock())
    idle = pending(coach, "d" * 16, "idle")
    in_match = pending(coach, "e" * 16, "match")
    coach.clear_deck_strategy()
    assert idle.is_set() and not in_match.is_set()


def test_match_start_aborts_an_idle_thinking_build_and_the_match_builds_its_own(monkeypatch, tmp_path):
    """Q3 rework 2026-11 (verifier rejection of 46862f6): with an idle thinking
    build in flight for this very deck, a match starting stops it between passes
    (compile never runs, nothing is stored) and the live analysis builds its own
    compact playbook instead of waiting on or failing after the idle build."""
    from tests.deck_fakes import deck_case

    from arenamcp.coach import CoachEngine

    state, catalog, response = deck_case()
    monkeypatch.setattr("arenamcp.match_context._local_card", lambda gid, epoch: catalog[gid])
    coach = CoachEngine(backend=Mock())
    monkeypatch.setattr(
        coach, "_playbook_cache_path", staticmethod(lambda identity: tmp_path / f"{identity}.json")
    )

    in_discovery = threading.Event()
    release_discovery = threading.Event()
    idle_calls: list[str] = []

    def idle_discovery(system, message, **kwargs):
        idle_calls.append("discovery")
        in_discovery.set()
        release_discovery.wait(5)
        return "notes about the deck"

    idle_backend = Mock()
    idle_backend.complete.side_effect = idle_discovery

    result: dict = {}

    def build_idle():
        try:
            coach.prepare_playbook(state, backend=idle_backend)
            result["ok"] = True
        except Exception as error:  # noqa: BLE001 - mirrors the production abort path
            result["error"] = error

    worker = threading.Thread(target=build_idle)
    worker.start()
    assert in_discovery.wait(2)

    # A match starts with this same deck while discovery is in flight.
    coach.clear_deck_strategy()
    release_discovery.set()
    worker.join(5)
    assert isinstance(result.get("error"), ValueError)
    assert idle_calls == ["discovery"]  # compile never ran
    assert not list(tmp_path.glob("*.json"))  # the aborted build stored nothing

    # The live match analyses the same deck and gets its own compact playbook.
    live_backend = Mock()
    live_backend.complete.side_effect = ["live notes", json.dumps(response)]
    strategy = coach.analyze_deck(state, backend=live_backend)
    assert strategy is not None
    assert live_backend.complete.call_count == 2


def test_live_analysis_waits_out_a_cancelled_concurrent_build_then_builds(monkeypatch, tmp_path):
    """Q3 rework 2026-11 (verifier rejection of 46862f6): at match start the idle
    build for this very deck is cancelled; the live analysis waits out that dying
    run and then builds its own compact playbook instead of raising 'The
    concurrent analysis of this deck produced no playbook'."""
    import time

    from tests.deck_fakes import deck_case

    from arenamcp.coach import CoachEngine
    from arenamcp.deck_strategy import deck_identity

    state, catalog, response = deck_case()
    monkeypatch.setattr("arenamcp.match_context._local_card", lambda gid, epoch: catalog[gid])
    coach = CoachEngine(backend=Mock())
    monkeypatch.setattr(
        coach, "_playbook_cache_path", staticmethod(lambda identity: tmp_path / f"{identity}.json")
    )
    identity = deck_identity(state)

    # A concurrent (about-to-be-cancelled) run holds the slot; it winds down
    # exactly as _finish_playbook_run does: entry removed first, event set after.
    stale = threading.Event()
    coach._playbook_runs[identity] = stale

    def wind_down():
        time.sleep(0.1)
        coach._finish_playbook_run(identity, stale)

    threading.Thread(target=wind_down).start()

    backend = Mock()
    backend.complete.side_effect = ["live notes", json.dumps(response)]
    strategy = coach.analyze_deck(state, backend=backend)
    assert strategy is not None
    assert backend.complete.call_count == 2


def test_clearing_strategy_stops_a_queued_build_before_the_lane_grant(monkeypatch, tmp_path):
    """Q3 rework 2 (verifier rejection of 4089f58): a Decks-window build with
    cancel=None that is still WAITING for the strategic lane when a match
    boundary lands stops at the boundary — no model call, nothing stored."""
    import time

    from tests.deck_fakes import deck_case

    from arenamcp.coach import CoachEngine
    from arenamcp.deck_strategy import deck_identity
    from arenamcp.game_plan import STRATEGIC_LANE

    state, catalog, response = deck_case()
    monkeypatch.setattr("arenamcp.match_context._local_card", lambda gid, epoch: catalog[gid])
    coach = CoachEngine(backend=Mock())
    monkeypatch.setattr(
        coach, "_playbook_cache_path", staticmethod(lambda identity: tmp_path / f"{identity}.json")
    )
    identity = deck_identity(state)

    held = STRATEGIC_LANE.try_acquire("held")
    assert held is not None
    try:
        backend = Mock()
        result: dict = {}

        def build_idle():
            try:
                coach.prepare_playbook(state, backend=backend)
                result["ok"] = True
            except Exception as error:  # noqa: BLE001 - mirrors the production abort path
                result["error"] = error

        worker = threading.Thread(target=build_idle)
        worker.start()
        # The build is queued: its cancellation is registered before the grant.
        deadline = time.time() + 5
        while identity not in coach._playbook_cancels and time.time() < deadline:
            time.sleep(0.01)
        assert identity in coach._playbook_cancels

        # The boundary lands while the build still waits for the lane.
        coach.clear_deck_strategy()
        assert coach._playbook_cancels[identity].is_set()
    finally:
        STRATEGIC_LANE.release(held)

    worker.join(5)
    assert isinstance(result.get("error"), ValueError)
    assert backend.complete.call_count == 0  # neither model pass ran
    assert not list(tmp_path.glob("*.json"))  # nothing was cached


def test_a_match_start_stops_a_queued_build_for_the_same_deck(monkeypatch, tmp_path):
    """Q3 rework 2 (verifier rejection of 4089f58): begin_deck_analysis for the
    SAME deck stops a queued idle build before its lane grant ('a match needs
    this deck now') instead of letting both model passes run once granted."""
    import time

    from tests.deck_fakes import deck_case

    from arenamcp.coach import CoachEngine
    from arenamcp.deck_strategy import deck_identity
    from arenamcp.game_plan import STRATEGIC_LANE

    state, catalog, response = deck_case()
    monkeypatch.setattr("arenamcp.match_context._local_card", lambda gid, epoch: catalog[gid])
    coach = CoachEngine(backend=Mock())
    monkeypatch.setattr(
        coach, "_playbook_cache_path", staticmethod(lambda identity: tmp_path / f"{identity}.json")
    )
    identity = deck_identity(state)

    held = STRATEGIC_LANE.try_acquire("held")
    assert held is not None
    try:
        backend = Mock()
        result: dict = {}

        def build_idle():
            try:
                coach.prepare_playbook(state, backend=backend)
                result["ok"] = True
            except Exception as error:  # noqa: BLE001 - mirrors the production abort path
                result["error"] = error

        worker = threading.Thread(target=build_idle)
        worker.start()
        deadline = time.time() + 5
        while identity not in coach._playbook_cancels and time.time() < deadline:
            time.sleep(0.01)
        assert identity in coach._playbook_cancels

        # A match starts with this same deck while the build is still queued.
        coach.begin_deck_analysis(identity)
        assert coach._playbook_cancels[identity].is_set()
    finally:
        STRATEGIC_LANE.release(held)

    worker.join(5)
    assert isinstance(result.get("error"), ValueError)
    assert backend.complete.call_count == 0
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.parametrize("already_playing", [True, False])
def test_manual_build_yields_before_server_if_match_begins_during_input(
    monkeypatch, tmp_path, already_playing
):
    from tests.deck_fakes import deck_case

    from arenamcp.coach import CoachEngine

    state, catalog, _ = deck_case()
    playing = already_playing
    backend = Mock()
    coach = CoachEngine(backend=backend)
    record = Mock(complete=True, deck_id="manual", name="Manual")
    record.game_state.return_value = state
    owner = Library(coach)
    monkeypatch.setattr(owner, "_deck_library_backend", lambda: backend)
    monkeypatch.setattr(owner, "_deck_library_in_match", lambda: playing)
    monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", tmp_path)

    def prepare(state):
        nonlocal playing
        playing = True
        return {"deck_catalog": catalog}

    monkeypatch.setattr("arenamcp.match_context.prepare_match_context", prepare)
    result = owner.analyze_deck_record(record, refresh=True)
    assert result["status"] == "error"
    assert "active play" in result["error"] or "changed during analysis" in result["error"]
    backend.complete.assert_not_called()
    assert not coach._playbook_cache
