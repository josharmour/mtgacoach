"""Cloud profile, deck sync, wildcard planner, and leaderboard client for mtgacoach.com."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from arenamcp.client_metadata import get_client_headers

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_S = 12.0
RARITIES = ("common", "uncommon", "rare", "mythic")

Opener = Callable[[str, str, bytes | None, dict[str, str]], tuple[int, str]]


def _website_base() -> str:
    override = os.environ.get("MTGACOACH_WEBSITE_URL", "").strip()
    if override:
        return override.rstrip("/")
    try:
        from arenamcp.subscription import WEBSITE_BASE

        return str(WEBSITE_BASE).rstrip("/")
    except Exception:
        return "https://mtgacoach.com"


def _license_key() -> str:
    try:
        from arenamcp.settings import get_settings

        val = get_settings().get("license_key", "")
        return str(val).strip() if val else ""
    except Exception:
        return ""


def is_cloud_sync_enabled() -> bool:
    try:
        from arenamcp.settings import get_settings

        return bool(get_settings().get("cloud_sync_enabled", True))
    except Exception:
        return True


def _default_opener(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            return int(resp.status), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        try:
            text = err.read().decode("utf-8", "replace")
        except Exception:
            text = ""
        return int(err.code), text
    except Exception as exc:
        logger.debug("cloud_sync HTTP error (%s %s): %s", method, url, exc)
        return 0, f"{type(exc).__name__}: {exc}"


def _api_call(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    license_key: str | None = None,
    base_url: str | None = None,
    opener: Opener | None = None,
    require_key: bool = True,
) -> tuple[int, dict[str, Any]]:
    key = (license_key if license_key is not None else _license_key()).strip()
    if require_key and not key:
        return 401, {"ok": False, "error": "No license or trial key configured."}
    base = (base_url or _website_base()).rstrip("/")
    headers = {"Accept": "application/json"}
    headers.update(get_client_headers())
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    fn = opener or _default_opener
    status, text = fn(method, f"{base}{path}", body, headers)
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        data = {"detail": text[:200]} if text else {}
    if not isinstance(data, dict):
        data = {"data": data}
    return status, data


# ---------------------------------------------------------------------------
# Pure deck formatting & local grpId resolution
# ---------------------------------------------------------------------------


def build_arena_export_text(deck: dict[str, Any]) -> str:
    """Render a synced deck dict in MTG Arena clipboard import format."""
    existing = str(deck.get("export_text") or "").strip()
    if existing:
        return existing
    sections = (
        ("Commander", deck.get("commander") or []),
        ("Companion", deck.get("companion") or []),
        ("Deck", deck.get("main") or []),
        ("Sideboard", deck.get("sideboard") or []),
    )
    blocks: list[str] = []
    for label, cards in sections:
        lines: list[str] = []
        for entry in cards:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            count = int(entry.get("count") or 0)
            if name and count > 0:
                lines.append(f"{count} {name}")
        if lines:
            blocks.append(label + "\n" + "\n".join(lines))
    return "\n\n".join(blocks)


def _load_card_db():
    try:
        from arenamcp.card_db import get_card_database

        return get_card_database()
    except Exception:
        return None


def resolve_deck_piles_to_grp_ids(
    deck: dict[str, Any],
    collection: dict[int, int] | None = None,
    database: Any = None,
) -> tuple[dict[str, list[dict[str, int]]], list[str]]:
    """Resolve synced deck card entries to bridge ``{grp_id, count}`` piles.

    Prefers printings the player already owns in ``collection`` so bridge-built
    decks avoid unnecessary wildcard deficits when another printing is owned.
    Returns ``(piles, unresolved_names)``.
    """
    db = database if database is not None else _load_card_db()
    owned_by_name: dict[str, list[tuple[int, int]]] = {}
    if db is not None and collection:
        for gid, qty in collection.items():
            try:
                g = int(gid)
                q = int(qty)
            except (TypeError, ValueError):
                continue
            if g <= 0 or q <= 0:
                continue
            info = db.get_card_by_arena_id(g)
            cname = str(getattr(info, "name", "") or "").strip()
            if cname:
                owned_by_name.setdefault(cname.casefold(), []).append((g, q))

    pile_map = {
        "main": "main",
        "sideboard": "sideboard",
        "commander": "command_zone",
        "command_zone": "command_zone",
        "companion": "companions",
        "companions": "companions",
    }
    out: dict[str, list[dict[str, int]]] = {
        "main": [],
        "sideboard": [],
        "command_zone": [],
        "companions": [],
    }
    unresolved: list[str] = []

    for src_pile, dst_pile in pile_map.items():
        entries = deck.get(src_pile) or []
        if not entries:
            continue
        merged: dict[int, int] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            count = int(entry.get("count") or 0)
            if count <= 0:
                continue
            name = str(entry.get("name") or "").strip()
            gid = int(entry.get("grp_id") or 0) if str(entry.get("grp_id") or "").isdigit() else 0
            # Prefer owned printing if one covers (or exists for) this card name.
            if name and owned_by_name.get(name.casefold()):
                owned_candidates = sorted(
                    owned_by_name[name.casefold()], key=lambda pair: (-pair[1], pair[0])
                )
                gid = owned_candidates[0][0]
            if gid <= 0 and name and db is not None:
                info = db.get_card_by_name(name)
                gid = int(getattr(info, "arena_id", 0) or 0) if info else 0
            if gid <= 0:
                if name and name not in unresolved:
                    unresolved.append(name)
                continue
            merged[gid] = merged.get(gid, 0) + count
        for gid, count in sorted(merged.items()):
            out[dst_pile].append({"grp_id": gid, "count": count})

    return out, unresolved


# ---------------------------------------------------------------------------
# Sync payload builders & API calls
# ---------------------------------------------------------------------------


def _serialize_match(rec: Any) -> dict[str, Any]:
    if isinstance(rec, dict):
        return dict(rec)
    return {
        "match_id": getattr(rec, "match_id", ""),
        "played_at": getattr(rec, "timestamp", ""),
        "result": getattr(rec, "result", ""),
        "format_name": getattr(rec, "format_name", ""),
        "deck_identity": getattr(rec, "deck_identity", ""),
        "deck_name": getattr(rec, "deck_name", ""),
        "deck_colors": "".join(getattr(rec, "local_deck_colors", None) or []),
        "opponent_colors": "".join(getattr(rec, "opponent_colors_seen", None) or []),
        "turns": int(getattr(rec, "turns", 0) or 0),
        "autopilot": getattr(rec, "autopilot", None),
        "rank_class": "",
        "rank_tier": int(getattr(rec, "local_rank_tier", 0) or 0),
    }


def _serialize_collection_and_catalog(
    collection_payload: dict[str, Any], database: Any = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    db = database if database is not None else _load_card_db()
    raw_cards = collection_payload.get("cards") or {}
    cards_by_name: dict[str, int] = {}
    catalog_entries: list[dict[str, Any]] = []
    seen_catalog: set[str] = set()

    for raw_gid, raw_qty in raw_cards.items():
        try:
            gid = int(raw_gid)
            qty = int(raw_qty)
        except (TypeError, ValueError):
            continue
        if gid <= 0 or qty <= 0:
            continue
        info = db.get_card_by_arena_id(gid) if db is not None else None
        name = str(getattr(info, "name", "") or "").strip()
        if not name:
            continue
        cards_by_name[name] = cards_by_name.get(name, 0) + qty
        k = name.casefold()
        if k not in seen_catalog:
            seen_catalog.add(k)
            rarity = str(getattr(info, "rarity", "") or "").lower()
            colors = "".join(getattr(info, "colors", None) or [])
            catalog_entries.append(
                {
                    "name": name,
                    "rarity": rarity if rarity in RARITIES else "",
                    "grp_id": gid,
                    "colors": colors,
                }
            )

    col_out = {
        "cards_by_name": cards_by_name,
        "wildcards": dict(collection_payload.get("wildcards") or {}),
        "gold": int(collection_payload.get("gold") or 0),
        "gems": int(collection_payload.get("gems") or 0),
    }
    return col_out, catalog_entries


def _serialize_decks(decks: Any, database: Any = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    db = database if database is not None else _load_card_db()
    items = list(decks.values()) if isinstance(decks, dict) else list(decks or [])
    out: list[dict[str, Any]] = []
    catalog_entries: list[dict[str, Any]] = []
    seen_catalog: set[str] = set()

    def _convert_pairs(pairs: Any) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for entry in pairs or []:
            if isinstance(entry, (tuple, list)) and len(entry) >= 2:
                gid, count = int(entry[0]), int(entry[1])
            elif isinstance(entry, dict):
                gid = int(entry.get("grp_id") or 0)
                count = int(entry.get("count") or 0)
            else:
                continue
            if gid <= 0 or count <= 0:
                continue
            info = db.get_card_by_arena_id(gid) if db is not None else None
            name = str(getattr(info, "name", "") or f"Card {gid}").strip()
            rarity = str(getattr(info, "rarity", "") or "").lower()
            if rarity not in RARITIES:
                rarity = ""
            converted.append({"name": name, "count": count, "grp_id": gid, "rarity": rarity})
            if info is not None and name.casefold() not in seen_catalog:
                seen_catalog.add(name.casefold())
                catalog_entries.append(
                    {
                        "name": name,
                        "rarity": rarity,
                        "grp_id": gid,
                        "colors": "".join(getattr(info, "colors", None) or []),
                    }
                )
        return converted

    def _convert_singles(singles: Any) -> list[dict[str, Any]]:
        counts: dict[int, int] = {}
        for item in singles or []:
            if isinstance(item, dict):
                gid = int(item.get("grp_id") or 0)
                qty = int(item.get("count") or 1)
            else:
                gid = int(item or 0)
                qty = 1
            if gid > 0 and qty > 0:
                counts[gid] = counts.get(gid, 0) + qty
        return _convert_pairs(list(counts.items()))

    for rec in items:
        if isinstance(rec, dict):
            deck_id = str(rec.get("deck_id") or "")
            name = str(rec.get("name") or "")
            fmt = str(rec.get("format") or "")
            main = rec.get("main") or []
            side = rec.get("sideboard") or []
            cmd = rec.get("command_zone") or []
            comp = rec.get("companions") or []
            ident = str(rec.get("identity") or "")
        else:
            if not getattr(rec, "complete", True) or getattr(rec, "snapshot", False):
                continue
            deck_id = str(getattr(rec, "deck_id", "") or "")
            name = str(getattr(rec, "name", "") or "")
            fmt = str(getattr(rec, "format", "") or "")
            main = getattr(rec, "main", []) or []
            side = getattr(rec, "sideboard", []) or []
            cmd = getattr(rec, "command_zone", []) or []
            comp = getattr(rec, "companions", []) or []
            ident = rec.identity() if callable(getattr(rec, "identity", None)) else ""
        if not name or name.startswith("?=?Loc/") or not main:
            continue
        main_conv = _convert_pairs(main)
        if not main_conv:
            continue
        out.append(
            {
                "arena_deck_id": deck_id,
                "deck_identity": ident,
                "name": name,
                "format": fmt,
                "main": main_conv,
                "sideboard": _convert_pairs(side),
                "commander": _convert_singles(cmd),
                "companion": _convert_singles(comp),
            }
        )
    return out, catalog_entries


def push_sync_payload(
    *,
    matches: list[Any] | None = None,
    ranks: dict[str, Any] | None = None,
    collection_payload: dict[str, Any] | None = None,
    decks: Any = None,
    database: Any = None,
    license_key: str | None = None,
    base_url: str | None = None,
    opener: Opener | None = None,
) -> dict[str, Any]:
    """Synchronously push matches, ranks, collection, and decks to mtgacoach.com."""
    payload: dict[str, Any] = {}
    catalog: list[dict[str, Any]] = []
    if matches:
        payload["matches"] = [_serialize_match(m) for m in matches]
    if ranks:
        payload["ranks"] = ranks
    if collection_payload and collection_payload.get("ok") is True:
        col_data, col_cat = _serialize_collection_and_catalog(collection_payload, database=database)
        payload["collection"] = col_data
        catalog.extend(col_cat)
    if decks:
        deck_list, deck_cat = _serialize_decks(decks, database=database)
        if deck_list:
            payload["decks"] = deck_list
        catalog.extend(deck_cat)
    if catalog:
        deduped: dict[str, dict[str, Any]] = {}
        for item in catalog:
            deduped.setdefault(item["name"].casefold(), item)
        payload["catalog_entries"] = list(deduped.values())[:5000]
    if not payload:
        return {"ok": True, "skipped": True}

    status, data = _api_call(
        "POST",
        "/v1/sync/push",
        payload=payload,
        license_key=license_key,
        base_url=base_url,
        opener=opener,
    )
    if status != 200:
        return {"ok": False, "status_code": status, **data}
    return data


def _latest_rank_snapshot() -> dict[str, Any] | None:
    try:
        from pathlib import Path

        from arenamcp.ladder import RankLog
        from arenamcp.watcher import _default_log_path

        log_path = Path(_default_log_path())
        if not log_path.exists():
            return None
        rlog = RankLog()
        while rlog.read(log_path):
            pass
        return rlog.snapshot or None
    except Exception:
        return None


def sync_match_async(record: Any, rank_snapshot: dict[str, Any] | None = None) -> None:
    """Fire-and-forget upload of a finished match and current rank snapshot."""
    if not is_cloud_sync_enabled() or not _license_key():
        return

    def _work() -> None:
        with contextlib.suppress(Exception):
            ranks = rank_snapshot if rank_snapshot is not None else _latest_rank_snapshot()
            push_sync_payload(matches=[record], ranks=ranks)

    threading.Thread(target=_work, name="cloud-sync-match", daemon=True).start()


def sync_collection_and_decks_async(
    *,
    collection_payload: dict[str, Any] | None = None,
    decks: Any = None,
) -> None:
    """Fire-and-forget upload of owned cards, wildcards, and Arena deck library."""
    if not is_cloud_sync_enabled() or not _license_key():
        return

    def _work() -> None:
        with contextlib.suppress(Exception):
            push_sync_payload(
                collection_payload=collection_payload,
                decks=decks,
                ranks=_latest_rank_snapshot(),
            )

    threading.Thread(target=_work, name="cloud-sync-library", daemon=True).start()


_STARTUP_BACKFILL_LOCK = threading.Lock()
_STARTUP_BACKFILL_RAN = False


def backfill_from_local(
    *,
    limit_matches: int = 500,
    license_key: str | None = None,
    base_url: str | None = None,
    opener: Opener | None = None,
) -> dict[str, Any]:
    """Push local MatchHistory, latest RankLog snapshot, and Player.log decks."""
    matches: list[Any] = []
    with contextlib.suppress(Exception):
        from arenamcp.match_history import MatchHistory

        history = MatchHistory()
        matches = history.get_recent(limit_matches)

    decks: Any = None
    with contextlib.suppress(Exception):
        from arenamcp.deck_library import load_log_decks

        decks = load_log_decks()

    ranks = _latest_rank_snapshot()
    if not matches and not decks and not ranks:
        return {"ok": True, "skipped": True}
    return push_sync_payload(
        matches=matches or None,
        ranks=ranks,
        decks=decks or None,
        license_key=license_key,
        base_url=base_url,
        opener=opener,
    )


def backfill_on_startup_async(delay_s: float = 4.0) -> None:
    """Once per process, push local history/ranks/decks after startup settles."""
    global _STARTUP_BACKFILL_RAN
    if not is_cloud_sync_enabled() or not _license_key():
        return
    with _STARTUP_BACKFILL_LOCK:
        if _STARTUP_BACKFILL_RAN:
            return
        _STARTUP_BACKFILL_RAN = True

    def _work() -> None:
        if delay_s > 0:
            time.sleep(delay_s)
        try:
            res = backfill_from_local()
            logger.info("cloud_sync startup backfill: %s", res)
        except Exception as exc:
            logger.warning("cloud_sync startup backfill failed: %s", exc)

    threading.Thread(target=_work, name="cloud-sync-backfill", daemon=True).start()


# ---------------------------------------------------------------------------
# Desktop UI read/write endpoints
# ---------------------------------------------------------------------------


def create_profile_login_url(
    *,
    section: str = "",
    license_key: str | None = None,
    base_url: str | None = None,
    opener: Opener | None = None,
) -> dict[str, Any]:
    body = {"section": section} if section else {}
    status, data = _api_call(
        "POST",
        "/v1/profile/login-token",
        payload=body,
        license_key=license_key,
        base_url=base_url,
        opener=opener,
    )
    if status != 200:
        return {"ok": False, "error": data.get("detail") or data.get("error") or f"HTTP {status}"}
    return data


def fetch_profile_me(
    *,
    license_key: str | None = None,
    base_url: str | None = None,
    opener: Opener | None = None,
) -> dict[str, Any]:
    status, data = _api_call(
        "GET",
        "/v1/profile/me",
        license_key=license_key,
        base_url=base_url,
        opener=opener,
    )
    if status != 200:
        return {"ok": False, "error": data.get("detail") or data.get("error") or f"HTTP {status}"}
    return {"ok": True, **data}


def update_profile_settings(
    *,
    display_name: str | None = None,
    leaderboard_opt_in: bool | None = None,
    discord_handle: str | None = None,
    license_key: str | None = None,
    base_url: str | None = None,
    opener: Opener | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if display_name is not None:
        payload["display_name"] = display_name
    if leaderboard_opt_in is not None:
        payload["leaderboard_opt_in"] = bool(leaderboard_opt_in)
    if discord_handle is not None:
        payload["discord_handle"] = discord_handle
    status, data = _api_call(
        "PATCH",
        "/v1/profile/settings",
        payload=payload,
        license_key=license_key,
        base_url=base_url,
        opener=opener,
    )
    if status != 200:
        return {"ok": False, "error": data.get("detail") or data.get("error") or f"HTTP {status}"}
    return data


def fetch_leaderboard(
    *,
    tab: str = "rank",
    fmt: str = "all",
    mode: str = "all",
    window: str = "30d",
    license_key: str | None = None,
    base_url: str | None = None,
    opener: Opener | None = None,
) -> dict[str, Any]:
    qs = urllib.parse.urlencode({"tab": tab, "format": fmt, "mode": mode, "window": window})
    status, data = _api_call(
        "GET",
        f"/v1/leaderboard?{qs}",
        license_key=license_key,
        base_url=base_url,
        opener=opener,
        require_key=False,
    )
    if status != 200:
        return {"ok": False, "error": data.get("detail") or data.get("error") or f"HTTP {status}"}
    return data


def fetch_discover_decks(
    *,
    fmt: str = "all",
    craftability: str = "all",
    sort: str = "winrate",
    q: str = "",
    license_key: str | None = None,
    base_url: str | None = None,
    opener: Opener | None = None,
) -> dict[str, Any]:
    qs = urllib.parse.urlencode({"format": fmt, "craftability": craftability, "sort": sort, "q": q})
    status, data = _api_call(
        "GET",
        f"/v1/decks/discover?{qs}",
        license_key=license_key,
        base_url=base_url,
        opener=opener,
        require_key=False,
    )
    if status != 200:
        return {"ok": False, "error": data.get("detail") or data.get("error") or f"HTTP {status}"}
    return data


def fetch_pending_arena_decks(
    *,
    license_key: str | None = None,
    base_url: str | None = None,
    opener: Opener | None = None,
) -> list[dict[str, Any]]:
    status, data = _api_call(
        "GET",
        "/v1/sync/pending-decks",
        license_key=license_key,
        base_url=base_url,
        opener=opener,
    )
    if status != 200:
        return []
    decks = data.get("decks")
    return list(decks) if isinstance(decks, list) else []


def ack_arena_deck_sync(
    deck_id: str,
    sync_status: str,
    *,
    arena_deck_id: str = "",
    error: str = "",
    license_key: str | None = None,
    base_url: str | None = None,
    opener: Opener | None = None,
) -> dict[str, Any]:
    if not deck_id:
        return {"ok": False, "error": "missing deck_id"}
    status, data = _api_call(
        "POST",
        f"/v1/sync/pending-decks/{urllib.parse.quote(deck_id, safe='')}/ack",
        payload={"status": sync_status, "arena_deck_id": arena_deck_id, "error": error},
        license_key=license_key,
        base_url=base_url,
        opener=opener,
    )
    if status != 200:
        return {"ok": False, "error": data.get("detail") or data.get("error") or f"HTTP {status}"}
    return data
