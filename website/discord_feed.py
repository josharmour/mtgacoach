"""Automatic Discord webhook live-updating leaderboard & top decks board."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from weakref import WeakKeyDictionary

import httpx
import profiles
import state
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

logger = logging.getLogger("website.discord_feed")

router = APIRouter()

DISCORD_FEED_CHECK_SECONDS = 60.0
DISCORD_FEED_DEBOUNCE_SECONDS = 3.0
DISCORD_FEED_DEFAULT_COOLDOWN_SECONDS = 900.0
DISCORD_FEED_POST_TIMEOUT_SECONDS = 15.0
DISCORD_FEED_LIMIT = 5
LIVE_BOARD_KEY = "live_board"

_POSITION_BADGES = ("🥇", "🥈", "🥉", "**#4**", "**#5**")
_WEBHOOK_SECRET_RE = re.compile(r"https://discord(?:app)?\.com/api/webhooks/[^\s\"'<>]+")

_FEED_SPECS: tuple[dict[str, Any], ...] = (
    {
        "key": "rank",
        "tab": "rank",
        "kind": "player",
        "title": "Highest Rank",
        "color": 0xD4A13C,
        "footer_path": "/leaderboard?tab=rank",
        "footer_text": "Last 30 days · All formats · mtgacoach.com",
    },
    {
        "key": "winrate",
        "tab": "winrate",
        "kind": "player",
        "title": "Highest Win Rate (20+ games)",
        "color": 0x4EA1D3,
        "footer_path": "/leaderboard?tab=winrate",
        "footer_text": "Last 30 days · 20+ games · mtgacoach.com",
    },
    {
        "key": "decks",
        "tab": "decks",
        "kind": "deck",
        "title": "Top Decks (10+ games)",
        "color": 0x57B874,
        "footer_path": "/leaderboard?tab=decks",
        "footer_text": "All-time · 10+ games · Public decks · mtgacoach.com",
    },
)

_check_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = WeakKeyDictionary()
_dirty_task: Optional[asyncio.Task[None]] = None
_dirty_pending = False


def init_db() -> None:
    """Ensure Discord feed state tracking table and schema migrations exist."""
    with state._get_db().get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS discord_feed_state (
                feed_key TEXT PRIMARY KEY,
                last_ids_json TEXT NOT NULL DEFAULT '[]',
                last_snapshot_json TEXT NOT NULL DEFAULT '[]',
                last_posted_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0,
                message_id TEXT NOT NULL DEFAULT ''
            );
            """
        )
        cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(discord_feed_state)").fetchall()}
        if "message_id" not in cols:
            conn.execute("ALTER TABLE discord_feed_state ADD COLUMN message_id TEXT NOT NULL DEFAULT ''")


def _get_check_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _check_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _check_locks[loop] = lock
    return lock


def _redact_secret(text: str, webhook_url: str = "") -> str:
    cleaned = text.replace(webhook_url, "[REDACTED_WEBHOOK]") if webhook_url else text
    return _WEBHOOK_SECRET_RE.sub("https://discord.com/api/webhooks/[REDACTED]", cleaned)


def _is_feed_enabled() -> bool:
    return os.environ.get("DISCORD_FEED_ENABLED", "1").strip() != "0"


def _public_base_url() -> str:
    val = os.environ.get("PUBLIC_BASE_URL", "https://mtgacoach.com").strip().rstrip("/")
    return val or "https://mtgacoach.com"


def _cooldown_seconds() -> float:
    raw = os.environ.get("DISCORD_FEED_COOLDOWN_SECONDS", "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return DISCORD_FEED_DEFAULT_COOLDOWN_SECONDS


def _pct_str(val: Optional[float]) -> str:
    if val is None:
        return "—"
    return f"{val * 100:.1f}%"


def _abs_url(base: str, path: str) -> str:
    if not path:
        return base
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return f"{base}{path if path.startswith('/') else '/' + path}"


def _md_label(text: str) -> str:
    """Escape Markdown link-label brackets and collapse newlines."""
    collapsed = " ".join(str(text or "").split())
    return collapsed.replace("[", r"\[").replace("]", r"\]")


def _position_badge(idx: int) -> str:
    if 1 <= idx <= len(_POSITION_BADGES):
        return _POSITION_BADGES[idx - 1]
    return f"**#{idx}**"


def _webhook_create_url(webhook_url: str) -> str:
    """Append wait=true while preserving existing query parameters such as thread_id."""
    parts = urlsplit(webhook_url)
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "wait"]
    params.append(("wait", "true"))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))


