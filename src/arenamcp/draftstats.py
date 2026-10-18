"""17lands draft statistics with JSON download and caching."""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from arenamcp.cache_utils import FileCache
from arenamcp.limited_formats import (  # noqa: F401  (re-exported)
    alchemy_expansion,
    ratings_expansion,
    use_event_expansion,
)

logger = logging.getLogger(__name__)

# Cache configuration
CACHE_DIR = Path.home() / ".arenamcp" / "cache" / "17lands"
CACHE_MAX_AGE_HOURS = 24
SEVENTEEN_LANDS_URL = "https://www.17lands.com/card_ratings/data"
SEVENTEEN_LANDS_COLOR_URL = "https://www.17lands.com/color_ratings/data"

# Mapping from color letter sets (sorted) to 17lands color_name keys.
# 17lands formats names like "Mono-White (W)", "Azorius (WU)", "Bant (WUG)".
# We parse on the parenthesized letters instead of the archetype name.
COLOR_LETTERS = ("W", "U", "B", "R", "G")

# Alchemy pair rates replace the base set's once this many two-color games exist.
MIN_ALCHEMY_PAIR_GAMES = 1000


_WIN_RATE_FIELDS = (
    "ever_drawn_win_rate",
    "ever_drawn_game_count",
    "drawn_improvement_win_rate",
    "win_rate",
    "game_count",
    "opening_hand_win_rate",
    "drawn_win_rate",
    "never_drawn_win_rate",
)


