"""The corpus builders must block on the card DB and reject degraded records.

Two guards, tested here:

1. ``arenamcp.card_db.require_card_database`` — corpus builders wait for the
   card database instead of racing ``get_mtgjson()``'s background daemon
   thread, and raise rather than return a half-loaded one.
2. ``tools.training.corpus_selfcheck`` — a built corpus is scanned for the
   fingerprints the race left behind (numeric type codes, permanents that did
   not resolve, battlefield lands yielding no mana) and rejected if it carries
   any.

Together these are what stop a repeat of the incident where 20.4% of the gate
test prompts were silently wrong and nothing anywhere reported a problem.
"""

import json
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.training import corpus_selfcheck as sc

from arenamcp.card_db import CardDatabaseUnavailable, require_card_database
from arenamcp.mtgjson import MTGJSONDatabase

# ---------------------------------------------------------------------------
# require_card_database: blocking + fail-closed
# ---------------------------------------------------------------------------


class _SlowLoadingMTGJSON:
    """Stands in for MTGJSON: not ready until ``load()`` has been awaited."""

    def __init__(self):
        self._ready = threading.Event()
        self.load_calls = 0

    @property
    def available(self):
        return self._ready.is_set()

    def load(self, force_download=False):
        self.load_calls += 1
        self._ready.set()
        return True


class _StubMTGA:
    def __init__(self, available=True):
        self.available = available


def test_require_card_database_blocks_until_loaded(monkeypatch):
    """It must not hand back a database that is still loading."""
    slow = _SlowLoadingMTGJSON()
    seen = {}

    def fake_get_mtgjson(*, block=False):
        # Mirrors the real signature: only block=True waits for the loader.
        seen["block"] = block
        if block:
            slow.load()
        return slow

    monkeypatch.setattr("arenamcp.mtgjson.get_mtgjson", fake_get_mtgjson)
    monkeypatch.setattr("arenamcp.mtgadb.MTGADatabase", lambda *a, **k: _StubMTGA())

    assert not slow.available, "precondition: unloaded"
    db = require_card_database()

    assert seen["block"] is True, "must request the blocking load, not the async one"
    assert slow.available, "require_card_database must wait for the load"
    assert db is not None


def test_require_card_database_rejects_a_database_that_never_loads(monkeypatch):
    """Blocking is not enough — the result must actually be available."""
    never = _SlowLoadingMTGJSON()
    never.load = lambda force_download=False: False  # returns, but never ready

    monkeypatch.setattr("arenamcp.mtgjson.get_mtgjson", lambda *, block=False: never)
    monkeypatch.setattr("arenamcp.mtgadb.MTGADatabase", lambda *a, **k: _StubMTGA())

    with pytest.raises(CardDatabaseUnavailable):
        require_card_database()


def test_require_card_database_raises_when_mtgjson_unavailable(monkeypatch):
    class _Dead:
        available = False

        def load(self, force_download=False):
            return False

    monkeypatch.setattr("arenamcp.mtgjson.get_mtgjson", lambda *, block=False: _Dead())
    monkeypatch.setattr("arenamcp.mtgadb.MTGADatabase", lambda *a, **k: _StubMTGA())

    with pytest.raises(CardDatabaseUnavailable) as exc:
        require_card_database()
    assert "MTGJSON" in str(exc.value)


def test_require_card_database_raises_when_mtga_db_unavailable(monkeypatch):
    ready = _SlowLoadingMTGJSON()
    ready.load()
    monkeypatch.setattr("arenamcp.mtgjson.get_mtgjson", lambda *, block=False: ready)
    monkeypatch.setattr("arenamcp.mtgadb.MTGADatabase", lambda *a, **k: _StubMTGA(available=False))

    with pytest.raises(CardDatabaseUnavailable) as exc:
        require_card_database()
    assert "MTGA local CardDatabase" in str(exc.value)


def test_require_card_database_error_explains_the_consequence(monkeypatch):
    """The message has to say WHY, or the next person will just retry."""

    class _Dead:
        available = False

        def load(self, force_download=False):
            return False

    monkeypatch.setattr("arenamcp.mtgjson.get_mtgjson", lambda *, block=False: _Dead())
    monkeypatch.setattr("arenamcp.mtgadb.MTGADatabase", lambda *a, **k: _StubMTGA(available=False))

    with pytest.raises(CardDatabaseUnavailable) as exc:
        require_card_database()
    assert "no mana" in str(exc.value)


def test_mtgjson_load_is_serialized(tmp_path):
    """A second ``load()`` waits for the in-flight one instead of racing it.

    This is what makes ``load()`` usable as "wait until ready" by the builders.
    """
    db = MTGJSONDatabase(cache_dir=tmp_path)
    order = []
    started = threading.Event()
    release = threading.Event()

    def slow_load_indexes():
        order.append("enter")
        started.set()
        release.wait(5)
        db._arena_index = {}
        db._name_index = {"plains": object()}
        order.append("exit")
        return True

    db._load_indexes = slow_load_indexes

    background = threading.Thread(target=db.load, daemon=True)
    background.start()
    assert started.wait(5), "background load should have started"

    results = []
    waiter = threading.Thread(target=lambda: results.append(db.load()), daemon=True)
    waiter.start()
    release.set()
    background.join(5)
    waiter.join(5)

    assert order == ["enter", "exit"], "loads must not interleave"
    assert results == [True]


