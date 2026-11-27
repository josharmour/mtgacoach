"""In-app Discord bug report intake, member gate, duplicate check, and private storage.

Privacy & Safety (Option A):
- Requires a valid Coach subscription/trial/gateway license key (no anonymous path).
- Requires verified membership in the configured MTGA Coach Discord server so every
  report has an identifiable Discord member (<@user_id>) for follow-up.
- Stores full redacted diagnostic JSON privately on the server data volume
  (``bug_reports/YYYY-MM-DD/<bug_id>.json.gz``); raw logs/JSON are NEVER posted
  to public Discord.
- Posts only title, category tag, user note, app version/OS, and optional user-approved
  PNG screenshots into the Discord ``#bug-reports`` Forum channel (either attaching to an
  existing open thread or creating a new one).
"""

from __future__ import annotations

import asyncio
import base64
import difflib
import gzip
import io
import json
import logging
import math
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import httpx
import match_bundles
import profiles
import state
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from state import _extract_client_metadata, _get_db, _record_client_telemetry, _require_admin

logger = logging.getLogger("website.bug_reports")

router = APIRouter()

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_TIMEOUT_S = 12.0
LLM_DEDUPE_TIMEOUT_S = 4.0

MAX_REQUEST_BODY_BYTES = 14 * 1024 * 1024
MAX_REPORT_GZ_BYTES = 4 * 1024 * 1024
MAX_REPORT_JSON_BYTES = 16 * 1024 * 1024
MAX_SCREENSHOTS = 2
MAX_SCREENSHOT_BYTES = 4 * 1024 * 1024
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
GZIP_MAGIC = b"\x1f\x8b"

QUOTA_WINDOW_S = 3600.0
MAX_REPORTS_PER_HOUR = 5
MIN_REPORT_INTERVAL_S = 30.0
DEDUPE_LOOKBACK_S = 60 * 86400.0
LEXICAL_DUPLICATE_THRESHOLD = 0.42

VALID_CATEGORIES = ("desktop", "autopilot", "coach", "website")
_SNOWFLAKE_RE = re.compile(r"^[0-9]{15,22}$")
_BUG_ID_RE = re.compile(r"^BUG-[A-F0-9]{6,12}$")

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "bug",
        "but",
        "by",
        "can",
        "cant",
        "did",
        "didnt",
        "do",
        "does",
        "doesnt",
        "for",
        "from",
        "get",
        "got",
        "had",
        "has",
        "have",
        "i",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "out",
        "report",
        "so",
        "that",
        "the",
        "their",
        "then",
        "there",
        "they",
        "this",
        "to",
        "up",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "why",
        "will",
        "with",
        "wont",
        "would",
        "you",
        "your",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")
_discord_client: Optional[httpx.AsyncClient] = None


def _get_discord_client() -> httpx.AsyncClient:
    global _discord_client
    if state.http_client is not None:
        return state.http_client
    if _discord_client is None or _discord_client.is_closed:
        _discord_client = httpx.AsyncClient(timeout=DISCORD_TIMEOUT_S)
    return _discord_client


async def shutdown() -> None:
    global _discord_client
    if _discord_client is not None and not _discord_client.is_closed:
        await _discord_client.aclose()
        _discord_client = None


def bug_reports_root() -> Path:
    """Directory storing private gzipped diagnostic payloads (next to SQLite DB)."""
    return Path(_get_db().DB_PATH).parent / "bug_reports"


