"""Match-bundle intake for the coach's "share match logs" feature.

The desktop coach uploads one gzip-compressed JSON bundle per finished match
(packet, redacted coach-log and Player.log slices, final state, settings
subset).  This router stores them on the data volume and indexes them in
SQLite so the dev-only reviewer can pull new bundles and the admin dashboard
can count them.

Endpoints
---------
POST /api/match-bundle                  customer upload (license key auth)
GET  /api/match-bundles?since=&limit=   admin listing
GET  /api/match-bundle/{install}/{match}?owner=  admin download (raw .json.gz)
GET  /admin/api/match-bundles/stats     admin dashboard counts

Auth for uploads tries, in order: the subscribers table (Patreon keys), the
trials table (7-day trial keys), then LiteLLM ``/key/info`` with the master
key (any other live virtual key).  There is deliberately no unauthenticated
or install-id-only path.

Storage layout:
``<data dir>/match_bundles/YYYY-MM-DD/<owner>/<install_id>/<match_id>.json.gz``
(``/app/data/match_bundles/...`` in the container), where ``owner`` is the
first 16 hex chars of sha256(license key).  Both ids are client-chosen, so the
owner segment is what keeps one key from writing into (or pre-empting the
dedupe of) another key's installs.  Ids are validated with a strict character
class, so they are safe as path components.

Abuse limits (per owner and per install, rolling 24 h): bundle count, bytes
and a minimum interval between uploads (429 + Retry-After), plus a free-space
floor on the data volume (507), so one trial key cannot fill the volume that
also holds the subscriber database.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import math
import os
import re
import shutil
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import patreon
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from state import (
    _extract_client_metadata,
    _extract_license_key,
    _get_db,
    _record_client_telemetry,
    _require_admin,
)

logger = logging.getLogger("website.match_bundles")

router = APIRouter()

MAX_BODY_BYTES = 8 * 1024 * 1024  # compressed upload cap (= the client's GZIP_CAP)
MAX_JSON_BYTES = 32 * 1024 * 1024  # decompressed cap (zip-bomb guard; client JSON is < 12 MB)
DECOMPRESS_CHUNK = 1024 * 1024
# Rolling-24 h quotas, each applied per owner (key digest) and per install.
QUOTA_WINDOW_S = 86400.0
MAX_BUNDLES_PER_DAY = 150
MAX_BYTES_PER_DAY = 256 * 1024 * 1024
MIN_UPLOAD_INTERVAL_S = 10.0
# Refuse writes when the data volume (shared with the SQLite DB) runs this low.
MIN_FREE_BYTES = int(os.environ.get("MATCH_BUNDLE_MIN_FREE_MB", "1024")) * 1024 * 1024
LIST_LIMIT_DEFAULT = 200
LIST_LIMIT_MAX = 2000
KEY_INFO_CACHE_TTL = 600.0  # positive /key/info verdicts
KEY_INFO_NEG_CACHE_TTL = 60.0  # negative verdicts (keeps spam off the gateway)
GZIP_MAGIC = b"\x1f\x8b"

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

# sha256(key) -> (expires_at_monotonic, ok)
_key_info_cache: dict[str, tuple[float, bool]] = {}


# ---------------------------------------------------------------------------
# Storage + index
# ---------------------------------------------------------------------------


def bundle_root() -> Path:
    """Directory that holds the bundle tree (next to the SQLite database)."""
    return Path(_get_db().DB_PATH).parent / "match_bundles"


def init_db() -> None:
    """Create the index table.  Idempotent; app.py calls it on every load.

    A pre-owner table (never deployed, but cheap to tolerate) is moved aside
    rather than migrated: its UNIQUE(install_id, match_id) constraint cannot
    be dropped in place.
    """
    with _get_db().get_db() as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(match_bundles)").fetchall()}
        if cols and "owner" not in cols:
            conn.execute("ALTER TABLE match_bundles RENAME TO match_bundles_v0")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS match_bundles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT NOT NULL,
                install_id TEXT NOT NULL,
                match_id TEXT NOT NULL,
                license_key TEXT,
                auth_source TEXT,
                received_at REAL NOT NULL,
                size INTEGER NOT NULL,
                path TEXT NOT NULL,
                app_version TEXT,
                platform TEXT,
                result TEXT,
                schema_version INTEGER,
                UNIQUE(owner, install_id, match_id)
            );
            CREATE INDEX IF NOT EXISTS idx_match_bundles_received ON match_bundles(received_at);
            CREATE INDEX IF NOT EXISTS idx_match_bundles_install ON match_bundles(install_id);
            CREATE INDEX IF NOT EXISTS idx_match_bundles_owner ON match_bundles(owner, received_at);
            """
        )


