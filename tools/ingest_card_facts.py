#!/usr/bin/env python3
"""Ingest compiled card-facts records into the shared server store (Fable Nov S2).

The F2 compiler produces one JSON record per printed card under a local
directory; S1 gave every customer a lookup endpoint backed by one shared
server store plus a ``card_facts_wanted`` demand counter.  This job closes
the loop between them:

1. Pick a source of work:
     --wanted      hashes customers asked for and the store cannot serve
                   (ranked by demand via GET /v1/card-facts/admin/wanted)
     --set CODE    every card in a named Arena set from the Scryfall cache
     --all         every card in the Scryfall cache
2. Skip what is already done: hashes whose store status is ``ok`` are never
   recompiled; hashes with a usable local record are uploaded without a
   model call. A cached record is only trusted when its provenance checks
   pass (stamped hash and printed facts match the requested card);
   anything else is quarantined under ``<out>/quarantine/`` and recompiled.
3. Compile the rest two cards at a time through the F2 compiler
   (``compile_one``), writing each record under ``--out`` as it lands.
4. Upload finished records in batches to POST /v1/card-facts/admin/upsert.
   Records that failed validation are uploaded with status ``review`` so a
   human can look at them instead of silently disappearing. A run only
   reports success once every staged batch has been uploaded (or has
   deliberately stayed local after a failure).
5. Stop cleanly on SIGINT/SIGTERM: no new compiles start, in-flight cards
   finish or are abandoned within a bounded drain (compiles run on daemon
   threads, so a wedged model call cannot hold the process hostage),
   completed work is uploaded, and a rerun resumes where this one left off
   (server ``ok`` statuses plus local records).

Everything is driven by plain HTTP against the admin endpoints; nothing is
deployed or restarted here.  Run it with PYTHONPATH=src:

    PYTHONPATH=src python tools/ingest_card_facts.py --set NEO --dry-run

Configuration comes from flags or the environment:

    ARENAMCP_CARD_FACTS_URL   base URL of the coach API (no default)
    ARENAMCP_ADMIN_KEY        admin credential for the card-facts endpoints

Secrets are read from the environment or flags and are never logged.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arenamcp.card_facts.compiler import CardResolver, compile_one  # noqa: E402
from arenamcp.card_facts.hashing import rules_hash  # noqa: E402
from arenamcp.card_facts.schema import (  # noqa: E402
    CardFacts,
    card_facts_to_dict,
    from_json,
    to_json,
)

# At most two compilations run at a time so the job shares the inference
# engine politely instead of monopolising it.
MAX_WORKERS = 2

# Server-side caps mirrored here so a batch never has to bounce.
SERVER_MAX_BATCH_RECORDS = 200
SERVER_MAX_HASHES_PER_REQUEST = 300

DEFAULT_OUT_DIR = Path("~/.arenamcp/card_facts")
DEFAULT_BATCH_SIZE = 100
DEFAULT_WANTED_LIMIT = 500
BULK_LOAD_TIMEOUT_SECONDS = 300

# Seconds to let an in-flight compilation finish after an interrupt before
# it is abandoned (model calls can legitimately take a minute or two).
DRAIN_TIMEOUT_S = 180.0


class IngestError(RuntimeError):
    """A condition the operator must fix before the job can run."""


class ServerError(RuntimeError):
    """An admin endpoint answered with an error status."""

    def __init__(self, status: int, url: str, snippet: str):
        super().__init__(f"server returned {status} for {url}: {snippet}")
        self.status = status


class CardFactsServer:
    """Minimal admin client for the card-facts endpoints."""

    def __init__(self, base_url: str, admin_key: str, timeout_s: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.admin_key = admin_key
        self.timeout_s = timeout_s

    def _request(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("X-Admin-Key", self.admin_key)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            snippet = error.read().decode("utf-8", errors="replace")[:200]
            raise ServerError(error.code, path, snippet) from error
        except urllib.error.URLError as error:
            raise ServerError(0, path, str(error.reason)) from error
        if not isinstance(payload, dict):
            raise ServerError(0, path, "response was not a JSON object")
        return payload

    def wanted(self, limit: int) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"limit": limit})
        payload = self._request("GET", f"/v1/card-facts/admin/wanted?{query}", None)
        entries = payload.get("wanted")
        if not isinstance(entries, list):
            raise ServerError(0, "/v1/card-facts/admin/wanted", "missing 'wanted' list")
        return entries

    def statuses(self, rules_hashes: Iterable[str]) -> dict[str, str]:
        """Store status per hash; hashes with no row are absent."""
        hashes = list(rules_hashes)
        statuses: dict[str, str] = {}
        for start in range(0, len(hashes), SERVER_MAX_HASHES_PER_REQUEST):
            chunk = hashes[start : start + SERVER_MAX_HASHES_PER_REQUEST]
            query = urllib.parse.urlencode({"hashes": ",".join(chunk)})
            payload = self._request("GET", f"/v1/card-facts/admin/records?{query}", None)
            records = payload.get("records")
            if isinstance(records, dict):
                statuses.update({str(k): str(v) for k, v in records.items()})
        return statuses

    def upsert(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request("POST", "/v1/card-facts/admin/upsert", {"records": records})


# ---------------------------------------------------------------------------
# Work-list construction
# ---------------------------------------------------------------------------


def _raw_universe(scryfall: Any) -> Iterable[dict[str, Any]]:
    """Every distinct printed card in the Scryfall cache as raw dicts."""
    index = getattr(scryfall, "_name_index", None)
    if not index:
        raise IngestError(
            "Scryfall cache has no bulk index; warm it once by running any "
            "card-facts compile or client fetch first"
        )
    return index.values()


def _printed_facts(scryfall: Any, raw: dict[str, Any]) -> dict[str, str]:
    """Printed facts and hash exactly as the compiler will compute them."""
    card = scryfall._card_dict_to_scryfall_card(raw)
    return {
        "name": card.name,
        "mana_cost": card.mana_cost,
        "type_line": card.type_line,
        "rules_hash": rules_hash(card.name, card.mana_cost, card.type_line, card.oracle_text),
    }


def build_work_list(
    mode: str,
    set_code: str | None,
    scryfall: Any,
    server: CardFactsServer | None,
    wanted_limit: int,
) -> tuple[list[dict[str, Any]], int]:
    """Return ``(work_items, unresolved_hashes)`` for the chosen source.

    Each work item carries the printed facts (``name``, ``mana_cost``,
    ``type_line``), the ``rules_hash`` and, for wanted mode, the
    ``wanted_count``. Items are unique by rules_hash and keep their source
    order (demand rank for --wanted).
    """
    items_by_hash: dict[str, dict[str, Any]] = {}
    unresolved = 0

    if mode == "wanted":
        if server is None:
            raise IngestError("--wanted needs --server or ARENAMCP_CARD_FACTS_URL")
        entries = server.wanted(wanted_limit)
        hash_to_facts: dict[str, dict[str, str]] = {}
        for raw in _raw_universe(scryfall):
            facts = _printed_facts(scryfall, raw)
            hash_to_facts.setdefault(facts["rules_hash"], facts)
        for entry in entries:
            digest = str(entry.get("rules_hash", ""))
            facts = hash_to_facts.get(digest)
            if facts is None:
                unresolved += 1
                continue
            items_by_hash[digest] = {**facts, "wanted_count": entry.get("wanted_count")}
    else:
        wanted_set = set_code.strip().lower() if set_code else None
        for raw in _raw_universe(scryfall):
            if wanted_set is not None and str(raw.get("set", "")).lower() != wanted_set:
                continue
            facts = _printed_facts(scryfall, raw)
            items_by_hash[facts["rules_hash"]] = facts

    return list(items_by_hash.values()), unresolved


# ---------------------------------------------------------------------------
# Local record cache (--out directory)
# ---------------------------------------------------------------------------


def load_local_record(out_dir: Path, rules_hash_: str) -> CardFacts | None:
    """The locally cached record for a hash when it is usable for upload."""
    path = out_dir / f"{rules_hash_}.json"
    if not path.exists():
        return None
    try:
        record = from_json(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if record.confidence == "failed":
        # A failed record must be recompiled rather than re-uploaded.
        return None
    return record


def write_local_record(out_dir: Path, rules_hash_: str, record: CardFacts) -> None:
    """Write one record atomically so an interrupt cannot truncate it."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{rules_hash_}.json"
    tmp = path.with_name(f"{rules_hash_}.{os.getpid()}.tmp")
    tmp.write_text(to_json(record), encoding="utf-8")
    os.replace(tmp, path)


