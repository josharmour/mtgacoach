"""Tests for the S2 set-ingest job (tools/ingest_card_facts.py).

The card universe is the same real printed text the compiler tests use
(test_card_facts_schema.PRINTED_CARDS — Scryfall oracle text fetched
2026-11-10), so every rules_hash below is computed from real printed facts,
and cached records are that suite's hand-built records, each proven there
to validate clean against its printed card (Fury's hash verified in F1,
16de70d3…).  The server is a real localhost HTTP server
speaking the S1 admin routes; the compiler is faked at the compile_one
boundary, so no model and no network beyond 127.0.0.1 is involved.
"""

from __future__ import annotations

import importlib.util
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from arenamcp.card_facts import rules_hash
from arenamcp.card_facts.compiler import CompileResult
from arenamcp.card_facts.schema import CardFacts, to_json
from test_card_facts_schema import (
    PRINTED_CARDS,
    chrome_mox_record,
    fury_record,
    gemstone_caverns_record,
    minsc_boo_record,
)

TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "ingest_card_facts.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("ingest_card_facts", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def ingest(monkeypatch):
    module = _load_tool()
    # Fresh stop flag per test so an interrupt in one test cannot leak.
    monkeypatch.setattr(module, "_stop", threading.Event())
    return module


# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeScryfall:
    """The bulk-index surface the ingest job reads, over real printed cards."""

    def __init__(self, sets: dict[str, str] | None = None):
        # name -> set code; defaults put every fixture card in "neo".
        self._sets = sets or {name: "neo" for name in PRINTED_CARDS}
        self._name_index = {name: {"name": name, "set": code} for name, code in self._sets.items()}

    def _card_dict_to_scryfall_card(self, raw):
        return PRINTED_CARDS[raw["name"]]

    def get_card_by_name(self, name: str):
        return PRINTED_CARDS.get(name.strip().casefold()) or PRINTED_CARDS.get(name)


class FakeServer:
    """Speaks the three S1/S2 admin routes over real HTTP on 127.0.0.1."""

    ADMIN_KEY = "test-admin"

    def __init__(self):
        self.lock = threading.Lock()
        self.store: dict[str, dict] = {}  # rules_hash -> {"record":..., "status":...}
        self.wanted_rows: list[dict] = []
        self.upsert_calls = 0
        self.wanted_calls = 0
        self.records_calls = 0
        # Test hook: fail the upsert requests whose 1-based call number is in
        # this set (lets a test fail exactly the second call, say).
        self.fail_upsert_calls: set[int] = set()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def _make_handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _check_auth(self):
                return self.headers.get("X-Admin-Key") == outer.ADMIN_KEY

            def _send(self, payload, status=200):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if not self._check_auth():
                    self._send({"error": "forbidden"}, 403)
                    return
                if self.path.startswith("/v1/card-facts/admin/wanted"):
                    with outer.lock:
                        outer.wanted_calls += 1
                        self._send({"wanted": list(outer.wanted_rows)})
                    return
                if self.path.startswith("/v1/card-facts/admin/records"):
                    from urllib.parse import parse_qs, urlparse

                    params = parse_qs(urlparse(self.path).query)
                    hashes = (params.get("hashes") or [""])[0]
                    requested = [h for h in (p.strip() for p in hashes.split(",")) if h]
                    with outer.lock:
                        outer.records_calls += 1
                        records = {h: outer.store[h]["status"] for h in requested if h in outer.store}
                    self._send({"records": records})
                    return
                self._send({"error": "not found"}, 404)

            def do_POST(self):
                if not self._check_auth():
                    self._send({"error": "forbidden"}, 403)
                    return
                if not self.path.startswith("/v1/card-facts/admin/upsert"):
                    self._send({"error": "not found"}, 404)
                    return
                with outer.lock:
                    outer.upsert_calls += 1
                    if outer.upsert_calls in outer.fail_upsert_calls:
                        self._send({"error": "upstream unavailable"}, 500)
                        return
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                stored = 0
                with outer.lock:
                    for record in payload.get("records", []):
                        digest = record.get("rules_hash", "")
                        if not digest:
                            continue
                        outer.store[digest] = {
                            "record": record,
                            "status": record.get("status", "ok"),
                        }
                        stored += 1
                self._send({"stored": stored, "rejected": []})

        return Handler

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def statuses(self) -> dict[str, str]:
        with self.lock:
            return {h: row["status"] for h, row in self.store.items()}