def init_db() -> None:
    """Ensure the bug_reports table and indexes exist (idempotent)."""
    with _get_db().get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS bug_reports (
                bug_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                discord_user_id TEXT NOT NULL,
                discord_username TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'desktop',
                title TEXT NOT NULL,
                note TEXT NOT NULL,
                app_version TEXT NOT NULL DEFAULT '',
                platform TEXT NOT NULL DEFAULT '',
                discord_thread_id TEXT NOT NULL,
                discord_thread_url TEXT NOT NULL,
                attached_to_existing INTEGER NOT NULL DEFAULT 0,
                report_rel_path TEXT NOT NULL DEFAULT '',
                report_bytes INTEGER NOT NULL DEFAULT 0,
                screenshot_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'open',
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_bug_reports_owner_ts
                ON bug_reports(owner, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_bug_reports_status_ts
                ON bug_reports(status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_bug_reports_thread
                ON bug_reports(discord_thread_id, created_at DESC);
            """
        )


def _discord_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bot {token}",
        "User-Agent": "DiscordBot (https://mtgacoach.com, 1.0)",
    }


def _clean_discord_handle(raw: Any) -> str:
    text = str(raw or "").strip()
    if text.startswith("@"):
        text = text[1:].strip()
    if "#" in text:
        name_part, disc = text.rsplit("#", 1)
        if disc.isdigit() and len(disc) <= 4:
            text = name_part.strip()
    return text[:64]


def _pick_matched_member(members: list[dict[str, Any]], query: str) -> Optional[dict[str, Any]]:
    """Select an exact (or unambiguous display/nick) non-bot guild member match."""
    q_low = query.lower()
    human: list[dict[str, Any]] = []
    for m in members:
        if not isinstance(m, dict):
            continue
        user = m.get("user")
        if not isinstance(user, dict) or user.get("bot"):
            continue
        if not str(user.get("id") or "").strip():
            continue
        human.append(m)

    # 1. Exact username match (Discord usernames are unique & lowercase in modern Discord)
    for m in human:
        uname = str((m.get("user") or {}).get("username") or "").strip().lower()
        if uname == q_low:
            return m

    # 2. Unambiguous exact global_name or server nick match
    display_matches = [
        m
        for m in human
        if q_low
        in {
            str((m.get("user") or {}).get("global_name") or "").strip().lower(),
            str(m.get("nick") or "").strip().lower(),
        }
        - {""}
    ]
    if len(display_matches) == 1:
        return display_matches[0]
    return None


async def verify_discord_member(handle: str) -> dict[str, str]:
    """Verify *handle* belongs to a non-bot member in the configured MTGA Coach guild."""
    clean = _clean_discord_handle(handle)
    invite_url = state.get_discord_invite_url()
    if not clean:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "discord_handle_required",
                "message": "Enter your Discord username (and make sure you've joined the MTGA Coach Discord server) so we can follow up with you.",
                "invite_url": invite_url,
            },
        )

    token = state.get_discord_bot_token()
    guild_id = state.get_discord_guild_id()
    channel_id = state.get_discord_bug_reports_channel_id()
    if not token or not guild_id or not channel_id:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "discord_not_configured",
                "message": "Discord bug reporting is not configured on the server yet.",
                "invite_url": invite_url,
            },
        )

    url = f"{DISCORD_API_BASE}/guilds/{guild_id}/members/search?query={quote(clean)}&limit=10"
    try:
        client = _get_discord_client()
        resp = await client.get(url, headers=_discord_headers(token), timeout=DISCORD_TIMEOUT_S)
    except httpx.HTTPError as exc:
        logger.warning("Discord member search failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={
                "error": "discord_unavailable",
                "message": "Couldn't reach Discord to verify your server membership. Please try again in a moment.",
                "invite_url": invite_url,
            },
        ) from exc

    if resp.status_code != 200:
        logger.warning("Discord member search returned HTTP %s: %s", resp.status_code, resp.text[:200])
        raise HTTPException(
            status_code=503,
            detail={
                "error": "discord_unavailable",
                "message": "Discord verification is temporarily unavailable. Please try again shortly.",
                "invite_url": invite_url,
            },
        )

    try:
        members = resp.json()
    except ValueError:
        members = []
    if not isinstance(members, list):
        members = []

    match = _pick_matched_member(members, clean)
    if not match:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "not_in_discord_server",
                "message": (
                    f"We couldn't find '@{clean}' in the MTGA Coach Discord server. "
                    "Please join the server first so we can follow up with you, then try again."
                ),
                "invite_url": invite_url,
            },
        )

    user = match["user"]
    return {
        "user_id": str(user["id"]),
        "username": str(user.get("username") or clean),
        "display_name": str(match.get("nick") or user.get("global_name") or user.get("username") or clean),
    }


def _tokenize(text: str) -> set[str]:
    return {tok for tok in _TOKEN_RE.findall(str(text or "").lower()) if tok not in _STOPWORDS}


def _lexical_similarity(title: str, note: str, cand_title: str, cand_note: str) -> float:
    q_title_tokens = _tokenize(title)
    c_title_tokens = _tokenize(cand_title)
    q_all_tokens = _tokenize(f"{title} {note}")
    c_all_tokens = _tokenize(f"{cand_title} {cand_note}")

    title_jaccard = (
        len(q_title_tokens & c_title_tokens) / max(1, len(q_title_tokens | c_title_tokens))
        if (q_title_tokens and c_title_tokens)
        else 0.0
    )
    all_jaccard = (
        len(q_all_tokens & c_all_tokens) / max(1, len(q_all_tokens | c_all_tokens))
        if (q_all_tokens and c_all_tokens)
        else 0.0
    )
    seq_title = difflib.SequenceMatcher(None, title.lower(), cand_title.lower()).ratio()
    return round(max(title_jaccard * 0.65 + seq_title * 0.35, all_jaccard * 0.8 + seq_title * 0.2), 4)


def _collect_open_candidates(
    *,
    guild_id: str,
    channel_id: str,
    fixed_tag_id: str,
    discord_threads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    cutoff = time.time() - DEDUPE_LOOKBACK_S
    by_thread: dict[str, dict[str, Any]] = {}

    with _get_db().get_db() as conn:
        rows = conn.execute(
            """
            SELECT discord_thread_id, discord_thread_url, title, note, category, created_at
            FROM bug_reports
            WHERE status = 'open' AND created_at > ?
            ORDER BY created_at ASC
            """,
            (cutoff,),
        ).fetchall()

    for r in rows:
        tid = str(r["discord_thread_id"] or "").strip()
        if not tid:
            continue
        entry = by_thread.get(tid)
        if entry is None:
            by_thread[tid] = {
                "thread_id": tid,
                "url": str(r["discord_thread_url"] or f"https://discord.com/channels/{guild_id}/{tid}"),
                "title": str(r["title"] or ""),
                "note": str(r["note"] or ""),
                "category": str(r["category"] or ""),
                "report_count": 1,
                "created_at": float(r["created_at"] or 0.0),
            }
        else:
            entry["report_count"] += 1
            if len(entry["note"]) < 400 and r["note"]:
                entry["note"] = (entry["note"] + " " + str(r["note"]))[:500]

    closed_tids: list[str] = []
    for th in discord_threads:
        if not isinstance(th, dict):
            continue
        if str(th.get("parent_id") or "") != channel_id:
            continue
        tid = str(th.get("id") or "").strip()
        if not tid:
            continue
        meta = th.get("thread_metadata") or {}
        applied = [str(t) for t in (th.get("applied_tags") or [])]
        if meta.get("archived") or meta.get("locked") or (fixed_tag_id and fixed_tag_id in applied):
            by_thread.pop(tid, None)
            closed_tids.append(tid)
            continue
        raw_name = str(th.get("name") or "").strip()
        clean_name = re.sub(r"^\[BUG-[A-F0-9]{6,12}\]\s*", "", raw_name)
        if tid in by_thread:
            if clean_name:
                by_thread[tid]["title"] = clean_name
        else:
            by_thread[tid] = {
                "thread_id": tid,
                "url": f"https://discord.com/channels/{guild_id}/{tid}",
                "title": clean_name or raw_name,
                "note": "",
                "category": "",
                "report_count": max(1, int(th.get("total_message_sent") or 1)),
                "created_at": 0.0,
            }

    if closed_tids:
        with _get_db().get_db() as conn:
            conn.executemany(
                "UPDATE bug_reports SET status = 'closed' WHERE discord_thread_id = ? AND status = 'open'",
                [(tid,) for tid in closed_tids],
            )

    return list(by_thread.values())


async def _fetch_active_forum_threads(token: str, guild_id: str) -> list[dict[str, Any]]:
    if not token or not guild_id:
        return []
    url = f"{DISCORD_API_BASE}/guilds/{guild_id}/threads/active"
    try:
        client = _get_discord_client()
        resp = await client.get(url, headers=_discord_headers(token), timeout=DISCORD_TIMEOUT_S)
        if resp.status_code == 200:
            data = resp.json()
            threads = data.get("threads") if isinstance(data, dict) else None
            return threads if isinstance(threads, list) else []
    except Exception as exc:
        logger.debug("Active thread lookup skipped: %s", exc)
    return []


async def _llm_rank_duplicates(
    title: str,
    note: str,
    candidates: list[dict[str, Any]],
) -> Optional[dict[str, str]]:
    """Ask the configured fast model whether any candidate describes the same bug.

    Returns ``{thread_id: short_reason}`` on success, or ``None`` if unavailable/timed out.
    """
    if os.environ.get("BUG_REPORT_LLM_DEDUPE", "1").strip() == "0":
        return None
    if not candidates or not getattr(state, "router", None):
        return None
    provider = next((p for p in state.router.providers if getattr(p, "available", False)), None)
    if provider is None:
        return None

    cand_lines = [
        {
            "thread_id": c["thread_id"],
            "title": c["title"][:120],
            "note": c["note"][:220],
        }
        for c in candidates[:8]
    ]
    prompt = (
        "You deduplicate user bug reports for MTGA Coach.\n"
        'Return ONLY JSON: {"matches": [{"thread_id": "...", "reason": "short reason"}]}.\n'
        "Include ONLY candidates that likely have the SAME underlying bug or symptom as the new report (at most 3). "
        'If none match closely, return {"matches": []}.\n\n'
        f"New report title: {title[:120]}\n"
        f"New report details: {note[:400]}\n\n"
        f"Open threads:\n{json.dumps(cand_lines, ensure_ascii=False)}"
    )
    model = (getattr(provider, "models", None) or [state._resolved_default_model() or "gpt-5.4"])[0]
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": 220,
    }
    try:
        client = _get_discord_client()
        resp = await asyncio.wait_for(provider.forward_chat(body, client), timeout=LLM_DEDUPE_TIMEOUT_S)
        if resp.status_code != 200:
            return None
        data = resp.json()
        text = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        if not text:
            return None
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()
        parsed = json.loads(text)
        raw_matches = parsed.get("matches") if isinstance(parsed, dict) else None
        if not isinstance(raw_matches, list):
            return None
        valid_ids = {c["thread_id"] for c in cand_lines}
        out: dict[str, str] = {}
        for item in raw_matches:
            if not isinstance(item, dict):
                continue
            tid = str(item.get("thread_id") or "").strip()
            reason = " ".join(str(item.get("reason") or "Similar open report").split())[:140]
            if tid in valid_ids:
                out[tid] = reason or "Similar open report"
        return out
    except Exception as exc:
        logger.debug("LLM bug dedupe fallback to lexical matching: %s", exc)
        return None


async def find_similar_open_reports(
    *,
    title: str,
    note: str,
    token: str,
    guild_id: str,
    channel_id: str,
    fixed_tag_id: str,
) -> list[dict[str, Any]]:
    """Return up to 3 open #bug-reports threads similar to *(title, note)*."""
    discord_threads = await _fetch_active_forum_threads(token, guild_id)
    candidates = _collect_open_candidates(
        guild_id=guild_id,
        channel_id=channel_id,
        fixed_tag_id=fixed_tag_id,
        discord_threads=discord_threads,
    )
    if not candidates:
        return []

    scored: list[tuple[float, dict[str, Any]]] = []
    for cand in candidates:
        score = _lexical_similarity(title, note, cand["title"], cand.get("note", ""))
        scored.append((score, cand))
    scored.sort(key=lambda item: (-item[0], -item[1]["report_count"], item[1]["thread_id"]))

    llm_reasons = await _llm_rank_duplicates(title, note, [item[1] for item in scored[:8]])
    results: list[dict[str, Any]] = []
    for score, cand in scored:
        tid = cand["thread_id"]
        if llm_reasons is not None:
            if tid not in llm_reasons:
                continue
            reason = llm_reasons[tid]
        else:
            if score < LEXICAL_DUPLICATE_THRESHOLD:
                continue
            reason = "Similar title or description"
        results.append(
            {
                "thread_id": tid,
                "title": cand["title"],
                "url": cand["url"],
                "report_count": int(cand["report_count"]),
                "score": score,
                "reason": reason,
            }
        )
        if len(results) >= 3:
            break
    return results


def _enforce_report_quota(owner: str, now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    since = now - QUOTA_WINDOW_S
    with _get_db().get_db() as conn:
        count, last = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(created_at), 0) FROM bug_reports WHERE owner = ? AND created_at > ?",
            (owner, since),
        ).fetchone()
    last_f = float(last or 0.0)
    if last_f and (now - last_f) < MIN_REPORT_INTERVAL_S:
        wait = max(1, math.ceil(MIN_REPORT_INTERVAL_S - (now - last_f)))
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rate_limited",
                "message": f"Please wait {wait}s before submitting another bug report.",
            },
            headers={"Retry-After": str(wait)},
        )
    if int(count or 0) >= MAX_REPORTS_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "quota_exceeded",
                "message": "Hourly bug report limit reached (5/hour). Add details directly in your existing Discord thread if needed.",
            },
            headers={"Retry-After": "600"},
        )


