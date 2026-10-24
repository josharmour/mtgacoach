"""17Lands card catalogs for rotated sets: packaged baselines + in-app rebuild (#218).

Once a set rotates, 17lands' card-ratings endpoint keeps naming every card
(mtga_id, rarity, types) but stops serving win rates -- every row comes back
with 0 games, so picks, deck build and the limited prompts ran blind. The
public game-data dumps on 17lands' S3 bucket still hold every PremierDraft
game, so a catalog of GIH/GP win rates can be rebuilt from them.

A catalog file is the same JSON the dev pipeline (tools/deck_benchmark/
catalog*.py) writes: {"set", "coverage", "cards": {name: {grp_id, name,
oracle_text, type_line, mana_cost, cmc, colors, rarity, gih_games, gih_wr,
gp_wr}}}. Consumers only need ``cards``.

Resolution order for a set's catalog:
  1. a valid catalog in the user cache (~/.arenamcp/cache/17lands/public/work/),
  2. a catalog packaged with the app (arenamcp/resources/catalogs/),
  3. a background rebuild from public data -- rate-limited, best effort,
     never on the decision path.

Everything here degrades to "no catalog" without raising.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Iterable

import requests

logger = logging.getLogger(__name__)

SEVENTEEN_LANDS_RATINGS_URL = "https://www.17lands.com/card_ratings/data"
PUBLIC_GAME_DATA_URL = (
    "https://17lands-public.s3.amazonaws.com/analysis_data/game_data/"
    "game_data_public.{set}.{fmt}.csv.gz"
)

CATALOG_WORK_SUBPATH = Path("public") / "work"
PACKAGED_CATALOG_DIR = Path(__file__).resolve().parent / "resources" / "catalogs"

# 17Lands hides GIH below ~500 games in hand; below this a win rate is noise.
MIN_GIH_GAMES = 500
# A catalog with fewer rated cards than this is not worth shipping as a fallback.
MIN_RATED_CARDS = 50

_TRUE = {"True", "true", "1", "1.0"}
_ZERO = {"0", "0.0", ""}
_BASIC_LAND_COLORS = {"Plains": "W", "Island": "U", "Swamp": "B", "Mountain": "R", "Forest": "G"}

# Background-fetch scheduling: one attempt per expansion per interval, with
# growing backoff after failures, so a draft loop cannot hammer 17lands.
_MIN_ATTEMPT_INTERVAL_S = 6 * 3600
_BACKOFF_BASE_S = 10 * 60
_MAX_BACKOFF_S = 24 * 3600

_lock = threading.Lock()
_last_attempt: dict[str, float] = {}
_failure_count: dict[str, int] = {}
_threads: set[str] = set()


# -- Catalog files -----------------------------------------------------------


def catalog_path(cache_dir: Path | str, expansion: str) -> Path:
    """Where a set's catalog lives in the user cache."""
    return Path(cache_dir) / CATALOG_WORK_SUBPATH / f"{expansion.upper()}.catalog.json"


def packaged_catalog_path(expansion: str) -> Path | None:
    """A precomputed catalog shipped with the app, if this set has one."""
    path = PACKAGED_CATALOG_DIR / f"{expansion.upper()}.catalog.json"
    return path if path.exists() else None


def read_catalog(path: Path | None) -> dict[str, dict[str, Any]] | None:
    """The ``cards`` mapping of a catalog file, or None if unreadable/too thin."""
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cards = data.get("cards")
        if not isinstance(cards, dict) or not cards:
            return None
        rated = sum(1 for card in cards.values() if card.get("gih_wr") is not None)
        if rated < MIN_RATED_CARDS:
            return None
        return cards
    except Exception as e:  # corrupt/partial file must never break a draft
        logger.debug(f"Catalog {path} unusable: {e}")
        return None


def resolve_catalog(
    expansion: str, cache_dir: Path | str, allow_fetch: bool = True
) -> dict[str, dict[str, Any]] | None:
    """Best available catalog cards for a set; schedules a rebuild if none."""
    expansion = str(expansion or "").upper()
    if not expansion:
        return None
    cards = read_catalog(catalog_path(cache_dir, expansion))
    if cards is None:
        cards = read_catalog(packaged_catalog_path(expansion))
    if cards is None and allow_fetch:
        schedule_fetch(expansion, cache_dir)
    return cards


# -- Building a catalog from public game data --------------------------------


def _card_dict(meta: dict[str, Any], grp_id: int, name: str) -> dict[str, Any]:
    return {
        "grp_id": grp_id,
        "name": name,
        "oracle_text": meta.get("oracle_text") or "",
        "type_line": meta.get("type_line") or "",
        "mana_cost": meta.get("mana_cost") or "",
        "cmc": meta.get("cmc"),
        "colors": [c for c in (meta.get("colors") or []) if c in ("W", "U", "B", "R", "G")],
        "rarity": meta.get("rarity") or "",
    }