def cache_problems(record: CardFacts, item: dict[str, Any]) -> list[str]:
    """Ways a locally cached record fails to be about the requested card.

    A cache file named ``<hash>.json`` is only trusted for upload when it
    really is that card's record: its stamped ``rules_hash`` must equal the
    requested hash and its printed facts (name, mana cost, type line) must
    match the Scryfall card the hash was computed from. Anything else — a
    record for another card under this filename, edited printed fields — is
    quarantined and recompiled rather than uploaded.
    """
    problems: list[str] = []
    if record.rules_hash != item["rules_hash"]:
        problems.append(
            f"record rules_hash {record.rules_hash!r} does not match requested {item['rules_hash']!r}"
        )
    for field_name in ("name", "mana_cost", "type_line"):
        expected = item.get(field_name)
        actual = getattr(record, field_name)
        if expected is not None and actual != expected:
            problems.append(f"record {field_name} {actual!r} does not match printed {expected!r}")
    return problems


def quarantine_record(out_dir: Path, rules_hash_: str) -> Path | None:
    """Move an untrusted cache file aside under ``<out>/quarantine/``."""
    source = out_dir / f"{rules_hash_}.json"
    target_dir = out_dir / "quarantine"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / source.name
        os.replace(source, target)
        return target
    except OSError:
        return None