def _decode_report_payload(body: dict[str, Any]) -> bytes:
    """Validate and return gzip-compressed bytes of the redacted diagnostic JSON."""
    raw_b64 = body.get("report_gz_b64")
    if isinstance(raw_b64, str) and raw_b64.strip():
        try:
            gz_bytes = base64.b64decode(raw_b64.strip(), validate=True)
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_report_payload",
                    "message": "Diagnostic bundle was not valid base64.",
                },
            ) from exc
        if len(gz_bytes) > MAX_REPORT_GZ_BYTES:
            raise HTTPException(
                status_code=413,
                detail={
                    "error": "report_too_large",
                    "message": "Diagnostic payload exceeds 4 MB compressed limit.",
                },
            )
        if not gz_bytes.startswith(GZIP_MAGIC):
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_report_payload",
                    "message": "Diagnostic bundle must be gzip-compressed JSON.",
                },
            )
        chunks: list[bytes] = []
        total = 0
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(gz_bytes)) as gz:
                while True:
                    chunk = gz.read(256 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_REPORT_JSON_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail={
                                "error": "report_too_large",
                                "message": "Diagnostic JSON exceeds 16 MB limit.",
                            },
                        )
                    chunks.append(chunk)
            parsed = json.loads(b"".join(chunks).decode("utf-8"))
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_report_payload", "message": "Could not decode diagnostic JSON."},
            ) from exc
        if not isinstance(parsed, dict):
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_report_payload", "message": "Diagnostic JSON must be an object."},
            )
        return gz_bytes

    report_obj = body.get("report")
    if isinstance(report_obj, dict):
        encoded = json.dumps(report_obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_REPORT_JSON_BYTES:
            raise HTTPException(
                status_code=413,
                detail={"error": "report_too_large", "message": "Diagnostic JSON exceeds 16 MB limit."},
            )
        return gzip.compress(encoded)

    return gzip.compress(b"{}")


def _decode_screenshots(raw_list: Any) -> list[tuple[str, bytes]]:
    if raw_list is None:
        return []
    if not isinstance(raw_list, list):
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_screenshots", "message": "Screenshots must be a list."},
        )
    if len(raw_list) > MAX_SCREENSHOTS:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "too_many_screenshots",
                "message": f"At most {MAX_SCREENSHOTS} screenshots allowed.",
            },
        )
    out: list[tuple[str, bytes]] = []
    for idx, item in enumerate(raw_list):
        if not isinstance(item, dict):
            continue
        b64 = str(item.get("png_b64") or "").strip()
        if not b64:
            continue
        try:
            raw = base64.b64decode(b64, validate=True)
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_screenshots",
                    "message": "Screenshot was not valid base64 PNG data.",
                },
            ) from exc
        if len(raw) > MAX_SCREENSHOT_BYTES or not raw.startswith(PNG_MAGIC):
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_screenshots",
                    "message": "Screenshots must be valid PNG files under 4 MB.",
                },
            )
        raw_name = re.sub(
            r"[^A-Za-z0-9_.-]+", "_", str(item.get("name") or f"screenshot_{idx + 1}.png")
        ).strip("._")
        if not raw_name.lower().endswith(".png"):
            raw_name = f"{raw_name or f'screenshot_{idx + 1}'}.png"
        out.append((raw_name[:60], raw))
    return out


