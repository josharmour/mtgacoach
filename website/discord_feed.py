"""Automatic Discord webhook announcements for Top 3 leaderboard and highest win-rate deck changes."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Optional
from urllib.parse import quote
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
DISCORD_FEED_LIMIT = 3

_WEBHOOK_SECRET_RE = re.compile(r"https://discord(?:app)?\.com/api/webhooks/[^\s\"'<>]+")

_FEED_SPECS: tuple[dict[str, Any], ...] = (
    {
        "key": "rank_top3",
        "tab": "rank",
        "kind": "player",
        "title": "Leaderboard Top 3 Switched — Highest Rank",
        "color": 0xD4A13C,
        "footer_path": "/leaderboard?tab=rank",
    },
    {
        "key": "winrate_top3",
        "tab": "winrate",
        "kind": "player",
        "title": "Leaderboard Top 3 Switched — Highest Win Rate (30d)",
        "color": 0x4EA1D3,
        "footer_path": "/leaderboard?tab=winrate",
    },
    {
        "key": "decks_top3",
        "tab": "decks",
        "kind": "deck",
        "title": "Highest Win-Rate Decks Updated",
        "color": 0x57B874,
        "footer_path": "/leaderboard?tab=decks",
    },
)

_check_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = WeakKeyDictionary()
_dirty_task: Optional[asyncio.Task[None]] = None
_dirty_pending = False


def init_db() -> None:
    """Ensure Discord feed state tracking table exists."""
    with state._get_db().get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS discord_feed_state (
                feed_key TEXT PRIMARY KEY,
                last_ids_json TEXT NOT NULL DEFAULT '[]',
                last_snapshot_json TEXT NOT NULL DEFAULT '[]',
                last_posted_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0
            );
            """
        )


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