def record_payload(record: CardFacts) -> dict[str, Any]:
    """Wire format for one record; failed validations go to review."""
    payload = card_facts_to_dict(record)
    payload["status"] = "review" if record.confidence == "failed" else "ok"
    return payload


# ---------------------------------------------------------------------------
# The job itself
# ---------------------------------------------------------------------------

_stop = threading.Event()


def request_stop(_signum: Any = None, _frame: Any = None) -> None:
    _stop.set()


def classify_work(
    work_items: list[dict[str, Any]],
    out_dir: Path,
    server: CardFactsServer | None,
    log: Any,
) -> tuple[dict[str, str], dict[str, CardFacts]]:
    """Split the work list into what can be skipped and what must be compiled.

    Returns ``(server_statuses, cached_records)``:

    * ``server_statuses`` maps hash -> stored status for every hash the store
      knows about (only ``ok`` ones are skipped; ``review``/``failed`` rows do
      not serve customers and are worth refreshing).
    * ``cached_records`` maps hash -> locally cached usable record for hashes
      that are neither ok on the server nor missing locally; these are
      uploaded without a model call. A cached file is only trusted after its
      provenance checks pass (stamped hash and printed facts match the
      requested card); anything else is quarantined and recompiled.

    Everything else in the work list needs compiling.
    """
    hashes = [item["rules_hash"] for item in work_items]
    statuses: dict[str, str] = {}
    if server is not None and hashes:
        statuses = server.statuses(hashes)

    cached: dict[str, CardFacts] = {}
    for item in work_items:
        digest = item["rules_hash"]
        if statuses.get(digest) == "ok":
            continue
        record = load_local_record(out_dir, digest)
        if record is None:
            continue
        problems = cache_problems(record, item)
        if problems:
            quarantine_record(out_dir, digest)
            log(
                f"warning: cached record for {item['name']} failed provenance checks "
                f"({' ; '.join(problems)[:200]}); quarantined and requeued for compile"
            )
            continue
        cached[digest] = record
    return statuses, cached


class Uploader:
    """Batches finished records and uploads them to the admin upsert route."""

    def __init__(
        self,
        server: CardFactsServer | None,
        batch_size: int,
        summary: dict[str, Any],
        log: Any,
    ):
        self.server = server
        self.batch_size = min(batch_size, SERVER_MAX_BATCH_RECORDS)
        self.summary = summary
        self.log = log
        self.buffer: list[dict[str, Any]] = []

    def stage(self, record: CardFacts) -> None:
        self.buffer.append(record_payload(record))

    def flush(self) -> bool:
        """Upload the staged buffer; True on success (or nothing to do)."""
        if not self.buffer:
            return True
        batch = self.buffer[: self.batch_size]
        if self.server is None:
            # Dry run or offline inspection: nothing leaves this machine.
            self.buffer.clear()
            return True
        try:
            result = self.server.upsert(batch)
            stored = int(result.get("stored", 0))
            rejected = result.get("rejected", [])
            self.summary["upload_batches"] += 1
            self.summary["uploaded"] += stored
            if rejected:
                self.summary["upload_rejected"] += len(rejected)
                self.log(f"warning: {len(rejected)} record(s) rejected by upsert")
            del self.buffer[: len(batch)]
            return True
        except ServerError as error:
            self.summary["upload_failures"] += 1
            self.log(f"warning: upsert failed ({error}); keeping records local")
            return False

    def flush_all(self) -> bool:
        ok = True
        while self.buffer:
            if not self.flush():
                ok = False
                break
        return ok