async def _validate_existing_thread(
    *,
    thread_id: str,
    token: str,
    channel_id: str,
    guild_id: str,
    fixed_tag_id: str,
) -> str:
    """Confirm *thread_id* is an open thread inside #bug-reports and return its URL."""
    if not _SNOWFLAKE_RE.fullmatch(thread_id):
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_thread", "message": "Selected Discord thread ID is invalid."},
        )

    # First consult live thread metadata so archived/locked/Fixed threads are rejected
    try:
        client = _get_discord_client()
        resp = await client.get(
            f"{DISCORD_API_BASE}/channels/{thread_id}",
            headers=_discord_headers(token),
            timeout=DISCORD_TIMEOUT_S,
        )
        if resp.status_code == 200:
            th = resp.json()
            if str(th.get("parent_id") or "") != channel_id:
                raise HTTPException(
                    status_code=422,
                    detail={"error": "invalid_thread", "message": "Selected thread is not in #bug-reports."},
                )
            meta = th.get("thread_metadata") or {}
            if meta.get("locked") or meta.get("archived"):
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "thread_closed",
                        "message": "That bug report thread is closed or archived. Please create a new post instead.",
                    },
                )
            applied = [str(t) for t in (th.get("applied_tags") or [])]
            if fixed_tag_id and fixed_tag_id in applied:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "thread_closed",
                        "message": "That bug report is already marked Fixed. Please create a new post if it still occurs.",
                    },
                )
            return f"https://discord.com/channels/{guild_id}/{thread_id}"
        if resp.status_code == 404:
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_thread", "message": "Selected Discord thread no longer exists."},
            )
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        logger.warning("Discord thread lookup failed for %s: %s", thread_id, exc)

    # Fallback to local DB if Discord GET was transiently unavailable
    with _get_db().get_db() as conn:
        row = conn.execute(
            "SELECT discord_thread_url, status FROM bug_reports WHERE discord_thread_id = ? LIMIT 1",
            (thread_id,),
        ).fetchone()
    if row and row["status"] == "open":
        return str(row["discord_thread_url"] or f"https://discord.com/channels/{guild_id}/{thread_id}")
    raise HTTPException(
        status_code=422,
        detail={"error": "invalid_thread", "message": "Could not verify the selected Discord thread."},
    )