@pytest.fixture()
def server():
    fake = FakeServer()
    yield fake
    fake.close()


def _expected_hash(name: str) -> str:
    card = PRINTED_CARDS[name]
    return rules_hash(card.name, card.mana_cost, card.type_line, card.oracle_text)


# The schema suite's hand-built records: each one that suite proves validates
# clean against its printed card, so a cached copy is exactly what a real F2
# compile of that card would have left behind.
_RECORD_BUILDERS = {
    "Fury": fury_record,
    "Minsc & Boo, Timeless Heroes": minsc_boo_record,
    "Chrome Mox": chrome_mox_record,
    "Gemstone Caverns": gemstone_caverns_record,
}


def _compiled_record(name: str, confidence="high") -> CardFacts:
    record = _RECORD_BUILDERS[name]()
    record.rules_hash = _expected_hash(name)
    record.confidence = confidence
    return record


def _all_ok() -> dict[str, str]:
    return {_expected_hash(name): "ok" for name in PRINTED_CARDS}


def _run(ingest, server_or_none, argv_extra, monkeypatch, fake=None, **kw):
    if fake is None:

        def forbidden(*a, **k):
            raise AssertionError("compiler must not be called")

        fake = forbidden
    monkeypatch.setattr(ingest, "compile_one", fake)
    argv = list(argv_extra)
    if server_or_none is not None:
        argv += ["--server", server_or_none.url(), "--admin-key", FakeServer.ADMIN_KEY]
    scryfall = kw.pop("scryfall", None) or FakeScryfall()
    kwargs = {"backend_factory": lambda: object(), "scryfall": scryfall, **kw}
    return ingest.main(argv, **kwargs)


# ── Set mode end to end ──────────────────────────────────────────────────────


def test_set_run_compiles_uploads_and_marks_store_ok(ingest, server, monkeypatch, tmp_path):
    """A --set run compiles every card in the set and lands them as ok."""
    compiled: list[str] = []

    def fake(identifier, resolver, backend):
        compiled.append(identifier)
        return CompileResult(
            identifier=str(identifier),
            name=identifier,
            rules_hash=_expected_hash(identifier),
            status="compiled",
            record=_compiled_record(identifier),
        )

    out = tmp_path / "records"
    code = _run(ingest, server, ["--set", "neo", "--out", str(out)], monkeypatch, fake=fake)
    assert code == 0
    assert sorted(compiled) == sorted(PRINTED_CARDS)
    assert server.statuses() == _all_ok()
    for name in PRINTED_CARDS:
        assert (out / f"{_expected_hash(name)}.json").exists()


def test_rerun_skips_cards_the_store_already_serves(ingest, server, monkeypatch, tmp_path):
    """Second run recompiles nothing: ok rows on the server are skipped."""
    out = tmp_path / "r"

    def first_fake(identifier, resolver, backend):
        return CompileResult(
            identifier=str(identifier),
            name=identifier,
            rules_hash=_expected_hash(identifier),
            status="compiled",
            record=_compiled_record(identifier),
        )

    _run(ingest, server, ["--set", "neo", "--out", str(out)], monkeypatch, fake=first_fake)

    def forbidden(*a, **k):
        raise AssertionError("compiler must not run on a fully-served set")

    code = _run(ingest, server, ["--set", "neo", "--out", str(out)], monkeypatch, fake=forbidden)
    assert code == 0