def run_job(
    *,
    mode: str,
    set_code: str | None,
    out_dir: Path,
    server: CardFactsServer | None,
    scryfall: Any,
    mtgadb: Any,
    backend_factory: Any,
    batch_size: int,
    wanted_limit: int,
    dry_run: bool,
    log: Any,
) -> dict[str, Any]:
    """Execute one ingest run and return its summary."""
    started = time.monotonic()
    summary: dict[str, Any] = {
        "mode": mode,
        "set": set_code,
        "dry_run": dry_run,
        "work_list": 0,
        "already_ok_on_server": 0,
        "uploaded_from_cache": 0,
        "to_compile": 0,
        "compiled_ok": 0,
        "compiled_review": 0,
        "unresolved_hashes": 0,
        "upload_batches": 0,
        "uploaded": 0,
        "upload_rejected": 0,
        "upload_failures": 0,
        "abandoned_compiles": 0,
        "interrupted": False,
        "seconds": 0.0,
    }

    work_items, unresolved_hashes = build_work_list(
        mode=mode,
        set_code=set_code,
        scryfall=scryfall,
        server=server if mode == "wanted" else None,
        wanted_limit=wanted_limit,
    )
    summary["unresolved_hashes"] += unresolved_hashes

    statuses, cached = classify_work(work_items, out_dir, server, log)
    summary["work_list"] = len(work_items)
    if server is None:
        log("no server configured; records will only be written locally")
    summary["already_ok_on_server"] = sum(
        1 for item in work_items if statuses.get(item["rules_hash"]) == "ok"
    )

    uploader = Uploader(server, batch_size, summary, log)

    # Cached records upload without a model call, in dry runs they are only
    # counted. Every cached batch is flushed before the run can succeed: a
    # cache-only run must not return while records remain buffered.
    staged_cached = len(cached)
    for record in cached.values():
        uploader.stage(record)
    if dry_run:
        summary["uploaded_from_cache"] = staged_cached
        summary["to_compile"] = len(work_items) - summary["already_ok_on_server"] - len(cached)
        summary["seconds"] = round(time.monotonic() - started, 2)
        return summary
    if staged_cached and not uploader.flush_all():
        # Batches that succeeded are gone from the buffer; the rest stay
        # local and a rerun picks them up. Report what actually uploaded.
        summary["uploaded_from_cache"] = staged_cached - len(uploader.buffer)
        summary["seconds"] = round(time.monotonic() - started, 2)
        return summary
    summary["uploaded_from_cache"] = staged_cached

    to_compile = [
        item
        for item in work_items
        if statuses.get(item["rules_hash"]) != "ok" and item["rules_hash"] not in cached
    ]
    summary["to_compile"] = len(to_compile)
    if not to_compile:
        summary["seconds"] = round(time.monotonic() - started, 2)
        return summary

    resolver = CardResolver(scryfall=scryfall, mtgadb=mtgadb)
    backend = backend_factory()

    def compile_item(item: dict[str, Any]):
        return compile_one(item["name"], resolver, backend)

    def handle_result(item: dict[str, Any], result: Any) -> None:
        if result is None:
            return
        digest = result.rules_hash or item["rules_hash"]
        if result.record is not None:
            write_local_record(out_dir, digest, result.record)
            uploader.stage(result.record)
        if result.status == "compiled":
            summary["compiled_ok"] += 1
        else:
            summary["compiled_review"] += 1
            log(f"warning: {result.name or item['name']} needs review: {'; '.join(result.problems)[:200]}")

    # Compiles run on daemon worker threads so a wedged model call can never
    # hold the process hostage: once a drain deadline expires the run walks
    # away from it and lets the daemon die with the process.
    work_queue: queue.SimpleQueue = queue.SimpleQueue()
    for index, item in enumerate(to_compile):
        work_queue.put((index, item))
    results: queue.SimpleQueue = queue.SimpleQueue()

    def worker() -> None:
        # A stop (or an upload failure, which ends the run early) stops new
        # compiles from starting; the item a worker already holds finishes
        # naturally or is abandoned at the bounded drain deadline below,
        # where its daemon thread dies with the process.
        while not _stop.is_set() and not summary["upload_failures"]:
            try:
                index, item = work_queue.get_nowait()
            except queue.Empty:
                return
            try:
                results.put((index, compile_item(item)))
            except Exception as error:
                results.put((index, error))

    threads = [
        threading.Thread(target=worker, name=f"ingest-compile-{number}", daemon=True)
        for number in range(min(MAX_WORKERS, len(to_compile)))
    ]
    for thread in threads:
        thread.start()

    handled: set[int] = set()
    drain_deadline: float | None = None

    def harvest() -> None:
        while True:
            try:
                index, payload = results.get_nowait()
            except queue.Empty:
                return
            handled.add(index)
            item = to_compile[index]
            if isinstance(payload, BaseException):
                log(f"warning: compiling {item['name']} raised {payload!r}")
                continue
            try:
                handle_result(item, payload)
            except Exception as error:  # keep the run alive; record locally
                log(f"warning: handling result for {item['name']} raised {error!r}")

    while True:
        harvest()
        draining = _stop.is_set() or bool(summary["upload_failures"])
        if draining and drain_deadline is None:
            drain_deadline = time.monotonic() + DRAIN_TIMEOUT_S
        # When draining, queued-but-unstarted items are abandoned by design;
        # only in-flight results and live workers need accounting.
        all_accounted = results.empty() and not any(thread.is_alive() for thread in threads)
        if all_accounted:
            break
        if drain_deadline is not None and time.monotonic() >= drain_deadline:
            break  # bounded drain expired; abandon whatever is still running
        time.sleep(0.02)

    harvest()  # catch results that landed between the last harvest and here

    abandoned = len(to_compile) - len(handled)
    if abandoned:
        log(f"warning: abandoned {abandoned} compilation(s) at the drain deadline")
    summary["abandoned_compiles"] = abandoned

    uploader.flush_all()
    summary["interrupted"] = _stop.is_set()
    summary["seconds"] = round(time.monotonic() - started, 2)
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def default_backend_factory():
    from arenamcp.coach import create_backend

    return create_backend("online")