async def _discord_post_with_files(
    url: str,
    token: str,
    payload: dict[str, Any],
    screenshots: list[tuple[str, bytes]],
) -> dict[str, Any]:
    client = _get_discord_client()
    headers = _discord_headers(token)
    try:
        if not screenshots:
            resp = await client.post(url, json=payload, headers=headers, timeout=DISCORD_TIMEOUT_S)
        else:
            files = {
                f"files[{idx}]": (filename, data, "image/png")
                for idx, (filename, data) in enumerate(screenshots)
            }
            resp = await client.post(
                url,
                data={"payload_json": json.dumps(payload, ensure_ascii=False)},
                files=files,
                headers=headers,
                timeout=DISCORD_TIMEOUT_S,
            )
    except httpx.HTTPError as exc:
        logger.warning("Discord post %s network error: %s", url, exc)
        raise HTTPException(
            status_code=503,
            detail={
                "error": "discord_unavailable",
                "message": "Couldn't reach Discord to post your bug report. Please try again in a moment.",
            },
        ) from exc
    if resp.status_code not in (200, 201):
        logger.warning("Discord post %s returned HTTP %s: %s", url, resp.status_code, resp.text[:250])
        raise HTTPException(
            status_code=502,
            detail={
                "error": "discord_post_failed",
                "message": "Discord rejected the bug report post. Please try again later.",
            },
        )
    return resp.json()