def merge_alchemy_rows(alchemy: list[dict[str, Any]], base: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Alchemy rows, with base-set win rates for paper cards the Alchemy data hides.

    Pick order (ALSA/ATA) always comes from the Alchemy format; only rows whose
    win rates are hidden take the base set's win rates and sample sizes.
    """
    base_by_name = {str(row.get("name") or "").lower(): row for row in base}
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in alchemy:
        name = str(row.get("name") or "").lower()
        seen.add(name)
        paper = base_by_name.get(name)
        if row.get("ever_drawn_win_rate") is None and paper and paper.get("ever_drawn_win_rate") is not None:
            # The sample sizes travel with the rates they belong to; the format's own
            # count stays, so a 6-day-old Alchemy card is not "rarely played" next
            # to paper cards carrying weeks of base-set games.
            own_games = row.get("ever_drawn_game_count") or 0
            row = dict(row) | {key: paper.get(key) for key in _WIN_RATE_FIELDS}
            row["format_game_count"] = own_games
            row["win_rates_from"] = "base"
        merged.append(row)
    merged.extend(row for name, row in base_by_name.items() if name and name not in seen)
    return merged


def _parse_color_key(color_name: str) -> str | None:
    """Extract parenthesized color letters from a 17lands color_name string."""
    if "(" not in color_name or ")" not in color_name:
        return None
    letters = color_name[color_name.index("(") + 1 : color_name.index(")")].strip().upper()
    if not letters or not all(c in COLOR_LETTERS for c in letters):
        return None
    # Normalize to WUBRG order
    return "".join(c for c in COLOR_LETTERS if c in letters)


@dataclass
class DraftStats:
    """17lands draft statistics for a card."""

    name: str
    set_code: str
    gih_wr: float | None  # Games in Hand Win Rate (0.0-1.0)
    alsa: float | None  # Average Last Seen At
    iwd: float | None  # Improvement When Drawn
    games_in_hand: int  # Sample size for GIH WR


@dataclass
class ColorPairStats:
    """17lands win rate and popularity for a color combination."""

    color_key: str  # Normalized WUBRG-ordered letters, e.g. "WU", "BRG"
    wins: int
    games: int
    win_rate: float  # wins/games (0.0-1.0), or overall baseline if games == 0


class DraftStatsCache:
    """17lands draft statistics with JSON download and caching.

    Downloads and caches 17lands card ratings by set, providing
    GIH WR, ALSA, and IWD metrics for draft card evaluation.
    """

    def __init__(self, cache_dir: Path | None = None):
        """Initialize the cache.

        Args:
            cache_dir: Directory for cache files. Defaults to ~/.arenamcp/cache/17lands/
        """
        self._cache_dir = cache_dir or CACHE_DIR
        self._file_cache = FileCache(self._cache_dir, ttl_seconds=CACHE_MAX_AGE_HOURS * 3600)

        # In-memory cache: {set_code: {card_name_lower: DraftStats}}
        self._stats_cache: dict[str, dict[str, DraftStats]] = {}
        # In-memory cache: {set_code: {color_key: ColorPairStats}}
        self._color_cache: dict[str, dict[str, ColorPairStats]] = {}

    def _get_cache_path(self, set_code: str) -> Path:
        """Get path to the cached JSON file for a set."""
        return self._cache_dir / f"{set_code.upper()}_PremierDraft.json"

    def _is_cache_stale(self, set_code: str) -> bool:
        """Check if the cache file is older than CACHE_MAX_AGE_HOURS."""
        cache_path = self._get_cache_path(set_code)
        return not self._file_cache.is_cache_valid(cache_path)

    def _download_set_data(self, set_code: str) -> list[dict[str, Any]]:
        """Download card ratings JSON from 17lands.

        Args:
            set_code: Set code like 'DSK', 'BLB', etc.

        Returns:
            List of card data dicts from 17lands API.

        Raises:
            requests.RequestException: If download fails.
        """
        url = f"{SEVENTEEN_LANDS_URL}?expansion={set_code.upper()}&format=PremierDraft"
        logger.info(f"Downloading 17lands data from {url}...")

        response = requests.get(url, timeout=30)
        response.raise_for_status()

        data = response.json()

        # Save to cache
        cache_path = self._get_cache_path(set_code)
        cache_path.write_text(json.dumps(data), encoding="utf-8")
        logger.info(f"Cached 17lands data to {cache_path}")

        return data

    def _parse_json(self, cards_data: list[dict[str, Any]], set_code: str) -> dict[str, DraftStats]:
        """Parse 17lands JSON into DraftStats dict keyed by lowercase card name.

        17lands JSON fields:
        - name: Card name
        - avg_seen: ALSA (Average Last Seen At)
        - ever_drawn_win_rate: GIH WR (Games in Hand Win Rate)
        - ever_drawn_game_count: Number of games for GIH WR
        - drawn_improvement_win_rate: IWD (Improvement When Drawn)
        """
        result: dict[str, DraftStats] = {}

        for card in cards_data:
            name = card.get("name", "").strip()
            if not name:
                continue

            # Parse win rates (already as decimals 0.0-1.0, or null)
            gih_wr = card.get("ever_drawn_win_rate")
            alsa = card.get("avg_seen")
            iwd = card.get("drawn_improvement_win_rate")
            games_in_hand = card.get("ever_drawn_game_count") or 0

            stats = DraftStats(
                name=name,
                set_code=set_code.upper(),
                gih_wr=gih_wr,
                alsa=alsa,
                iwd=iwd,
                games_in_hand=games_in_hand,
            )

            result[name.lower()] = stats

        logger.info(f"Parsed {len(result)} cards from 17lands data for {set_code}")
        return result

    def _load_set(self, set_code: str) -> dict[str, DraftStats]:
        """Load set data from cache or download if needed."""
        set_code = set_code.upper()
        expansion = ratings_expansion(set_code)

        # Check in-memory cache first
        if expansion in self._stats_cache:
            return self._stats_cache[expansion]

        # Parse and cache in memory
        self._stats_cache[expansion] = self._parse_json(self.get_raw_ratings(set_code), set_code)
        return self._stats_cache[expansion]

    def _expansion_rows(self, expansion: str) -> list[dict[str, Any]]:
        cache_path = self._get_cache_path(expansion)
        if cache_path.exists() and not self._is_cache_stale(expansion):
            logger.info(f"Loading cached 17lands data from {cache_path}")
            with open(cache_path, encoding="utf-8") as f:
                return json.load(f)
        return self._download_set_data(expansion)

    def get_raw_ratings(self, set_code: str) -> list[dict[str, Any]]:
        """The 17lands rows for the current event's format (mtga_id, ATA, ALSA, GIH, IWD...).

        For an Alchemy event the rows are the Alchemy expansion's, with base-set win
        rates filled in where 17lands still hides them.
        """
        set_code = set_code.upper()
        expansion = ratings_expansion(set_code)
        rows = self._expansion_rows(expansion)
        if expansion == set_code:
            return rows
        try:
            base = self._expansion_rows(set_code)
        except requests.RequestException as e:
            logger.warning(f"Base-set 17lands data for {set_code} unavailable: {e}")
            return rows
        return merge_alchemy_rows(rows, base)

    def load_set(self, set_code: str) -> None:
        """Pre-load a set's data into memory.

        Args:
            set_code: Set code like 'DSK', 'BLB', etc.
        """
        self._load_set(set_code)

    def get_draft_rating(self, card_name: str, set_code: str) -> DraftStats | None:
        """Get draft statistics for a card.

        Args:
            card_name: Card name (case-insensitive)
            set_code: Set code like 'DSK', 'BLB', etc.

        Returns:
            DraftStats with GIH WR, ALSA, IWD metrics, or None if not found.
        """
        try:
            set_data = self._load_set(set_code)
        except requests.RequestException as e:
            logger.warning(f"Failed to load 17lands data for {set_code}: {e}")
            return None

        return set_data.get(card_name.lower())

    # -- Color-pair win rates -----------------------------------------------

    def _get_color_cache_path(self, set_code: str, combine_splash: bool = True) -> Path:
        # "Combined": each pair includes its splash decks; the split rows (pure,
        # "+ Splash", three-color summaries) stay in the original file.
        suffix = "ColorRatingsCombined" if combine_splash else "ColorRatings"
        return self._cache_dir / f"{set_code.upper()}_{suffix}.json"

    def _download_color_data(self, set_code: str, combine_splash: bool = True) -> list[dict[str, Any]]:
        """Download per-color-pair win rates from 17lands.

        Pair strength uses ``combine_splash`` (splash decks counted in their
        pair). 2026-10-13: the pure two-color rows left out ~60% of BG decks
        (they splash) and ranked it 6th; combined it is 3rd, as the post-release
        guides and the 17lands rankings have it. The split rows are kept for
        opponent deck priors (``opponent_tricks``).
        """
        url = (
            f"{SEVENTEEN_LANDS_COLOR_URL}?expansion={set_code.upper()}"
            f"&event_type=PremierDraft&start_date=2020-01-01&end_date=2099-12-31"
            + ("&combine_splash=true" if combine_splash else "")
        )
        logger.info(f"Downloading 17lands color data from {url}...")
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
        cache_path = self._get_color_cache_path(set_code, combine_splash)
        cache_path.write_text(json.dumps(data), encoding="utf-8")
        logger.info(f"Cached 17lands color data to {cache_path}")
        return data

    def _parse_color_data(self, raw: list[dict[str, Any]]) -> dict[str, ColorPairStats]:
        result: dict[str, ColorPairStats] = {}
        for row in raw:
            name = row.get("color_name", "")
            if row.get("is_summary"):
                continue
            # "Azorius (WU) + Splash" parses to the same letters as "Azorius (WU)"
            # and used to overwrite it, so every pair rate was its splash variant.
            if "splash" in name.lower() or str(row.get("short_name") or "").endswith("+"):
                continue
            key = _parse_color_key(name)
            if key is None:
                continue
            wins = int(row.get("wins") or 0)
            games = int(row.get("games") or 0)
            if games <= 0:
                continue
            result[key] = ColorPairStats(
                color_key=key,
                wins=wins,
                games=games,
                win_rate=wins / games,
            )
        return result

    def _load_color_data(self, set_code: str) -> dict[str, ColorPairStats]:
        set_code = set_code.upper()
        expansion = ratings_expansion(set_code)
        if expansion in self._color_cache:
            return self._color_cache[expansion]
        parsed = self._load_expansion_colors(expansion)
        if expansion != set_code and sum(p.games for p in parsed.values() if len(p.color_key) == 2) < (
            MIN_ALCHEMY_PAIR_GAMES
        ):
            logger.info(f"{expansion} has too few games for pair win rates; using {set_code}")
            parsed = self._load_expansion_colors(set_code)
        self._color_cache[expansion] = parsed
        return parsed

    def _load_expansion_colors(self, set_code: str) -> dict[str, ColorPairStats]:
        cache_path = self._get_color_cache_path(set_code)
        if cache_path.exists() and self._file_cache.is_cache_valid(cache_path):
            try:
                with open(cache_path, encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception as e:
                logger.warning(f"Failed to read cached color data for {set_code}: {e}")
                raw = self._download_color_data(set_code)
        else:
            raw = self._download_color_data(set_code)

        parsed = self._parse_color_data(raw)
        logger.info(f"Loaded {len(parsed)} color-pair entries for {set_code}")
        return parsed

    def get_color_rows(self, set_code: str, combine_splash: bool = False) -> list[dict[str, Any]] | None:
        """17lands color-rating rows as served (split rows by default), cached on disk."""
        set_code = ratings_expansion(set_code)
        path = self._get_color_cache_path(set_code, combine_splash)
        try:
            if path.exists() and self._file_cache.is_cache_valid(path):
                rows = json.loads(path.read_text(encoding="utf-8"))
            else:
                rows = self._download_color_data(set_code, combine_splash)
        except Exception as e:
            logger.debug(f"Color rows for {set_code} unavailable: {e}")
            return None
        return rows if isinstance(rows, list) else None

    def get_color_pair_stats(self, set_code: str) -> dict[str, ColorPairStats]:
        """Return per-color-pair win rates for a set (splash decks counted in their pair).

        Keyed by normalized WUBRG-ordered color letters (e.g. "WU", "BRG").
        Empty dict if the lookup fails.
        """
        try:
            return self._load_color_data(set_code)
        except requests.RequestException as e:
            logger.warning(f"Failed to load 17lands color data for {set_code}: {e}")
            return {}
        except Exception as e:
            logger.debug(f"Color data load failed for {set_code}: {e}")
            return {}