def default_scryfall():
    from arenamcp.scryfall import ScryfallCache

    scryfall = ScryfallCache()
    # The bulk index loads on a daemon thread; the work-source enumeration
    # reads it directly, so wait for the load (or its failure) to finish.
    loader = getattr(scryfall, "_thread", None)
    if loader is not None:
        loader.join(timeout=BULK_LOAD_TIMEOUT_SECONDS)
    return scryfall


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--wanted",
        action="store_true",
        help="work the card_facts_wanted backlog (most-demanded hashes first)",
    )
    source.add_argument("--set", dest="set_code", metavar="CODE", help="every card in one Arena set")
    source.add_argument("--all", action="store_true", help="every card in the Scryfall cache")
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help="local record cache directory (default %(default)s)",
    )
    parser.add_argument(
        "--server", default=os.environ.get("ARENAMCP_CARD_FACTS_URL"), help="coach API base URL"
    )
    parser.add_argument(
        "--admin-key",
        default=os.environ.get("ARENAMCP_ADMIN_KEY"),
        help="admin credential (default $ARENAMCP_ADMIN_KEY)",
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="records per upsert batch")
    parser.add_argument("--limit", type=int, default=DEFAULT_WANTED_LIMIT, help="max hashes for --wanted")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="plan the run (server reads allowed) but compile and upload nothing",
    )
    args = parser.parse_args(argv)
    args.mode = "wanted" if args.wanted else ("set" if args.set_code else "all")
    return args


def main(
    argv: list[str] | None = None,
    *,
    scryfall: Any | None = None,
    mtgadb: Any | None = None,
    backend_factory: Any | None = None,
) -> int:
    args = parse_args(argv)
    out_dir = args.out.expanduser()

    server = None
    if args.server:
        if not args.admin_key:
            print("error: --server needs --admin-key or $ARENAMCP_ADMIN_KEY", file=sys.stderr)
            return 2
        server = CardFactsServer(args.server, args.admin_key)

    try:
        scryfall_obj = scryfall if scryfall is not None else default_scryfall()
        summary = run_job(
            mode=args.mode,
            set_code=args.set_code,
            out_dir=out_dir,
            server=server,
            scryfall=scryfall_obj,
            mtgadb=mtgadb,
            backend_factory=backend_factory or default_backend_factory,
            batch_size=max(1, args.batch_size),
            wanted_limit=max(1, args.limit),
            dry_run=args.dry_run,
            log=lambda message: print(message, file=sys.stderr),
        )
    except (IngestError, ServerError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(json.dumps(summary, indent=2))
    if summary.get("interrupted"):
        return 130
    # Buffered records that never made it to the server are a failed run,
    # not a successful one (they stay local for a rerun to pick up).
    return 1 if summary.get("upload_failures") else 0


if __name__ == "__main__":
    for _sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(_sig, request_stop)
        except ValueError:  # not the main thread (tests)
            pass
    sys.exit(main())