def test_interrupt_keeps_finished_work_and_rerun_completes_backlog(ingest, server, monkeypatch, tmp_path):
    """Ctrl-C mid-run uploads finished cards; a rerun finishes the rest."""
    out = tmp_path / "r"
    first_pass: list[str] = []

    def stopping_fake(identifier, resolver, backend):
        ingest.request_stop()  # simulate SIGINT arriving during the run
        first_pass.append(identifier)
        return CompileResult(
            identifier=str(identifier),
            name=identifier,
            rules_hash=_expected_hash(identifier),
            status="compiled",
            record=_compiled_record(identifier),
        )

    code = _run(ingest, server, ["--set", "neo", "--out", str(out)], monkeypatch, fake=stopping_fake)
    assert code == 130
    assert len(server.statuses()) >= 1

    second_pass: list[str] = []
    ingest._stop.clear()  # a rerun is a fresh process; the flag does not carry over

    def finishing_fake(identifier, resolver, backend):
        second_pass.append(identifier)
        return CompileResult(
            identifier=str(identifier),
            name=identifier,
            rules_hash=_expected_hash(identifier),
            status="compiled",
            record=_compiled_record(identifier),
        )

    code = _run(ingest, server, ["--set", "neo", "--out", str(out)], monkeypatch, fake=finishing_fake)
    assert code == 0
    assert server.statuses() == _all_ok()
    # Nothing compiled twice across the two passes.
    assert len(first_pass) + len(second_pass) == len(PRINTED_CARDS)


def test_failed_validation_upload_lands_as_review_not_ok(ingest, server, monkeypatch, tmp_path):
    """A record that fails validation is uploaded as review for a human."""
    compiled: list[str] = []

    def failing_fake(identifier, resolver, backend):
        compiled.append(identifier)
        return CompileResult(
            identifier=str(identifier),
            name=identifier,
            rules_hash=_expected_hash(identifier),
            status="needs_review",
            problems=["oracle mismatch"],
            record=_compiled_record(identifier, confidence="failed"),
        )

    scryfall = FakeScryfall({"Fury": "neo"})
    code = _run(
        ingest,
        server,
        ["--set", "neo", "--out", str(tmp_path / "r")],
        monkeypatch,
        fake=failing_fake,
        scryfall=scryfall,
    )
    assert code == 0
    assert compiled == ["Fury"]
    assert server.statuses() == {_expected_hash("Fury"): "review"}


def test_dry_run_compiles_and_uploads_nothing(ingest, server, monkeypatch, tmp_path):
    """--dry-run plans the work but writes no files and calls no upsert."""
    out = tmp_path / "r"

    def forbidden(*a, **k):
        raise AssertionError("dry run must not compile")

    code = _run(ingest, server, ["--set", "neo", "--out", str(out), "--dry-run"], monkeypatch, fake=forbidden)
    assert code == 0
    assert server.upsert_calls == 0
    assert not out.exists() or not list(out.iterdir())


def test_wanted_mode_resolves_demanded_hashes_and_counts_unresolved(ingest, server, monkeypatch, tmp_path):
    """--wanted maps demanded hashes back to cards; unknown hashes count."""
    fury_hash = _expected_hash("Fury")
    mox_hash = _expected_hash("Chrome Mox")
    server.wanted_rows = [
        {"rules_hash": fury_hash, "wanted_count": 7},
        {"rules_hash": mox_hash, "wanted_count": 3},
        {"rules_hash": "f" * 64, "wanted_count": 99},  # demanded but not in this cache
    ]

    compiled: list[str] = []

    def fake(identifier, resolver, backend):
        compiled.append(identifier)
        return CompileResult(
            identifier=str(identifier),
            name=identifier,
            rules_hash=_expected_hash(identifier),
            status="compiled",
            record=_compiled_record(identifier),
        )

    code = _run(
        ingest, server, ["--wanted", "--limit", "10", "--out", str(tmp_path / "r")], monkeypatch, fake=fake
    )
    assert code == 0
    assert sorted(compiled) == ["Chrome Mox", "Fury"]
    assert server.statuses() == {fury_hash: "ok", mox_hash: "ok"}


