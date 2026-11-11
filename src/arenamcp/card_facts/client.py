"""Client-side card-facts cache and background fetcher (Fable Nov S3).

The coach learns card lists in three places: the deck analysis path
(``standalone_deck._maybe_analyze_deck``), the deck library before a match
(``standalone_decks.collect_decks``), and opponent cards as they are revealed
(``gamestate_annotations``). Each of those hands grp ids to
:meth:`CardFactsClient.prefetch_grp_ids`, which resolves them against the local
card database and queues a rules-hash lookup against the server's
``/v1/card-facts/lookup`` endpoint (built by Fable Nov S1).

Contracts this module must keep:

* Never blocks a decision. ``facts_for`` answers from memory or the disk cache
  and only *queues* anything it does not have; the network work happens on one
  background daemon thread. A card without a record simply has none.
* The coach plays exactly as today when the server is unreachable: failures are
  logged once at INFO (then at DEBUG while the outage continues) and retried
  later. Nothing raises out of the background thread.
* Cached records older than ``REFRESH_AFTER_S`` (30 days) are still served, but
  refreshed in the background.
* Hashes are computed with :func:`arenamcp.card_facts.hashing.rules_hash`
  (Fable Nov F1) over the card fields exactly as supplied, so client and server
  compiler agree as long as both read the same card text.

Only the standard library is used.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from arenamcp.card_facts.hashing import rules_hash

logger = logging.getLogger(__name__)

#: Where fetched records live on disk, one JSON envelope per rules hash.
CACHE_DIR = Path.home() / ".arenamcp" / "cache" / "card_facts"

LOOKUP_PATH = "/v1/card-facts/lookup"

#: Mirrors website/card_facts.py MAX_HASHES_PER_LOOKUP (Fable Nov S1).
MAX_HASHES_PER_LOOKUP = 300

#: Cached records older than this are served but refreshed in the background.
REFRESH_AFTER_S = 30 * 86400.0

#: The server answered "missing": ask again after this long.
MISSING_RETRY_S = 10 * 60.0

#: Network or server failure: retry after this long.
FAILURE_RETRY_S = 5 * 60.0

#: Minimum spacing between lookup requests; the server rate-limits lookups to
#: one per second per owner (website/card_facts.py MIN_LOOKUP_INTERVAL_S).
MIN_SEND_INTERVAL_S = 1.1

REQUEST_TIMEOUT_S = 10.0

Fetcher = Callable[[str, bytes, dict[str, str]], tuple[int, str]]
Resolver = Callable[[int], dict[str, Any] | None]


def _field(card: Any, key: str) -> str:
    """Read *key* off a card dict or CardInfo, tolerating either shape."""
    if isinstance(card, dict):
        value = card.get(key, "")
    else:
        value = getattr(card, key, "")
    return value if isinstance(value, str) else ""


def hash_for_card(card: Any) -> str | None:
    """Return the F1 rules_hash for *card*, or None when it has no usable name.

    Fields are passed through exactly as supplied: the hash is only stable if
    the client and the server compiler read the same card text.
    """
    name = _field(card, "name")
    if not name or name.startswith("Unknown("):
        return None
    return rules_hash(
        name,
        _field(card, "mana_cost"),
        _field(card, "type_line"),
        _field(card, "oracle_text"),
    )


def _default_resolver(grp_id: int) -> dict[str, Any] | None:
    """Resolve a grp id through the coach's local card database."""
    try:
        from arenamcp.match_context import _local_card

        # Rolling epoch, same convention as match_context's own callers: a
        # fixed epoch would latch an Unknown result forever when the local
        # card database finishes loading after the first resolution.
        return _local_card(grp_id, int(time.monotonic() // 30))
    except Exception as error:  # pragma: no cover - defensive: never raise here
        logger.debug("card facts resolver failed for grp %s: %s", grp_id, error)
        return None


def _default_opener(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, str]:
    """POST *body* to *url* and return ``(status, text)``; never raises."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            return int(response.status), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        try:
            text = error.read().decode("utf-8", "replace")
        except Exception:
            text = ""
        return int(error.code), text
    except Exception as error:
        logger.debug("card facts lookup transport error: %s", error)
        return 0, f"{type(error).__name__}: {error}"


def _license_key() -> str:
    try:
        from arenamcp.settings import get_settings

        value = get_settings().get("license_key", "")
        return value if isinstance(value, str) else ""
    except Exception:
        return ""


def _website_base() -> str:
    try:
        from arenamcp.subscription import WEBSITE_BASE

        return str(WEBSITE_BASE).rstrip("/")
    except Exception:
        return "https://mtgacoach.com"


class CardFactsClient:
    """Memory + disk cache in front of the server's card-facts lookup."""

    def __init__(
        self,
        *,
        license_key: str = "",
        base_url: str | None = None,
        opener: Fetcher | None = None,
        resolver: Resolver | None = None,
        cache_dir: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._license_key = license_key
        self._base_url = base_url
        self._opener = opener or _default_opener
        self._resolver = resolver or _default_resolver
        self._cache_dir = Path(cache_dir) if cache_dir is not None else CACHE_DIR
        self._clock = clock
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        # hash -> (record, fetched_at)
        self._memory: dict[str, tuple[dict[str, Any], float]] = {}
        # hash -> earliest time the next lookup attempt may run
        self._want_fetch: dict[str, float] = {}
        # grp ids awaiting resolution; dict used as an ordered set
        self._want_resolve: dict[int, None] = {}
        # hashes sent to the server in the batch currently in flight
        self._in_flight: list[str] = []
        self._failing = False
        self._worker: threading.Thread | None = None
        self._next_send_monotonic = 0.0

    # ------------------------------------------------------------------ API

    def facts_for(self, cards: Any) -> dict[str, dict[str, Any]]:
        """Records for *cards* from memory or disk; the rest queue for one fetch.

        Returns ``{rules_hash: record}`` for every card that already has a
        record. Cards without one are queued for a single background batch
        lookup and simply have no record yet — this call never waits on the
        network.
        """
        now = self._clock()
        results: dict[str, dict[str, Any]] = {}
        wanted: list[str] = []
        seen: set[str] = set()
        for card in cards or []:
            digest = hash_for_card(card)
            if not digest or digest in seen:
                continue
            seen.add(digest)
            record = self._cached(digest, now)
            if record is not None:
                results[digest] = record
            else:
                wanted.append(digest)
        if wanted:
            self.queue_hashes(wanted)
        return results

    def prefetch_grp_ids(self, grp_ids: Any) -> None:
        """Queue grp ids for background resolution and lookup. Never blocks."""
        ids = [gid for gid in (grp_ids or []) if isinstance(gid, int)]
        if not ids:
            return
        with self._wake:
            for gid in ids:
                self._want_resolve.setdefault(gid, None)
            self._ensure_worker_locked()
            self._wake.notify()

    def queue_hashes(self, hashes: Any) -> None:
        """Queue rules hashes for one background batch lookup. Never blocks."""
        wanted = [digest for digest in (hashes or []) if isinstance(digest, str)]
        if not wanted:
            return
        now = self._clock()
        with self._wake:
            for digest in wanted:
                if digest in self._in_flight:
                    # A lookup covering this hash is already on the wire;
                    # queueing it again at ``now`` would cancel the failure/
                    # missing backoff when that request settles.
                    continue
                scheduled = self._want_fetch.get(digest)
                # Respect a pending retry schedule (failure/missing backoff);
                # only pull work forward when nothing is scheduled or the
                # schedule is already overdue.
                if scheduled is None or scheduled <= now:
                    self._want_fetch[digest] = now
            self._ensure_worker_locked()
            self._wake.notify()

    # -------------------------------------------------------------- caching

    def _cached(self, digest: str, now: float) -> dict[str, Any] | None:
        """Return the record for *digest*, queueing a refresh when stale."""
        with self._lock:
            entry = self._memory.get(digest)
            record = entry[0] if entry is not None else None
            stale = entry is not None and now - entry[1] > REFRESH_AFTER_S
            if record is not None and not stale:
                return record
            disk_entry = self._read_disk(digest)
            if disk_entry is not None:
                record, fetched_at = disk_entry
                stale = now - fetched_at > REFRESH_AFTER_S
                self._memory[digest] = (record, fetched_at)
            elif record is None:
                return None
            if stale and digest not in self._want_fetch and digest not in self._in_flight:
                self._want_fetch[digest] = now
                self._ensure_worker_locked()
                self._wake.notify()
            return record

    def _envelope_path(self, digest: str) -> Path:
        return self._cache_dir / f"{digest}.json"

    def _read_disk(self, digest: str) -> tuple[dict[str, Any], float] | None:
        path = self._envelope_path(digest)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            record = payload["record"]
            fetched_at = float(payload["fetched_at"])
            if isinstance(record, dict):
                return record, fetched_at
        except FileNotFoundError:
            pass
        except Exception as error:
            logger.debug("card facts cache read failed for %s: %s", digest[:12], error)
        return None

    def _store(self, digest: str, record: dict[str, Any]) -> None:
        now = self._clock()
        envelope = json.dumps({"fetched_at": now, "record": record})
        path = self._envelope_path(digest)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(envelope, encoding="utf-8")
            tmp.replace(path)
        except OSError as error:
            logger.debug("card facts cache write failed for %s: %s", digest[:12], error)
        with self._lock:
            self._memory[digest] = (record, now)

    # --------------------------------------------------------------- worker

    def _ensure_worker_locked(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(
            target=self._run_worker,
            name="card-facts-fetch",
            daemon=True,
        )
        self._worker.start()

    def _run_worker(self) -> None:
        while True:
            batch: list[str] = []
            grp_ids: list[int] = []
            with self._wake:
                now = self._clock()
                due = sorted(digest for digest, at in self._want_fetch.items() if at <= now)
                if due:
                    delay = self._next_send_monotonic - time.monotonic()
                    if delay > 0:
                        # Pace requests so we stay under the server's one-per-
                        # second quota; wait() releases the lock while sleeping.
                        self._wake.wait(min(delay + 0.01, 5.0))
                        continue
                    batch = due[:MAX_HASHES_PER_LOOKUP]
                    for digest in batch:
                        del self._want_fetch[digest]
                    self._next_send_monotonic = time.monotonic() + MIN_SEND_INTERVAL_S
                else:
                    grp_ids = list(self._want_resolve)
                    self._want_resolve.clear()
                    if not grp_ids:
                        # Idle wait; new work arrives via notify().
                        self._wake.wait(30.0)
                        continue
            if grp_ids:
                self._resolve_and_queue(grp_ids)
            if batch:
                self._fetch(batch)

    def _resolve_and_queue(self, grp_ids: list[int]) -> None:
        """Resolve grp ids to cards outside the lock, then queue their hashes."""
        wanted: list[str] = []
        for gid in grp_ids:
            try:
                card = self._resolver(gid)
            except Exception as error:  # defensive: resolver is pluggable
                logger.debug("card facts resolver raised for grp %s: %s", gid, error)
                card = None
            digest = hash_for_card(card) if card else None
            if not digest:
                continue
            if self._cached(digest, self._clock()) is not None:
                # Already cached fresh (a stale copy gets its refresh queued
                # by _cached): warm prefetch must not refetch it.
                continue
            wanted.append(digest)
        if wanted:
            self.queue_hashes(wanted)

    def _fetch(self, hashes: list[str]) -> None:
        """One lookup request for *hashes*; requeues on failure or missing."""
        headers = {"Content-Type": "application/json"}
        key = self._license_key or _license_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        url = (self._base_url or _website_base()).rstrip("/") + LOOKUP_PATH
        body = json.dumps({"hashes": hashes}).encode("utf-8")
        with self._wake:
            self._in_flight = list(hashes)
        try:
            status, text = self._opener(url, body, headers)
        except Exception as error:  # defensive: openers are pluggable
            status, text = 0, f"{type(error).__name__}: {error}"
        found: dict[str, Any] | None = None
        if status == 200:
            try:
                payload = json.loads(text)
                candidate = payload.get("found")
                if isinstance(candidate, dict):
                    found = candidate
            except Exception as error:
                logger.debug("card facts lookup response unreadable: %s", error)
                found = None
        if found is None:
            self._on_failure(status, text)
            return
        missing_retry_at = self._clock() + MISSING_RETRY_S
        stored: list[str] = []
        for digest in hashes:
            record = found.get(digest)
            if isinstance(record, dict):
                self._store(digest, record)
                stored.append(digest)
            else:
                # Missing (or an unexpected shape): ask again later. The
                # attempt just happened, so its deadline replaces any older
                # schedule instead of merging with it.
                with self._wake:
                    self._want_fetch[digest] = missing_retry_at
                    self._wake.notify()
        if stored:
            with self._wake:
                if self._failing:
                    self._failing = False
                    logger.info(
                        "card facts lookup recovered (%d record(s) cached)",
                        len(stored),
                    )

    def _on_failure(self, status: int, text: str) -> None:
        """Log the first failure at INFO, repeats at DEBUG; retry later."""
        retry_at = self._clock() + FAILURE_RETRY_S
        with self._wake:
            first = not self._failing
            self._failing = True
            self._wake.notify()
        detail = (text or "").strip().replace("\n", " ")[:200]
        if first:
            logger.info(
                "card facts lookup unavailable (status %s); coach continues "
                "without card facts and will retry: %s",
                status or "network-error",
                detail,
            )
        else:
            logger.debug("card facts lookup still failing (status %s): %s", status, detail)
        # Requeue everything that was in flight for this batch. An attempt
        # just failed, so its backoff deadline replaces any schedule queued
        # while the request was airborne rather than merging with it.
        with self._wake:
            for digest in self._in_flight:
                self._want_fetch[digest] = retry_at
            self._in_flight.clear()
            self._wake.notify()


# ------------------------------------------------------------ shared client

_shared: CardFactsClient | None = None
_shared_lock = threading.Lock()


def shared_client() -> CardFactsClient:
    """Process-wide client used by the coach hooks."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = CardFactsClient()
        return _shared


def facts_for(cards: Any) -> dict[str, dict[str, Any]]:
    """See :meth:`CardFactsClient.facts_for`."""
    return shared_client().facts_for(cards)


def prefetch_grp_ids(grp_ids: Any) -> None:
    """See :meth:`CardFactsClient.prefetch_grp_ids`."""
    shared_client().prefetch_grp_ids(grp_ids)