def _webhook_edit_url(webhook_url: str, message_id: str) -> str:
    """Build webhook PATCH URL for a message while preserving non-wait query params."""
    parts = urlsplit(webhook_url)
    path = f"{parts.path.rstrip('/')}/messages/{quote(str(message_id), safe='')}"
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "wait"]
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(params), parts.fragment))


def _collect_top_entries(conn, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch public-safe Top N snapshot rows alongside internal stability IDs."""
    tab = spec["tab"]
    if tab == "decks":
        fetch_limit = 25
    elif tab == "rank":
        fetch_limit = 15
    else:
        fetch_limit = DISCORD_FEED_LIMIT

    data = profiles.compute_leaderboard_entries(
        conn,
        tab=tab,
        fmt="all",
        mode="all",
        window="30d",
        limit=fetch_limit,
        include_owner=(tab != "decks"),
    )
    rows = data["entries"]
    if tab == "decks":
        qualified = [
            d
            for d in rows
            if int(d.get("display_games") or 0) >= profiles.MIN_DECK_WINRATE_GAMES
            and str(d.get("id") or "").strip()
        ][:DISCORD_FEED_LIMIT]
        return [
            {
                "id": str(d["id"]),
                "name": str(d.get("name") or "Unnamed Deck"),
                "slug": str(d.get("slug") or ""),
                "format": str(d.get("format") or "standard"),
                "author_name": str(d.get("author_name") or ""),
                "author_profile_url": str(d.get("author_profile_url") or ""),
                "author_is_patron": bool(d.get("author_is_patron")),
                "is_source": bool(d.get("is_source")),
                "wins": int(d.get("display_wins") or 0),
                "losses": int(d.get("display_losses") or 0),
                "games": int(d.get("display_games") or 0),
                "win_rate": d.get("display_win_rate"),
            }
            for d in qualified
        ]

    filtered = [
        p
        for p in rows
        if str(p.get("owner") or "").strip() and (tab != "rank" or float(p.get("rank_score") or 0.0) > 0.0)
    ][:DISCORD_FEED_LIMIT]
    return [
        {
            "id": str(p.get("owner") or ""),
            "handle": str(p.get("handle") or ""),
            "profile_url": str(p.get("profile_url") or ""),
            "is_patron": bool(p.get("is_patron")),
            "rank_label": str(p.get("rank_label") or "Unranked"),
            "wins": int(p.get("wins") or 0),
            "losses": int(p.get("losses") or 0),
            "games": int(p.get("games") or 0),
            "win_rate": p.get("win_rate"),
            "top_deck": str(p.get("top_deck") or ""),
            "top_deck_slug": str(p.get("top_deck_slug") or ""),
        }
        for p in filtered
    ]


def _format_player_line(
    spec: dict[str, Any],
    idx: int,
    item: dict[str, Any],
    base_url: str,
) -> str:
    badge = _position_badge(idx)
    handle = _md_label(item.get("handle") or "")
    p_url = _abs_url(base_url, item.get("profile_url") or spec["footer_path"])
    patron_badge = " ★" if item.get("is_patron") else ""
    wr = _pct_str(item.get("win_rate"))
    record = f"{item['wins']}W-{item['losses']}L"
    rank_label = str(item.get("rank_label") or "Unranked")

    top_deck_name = str(item.get("top_deck") or "").strip()
    top_deck_slug = str(item.get("top_deck_slug") or "").strip()
    deck_part = ""
    if top_deck_name and top_deck_slug:
        d_url = _abs_url(base_url, f"/decks/{quote(top_deck_slug, safe='')}")
        deck_part = f" · [{_md_label(top_deck_name)}]({d_url})"
    elif top_deck_name:
        deck_part = f" · {_md_label(top_deck_name)}"

    if spec["key"] == "rank":
        return f"{badge} [{handle}]({p_url}){patron_badge} — **{rank_label}** · {wr} ({record}){deck_part}"

    rank_part = f" · {rank_label}" if rank_label and rank_label != "Unranked" else ""
    return f"{badge} [{handle}]({p_url}){patron_badge} — **{wr}** ({record}){rank_part}{deck_part}"


def _format_deck_line(
    spec: dict[str, Any],
    idx: int,
    item: dict[str, Any],
    base_url: str,
) -> str:
    badge = _position_badge(idx)
    name = _md_label(item.get("name") or "Unnamed Deck")
    slug = str(item.get("slug") or "").strip()
    deck_url = (
        _abs_url(base_url, f"/decks/{quote(slug, safe='')}")
        if slug
        else _abs_url(base_url, spec["footer_path"])
    )
    fmt_label = (item.get("format") or "standard").title()
    wr = _pct_str(item.get("win_rate"))
    record = f"{item['wins']}W-{item['losses']}L"
    author_name = str(item.get("author_name") or "").strip()
    author_url = str(item.get("author_profile_url") or "").strip()
    author_badge = " ★" if item.get("author_is_patron") and not item.get("is_source") else ""
    if author_url and not item.get("is_source"):
        by_part = f" · by [{_md_label(author_name)}]({_abs_url(base_url, author_url)}){author_badge}"
    elif author_name:
        by_part = f" · by {_md_label(author_name)}{author_badge}"
    else:
        by_part = ""
    return f"{badge} [{name}]({deck_url}) *({fmt_label})* — **{wr}** ({record}){by_part}"


def _build_section_embed(
    spec: dict[str, Any],
    items: list[dict[str, Any]],
    base_url: str,
) -> dict[str, Any]:
    view_url = _abs_url(base_url, spec["footer_path"])
    lines = [
        (
            _format_deck_line(spec, idx, item, base_url)
            if spec["kind"] == "deck"
            else _format_player_line(spec, idx, item, base_url)
        )
        for idx, item in enumerate(items, start=1)
    ]
    lines.append("")
    lines.append(f"[View full tab →]({view_url})")
    return {
        "title": spec["title"],
        "url": view_url,
        "description": "\n".join(lines)[:4000],
        "color": spec["color"],
        "footer": {"text": spec["footer_text"]},
    }


def _build_live_board_payload(
    snapshots: dict[str, list[dict[str, Any]]],
    base_url: str,
    *,
    updated_at: float,
    for_edit: bool,
) -> dict[str, Any]:
    embeds = [
        _build_section_embed(spec, items, base_url)
        for spec in _FEED_SPECS
        if (items := snapshots.get(spec["key"]) or [])
    ]
    payload: dict[str, Any] = {
        "content": (
            f"**MTGA Coach Live Leaderboard** · Updated <t:{int(updated_at)}:R> · ★ = Mythic Patron · "
            f"[Full Leaderboard]({_abs_url(base_url, '/leaderboard')})"
        ),
        "allowed_mentions": {"parse": []},
        "embeds": embeds,
    }
    if not for_edit:
        payload["username"] = "MTGA Coach Leaderboard"
    return payload


async def _upsert_live_board_message(
    webhook_url: str,
    message_id: str,
    create_payload: dict[str, Any],
    edit_payload: dict[str, Any],
    client: httpx.AsyncClient,
) -> tuple[str, str]:
    """Edit the existing live board message in place, creating or self-healing on 404."""
    if message_id:
        edit_url = _webhook_edit_url(webhook_url, message_id)
        try:
            resp = await client.patch(
                edit_url,
                json=edit_payload,
                timeout=DISCORD_FEED_POST_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning(
                "Discord webhook PATCH failed: %s",
                _redact_secret(str(exc), webhook_url),
            )
            return ("error", message_id)
        if 200 <= resp.status_code < 300:
            return ("edited", message_id)
        if resp.status_code == 404:
            logger.info(
                "Discord live leaderboard message %s not found (404); recreating",
                message_id,
            )
        else:
            logger.warning(
                "Discord webhook PATCH returned non-success status=%s",
                resp.status_code,
            )
            return ("error", message_id)

    create_url = _webhook_create_url(webhook_url)
    try:
        resp = await client.post(
            create_url,
            json=create_payload,
            timeout=DISCORD_FEED_POST_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning(
            "Discord webhook POST failed: %s",
            _redact_secret(str(exc), webhook_url),
        )
        return ("error", "")
    if 200 <= resp.status_code < 300:
        try:
            body = resp.json()
        except Exception:
            body = None
        new_msg_id = str((body or {}).get("id") or "").strip() if isinstance(body, dict) else ""
        if not new_msg_id:
            logger.warning("Discord webhook POST ?wait=true returned no message id")
            return ("error", "")
        return ("created", new_msg_id)
    logger.warning("Discord webhook POST returned non-success status=%s", resp.status_code)
    return ("error", "")


async def check_and_post_updates(
    *,
    force_post: bool = False,
    http_client: Optional[httpx.AsyncClient] = None,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Sync the single live Discord leaderboard message in place when standings change."""
    webhook_url = state.get_discord_webhook_url()
    if not webhook_url:
        return {"ok": True, "skipped": "no_webhook", "results": {}}

    init_db()
    ts = float(time.time() if now is None else now)
    cooldown_s = _cooldown_seconds()
    base_url = _public_base_url()

    async with _get_check_lock():
        with state._get_db().get_db() as conn:
            snapshots = {spec["key"]: _collect_top_entries(conn, spec) for spec in _FEED_SPECS}
            prev_row = conn.execute(
                "SELECT * FROM discord_feed_state WHERE feed_key = ?",
                (LIVE_BOARD_KEY,),
            ).fetchone()

        if not any(snapshots.values()):
            return {"ok": True, "results": {LIVE_BOARD_KEY: "empty"}, "checked_at": ts}

        existing_message_id = str(prev_row["message_id"] or "").strip() if prev_row else ""
        snapshot_json = json.dumps(snapshots, sort_keys=True)
        ids_json = json.dumps(
            {k: [it["id"] for it in v] for k, v in snapshots.items()},
            sort_keys=True,
        )

        if existing_message_id and prev_row is not None:
            prev_snapshot_json = str(prev_row["last_snapshot_json"] or "")
            last_posted_at = float(prev_row["last_posted_at"] or 0.0)
            if not force_post and snapshot_json == prev_snapshot_json:
                return {"ok": True, "results": {LIVE_BOARD_KEY: "unchanged"}, "checked_at": ts}
            if not force_post and last_posted_at > 0 and (ts - last_posted_at) < cooldown_s:
                return {"ok": True, "results": {LIVE_BOARD_KEY: "cooldown"}, "checked_at": ts}

        create_payload = _build_live_board_payload(snapshots, base_url, updated_at=ts, for_edit=False)
        edit_payload = _build_live_board_payload(snapshots, base_url, updated_at=ts, for_edit=True)

        owns_client = http_client is None
        client = http_client or state.http_client
        close_temp = False
        if client is None:
            client = httpx.AsyncClient(timeout=DISCORD_FEED_POST_TIMEOUT_SECONDS)
            close_temp = True

        try:
            action, new_msg_id = await _upsert_live_board_message(
                webhook_url,
                existing_message_id,
                create_payload,
                edit_payload,
                client,
            )
        finally:
            if owns_client and close_temp:
                await client.aclose()

        if action == "error" or not new_msg_id:
            return {"ok": True, "results": {LIVE_BOARD_KEY: "error"}, "checked_at": ts}

        with state._get_db().get_db() as conn:
            conn.execute(
                """
                INSERT INTO discord_feed_state
                    (feed_key, last_ids_json, last_snapshot_json, last_posted_at, updated_at, message_id)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(feed_key) DO UPDATE SET
                    last_ids_json = excluded.last_ids_json,
                    last_snapshot_json = excluded.last_snapshot_json,
                    last_posted_at = excluded.last_posted_at,
                    updated_at = excluded.updated_at,
                    message_id = excluded.message_id
                """,
                (LIVE_BOARD_KEY, ids_json, snapshot_json, ts, ts, new_msg_id),
            )
        logger.info("Synced Discord live leaderboard action=%s message_id=%s", action, new_msg_id)
        return {"ok": True, "results": {LIVE_BOARD_KEY: action}, "checked_at": ts}


async def _debounced_dirty_check() -> None:
    global _dirty_pending, _dirty_task
    try:
        while True:
            _dirty_pending = False
            await asyncio.sleep(DISCORD_FEED_DEBOUNCE_SECONDS)
            await check_and_post_updates()
            if not _dirty_pending:
                break
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Debounced Discord leaderboard check failed: %s", exc)
    finally:
        _dirty_pending = False
        _dirty_task = None


async def shutdown() -> None:
    """Cancel any pending debounced Discord feed task during server teardown."""
    global _dirty_pending, _dirty_task
    _dirty_pending = False
    task = _dirty_task
    _dirty_task = None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def notify_leaderboard_dirty() -> None:
    """Coalesce mutation notifications into a background Top N live board check when active."""
    global _dirty_pending, _dirty_task
    if not _is_feed_enabled() or not state.get_discord_webhook_url():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _dirty_pending = True
    if _dirty_task is not None and not _dirty_task.done():
        return
    _dirty_task = loop.create_task(_debounced_dirty_check())


@router.post("/admin/discord-feed/check")
async def admin_check_discord_feed(
    force_post: bool = Query(default=False),
    _admin: str = Depends(state._require_admin),
):
    res = await check_and_post_updates(force_post=force_post)
    return JSONResponse(res)