def enrich_meta_rows(meta_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Card rules for the ratings-API rows, from the local card database.

    The ratings rows carry name/mtga_id/rarity/types but no rules text; the
    local MTGA/MTGJSON databases fill in oracle text, mana cost and cmc. The
    local database is optional -- without it the catalog still carries win
    rates keyed by name (all a ratings fallback needs).
    """
    enriched: dict[str, dict[str, Any]] = {}
    db = None
    try:
        from arenamcp.card_db import require_card_database

        db = require_card_database(allow_network=False)
    except Exception as e:
        logger.debug(f"Local card database unavailable for catalog rules: {e}")
    for row in meta_rows:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        meta: dict[str, Any] = {
            "rarity": row.get("rarity") or "",
            "colors": [row["color"]] if row.get("color") in ("W", "U", "B", "R", "G") else [],
            "type_line": " — ".join(row.get("types") or []),
        }
        grp_id = row.get("mtga_id") if isinstance(row.get("mtga_id"), int) else None
        if db is not None and grp_id is not None:
            try:
                info = db.get_card_by_arena_id(grp_id)
            except Exception:
                info = None
            if info is not None and getattr(info, "type_line", ""):
                meta |= {
                    "oracle_text": info.oracle_text or "",
                    "type_line": info.type_line or meta["type_line"],
                    "mana_cost": info.mana_cost or "",
                    "cmc": info.cmc,
                    "colors": [c for c in (info.colors or []) if c in ("W", "U", "B", "R", "G")],
                    "rarity": info.rarity or meta["rarity"],
                }
        meta["grp_id"] = grp_id
        enriched[name] = meta
    return enriched


def build_catalog_from_stream(
    set_code: str,
    meta_rows: list[dict[str, Any]],
    game_rows: Iterable[tuple[list[str], list[str]]],
) -> dict[str, Any]:
    """GIH/GP win rates for every card in ``meta_rows`` from streamed game rows.

    ``game_rows`` yields (header, row) pairs like tools/deck_benchmark/
    common.iter_games. Per game row: a card counts as games-present when its
    ``deck_<name>`` column is non-zero and as games-in-hand when additionally
    ``opening_hand_<name>`` or ``drawn_<name>`` is non-zero; ``won`` splits the
    wins. Every game feeds the ratings here (the dev pipeline hashes drafts in
    half so model rows never score themselves; a catalog has no such conflict).
    """
    meta_by_name = enrich_meta_rows(meta_rows)
    names = sorted(meta_by_name)
    gih_games = [0] * len(names)
    gih_wins = [0] * len(names)
    gp_games = [0] * len(names)
    gp_wins = [0] * len(names)
    rating_games = 0

    header_seen = False
    oh_idx: list[int] = []
    dr_idx: list[int] = []
    dk_idx: list[int] = []
    won_idx = -1
    for header, row in game_rows:
        if not header_seen:
            col = {column: i for i, column in enumerate(header)}
            missing = [
                prefix + name
                for name in names
                for prefix in ("opening_hand_", "drawn_", "deck_")
                if prefix + name not in col
            ]
            if missing:
                raise ValueError(f"Game data missing columns for {len(missing)} cards, e.g. {missing[:3]}")
            oh_idx = [col["opening_hand_" + name] for name in names]
            dr_idx = [col["drawn_" + name] for name in names]
            dk_idx = [col["deck_" + name] for name in names]
            won_idx = col["won"]
            header_seen = True
        rating_games += 1
        won = row[won_idx] in _TRUE
        for i in range(len(names)):
            if row[dk_idx[i]] in _ZERO:
                continue
            gp_games[i] += 1
            gp_wins[i] += won
            if row[oh_idx[i]] not in _ZERO or row[dr_idx[i]] not in _ZERO:
                gih_games[i] += 1
                gih_wins[i] += won

    cards: dict[str, dict[str, Any]] = {}
    synthetic = 9_000_000
    seen_grps: set[int] = set()
    for i, name in enumerate(names):
        meta = meta_by_name[name]
        grp_id = meta.get("grp_id")
        if not isinstance(grp_id, int) or grp_id <= 0 or grp_id in seen_grps:
            synthetic += 1
            grp_id = synthetic
        seen_grps.add(grp_id)
        card = _card_dict(meta, grp_id, name)
        games_in_hand, games_present = gih_games[i], gp_games[i]
        if card["type_line"].lower().startswith("basic land"):
            games_in_hand = games_present = 0  # 17Lands does not rate basic lands
        card["gih_games"] = games_in_hand
        card["gih_wr"] = round(gih_wins[i] / games_in_hand, 5) if games_in_hand >= MIN_GIH_GAMES else None
        card["gp_wr"] = round(gp_wins[i] / games_present, 5) if games_present >= MIN_GIH_GAMES else None
        cards[name] = card

    rated = sum(1 for card in cards.values() if card["gih_wr"] is not None)
    return {
        "set": set_code.upper(),
        "coverage": {
            "names": len(names),
            "rated_names": rated,
            "rating_games": rating_games,
            "source": "public_game_data",
        },
        "cards": cards,
    }


# -- Fetching ----------------------------------------------------------------


def _meta_rows(expansion: str, timeout_s: float) -> list[dict[str, Any]]:
    """The ratings-API rows for a set: every card with mtga_id/rarity/types."""
    url = f"{SEVENTEEN_LANDS_RATINGS_URL}?expansion={expansion}&format=PremierDraft"
    response = requests.get(url, timeout=timeout_s)
    response.raise_for_status()
    rows = response.json()
    return rows if isinstance(rows, list) else []


class _LimitedReader:
    """File-like wrapper that caps how many compressed bytes a stream may read."""

    def __init__(self, response: requests.Response, max_bytes: int):
        self._chunks = response.iter_content(chunk_size=1 << 20)
        self._buffer = b""
        self._remaining = max_bytes

    def read(self, size: int) -> bytes:
        while len(self._buffer) < size and self._remaining > 0:
            chunk = next(self._chunks, b"")
            if not chunk:
                break
            self._remaining -= len(chunk)
            self._buffer += chunk
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data


def _stream_game_rows(
    expansion: str,
    timeout_s: float,
    max_bytes: int,
) -> Iterable[tuple[list[str], list[str]]]:
    """Stream (header, row) pairs from the public PremierDraft game-data dump."""
    url = PUBLIC_GAME_DATA_URL.format(set=expansion.upper(), fmt="PremierDraft")
    deadline = time.monotonic() + timeout_s
    with requests.get(url, stream=True, timeout=(10, timeout_s)) as response:
        response.raise_for_status()
        reader = csv.reader(
            io.TextIOWrapper(
                gzip.GzipFile(fileobj=_LimitedReader(response, max_bytes)),
                encoding="utf-8",
                newline="",
            )
        )
        header: list[str] | None = None
        try:
            for row in reader:
                if header is None:
                    header = row
                    continue
                yield header, row
                if time.monotonic() > deadline:
                    logger.info(f"17lands public game data for {expansion}: time cap reached")
                    return
        except (EOFError, OSError) as e:
            # A byte-capped sample stops mid-archive; the games read so far stand.
            logger.info(f"17lands public game data for {expansion}: stream cap reached ({e})")


def fetch_catalog(
    expansion: str,
    cache_dir: Path | str,
    timeout_s: float = 900,
    max_bytes: int = 200 << 20,
) -> Path | None:
    """Rebuild a set's catalog from public data and cache it. Best effort.

    Reads at most ``max_bytes`` of compressed game data within ``timeout_s`` --
    enough games that commons and uncommons clear the 500-game threshold; rarer
    cards simply stay unrated until a fuller pass (the dev pipeline). Returns
    the written path, or None on any failure.
    """
    expansion = str(expansion or "").upper()
    if not expansion:
        return None
    try:
        meta_rows = _meta_rows(expansion, timeout_s=min(timeout_s, 30))
        if len(meta_rows) < MIN_RATED_CARDS:
            logger.info(f"No usable 17lands card list for {expansion}; no catalog possible")
            return None
        game_rows = _stream_game_rows(expansion, timeout_s=timeout_s, max_bytes=max_bytes)
        catalog = build_catalog_from_stream(expansion, meta_rows, game_rows)
        rated = catalog["coverage"]["rated_names"]
        destination = catalog_path(cache_dir, expansion)
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = destination.with_name(destination.name + ".tmp")
        tmp_path.write_text(json.dumps(catalog), encoding="utf-8")
        tmp_path.replace(destination)
        logger.info(f"Built 17lands catalog for {expansion}: {rated} rated cards -> {destination}")
        return destination
    except Exception as e:
        logger.warning(f"17lands catalog rebuild for {expansion} failed: {e}")
        return None


def schedule_fetch(expansion: str, cache_dir: Path | str) -> bool:
    """Kick off a background catalog rebuild at most once per interval.

    Returns True when a rebuild was actually scheduled. Failures back off so a
    polling draft loop cannot hammer 17lands; successes reset the backoff.
    """
    expansion = str(expansion or "").upper()
    now = time.monotonic()
    with _lock:
        failures = _failure_count.get(expansion, 0)
        interval = min(_MAX_BACKOFF_S, _MIN_ATTEMPT_INTERVAL_S + failures * _BACKOFF_BASE_S)
        if now - _last_attempt.get(expansion, now - interval * 2) < interval:
            return False
        if expansion in _threads:
            return False
        _last_attempt[expansion] = now

        def run() -> None:
            try:
                ok_path = fetch_catalog(expansion, cache_dir)
            except Exception as e:  # fetch_catalog already guards; belt and braces
                logger.debug(f"Catalog rebuild thread for {expansion} raised: {e}")
                ok_path = None
            with _lock:
                _threads.discard(expansion)
                if ok_path is None:
                    _failure_count[expansion] = _failure_count.get(expansion, 0) + 1
                else:
                    _failure_count.pop(expansion, None)

        thread = threading.Thread(
            target=run, name=f"catalog-{expansion}", daemon=True
        )
        _threads.add(expansion)
        thread.start()
        return True


def reset_fetch_state() -> None:
    """Clear attempt/backoff bookkeeping (tests)."""
    with _lock:
        _last_attempt.clear()
        _failure_count.clear()
        _threads.clear()
