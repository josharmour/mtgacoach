"""Scheduled ingestion of open competitive MTG decks & MTGOFormatData archetypes.

Pulls structured data without browser scraping or bypasses:
1. ``fbettega/MTG_decklistcache`` (GitHub JSON dumps from MTGO, Melee, TopDeck)
2. ``Badaro/MTGOFormatData`` (archetype rule classification for Standard / Pioneer)
3. ``mtgazone.com`` tier lists (Timeless, Alchemy, Historic, Explorer)
4. ``metaconfluence.com`` tier lists + embedded Moxfield v3 JSON (Timeless, Brawl)
5. ``archidekt.com`` public REST API (Alchemy, Brawl, Explorer, Standard)
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Optional

import httpx
import profiles
import state
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

logger = logging.getLogger("website.meta_sync")

router = APIRouter()

META_SYNC_INTERVAL_S = 86400.0
ARCHETYPE_RULE_TTL_S = 86400.0
DEFAULT_HTTP_UA = "mtgacoach-meta-sync/1.0 (+https://mtgacoach.com)"
MOXFIELD_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_DECK_SLUG_RE = re.compile(r"[^a-z0-9]+")

MTGAZONE_TIER_URLS: tuple[tuple[str, str, str], ...] = (
    ("timeless", "https://mtgazone.com/timeless-bo1-metagame-tier-list/", "Timeless Bo1"),
    ("timeless", "https://mtgazone.com/timeless-bo3-metagame-tier-list/", "Timeless Bo3"),
    ("alchemy", "https://mtgazone.com/alchemy-bo1-metagame-tier-list/", "Alchemy Bo1"),
    ("alchemy", "https://mtgazone.com/alchemy-bo3-metagame-tier-list/", "Alchemy Bo3"),
    ("historic", "https://mtgazone.com/historic-bo1-metagame-tier-list/", "Historic Bo1"),
    ("explorer", "https://mtgazone.com/explorer-bo1-metagame-tier-list/", "Explorer Bo1"),
)

METACONFLUENCE_TIER_PAGES: tuple[tuple[str, str, str], ...] = (
    ("timeless", "https://metaconfluence.com/timeless-tier-list/", "/timeless-deck-list/"),
    ("brawl", "https://metaconfluence.com/competitive-brawl-tier-list/", "/brawl-deck-list/"),
)

ARCHIDEKT_FORMATS: tuple[tuple[int, str, int], ...] = (
    (18, "alchemy", 60),
    (20, "brawl", 98),
    (13, "brawl", 59),
    (19, "explorer", 60),
    (1, "standard", 60),
)

_COLOR_NAMES: dict[str, str] = {
    "": "Colorless",
    "W": "Mono-White",
    "U": "Mono-Blue",
    "B": "Mono-Black",
    "R": "Mono-Red",
    "G": "Mono-Green",
    "WU": "Azorius",
    "UB": "Dimir",
    "BR": "Rakdos",
    "RG": "Gruul",
    "WG": "Selesnya",
    "WB": "Orzhov",
    "UR": "Izzet",
    "BG": "Golgari",
    "WR": "Boros",
    "UG": "Simic",
    "WUB": "Esper",
    "UBR": "Grixis",
    "BRG": "Jund",
    "WRG": "Naya",
    "WUG": "Bant",
    "WBG": "Abzan",
    "WUR": "Jeskai",
    "UBG": "Sultai",
    "WBR": "Mardu",
    "URG": "Temur",
}

_COLOR_NAME_ALIASES: dict[str, str] = {
    "W": "White",
    "U": "Blue",
    "B": "Black",
    "R": "Red",
    "G": "Green",
}


def init_db() -> None:
    """Create archetype-rule and scheduled meta-sync tables."""
    with state._get_db().get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta_archetype_rules (
                format TEXT PRIMARY KEY,
                rules_json TEXT NOT NULL,
                card_colors_json TEXT NOT NULL DEFAULT '{}',
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS meta_sync_state (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            """
        )


def _parse_jsonc(text: str) -> Any:
    """Parse JSON that may contain trailing commas (common in MTGOFormatData)."""
    cleaned = _TRAILING_COMMA_RE.sub(r"\1", text or "")
    return json.loads(cleaned)


