"""Auth-enabled mtgacoach.com profiles, server-side deck sync, wildcard planner, and leaderboard."""

from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote, urlparse

import match_bundles
import state
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from state import _extract_license_key, _get_db, get_discord_invite_url

logger = logging.getLogger("website.profiles")

router = APIRouter()

RARITIES = ("common", "uncommon", "rare", "mythic")
BASIC_LANDS = frozenset({"Plains", "Island", "Swamp", "Mountain", "Forest"})
FORMAT_BUCKETS = ("standard", "brawl", "historic", "alchemy", "limited", "other")
ALLOWED_DECK_FORMATS = frozenset({"standard", "brawl", "historic", "alchemy", "explorer", "timeless", "limited"})
MIN_WINRATE_GAMES = 20
MIN_DECK_WINRATE_GAMES = 10
LOGIN_TOKEN_TTL_S = 300
SESSION_TTL_S = 14 * 86400
MAX_MATCHES_PER_SYNC = 500
MAX_COLLECTION_CARDS = 30000
MAX_CATALOG_ENTRIES = 5000
MAX_DECKS_PER_SYNC = 300
MAX_SYNC_BODY_BYTES = 8 * 1024 * 1024

_DISPLAY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{1,31}$")
_DISCORD_HANDLE_RE = re.compile(r"^[A-Za-z0-9._#@-]{0,40}$")
_DECK_LINE_RE = re.compile(r"^\s*(\d+)\s+(.+?)\s*$")
_SET_SUFFIX_RE = re.compile(r"\s+\([A-Za-z0-9]{2,8}\)\s+[A-Za-z0-9-]+\s*$")

_RANK_ORDER = {
    "bronze": 100.0,
    "silver": 200.0,
    "gold": 300.0,
    "platinum": 400.0,
    "diamond": 500.0,
    "mythic": 1000.0,
}

_SECTION_HEADERS = {
    "deck": "main",
    "main": "main",
    "sideboard": "sideboard",
    "commander": "commander",
    "companion": "companion",
}

PATRON_DISCORD_ROOMS = (
    {"channel": "#patron-lounge", "topic": "Direct chat with the dev team and active patrons"},
    {"channel": "#winning-decks-and-meta", "topic": "High-winrate lists, sideboard notes, and meta calls"},
    {"channel": "#autopilot-tuning", "topic": "Share deck playbooks, guard configs, and match logs"},
    {"channel": "#feature-requests", "topic": "Vote on upcoming Coach 4.0 and bridge features"},
)


# ---------------------------------------------------------------------------
# Database schema
# ---------------------------------------------------------------------------


