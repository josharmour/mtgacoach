"""17lands draft statistics with JSON download and caching."""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import requests

from arenamcp.cache_utils import FileCache

logger = logging.getLogger(__name__)

# Cache configuration
CACHE_DIR = Path.home() / ".arenamcp" / "cache" / "17lands"
CACHE_MAX_AGE_HOURS = 24
SEVENTEEN_LANDS_URL = "https://www.17lands.com/card_ratings/data"


@dataclass
class DraftStats:
    """17lands draft statistics for a card."""

    name: str
    set_code: str
    gih_wr: Optional[float]  # Games in Hand Win Rate (0.0-1.0)
    alsa: Optional[float]  # Average Last Seen At
    iwd: Optional[float]  # Improvement When Drawn
    games_in_hand: int  # Sample size for GIH WR


class DraftStatsCache:
    """17lands draft statistics with JSON download and caching.

    Downloads and caches 17lands card ratings by set, providing
    GIH WR, ALSA, and IWD metrics for draft card evaluation.
    """

    def __init__(self, cache_dir: Optional[Path] = None):
        """Initialize the cache.

        Args:
            cache_dir: Directory for cache files. Defaults to ~/.arenamcp/cache/17lands/
        """
        self._cache_dir = cache_dir or CACHE_DIR
        self._file_cache = FileCache(
            self._cache_dir, ttl_seconds=CACHE_MAX_AGE_HOURS * 3600
        )

        # In-memory cache: {set_code: {card_name_lower: DraftStats}}
        self._stats_cache: dict[str, dict[str, DraftStats]] = {}

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

    def _parse_json(
        self, cards_data: list[dict[str, Any]], set_code: str
    ) -> dict[str, DraftStats]:
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

        # Check in-memory cache first
        if set_code in self._stats_cache:
            return self._stats_cache[set_code]

        # Check file cache
        cache_path = self._get_cache_path(set_code)
        if cache_path.exists() and not self._is_cache_stale(set_code):
            logger.info(f"Loading cached 17lands data from {cache_path}")
            with open(cache_path, "r", encoding="utf-8") as f:
                cards_data = json.load(f)
        else:
            # Download fresh data
            cards_data = self._download_set_data(set_code)

        # Parse and cache in memory
        self._stats_cache[set_code] = self._parse_json(cards_data, set_code)
        return self._stats_cache[set_code]

    def load_set(self, set_code: str) -> None:
        """Pre-load a set's data into memory.

        Args:
            set_code: Set code like 'DSK', 'BLB', etc.
        """
        self._load_set(set_code)

    def get_draft_rating(
        self, card_name: str, set_code: str
    ) -> Optional[DraftStats]:
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
