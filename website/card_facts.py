"""Shared card-facts store for every customer (Fable Nov S1).

Compiled card-facts records (see the F1 record schema) are stored once on
the server; any customer looks up the records they need by rules_hash, so
nobody compiles the same card locally.

Endpoints
---------
POST /v1/card-facts/lookup         customer lookup (license key auth):
                                   up to MAX_HASHES_PER_LOOKUP hashes in,
                                   the ``ok`` records plus the missing
                                   hashes out.  Missing hashes are counted
                                   in ``card_facts_wanted`` so the ingest
                                   job (S2) knows what customers need.
POST /v1/card-facts/admin/upsert   admin batch upsert (the same admin
                                   credential the admin router checks).
                                   Rejects records over MAX_RECORD_BYTES
                                   and anything that is not a JSON object
                                   with a rules_hash.

Customer auth reuses match_bundles.authorize_upload verbatim (subscribers
table, trials table, then LiteLLM /key/info) so there is exactly one
definition of "valid customer key".  Lookups are rate limited per key the
way the match-bundle upload is: a minimum interval plus a rolling-24 h
quota, both answered 429 + Retry-After.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from match_bundles import authorize_upload, owner_of
from state import _get_db, _require_admin

logger = logging.getLogger("website.card_facts")

router = APIRouter()

STATUSES = ("ok", "failed", "review")
DEFAULT_STATUS = "ok"

# sha256 hexdigest of the card text as supplied (F1 hashing recipe).
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

# Strict-JSON bounds.  A stored float must be finite (Python's serializer
# would otherwise emit bare NaN/Infinity literals, which RFC 8259 clients
# and strict parsers reject) and every integer must fit SQLite's signed
# 64-bit columns, or binding it raises and takes the batch with it.
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1

# Nesting bound for every record we store or serve.  Card-facts records are
# flat (the F1 schema nests a handful of levels); anything deeper is abuse
# or corruption, and validating it recursively would trade a clean
# per-record rejection for a RecursionError that takes the whole request
# down (Dotty round-3 verification: a 1000-deep array in one record aborted
# the admin batch with a 500).  _check_strict_json enforces this bound
# itself, so validation never depends on the ambient stack depth.
MAX_JSON_DEPTH = 64


def _check_strict_json(value: Any) -> None:
    """Raise ValueError unless value is strict finite JSON.

    Applied to every record at intake and again to every payload at
    serialization, so a non-finite number, an out-of-range integer or a
    string that cannot be encoded as UTF-8 (e.g. a lone surrogate escape
    like ``\\ud800``) can never reach a stored row or a customer response.
    Structures nested deeper than MAX_JSON_DEPTH are rejected too, and the
    walk is iterative so validation itself can never raise RecursionError
    on deeply nested input (a recursive walk turned a rejectable record
    into a 500 that aborted the whole batch).
    """
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, float):
            if not math.isfinite(node):
                raise ValueError("non-finite number (NaN/Infinity)")
        elif isinstance(node, int) and not _INT64_MIN <= node <= _INT64_MAX:
            raise ValueError("integer outside the signed 64-bit range")
        elif isinstance(node, str):
            _check_encodable_utf8(node)
        elif isinstance(node, dict):
            if depth >= MAX_JSON_DEPTH:
                raise ValueError(f"JSON nests deeper than {MAX_JSON_DEPTH} levels")
            for key, item in node.items():
                if not isinstance(key, str):
                    raise ValueError("object key is not a string")
                _check_encodable_utf8(key)
                stack.append((item, depth + 1))
        elif isinstance(node, list):
            if depth >= MAX_JSON_DEPTH:
                raise ValueError(f"JSON nests deeper than {MAX_JSON_DEPTH} levels")
            stack.extend((item, depth + 1) for item in node)


def _check_encodable_utf8(text: str) -> None:
    """Reject strings SQLite and the JSON response layer cannot encode.

    ``json.loads`` happily turns an escaped lone surrogate (``"\\ud800"``)
    into a Python str, but binding it to a TEXT column or emitting it in an
    ``ensure_ascii=False`` response raises UnicodeEncodeError.  Only
    well-formed Unicode may be stored or served.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"string is not valid Unicode ({exc})") from exc