def _new_bug_id() -> str:
    return f"BUG-{secrets.token_hex(4).upper()}"


def _save_private_report(bug_id: str, gz_bytes: bytes, now: float) -> str:
    root = bug_reports_root()
    match_bundles._enforce_free_space(root)
    day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
    rel = f"{day}/{bug_id}.json.gz"
    target = match_bundles._safe_target(root, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.gz.part")
    tmp.write_bytes(gz_bytes)
    os.replace(tmp, target)
    return rel


@router.get("/api/bug-reports/me")
async def bug_report_me(auth: dict = Depends(match_bundles.authorize_upload)):
    """Return the signed-in customer's saved Discord handle + public invite URL."""
    owner = match_bundles.owner_of(auth["license_key"])
    with _get_db().get_db() as conn:
        row = conn.execute("SELECT discord_handle FROM player_profiles WHERE owner = ?", (owner,)).fetchone()
    return {
        "discord_handle": str(row["discord_handle"] or "") if row else "",
        "invite_url": state.get_discord_invite_url(),
    }


@router.post("/api/bug-reports")
async def submit_bug_report(request: Request, auth: dict = Depends(match_bundles.authorize_upload)):
    """Verify Discord membership, check for open duplicates, and post Option A summary to #bug-reports."""
    raw_body = await match_bundles._read_body_capped(request, MAX_REQUEST_BODY_BYTES)
    if not raw_body:
        raise HTTPException(
            status_code=400, detail={"error": "empty_body", "message": "Request body was empty."}
        )
    try:
        body = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_json", "message": "Request body must be valid JSON."},
        ) from exc
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_json", "message": "Request body must be a JSON object."},
        )

    _record_client_telemetry(request, auth["license_key"])
    meta = _extract_client_metadata(request)
    owner = match_bundles.owner_of(auth["license_key"])

    title = " ".join(str(body.get("title") or "").split())
    note = str(body.get("note") or "").strip()
    category = str(body.get("category") or "desktop").strip().lower() or "desktop"
    existing_thread_id = str(body.get("existing_thread_id") or "").strip()
    force_new = bool(body.get("force_new"))
    app_version = " ".join(str(body.get("app_version") or meta.get("client_version") or "").split())[:40]
    plat = " ".join(str(body.get("platform") or meta.get("user_agent") or "").split())[:60]

    if len(title) < 4 or len(title) > 90:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_title", "message": "Title must be between 4 and 90 characters."},
        )
    if len(note) < 10 or len(note) > 2500:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_note",
                "message": "Description must be between 10 and 2500 characters.",
            },
        )
    if category not in VALID_CATEGORIES:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_category",
                "message": f"Category must be one of: {', '.join(VALID_CATEGORIES)}.",
            },
        )

    gz_bytes = _decode_report_payload(body)
    screenshots = _decode_screenshots(body.get("screenshots"))

    # Fall back to saved profile handle if client omitted it
    raw_handle = body.get("discord_handle")
    if not str(raw_handle or "").strip():
        with _get_db().get_db() as conn:
            row = conn.execute(
                "SELECT discord_handle FROM player_profiles WHERE owner = ?", (owner,)
            ).fetchone()
            if row and row["discord_handle"]:
                raw_handle = row["discord_handle"]

    member = await verify_discord_member(str(raw_handle or ""))

    # Sync verified Discord handle back to player_profiles
    now = time.time()
    with _get_db().get_db() as conn:
        profiles.ensure_profile(conn, owner, auth_source=auth["source"])
        conn.execute(
            "UPDATE player_profiles SET discord_handle = ?, updated_at = ? WHERE owner = ?",
            (member["username"], now, owner),
        )

    _enforce_report_quota(owner, now)

    token = state.get_discord_bot_token()
    guild_id = state.get_discord_guild_id()
    channel_id = state.get_discord_bug_reports_channel_id()
    fixed_tag_id = state.get_discord_fixed_tag_id()
    bug_tags = state.get_discord_bug_tag_ids()

    if not existing_thread_id and not force_new:
        matches = await find_similar_open_reports(
            title=title,
            note=note,
            token=token,
            guild_id=guild_id,
            channel_id=channel_id,
            fixed_tag_id=fixed_tag_id,
        )
        if matches:
            return {
                "status": "possible_duplicates",
                "discord_username": member["username"],
                "matches": matches,
            }

    match_bundles._enforce_free_space(bug_reports_root())
    bug_id = _new_bug_id()
    with _get_db().get_db() as conn:
        for _ in range(8):
            if not conn.execute("SELECT 1 FROM bug_reports WHERE bug_id = ?", (bug_id,)).fetchone():
                break
            bug_id = _new_bug_id()

    kb = max(1, (len(gz_bytes) + 1023) // 1024)
    embed_fields = [
        {"name": "Category", "value": category.capitalize(), "inline": True},
        {"name": "App", "value": app_version or "Unknown", "inline": True},
        {"name": "OS", "value": plat or "Unknown", "inline": True},
        {
            "name": "Diagnostics",
            "value": f"Stored privately on server ({kb} KB) — ID `{bug_id}`",
            "inline": False,
        },
    ]
    attachments = [{"id": idx, "filename": filename} for idx, (filename, _data) in enumerate(screenshots)]

    if existing_thread_id:
        thread_url = await _validate_existing_thread(
            thread_id=existing_thread_id,
            token=token,
            channel_id=channel_id,
            guild_id=guild_id,
            fixed_tag_id=fixed_tag_id,
        )
        msg_payload: dict[str, Any] = {
            "content": f"Additional report (**{bug_id}**) from <@{member['user_id']}> (`{member['username']}`)",
            "allowed_mentions": {"parse": [], "users": [member["user_id"]]},
            "embeds": [
                {
                    "title": f"[{bug_id}] Follow-up: {title}"[:256],
                    "description": note[:2000],
                    "color": 0x4EA1D3,
                    "fields": embed_fields,
                }
            ],
        }
        if attachments:
            msg_payload["attachments"] = attachments
        await _discord_post_with_files(
            f"{DISCORD_API_BASE}/channels/{existing_thread_id}/messages",
            token,
            msg_payload,
            screenshots,
        )
        thread_id = existing_thread_id
        attached_flag = 1
        status_label = "attached"
    else:
        inner_msg: dict[str, Any] = {
            "content": f"Reported by <@{member['user_id']}> (`{member['username']}`)",
            "allowed_mentions": {"parse": [], "users": [member["user_id"]]},
            "embeds": [
                {
                    "title": f"[{bug_id}] {title}"[:256],
                    "description": note[:2000],
                    "color": 0xD4A13C,
                    "fields": embed_fields,
                }
            ],
        }
        if attachments:
            inner_msg["attachments"] = attachments
        thread_payload: dict[str, Any] = {
            "name": f"[{bug_id}] {title}"[:100],
            "message": inner_msg,
        }
        tag_id = bug_tags.get(category)
        if tag_id:
            thread_payload["applied_tags"] = [tag_id]
        created = await _discord_post_with_files(
            f"{DISCORD_API_BASE}/channels/{channel_id}/threads",
            token,
            thread_payload,
            screenshots,
        )
        thread_id = str(created.get("id") or "")
        if not thread_id:
            raise HTTPException(
                status_code=502,
                detail={"error": "discord_post_failed", "message": "Discord did not return a thread ID."},
            )
        thread_url = f"https://discord.com/channels/{guild_id}/{thread_id}"
        attached_flag = 0
        status_label = "created"

    rel_path = _save_private_report(bug_id, gz_bytes, now)
    with _get_db().get_db() as conn:
        conn.execute(
            """
            INSERT INTO bug_reports (
                bug_id, owner, discord_user_id, discord_username, category, title, note,
                app_version, platform, discord_thread_id, discord_thread_url,
                attached_to_existing, report_rel_path, report_bytes, screenshot_count,
                status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
            """,
            (
                bug_id,
                owner,
                member["user_id"],
                member["username"],
                category,
                title,
                note,
                app_version,
                plat,
                thread_id,
                thread_url,
                attached_flag,
                rel_path,
                len(gz_bytes),
                len(screenshots),
                now,
            ),
        )

    return JSONResponse(
        status_code=201,
        content={
            "status": status_label,
            "bug_id": bug_id,
            "discord_username": member["username"],
            "thread_id": thread_id,
            "thread_url": thread_url,
        },
    )


@router.get("/admin/api/bug-reports")
async def admin_list_bug_reports(
    limit: int = Query(default=50, ge=1, le=500),
    _=Depends(_require_admin),
):
    """List submitted bug reports and private diagnostic sizes for admin triage."""
    with _get_db().get_db() as conn:
        rows = conn.execute(
            """
            SELECT bug_id, owner, discord_user_id, discord_username, category, title, note,
                   app_version, platform, discord_thread_id, discord_thread_url,
                   attached_to_existing, report_bytes, screenshot_count, status, created_at
            FROM bug_reports
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return {"reports": [dict(r) for r in rows]}


@router.get("/admin/api/bug-reports/{bug_id}/download")
async def admin_download_bug_report(bug_id: str, _=Depends(_require_admin)):
    """Download the private redacted ``.json.gz`` diagnostics for *bug_id*."""
    clean_id = bug_id.strip().upper()
    if not _BUG_ID_RE.fullmatch(clean_id):
        raise HTTPException(400, "Invalid bug_id")
    with _get_db().get_db() as conn:
        row = conn.execute(
            "SELECT report_rel_path FROM bug_reports WHERE bug_id = ?",
            (clean_id,),
        ).fetchone()
    if not row or not row["report_rel_path"]:
        raise HTTPException(404, "Bug report not found")
    target = match_bundles._safe_target(bug_reports_root(), str(row["report_rel_path"]))
    if not target.is_file():
        raise HTTPException(404, "Bug report file missing on disk")
    return FileResponse(
        target,
        media_type="application/gzip",
        filename=f"{clean_id}.json.gz",
    )