def _public_row(row: Any) -> dict[str, Any]:
    """Index row for admin consumers: the license key never leaves the server."""
    item = dict(row)
    item.pop("license_key", None)
    return item


def find_bundle(install_id: str, match_id: str, owner: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The index row for (install, match); with *owner* exact, else the earliest upload."""
    with _get_db().get_db() as conn:
        if owner is not None:
            row = conn.execute(
                "SELECT * FROM match_bundles WHERE owner = ? AND install_id = ? AND match_id = ?",
                (owner, install_id, match_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM match_bundles WHERE install_id = ? AND match_id = ? "
                "ORDER BY received_at ASC, id ASC LIMIT 1",
                (install_id, match_id),
            ).fetchone()
    return dict(row) if row else None


def owner_of(license_key: str) -> str:
    """Namespace for everything one license key uploads (never the key itself)."""
    return _key_digest(license_key)[:16]


def usage(owner: str, install_id: str, now: Optional[float] = None) -> dict[str, dict[str, float]]:
    """Rolling-window upload totals for an owner and an install: count, bytes, last upload."""
    now = time.time() if now is None else now
    since = now - QUOTA_WINDOW_S
    out: dict[str, dict[str, float]] = {}
    with _get_db().get_db() as conn:
        for scope, column, value in (("owner", "owner", owner), ("install", "install_id", install_id)):
            count, total, last = conn.execute(
                f"SELECT COUNT(*), COALESCE(SUM(size), 0), COALESCE(MAX(received_at), 0) "
                f"FROM match_bundles WHERE {column} = ? AND received_at > ?",
                (value, since),
            ).fetchone()
            out[scope] = {"count": int(count), "bytes": int(total), "last": float(last)}
    return out


def insert_bundle(
    *,
    owner: str,
    install_id: str,
    match_id: str,
    license_key: str,
    auth_source: str,
    size: int,
    path: str,
    app_version: str,
    platform: str,
    result: str,
    schema_version: Optional[int],
) -> bool:
    """Insert an index row.  Returns False when (owner, install_id, match_id) exists."""
    with _get_db().get_db() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO match_bundles (
                owner, install_id, match_id, license_key, auth_source, received_at, size,
                path, app_version, platform, result, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                owner,
                install_id,
                match_id,
                license_key,
                auth_source,
                time.time(),
                size,
                path,
                app_version,
                platform,
                result,
                schema_version,
            ),
        )
        return cur.rowcount > 0


def list_bundles(since: float = 0.0, limit: int = LIST_LIMIT_DEFAULT) -> list[dict[str, Any]]:
    with _get_db().get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM match_bundles WHERE received_at > ? ORDER BY received_at ASC, id ASC LIMIT ?",
            (since, limit),
        ).fetchall()
    return [_public_row(r) for r in rows]


def bundle_stats() -> dict[str, Any]:
    now = time.time()
    with _get_db().get_db() as conn:
        total, bytes_total = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM match_bundles"
        ).fetchone()
        last_24h = conn.execute(
            "SELECT COUNT(*) FROM match_bundles WHERE received_at > ?", (now - 86400,)
        ).fetchone()[0]
        last_7d = conn.execute(
            "SELECT COUNT(*) FROM match_bundles WHERE received_at > ?", (now - 7 * 86400,)
        ).fetchone()[0]
        installs_7d = conn.execute(
            "SELECT COUNT(DISTINCT install_id) FROM match_bundles WHERE received_at > ?",
            (now - 7 * 86400,),
        ).fetchone()[0]
        installs_total = conn.execute("SELECT COUNT(DISTINCT install_id) FROM match_bundles").fetchone()[0]
    return {
        "total": int(total),
        "bytes_total": int(bytes_total),
        "last_24h": int(last_24h),
        "last_7d": int(last_7d),
        "installs_7d": int(installs_7d),
        "installs_total": int(installs_total),
    }


# ---------------------------------------------------------------------------
# Upload auth
# ---------------------------------------------------------------------------


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def get_trial_by_key(key: str) -> Optional[dict[str, Any]]:
    """Trial record whose LiteLLM key is *key* (trial keys are not subscribers)."""
    with _get_db().get_db() as conn:
        row = conn.execute("SELECT * FROM trials WHERE litellm_key = ?", (key,)).fetchone()
    return dict(row) if row else None


def _key_digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


async def _litellm_key_is_live(key: str) -> bool:
    """Ask the gateway whether *key* is a live virtual key.

    The customer's key authenticates its own ``/key/info`` call (LiteLLM
    answers about the calling key when no ``key`` parameter is given), so
    the key never appears in a URL or the gateway's access log, and the
    master key is never sent on a customer's behalf.  The gateway path is
    only enabled where the deployment has a gateway configured (signalled by
    ``LITELLM_MASTER_KEY``).  Results are cached by key digest.  Any gateway
    error counts as "not verified" (the caller then returns 401) and is not
    cached for long, so a gateway blip does not lock anyone out for 10 min.
    """
    if not (os.environ.get("LITELLM_MASTER_KEY", "") or patreon.LITELLM_MASTER_KEY):
        return False

    digest = _key_digest(key)
    now = time.monotonic()
    cached = _key_info_cache.get(digest)
    if cached and cached[0] > now:
        return cached[1]

    ok = False
    ttl = KEY_INFO_NEG_CACHE_TTL
    try:
        resp = await patreon._http().get(
            f"{patreon.LITELLM_URL}/key/info",
            headers={"Authorization": f"Bearer {key}"},
        )
        if resp.status_code == 200:
            info = resp.json() if hasattr(resp, "json") else {}
            details = info.get("info") if isinstance(info, dict) else None
            details = details if isinstance(details, dict) else {}
            expires = details.get("expires")
            blocked = bool(details.get("blocked"))
            exp_dt = _parse_iso(expires) if expires else None
            expired = exp_dt is not None and exp_dt <= datetime.now(timezone.utc)
            ok = not blocked and not expired
            ttl = KEY_INFO_CACHE_TTL if ok else KEY_INFO_NEG_CACHE_TTL
        elif resp.status_code in (400, 401, 403, 404):
            ok = False
        else:
            logger.warning("LiteLLM /key/info returned HTTP %s", resp.status_code)
            ttl = 5.0
    except Exception as exc:  # network error, bad JSON, ...
        logger.warning("LiteLLM /key/info failed: %s", exc)
        ttl = 5.0

    if len(_key_info_cache) > 4096:
        _key_info_cache.clear()
    _key_info_cache[digest] = (now + ttl, ok)
    return ok


async def authorize_upload(request: Request) -> dict[str, str]:
    """Validate the Bearer key for a bundle upload.

    Returns ``{"license_key": key, "source": "subscriber"|"trial"|"litellm"}``
    or raises 401/402.
    """
    key = _extract_license_key(request).strip()
    if not key:
        raise HTTPException(401, "Missing license key")

    db = _get_db()
    sub = db.check_license(key)
    if sub:
        if sub["status"] in ("active", "trial"):
            return {"license_key": key, "source": "subscriber"}
        raise HTTPException(402, f"Subscription {sub['status']}")

    trial = get_trial_by_key(key)
    if trial:
        expires = _parse_iso(trial.get("expires_at") or "")
        if expires and expires > datetime.now(timezone.utc):
            return {"license_key": key, "source": "trial"}
        raise HTTPException(402, "Trial expired")

    if await _litellm_key_is_live(key):
        return {"license_key": key, "source": "litellm"}

    raise HTTPException(401, "Invalid license key")


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


async def _read_body_capped(request: Request, cap: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > cap:
                raise HTTPException(413, f"Bundle exceeds {cap // (1024 * 1024)} MB")
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            raise HTTPException(413, f"Bundle exceeds {cap // (1024 * 1024)} MB")
        chunks.append(chunk)
    return b"".join(chunks)


def _decode_bundle(raw: bytes) -> dict[str, Any]:
    if not raw.startswith(GZIP_MAGIC):
        raise HTTPException(415, "Expected a gzip-compressed JSON bundle")
    chunks: list[bytes] = []
    total = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
            while True:
                chunk = gz.read(DECOMPRESS_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_JSON_BYTES:
                    # Stop inflating as soon as the cap is crossed: a bomb costs
                    # at most MAX_JSON_BYTES + one chunk of memory.
                    raise HTTPException(413, "Decompressed bundle too large")
                chunks.append(chunk)
    except (OSError, EOFError, zlib.error) as exc:
        raise HTTPException(400, f"Invalid gzip payload: {exc}")
    text = b"".join(chunks)
    del chunks
    try:
        payload = json.loads(text)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise HTTPException(400, f"Bundle is not valid JSON: {type(exc).__name__}")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Bundle must be a JSON object")
    return payload


def _validated_id(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise HTTPException(422, f"{name} must match {_ID_RE.pattern}")
    return value


def _summary_fields(payload: dict[str, Any]) -> tuple[str, str, str, Optional[int]]:
    app = payload.get("app") if isinstance(payload.get("app"), dict) else {}
    settings = payload.get("settings_subset") if isinstance(payload.get("settings_subset"), dict) else {}
    packet = payload.get("packet") if isinstance(payload.get("packet"), dict) else {}
    version = str(app.get("version") or settings.get("version") or "")[:40]
    platform = str(app.get("platform") or settings.get("platform") or "")[:40]
    result = str(packet.get("result") or payload.get("result") or "")[:20]
    schema = payload.get("schema")
    schema_version = int(schema) if isinstance(schema, int) else None
    return version, platform, result, schema_version


def _relative_bundle_path(owner: str, install_id: str, match_id: str) -> str:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{day}/{owner}/{install_id}/{match_id}.json.gz"


def _enforce_quota(owner: str, install_id: str, size: int) -> None:
    """429 when this owner or install is over its rolling-24 h budget or uploading too fast."""
    now = time.time()
    for scope, totals in usage(owner, install_id, now).items():
        wait = MIN_UPLOAD_INTERVAL_S - (now - totals["last"])
        if totals["last"] and wait > 0:
            raise HTTPException(
                429,
                f"Too many uploads from this {scope}; retry in {math.ceil(wait)}s",
                headers={"Retry-After": str(math.ceil(wait))},
            )
        if totals["count"] >= MAX_BUNDLES_PER_DAY or totals["bytes"] + size > MAX_BYTES_PER_DAY:
            raise HTTPException(
                429,
                f"Daily match-bundle quota reached for this {scope}",
                headers={"Retry-After": str(int(QUOTA_WINDOW_S // 24))},
            )


def _enforce_free_space(root: Path) -> None:
    """507 when the data volume is close to full (it also holds the subscriber DB)."""
    probe = root
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return
    if free < MIN_FREE_BYTES:
        logger.warning("Match bundle refused: %d MB free on %s", free // (1024 * 1024), probe)
        raise HTTPException(507, "Server storage is full; try again later")


def _safe_target(root: Path, rel: str) -> Path:
    root_abs = root.resolve()
    target = (root_abs / rel).resolve()
    if root_abs not in target.parents:
        raise HTTPException(400, "Invalid bundle path")
    return target


@router.post("/api/match-bundle")
async def upload_match_bundle(request: Request, auth: dict = Depends(authorize_upload)):
    raw = await _read_body_capped(request, MAX_BODY_BYTES)
    if not raw:
        raise HTTPException(400, "Empty body")
    payload = _decode_bundle(raw)

    install_id = _validated_id(payload.get("install_id"), "install_id")
    match_id = _validated_id(payload.get("match_id"), "match_id")

    header_install = _extract_client_metadata(request).get("install_id", "")
    if header_install != install_id:
        raise HTTPException(403, "X-MTGACoach-Install-ID does not match the bundle's install_id")

    _record_client_telemetry(request, auth["license_key"])
    owner = owner_of(auth["license_key"])

    # Dedupe only within the uploader's own namespace: another key's upload
    # under the same ids can neither pre-empt nor be served as this one's.
    existing = find_bundle(install_id, match_id, owner)
    if existing:
        return {"status": "duplicate", "match_id": match_id, "received_at": existing["received_at"]}

    _enforce_quota(owner, install_id, len(raw))
    root = bundle_root()
    _enforce_free_space(root)

    version, platform, result, schema_version = _summary_fields(payload)
    rel = _relative_bundle_path(owner, install_id, match_id)
    target = _safe_target(root, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.gz.part")
    tmp.write_bytes(raw)
    os.replace(tmp, target)

    inserted = insert_bundle(
        owner=owner,
        install_id=install_id,
        match_id=match_id,
        license_key=auth["license_key"],
        auth_source=auth["source"],
        size=len(raw),
        path=rel,
        app_version=version,
        platform=platform,
        result=result,
        schema_version=schema_version,
    )
    if not inserted:
        # Lost a race with a concurrent upload of the same match: keep theirs.
        try:
            target.unlink()
        except OSError:
            pass
        prior = find_bundle(install_id, match_id, owner) or {}
        return {"status": "duplicate", "match_id": match_id, "received_at": prior.get("received_at")}

    logger.info(
        "Match bundle stored owner=%s install=%s match=%s size=%d version=%s result=%s auth=%s",
        owner,
        install_id,
        match_id,
        len(raw),
        version or "?",
        result or "?",
        auth["source"],
    )
    return {"status": "stored", "match_id": match_id, "size": len(raw), "path": rel}


# ---------------------------------------------------------------------------
# Admin read side
# ---------------------------------------------------------------------------


def _parse_since(value: str) -> float:
    value = (value or "").strip()
    if not value:
        return 0.0
    try:
        number = float(value)
    except ValueError:
        number = None
    if number is not None:
        if not math.isfinite(number):
            raise HTTPException(422, "since must be a finite epoch timestamp")
        return number
    dt = _parse_iso(value)
    if dt is None:
        raise HTTPException(422, "since must be an epoch timestamp or ISO-8601 datetime")
    return dt.timestamp()


@router.get("/api/match-bundles")
async def list_match_bundles(request: Request, _=Depends(_require_admin)):
    since = _parse_since(request.query_params.get("since", ""))
    try:
        limit = int(request.query_params.get("limit", str(LIST_LIMIT_DEFAULT)))
    except ValueError:
        limit = LIST_LIMIT_DEFAULT
    limit = max(1, min(LIST_LIMIT_MAX, limit))
    rows = list_bundles(since=since, limit=limit)
    return {"bundles": rows, "count": len(rows), "since": since, "limit": limit}


@router.get("/api/match-bundle/{install_id}/{match_id}")
async def download_match_bundle(install_id: str, match_id: str, request: Request, _=Depends(_require_admin)):
    install_id = _validated_id(install_id, "install_id")
    match_id = _validated_id(match_id, "match_id")
    owner = (request.query_params.get("owner") or "").strip() or None
    if owner is not None and not re.fullmatch(r"[0-9a-f]{16}", owner):
        raise HTTPException(422, "owner must be the 16-hex owner id from the listing")
    row = find_bundle(install_id, match_id, owner)
    if not row:
        raise HTTPException(404, "No such bundle")
    target = _safe_target(bundle_root(), row["path"])
    if not target.is_file():
        raise HTTPException(404, "Bundle file missing on disk")
    return FileResponse(
        str(target),
        media_type="application/gzip",
        filename=f"{install_id}_{match_id}.json.gz",
    )


@router.get("/admin/api/match-bundles/stats")
async def match_bundle_stats(request: Request, _=Depends(_require_admin)):
    return bundle_stats()


# Create the index table when the module is imported alongside the app;
# app.py calls init_db() again after state reload so test harnesses that
# swap the database module get a table too.
try:
    init_db()
except Exception as _exc:  # pragma: no cover - surfaced at app start instead
    logger.warning("match_bundles.init_db at import failed: %s", _exc)