# ---------------------------------------------------------------------------
# corpus_selfcheck
# ---------------------------------------------------------------------------


def _battlefield(type_line, *, name="Plains", grp_id=75556, tapped=False, seat=1):
    return {
        "grp_id": grp_id,
        "name": name,
        "type_line": type_line,
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "is_tapped": tapped,
    }


def _state(*cards, hand=()):
    return {"battlefield": list(cards), "hand": list(hand)}


def test_clean_state_produces_no_findings():
    audit, findings = sc.check_decision(
        "ok", _state(_battlefield("Basic Land — Plains")), local_seat=1, mana_total=1
    )
    assert findings == []
    assert audit["untapped_local_lands"] == 1


def test_numeric_type_code_is_rejected():
    _audit, findings = sc.check_decision("bad", _state(_battlefield("5")), local_seat=1, mana_total=0)
    kinds = {f.kind for f in findings}
    assert "numeric_type_code" in kinds


def test_untapped_land_with_zero_mana_is_rejected():
    """The exact signature of the incident: lands on board, 'Mana: 0'."""
    _audit, findings = sc.check_decision(
        "bad", _state(_battlefield("Basic Land — Plains")), local_seat=1, mana_total=0
    )
    assert [f.kind for f in findings] == ["land_yields_no_mana"]


def test_unresolved_permanent_is_rejected():
    state = _state(_battlefield("", name="#99999", grp_id=99999))
    _audit, findings = sc.check_decision("bad", state, local_seat=1, mana_total=0)
    kinds = {f.kind for f in findings}
    assert "unresolved_permanent" in kinds
    assert "missing_type_line" in kinds


def test_face_down_card_is_not_a_finding():
    """Hidden by the rules is not the same as a failed lookup."""
    state = _state(_battlefield("", name="#3", grp_id=3, seat=2))
    audit, findings = sc.check_decision("hidden", state, local_seat=1, mana_total=0)
    assert findings == []
    assert audit["hidden_card_grp_ids"] == [3]


def test_tapped_land_with_zero_mana_is_fine():
    """A tapped land legitimately produces nothing — no false positive."""
    _audit, findings = sc.check_decision(
        "ok", _state(_battlefield("Basic Land — Plains", tapped=True)), local_seat=1, mana_total=0
    )
    assert findings == []


def test_opponent_land_does_not_count_toward_your_pool():
    _audit, findings = sc.check_decision(
        "ok", _state(_battlefield("Basic Land — Plains", seat=2)), local_seat=1, mana_total=0
    )
    assert findings == []


def test_rendered_mana_line_is_not_compared_against_the_pool():
    """The two mana accountings legitimately differ — no false positives.

    ``RulesEngine._get_mana_pool`` counts a colourless land in ``total`` but in
    no colour bucket, and misses mana creatures entirely (its regex wants
    ``{T}`` while MTGA writes ``{oT}``); the coach formatter's ``Mana:`` line
    handles the ``{o…}`` style. Asserting equality flagged 34/206 healthy
    records, so the check was removed rather than left to cry wolf.
    """
    record = {
        "id": "x",
        "user": "Legal: (pick by number)\nMana: 0\nLand: AVAILABLE",
        "meta": {sc.AUDIT_KEY: {"untapped_local_lands": 0, "mana_total": 2}},
    }
    assert sc.check_corpus_record(record) == []


def test_corpus_without_provenance_is_not_silently_accepted():
    """ "Cannot verify" must never be reported as "verified clean"."""
    findings = sc.check_corpus_record({"id": "old", "user": "Mana: 0", "meta": {}})
    assert [f.kind for f in findings] == ["no_provenance"]


def test_scan_corpus_file_and_cli(tmp_path, capsys):
    good = {
        "id": "good",
        "user": "Mana: 1",
        "meta": {sc.AUDIT_KEY: {"untapped_local_lands": 1, "mana_total": 1}},
    }
    bad = {
        "id": "bad",
        "user": "Mana: 0",
        "meta": {sc.AUDIT_KEY: {"untapped_local_lands": 2, "mana_total": 0}},
    }
    ok_path = tmp_path / "ok.jsonl"
    ok_path.write_text(json.dumps(good) + "\n", encoding="utf-8")
    bad_path = tmp_path / "bad.jsonl"
    bad_path.write_text(json.dumps(good) + "\n" + json.dumps(bad) + "\n", encoding="utf-8")

    assert sc.scan_corpus_file(ok_path) == []
    assert sc.main([str(ok_path)]) == 0
    assert sc.main([str(bad_path)]) == 2, "a corpus with findings must fail the CLI"


def test_summarise_counts_by_kind():
    findings = [
        sc.Finding("a", "numeric_type_code", "x"),
        sc.Finding("a", "land_yields_no_mana", "y"),
        sc.Finding("b", "numeric_type_code", "z"),
    ]
    summary = sc.summarise(findings)
    assert summary["findings"] == 3
    assert summary["records_affected"] == 2
    assert summary["by_kind"]["numeric_type_code"] == 2