def _github_headers() -> dict[str, str]:
    headers = {
        "User-Agent": DEFAULT_HTTP_UA,
        "Accept": "application/vnd.github+json",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _fold_name(name: str) -> str:
    return " ".join(str(name or "").strip().casefold().split())


def _ordered_colors(colors: str | set[str] | list[str]) -> str:
    raw = "".join(str(c).upper() for c in colors) if isinstance(colors, (list, set)) else str(colors).upper()
    return "".join(c for c in "WUBRG" if c in raw)


def color_label(colors: str) -> str:
    ordered = _ordered_colors(colors)
    if not ordered:
        return "Colorless"
    if ordered in _COLOR_NAMES:
        return _COLOR_NAMES[ordered]
    if len(ordered) == 4:
        return "4c"
    if len(ordered) >= 5:
        return "5c"
    return _COLOR_NAME_ALIASES.get(ordered, ordered)


# ---------------------------------------------------------------------------
# Badaro/MTGOFormatData classifier
# ---------------------------------------------------------------------------


def _eval_condition(
    cond: dict[str, Any],
    main_counts: Counter[str],
    side_counts: Counter[str],
) -> bool:
    ctype = str(cond.get("Type") or "").strip()
    cards = [_fold_name(c) for c in (cond.get("Cards") or []) if str(c).strip()]
    if not ctype or not cards:
        return True

    combined = main_counts + side_counts
    if ctype == "InMainboard":
        return all(main_counts.get(c, 0) >= 1 for c in cards)
    if ctype == "InSideboard":
        return all(side_counts.get(c, 0) >= 1 for c in cards)
    if ctype == "InMainOrSideboard":
        return all(combined.get(c, 0) >= 1 for c in cards)
    if ctype == "OneOrMoreInMainboard":
        return any(main_counts.get(c, 0) >= 1 for c in cards)
    if ctype == "TwoOrMoreInMainboard":
        return sum(1 for c in cards if main_counts.get(c, 0) >= 1) >= 2
    if ctype == "OneOrMoreInSideboard":
        return any(side_counts.get(c, 0) >= 1 for c in cards)
    if ctype == "OneOrMoreInMainOrSideboard":
        return any(combined.get(c, 0) >= 1 for c in cards)
    if ctype == "DoesNotContain":
        return all(combined.get(c, 0) == 0 for c in cards)
    if ctype == "DoesNotContainMainboard":
        return all(main_counts.get(c, 0) == 0 for c in cards)
    if ctype == "DoesNotContainSideboard":
        return all(side_counts.get(c, 0) == 0 for c in cards)
    return True


def _matches_conditions(
    conditions: list[dict[str, Any]],
    main_counts: Counter[str],
    side_counts: Counter[str],
) -> bool:
    return all(_eval_condition(c, main_counts, side_counts) for c in (conditions or []) if isinstance(c, dict))


class MTGOFormatDataClassifier:
    """Rules-based deck archetype classifier backed by Badaro/MTGOFormatData."""

    FORMAT_DIR_MAP = {
        "standard": "Standard",
        "explorer": "Pioneer",
        "pioneer": "Pioneer",
    }

    def __init__(
        self,
        rules_by_format: dict[str, list[dict[str, Any]]] | None = None,
        card_colors: dict[str, str] | None = None,
    ) -> None:
        self.rules_by_format = dict(rules_by_format or {})
        self.card_colors = {_fold_name(k): _ordered_colors(v) for k, v in (card_colors or {}).items()}

    def infer_colors(
        self,
        main_entries: list[dict[str, Any]],
        side_entries: list[dict[str, Any]] | None = None,
        *,
        catalog_colors: dict[str, str] | None = None,
    ) -> str:
        seen: set[str] = set()
        for entry in list(main_entries or []) + list(side_entries or []):
            name = _fold_name(entry.get("name") or entry.get("CardName") or "")
            if not name:
                continue
            col = self.card_colors.get(name) or (catalog_colors or {}).get(name, "")
            if col and len(col) <= 2:
                # Ignore 5c rainbow lands unless the deck truly needs all colors;
                # nonlands and 1-2 color lands capture primary color identity cleanly.
                for ch in col:
                    if ch in "WUBRG":
                        seen.add(ch)
            elif col and name not in {
                "command tower",
                "mana confluence",
                "city of brass",
                "spire of industry",
                "fabled passage",
                "evolving wilds",
                "terramorphic expanse",
            }:
                for ch in col:
                    if ch in "WUBRG":
                        seen.add(ch)
        return _ordered_colors(seen)

    def classify(
        self,
        fmt: str,
        main_entries: list[dict[str, Any]],
        side_entries: list[dict[str, Any]] | None = None,
        *,
        explicit_colors: str = "",
        catalog_colors: dict[str, str] | None = None,
    ) -> tuple[str, str]:
        """Return (archetype_name, colors_string) for a decklist."""
        norm_fmt = profiles.normalize_deck_format(fmt)
        colors = _ordered_colors(explicit_colors) or self.infer_colors(
            main_entries, side_entries, catalog_colors=catalog_colors
        )
        rules = self.rules_by_format.get(norm_fmt) or []
        if not rules:
            return "", colors

        main_counts: Counter[str] = Counter()
        for e in main_entries or []:
            nm = _fold_name(e.get("name") or e.get("CardName") or "")
            qty = int(e.get("count") or e.get("Count") or 1)
            if nm and qty > 0:
                main_counts[nm] += qty

        side_counts: Counter[str] = Counter()
        for e in side_entries or []:
            nm = _fold_name(e.get("name") or e.get("CardName") or "")
            qty = int(e.get("count") or e.get("Count") or 1)
            if nm and qty > 0:
                side_counts[nm] += qty

        for rule in rules:
            conds = rule.get("Conditions") or []
            if not conds or not _matches_conditions(conds, main_counts, side_counts):
                continue
            chosen_name = str(rule.get("Name") or "").strip()
            include_color = bool(rule.get("IncludeColorInName"))
            for var in rule.get("Variants") or []:
                if not isinstance(var, dict):
                    continue
                vconds = var.get("Conditions") or []
                if vconds and _matches_conditions(vconds, main_counts, side_counts):
                    vname = str(var.get("Name") or "").strip()
                    if vname:
                        chosen_name = vname
                        include_color = bool(var.get("IncludeColorInName"))
                    break
            if chosen_name:
                if include_color and colors:
                    prefix = color_label(colors)
                    if prefix and not chosen_name.lower().startswith(prefix.lower()):
                        chosen_name = f"{prefix} {chosen_name}"
                return chosen_name, colors

        return "", colors


async def load_or_fetch_classifier(
    client: httpx.AsyncClient,
    *,
    force_refresh: bool = False,
) -> MTGOFormatDataClassifier:
    """Load Badaro/MTGOFormatData rules from SQLite cache or GitHub."""
    init_db()
    now = time.time()
    rules_by_fmt: dict[str, list[dict[str, Any]]] = {}
    card_colors: dict[str, str] = {}

    if not force_refresh:
        with state._get_db().get_db() as conn:
            rows = conn.execute(
                "SELECT format, rules_json, card_colors_json, updated_at FROM meta_archetype_rules"
            ).fetchall()
        fresh = True
        for row in rows:
            if (now - float(row["updated_at"] or 0)) > ARCHETYPE_RULE_TTL_S:
                fresh = False
            rules_by_fmt[row["format"]] = json.loads(row["rules_json"] or "[]")
            card_colors.update(json.loads(row["card_colors_json"] or "{}"))
        if fresh and "standard" in rules_by_fmt and "explorer" in rules_by_fmt:
            return MTGOFormatDataClassifier(rules_by_fmt, card_colors)

    gh_hdrs = _github_headers()
    try:
        cc_resp = await client.get(
            "https://raw.githubusercontent.com/Badaro/MTGOFormatData/main/Formats/card_colors.json",
            headers=gh_hdrs,
        )
        if cc_resp.status_code == 200:
            cc_data = _parse_jsonc(cc_resp.text)
            for section in ("Lands", "NonLands"):
                for item in cc_data.get(section) or []:
                    if isinstance(item, dict) and item.get("Name") and item.get("Color"):
                        card_colors[_fold_name(item["Name"])] = _ordered_colors(item["Color"])
    except Exception as exc:
        logger.warning("MTGOFormatData card_colors fetch failed: %s", exc)

    sem = asyncio.Semaphore(8)

    async def _fetch_rule(dl_url: str) -> Optional[dict[str, Any]]:
        async with sem:
            try:
                r = await client.get(dl_url, headers={"User-Agent": DEFAULT_HTTP_UA})
                if r.status_code == 200:
                    data = _parse_jsonc(r.text)
                    return data if isinstance(data, dict) else None
            except Exception:
                return None
        return None

    for norm_fmt, dir_name in (("standard", "Standard"), ("explorer", "Pioneer")):
        fmt_colors = dict(card_colors)
        try:
            ov_resp = await client.get(
                f"https://raw.githubusercontent.com/Badaro/MTGOFormatData/main/Formats/{dir_name}/color_overrides.json",
                headers={"User-Agent": DEFAULT_HTTP_UA},
            )
            if ov_resp.status_code == 200:
                ov = _parse_jsonc(ov_resp.text)
                for section in ("Lands", "NonLands"):
                    for item in ov.get(section) or []:
                        if isinstance(item, dict) and item.get("Name") and item.get("Color"):
                            fmt_colors[_fold_name(item["Name"])] = _ordered_colors(item["Color"])
        except Exception:
            pass

        try:
            idx_resp = await client.get(
                f"https://api.github.com/repos/Badaro/MTGOFormatData/contents/Formats/{dir_name}/Archetypes",
                headers=gh_hdrs,
            )
            if idx_resp.status_code != 200:
                continue
            entries = [
                e for e in idx_resp.json()
                if isinstance(e, dict) and str(e.get("name") or "").endswith(".json") and e.get("download_url")
            ]
            fetched = await asyncio.gather(*(_fetch_rule(e["download_url"]) for e in entries))
            fmt_rules = [r for r in fetched if r is not None]
            if fmt_rules:
                rules_by_fmt[norm_fmt] = fmt_rules
                card_colors.update(fmt_colors)
                with state._get_db().get_db() as conn:
                    conn.execute(
                        """
                        INSERT INTO meta_archetype_rules (format, rules_json, card_colors_json, updated_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(format) DO UPDATE SET
                            rules_json = excluded.rules_json,
                            card_colors_json = excluded.card_colors_json,
                            updated_at = excluded.updated_at
                        """,
                        (norm_fmt, json.dumps(fmt_rules), json.dumps(fmt_colors), now),
                    )
        except Exception as exc:
            logger.warning("MTGOFormatData %s fetch failed: %s", dir_name, exc)

    return MTGOFormatDataClassifier(rules_by_fmt, card_colors)


def classify_untagged_player_decks(classifier: MTGOFormatDataClassifier) -> int:
    """Populate empty synced_decks.archetype on Standard/Explorer decks without overwriting."""
    if not classifier.rules_by_format:
        return 0
    updated = 0
    with state._get_db().get_db() as conn:
        catalog_colors = {
            str(r["name_key"]): str(r["colors"] or "")
            for r in conn.execute("SELECT name_key, colors FROM card_catalog_cache WHERE colors != ''").fetchall()
        }
        rows = conn.execute(
            """
            SELECT id, format, colors, main_json, sideboard_json
            FROM synced_decks
            WHERE trim(COALESCE(archetype, '')) = '' AND lower(format) IN ('standard', 'explorer')
            """
        ).fetchall()
        now = time.time()
        for row in rows:
            main = json.loads(row["main_json"] or "[]")
            side = json.loads(row["sideboard_json"] or "[]")
            arch, colors = classifier.classify(
                row["format"],
                main,
                side,
                explicit_colors=str(row["colors"] or ""),
                catalog_colors=catalog_colors,
            )
            if arch or (colors and not row["colors"]):
                conn.execute(
                    """
                    UPDATE synced_decks
                    SET archetype = CASE WHEN ? != '' THEN ? ELSE archetype END,
                        colors = CASE WHEN ? != '' THEN ? ELSE colors END,
                        updated_at = CASE WHEN ? != '' THEN updated_at ELSE ? END
                    WHERE id = ?
                    """,
                    (arch, arch, colors if not row["colors"] else "", colors, "", now, row["id"]),
                )
                if arch:
                    updated += 1
    return updated


# ---------------------------------------------------------------------------
# 1. fbettega/MTG_decklistcache tournament ingestion
# ---------------------------------------------------------------------------


def _placing_rank(result_str: str) -> tuple[int, str]:
    txt = str(result_str or "").strip().lower()
    m = re.match(r"^(\d+)(?:st|nd|rd|th)?\s+place$", txt)
    if m:
        return (int(m.group(1)), txt)
    if txt.startswith("1st"):
        return (1, txt)
    if txt.startswith("2nd"):
        return (2, txt)
    if txt.startswith("5-0") or txt.startswith("6-0") or txt.startswith("7-0"):
        return (5, txt)
    if txt.startswith("4-1"):
        return (10, txt)
    return (99, txt)


def _deck_entries_from_cache(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = " ".join(str(item.get("CardName") or item.get("name") or "").split())[:120]
        try:
            cnt = int(item.get("Count") or item.get("count") or 0)
        except (TypeError, ValueError):
            cnt = 0
        if name and cnt > 0:
            out.append({"name": name, "count": cnt})
    return out


def _matches_target_tournament_name(filename: str) -> Optional[str]:
    low = filename.lower()
    if not low.endswith(".json"):
        return None
    if "standard" in low:
        return "standard"
    if "pioneer" in low:
        return "explorer"
    return None


async def fetch_mtg_decklistcache_decks(
    client: httpx.AsyncClient,
    classifier: MTGOFormatDataClassifier,
    *,
    sources: tuple[str, ...] = ("MTGO", "MTGmelee", "Topdeck"),
    lookback_days: int = 10,
    max_tournaments: int = 18,
    now_dt: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Fetch recent Standard & Pioneer tournament lists from fbettega/MTG_decklistcache."""
    gh_hdrs = _github_headers()
    ref_dt = now_dt or datetime.now(timezone.utc)
    months: list[tuple[str, str]] = [(ref_dt.strftime("%Y"), ref_dt.strftime("%m"))]
    earlier = ref_dt - timedelta(days=lookback_days)
    prev_ym = (earlier.strftime("%Y"), earlier.strftime("%m"))
    if prev_ym not in months:
        months.append(prev_ym)

    candidate_files: list[tuple[str, str, str, str]] = []  # (day_key, fmt, download_url, t_src)
    for t_src in sources:
        day_dirs: list[tuple[str, str]] = []
        for yr, mo in months:
            url = f"https://api.github.com/repos/fbettega/MTG_decklistcache/contents/Tournaments/{t_src}/{yr}/{mo}"
            try:
                resp = await client.get(url, headers=gh_hdrs)
                if resp.status_code != 200:
                    continue
                for entry in resp.json():
                    if isinstance(entry, dict) and entry.get("type") == "dir" and entry.get("name"):
                        day_dirs.append((f"{yr}-{mo}-{entry['name']}", f"{yr}/{mo}/{entry['name']}"))
            except Exception as exc:
                logger.warning("MTG_decklistcache month list failed (%s %s/%s): %s", t_src, yr, mo, exc)

        day_dirs.sort(key=lambda x: x[0], reverse=True)
        for day_key, rel_day in day_dirs[:6]:
            try:
                d_resp = await client.get(
                    f"https://api.github.com/repos/fbettega/MTG_decklistcache/contents/Tournaments/{t_src}/{rel_day}",
                    headers=gh_hdrs,
                )
                if d_resp.status_code != 200:
                    continue
                for f in d_resp.json():
                    if not isinstance(f, dict):
                        continue
                    norm_fmt = _matches_target_tournament_name(str(f.get("name") or ""))
                    if norm_fmt and f.get("download_url"):
                        candidate_files.append((day_key, norm_fmt, str(f["download_url"]), t_src))
            except Exception:
                continue

    candidate_files.sort(key=lambda x: x[0], reverse=True)
    selected_files = candidate_files[:max_tournaments]
    if not selected_files:
        return []

    sem = asyncio.Semaphore(6)

    async def _load_tourn(item: tuple[str, str, str, str]):
        day_key, norm_fmt, dl_url, t_src = item
        async with sem:
            try:
                r = await client.get(dl_url, headers={"User-Agent": DEFAULT_HTTP_UA})
                if r.status_code == 200:
                    return day_key, norm_fmt, t_src, r.json()
            except Exception:
                return None
        return None

    tournaments = await asyncio.gather(*(_load_tourn(it) for it in selected_files))

    best_by_key: dict[tuple[str, str], tuple[tuple[int, str], dict[str, Any]]] = {}
    for loaded in tournaments:
        if not loaded or not isinstance(loaded[3], dict):
            continue
        day_key, fallback_fmt, t_src, payload = loaded
        t_meta = payload.get("Tournament") or {}
        t_name = " ".join(str(t_meta.get("Name") or f"{t_src} Event").split())[:80]
        t_date = str(t_meta.get("Date") or day_key)[:10]
        t_uri = str(t_meta.get("Uri") or "").strip()
        raw_fmt = str(t_meta.get("Formats") or fallback_fmt)
        norm_fmt = profiles.normalize_deck_format(raw_fmt) if raw_fmt else fallback_fmt
        if norm_fmt not in ("standard", "explorer"):
            norm_fmt = fallback_fmt

        for idx, d in enumerate(payload.get("Decks") or []):
            if not isinstance(d, dict):
                continue
            try:
                main = _deck_entries_from_cache(d.get("Mainboard") or [])
                side = _deck_entries_from_cache(d.get("Sideboard") or [])
                if sum(c["count"] for c in main) < 60:
                    continue
                arch, colors = classifier.classify(norm_fmt, main, side)
                if not arch:
                    c_name = color_label(colors)
                    arch = f"{c_name} {'Pioneer' if norm_fmt == 'explorer' else 'Standard'}"
                result = str(d.get("Result") or "").strip()
                player = str(d.get("Player") or "").strip()
                anchor = str(d.get("AnchorUri") or t_uri).strip()
                key = (norm_fmt, arch.lower())
                score = (_placing_rank(result)[0], f"{t_date}:{idx:03d}")
                # Sort key: prefer higher placing (smaller number), then newer date
                cur_best = best_by_key.get(key)
                if cur_best is not None:
                    prev_rank = cur_best[0][0]
                    prev_date = cur_best[0][1]
                    if score[0] > prev_rank or (score[0] == prev_rank and prev_date >= t_date):
                        continue

                fmt_display = "Pioneer / Explorer" if norm_fmt == "explorer" else "Standard"
                deck_title = arch if result in ("", "0-0") else f"{arch} ({result})"
                best_by_key[key] = (
                    (score[0], t_date),
                    {
                        "name": deck_title[:100],
                        "format": norm_fmt,
                        "source_site": "MTGO / Tourney",
                        "source": f"MTG_decklistcache {fmt_display} — {t_name}"[:80],
                        "url": anchor or f"https://github.com/fbettega/MTG_decklistcache#{norm_fmt}:{_DECK_SLUG_RE.sub('-', arch.lower())}",
                        "colors": colors,
                        "main": main,
                        "sideboard": side,
                        "commander": [],
                        "companion": [],
                    },
                )
                if player:
                    best_by_key[key][1]["description"] = f"{result} at {t_name} ({t_date}) by {player}"
            except Exception as exc:
                logger.debug("Skipping malformed MTG_decklistcache deck entry: %s", exc)
                continue

    return [pair[1] for pair in best_by_key.values()]


# ---------------------------------------------------------------------------
# 2. MTG Arena Zone embedded tier lists (stdlib HTMLParser)
# ---------------------------------------------------------------------------


class _MTGAZoneParser(HTMLParser):
    """Extract embedded <div class='deck-block'> lists using Python stdlib HTMLParser."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict[str, Any]] = []
        self._in_block = False
        self._block_depth = 0
        self._cur: Optional[dict[str, Any]] = None
        self._capture_name = False
        self._name_depth = 0
        self._name_buf: list[str] = []
        self._pile_stack: list[tuple[int, str]] = []
        self._current_pile = "main"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        amap = {k.lower(): (v or "") for k, v in attrs}
        cls_tokens = set(amap.get("class", "").split())

        if tag == "div":
            if not self._in_block and "deck-block" in cls_tokens:
                self._in_block = True
                self._block_depth = 1
                self._cur = {
                    "id": amap.get("id", ""),
                    "name": "",
                    "main": [],
                    "sideboard": [],
                    "commander": [],
                    "companion": [],
                }
                self._pile_stack = []
                self._current_pile = "main"
                return
            if self._in_block:
                self._block_depth += 1
                if not self._cur["name"] and ("name" in cls_tokens or "deck-name" in cls_tokens):
                    self._capture_name = True
                    self._name_depth = 1
                    self._name_buf = []
                elif self._capture_name:
                    self._name_depth += 1

                if "decklist" in cls_tokens and "info" not in cls_tokens:
                    pile = "main"
                    if "sideboard" in cls_tokens:
                        pile = "sideboard"
                    elif "commander" in cls_tokens:
                        pile = "commander"
                    elif "companion" in cls_tokens:
                        pile = "companion"
                    self._pile_stack.append((self._block_depth, pile))
                    self._current_pile = pile

        if self._in_block and "data-name" in amap and "data-quantity" in amap:
            cname = " ".join(html.unescape(amap["data-name"]).split())
            try:
                qty = int(amap["data-quantity"])
            except ValueError:
                qty = 0
            if cname and qty > 0:
                self._cur[self._current_pile].append({"name": cname, "count": qty})

    def handle_data(self, data: str) -> None:
        if self._capture_name:
            self._name_buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._in_block:
            return
        if tag == "div":
            if self._capture_name:
                self._name_depth -= 1
                if self._name_depth <= 0:
                    self._capture_name = False
                    if self._cur and not self._cur["name"]:
                        self._cur["name"] = " ".join("".join(self._name_buf).split())
            if self._pile_stack and self._pile_stack[-1][0] == self._block_depth:
                self._pile_stack.pop()
                self._current_pile = self._pile_stack[-1][1] if self._pile_stack else "main"
            self._block_depth -= 1
            if self._block_depth <= 0:
                if self._cur and self._cur["name"] and self._cur["main"]:
                    self.blocks.append(self._cur)
                self._in_block = False
                self._cur = None


def parse_mtgazone_tier_html(page_html: str, *, fmt: str, page_url: str, label: str) -> list[dict[str, Any]]:
    parser = _MTGAZoneParser()
    parser.feed(page_html or "")
    parser.close()
    decks: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for blk in parser.blocks:
        name = blk["name"][:100]
        if not name or name.lower() in seen_names:
            continue
        min_cards = 99 if fmt == "brawl" else 60
        if sum(c["count"] for c in blk["main"]) < min_cards:
            continue
        seen_names.add(name.lower())
        slug = _DECK_SLUG_RE.sub("-", name.lower()).strip("-") or blk.get("id") or str(len(decks))
        decks.append(
            {
                "name": name,
                "format": fmt,
                "source_site": "MTG Arena Zone",
                "source": f"MTG Arena Zone {label} Tier List",
                "url": f"{page_url.rstrip('/')}#{slug}",
                "colors": "",
                "main": blk["main"],
                "sideboard": blk["sideboard"],
                "commander": blk["commander"],
                "companion": blk["companion"],
            }
        )
    return decks


async def fetch_mtgazone_tier_decks(
    client: httpx.AsyncClient,
    pages: tuple[tuple[str, str, str], ...] = MTGAZONE_TIER_URLS,
) -> list[dict[str, Any]]:
    """Fetch public tier-list decks from mtgazone.com (Timeless, Alchemy, Historic, Explorer)."""
    collected: list[dict[str, Any]] = []
    for fmt, url, label in pages:
        try:
            resp = await client.get(url, headers={"User-Agent": MOXFIELD_UA})
            if resp.status_code != 200:
                logger.warning("MTG Arena Zone %s returned HTTP %s", url, resp.status_code)
                continue
            collected.extend(parse_mtgazone_tier_html(resp.text, fmt=fmt, page_url=url, label=label))
        except Exception as exc:
            logger.warning("MTG Arena Zone %s failed: %s", url, exc)
    return collected


# ---------------------------------------------------------------------------
# 3. Meta Confluence (Timeless & Competitive Brawl tier lists -> Moxfield embeds)
# ---------------------------------------------------------------------------


def _moxfield_board_entries(board: Any) -> list[dict[str, Any]]:
    if not isinstance(board, dict):
        return []
    cards = board.get("cards")
    if not isinstance(cards, dict):
        return []
    entries: list[dict[str, Any]] = []
    for item in cards.values():
        if not isinstance(item, dict):
            continue
        qty = int(item.get("quantity") or 0)
        card = item.get("card")
        if not isinstance(card, dict) or qty <= 0:
            continue
        cname = " ".join(str(card.get("name") or "").split())[:120]
        if cname:
            entries.append(
                {
                    "name": cname,
                    "count": qty,
                    "rarity": str(card.get("rarity") or "").lower(),
                }
            )
    return entries


async def fetch_metaconfluence_decks(
    client: httpx.AsyncClient,
    pages: tuple[tuple[str, str, str], ...] = METACONFLUENCE_TIER_PAGES,
    *,
    max_per_format: int = 16,
    pace_s: float = 0.15,
) -> list[dict[str, Any]]:
    """Fetch Meta Confluence Timeless & Competitive Brawl tier lists via embedded Moxfield IDs."""
    collected: list[dict[str, Any]] = []
    hdrs = {"User-Agent": MOXFIELD_UA, "Accept": "application/json,text/html,*/*"}
    embed_re = re.compile(r"moxfield\.com/(?:embed|decks)/([A-Za-z0-9_-]{3,64})")

    for fmt, tier_url, path_prefix in pages:
        try:
            idx_resp = await client.get(tier_url, headers=hdrs)
            if idx_resp.status_code != 200:
                continue
        except Exception as exc:
            logger.warning("Meta Confluence %s failed: %s", tier_url, exc)
            continue

        link_re = re.compile(rf"https://metaconfluence\.com{re.escape(path_prefix)}[A-Za-z0-9_-]+/?")
        deck_urls: list[str] = []
        seen_urls: set[str] = set()
        for u in link_re.findall(idx_resp.text):
            norm = u.rstrip("/") + "/"
            if norm not in seen_urls:
                seen_urls.add(norm)
                deck_urls.append(norm)

        for deck_url in deck_urls[:max_per_format]:
            if pace_s > 0:
                await asyncio.sleep(pace_s)
            try:
                p_resp = await client.get(deck_url, headers=hdrs)
                if p_resp.status_code != 200:
                    continue
                em = embed_re.search(p_resp.text)
                if not em:
                    continue
                pub_id = em.group(1)
                m_resp = await client.get(f"https://api2.moxfield.com/v3/decks/all/{pub_id}", headers=hdrs)
                if m_resp.status_code != 200:
                    continue
                mdata = m_resp.json()
                boards = mdata.get("boards") or {}
                main = _moxfield_board_entries(boards.get("mainboard"))
                side = _moxfield_board_entries(boards.get("sideboard"))
                cmd = _moxfield_board_entries(boards.get("commanders"))
                comp = _moxfield_board_entries(boards.get("companions"))
                min_main = 98 if fmt == "brawl" else 60
                if sum(c["count"] for c in main) < min_main:
                    continue
                if fmt == "brawl" and not cmd:
                    continue
                dname = " ".join(str(mdata.get("name") or "").split())[:100]
                if not dname:
                    tmatch = re.search(r"<title>([^<]+)</title>", p_resp.text)
                    dname = (tmatch.group(1).split(" - ")[0].strip() if tmatch else pub_id)[:100]
                colors = _ordered_colors(mdata.get("colors") or mdata.get("colorIdentity") or [])
                collected.append(
                    {
                        "name": dname,
                        "format": fmt,
                        "source_site": "Meta Confluence",
                        "source": f"Meta Confluence {fmt.title()} Tier List",
                        "url": deck_url,
                        "colors": colors,
                        "main": main,
                        "sideboard": side,
                        "commander": cmd,
                        "companion": comp,
                    }
                )
            except Exception as exc:
                logger.debug("Meta Confluence deck %s skipped: %s", deck_url, exc)
                continue

    return collected


# ---------------------------------------------------------------------------
# 4. Archidekt public REST API (Alchemy, Brawl, Explorer, Standard)
# ---------------------------------------------------------------------------


def parse_archidekt_deck(detail: dict[str, Any], *, fmt: str, min_main: int) -> Optional[dict[str, Any]]:
    if not isinstance(detail, dict):
        return None
    name = " ".join(str(detail.get("name") or "").split())[:100]
    did = detail.get("id")
    if not name or not did:
        return None

    included_cats: dict[str, bool] = {}
    for cat in detail.get("categories") or []:
        if isinstance(cat, dict) and cat.get("name"):
            included_cats[str(cat["name"]).casefold()] = bool(cat.get("includedInDeck", True))

    main: Counter[str] = Counter()
    side: Counter[str] = Counter()
    cmd: Counter[str] = Counter()
    comp: Counter[str] = Counter()
    rarities: dict[str, str] = {}
    color_set: set[str] = set()

    for item in detail.get("cards") or []:
        if not isinstance(item, dict):
            continue
        qty = int(item.get("quantity") or 0)
        if qty <= 0:
            continue
        cats = [str(c).casefold() for c in (item.get("categories") or [])]
        if any("maybeboard" in c for c in cats):
            continue
        if cats and any(included_cats.get(c) is False for c in cats):
            continue
        card_obj = item.get("card") or {}
        oracle = card_obj.get("oracleCard") or {}
        cname = " ".join(str(oracle.get("name") or card_obj.get("name") or "").split())[:120]
        if not cname:
            continue
        rar = str(card_obj.get("rarity") or "").lower()
        if rar:
            rarities[cname] = rar
        for col_name in oracle.get("colorIdentity") or oracle.get("colors") or []:
            mapped = {
                "white": "W",
                "blue": "U",
                "black": "B",
                "red": "R",
                "green": "G",
                "w": "W",
                "u": "U",
                "b": "B",
                "r": "R",
                "g": "G",
            }.get(str(col_name).strip().lower())
            if mapped:
                color_set.add(mapped)

        if item.get("companion") or any(c == "companion" for c in cats):
            comp[cname] += qty
        elif any(c == "commander" for c in cats):
            cmd[cname] += qty
        elif any(c == "sideboard" for c in cats):
            side[cname] += qty
        else:
            main[cname] += qty

    if sum(main.values()) < min_main:
        return None
    if fmt == "brawl" and not cmd:
        return None

    def _to_list(counter: Counter[str]) -> list[dict[str, Any]]:
        return [
            {"name": k, "count": v, **({"rarity": rarities[k]} if k in rarities else {})}
            for k, v in counter.items()
        ]

    return {
        "name": name,
        "format": fmt,
        "source_site": "Archidekt",
        "source": f"Archidekt {fmt.title()} (community)",
        "url": f"https://archidekt.com/decks/{did}",
        "colors": _ordered_colors(color_set),
        "main": _to_list(main),
        "sideboard": _to_list(side),
        "commander": _to_list(cmd),
        "companion": _to_list(comp),
    }


async def fetch_archidekt_decks(
    client: httpx.AsyncClient,
    format_specs: tuple[tuple[int, str, int], ...] = ARCHIDEKT_FORMATS,
    *,
    max_per_format: int = 5,
    pace_s: float = 0.85,
) -> list[dict[str, Any]]:
    """Fetch public Archidekt decks politely (respecting 429 rate limits)."""
    collected: list[dict[str, Any]] = []
    hdrs = {"User-Agent": DEFAULT_HTTP_UA, "Accept": "application/json"}

    for fid, fmt, min_main in format_specs:
        if pace_s > 0:
            await asyncio.sleep(pace_s)
        try:
            list_resp = await client.get(
                f"https://archidekt.com/api/decks/v3/?deckFormat={fid}&orderBy=-updatedAt&pageSize=20",
                headers=hdrs,
            )
            if list_resp.status_code == 429:
                logger.info("Archidekt rate-limited (429) on format %s; stopping early", fid)
                break
            if list_resp.status_code != 200:
                continue
            candidates = [
                d for d in (list_resp.json().get("results") or [])
                if isinstance(d, dict) and int(d.get("size") or 0) >= min_main and d.get("id")
            ]
        except Exception as exc:
            logger.warning("Archidekt list %s failed: %s", fid, exc)
            continue

        picked = 0
        for item in candidates:
            if picked >= max_per_format:
                break
            if pace_s > 0:
                await asyncio.sleep(pace_s)
            try:
                d_resp = await client.get(f"https://archidekt.com/api/decks/{item['id']}/", headers=hdrs)
                if d_resp.status_code == 429:
                    logger.info("Archidekt rate-limited (429) fetching deck detail; returning collected=%s", len(collected))
                    return collected
                if d_resp.status_code != 200:
                    continue
                parsed = parse_archidekt_deck(d_resp.json(), fmt=fmt, min_main=min_main)
                if parsed:
                    collected.append(parsed)
                    picked += 1
            except Exception:
                continue

    return collected


# ---------------------------------------------------------------------------
# Coordinator & Admin API
# ---------------------------------------------------------------------------


_REFRESH_IN_PROGRESS = False


async def refresh_all_meta_sources(
    *,
    force: bool = False,
    sources: Optional[tuple[str, ...]] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    """Refresh external tournament/tier-list decks and archetype classifications."""
    global _REFRESH_IN_PROGRESS
    if _REFRESH_IN_PROGRESS:
        return {"ok": True, "skipped": True, "reason": "in_progress"}
    _REFRESH_IN_PROGRESS = True
    try:
        return await _refresh_all_meta_sources_impl(force=force, sources=sources, client=client)
    finally:
        _REFRESH_IN_PROGRESS = False


async def _refresh_all_meta_sources_impl(
    *,
    force: bool = False,
    sources: Optional[tuple[str, ...]] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    init_db()
    now = time.time()
    if not force:
        with state._get_db().get_db() as conn:
            row = conn.execute(
                "SELECT value_json, updated_at FROM meta_sync_state WHERE key = 'last_refresh'"
            ).fetchone()
        if row and (now - float(row["updated_at"] or 0)) < META_SYNC_INTERVAL_S:
            return {"ok": True, "skipped": True, "reason": "fresh"}

    active = set(sources or ("mtgo_cache", "mtgazone", "metaconfluence", "archidekt"))
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=20.0, follow_redirects=True)
    per_source: dict[str, Any] = {}
    collected: list[dict[str, Any]] = []
    classifier = MTGOFormatDataClassifier()

    try:
        classifier = await load_or_fetch_classifier(http_client, force_refresh=force)
        per_source["mtgo_format_rules"] = {fmt: len(rules) for fmt, rules in classifier.rules_by_format.items()}

        if "mtgo_cache" in active:
            try:
                decks = await fetch_mtg_decklistcache_decks(http_client, classifier)
                per_source["mtgo_cache"] = len(decks)
                collected.extend(decks)
            except Exception as exc:
                logger.warning("mtgo_cache sync failed: %s", exc)
                per_source["mtgo_cache"] = f"error: {exc}"

        if "mtgazone" in active:
            try:
                decks = await fetch_mtgazone_tier_decks(http_client)
                per_source["mtgazone"] = len(decks)
                collected.extend(decks)
            except Exception as exc:
                logger.warning("mtgazone sync failed: %s", exc)
                per_source["mtgazone"] = f"error: {exc}"

        if "metaconfluence" in active:
            try:
                decks = await fetch_metaconfluence_decks(http_client)
                per_source["metaconfluence"] = len(decks)
                collected.extend(decks)
            except Exception as exc:
                logger.warning("metaconfluence sync failed: %s", exc)
                per_source["metaconfluence"] = f"error: {exc}"

        if "archidekt" in active:
            try:
                decks = await fetch_archidekt_decks(http_client)
                per_source["archidekt"] = len(decks)
                collected.extend(decks)
            except Exception as exc:
                logger.warning("archidekt sync failed: %s", exc)
                per_source["archidekt"] = f"error: {exc}"
    finally:
        if owns_client:
            await http_client.aclose()

    upsert_stats = profiles.upsert_sourced_decks(collected) if collected else {
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "total_public_sourced": 0,
    }
    player_classified = classify_untagged_player_decks(classifier)

    summary = {
        "ok": True,
        "skipped": False,
        "refreshed_at": time.time(),
        "fetched_total": len(collected),
        "sources": per_source,
        "upsert": upsert_stats,
        "player_decks_classified": player_classified,
    }
    with state._get_db().get_db() as conn:
        conn.execute(
            """
            INSERT INTO meta_sync_state (key, value_json, updated_at)
            VALUES ('last_refresh', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
            """,
            (json.dumps(summary), time.time()),
        )
    try:
        import discord_feed

        discord_feed.notify_leaderboard_dirty()
    except Exception:
        pass
    return summary


async def maybe_refresh_meta_sources() -> dict[str, Any]:
    return await refresh_all_meta_sources(force=False)


@router.post("/admin/meta-sync/refresh")
async def admin_refresh_meta_sources(_admin: str = Depends(state._require_admin)):
    res = await refresh_all_meta_sources(force=True)
    return JSONResponse(res)