def init_db() -> None:
    """Create profile, match, deck, catalog, and auth session tables."""
    with _get_db().get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS player_profiles (
                owner TEXT PRIMARY KEY,
                display_name TEXT NOT NULL DEFAULT '',
                leaderboard_opt_in INTEGER NOT NULL DEFAULT 1,
                is_patron INTEGER NOT NULL DEFAULT 0,
                constructed_rank_class TEXT NOT NULL DEFAULT '',
                constructed_rank_tier INTEGER NOT NULL DEFAULT 0,
                constructed_rank_step INTEGER NOT NULL DEFAULT 0,
                constructed_mythic_pct REAL,
                constructed_mythic_place INTEGER,
                constructed_rank_score REAL NOT NULL DEFAULT 0,
                limited_rank_class TEXT NOT NULL DEFAULT '',
                limited_rank_tier INTEGER NOT NULL DEFAULT 0,
                limited_rank_step INTEGER NOT NULL DEFAULT 0,
                limited_mythic_pct REAL,
                limited_mythic_place INTEGER,
                limited_rank_score REAL NOT NULL DEFAULT 0,
                wildcards_json TEXT NOT NULL DEFAULT '{}',
                gold INTEGER NOT NULL DEFAULT 0,
                gems INTEGER NOT NULL DEFAULT 0,
                collection_json TEXT NOT NULL DEFAULT '{}',
                collection_card_count INTEGER NOT NULL DEFAULT 0,
                collection_updated_at REAL,
                discord_handle TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_profiles_display_name
                ON player_profiles(lower(display_name))
                WHERE display_name != '';

            CREATE TABLE IF NOT EXISTS player_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT NOT NULL,
                match_id TEXT NOT NULL,
                played_at REAL NOT NULL,
                result TEXT NOT NULL,
                format_name TEXT NOT NULL DEFAULT '',
                format_bucket TEXT NOT NULL DEFAULT 'other',
                deck_identity TEXT NOT NULL DEFAULT '',
                deck_name TEXT NOT NULL DEFAULT '',
                deck_colors TEXT NOT NULL DEFAULT '',
                opponent_colors TEXT NOT NULL DEFAULT '',
                turns INTEGER NOT NULL DEFAULT 0,
                autopilot INTEGER,
                rank_class TEXT NOT NULL DEFAULT '',
                rank_tier INTEGER NOT NULL DEFAULT 0,
                UNIQUE(owner, match_id)
            );
            CREATE INDEX IF NOT EXISTS idx_player_matches_owner_ts
                ON player_matches(owner, played_at DESC);
            CREATE INDEX IF NOT EXISTS idx_player_matches_bucket_ts
                ON player_matches(format_bucket, played_at DESC);
            CREATE INDEX IF NOT EXISTS idx_player_matches_deck_identity
                ON player_matches(owner, deck_identity);

            CREATE TABLE IF NOT EXISTS synced_decks (
                id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                slug TEXT UNIQUE NOT NULL,
                arena_deck_id TEXT NOT NULL DEFAULT '',
                deck_identity TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL,
                format TEXT NOT NULL DEFAULT 'standard',
                archetype TEXT NOT NULL DEFAULT '',
                colors TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                main_json TEXT NOT NULL,
                sideboard_json TEXT NOT NULL DEFAULT '[]',
                commander_json TEXT NOT NULL DEFAULT '[]',
                companion_json TEXT NOT NULL DEFAULT '[]',
                is_public INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'arena',
                parent_deck_id TEXT,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                autopilot_wins INTEGER NOT NULL DEFAULT 0,
                autopilot_losses INTEGER NOT NULL DEFAULT 0,
                sync_to_arena_status TEXT NOT NULL DEFAULT '',
                sync_to_arena_error TEXT NOT NULL DEFAULT '',
                sync_requested_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_synced_decks_owner
                ON synced_decks(owner, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_synced_decks_public
                ON synced_decks(is_public, format, wins DESC);
            CREATE INDEX IF NOT EXISTS idx_synced_decks_pending
                ON synced_decks(owner, sync_to_arena_status);

            CREATE TABLE IF NOT EXISTS card_catalog_cache (
                name_key TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                rarity TEXT NOT NULL DEFAULT '',
                grp_id INTEGER NOT NULL DEFAULT 0,
                colors TEXT NOT NULL DEFAULT '',
                mana_cost TEXT NOT NULL DEFAULT '',
                type_line TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_card_catalog_name
                ON card_catalog_cache(name);

            CREATE TABLE IF NOT EXISTS card_prints_cache (
                grp_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                rarity TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS web_login_tokens (
                token_hash TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                auth_source TEXT NOT NULL,
                expires_at REAL NOT NULL,
                used_at REAL
            );

            CREATE TABLE IF NOT EXISTS web_sessions (
                session_hash TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                auth_source TEXT NOT NULL,
                csrf_token TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_web_sessions_expires
                ON web_sessions(expires_at);
            """
        )


# ---------------------------------------------------------------------------
# Pure domain helpers (ranks, formats, deck parsing, craftability)
# ---------------------------------------------------------------------------


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _name_key(name: str) -> str:
    return " ".join(str(name or "").strip().casefold().split())


def anonymous_handle(owner: str) -> str:
    """Deterministic privacy-preserving handle shown when no custom name is set."""
    tag = "".join(ch for ch in str(owner or "") if ch.isalnum())[:4].upper() or "0000"
    return f"Player #{tag}"


def canonical_format_bucket(format_name: str) -> str:
    """Bucket an Arena format/queue string into a canonical leaderboard format."""
    folded = " ".join(str(format_name or "").replace("_", " ").replace("-", " ").lower().split())
    if not folded:
        return "other"
    if "brawl" in folded:
        return "brawl"
    if "alchemy" in folded:
        return "alchemy"
    if "historic" in folded or "timeless" in folded or "explorer" in folded:
        return "historic"
    if any(tok in folded for tok in ("draft", "sealed", "limited", "jumpin", "jump in")):
        return "limited"
    if folded in {"ladder", "tradladder", "play", "standard", "traditional standard"} or "standard" in folded:
        return "standard"
    return "other"


def normalize_deck_format(fmt: str) -> str:
    folded = " ".join(str(fmt or "").replace("_", " ").lower().split())
    if "brawl" in folded:
        return "brawl"
    if "alchemy" in folded:
        return "alchemy"
    if "timeless" in folded:
        return "timeless"
    if "explorer" in folded or "pioneer" in folded:
        return "explorer"
    if "historic" in folded:
        return "historic"
    if "draft" in folded or "sealed" in folded or "limited" in folded:
        return "limited"
    if "standard" in folded or folded in {"", "ladder", "play", "tradladder"}:
        return "standard"
    return folded if folded in ALLOWED_DECK_FORMATS else "standard"


def compute_rank_score(
    rank_class: str,
    tier: int = 0,
    step: int = 0,
    mythic_pct: Optional[float] = None,
    mythic_place: Optional[int] = None,
) -> float:
    """Numeric rank score for ordering players (Mythic #1 highest, Bronze 4 lowest)."""
    cls = str(rank_class or "").strip().lower()
    if not cls:
        return 0.0
    if cls == "mythic":
        if mythic_place is not None and int(mythic_place) > 0:
            # #1 -> ~1999, #1000 -> ~1000.1
            return 1000.0 + max(0.0, 1000.0 - float(int(mythic_place)) * 0.5)
        if mythic_pct is not None and math.isfinite(float(mythic_pct)):
            return 1000.0 + max(0.0, min(100.0, float(mythic_pct)))
        return 1000.0
    base = _RANK_ORDER.get(cls, 0.0)
    if base <= 0.0:
        return 0.0
    tier_num = max(1, min(4, int(tier or 4)))
    step_num = max(0, min(10, int(step or 0)))
    # Tier 1 > Tier 4
    return base + (5 - tier_num) * 10.0 + step_num


def format_rank_label(
    rank_class: str,
    tier: int = 0,
    step: int = 0,
    mythic_pct: Optional[float] = None,
    mythic_place: Optional[int] = None,
) -> str:
    cls = str(rank_class or "").strip()
    if not cls:
        return "Unranked"
    if cls.lower() == "mythic":
        if mythic_place is not None and int(mythic_place) > 0:
            return f"Mythic #{int(mythic_place)}"
        if mythic_pct is not None and math.isfinite(float(mythic_pct)):
            return f"Mythic {float(mythic_pct):.1f}%"
        return "Mythic"
    return f"{cls} {int(tier)}" if int(tier or 0) > 0 else cls


def _parse_iso_ts(value: Any) -> float:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return time.time()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return time.time()


def _sanitize_card_entries(raw: Any, *, max_cards: int = 250) -> list[dict[str, Any]]:
    """Normalize a list of deck card entries into [{name, count, grp_id, rarity}]."""
    merged: dict[str, dict[str, Any]] = {}
    total = 0
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        name = " ".join(str(item.get("name") or "").split())[:120]
        if not name:
            continue
        try:
            count = int(item.get("count") or 0)
        except (TypeError, ValueError):
            continue
        if count <= 0 or count > 250:
            continue
        total += count
        if total > max_cards:
            raise ValueError(f"Deck pile exceeds {max_cards} cards")
        rarity = str(item.get("rarity") or "").strip().lower()
        if rarity not in RARITIES:
            rarity = ""
        try:
            grp_id = int(item.get("grp_id") or 0)
        except (TypeError, ValueError):
            grp_id = 0
        key = _name_key(name)
        slot = merged.get(key)
        if slot is None:
            merged[key] = {
                "name": name,
                "count": count,
                "grp_id": grp_id if grp_id > 0 else None,
                "rarity": rarity,
            }
        else:
            slot["count"] += count
            if grp_id > 0 and not slot.get("grp_id"):
                slot["grp_id"] = grp_id
            if rarity and not slot.get("rarity"):
                slot["rarity"] = rarity
    return list(merged.values())


def parse_arena_deck_text(text: str) -> dict[str, list[dict[str, Any]]]:
    """Parse an Arena clipboard deck export into structured card piles."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Deck export text is empty")
    if len(text) > 64 * 1024:
        raise ValueError("Deck export text is too large")
    piles: dict[str, list[dict[str, Any]]] = {
        "main": [],
        "sideboard": [],
        "commander": [],
        "companion": [],
    }
    current = "main"
    total_cards = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        header = _SECTION_HEADERS.get(line.casefold())
        if header is not None:
            current = header
            continue
        if line.casefold() in {"about", "name"}:
            continue
        if line.casefold().startswith("name "):
            continue
        match = _DECK_LINE_RE.fullmatch(line)
        if not match:
            raise ValueError(f"Unrecognized deck line: {line[:80]}")
        count = int(match.group(1))
        name = _SET_SUFFIX_RE.sub("", match.group(2)).strip()
        if count <= 0 or count > 250 or not name:
            raise ValueError(f"Invalid card entry: {line[:80]}")
        total_cards += count
        if total_cards > 300:
            raise ValueError("Deck exceeds 300 total cards")
        piles[current].append({"name": name, "count": count, "grp_id": None, "rarity": ""})
    if not piles["main"]:
        raise ValueError("Main deck cannot be empty")
    return {pile: _sanitize_card_entries(entries, max_cards=250) for pile, entries in piles.items()}


def format_arena_export(deck: dict[str, Any]) -> str:
    """Format a deck dict into Arena's clipboard import format."""
    sections = (
        ("Commander", deck.get("commander") or []),
        ("Companion", deck.get("companion") or []),
        ("Deck", deck.get("main") or []),
        ("Sideboard", deck.get("sideboard") or []),
    )
    blocks: list[str] = []
    for label, cards in sections:
        lines = [
            f"{int(c['count'])} {c['name']}"
            for c in cards
            if isinstance(c, dict) and int(c.get("count") or 0) > 0 and c.get("name")
        ]
        if lines:
            blocks.append(label + "\n" + "\n".join(lines))
    return "\n\n".join(blocks)


def _deck_identity_from_piles(piles: dict[str, list[dict[str, Any]]]) -> str:
    """Deterministic card-name hash when an Arena grp_id identity isn't supplied."""
    parts: list[str] = []
    for pile in ("commander", "main", "sideboard", "companion"):
        for item in sorted(piles.get(pile) or [], key=lambda c: _name_key(c.get("name", ""))):
            parts.append(f"{pile}:{_name_key(item['name'])}:{int(item['count'])}")
    return _sha256_hex("|".join(parts)) if parts else ""


def _enrich_piles_from_catalog(
    conn, piles: dict[str, list[dict[str, Any]]]
) -> dict[str, list[dict[str, Any]]]:
    """Attach known rarities and grp_ids from card_catalog_cache."""
    keys = {
        _name_key(card["name"])
        for cards in piles.values()
        for card in cards
        if card.get("name")
    }
    catalog: dict[str, dict[str, Any]] = {}
    if keys:
        placeholders = ",".join("?" for _ in keys)
        rows = conn.execute(
            f"SELECT name_key, name, rarity, grp_id FROM card_catalog_cache WHERE name_key IN ({placeholders})",
            tuple(keys),
        ).fetchall()
        for row in rows:
            catalog[row["name_key"]] = dict(row)

    out: dict[str, list[dict[str, Any]]] = {}
    for pile, cards in piles.items():
        enriched: list[dict[str, Any]] = []
        for card in cards:
            item = dict(card)
            key = _name_key(item["name"])
            cat = catalog.get(key)
            if item["name"] in BASIC_LANDS:
                item["rarity"] = "common"
            elif cat:
                if not item.get("rarity") and cat.get("rarity") in RARITIES:
                    item["rarity"] = cat["rarity"]
                if not item.get("grp_id") and int(cat.get("grp_id") or 0) > 0:
                    item["grp_id"] = int(cat["grp_id"])
            enriched.append(item)
        out[pile] = enriched
    return out


def _catalog_rarities_for_cards(conn, names: set[str]) -> dict[str, str]:
    keys = {_name_key(n) for n in names if n}
    out: dict[str, str] = {}
    if not keys:
        return out
    placeholders = ",".join("?" for _ in keys)
    rows = conn.execute(
        f"SELECT name_key, rarity FROM card_catalog_cache WHERE name_key IN ({placeholders})",
        tuple(keys),
    ).fetchall()
    for row in rows:
        r = str(row["rarity"] or "").lower()
        if r in RARITIES:
            out[row["name_key"]] = r
    return out


def evaluate_deck_craftability(
    deck: dict[str, Any],
    collection_by_name: dict[str, int],
    wildcards: dict[str, int],
    catalog_rarities: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Compare a deck's card needs against a player's collection and wildcard balance.

    Mirrors ``arenamcp.deck_candidates.compare_collection``:
    - Basic lands cost 0 wildcards.
    - Companion shares copy with sideboard (max).
    - Missing cards with unknown rarity are never treated as free (``affordable=False``).
    """
    owned_norm = Counter()
    for raw_name, qty in (collection_by_name or {}).items():
        try:
            q = int(qty or 0)
        except (TypeError, ValueError):
            continue
        if q > 0 and raw_name:
            owned_norm[_name_key(raw_name)] += q

    main_counts: Counter[str] = Counter()
    side_counts: Counter[str] = Counter()
    cmd_counts: Counter[str] = Counter()
    comp_counts: Counter[str] = Counter()
    display_names: dict[str, str] = {}
    card_rarities: dict[str, str] = dict(catalog_rarities or {})

    for pile, target in (
        ("main", main_counts),
        ("sideboard", side_counts),
        ("commander", cmd_counts),
        ("companion", comp_counts),
    ):
        for entry in deck.get(pile) or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            cnt = int(entry.get("count") or 0)
            if not name or cnt <= 0:
                continue
            k = _name_key(name)
            display_names.setdefault(k, name)
            target[k] += cnt
            r = str(entry.get("rarity") or "").lower()
            if r in RARITIES and k not in card_rarities:
                card_rarities[k] = r

    for k, cnt in comp_counts.items():
        side_counts[k] = max(side_counts[k], cnt)

    required = main_counts + side_counts + cmd_counts
    spend: Counter[str] = Counter()
    missing_cards: list[dict[str, Any]] = []
    unknown: list[str] = []
    missing_total = 0

    for k, needed in required.items():
        display = display_names.get(k, k)
        if display in BASIC_LANDS:
            continue
        have = owned_norm.get(k, 0)
        shortfall = max(0, needed - have)
        if shortfall <= 0:
            continue
        missing_total += shortfall
        rarity = card_rarities.get(k, "")
        if rarity not in RARITIES:
            unknown.append(display)
            missing_cards.append({"name": display, "missing": shortfall, "rarity": "unknown"})
        else:
            spend[rarity] += shortfall
            missing_cards.append({"name": display, "missing": shortfall, "rarity": rarity})

    bal = {r: max(0, int((wildcards or {}).get(r) or 0)) for r in RARITIES}
    shortfall_wildcards = {r: spend[r] - bal[r] for r in RARITIES if spend[r] > bal[r]}
    owned = bool(required) and missing_total == 0
    affordable = bool(required) and not unknown and not shortfall_wildcards
    extra_needed = sum(shortfall_wildcards.values())
    close = bool(required) and not unknown and (affordable or extra_needed <= 4)

    return {
        "owned": owned,
        "affordable": affordable,
        "close": close,
        "spend": {r: int(spend.get(r, 0)) for r in RARITIES},
        "shortfall_wildcards": shortfall_wildcards,
        "extra_wildcards_needed": int(extra_needed),
        "missing_total": int(missing_total),
        "missing_cards": missing_cards[:40],
        "unknown": unknown[:20],
        "has_collection": bool(owned_norm) or any(v > 0 for v in bal.values()),
    }


# ---------------------------------------------------------------------------
# Auth, sessions, login tokens, and security headers
# ---------------------------------------------------------------------------


def _is_https_request(request: Request) -> bool:
    proto = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or proto == "https"


def _safe_next_url(value: Any, default: str = "/profile") -> str:
    val = str(value or "").strip()
    if (
        not val
        or not val.startswith("/")
        or val.startswith("//")
        or "\\" in val
        or any(ch in val for ch in ("\r", "\n", "\t"))
    ):
        return default
    return val


def _public_origin(request: Request) -> str:
    configured = (
        os.environ.get("PUBLIC_BASE_URL", "").strip()
        or str((state.config.get("server", {}) if isinstance(state.config, dict) else {}).get("public_url") or "").strip()
    )
    if configured:
        return configured.rstrip("/")
    parsed = urlparse(str(request.base_url))
    host = (parsed.hostname or "").lower()
    allowed = host in {"localhost", "127.0.0.1", "testserver", "mtgacoach.com"} or host.endswith(".mtgacoach.com")
    if not allowed:
        return "https://mtgacoach.com"
    scheme = "https" if _is_https_request(request) else (parsed.scheme or "http")
    netloc = parsed.netloc
    return f"{scheme}://{netloc}".rstrip("/")


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_opt_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _safe_opt_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _session_cookie_name(request: Request) -> str:
    return "__Host-mc-session" if _is_https_request(request) else "mc-session"


def _set_session_cookie(response: Any, request: Request, raw_session: str) -> None:
    is_https = _is_https_request(request)
    response.set_cookie(
        _session_cookie_name(request),
        raw_session,
        max_age=SESSION_TTL_S,
        httponly=True,
        secure=is_https,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Any, request: Request) -> None:
    is_https = _is_https_request(request)
    for name, sec in (("__Host-mc-session", True), ("mc-session", False)):
        if name == "__Host-mc-session" and not is_https:
            continue
        response.delete_cookie(name, secure=sec, httponly=True, samesite="lax", path="/")


def _apply_security_headers(response: Any, *, is_html: bool = False, no_store: bool = False) -> Any:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    if is_html:
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; frame-ancestors 'none'; object-src 'none'; "
            "base-uri 'none'; form-action 'self'",
        )
    if no_store:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response


def ensure_profile(conn, owner: str, *, auth_source: str = "", default_opt_in: bool = True) -> dict[str, Any]:
    """Ensure a player_profiles row exists for owner and refresh patron status."""
    now = time.time()
    is_patron = 1 if auth_source == "subscriber" else None
    row = conn.execute("SELECT * FROM player_profiles WHERE owner = ?", (owner,)).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO player_profiles (owner, leaderboard_opt_in, is_patron, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (owner, 1 if default_opt_in else 0, 1 if is_patron else 0, now, now),
        )
        row = conn.execute("SELECT * FROM player_profiles WHERE owner = ?", (owner,)).fetchone()
    elif is_patron is not None and int(row["is_patron"] or 0) != is_patron:
        conn.execute(
            "UPDATE player_profiles SET is_patron = ?, updated_at = ? WHERE owner = ?",
            (is_patron, now, owner),
        )
        row = conn.execute("SELECT * FROM player_profiles WHERE owner = ?", (owner,)).fetchone()
    return dict(row)


def create_web_session(owner: str, auth_source: str) -> tuple[str, str]:
    """Create a persistent browser session and return (raw_session_token, csrf_token)."""
    init_db()
    raw_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(24)
    now = time.time()
    with _get_db().get_db() as conn:
        ensure_profile(conn, owner, auth_source=auth_source)
        conn.execute("DELETE FROM web_sessions WHERE expires_at <= ?", (now,))
        conn.execute(
            """
            INSERT INTO web_sessions (session_hash, owner, auth_source, csrf_token, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (_sha256_hex(raw_token), owner, auth_source, csrf_token, now, now + SESSION_TTL_S),
        )
    return raw_token, csrf_token


def mint_login_token(owner: str, auth_source: str) -> tuple[str, int]:
    """Mint a 5-minute single-use login token for desktop -> web handoff."""
    init_db()
    raw_token = secrets.token_urlsafe(32)
    now = time.time()
    with _get_db().get_db() as conn:
        ensure_profile(conn, owner, auth_source=auth_source)
        conn.execute("DELETE FROM web_login_tokens WHERE expires_at <= ?", (now - 3600,))
        conn.execute(
            """
            INSERT INTO web_login_tokens (token_hash, owner, auth_source, expires_at, used_at)
            VALUES (?, ?, ?, ?, NULL)
            """,
            (_sha256_hex(raw_token), owner, auth_source, now + LOGIN_TOKEN_TTL_S),
        )
    return raw_token, LOGIN_TOKEN_TTL_S


def consume_login_token(raw_token: str) -> Optional[dict[str, str]]:
    """Atomically verify and consume a single-use login token."""
    token = str(raw_token or "").strip()
    if not token:
        return None
    token_hash = _sha256_hex(token)
    now = time.time()
    with _get_db().get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT owner, auth_source, expires_at, used_at FROM web_login_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if not row or row["used_at"] is not None or float(row["expires_at"]) <= now:
            return None
        conn.execute(
            "UPDATE web_login_tokens SET used_at = ? WHERE token_hash = ?",
            (now, token_hash),
        )
        return {"owner": row["owner"], "auth_source": row["auth_source"]}


async def authorize_key_value(raw_key: str) -> dict[str, str]:
    """Authorize a raw license or trial key using the canonical match_bundles rules."""
    key = str(raw_key or "").strip()
    if not key:
        raise HTTPException(401, "Missing license key")

    database = _get_db()
    sub = database.check_license(key)
    if sub:
        if sub["status"] in ("active", "trial"):
            return {"license_key": key, "source": "subscriber"}
        raise HTTPException(402, f"Subscription {sub['status']}")

    trial = match_bundles.get_trial_by_key(key)
    if trial:
        expires = match_bundles._parse_iso(trial.get("expires_at") or "")
        if expires and expires > datetime.now(timezone.utc):
            return {"license_key": key, "source": "trial"}
        raise HTTPException(402, "Trial expired")

    if await match_bundles._litellm_key_is_live(key):
        return {"license_key": key, "source": "litellm"}

    raise HTTPException(401, "Invalid license key")


async def resolve_auth(
    request: Request,
    *,
    required: bool = True,
    check_csrf: bool = False,
    allow_bearer: bool = True,
    allow_cookie: bool = True,
    csrf_override: str = "",
) -> Optional[dict[str, str]]:
    """Authenticate via Bearer license/trial key or HttpOnly web session cookie."""
    bearer = _extract_license_key(request).strip()
    if bearer and allow_bearer:
        auth = await match_bundles.authorize_upload(request)
        owner = match_bundles.owner_of(auth["license_key"])
        with _get_db().get_db() as conn:
            ensure_profile(conn, owner, auth_source=auth["source"])
        return {"owner": owner, "auth_source": auth["source"], "via": "bearer", "csrf_token": ""}

    if allow_cookie:
        cookie_val = (
            request.cookies.get("__Host-mc-session") or request.cookies.get("mc-session") or ""
        ).strip()
        if cookie_val:
            now = time.time()
            with _get_db().get_db() as conn:
                row = conn.execute(
                    "SELECT owner, auth_source, csrf_token, expires_at FROM web_sessions WHERE session_hash = ?",
                    (_sha256_hex(cookie_val),),
                ).fetchone()
                if row and float(row["expires_at"]) > now:
                    if check_csrf:
                        supplied = (csrf_override or request.headers.get("X-CSRF-Token") or "").strip()
                        if not supplied or not hmac.compare_digest(supplied, str(row["csrf_token"])):
                            raise HTTPException(403, "Invalid or missing CSRF token")
                    ensure_profile(conn, row["owner"], auth_source=row["auth_source"])
                    return {
                        "owner": row["owner"],
                        "auth_source": row["auth_source"],
                        "via": "cookie",
                        "csrf_token": str(row["csrf_token"]),
                    }

    if required:
        raise HTTPException(401, "Authentication required")
    return None


# ---------------------------------------------------------------------------
# Sync, deck, and stats aggregation
# ---------------------------------------------------------------------------


def refresh_owner_deck_stats(conn, owner: str) -> None:
    """Recompute W/L and autopilot W/L on synced_decks from player_matches."""
    decks = conn.execute(
        "SELECT id, deck_identity, name FROM synced_decks WHERE owner = ?", (owner,)
    ).fetchall()
    if not decks:
        return
    matches = conn.execute(
        "SELECT result, deck_identity, deck_name, autopilot FROM player_matches WHERE owner = ?",
        (owner,),
    ).fetchall()

    by_ident: dict[str, list[int]] = {}
    by_name: dict[str, list[int]] = {}
    for m in matches:
        res = str(m["result"] or "").lower()
        if res not in ("win", "loss"):
            continue
        is_win = 1 if res == "win" else 0
        is_ap = m["autopilot"] == 1
        ident = str(m["deck_identity"] or "").strip()
        dname = _name_key(m["deck_name"] or "")
        if ident:
            slot = by_ident.setdefault(ident, [0, 0, 0, 0])
            slot[0 if is_win else 1] += 1
            if is_ap:
                slot[2 if is_win else 3] += 1
        if dname:
            slot = by_name.setdefault(dname, [0, 0, 0, 0])
            slot[0 if is_win else 1] += 1
            if is_ap:
                slot[2 if is_win else 3] += 1

    for d in decks:
        ident = str(d["deck_identity"] or "").strip()
        dname = _name_key(d["name"] or "")
        counts = by_ident.get(ident) if ident and ident in by_ident else by_name.get(dname)
        w, l, aw, al = counts if counts else (0, 0, 0, 0)
        conn.execute(
            """
            UPDATE synced_decks
            SET wins = ?, losses = ?, autopilot_wins = ?, autopilot_losses = ?
            WHERE id = ?
            """,
            (w, l, aw, al, d["id"]),
        )


def _deck_row_to_dict(
    row: Any,
    *,
    collection: Optional[dict[str, int]] = None,
    wildcards: Optional[dict[str, int]] = None,
    catalog_rarities: Optional[dict[str, str]] = None,
    author_name: str = "",
    author_is_patron: bool = False,
) -> dict[str, Any]:
    main = json.loads(row["main_json"] or "[]")
    sideboard = json.loads(row["sideboard_json"] or "[]")
    commander = json.loads(row["commander_json"] or "[]")
    companion = json.loads(row["companion_json"] or "[]")
    wins = int(row["wins"] or 0)
    losses = int(row["losses"] or 0)
    ap_wins = int(row["autopilot_wins"] or 0)
    ap_losses = int(row["autopilot_losses"] or 0)
    total = wins + losses
    ap_total = ap_wins + ap_losses
    deck_dict: dict[str, Any] = {
        "id": row["id"],
        "slug": row["slug"],
        "arena_deck_id": row["arena_deck_id"] or "",
        "deck_identity": row["deck_identity"] or "",
        "name": row["name"],
        "format": row["format"],
        "archetype": row["archetype"] or "",
        "colors": row["colors"] or "",
        "description": row["description"] or "",
        "main": main,
        "sideboard": sideboard,
        "commander": commander,
        "companion": companion,
        "card_count": sum(int(c.get("count") or 0) for c in main)
        + sum(int(c.get("count") or 0) for c in commander),
        "is_public": bool(row["is_public"]),
        "source": row["source"] or "arena",
        "wins": wins,
        "losses": losses,
        "games": total,
        "win_rate": round(wins / total, 4) if total > 0 else None,
        "autopilot_wins": ap_wins,
        "autopilot_losses": ap_losses,
        "autopilot_games": ap_total,
        "autopilot_win_rate": round(ap_wins / ap_total, 4) if ap_total > 0 else None,
        "sync_to_arena_status": row["sync_to_arena_status"] or "",
        "sync_to_arena_error": row["sync_to_arena_error"] or "",
        "updated_at": float(row["updated_at"] or 0),
        "created_at": float(row["created_at"] or 0),
    }
    if author_name:
        deck_dict["author_name"] = author_name
        deck_dict["author_is_patron"] = bool(author_is_patron)
    deck_dict["export_text"] = format_arena_export(deck_dict)
    if collection is not None or wildcards is not None:
        deck_dict["craftability"] = evaluate_deck_craftability(
            deck_dict, collection or {}, wildcards or {}, catalog_rarities
        )
    return deck_dict


def _summarize_match_window(matches: list[Any], *, days: Optional[int] = None, now: Optional[float] = None) -> dict[str, Any]:
    cutoff = ((now or time.time()) - days * 86400.0) if days is not None else 0.0
    w = 0
    l = 0
    ap_w = 0
    ap_l = 0
    man_w = 0
    man_l = 0
    for m in matches:
        if float(m["played_at"]) < cutoff:
            continue
        res = str(m["result"] or "").lower()
        if res not in ("win", "loss"):
            continue
        is_win = res == "win"
        if is_win:
            w += 1
        else:
            l += 1
        if m["autopilot"] == 1:
            if is_win:
                ap_w += 1
            else:
                ap_l += 1
        elif m["autopilot"] == 0:
            if is_win:
                man_w += 1
            else:
                man_l += 1
    tot = w + l
    ap_tot = ap_w + ap_l
    man_tot = man_w + man_l
    return {
        "wins": w,
        "losses": l,
        "games": tot,
        "win_rate": round(w / tot, 4) if tot > 0 else None,
        "autopilot_wins": ap_w,
        "autopilot_losses": ap_l,
        "autopilot_games": ap_tot,
        "autopilot_win_rate": round(ap_w / ap_tot, 4) if ap_tot > 0 else None,
        "manual_wins": man_w,
        "manual_losses": man_l,
        "manual_games": man_tot,
        "manual_win_rate": round(man_w / man_tot, 4) if man_tot > 0 else None,
    }


def _build_deck_performance(conn, row: Any) -> dict[str, Any]:
    """Compute historical performance and recent matches for one deck row."""
    owner = str(row["owner"] or "")
    is_source = owner.startswith("source:")
    now = time.time()
    empty = {
        "all_time": _summarize_match_window([], now=now),
        "last_30d": _summarize_match_window([], days=30, now=now),
        "autopilot": {"wins": 0, "losses": 0, "games": 0, "win_rate": None},
        "manual": {"wins": 0, "losses": 0, "games": 0, "win_rate": None},
        "avg_turns": None,
        "streak": "",
        "matchups": [],
        "recent_matches": [],
    }

    if is_source:
        fb = canonical_format_bucket(row["format"])
        target_fold = _fold_deck_label(row["name"] or "")
        if not target_fold:
            return empty
        cand_rows = conn.execute(
            "SELECT * FROM player_matches WHERE format_bucket = ? AND result IN ('win', 'loss') ORDER BY played_at DESC",
            (fb,),
        ).fetchall()
        matches = [m for m in cand_rows if _fold_deck_label(m["deck_name"] or "") == target_fold]
        if len(matches) < MIN_WINRATE_GAMES:
            return empty
    else:
        ident = str(row["deck_identity"] or "").strip()
        dname = str(row["name"] or "").strip()
        if ident and dname:
            matches = conn.execute(
                """
                SELECT * FROM player_matches
                WHERE owner = ? AND ((deck_identity != '' AND deck_identity = ?) OR lower(deck_name) = lower(?))
                ORDER BY played_at DESC
                """,
                (owner, ident, dname),
            ).fetchall()
        elif ident:
            matches = conn.execute(
                "SELECT * FROM player_matches WHERE owner = ? AND deck_identity = ? ORDER BY played_at DESC",
                (owner, ident),
            ).fetchall()
        elif dname:
            matches = conn.execute(
                "SELECT * FROM player_matches WHERE owner = ? AND lower(deck_name) = lower(?) ORDER BY played_at DESC",
                (owner, dname),
            ).fetchall()
        else:
            matches = []

    all_time = _summarize_match_window(matches, now=now)
    last_30d = _summarize_match_window(matches, days=30, now=now)
    streak_ch = ""
    streak_len = 0
    turns_sum = 0
    turns_cnt = 0
    by_opp: dict[str, list[int]] = {}
    recent: list[dict[str, Any]] = []

    for m in matches:
        res = str(m["result"] or "").lower()
        if res in ("win", "loss"):
            ch = "W" if res == "win" else "L"
            if not streak_ch:
                streak_ch = ch
                streak_len = 1
            elif streak_ch == ch and streak_len > 0:
                streak_len += 1
            elif streak_len > 0:
                streak_len = -streak_len
            opp = "".join(sorted(set(str(m["opponent_colors"] or ""))))
            if opp:
                slot = by_opp.setdefault(opp, [0, 0])
                slot[0 if res == "win" else 1] += 1
        t = int(m["turns"] or 0)
        if t > 0:
            turns_sum += t
            turns_cnt += 1
        if not is_source and len(recent) < 15:
            ts = float(m["played_at"] or 0)
            recent.append(
                {
                    "result": res,
                    "format_name": m["format_name"] or m["format_bucket"] or "",
                    "turns": t,
                    "autopilot": None if m["autopilot"] is None else bool(m["autopilot"]),
                    "played_at": ts,
                    "played_label": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts > 0 else "—",
                }
            )

    return {
        "all_time": all_time,
        "last_30d": last_30d,
        "autopilot": {
            "wins": all_time["autopilot_wins"],
            "losses": all_time["autopilot_losses"],
            "games": all_time["autopilot_games"],
            "win_rate": all_time["autopilot_win_rate"],
        },
        "manual": {
            "wins": all_time["manual_wins"],
            "losses": all_time["manual_losses"],
            "games": all_time["manual_games"],
            "win_rate": all_time["manual_win_rate"],
        },
        "avg_turns": round(turns_sum / turns_cnt, 1) if turns_cnt else None,
        "streak": f"{streak_ch}{abs(streak_len)}" if streak_ch else "",
        "matchups": [
            {
                "colors": cols,
                "wins": wl[0],
                "losses": wl[1],
                "games": wl[0] + wl[1],
                "win_rate": round(wl[0] / (wl[0] + wl[1]), 4) if (wl[0] + wl[1]) else 0.0,
            }
            for cols, wl in sorted(by_opp.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))[:8]
        ],
        "recent_matches": recent,
    }


def _deck_slug_lookup(deck_rows: list[Any]) -> tuple[dict[str, str], dict[str, str]]:
    slug_by_ident: dict[str, str] = {}
    slug_by_name: dict[str, str] = {}
    for dr in deck_rows:
        slug = str(dr["slug"] or "")
        if not slug:
            continue
        ident = str(dr["deck_identity"] or "").strip()
        dname = str(dr["name"] or "").strip().lower()
        if ident and ident not in slug_by_ident:
            slug_by_ident[ident] = slug
        if dname and dname not in slug_by_name:
            slug_by_name[dname] = slug
    return slug_by_ident, slug_by_name


def _build_profile_summary(conn, owner: str) -> dict[str, Any]:
    prof = dict(conn.execute("SELECT * FROM player_profiles WHERE owner = ?", (owner,)).fetchone())
    wildcards = json.loads(prof.get("wildcards_json") or "{}")
    collection = json.loads(prof.get("collection_json") or "{}")

    matches = conn.execute(
        "SELECT * FROM player_matches WHERE owner = ? ORDER BY played_at DESC",
        (owner,),
    ).fetchall()

    now = time.time()

    def _window_stats(days: Optional[int]) -> dict[str, Any]:
        cutoff = (now - days * 86400.0) if days is not None else 0.0
        w = 0
        l = 0
        ap_w = 0
        ap_l = 0
        for m in matches:
            if float(m["played_at"]) < cutoff:
                continue
            res = str(m["result"] or "").lower()
            if res not in ("win", "loss"):
                continue
            if res == "win":
                w += 1
                if m["autopilot"] == 1:
                    ap_w += 1
            else:
                l += 1
                if m["autopilot"] == 1:
                    ap_l += 1
        tot = w + l
        ap_tot = ap_w + ap_l
        return {
            "wins": w,
            "losses": l,
            "games": tot,
            "win_rate": round(w / tot, 4) if tot > 0 else None,
            "autopilot_wins": ap_w,
            "autopilot_losses": ap_l,
            "autopilot_games": ap_tot,
            "autopilot_win_rate": round(ap_w / ap_tot, 4) if ap_tot > 0 else None,
        }

    # Current streak + avg turns + format breakdown + opponent color matchups
    streak_ch = ""
    streak_len = 0
    turns_sum = 0
    turns_cnt = 0
    by_format: dict[str, list[int]] = {}
    by_opp_colors: dict[str, list[int]] = {}
    recent_matches: list[dict[str, Any]] = []

    for m in matches:
        res = str(m["result"] or "").lower()
        if res in ("win", "loss"):
            ch = "W" if res == "win" else "L"
            if not streak_ch:
                streak_ch = ch
                streak_len = 1
            elif streak_ch == ch and streak_len > 0:
                streak_len += 1
            elif streak_len > 0:
                streak_len = -streak_len
            bucket = str(m["format_bucket"] or "other")
            fslot = by_format.setdefault(bucket, [0, 0])
            fslot[0 if res == "win" else 1] += 1
            opp = "".join(sorted(set(str(m["opponent_colors"] or ""))))
            if opp:
                cslot = by_opp_colors.setdefault(opp, [0, 0])
                cslot[0 if res == "win" else 1] += 1
        t = int(m["turns"] or 0)
        if t > 0:
            turns_sum += t
            turns_cnt += 1
        if len(recent_matches) < 30:
            ts = float(m["played_at"])
            recent_matches.append(
                {
                    "match_id": m["match_id"],
                    "played_at": ts,
                    "played_label": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts > 0 else "—",
                    "result": res,
                    "format_name": m["format_name"] or m["format_bucket"],
                    "format_bucket": m["format_bucket"],
                    "deck_identity": m["deck_identity"] or "",
                    "deck_name": m["deck_name"] or "",
                    "deck_slug": "",
                    "deck_colors": m["deck_colors"] or "",
                    "opponent_colors": m["opponent_colors"] or "",
                    "turns": t,
                    "autopilot": None if m["autopilot"] is None else bool(m["autopilot"]),
                }
            )

    deck_rows = conn.execute(
        "SELECT * FROM synced_decks WHERE owner = ? ORDER BY updated_at DESC",
        (owner,),
    ).fetchall()
    slug_by_ident, slug_by_name = _deck_slug_lookup(deck_rows)
    for rm in recent_matches:
        ident = rm.get("deck_identity") or ""
        dname = str(rm.get("deck_name") or "").strip().lower()
        rm["deck_slug"] = (ident and slug_by_ident.get(ident)) or (dname and slug_by_name.get(dname)) or ""
    all_card_names = {
        c.get("name", "")
        for r in deck_rows
        for pile in ("main_json", "sideboard_json", "commander_json", "companion_json")
        for c in json.loads(r[pile] or "[]")
        if isinstance(c, dict)
    }
    catalog_rarities = _catalog_rarities_for_cards(conn, all_card_names)
    decks = [
        _deck_row_to_dict(r, collection=collection, wildcards=wildcards, catalog_rarities=catalog_rarities)
        for r in deck_rows
    ]

    constructed_label = format_rank_label(
        prof.get("constructed_rank_class", ""),
        int(prof.get("constructed_rank_tier") or 0),
        int(prof.get("constructed_rank_step") or 0),
        prof.get("constructed_mythic_pct"),
        prof.get("constructed_mythic_place"),
    )
    limited_label = format_rank_label(
        prof.get("limited_rank_class", ""),
        int(prof.get("limited_rank_tier") or 0),
        int(prof.get("limited_rank_step") or 0),
        prof.get("limited_mythic_pct"),
        prof.get("limited_mythic_place"),
    )

    return {
        "profile": {
            "owner": prof["owner"],
            "display_name": prof["display_name"] or "",
            "effective_handle": (prof["display_name"] or "").strip() or anonymous_handle(prof["owner"]),
            "leaderboard_opt_in": bool(prof["leaderboard_opt_in"]),
            "is_patron": bool(prof["is_patron"]),
            "discord_handle": prof["discord_handle"] or "",
            "constructed_rank": constructed_label,
            "constructed_rank_score": float(prof.get("constructed_rank_score") or 0.0),
            "limited_rank": limited_label,
            "limited_rank_score": float(prof.get("limited_rank_score") or 0.0),
            "wildcards": {r: int(wildcards.get(r) or 0) for r in RARITIES},
            "gold": int(prof.get("gold") or 0),
            "gems": int(prof.get("gems") or 0),
            "collection_card_count": int(prof.get("collection_card_count") or 0),
            "collection_unique_count": len(collection),
            "collection_updated_at": prof.get("collection_updated_at"),
        },
        "stats": {
            "all_time": _window_stats(None),
            "last_30d": _window_stats(30),
            "last_7d": _window_stats(7),
            "streak": f"{streak_ch}{abs(streak_len)}" if streak_ch else "",
            "avg_turns": round(turns_sum / turns_cnt, 1) if turns_cnt else None,
            "by_format": [
                {
                    "format": fmt,
                    "wins": wl[0],
                    "losses": wl[1],
                    "games": wl[0] + wl[1],
                    "win_rate": round(wl[0] / (wl[0] + wl[1]), 4) if (wl[0] + wl[1]) else 0.0,
                }
                for fmt, wl in sorted(by_format.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))
            ],
            "matchups": [
                {
                    "colors": colors,
                    "wins": wl[0],
                    "losses": wl[1],
                    "games": wl[0] + wl[1],
                    "win_rate": round(wl[0] / (wl[0] + wl[1]), 4) if (wl[0] + wl[1]) else 0.0,
                }
                for colors, wl in sorted(by_opp_colors.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))[:8]
            ],
        },
        "decks": decks,
        "recent_matches": recent_matches,
        "discord": {
            "invite_url": get_discord_invite_url(),
            "patron_rooms_unlocked": bool(prof["is_patron"]),
            "rooms": list(PATRON_DISCORD_ROOMS),
        },
    }


def _upsert_deck_record(
    conn,
    owner: str,
    *,
    name: str,
    fmt: str,
    piles: dict[str, list[dict[str, Any]]],
    arena_deck_id: str = "",
    deck_identity: str = "",
    archetype: str = "",
    colors: str = "",
    description: str = "",
    is_public: Optional[bool] = False,
    default_public: bool = False,
    source: str = "arena",
    parent_deck_id: Optional[str] = None,
    queue_for_arena: bool = False,
    deck_id: Optional[str] = None,
    clear_empty_meta: bool = True,
) -> str:
    now = time.time()
    enriched = _enrich_piles_from_catalog(conn, piles)
    ident = deck_identity.strip() or _deck_identity_from_piles(enriched)
    clean_name = " ".join(str(name or "").split())[:100] or "Unnamed Deck"
    clean_fmt = normalize_deck_format(fmt)
    clean_arch = " ".join(str(archetype or "").split())[:80]
    clean_desc = str(description or "").strip()[:1000]
    clean_colors = "".join(c for c in str(colors or "").upper() if c in "WUBRG")[:5]

    existing = None
    if deck_id:
        existing = conn.execute(
            "SELECT * FROM synced_decks WHERE id = ? AND owner = ?", (deck_id, owner)
        ).fetchone()
    elif arena_deck_id:
        existing = conn.execute(
            "SELECT * FROM synced_decks WHERE owner = ? AND arena_deck_id = ?",
            (owner, arena_deck_id),
        ).fetchone()

    main_json = json.dumps(enriched.get("main") or [])
    side_json = json.dumps(enriched.get("sideboard") or [])
    cmd_json = json.dumps(enriched.get("commander") or [])
    comp_json = json.dumps(enriched.get("companion") or [])

    if existing is not None:
        sync_status = "pending" if queue_for_arena else existing["sync_to_arena_status"]
        sync_ts = now if queue_for_arena else existing["sync_requested_at"]
        resolved_public = existing["is_public"] if is_public is None else (1 if is_public else 0)
        resolved_arch = clean_arch if clear_empty_meta else (clean_arch or existing["archetype"])
        resolved_desc = clean_desc if clear_empty_meta else (clean_desc or existing["description"])
        conn.execute(
            """
            UPDATE synced_decks
            SET name = ?, format = ?, deck_identity = ?, archetype = ?, colors = ?, description = ?,
                main_json = ?, sideboard_json = ?, commander_json = ?, companion_json = ?,
                is_public = ?, sync_to_arena_status = ?, sync_requested_at = ?, updated_at = ?
            WHERE id = ? AND owner = ?
            """,
            (
                clean_name,
                clean_fmt,
                ident,
                resolved_arch,
                clean_colors or existing["colors"],
                resolved_desc,
                main_json,
                side_json,
                cmd_json,
                comp_json,
                resolved_public,
                sync_status,
                sync_ts,
                now,
                existing["id"],
                owner,
            ),
        )
        return str(existing["id"])

    new_id = uuid.uuid4().hex
    slug = secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:10]
    if not slug:
        slug = new_id[:10]
    initial_public = 1 if (default_public if is_public is None else is_public) else 0
    conn.execute(
        """
        INSERT INTO synced_decks (
            id, owner, slug, arena_deck_id, deck_identity, name, format, archetype, colors, description,
            main_json, sideboard_json, commander_json, companion_json, is_public, source, parent_deck_id,
            sync_to_arena_status, sync_requested_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_id,
            owner,
            slug,
            arena_deck_id,
            ident,
            clean_name,
            clean_fmt,
            clean_arch,
            clean_colors,
            clean_desc,
            main_json,
            side_json,
            cmd_json,
            comp_json,
            initial_public,
            source,
            parent_deck_id,
            "pending" if queue_for_arena else "",
            now if queue_for_arena else None,
            now,
            now,
        ),
    )
    return new_id


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    next_url = _safe_next_url(request.query_params.get("next") or "/profile")
    auth = await resolve_auth(request, required=False)
    if auth is not None:
        resp = RedirectResponse(next_url, status_code=302)
        return _apply_security_headers(resp, no_store=True)
    resp = state.templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"next_url": next_url, "error": str(request.query_params.get("error") or "")},
    )
    return _apply_security_headers(resp, is_html=True, no_store=True)


@router.post("/login/key")
async def login_with_key(request: Request):
    content_type = (request.headers.get("content-type") or "").lower()
    wants_json = "application/json" in content_type
    license_key = ""
    next_url = "/profile"
    if wants_json:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON body")
        if isinstance(body, dict):
            license_key = str(body.get("license_key") or "").strip()
            next_url = str(body.get("next") or "/profile").strip()
    else:
        form = await request.form()
        license_key = str(form.get("license_key") or "").strip()
        next_url = str(form.get("next") or "/profile").strip()

    next_url = _safe_next_url(next_url)

    try:
        auth = await authorize_key_value(license_key)
    except HTTPException as exc:
        if wants_json:
            raise
        resp = state.templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"next_url": next_url, "error": str(exc.detail)},
            status_code=exc.status_code,
        )
        return _apply_security_headers(resp, is_html=True, no_store=True)

    owner = match_bundles.owner_of(auth["license_key"])
    raw_session, _csrf = create_web_session(owner, auth["source"])
    if wants_json:
        resp = JSONResponse({"ok": True, "redirect": next_url})
    else:
        resp = RedirectResponse(next_url, status_code=303)
    _set_session_cookie(resp, request, raw_session)
    return _apply_security_headers(resp, no_store=True)


@router.post("/logout")
async def logout(request: Request):
    form_csrf = ""
    try:
        form = await request.form()
        form_csrf = str(form.get("csrf_token") or "").strip()
    except Exception:
        pass
    await resolve_auth(request, required=False, check_csrf=True, allow_bearer=False, csrf_override=form_csrf)
    cookie_val = (
        request.cookies.get("__Host-mc-session") or request.cookies.get("mc-session") or ""
    ).strip()
    if cookie_val:
        with _get_db().get_db() as conn:
            conn.execute("DELETE FROM web_sessions WHERE session_hash = ?", (_sha256_hex(cookie_val),))
    resp = RedirectResponse("/", status_code=303)
    _clear_session_cookie(resp, request)
    resp.headers["Clear-Site-Data"] = '"cookies", "storage", "cache"'
    return _apply_security_headers(resp, no_store=True)


@router.get("/profile/auth/exchange")
async def exchange_login_token(request: Request, token: str = ""):
    consumed = consume_login_token(token)
    if not consumed:
        resp = state.templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"next_url": "/profile", "error": "That sign-in link has expired or was already used."},
            status_code=403,
        )
        return _apply_security_headers(resp, is_html=True, no_store=True)
    raw_session, _csrf = create_web_session(consumed["owner"], consumed["auth_source"])
    next_hash = str(request.query_params.get("section") or "").strip()
    target = "/profile" + (f"#{quote(next_hash)}" if next_hash in {"overview", "decks", "builder", "discover", "leaderboard"} else "")
    resp = RedirectResponse(target, status_code=303)
    _set_session_cookie(resp, request, raw_session)
    return _apply_security_headers(resp, no_store=True)


@router.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    auth = await resolve_auth(request, required=False, allow_bearer=False)
    if auth is None:
        resp = RedirectResponse("/login?next=/profile", status_code=302)
        return _apply_security_headers(resp, no_store=True)
    with _get_db().get_db() as conn:
        summary = _build_profile_summary(conn, auth["owner"])
    resp = state.templates.TemplateResponse(
        request=request,
        name="profile.html",
        context={
            "csrf_token": auth["csrf_token"],
            "auth_source": auth["auth_source"],
            "summary": summary,
            "discord_invite_url": get_discord_invite_url(),
            "patron_rooms": PATRON_DISCORD_ROOMS,
        },
    )
    return _apply_security_headers(resp, is_html=True, no_store=True)


@router.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard_page(request: Request):
    auth = await resolve_auth(request, required=False, allow_bearer=False)
    resp = state.templates.TemplateResponse(
        request=request,
        name="leaderboard.html",
        context={
            "signed_in": auth is not None,
            "csrf_token": auth["csrf_token"] if auth else "",
            "discord_invite_url": get_discord_invite_url(),
        },
    )
    return _apply_security_headers(resp, is_html=True)


@router.get("/decks/{slug}", response_class=HTMLResponse)
async def deck_detail_page(request: Request, slug: str):
    auth = await resolve_auth(request, required=False, allow_bearer=False)
    with _get_db().get_db() as conn:
        row = conn.execute(
            """
            SELECT d.*, COALESCE(p.display_name, '') AS author_name,
                   COALESCE(p.is_patron, 0) AS author_is_patron,
                   COALESCE(p.leaderboard_opt_in, 1) AS author_opt_in
            FROM synced_decks d
            LEFT JOIN player_profiles p ON p.owner = d.owner
            WHERE d.slug = ?
            """,
            (slug,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Deck not found")
        row_owner = str(row["owner"] or "")
        is_source = row_owner.startswith("source:")
        public_ok = bool(row["is_public"]) and (is_source or bool(row["author_opt_in"]))
        if not public_ok and (not auth or auth["owner"] != row_owner):
            raise HTTPException(404, "Deck not found")
        collection = None
        wildcards = None
        catalog_rarities = None
        if auth:
            prof = conn.execute(
                "SELECT collection_json, wildcards_json FROM player_profiles WHERE owner = ?",
                (auth["owner"],),
            ).fetchone()
            if prof:
                collection = json.loads(prof["collection_json"] or "{}")
                wildcards = json.loads(prof["wildcards_json"] or "{}")
                names = {
                    c.get("name", "")
                    for pile in ("main_json", "sideboard_json", "commander_json", "companion_json")
                    for c in json.loads(row[pile] or "[]")
                    if isinstance(c, dict)
                }
                catalog_rarities = _catalog_rarities_for_cards(conn, names)
        author_display = row["author_name"] or anonymous_handle(row_owner)
        deck = _deck_row_to_dict(
            row,
            collection=collection,
            wildcards=wildcards,
            catalog_rarities=catalog_rarities,
            author_name=author_display,
            author_is_patron=bool(row["author_is_patron"]),
        )
        performance = _build_deck_performance(conn, row)
        author_profile_url = (
            "" if (is_source or not bool(row["author_opt_in"])) else f"/players/{quote(author_display, safe='')}"
        )
    resp = state.templates.TemplateResponse(
        request=request,
        name="deck_detail.html",
        context={
            "deck": deck,
            "performance": performance,
            "is_source": is_source,
            "author_profile_url": author_profile_url,
            "discord_invite_url": get_discord_invite_url(),
            "signed_in": auth is not None,
            "is_owner": bool(auth and auth["owner"] == row_owner),
            "csrf_token": auth["csrf_token"] if auth else "",
        },
    )
    return _apply_security_headers(resp, is_html=True)


def _resolve_public_player_row(conn, handle: str) -> Optional[dict[str, Any]]:
    target = str(handle or "").strip().lower()
    if not target:
        return None
    direct = conn.execute(
        """
        SELECT * FROM player_profiles
        WHERE leaderboard_opt_in = 1 AND owner NOT LIKE 'source:%' AND lower(trim(display_name)) = ?
        """,
        (target,),
    ).fetchone()
    if direct is not None:
        return dict(direct)
    for r in conn.execute(
        "SELECT * FROM player_profiles WHERE leaderboard_opt_in = 1 AND owner NOT LIKE 'source:%' AND trim(display_name) = ''"
    ).fetchall():
        if anonymous_handle(r["owner"]).lower() == target:
            return dict(r)
    return None


@router.get("/players/{handle}", response_class=HTMLResponse)
async def player_profile_page(request: Request, handle: str):
    auth = await resolve_auth(request, required=False, allow_bearer=False)
    with _get_db().get_db() as conn:
        target = _resolve_public_player_row(conn, handle)
        if target is None:
            raise HTTPException(404, "Player profile not found")
        target_owner = target["owner"]
        summary = _build_profile_summary(conn, target_owner)
        # Keep only public decks on public player pages unless viewer is the owner
        is_self = bool(auth and auth["owner"] == target_owner)
        public_rows = conn.execute(
            """
            SELECT * FROM synced_decks
            WHERE owner = ? AND is_public = 1
            ORDER BY (wins + losses) DESC, updated_at DESC
            """,
            (target_owner,),
        ).fetchall()
        viewer_col = None
        viewer_wc = None
        viewer_rarities = None
        if auth:
            vprof = conn.execute(
                "SELECT collection_json, wildcards_json FROM player_profiles WHERE owner = ?",
                (auth["owner"],),
            ).fetchone()
            if vprof:
                viewer_col = json.loads(vprof["collection_json"] or "{}")
                viewer_wc = json.loads(vprof["wildcards_json"] or "{}")
                names = {
                    c.get("name", "")
                    for r in public_rows
                    for pile in ("main_json", "sideboard_json", "commander_json", "companion_json")
                    for c in json.loads(r[pile] or "[]")
                    if isinstance(c, dict)
                }
                viewer_rarities = _catalog_rarities_for_cards(conn, names)
        decks = [
            _deck_row_to_dict(
                r,
                collection=viewer_col,
                wildcards=viewer_wc,
                catalog_rarities=viewer_rarities,
            )
            for r in public_rows
        ]
        pub_slug_ident, pub_slug_name = _deck_slug_lookup(public_rows)
        recent_matches = []
        for rm in summary["recent_matches"][:15]:
            ident = rm.get("deck_identity") or ""
            dname = str(rm.get("deck_name") or "").strip().lower()
            recent_matches.append(
                {
                    **rm,
                    "deck_slug": (ident and pub_slug_ident.get(ident)) or (dname and pub_slug_name.get(dname)) or "",
                }
            )

    player_ctx = {
        "handle": summary["profile"]["effective_handle"],
        "is_patron": summary["profile"]["is_patron"],
        "constructed_rank": summary["profile"]["constructed_rank"],
        "limited_rank": summary["profile"]["limited_rank"],
    }
    resp = state.templates.TemplateResponse(
        request=request,
        name="player_profile.html",
        context={
            "player": player_ctx,
            "stats": summary["stats"],
            "decks": decks,
            "recent_matches": recent_matches,
            "signed_in": auth is not None,
            "is_self": is_self,
            "csrf_token": auth["csrf_token"] if auth else "",
            "discord_invite_url": get_discord_invite_url(),
        },
    )
    return _apply_security_headers(resp, is_html=True)


# ---------------------------------------------------------------------------
# JSON APIs (/v1/...)
# ---------------------------------------------------------------------------


@router.post("/v1/profile/login-token")
async def api_mint_login_token(request: Request):
    auth = await resolve_auth(request, required=True, allow_cookie=False)
    token, ttl = mint_login_token(auth["owner"], auth["auth_source"])
    base = _public_origin(request)
    section = ""
    try:
        body = await request.json()
        if isinstance(body, dict):
            sec = str(body.get("section") or "").strip()
            if sec in {"overview", "decks", "builder", "discover", "leaderboard"}:
                section = f"&section={sec}"
    except Exception:
        pass
    return JSONResponse(
        {
            "ok": True,
            "login_url": f"{base}/profile/auth/exchange?token={quote(token)}{section}",
            "expires_in": ttl,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/v1/profile/me")
async def api_profile_me(request: Request):
    auth = await resolve_auth(request, required=True)
    with _get_db().get_db() as conn:
        summary = _build_profile_summary(conn, auth["owner"])
    return JSONResponse(summary, headers={"Cache-Control": "no-store"})


def _notify_discord_feed() -> None:
    try:
        import discord_feed

        discord_feed.notify_leaderboard_dirty()
    except Exception:
        pass


@router.patch("/v1/profile/settings")
async def api_profile_settings(request: Request):
    auth = await resolve_auth(request, required=True, check_csrf=True)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "Body must be a JSON object")

    updates: dict[str, Any] = {}
    if "display_name" in body:
        name = " ".join(str(body.get("display_name") or "").split())
        if name and not _DISPLAY_NAME_RE.fullmatch(name):
            raise HTTPException(
                422,
                "Handle must be 2-32 characters (letters, numbers, spaces, ., _ or -).",
            )
        updates["display_name"] = name
    if "leaderboard_opt_in" in body:
        updates["leaderboard_opt_in"] = 1 if bool(body.get("leaderboard_opt_in")) else 0
    if "discord_handle" in body:
        d_handle = str(body.get("discord_handle") or "").strip()
        if d_handle and not _DISCORD_HANDLE_RE.fullmatch(d_handle):
            raise HTTPException(422, "Invalid Discord handle format.")
        updates["discord_handle"] = d_handle

    now = time.time()
    with _get_db().get_db() as conn:
        if "display_name" in updates and updates["display_name"]:
            clash = conn.execute(
                "SELECT owner FROM player_profiles WHERE lower(display_name) = lower(?) AND owner != ?",
                (updates["display_name"], auth["owner"]),
            ).fetchone()
            if clash is not None:
                raise HTTPException(409, "That leaderboard handle is already taken.")
        if updates:
            updates["updated_at"] = now
            cols = ", ".join(f"{k} = ?" for k in updates)
            vals = list(updates.values()) + [auth["owner"]]
            conn.execute(f"UPDATE player_profiles SET {cols} WHERE owner = ?", vals)
        summary = _build_profile_summary(conn, auth["owner"])
    if "display_name" in updates or "leaderboard_opt_in" in updates:
        _notify_discord_feed()
    return JSONResponse({"ok": True, "profile": summary["profile"]})


@router.post("/v1/sync/push")
async def api_sync_push(request: Request):
    auth = await resolve_auth(request, required=True, allow_cookie=False)
    raw = await match_bundles._read_body_capped(request, MAX_SYNC_BODY_BYTES)
    try:
        body = json.loads(raw)
    except Exception:
        raise HTTPException(400, "Invalid JSON payload")
    if not isinstance(body, dict):
        raise HTTPException(400, "Payload must be a JSON object")

    owner = auth["owner"]
    now = time.time()
    counts = {"matches": 0, "decks": 0, "catalog": 0, "collection": 0}

    with _get_db().get_db() as conn:
        prof = ensure_profile(conn, owner, auth_source=auth["auth_source"])
        owner_default_public = bool(prof.get("leaderboard_opt_in"))

        # 1. Catalog entries (name -> rarity/grp_id/colors)
        catalog_entries = body.get("catalog_entries")
        if isinstance(catalog_entries, list):
            for entry in catalog_entries[:MAX_CATALOG_ENTRIES]:
                if not isinstance(entry, dict):
                    continue
                cname = " ".join(str(entry.get("name") or "").split())[:120]
                if not cname:
                    continue
                rarity = str(entry.get("rarity") or "").lower()
                if rarity not in RARITIES:
                    rarity = ""
                grp_id = int(entry.get("grp_id") or 0) if str(entry.get("grp_id") or "").isdigit() else 0
                colors = "".join(c for c in str(entry.get("colors") or "").upper() if c in "WUBRG")[:5]
                conn.execute(
                    """
                    INSERT INTO card_catalog_cache (name_key, name, rarity, grp_id, colors, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(name_key) DO UPDATE SET
                        name = excluded.name,
                        rarity = CASE WHEN excluded.rarity != '' THEN excluded.rarity ELSE card_catalog_cache.rarity END,
                        grp_id = CASE WHEN excluded.grp_id > 0 THEN excluded.grp_id ELSE card_catalog_cache.grp_id END,
                        colors = CASE WHEN excluded.colors != '' THEN excluded.colors ELSE card_catalog_cache.colors END,
                        updated_at = excluded.updated_at
                    """,
                    (_name_key(cname), cname, rarity, grp_id, colors, now),
                )
                if grp_id > 0:
                    conn.execute(
                        """
                        INSERT INTO card_prints_cache (grp_id, name, rarity)
                        VALUES (?, ?, ?)
                        ON CONFLICT(grp_id) DO UPDATE SET
                            name = excluded.name,
                            rarity = CASE WHEN excluded.rarity != '' THEN excluded.rarity ELSE card_prints_cache.rarity END
                        """,
                        (grp_id, cname, rarity),
                    )
                counts["catalog"] += 1

        # 2. Ranks
        ranks = body.get("ranks")
        if isinstance(ranks, dict):
            c_raw = ranks.get("constructed") if isinstance(ranks.get("constructed"), dict) else ranks
            l_raw = ranks.get("limited") if isinstance(ranks.get("limited"), dict) else ranks

            if isinstance(c_raw, dict) and (c_raw.get("rank_class") or c_raw.get("constructedClass")):
                cls = str(c_raw.get("rank_class") or c_raw.get("constructedClass") or "")[:32]
                tier = _safe_int(c_raw.get("tier") or c_raw.get("constructedLevel"), 0)
                step = _safe_int(c_raw.get("step") or c_raw.get("constructedStep"), 0)
                pct = _safe_opt_float(c_raw.get("mythic_pct", c_raw.get("constructedPercentile")))
                place = _safe_opt_int(c_raw.get("mythic_place", c_raw.get("constructedLeaderboardPlace")))
                score = compute_rank_score(cls, tier, step, pct, place)
                conn.execute(
                    """
                    UPDATE player_profiles
                    SET constructed_rank_class = ?, constructed_rank_tier = ?, constructed_rank_step = ?,
                        constructed_mythic_pct = ?, constructed_mythic_place = ?, constructed_rank_score = ?,
                        updated_at = ?
                    WHERE owner = ?
                    """,
                    (cls, tier, step, pct, place, score, now, owner),
                )
            if isinstance(l_raw, dict) and (l_raw.get("rank_class") or l_raw.get("limitedClass")):
                cls = str(l_raw.get("rank_class") or l_raw.get("limitedClass") or "")[:32]
                tier = _safe_int(l_raw.get("tier") or l_raw.get("limitedLevel"), 0)
                step = _safe_int(l_raw.get("step") or l_raw.get("limitedStep"), 0)
                pct = _safe_opt_float(l_raw.get("mythic_pct", l_raw.get("limitedPercentile")))
                place = _safe_opt_int(l_raw.get("mythic_place", l_raw.get("limitedLeaderboardPlace")))
                score = compute_rank_score(cls, tier, step, pct, place)
                conn.execute(
                    """
                    UPDATE player_profiles
                    SET limited_rank_class = ?, limited_rank_tier = ?, limited_rank_step = ?,
                        limited_mythic_pct = ?, limited_mythic_place = ?, limited_rank_score = ?,
                        updated_at = ?
                    WHERE owner = ?
                    """,
                    (cls, tier, step, pct, place, score, now, owner),
                )

        # 3. Collection & wildcards
        col = body.get("collection")
        if isinstance(col, dict):
            cards_raw = col.get("cards_by_name") if isinstance(col.get("cards_by_name"), dict) else {}
            clean_col: dict[str, int] = {}
            total_copies = 0
            for idx, (cname, qty) in enumerate(cards_raw.items()):
                if idx >= MAX_COLLECTION_CARDS:
                    break
                q = _safe_int(qty, 0)
                clean_name = " ".join(str(cname or "").split())[:120]
                if clean_name and q > 0:
                    clean_col[clean_name] = min(99, q)
                    total_copies += min(99, q)
            wc_raw = col.get("wildcards") if isinstance(col.get("wildcards"), dict) else {}
            clean_wc = {r: max(0, _safe_int(wc_raw.get(r), 0)) for r in RARITIES}
            gold = max(0, _safe_int(col.get("gold"), 0))
            gems = max(0, _safe_int(col.get("gems"), 0))
            if clean_col or wc_raw:
                conn.execute(
                    """
                    UPDATE player_profiles
                    SET wildcards_json = ?, gold = ?, gems = ?,
                        collection_json = CASE WHEN ? > 0 THEN ? ELSE collection_json END,
                        collection_card_count = CASE WHEN ? > 0 THEN ? ELSE collection_card_count END,
                        collection_updated_at = ?, updated_at = ?
                    WHERE owner = ?
                    """,
                    (
                        json.dumps(clean_wc),
                        gold,
                        gems,
                        len(clean_col),
                        json.dumps(clean_col),
                        len(clean_col),
                        total_copies,
                        now,
                        now,
                        owner,
                    ),
                )
                counts["collection"] = len(clean_col) or 1

        # 4. Matches
        matches = body.get("matches")
        if isinstance(matches, list):
            for m in matches[:MAX_MATCHES_PER_SYNC]:
                if not isinstance(m, dict):
                    continue
                mid = str(m.get("match_id") or "").strip()[:120]
                res = str(m.get("result") or "").strip().lower()
                if not mid or res not in ("win", "loss", "draw"):
                    continue
                fmt_name = str(m.get("format_name") or "").strip()[:120]
                fmt_bucket = canonical_format_bucket(fmt_name)
                ap = m.get("autopilot")
                ap_val = None if ap is None else (1 if bool(ap) else 0)
                deck_colors = "".join(
                    c for c in str(m.get("deck_colors") or m.get("local_deck_colors") or "").upper() if c in "WUBRG"
                )[:5]
                opp_colors = "".join(
                    c
                    for c in str(m.get("opponent_colors") or m.get("opponent_colors_seen") or "").upper()
                    if c in "WUBRG"
                )[:5]
                existed = (
                    conn.execute(
                        "SELECT 1 FROM player_matches WHERE owner = ? AND match_id = ?",
                        (owner, mid),
                    ).fetchone()
                    is not None
                )
                cur = conn.execute(
                    """
                    INSERT INTO player_matches (
                        owner, match_id, played_at, result, format_name, format_bucket,
                        deck_identity, deck_name, deck_colors, opponent_colors, turns,
                        autopilot, rank_class, rank_tier
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(owner, match_id) DO UPDATE SET
                        format_name = CASE WHEN excluded.format_name != '' THEN excluded.format_name ELSE player_matches.format_name END,
                        format_bucket = CASE WHEN excluded.format_bucket != 'other' THEN excluded.format_bucket ELSE player_matches.format_bucket END,
                        deck_identity = CASE WHEN excluded.deck_identity != '' THEN excluded.deck_identity ELSE player_matches.deck_identity END,
                        deck_name = CASE WHEN excluded.deck_name != '' THEN excluded.deck_name ELSE player_matches.deck_name END,
                        deck_colors = CASE WHEN excluded.deck_colors != '' THEN excluded.deck_colors ELSE player_matches.deck_colors END,
                        opponent_colors = CASE WHEN excluded.opponent_colors != '' THEN excluded.opponent_colors ELSE player_matches.opponent_colors END,
                        turns = CASE WHEN excluded.turns > 0 THEN excluded.turns ELSE player_matches.turns END,
                        autopilot = COALESCE(excluded.autopilot, player_matches.autopilot),
                        rank_class = CASE WHEN excluded.rank_class != '' THEN excluded.rank_class ELSE player_matches.rank_class END,
                        rank_tier = CASE WHEN excluded.rank_tier > 0 THEN excluded.rank_tier ELSE player_matches.rank_tier END
                    """,
                    (
                        owner,
                        mid,
                        _parse_iso_ts(m.get("played_at") or m.get("timestamp")),
                        res,
                        fmt_name,
                        fmt_bucket,
                        str(m.get("deck_identity") or "").strip()[:128],
                        " ".join(str(m.get("deck_name") or "").split())[:100],
                        deck_colors,
                        opp_colors,
                        max(0, _safe_int(m.get("turns"), 0)),
                        ap_val,
                        str(m.get("rank_class") or "")[:32],
                        _safe_int(m.get("rank_tier"), 0),
                    ),
                )
                if not existed and cur.rowcount > 0:
                    counts["matches"] += 1

        # 5. Arena decks
        decks_in = body.get("decks")
        if isinstance(decks_in, list):
            for d in decks_in[:MAX_DECKS_PER_SYNC]:
                if not isinstance(d, dict):
                    continue
                try:
                    piles = {
                        "main": _sanitize_card_entries(d.get("main") or [], max_cards=250),
                        "sideboard": _sanitize_card_entries(d.get("sideboard") or [], max_cards=100),
                        "commander": _sanitize_card_entries(d.get("commander") or [], max_cards=5),
                        "companion": _sanitize_card_entries(d.get("companion") or [], max_cards=5),
                    }
                except ValueError:
                    continue
                if not piles["main"]:
                    continue
                _upsert_deck_record(
                    conn,
                    owner,
                    name=str(d.get("name") or "Unnamed Deck"),
                    fmt=str(d.get("format") or "standard"),
                    piles=piles,
                    arena_deck_id=str(d.get("arena_deck_id") or d.get("deck_id") or "")[:120],
                    deck_identity=str(d.get("deck_identity") or d.get("identity") or "")[:128],
                    archetype=str(d.get("archetype") or ""),
                    colors=str(d.get("colors") or ""),
                    is_public=bool(d["is_public"]) if "is_public" in d else None,
                    default_public=owner_default_public,
                    source="arena",
                    clear_empty_meta=False,
                )
                counts["decks"] += 1

        refresh_owner_deck_stats(conn, owner)

        pending_rows = conn.execute(
            "SELECT * FROM synced_decks WHERE owner = ? AND sync_to_arena_status = 'pending' ORDER BY sync_requested_at ASC",
            (owner,),
        ).fetchall()
        pending = [_deck_row_to_dict(r) for r in pending_rows]

    if counts["matches"] > 0 or counts["decks"] > 0 or isinstance(body.get("ranks"), dict):
        _notify_discord_feed()
    return JSONResponse({"ok": True, "synced": counts, "pending_arena_decks": pending})


@router.get("/v1/sync/pending-decks")
async def api_pending_decks(request: Request):
    auth = await resolve_auth(request, required=True)
    with _get_db().get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM synced_decks WHERE owner = ? AND sync_to_arena_status = 'pending' ORDER BY sync_requested_at ASC",
            (auth["owner"],),
        ).fetchall()
        decks = [_deck_row_to_dict(r) for r in rows]
    return JSONResponse({"ok": True, "decks": decks}, headers={"Cache-Control": "no-store"})


@router.post("/v1/sync/pending-decks/{deck_id}/ack")
async def api_ack_pending_deck(request: Request, deck_id: str):
    auth = await resolve_auth(request, required=True, check_csrf=True)
    try:
        body = await request.json()
    except Exception:
        body = {}
    status = str((body or {}).get("status") or "synced").strip().lower()
    if status not in ("synced", "failed"):
        raise HTTPException(422, "status must be 'synced' or 'failed'")
    arena_deck_id = str((body or {}).get("arena_deck_id") or "").strip()[:120]
    error = str((body or {}).get("error") or "").strip()[:300]
    now = time.time()
    with _get_db().get_db() as conn:
        cur = conn.execute(
            """
            UPDATE synced_decks
            SET sync_to_arena_status = ?,
                sync_to_arena_error = ?,
                arena_deck_id = CASE WHEN ? != '' THEN ? ELSE arena_deck_id END,
                updated_at = ?
            WHERE id = ? AND owner = ?
            """,
            (status, error, arena_deck_id, arena_deck_id, now, deck_id, auth["owner"]),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Deck not found")
    return JSONResponse({"ok": True, "status": status})


@router.get("/v1/decks")
async def api_my_decks(request: Request):
    auth = await resolve_auth(request, required=True)
    with _get_db().get_db() as conn:
        prof = conn.execute(
            "SELECT collection_json, wildcards_json FROM player_profiles WHERE owner = ?",
            (auth["owner"],),
        ).fetchone()
        collection = json.loads(prof["collection_json"] or "{}") if prof else {}
        wildcards = json.loads(prof["wildcards_json"] or "{}") if prof else {}
        rows = conn.execute(
            "SELECT * FROM synced_decks WHERE owner = ? ORDER BY updated_at DESC",
            (auth["owner"],),
        ).fetchall()
        names = {
            c.get("name", "")
            for r in rows
            for pile in ("main_json", "sideboard_json", "commander_json", "companion_json")
            for c in json.loads(r[pile] or "[]")
            if isinstance(c, dict)
        }
        rarities = _catalog_rarities_for_cards(conn, names)
        decks = [
            _deck_row_to_dict(r, collection=collection, wildcards=wildcards, catalog_rarities=rarities)
            for r in rows
        ]
    return JSONResponse({"ok": True, "decks": decks}, headers={"Cache-Control": "no-store"})


@router.post("/v1/decks")
async def api_create_deck(request: Request):
    auth = await resolve_auth(request, required=True, check_csrf=True)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "Body must be a JSON object")

    try:
        if body.get("import_text"):
            piles = parse_arena_deck_text(str(body.get("import_text") or ""))
        else:
            piles = {
                "main": _sanitize_card_entries(body.get("main") or [], max_cards=250),
                "sideboard": _sanitize_card_entries(body.get("sideboard") or [], max_cards=100),
                "commander": _sanitize_card_entries(body.get("commander") or [], max_cards=5),
                "companion": _sanitize_card_entries(body.get("companion") or [], max_cards=5),
            }
            if not piles["main"]:
                raise ValueError("Main deck cannot be empty")
    except ValueError as exc:
        raise HTTPException(422, str(exc))

    name = str(body.get("name") or "New Deck").strip()
    fmt = str(body.get("format") or "standard").strip()
    with _get_db().get_db() as conn:
        new_id = _upsert_deck_record(
            conn,
            auth["owner"],
            name=name,
            fmt=fmt,
            piles=piles,
            archetype=str(body.get("archetype") or ""),
            colors=str(body.get("colors") or ""),
            description=str(body.get("description") or ""),
            is_public=bool(body.get("is_public", False)),
            source="web_builder",
            queue_for_arena=bool(body.get("queue_for_arena", False)),
        )
        refresh_owner_deck_stats(conn, auth["owner"])
        row = conn.execute("SELECT * FROM synced_decks WHERE id = ?", (new_id,)).fetchone()
        prof = conn.execute(
            "SELECT collection_json, wildcards_json FROM player_profiles WHERE owner = ?",
            (auth["owner"],),
        ).fetchone()
        collection = json.loads(prof["collection_json"] or "{}") if prof else {}
        wildcards = json.loads(prof["wildcards_json"] or "{}") if prof else {}
        deck = _deck_row_to_dict(row, collection=collection, wildcards=wildcards)
    if deck.get("is_public"):
        _notify_discord_feed()
    return JSONResponse({"ok": True, "deck": deck}, status_code=201)


@router.patch("/v1/decks/{deck_id}")
async def api_update_deck(request: Request, deck_id: str):
    auth = await resolve_auth(request, required=True, check_csrf=True)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "Body must be a JSON object")

    with _get_db().get_db() as conn:
        existing = conn.execute(
            "SELECT * FROM synced_decks WHERE id = ? AND owner = ?", (deck_id, auth["owner"])
        ).fetchone()
        if existing is None:
            raise HTTPException(404, "Deck not found")
        try:
            if body.get("import_text"):
                piles = parse_arena_deck_text(str(body.get("import_text") or ""))
            elif any(k in body for k in ("main", "sideboard", "commander", "companion")):
                piles = {
                    "main": _sanitize_card_entries(
                        body.get("main", json.loads(existing["main_json"] or "[]")), max_cards=250
                    ),
                    "sideboard": _sanitize_card_entries(
                        body.get("sideboard", json.loads(existing["sideboard_json"] or "[]")),
                        max_cards=100,
                    ),
                    "commander": _sanitize_card_entries(
                        body.get("commander", json.loads(existing["commander_json"] or "[]")),
                        max_cards=5,
                    ),
                    "companion": _sanitize_card_entries(
                        body.get("companion", json.loads(existing["companion_json"] or "[]")),
                        max_cards=5,
                    ),
                }
                if not piles["main"]:
                    raise ValueError("Main deck cannot be empty")
            else:
                piles = {
                    "main": json.loads(existing["main_json"] or "[]"),
                    "sideboard": json.loads(existing["sideboard_json"] or "[]"),
                    "commander": json.loads(existing["commander_json"] or "[]"),
                    "companion": json.loads(existing["companion_json"] or "[]"),
                }
        except ValueError as exc:
            raise HTTPException(422, str(exc))

        _upsert_deck_record(
            conn,
            auth["owner"],
            deck_id=deck_id,
            name=str(body.get("name", existing["name"])),
            fmt=str(body.get("format", existing["format"])),
            piles=piles,
            archetype=str(body.get("archetype", existing["archetype"])),
            colors=str(body.get("colors", existing["colors"])),
            description=str(body.get("description", existing["description"])),
            is_public=bool(body.get("is_public", existing["is_public"])),
            queue_for_arena=bool(body.get("queue_for_arena", False)),
        )
        refresh_owner_deck_stats(conn, auth["owner"])
        row = conn.execute("SELECT * FROM synced_decks WHERE id = ?", (deck_id,)).fetchone()
        prof = conn.execute(
            "SELECT collection_json, wildcards_json FROM player_profiles WHERE owner = ?",
            (auth["owner"],),
        ).fetchone()
        collection = json.loads(prof["collection_json"] or "{}") if prof else {}
        wildcards = json.loads(prof["wildcards_json"] or "{}") if prof else {}
        deck = _deck_row_to_dict(row, collection=collection, wildcards=wildcards)
    if deck.get("is_public") or bool(existing["is_public"]):
        _notify_discord_feed()
    return JSONResponse({"ok": True, "deck": deck})


@router.delete("/v1/decks/{deck_id}")
async def api_delete_deck(request: Request, deck_id: str):
    auth = await resolve_auth(request, required=True, check_csrf=True)
    with _get_db().get_db() as conn:
        cur = conn.execute(
            "DELETE FROM synced_decks WHERE id = ? AND owner = ?", (deck_id, auth["owner"])
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Deck not found")
    _notify_discord_feed()
    return JSONResponse({"ok": True})


@router.post("/v1/decks/{deck_id}/clone")
async def api_clone_deck(request: Request, deck_id: str):
    auth = await resolve_auth(request, required=True, check_csrf=True)
    queue = False
    try:
        body = await request.json()
        if isinstance(body, dict):
            queue = bool(body.get("queue_for_arena", False))
    except Exception:
        pass
    with _get_db().get_db() as conn:
        src = conn.execute(
            """
            SELECT d.*, COALESCE(p.leaderboard_opt_in, 1) AS author_opt_in
            FROM synced_decks d
            LEFT JOIN player_profiles p ON p.owner = d.owner
            WHERE d.id = ?
            """,
            (deck_id,),
        ).fetchone()
        if src is None:
            raise HTTPException(404, "Deck not found")
        src_owner = str(src["owner"] or "")
        public_ok = bool(src["is_public"]) and (src_owner.startswith("source:") or bool(src["author_opt_in"]))
        if not public_ok and src_owner != auth["owner"]:
            raise HTTPException(404, "Deck not found")
        piles = {
            "main": json.loads(src["main_json"] or "[]"),
            "sideboard": json.loads(src["sideboard_json"] or "[]"),
            "commander": json.loads(src["commander_json"] or "[]"),
            "companion": json.loads(src["companion_json"] or "[]"),
        }
        new_id = _upsert_deck_record(
            conn,
            auth["owner"],
            name=f"{src['name']} (Copy)" if src["owner"] == auth["owner"] else src["name"],
            fmt=src["format"],
            piles=piles,
            archetype=src["archetype"],
            colors=src["colors"],
            description=src["description"],
            is_public=False,
            source="cloned",
            parent_deck_id=src["id"],
            queue_for_arena=queue,
        )
        row = conn.execute("SELECT * FROM synced_decks WHERE id = ?", (new_id,)).fetchone()
        prof = conn.execute(
            "SELECT collection_json, wildcards_json FROM player_profiles WHERE owner = ?",
            (auth["owner"],),
        ).fetchone()
        collection = json.loads(prof["collection_json"] or "{}") if prof else {}
        wildcards = json.loads(prof["wildcards_json"] or "{}") if prof else {}
        deck = _deck_row_to_dict(row, collection=collection, wildcards=wildcards)
    if deck.get("is_public"):
        _notify_discord_feed()
    return JSONResponse({"ok": True, "deck": deck}, status_code=201)


@router.post("/v1/decks/{deck_id}/queue-arena")
async def api_queue_deck_for_arena(request: Request, deck_id: str):
    auth = await resolve_auth(request, required=True, check_csrf=True)
    now = time.time()
    with _get_db().get_db() as conn:
        cur = conn.execute(
            """
            UPDATE synced_decks
            SET sync_to_arena_status = 'pending', sync_to_arena_error = '',
                sync_requested_at = ?, updated_at = ?
            WHERE id = ? AND owner = ?
            """,
            (now, now, deck_id, auth["owner"]),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Deck not found")
    return JSONResponse({"ok": True, "status": "pending"})


@router.get("/v1/decks/discover")
async def api_discover_decks(
    request: Request,
    fmt: str = Query(default="all", alias="format"),
    craftability: str = Query(default="all"),
    sort: str = Query(default="winrate"),
    q: str = Query(default=""),
    limit: int = Query(default=60, ge=1, le=250),
):
    auth = await resolve_auth(request, required=False)
    fmt_clean = fmt.strip().lower()
    craft_filter = craftability.strip().lower()
    sort_key = sort.strip().lower()
    query_text = q.strip().casefold()

    with _get_db().get_db() as conn:
        collection: dict[str, int] = {}
        wildcards: dict[str, int] = {}
        if auth:
            prof = conn.execute(
                "SELECT collection_json, wildcards_json FROM player_profiles WHERE owner = ?",
                (auth["owner"],),
            ).fetchone()
            if prof:
                collection = json.loads(prof["collection_json"] or "{}")
                wildcards = json.loads(prof["wildcards_json"] or "{}")

        sql = """
            SELECT d.*, COALESCE(p.display_name, '') AS author_name,
                   COALESCE(p.is_patron, 0) AS author_is_patron
            FROM synced_decks d
            LEFT JOIN player_profiles p ON p.owner = d.owner
            WHERE d.is_public = 1
              AND (d.owner LIKE 'source:%' OR COALESCE(p.leaderboard_opt_in, 1) = 1)
        """
        params: list[Any] = []
        if fmt_clean and fmt_clean != "all":
            sql += " AND lower(d.format) = ?"
            params.append(fmt_clean)
        rows = conn.execute(sql, params).fetchall()

        names = {
            c.get("name", "")
            for r in rows
            for pile in ("main_json", "sideboard_json", "commander_json", "companion_json")
            for c in json.loads(r[pile] or "[]")
            if isinstance(c, dict)
        }
        rarities = _catalog_rarities_for_cards(conn, names)

        items: list[dict[str, Any]] = []
        for r in rows:
            author_display = r["author_name"] or anonymous_handle(r["owner"])
            is_src = str(r["owner"] or "").startswith("source:")
            deck = _deck_row_to_dict(
                r,
                collection=collection if auth else None,
                wildcards=wildcards if auth else None,
                catalog_rarities=rarities,
                author_name=author_display,
                author_is_patron=bool(r["author_is_patron"]),
            )
            deck["is_source"] = is_src
            deck["author_profile_url"] = "" if is_src else f"/players/{quote(author_display, safe='')}"
            if query_text:
                hay = f"{deck['name']} {deck['archetype']} {deck['author_name']}".casefold()
                if query_text not in hay:
                    continue
            if auth and craft_filter in ("owned", "affordable", "close"):
                cinfo = deck.get("craftability") or {}
                if not cinfo.get(craft_filter):
                    continue
            items.append(deck)

    def _sort_val(d: dict[str, Any]) -> tuple:
        if sort_key == "wildcards":
            cinfo = d.get("craftability") or {}
            return (0 if cinfo.get("owned") else (1 if cinfo.get("affordable") else 2), cinfo.get("extra_wildcards_needed", 999), -(d["games"]))
        if sort_key == "games":
            return (-d["games"], -(d["win_rate"] or 0.0))
        if sort_key == "newest":
            return (-d["updated_at"], -d["games"])
        # winrate (20+ games prioritized, then raw WR)
        qualified = 0 if d["games"] >= 5 else 1
        return (qualified, -(d["win_rate"] or -1.0), -d["games"], -d["updated_at"])

    items.sort(key=_sort_val)
    return JSONResponse({"ok": True, "decks": items[:limit], "authenticated": auth is not None})


@router.get("/v1/cards/search")
async def api_card_search(q: str = Query(default=""), limit: int = Query(default=20, ge=1, le=50)):
    query = q.strip()
    if len(query) < 2:
        return JSONResponse({"ok": True, "cards": []})
    like = f"%{query}%"
    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    for land in sorted(BASIC_LANDS):
        if query.casefold() in land.casefold():
            seen.add(_name_key(land))
            results.append({"name": land, "rarity": "common", "grp_id": 0})

    with _get_db().get_db() as conn:
        rows = conn.execute(
            "SELECT name, rarity, grp_id FROM card_catalog_cache WHERE name LIKE ? ORDER BY length(name) ASC, name ASC LIMIT ?",
            (like, limit),
        ).fetchall()
        for r in rows:
            k = _name_key(r["name"])
            if k not in seen:
                seen.add(k)
                results.append({"name": r["name"], "rarity": r["rarity"] or "", "grp_id": int(r["grp_id"] or 0)})
        if len(results) < limit:
            cf_rows = conn.execute(
                "SELECT DISTINCT name FROM card_facts WHERE name LIKE ? ORDER BY length(name) ASC LIMIT ?",
                (like, limit - len(results)),
            ).fetchall()
            for r in cf_rows:
                k = _name_key(r["name"])
                if k not in seen and r["name"]:
                    seen.add(k)
                    results.append({"name": r["name"], "rarity": "", "grp_id": 0})

    return JSONResponse({"ok": True, "cards": results[:limit]})


def compute_leaderboard_entries(
    conn,
    *,
    tab: str = "rank",
    fmt: str = "all",
    mode: str = "all",
    window: str = "30d",
    limit: int = 50,
    auth_owner: Optional[str] = None,
    include_owner: bool = False,
) -> dict[str, Any]:
    """Compute public-safe leaderboard rows (and optional caller standing)."""
    tab_clean = tab.strip().lower()
    if tab_clean not in ("rank", "winrate", "decks"):
        tab_clean = "rank"
    fmt_clean = fmt.strip().lower()
    if fmt_clean not in ("all", *FORMAT_BUCKETS):
        fmt_clean = "all"
    mode_clean = mode.strip().lower()
    if mode_clean not in ("all", "autopilot", "manual"):
        mode_clean = "all"
    win_clean = window.strip().lower()
    since = 0.0
    if win_clean == "7d":
        since = time.time() - 7 * 86400.0
    elif win_clean == "30d":
        since = time.time() - 30 * 86400.0
    else:
        win_clean = "all"

    entries: list[dict[str, Any]] = []
    my_standing: Optional[dict[str, Any]] = None

    if tab_clean == "decks":
        sql = """
            SELECT d.*, COALESCE(p.display_name, '') AS author_name,
                   COALESCE(p.is_patron, 0) AS author_is_patron
            FROM synced_decks d
            LEFT JOIN player_profiles p ON p.owner = d.owner
            WHERE d.is_public = 1
              AND (d.wins + d.losses) > 0
              AND (d.owner LIKE 'source:%' OR COALESCE(p.leaderboard_opt_in, 1) = 1)
        """
        params: list[Any] = []
        if fmt_clean != "all":
            sql += " AND lower(d.format) = ?"
            params.append(fmt_clean)
        rows = conn.execute(sql, params).fetchall()
        collection: dict[str, int] = {}
        wildcards: dict[str, int] = {}
        if auth_owner:
            prof = conn.execute(
                "SELECT collection_json, wildcards_json FROM player_profiles WHERE owner = ?",
                (auth_owner,),
            ).fetchone()
            if prof:
                collection = json.loads(prof["collection_json"] or "{}")
                wildcards = json.loads(prof["wildcards_json"] or "{}")
        names = {
            c.get("name", "")
            for r in rows
            for pile in ("main_json", "sideboard_json", "commander_json", "companion_json")
            for c in json.loads(r[pile] or "[]")
            if isinstance(c, dict)
        }
        rarities = _catalog_rarities_for_cards(conn, names)
        for r in rows:
            author_display = r["author_name"] or anonymous_handle(r["owner"])
            is_src = str(r["owner"] or "").startswith("source:")
            d = _deck_row_to_dict(
                r,
                collection=collection if auth_owner else None,
                wildcards=wildcards if auth_owner else None,
                catalog_rarities=rarities,
                author_name=author_display,
                author_is_patron=bool(r["author_is_patron"]),
            )
            d["is_source"] = is_src
            d["author_profile_url"] = "" if is_src else f"/players/{quote(author_display, safe='')}"
            if mode_clean == "autopilot":
                g = d["autopilot_games"]
                w = d["autopilot_wins"]
                l = d["autopilot_losses"]
                wr = d["autopilot_win_rate"]
            elif mode_clean == "manual":
                w = max(0, d["wins"] - d["autopilot_wins"])
                l = max(0, d["losses"] - d["autopilot_losses"])
                g = w + l
                wr = round(w / g, 4) if g > 0 else None
            else:
                g = d["games"]
                w = d["wins"]
                l = d["losses"]
                wr = d["win_rate"]
            if g <= 0:
                continue
            d["display_games"] = g
            d["display_wins"] = w
            d["display_losses"] = l
            d["display_win_rate"] = wr
            entries.append(d)
        entries.sort(
            key=lambda x: (
                0 if x["display_games"] >= MIN_DECK_WINRATE_GAMES else 1,
                -(x["display_win_rate"] or 0.0),
                -x["display_games"],
            )
        )
        for idx, item in enumerate(entries[:limit], start=1):
            item["position"] = idx
        entries = entries[:limit]
    else:
        profiles_rows = conn.execute(
            """
            SELECT owner, display_name, is_patron,
                   constructed_rank_class, constructed_rank_tier, constructed_rank_step,
                   constructed_mythic_pct, constructed_mythic_place, constructed_rank_score,
                   limited_rank_class, limited_rank_tier, limited_rank_step,
                   limited_mythic_pct, limited_mythic_place, limited_rank_score
            FROM player_profiles
            WHERE leaderboard_opt_in = 1 AND owner NOT LIKE 'source:%'
            """
        ).fetchall()

        match_sql = """
            SELECT owner, result, format_bucket, autopilot, deck_name
            FROM player_matches
            WHERE result IN ('win', 'loss') AND played_at >= ?
        """
        m_params: list[Any] = [since]
        if fmt_clean != "all":
            match_sql += " AND format_bucket = ?"
            m_params.append(fmt_clean)
        if mode_clean == "autopilot":
            match_sql += " AND autopilot = 1"
        elif mode_clean == "manual":
            match_sql += " AND (autopilot IS NULL OR autopilot = 0)"
        match_rows = conn.execute(match_sql, m_params).fetchall()

        agg: dict[str, dict[str, Any]] = {}
        for m in match_rows:
            slot = agg.setdefault(m["owner"], {"wins": 0, "losses": 0, "decks": Counter()})
            if m["result"] == "win":
                slot["wins"] += 1
            else:
                slot["losses"] += 1
            if m["deck_name"]:
                slot["decks"][m["deck_name"]] += 1

        public_decks_by_owner: dict[str, list[tuple[str, str, int, int]]] = {}
        for dr in conn.execute(
            """
            SELECT owner, name, slug, wins, losses
            FROM synced_decks
            WHERE is_public = 1 AND owner NOT LIKE 'source:%'
            ORDER BY (wins + losses) DESC, updated_at DESC
            """
        ).fetchall():
            public_decks_by_owner.setdefault(dr["owner"], []).append(
                (
                    str(dr["name"] or ""),
                    str(dr["slug"] or ""),
                    int(dr["wins"] or 0),
                    int(dr["losses"] or 0),
                )
            )

        is_limited = fmt_clean == "limited"
        ranked_rows: list[dict[str, Any]] = []
        for p in profiles_rows:
            owner = p["owner"]
            stats = agg.get(owner, {"wins": 0, "losses": 0, "decks": Counter()})
            wins = stats["wins"]
            losses = stats["losses"]
            games = wins + losses
            wr = round(wins / games, 4) if games > 0 else None

            if is_limited:
                r_score = float(p["limited_rank_score"] or 0.0)
                r_label = format_rank_label(
                    p["limited_rank_class"],
                    int(p["limited_rank_tier"] or 0),
                    int(p["limited_rank_step"] or 0),
                    p["limited_mythic_pct"],
                    p["limited_mythic_place"],
                )
            else:
                r_score = float(p["constructed_rank_score"] or 0.0)
                r_label = format_rank_label(
                    p["constructed_rank_class"],
                    int(p["constructed_rank_tier"] or 0),
                    int(p["constructed_rank_step"] or 0),
                    p["constructed_mythic_pct"],
                    p["constructed_mythic_place"],
                )

            if tab_clean == "rank" and r_score <= 0.0 and games <= 0:
                continue
            if tab_clean == "winrate" and games < MIN_WINRATE_GAMES:
                continue

            top_deck = stats["decks"].most_common(1)[0][0] if stats["decks"] else ""
            owner_decks = public_decks_by_owner.get(owner) or []
            matched_deck = next((d for d in owner_decks if d[0].lower() == top_deck.lower()), None)
            if matched_deck is None and not top_deck and owner_decks:
                matched_deck = owner_decks[0]
                top_deck = matched_deck[0]
            top_slug = matched_deck[1] if matched_deck else ""
            d_wins = matched_deck[2] if matched_deck else 0
            d_losses = matched_deck[3] if matched_deck else 0
            d_games = d_wins + d_losses
            handle_display = str(p["display_name"] or "").strip() or anonymous_handle(owner)
            row_entry = {
                "handle": handle_display,
                "profile_url": f"/players/{quote(handle_display, safe='')}",
                "is_patron": bool(p["is_patron"]),
                "is_me": bool(auth_owner and auth_owner == owner),
                "rank_label": r_label,
                "rank_score": r_score,
                "wins": wins,
                "losses": losses,
                "games": games,
                "win_rate": wr,
                "top_deck": top_deck,
                "top_deck_slug": top_slug,
                "top_deck_games": d_games,
                "top_deck_win_rate": round(d_wins / d_games, 4) if d_games > 0 else None,
            }
            if include_owner:
                row_entry["owner"] = owner
            ranked_rows.append(row_entry)

        if tab_clean == "rank":
            ranked_rows.sort(
                key=lambda r: (-r["rank_score"], -(r["win_rate"] or 0.0), -r["games"], r["handle"].lower())
            )
        else:
            ranked_rows.sort(
                key=lambda r: (-(r["win_rate"] or 0.0), -r["games"], -r["rank_score"], r["handle"].lower())
            )

        for idx, item in enumerate(ranked_rows, start=1):
            item["position"] = idx
            if item["is_me"]:
                my_standing = item
        entries = ranked_rows[:limit]

    if auth_owner and my_standing is None:
        me = conn.execute(
            "SELECT display_name, leaderboard_opt_in, is_patron, constructed_rank_class, "
            "constructed_rank_tier, constructed_rank_step, constructed_mythic_pct, constructed_mythic_place, "
            "limited_rank_class, limited_rank_tier, limited_rank_step, limited_mythic_pct, limited_mythic_place "
            "FROM player_profiles WHERE owner = ?",
            (auth_owner,),
        ).fetchone()
        if me:
            is_lim = fmt_clean == "limited"
            my_standing = {
                "handle": str(me["display_name"] or "").strip() or anonymous_handle(auth_owner),
                "leaderboard_opt_in": bool(me["leaderboard_opt_in"]),
                "is_patron": bool(me["is_patron"]),
                "rank_label": format_rank_label(
                    me["limited_rank_class"] if is_lim else me["constructed_rank_class"],
                    int((me["limited_rank_tier"] if is_lim else me["constructed_rank_tier"]) or 0),
                    int((me["limited_rank_step"] if is_lim else me["constructed_rank_step"]) or 0),
                    me["limited_mythic_pct"] if is_lim else me["constructed_mythic_pct"],
                    me["limited_mythic_place"] if is_lim else me["constructed_mythic_place"],
                ),
                "position": None,
            }

    return {
        "tab": tab_clean,
        "format": fmt_clean,
        "mode": mode_clean,
        "window": win_clean,
        "entries": entries,
        "my_standing": my_standing,
    }


@router.get("/v1/leaderboard")
async def api_leaderboard(
    request: Request,
    tab: str = Query(default="rank"),
    fmt: str = Query(default="all", alias="format"),
    mode: str = Query(default="all"),
    window: str = Query(default="30d"),
    limit: int = Query(default=50, ge=1, le=100),
):
    auth = await resolve_auth(request, required=False)
    with _get_db().get_db() as conn:
        data = compute_leaderboard_entries(
            conn,
            tab=tab,
            fmt=fmt,
            mode=mode,
            window=window,
            limit=limit,
            auth_owner=auth["owner"] if auth else None,
            include_owner=False,
        )

    return JSONResponse(
        {
            "ok": True,
            **data,
            "min_winrate_games": MIN_WINRATE_GAMES,
            "discord_invite_url": get_discord_invite_url(),
            "updated_at": time.time(),
        }
    )


# ---------------------------------------------------------------------------
# Historical backfill & live match-bundle mirroring
# ---------------------------------------------------------------------------


_PLAYBOOK_PREFIX_RE = re.compile(r"^\s*DECK\s+PLAYBOOK(?:\s+v\d+)?\s*:\s*", re.IGNORECASE)


def _clean_bundle_deck_name(raw: Optional[str]) -> str:
    text = _PLAYBOOK_PREFIX_RE.sub("", str(raw or "").strip())
    return " ".join(text.split())[:100]


def record_match_from_bundle(
    *,
    owner: str,
    match_id: str,
    received_at: float,
    result: str,
    format_name: Optional[str],
    deck_name: Optional[str],
    autopilot_enabled: Optional[bool],
    auth_source: str = "",
) -> None:
    """Mirror a match_bundle upload into player_matches so older clients stay synced."""
    res = str(result or "").strip().lower()
    if not owner or not match_id or res not in ("win", "loss", "draw"):
        return
    init_db()
    fmt = str(format_name or "").strip()[:120]
    dname = _clean_bundle_deck_name(deck_name)
    ap_val = None if autopilot_enabled is None else (1 if bool(autopilot_enabled) else 0)
    with _get_db().get_db() as conn:
        ensure_profile(conn, owner, auth_source=auth_source)
        conn.execute(
            """
            INSERT OR IGNORE INTO player_matches (
                owner, match_id, played_at, result, format_name, format_bucket,
                deck_identity, deck_name, deck_colors, opponent_colors, turns,
                autopilot, rank_class, rank_tier
            ) VALUES (?, ?, ?, ?, ?, ?, '', ?, '', '', 0, ?, '', 0)
            """,
            (owner, match_id[:120], float(received_at), res, fmt, canonical_format_bucket(fmt), dname, ap_val),
        )
        refresh_owner_deck_stats(conn, owner)
    _notify_discord_feed()


def _bundle_deck_identity(deck_cards: list[int], commanders: list[int]) -> str:
    """Matches arenamcp.deck_strategy.deck_identity when card_prints_cache has grpIds."""
    parts = [f"m:{gid}" for gid in sorted(deck_cards)]
    parts.extend(f"c:{gid}" for gid in sorted(commanders))
    return _sha256_hex("|".join(parts)) if parts else ""


def backfill_from_match_bundles(*, extract_decks: bool = True, max_bundles_per_owner: int = 40) -> dict[str, int]:
    """Idempotently backfill player_profiles, player_matches, and private synced_decks.

    Backfilled profiles appear on the leaderboard under anonymous handles by
    default unless opted out; extracted personal decks remain private
    (is_public = 0) until explicitly shared. Safe to run repeatedly on live DBs.
    """
    init_db()
    counts = {"profiles": 0, "matches_inserted": 0, "decks_created": 0}
    touched_owners: set[str] = set()

    with _get_db().get_db() as conn:
        # 1. Ensure profiles for all active subscribers + all match_bundles owners
        sub_rows = conn.execute(
            "SELECT license_key, status FROM subscribers WHERE status IN ('active', 'trial')"
        ).fetchall()
        for sr in sub_rows:
            owner = match_bundles.owner_of(sr["license_key"])
            before = conn.execute("SELECT 1 FROM player_profiles WHERE owner = ?", (owner,)).fetchone()
            ensure_profile(conn, owner, auth_source="subscriber")
            if before is None:
                counts["profiles"] += 1
            touched_owners.add(owner)

        mb_owners = conn.execute("SELECT DISTINCT owner, auth_source FROM match_bundles").fetchall()
        for mr in mb_owners:
            owner = mr["owner"]
            before = conn.execute("SELECT 1 FROM player_profiles WHERE owner = ?", (owner,)).fetchone()
            ensure_profile(conn, owner, auth_source=str(mr["auth_source"] or ""))
            if before is None:
                counts["profiles"] += 1
            touched_owners.add(owner)

        # 2. Backfill all match_bundles rows into player_matches
        bundles = conn.execute(
            """
            SELECT owner, match_id, received_at, result, format_name, deck_name, autopilot_enabled
            FROM match_bundles
            WHERE lower(COALESCE(result, '')) IN ('win', 'loss', 'draw')
            """
        ).fetchall()
        for b in bundles:
            fmt = str(b["format_name"] or "").strip()[:120]
            dname = _clean_bundle_deck_name(b["deck_name"])
            ap = b["autopilot_enabled"]
            ap_val = None if ap is None else (1 if int(ap) else 0)
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO player_matches (
                    owner, match_id, played_at, result, format_name, format_bucket,
                    deck_identity, deck_name, deck_colors, opponent_colors, turns,
                    autopilot, rank_class, rank_tier
                ) VALUES (?, ?, ?, ?, ?, ?, '', ?, '', '', 0, ?, '', 0)
                """,
                (
                    b["owner"],
                    str(b["match_id"])[:120],
                    float(b["received_at"] or time.time()),
                    str(b["result"]).lower(),
                    fmt,
                    canonical_format_bucket(fmt),
                    dname,
                    ap_val,
                ),
            )
            if cur.rowcount > 0:
                counts["matches_inserted"] += 1

        # 3. Extract decks from newest distinct bundles per owner when print map exists
        if extract_decks:
            prints = {
                int(r["grp_id"]): (str(r["name"]), str(r["rarity"] or ""))
                for r in conn.execute("SELECT grp_id, name, rarity FROM card_prints_cache").fetchall()
            }
            if prints:
                root = match_bundles.bundle_root().resolve()
                # Pick newest bundle for each distinct (owner, format_name, deck_name)
                cand_rows = conn.execute(
                    """
                    SELECT owner, path, format_name, deck_name, MAX(received_at) AS newest
                    FROM match_bundles
                    WHERE path IS NOT NULL AND path != ''
                    GROUP BY owner, COALESCE(format_name, ''), COALESCE(deck_name, '')
                    ORDER BY owner ASC, newest DESC
                    """
                ).fetchall()
                per_owner_seen: Counter[str] = Counter()
                existing_idents_by_owner: dict[str, set[str]] = {}
                for dr in conn.execute("SELECT owner, deck_identity FROM synced_decks WHERE deck_identity != ''").fetchall():
                    existing_idents_by_owner.setdefault(dr["owner"], set()).add(dr["deck_identity"])

                for row in cand_rows:
                    owner = row["owner"]
                    if per_owner_seen[owner] >= max_bundles_per_owner:
                        continue
                    per_owner_seen[owner] += 1
                    fpath = (root / str(row["path"])).resolve()
                    if root not in fpath.parents or not fpath.is_file():
                        continue
                    try:
                        with gzip.open(fpath, "rt", encoding="utf-8") as fh:
                            payload = json.load(fh)
                    except Exception:
                        continue
                    fs = payload.get("final_state")
                    if not isinstance(fs, dict):
                        continue
                    raw_deck_cards = [int(g) for g in (fs.get("deck_cards") or []) if str(g).isdigit() and int(g) > 0]
                    raw_cmd = [int(g) for g in (fs.get("commander_grp_ids") or []) if str(g).isdigit() and int(g) > 0]
                    if len(raw_deck_cards) < 40:
                        continue
                    ident = _bundle_deck_identity(raw_deck_cards, raw_cmd)
                    known_idents = existing_idents_by_owner.setdefault(owner, set())
                    if not ident or ident in known_idents:
                        continue

                    main_counts: Counter[int] = Counter(raw_deck_cards)
                    cmd_counts: Counter[int] = Counter(raw_cmd)
                    main_entries: list[dict[str, Any]] = []
                    for gid, qty in main_counts.items():
                        info = prints.get(gid)
                        if not info:
                            continue
                        main_entries.append({"name": info[0], "count": qty, "grp_id": gid, "rarity": info[1]})
                    if sum(e["count"] for e in main_entries) < 40:
                        continue
                    cmd_entries: list[dict[str, Any]] = []
                    for gid, qty in cmd_counts.items():
                        info = prints.get(gid)
                        if info:
                            cmd_entries.append({"name": info[0], "count": qty, "grp_id": gid, "rarity": info[1]})

                    fmt_raw = str(fs.get("format_name") or row["format_name"] or "standard")
                    dname = _clean_bundle_deck_name(fs.get("deck_name") or row["deck_name"]) or f"{normalize_deck_format(fmt_raw).title()} Deck"
                    opt_row = conn.execute(
                        "SELECT leaderboard_opt_in FROM player_profiles WHERE owner = ?", (owner,)
                    ).fetchone()
                    owner_opt_in = bool(opt_row["leaderboard_opt_in"]) if opt_row is not None else True
                    _upsert_deck_record(
                        conn,
                        owner,
                        name=dname,
                        fmt=fmt_raw,
                        piles={"main": main_entries, "sideboard": [], "commander": cmd_entries, "companion": []},
                        deck_identity=ident,
                        is_public=None,
                        default_public=owner_opt_in,
                        source="bundle_backfill",
                        clear_empty_meta=False,
                    )
                    known_idents.add(ident)
                    counts["decks_created"] += 1

        for owner in touched_owners:
            refresh_owner_deck_stats(conn, owner)
    if touched_owners:
        _notify_discord_feed()

    return counts


def _fold_deck_label(name: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", _name_key(name)))


def upsert_sourced_decks(decks: list[dict[str, Any]]) -> dict[str, int]:
    """Idempotently seed/update public metagame decks from Coach internet sources.

    Mirrors public deck lists from AetherHub, MTGGoldfish, MTGTop8, and Moxfield
    into Discover Community Decks under dedicated ``source:<site>`` system
    profiles (excluded from the player rank/win-rate leaderboard). Attaches real
    fleet-wide ``player_matches`` W/L only when >= MIN_WINRATE_GAMES exist for
    the matching (format, deck_name); never invents win rates.
    """
    init_db()
    stats = {"created": 0, "updated": 0, "skipped": 0, "total_public_sourced": 0}
    if not decks:
        return stats

    with _get_db().get_db() as conn:
        # Aggregate real fleet match W/L by (format_bucket, folded_deck_name)
        fleet_wl: dict[tuple[str, str], list[int]] = {}
        for mr in conn.execute(
            "SELECT format_bucket, deck_name, result, autopilot FROM player_matches WHERE result IN ('win', 'loss')"
        ).fetchall():
            dkey = _fold_deck_label(mr["deck_name"] or "")
            if not dkey:
                continue
            slot = fleet_wl.setdefault((str(mr["format_bucket"] or "other"), dkey), [0, 0, 0, 0])
            is_win = mr["result"] == "win"
            slot[0 if is_win else 1] += 1
            if mr["autopilot"]:
                slot[2 if is_win else 3] += 1

        now = time.time()
        for raw in decks:
            if not isinstance(raw, dict):
                stats["skipped"] += 1
                continue
            name = " ".join(str(raw.get("name") or "").split())[:100]
            if not name:
                stats["skipped"] += 1
                continue
            site_label = " ".join(str(raw.get("source_site") or raw.get("source") or "Meta").split())[:32]
            site_slug = re.sub(r"[^a-z0-9]+", "", site_label.lower()) or "meta"
            sys_owner = f"source:{site_slug}"

            prof = ensure_profile(conn, sys_owner, default_opt_in=False)
            if prof.get("display_name") != site_label or int(prof.get("leaderboard_opt_in") or 0) != 0:
                conn.execute(
                    "UPDATE player_profiles SET display_name = ?, leaderboard_opt_in = 0, updated_at = ? WHERE owner = ?",
                    (site_label, now, sys_owner),
                )

            piles: dict[str, list[dict[str, Any]]] | None = None
            export_txt = str(raw.get("export") or raw.get("bo1_export") or "").strip()
            if isinstance(raw.get("main"), list) and raw.get("main"):
                try:
                    piles = {
                        "main": _sanitize_card_entries(raw.get("main") or [], max_cards=250),
                        "sideboard": _sanitize_card_entries(raw.get("sideboard") or [], max_cards=100),
                        "commander": _sanitize_card_entries(raw.get("commander") or [], max_cards=5),
                        "companion": _sanitize_card_entries(raw.get("companion") or [], max_cards=5),
                    }
                except ValueError:
                    piles = None
            if (piles is None or not piles["main"]) and export_txt:
                try:
                    piles = parse_arena_deck_text(export_txt)
                except ValueError:
                    piles = None
            if not piles or not piles.get("main"):
                stats["skipped"] += 1
                continue

            fmt = normalize_deck_format(str(raw.get("format") or "standard"))
            url = str(raw.get("url") or "").strip()
            colors = "".join(c for c in str(raw.get("colors") or "").upper() if c in "WUBRG")[:5]
            source_label = " ".join(str(raw.get("source") or site_label).split())[:80]
            desc = f"Sourced from {source_label}" + (f" ({url})" if url else "")
            ext_id = f"{site_slug}:{_sha256_hex(url or f'{fmt}:{name}')[:20]}"

            existed = conn.execute(
                "SELECT id FROM synced_decks WHERE owner = ? AND arena_deck_id = ?",
                (sys_owner, ext_id),
            ).fetchone()
            deck_id = _upsert_deck_record(
                conn,
                sys_owner,
                name=name,
                fmt=fmt,
                piles=piles,
                arena_deck_id=ext_id,
                archetype=source_label,
                colors=colors,
                description=desc[:1000],
                is_public=True,
                source=f"meta:{site_slug}",
                clear_empty_meta=False,
            )

            wl = fleet_wl.get((canonical_format_bucket(fmt), _fold_deck_label(name)))
            if wl and (wl[0] + wl[1]) >= MIN_WINRATE_GAMES:
                conn.execute(
                    """
                    UPDATE synced_decks
                    SET wins = ?, losses = ?, autopilot_wins = ?, autopilot_losses = ?
                    WHERE id = ?
                    """,
                    (wl[0], wl[1], wl[2], wl[3], deck_id),
                )
            else:
                conn.execute(
                    "UPDATE synced_decks SET wins = 0, losses = 0, autopilot_wins = 0, autopilot_losses = 0 WHERE id = ?",
                    (deck_id,),
                )

            if existed is not None:
                stats["updated"] += 1
            else:
                stats["created"] += 1

        stats["total_public_sourced"] = int(
            conn.execute(
                "SELECT COUNT(*) FROM synced_decks WHERE is_public = 1 AND owner LIKE 'source:%'"
            ).fetchone()[0]
        )

    return stats