def test_cached_local_record_uploads_without_model_call(ingest, server, monkeypatch, tmp_path):
    """A usable local record is uploaded as-is; the compiler never sees it."""
    out = tmp_path / "r"
    out.mkdir()
    # Fury's real compiled record from the schema suite, cached locally by an
    # earlier F2 compile run.
    from test_card_facts_schema import fury_record

    record = fury_record()
    record.rules_hash = _expected_hash("Fury")
    (out / f"{_expected_hash('Fury')}.json").write_text(to_json(record))

    def forbidden(*a, **k):
        raise AssertionError("cached record must not be recompiled")

    code = _run(ingest, server, ["--set", "neo", "--out", str(out)], monkeypatch, fake=forbidden)
    assert code == 0
    assert server.statuses() == {_expected_hash("Fury"): "ok"}


# ── Review findings (2026-11-11 review of clean38f2afd) ─────────────────────


def test_cache_only_run_flushes_every_buffered_record_before_success(ingest, server, monkeypatch, tmp_path):
    """Review case: cached-only 4 records, batch 1 uploads 1 then returns
    while 3 remain buffered.

    A cache-only run with --batch-size 1 whose first upsert fails must not
    exit 0: the three records still buffered stay local, the run reports
    failure, and a rerun uploads exactly the remaining three without any
    model call.
    """
    out = tmp_path / "r"
    out.mkdir()
    for name in PRINTED_CARDS:
        record = _compiled_record(name)
        (out / f"{_expected_hash(name)}.json").write_text(to_json(record))

    def forbidden(*a, **k):
        raise AssertionError("cached records must not be recompiled")

    server.fail_upsert_calls = {2}  # first batch lands, the second one fails
    code = _run(
        ingest, server, ["--set", "neo", "--out", str(out), "--batch-size", "1"], monkeypatch, fake=forbidden
    )
    assert code == 1, "a run that leaves buffered records must not report success"
    uploaded_first = set(server.statuses())
    assert len(uploaded_first) == 1

    # Rerun: the one ok row is skipped, the three survivors upload from cache.
    server.fail_upsert_calls = set()
    code = _run(
        ingest, server, ["--set", "neo", "--out", str(out), "--batch-size", "1"], monkeypatch, fake=forbidden
    )
    assert code == 0
    assert server.statuses() == _all_ok()


def test_quarantines_cached_record_whose_filename_hash_holds_another_card(
    ingest, server, monkeypatch, tmp_path
):
    """Review case: Fury hash filename containing Chrome Mox record accepted
    and uploaded ok.

    A cache file named <fury-hash>.json that actually holds Chrome Mox's
    record must not be uploaded as Fury: it is quarantined under
    <out>/quarantine/, Fury is recompiled from the model, and the store ends
    up with the real Fury record under Fury's hash.
    """
    out = tmp_path / "r"
    out.mkdir()
    fury_hash = _expected_hash("Fury")
    wrong_record = _compiled_record("Chrome Mox")
    (out / f"{fury_hash}.json").write_text(to_json(wrong_record))

    compiled: list[str] = []

    def fake(identifier, resolver, backend):
        compiled.append(identifier)
        return CompileResult(
            identifier=str(identifier),
            name=identifier,
            rules_hash=_expected_hash(identifier),
            status="compiled",
            record=_compiled_record(identifier),
        )

    code = _run(ingest, server, ["--set", "neo", "--out", str(out)], monkeypatch, fake=fake)
    assert code == 0
    assert "Fury" in compiled, "the mislabelled cache entry must be recompiled"
    assert server.statuses() == _all_ok()
    # The impostor file was moved aside, not uploaded or deleted.
    quarantined = out / "quarantine" / f"{fury_hash}.json"
    assert quarantined.exists()
    assert json.loads(quarantined.read_text())["name"] == "Chrome Mox"
    # And the real Fury record now lives under Fury's hash.
    fresh = json.loads((out / f"{fury_hash}.json").read_text())
    assert fresh["name"] == "Fury"


