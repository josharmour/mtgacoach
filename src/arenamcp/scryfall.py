"""Scryfall card database with bulk data caching and API fallback."""

import json
import logging
import os
import time
from dataclasses import dataclass
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Optional

import requests

from arenamcp.cache_utils import FileCache

logger = logging.getLogger(__name__)

# Cache configuration
CACHE_DIR = Path.home() / ".arenamcp" / "cache" / "scryfall"
BULK_DATA_URL = "https://api.scryfall.com/bulk-data"
CACHE_MAX_AGE_HOURS = 24
API_RATE_LIMIT_MS = 100


@dataclass
class ScryfallCard:
    """Scryfall card data with commonly used fields."""

    name: str
    oracle_text: str
    type_line: str
    mana_cost: str
    cmc: float
    colors: list[str]
    arena_id: int
    scryfall_uri: str


class ScryfallCache:
    """Scryfall card database with bulk data caching and API fallback.

    Downloads and caches Scryfall bulk data, building an index by arena_id
    for efficient lookups. Falls back to API for cards not in bulk data.
    """

    def __init__(self, cache_dir: Optional[Path] = None):
        """Initialize the cache.

        Args:
            cache_dir: Directory for cache files. Defaults to ~/.arenamcp/cache/scryfall/
        """
        self._cache_dir = cache_dir or CACHE_DIR
        self._file_cache = FileCache(
            self._cache_dir, ttl_seconds=CACHE_MAX_AGE_HOURS * 3600
        )

        self._arena_index: dict[int, dict[str, Any]] = {}
        self._last_api_call: float = 0.0
        self._not_found_cache: set[int] = set()  # Negative cache for 404s
        self._name_cache: dict[str, Optional[dict[str, Any]]] = {}  # Session cache for name lookups

        self._load_or_download_bulk_data()

    def _get_bulk_data_path(self) -> Path:
        """Get path to the bulk data JSON file."""
        return self._cache_dir / "default_cards.json"

    def _is_cache_stale(self) -> bool:
        """Check if the cache file is older than CACHE_MAX_AGE_HOURS."""
        bulk_path = self._get_bulk_data_path()
        return not self._file_cache.is_cache_valid(bulk_path)

    def _download_bulk_data(self) -> None:
        """Download bulk data from Scryfall API."""
        logger.info("Fetching bulk data manifest from Scryfall...")

        # Get bulk data manifest
        response = requests.get(BULK_DATA_URL, timeout=30)
        response.raise_for_status()
        manifest = response.json()

        # Find default_cards entry
        default_cards_entry = None
        for entry in manifest.get("data", []):
            if entry.get("type") == "default_cards":
                default_cards_entry = entry
                break

        if not default_cards_entry:
            raise ValueError("Could not find 'default_cards' in bulk data manifest")

        download_uri = default_cards_entry["download_uri"]
        logger.info(f"Downloading bulk data from {download_uri}...")

        # Download the bulk data file
        response = requests.get(download_uri, timeout=300, stream=True)
        response.raise_for_status()

        bulk_path = self._get_bulk_data_path()
        temp_path = bulk_path.with_suffix(".json.tmp")
        with open(temp_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())

        # Atomic swap avoids leaving a truncated cache file on interrupted downloads.
        os.replace(temp_path, bulk_path)

        logger.info(f"Bulk data saved to {bulk_path}")

    def _load_bulk_data(self) -> None:
        """Load bulk data JSON and build arena_id index."""
        bulk_path = self._get_bulk_data_path()
        logger.debug(f"Loading bulk data from {bulk_path}...")

        with open(bulk_path, "r", encoding="utf-8") as f:
            cards = json.load(f)

        self._arena_index.clear()
        for card in cards:
            arena_id = card.get("arena_id")
            if arena_id is not None:
                self._arena_index[arena_id] = card

        logger.info(f"Indexed {len(self._arena_index)} cards with arena_id")

    def _load_or_download_bulk_data(self) -> None:
        """Load bulk data from cache or download if stale/missing."""
        bulk_path = self._get_bulk_data_path()
        downloaded = False

        if self._is_cache_stale():
            try:
                self._download_bulk_data()
                downloaded = True
            except Exception as e:
                logger.warning(f"Failed to download bulk data: {e}")
                # If download fails but we have cached data, use it.
                if bulk_path.exists():
                    logger.info("Using stale cached data")
                else:
                    raise

        try:
            self._load_bulk_data()
            return
        except JSONDecodeError as e:
            # Corrupted cache can happen after interrupted writes from older versions.
            logger.warning(f"Corrupted Scryfall cache detected ({bulk_path}): {e}")
        except OSError as e:
            logger.warning(f"Failed reading Scryfall cache ({bulk_path}): {e}")

        # One recovery attempt: redownload and reload.
        try:
            if bulk_path.exists():
                try:
                    bad_path = bulk_path.with_name(
                        f"default_cards.corrupt.{int(time.time())}.json"
                    )
                    os.replace(bulk_path, bad_path)
                    logger.warning(f"Moved corrupted cache aside to: {bad_path}")
                except OSError as move_err:
                    logger.warning(f"Could not move corrupted cache file: {move_err}")
            if not downloaded:
                self._download_bulk_data()
            self._load_bulk_data()
        except Exception as e:
            # Keep running without bulk index; API fallback still works.
            logger.error(f"Scryfall cache recovery failed, continuing without bulk index: {e}")
            self._arena_index.clear()

    def _card_dict_to_scryfall_card(self, card: dict[str, Any]) -> ScryfallCard:
        """Convert raw Scryfall JSON dict to ScryfallCard dataclass.

        Handles double-faced cards (DFCs) by extracting oracle_text from card_faces.
        """
        # For DFCs/transform cards, oracle_text is in card_faces, not top level
        oracle_text = card.get("oracle_text", "")
        mana_cost = card.get("mana_cost", "")

        if not oracle_text and "card_faces" in card:
            # Combine oracle text from all faces
            faces = card["card_faces"]
            oracle_parts = []
            for face in faces:
                face_text = face.get("oracle_text", "")
                if face_text:
                    oracle_parts.append(face_text)
            oracle_text = "\n---\n".join(oracle_parts)

            # Get mana cost from front face if not at top level
            if not mana_cost and faces:
                mana_cost = faces[0].get("mana_cost", "")

        return ScryfallCard(
            name=card.get("name", ""),
            oracle_text=oracle_text,
            type_line=card.get("type_line", ""),
            mana_cost=mana_cost,
            cmc=card.get("cmc", 0.0),
            colors=card.get("colors", []),
            arena_id=card.get("arena_id", 0),
            scryfall_uri=card.get("scryfall_uri", ""),
        )

    def _rate_limit_api(self) -> None:
        """Enforce rate limiting for API calls."""
        now = time.time()
        elapsed_ms = (now - self._last_api_call) * 1000
        if elapsed_ms < API_RATE_LIMIT_MS:
            sleep_time = (API_RATE_LIMIT_MS - elapsed_ms) / 1000
            time.sleep(sleep_time)
        self._last_api_call = time.time()

    def _fetch_from_api(self, arena_id: int) -> Optional[dict[str, Any]]:
        """Fetch card from Scryfall API by arena_id."""
        self._rate_limit_api()

        url = f"https://api.scryfall.com/cards/arena/{arena_id}"
        logger.debug(f"Fetching card from API: {url}")

        try:
            response = requests.get(url, timeout=10)
            if response.status_code == 404:
                logger.debug(f"Card not found for arena_id {arena_id}")
                return None
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.warning(f"API request failed for arena_id {arena_id}: {e}")
            return None

    def get_card_by_arena_id(self, arena_id: int) -> Optional[ScryfallCard]:
        """Get card data by MTGA arena_id.

        First checks the in-memory index built from bulk data.
        Falls back to Scryfall API for cards not in bulk data.

        Args:
            arena_id: The MTGA arena ID of the card

        Returns:
            ScryfallCard with card data, or None if not found
        """
        # Check in-memory index first
        if arena_id in self._arena_index:
            return self._card_dict_to_scryfall_card(self._arena_index[arena_id])

        # Check negative cache - don't re-fetch known missing cards
        if arena_id in self._not_found_cache:
            return None

        # Fall back to API
        card_data = self._fetch_from_api(arena_id)
        if card_data:
            # Cache for this session
            self._arena_index[arena_id] = card_data
            return self._card_dict_to_scryfall_card(card_data)

        # Add to negative cache to avoid repeated API calls
        self._not_found_cache.add(arena_id)
        logger.debug(f"Added arena_id {arena_id} to not-found cache")
        return None

    def get_card_by_name(self, name: str) -> Optional[ScryfallCard]:
        """Get card data by name using Scryfall API fuzzy search.

        Useful for new sets where arena_id mappings aren't in bulk data yet.

        Args:
            name: The card name to search for

        Returns:
            ScryfallCard with card data, or None if not found
        """
        if not name or name.startswith("Unknown"):
            return None

        # Check session cache first
        if name in self._name_cache:
            card_data = self._name_cache[name]
            if card_data:
                return self._card_dict_to_scryfall_card(card_data)
            return None

        self._rate_limit_api()

        # Use fuzzy search which handles minor variations
        url = f"https://api.scryfall.com/cards/named"
        params = {"fuzzy": name}
        logger.debug(f"Fetching card by name from API: {name}")

        try:
            response = requests.get(url, params=params, timeout=10)
            if response.status_code == 404:
                logger.debug(f"Card not found by name: {name}")
                self._name_cache[name] = None  # Cache negative result
                return None
            response.raise_for_status()
            card_data = response.json()
            
            # Cache the result
            self._name_cache[name] = card_data
            return self._card_dict_to_scryfall_card(card_data)
        except requests.RequestException as e:
            logger.warning(f"API request failed for name '{name}': {e}")
            return None