def _collect_top3(conn, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch public-safe Top 3 snapshot rows alongside internal stability IDs."""
    tab = spec["tab"]
    fetch_limit = 15 if tab == "decks" else DISCORD_FEED_LIMIT
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
                "is_source": bool(d.get("is_source")),
                "wins": int(d.get("display_wins") or 0),
                "losses": int(d.get("display_losses") or 0),
                "games": int(d.get("display_games") or 0),
                "win_rate": d.get("display_win_rate"),
            }
            for d in qualified
        ]

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
        for p in rows[:DISCORD_FEED_LIMIT]
        if str(p.get("owner") or "").strip()
    ]


def _movement_badge(item_id: str, new_pos: int, prev_pos_by_id: dict[str, int]) -> str:
    old_pos = prev_pos_by_id.get(item_id)
    if old_pos is None:
        return " ★ NEW"
    if new_pos < old_pos:
        return f" ▲ from #{old_pos}"
    if new_pos > old_pos:
        return f" ▼ from #{old_pos}"
    return ""


def _headline_for_change(
    spec: dict[str, Any],
    items: list[dict[str, Any]],
    prev_ids: list[str],
    prev_pos_by_id: dict[str, int],
) -> str:
    if not items:
        return ""
    first = items[0]
    first_label = first["name"] if spec["kind"] == "deck" else first["handle"]
    if not prev_ids or first["id"] != prev_ids[0]:
        if spec["kind"] == "deck":
            return f"**{first_label}** climbed to **#1** highest win-rate deck!"
        return f"**{first_label}** took over **#1**!"
    for idx, item in enumerate(items[1:], start=2):
        old_pos = prev_pos_by_id.get(item["id"])
        label = item["name"] if spec["kind"] == "deck" else item["handle"]
        if old_pos is None:
            return f"**{label}** entered the Top 3 at **#{idx}**!"
        if idx != old_pos:
            return f"**{label}** moved to **#{idx}**!"
    return "Top 3 standings updated."


def _build_webhook_payload(
    spec: dict[str, Any],
    items: list[dict[str, Any]],
    prev_ids: list[str],
    base_url: str,
) -> dict[str, Any]:
    prev_pos_by_id = {item_id: idx for idx, item_id in enumerate(prev_ids, start=1)}
    headline = _headline_for_change(spec, items, prev_ids, prev_pos_by_id)
    lines: list[str] = []
    if headline:
        lines.append(headline)
        lines.append("")

    for idx, item in enumerate(items, start=1):
        badge = _movement_badge(item["id"], idx, prev_pos_by_id)
        badge_suffix = f" *({badge.strip()})*" if badge else ""
        if spec["kind"] == "deck":
            slug = item.get("slug") or ""
            deck_url = (
                _abs_url(base_url, f"/decks/{quote(slug, safe='')}")
                if slug
                else _abs_url(base_url, spec["footer_path"])
            )
            fmt_label = (item.get("format") or "standard").title()
            wr = _pct_str(item.get("win_rate"))
            record = f"{item['wins']}W-{item['losses']}L"
            author_name = item.get("author_name") or ""
            author_url = item.get("author_profile_url") or ""
            if author_url and not item.get("is_source"):
                by_part = f" · by [{author_name}]({_abs_url(base_url, author_url)})"
            elif author_name:
                by_part = f" · by {author_name}"
            else:
                by_part = ""
            lines.append(
                f"**#{idx}** · [{item['name']}]({deck_url}) *({fmt_label})* — **{wr}** ({record}){by_part}{badge_suffix}"
            )
        else:
            p_url = _abs_url(base_url, item.get("profile_url") or spec["footer_path"])
            wr = _pct_str(item.get("win_rate"))
            record = f"{item['wins']}W-{item['losses']}L"
            top_deck_name = item.get("top_deck") or ""
            top_deck_slug = item.get("top_deck_slug") or ""
            deck_part = ""
            if top_deck_name and top_deck_slug:
                d_url = _abs_url(base_url, f"/decks/{quote(top_deck_slug, safe='')}")
                deck_part = f" · [{top_deck_name}]({d_url})"
            elif top_deck_name:
                deck_part = f" · {top_deck_name}"
            lines.append(
                f"**#{idx}** · [{item['handle']}]({p_url}) — {item['rank_label']} · **{wr}** ({record}){deck_part}{badge_suffix}"
            )

    view_url = _abs_url(base_url, spec["footer_path"])
    lines.append("")
    lines.append(f"[View full leaderboard →]({view_url})")

    return {
        "username": "MTGA Coach Leaderboard",
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": spec["title"],
                "url": view_url,
                "description": "\n".join(lines)[:4000],
                "color": spec["color"],
                "footer": {"text": "mtgacoach.com · Last 30 days"},
            }
        ],
    }


async def _post_webhook(
    webhook_url: str,
    payload: dict[str, Any],
    client: httpx.AsyncClient,
) -> bool:
    try:
        resp = await client.post(
            webhook_url,
            json=payload,
            timeout=DISCORD_FEED_POST_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning("Discord webhook request failed: %s", _redact_secret(str(exc), webhook_url))
        return False
    if 200 <= resp.status_code < 300:
        return True
    logger.warning("Discord webhook returned non-success status=%s", resp.status_code)
    return False


async def check_and_post_updates(
    *,
    force_post: bool = False,
    http_client: Optional[httpx.AsyncClient] = None,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Compare current Top 3 snapshots against persisted state and post switches to Discord."""
    webhook_url = state.get_discord_webhook_url()
    if not webhook_url:
        return {"ok": True, "skipped": "no_webhook", "results": {}}

    init_db()
    ts = float(time.time() if now is None else now)
    cooldown_s = _cooldown_seconds()
    base_url = _public_base_url()

    async with _get_check_lock():
        with state._get_db().get_db() as conn:
            snapshots = {spec["key"]: _collect_top3(conn, spec) for spec in _FEED_SPECS}
            existing_rows = {
                r["feed_key"]: dict(r)
                for r in conn.execute("SELECT * FROM discord_feed_state").fetchall()
            }

        results: dict[str, str] = {}
        owns_client = http_client is None
        client = http_client or state.http_client
        close_temp = False
        if client is None:
            client = httpx.AsyncClient(timeout=DISCORD_FEED_POST_TIMEOUT_SECONDS)
            close_temp = True

        try:
            for spec in _FEED_SPECS:
                key = spec["key"]
                items = snapshots.get(key) or []
                current_ids = [it["id"] for it in items]
                if not current_ids:
                    results[key] = "empty"
                    continue

                prev_row = existing_rows.get(key)
                if prev_row is None:
                    if not force_post:
                        with state._get_db().get_db() as conn:
                            conn.execute(
                                """
                                INSERT INTO discord_feed_state
                                    (feed_key, last_ids_json, last_snapshot_json, last_posted_at, updated_at)
                                VALUES (?, ?, ?, ?, ?)
                                """,
                                (key, json.dumps(current_ids), json.dumps(items), 0.0, ts),
                            )
                        results[key] = "seeded"
                        continue
                    prev_ids: list[str] = []
                    last_posted_at = 0.0
                else:
                    try:
                        prev_ids = json.loads(prev_row.get("last_ids_json") or "[]")
                    except (json.JSONDecodeError, TypeError):
                        prev_ids = []
                    last_posted_at = float(prev_row.get("last_posted_at") or 0.0)

                if not force_post and current_ids == prev_ids:
                    results[key] = "unchanged"
                    continue

                if not force_post and last_posted_at > 0 and (ts - last_posted_at) < cooldown_s:
                    results[key] = "cooldown"
                    continue

                payload = _build_webhook_payload(spec, items, prev_ids, base_url)
                posted = await _post_webhook(webhook_url, payload, client)
                if not posted:
                    results[key] = "error"
                    continue

                with state._get_db().get_db() as conn:
                    conn.execute(
                        """
                        INSERT INTO discord_feed_state
                            (feed_key, last_ids_json, last_snapshot_json, last_posted_at, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(feed_key) DO UPDATE SET
                            last_ids_json = excluded.last_ids_json,
                            last_snapshot_json = excluded.last_snapshot_json,
                            last_posted_at = excluded.last_posted_at,
                            updated_at = excluded.updated_at
                        """,
                        (key, json.dumps(current_ids), json.dumps(items), ts, ts),
                    )
                results[key] = "posted"
                logger.info("Posted Discord leaderboard update feed=%s", key)
        finally:
            if owns_client and close_temp:
                await client.aclose()

    return {"ok": True, "results": results, "checked_at": ts}


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
    """Coalesce mutation notifications into a background Top 3 check when active."""
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