MAX_HASHES_PER_LOOKUP = 300
MAX_RECORD_BYTES = 64 * 1024  # per-record cap for admin upserts
MAX_BATCH_RECORDS = 200
MAX_LOOKUP_BODY_BYTES = 256 * 1024
# Worst-case batch: every record at the per-record cap plus JSON overhead.
MAX_UPSERT_BODY_BYTES = MAX_BATCH_RECORDS * (MAX_RECORD_BYTES + 1024) + 64 * 1024

# Lookup abuse limits, mirroring match_bundles' upload limits: per key
# (owner digest), rolling 24 h count plus a minimum interval between
# lookups, both 429 + Retry-After.
QUOTA_WINDOW_S = 86400.0
MAX_LOOKUPS_PER_DAY = 1000
MIN_LOOKUP_INTERVAL_S = 1.0


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def init_db() -> None:
    """Create the store tables.  Idempotent; app.py calls it on every load."""
    with _get_db().get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS card_facts (
                rules_hash TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                schema_version INTEGER,
                payload TEXT NOT NULL,
                updated_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'ok'
                    CHECK (status IN ('ok', 'failed', 'review'))
            );
            CREATE INDEX IF NOT EXISTS idx_card_facts_name ON card_facts(name);

            CREATE TABLE IF NOT EXISTS card_facts_wanted (
                rules_hash TEXT PRIMARY KEY,
                wanted_count INTEGER NOT NULL DEFAULT 0,
                last_requested_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS card_facts_lookups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT NOT NULL,
                looked_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_card_facts_lookups_owner
                ON card_facts_lookups(owner, looked_at);
            """
        )


def upsert_record(
    *,
    rules_hash: str,
    name: str,
    schema_version: Optional[int],
    payload: str,
    status: str,
) -> None:
    """Insert or replace one record; updated_at is always server time."""
    with _get_db().get_db() as conn:
        conn.execute(
            """
            INSERT INTO card_facts (rules_hash, name, schema_version, payload, updated_at, status)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(rules_hash) DO UPDATE SET
                name = excluded.name,
                schema_version = excluded.schema_version,
                payload = excluded.payload,
                updated_at = excluded.updated_at,
                status = excluded.status
            """,
            (rules_hash, name, schema_version, payload, time.time(), status),
        )


def get_ok_records(rules_hashes: list[str]) -> dict[str, Any]:
    """Parse and return the ``ok`` payloads for the given hashes."""
    found: dict[str, Any] = {}
    with _get_db().get_db() as conn:
        for chunk in (rules_hashes[i : i + 100] for i in range(0, len(rules_hashes), 100)):
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT rules_hash, payload FROM card_facts "
                f"WHERE status = 'ok' AND rules_hash IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                try:
                    parsed = json.loads(row["payload"])
                    _check_strict_json(parsed)
                except (ValueError, RecursionError):
                    # A stored payload should always parse into strict finite
                    # JSON of bounded depth; if one somehow does not (e.g. a
                    # row written before intake enforced this, or one nested
                    # so deeply that json.loads itself exhausts the stack),
                    # serve it as missing rather than poisoning the response.
                    logger.warning("card_facts payload for %s is not strict finite JSON", row["rules_hash"])
                else:
                    found[row["rules_hash"]] = parsed
    return found


def count_wanted(rules_hashes: list[str], now: Optional[float] = None) -> None:
    """Record customer demand for missing hashes (counter per hash)."""
    if not rules_hashes:
        return
    now = time.time() if now is None else now
    with _get_db().get_db() as conn:
        conn.executemany(
            """
            INSERT INTO card_facts_wanted (rules_hash, wanted_count, last_requested_at)
            VALUES (?, 1, ?)
            ON CONFLICT(rules_hash) DO UPDATE SET
                wanted_count = wanted_count + 1,
                last_requested_at = excluded.last_requested_at
            """,
            [(h, now) for h in rules_hashes],
        )


def wanted_counts() -> dict[str, int]:
    """Digest of demand for the ingest job's dashboard/log line."""
    with _get_db().get_db() as conn:
        rows = conn.execute(
            "SELECT rules_hash, wanted_count FROM card_facts_wanted ORDER BY wanted_count DESC"
        ).fetchall()
    return {row["rules_hash"]: row["wanted_count"] for row in rows}


# ---------------------------------------------------------------------------
# Lookup rate limiting (mirrors match_bundles' upload quotas)
# ---------------------------------------------------------------------------


def _enforce_lookup_quota(owner: str) -> None:
    now = time.time()
    with _get_db().get_db() as conn:
        # Prune this owner's spent window as we go so the log stays bounded.
        conn.execute(
            "DELETE FROM card_facts_lookups WHERE owner = ? AND looked_at <= ?",
            (owner, now - QUOTA_WINDOW_S),
        )
        count, last = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(looked_at), 0) FROM card_facts_lookups WHERE owner = ?",
            (owner,),
        ).fetchone()
    wait = MIN_LOOKUP_INTERVAL_S - (now - last)
    if last and wait > 0:
        raise HTTPException(
            429,
            f"Too many lookups from this key; retry in {math.ceil(wait)}s",
            headers={"Retry-After": str(math.ceil(wait))},
        )
    if count >= MAX_LOOKUPS_PER_DAY:
        raise HTTPException(
            429,
            "Daily card-facts lookup quota reached for this key",
            headers={"Retry-After": str(int(QUOTA_WINDOW_S // 24))},
        )


def _record_lookup(owner: str) -> None:
    with _get_db().get_db() as conn:
        conn.execute(
            "INSERT INTO card_facts_lookups (owner, looked_at) VALUES (?, ?)",
            (owner, time.time()),
        )


# ---------------------------------------------------------------------------
# Body handling
# ---------------------------------------------------------------------------


async def _read_body_capped(request: Request, cap: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > cap:
                raise HTTPException(413, f"Body exceeds {cap} bytes")
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            raise HTTPException(413, f"Body exceeds {cap} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_json_object(request: Request, cap: int) -> dict[str, Any]:
    raw = await _read_body_capped(request, cap)
    if not raw:
        raise HTTPException(400, "Empty body")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(400, f"Body is not valid JSON: {type(exc).__name__}")
    except RecursionError:
        # Valid JSON can still nest deeper than the interpreter can parse
        # (Dotty round-3 verification: a ~20 kB lookup body with a 10000-deep
        # array crashed parsing with a 500).  Answer 400 instead.
        raise HTTPException(400, "Body nests too deeply to parse")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Body must be a JSON object")
    return payload


def _validated_hash(value: Any) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise HTTPException(422, "rules_hash must be a 64-char lowercase hex sha256 digest")
    return value


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/v1/card-facts/lookup")
async def lookup_card_facts(request: Request, auth: dict = Depends(authorize_upload)):
    body = await _read_json_object(request, MAX_LOOKUP_BODY_BYTES)
    hashes = body.get("hashes")
    if not isinstance(hashes, list):
        raise HTTPException(422, 'Body must be {"hashes": [...]}')
    if len(hashes) > MAX_HASHES_PER_LOOKUP:
        raise HTTPException(422, f"Lookup is capped at {MAX_HASHES_PER_LOOKUP} hashes")

    seen: set[str] = set()
    wanted_list: list[str] = []
    for value in hashes:
        rules_hash = _validated_hash(value)
        if rules_hash not in seen:
            seen.add(rules_hash)
            wanted_list.append(rules_hash)

    owner = owner_of(auth["license_key"])
    _enforce_lookup_quota(owner)
    _record_lookup(owner)

    found = get_ok_records(wanted_list)
    missing = [h for h in wanted_list if h not in found]
    count_wanted(missing)
    return {"found": found, "missing": missing}


def _record_error(index: int, reason: str) -> dict[str, str]:
    return {"index": index, "reason": reason}


def _validate_admin_record(
    index: int, record: Any
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, str]]]:
    """Return (prepared row, rejection).  Exactly one side is non-None."""
    if not isinstance(record, dict):
        return None, _record_error(index, "record must be a JSON object")
    rules_hash = record.get("rules_hash")
    if not isinstance(rules_hash, str) or not _HASH_RE.fullmatch(rules_hash):
        return None, _record_error(index, "missing or malformed rules_hash")
    try:
        # Validate BEFORE serializing: the bounded walk rejects NaN/Infinity,
        # out-of-range integers, non-UTF-8-encodable strings and structures
        # nested deeper than MAX_JSON_DEPTH, so json.dumps below only ever
        # sees a shallow, strictly finite structure (serializing first let a
        # deeply nested record raise RecursionError and abort the batch).
        _check_strict_json(record)
        payload = json.dumps(record, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        return None, _record_error(index, f"record is not strict finite JSON: {exc}")
    if len(payload.encode("utf-8")) > MAX_RECORD_BYTES:
        return None, _record_error(index, f"record exceeds {MAX_RECORD_BYTES} bytes")
    status = record.get("status", DEFAULT_STATUS)
    if status not in STATUSES:
        return None, _record_error(index, "status must be one of ok|failed|review")
    name = str(record.get("name") or "")[:200]
    schema_version = record.get("schema_version")
    if not isinstance(schema_version, int):
        schema_version = None
    row: dict[str, Any] = {
        "rules_hash": rules_hash,
        "name": name,
        "schema_version": schema_version,
        "payload": payload,
        "status": status,
    }
    return row, None


@router.post("/v1/card-facts/admin/upsert")
async def admin_upsert_card_facts(request: Request, _=Depends(_require_admin)):
    body = await _read_json_object(request, MAX_UPSERT_BODY_BYTES)
    records = body.get("records")
    if not isinstance(records, list):
        raise HTTPException(422, 'Body must be {"records": [...]}')
    if len(records) > MAX_BATCH_RECORDS:
        raise HTTPException(422, f"Batch is capped at {MAX_BATCH_RECORDS} records")

    stored = 0
    rejected: list[dict[str, str]] = []
    for index, record in enumerate(records):
        row, error = _validate_admin_record(index, record)
        if error is not None:
            rejected.append(error)
            continue
        assert row is not None
        upsert_record(**row)
        stored += 1

    logger.info("Card-facts upsert stored=%d rejected=%d", stored, len(rejected))
    return {"stored": stored, "rejected": rejected}


# ---------------------------------------------------------------------------
# Admin read routes (S2: the ingest job must see what the store already has)
# ---------------------------------------------------------------------------

# Upper bound on the wanted-list page size.  The ingest job asks for a few
# hundred at a time; anything past this is a mistake, not a work list.
MAX_WANTED_LIMIT = 5000


def wanted_entries(limit: int = 500) -> list[dict[str, Any]]:
    """Missing hashes ranked by demand: most-requested first.

    Rows come straight from ``card_facts_wanted`` (fed by customer lookups);
    ties break on recency so a fresh burst of interest outranks an old one.
    """
    with _get_db().get_db() as conn:
        rows = conn.execute(
            "SELECT rules_hash, wanted_count, last_requested_at FROM card_facts_wanted"
            " ORDER BY wanted_count DESC, last_requested_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(row) for row in rows]


def record_statuses(rules_hashes: list[str]) -> dict[str, str]:
    """Stored status for each hash that exists in the store.

    Hashes with no row are simply absent from the result — absence is the
    "customers asked for this and nobody compiled it yet" signal the ingest
    job works from.
    """
    statuses: dict[str, str] = {}
    with _get_db().get_db() as conn:
        for start in range(0, len(rules_hashes), 100):
            chunk = rules_hashes[start : start + 100]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT rules_hash, status FROM card_facts WHERE rules_hash IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                statuses[row["rules_hash"]] = row["status"]
    return statuses


@router.get("/v1/card-facts/admin/wanted")
def admin_wanted_card_facts(
    limit: int = Query(default=500, ge=1, le=MAX_WANTED_LIMIT),
    _=Depends(_require_admin),
) -> dict[str, Any]:
    """The ingest job's default work list: demanded-but-missing hashes."""
    return {"wanted": wanted_entries(limit)}


@router.get("/v1/card-facts/admin/records")
def admin_card_facts_statuses(
    hashes: str = Query(...),
    _=Depends(_require_admin),
) -> dict[str, Any]:
    """Store status per comma-separated hash (used to skip already-ok cards)."""
    requested: list[str] = []
    for piece in hashes.split(","):
        piece = piece.strip()
        if piece and piece not in requested:
            requested.append(piece)
    if not requested:
        raise HTTPException(422, "hashes query parameter required")
    if len(requested) > MAX_HASHES_PER_LOOKUP:
        raise HTTPException(422, f"Capped at {MAX_HASHES_PER_LOOKUP} hashes per request")
    for value in requested:
        _validated_hash(value)
    return {"records": record_statuses(requested)}


# Create the tables when the module is imported alongside the app;
# app.py calls init_db() again after state reload so test harnesses that
# swap the database module get them too.
try:
    init_db()
except Exception as _exc:  # pragma: no cover - surfaced at app start instead
    logger.warning("card_facts.init_db at import failed: %s", _exc)