def test_quarantines_cached_record_whose_body_disagrees_with_the_printed_card(
    ingest, server, monkeypatch, tmp_path
):
    """Review case: same current hash/name/mana/type Fury with power 99
    fails validate but was uploaded ok.

    A cached record whose body contradicts the printed card must not be
    trusted just because its hash and printed facts still match: the full
    F1 validation against the current printed card catches the tampered
    body, the file is quarantined, Fury is recompiled, and the store only
    ever sees a body that agrees with the printed card.
    """
    out = tmp_path / "r"
    out.mkdir()
    fury_hash = _expected_hash("Fury")
    tampered = _compiled_record("Fury")
    tampered.body.power = "99"
    (out / f"{fury_hash}.json").write_text(to_json(tampered))

    compiled: list[str] = []

    def fake(identifier, resolver, backend):
        compiled.append(identifier)
        return CompileResult(
            identifier=str(identifier),
            name=identifier,
            rules_hash=_expected_hash(identifier),
            status="compiled",
            record=_compiled_record(identifier),
        )

    code = _run(ingest, server, ["--set", "neo", "--out", str(out)], monkeypatch, fake=fake)
    assert code == 0
    assert "Fury" in compiled, "the invalid-body cache entry must be recompiled"
    assert server.statuses() == _all_ok()
    stored = server.store[fury_hash]["record"]
    assert stored["body"]["power"] == "3", "the tampered body must never reach the store"
    quarantined = json.loads((out / "quarantine" / f"{fury_hash}.json").read_text())
    assert quarantined["body"]["power"] == "99"


def test_drain_deadline_abandons_wedged_compile_and_exits_promptly(ingest, server, monkeypatch, tmp_path):
    """Review case: DRAIN_TIMEOUT=.05 event-blocked compiler remains alive
    after .25s.

    The first compile call completes and requests a stop; every later call
    wedges on an event that never fires. The run must upload the one finished
    card, abandon the wedged ones at the drain deadline, and exit promptly
    (daemon worker threads) instead of waiting on them forever. Exactly one
    card can complete no matter how the two worker threads interleave.
    """
    monkeypatch.setattr(ingest, "DRAIN_TIMEOUT_S", 0.05)
    out = tmp_path / "r"
    wedged = threading.Event()  # never set: stands in for a hung model call
    calls = {"n": 0}
    lock = threading.Lock()

    def fake(identifier, resolver, backend):
        with lock:
            calls["n"] += 1
            first = calls["n"] == 1
        if first:
            ingest.request_stop()
            return CompileResult(
                identifier=str(identifier),
                name=identifier,
                rules_hash=_expected_hash(identifier),
                status="compiled",
                record=_compiled_record(identifier),
            )
        wedged.wait()
        raise AssertionError("unreachable: the wedged compile never returns")

    started = time.monotonic()
    code = _run(ingest, server, ["--set", "neo", "--out", str(out)], monkeypatch, fake=fake)
    elapsed = time.monotonic() - started

    assert code == 130
    # Bounded: far below any unbounded wait on the wedged compiles.
    assert elapsed < 5.0
    # Exactly one card made it through; it is one of the two items the
    # workers pick up first (the queue order is fixed).
    statuses = server.statuses()
    assert len(statuses) == 1
    first_two = {_expected_hash("Fury"), _expected_hash("Minsc & Boo, Timeless Heroes")}
    assert set(statuses) <= first_two
    wedged.set()  # release the daemon threads so the test process stays clean
