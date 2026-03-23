"""MTGJSON card database for complete oracle text coverage.

Downloads and caches MTGJSON's AtomicCards.json which provides complete
card data including oracle text, updated daily for new sets.
"""

import gzip
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

# Cache settings
CACHE_DIR = Path.home() / ".arenamcp" / "mtgjson"
ATOMIC_CARDS_URL = "https://mtgjson.com/api/v5/AtomicCards.json.gz"
CACHE_MAX_AGE_HOURS = 24  # Re-download if older than this


@dataclass
class MTGJSONCard:
    """Card data from MTGJSON."""
    name: str
    oracle_text: str
    type_line: str
    mana_cost: str
    cmc: float
    colors: list[str]
    arena_id: Optional[int] = None


class MTGJSONDatabase:
    """MTGJSON card database with arena_id and name lookups.

    Downloads AtomicCards.json and builds indexes for fast lookups.
    Supports both arena_id lookups (when available) and name-based
    lookups for cards without arena_id mappings.
    """

    def __init__(self, cache_dir: Optional[Path] = None):
        """Initialize the database.

        Args:
            cache_dir: Directory for cached data. Defaults to ~/.arenamcp/mtgjson
        """
        self._cache_dir = cache_dir or CACHE_DIR
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_file = self._cache_dir / "AtomicCards.json"
        self._index_file = self._cache_dir / "arena_id_index.json"
        self._name_index_file = self._cache_dir / "name_index.json"

        # In-memory indexes
        self._arena_index: dict[int, MTGJSONCard] = {}
        self._name_index: dict[str, MTGJSONCard] = {}  # lowercase name -> card
        self._loaded = False
        self._available = False

    @property
    def available(self) -> bool:
        """Check if database is loaded and available."""
        return self._available

    def _needs_update(self) -> bool:
        """Check if cache needs to be refreshed."""
        if not self._cache_file.exists():
            return True

        # Check age
        mtime = self._cache_file.stat().st_mtime
        age_hours = (time.time() - mtime) / 3600
        return age_hours > CACHE_MAX_AGE_HOURS

    def _download_data(self) -> bool:
        """Download AtomicCards.json.gz from MTGJSON.

        Returns:
            True if download succeeded, False otherwise.
        """
        logger.info("Downloading MTGJSON AtomicCards (one-time, ~30MB)...")

        try:
            response = requests.get(ATOMIC_CARDS_URL, timeout=120, stream=True)
            response.raise_for_status()

            # Download to temp file first
            temp_file = self._cache_file.with_suffix(".json.gz.tmp")
            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0

            with open(temp_file, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        pct = int(100 * downloaded / total_size)
                        if downloaded % (1024 * 1024) < 8192:  # Every ~1MB
                            logger.info("MTGJSON download progress: %d%%", pct)

            logger.info("MTGJSON download complete, decompressing...")

            # Decompress
            with gzip.open(temp_file, 'rt', encoding='utf-8') as gz:
                data = json.load(gz)

            # Save decompressed
            with open(self._cache_file, 'w', encoding='utf-8') as f:
                json.dump(data, f)

            # Cleanup temp file
            temp_file.unlink(missing_ok=True)

            logger.info(f"MTGJSON data saved to {self._cache_file}")
            return True

        except requests.RequestException as e:
            logger.error(f"Failed to download MTGJSON: {e}")
            return False
        except Exception as e:
            logger.error(f"Error processing MTGJSON data: {e}")
            return False

    def _build_indexes(self, data: dict[str, Any]) -> tuple[dict[int, MTGJSONCard], dict[str, MTGJSONCard]]:
        """Build both arena_id and name indexes from card data.

        Args:
            data: Raw MTGJSON AtomicCards data

        Returns:
            Tuple of (arena_id_index, name_index)
        """
        logger.info("Building card indexes...")
        arena_index: dict[int, MTGJSONCard] = {}
        name_index: dict[str, MTGJSONCard] = {}

        # AtomicCards format: { "data": { "Card Name": [printing1, printing2, ...] } }
        cards_data = data.get("data", {})

        for card_name, printings in cards_data.items():
            # Use first printing for name index (they share oracle text)
            if printings:
                printing = printings[0]
                card = MTGJSONCard(
                    name=printing.get("name", card_name),
                    oracle_text=printing.get("text", ""),
                    type_line=printing.get("type", ""),
                    mana_cost=printing.get("manaCost", ""),
                    cmc=printing.get("manaValue", 0.0),
                    colors=printing.get("colors", []),
                    arena_id=None,
                )
                # Index by lowercase name for case-insensitive lookup
                name_index[card_name.lower()] = card

            # Check all printings for arena_id
            for printing in printings:
                identifiers = printing.get("identifiers", {})
                arena_id_str = identifiers.get("mtgArenaId")

                if not arena_id_str:
                    continue

                try:
                    arena_id = int(arena_id_str)
                except (ValueError, TypeError):
                    continue

                # Skip if we already have this arena_id (keep first)
                if arena_id in arena_index:
                    continue

                # Extract card data with arena_id
                card = MTGJSONCard(
                    name=printing.get("name", card_name),
                    oracle_text=printing.get("text", ""),
                    type_line=printing.get("type", ""),
                    mana_cost=printing.get("manaCost", ""),
                    cmc=printing.get("manaValue", 0.0),
                    colors=printing.get("colors", []),
                    arena_id=arena_id,
                )
                arena_index[arena_id] = card

        logger.info(f"Built indexes: {len(arena_index)} arena_ids, {len(name_index)} names")
        return arena_index, name_index

    def _save_indexes(self) -> None:
        """Save both indexes to disk for faster startup."""
        try:
            # Save arena_id index
            arena_data = {}
            for arena_id, card in self._arena_index.items():
                arena_data[str(arena_id)] = {
                    "name": card.name,
                    "oracle_text": card.oracle_text,
                    "type_line": card.type_line,
                    "mana_cost": card.mana_cost,
                    "cmc": card.cmc,
                    "colors": card.colors,
                }

            with open(self._index_file, 'w', encoding='utf-8') as f:
                json.dump(arena_data, f)

            # Save name index
            name_data = {}
            for name, card in self._name_index.items():
                name_data[name] = {
                    "name": card.name,
                    "oracle_text": card.oracle_text,
                    "type_line": card.type_line,
                    "mana_cost": card.mana_cost,
                    "cmc": card.cmc,
                    "colors": card.colors,
                }

            with open(self._name_index_file, 'w', encoding='utf-8') as f:
                json.dump(name_data, f)

            logger.info(f"Saved indexes: {len(arena_data)} arena_ids, {len(name_data)} names")
        except Exception as e:
            logger.warning(f"Failed to save indexes: {e}")

    def _load_indexes(self) -> bool:
        """Load pre-built indexes from disk.

        Returns:
            True if indexes loaded successfully, False otherwise.
        """
        # Need both index files
        if not self._index_file.exists() or not self._name_index_file.exists():
            return False

        # Check if indexes are newer than cache file
        if self._cache_file.exists():
            cache_mtime = self._cache_file.stat().st_mtime
            if (self._index_file.stat().st_mtime < cache_mtime or
                self._name_index_file.stat().st_mtime < cache_mtime):
                logger.info("Indexes older than cache, rebuilding")
                return False

        try:
            # Load arena_id index
            with open(self._index_file, 'r', encoding='utf-8') as f:
                arena_data = json.load(f)

            self._arena_index = {}
            for arena_id_str, card_data in arena_data.items():
                arena_id = int(arena_id_str)
                self._arena_index[arena_id] = MTGJSONCard(
                    name=card_data["name"],
                    oracle_text=card_data["oracle_text"],
                    type_line=card_data["type_line"],
                    mana_cost=card_data["mana_cost"],
                    cmc=card_data["cmc"],
                    colors=card_data["colors"],
                    arena_id=arena_id,
                )

            # Load name index
            with open(self._name_index_file, 'r', encoding='utf-8') as f:
                name_data = json.load(f)

            self._name_index = {}
            for name, card_data in name_data.items():
                self._name_index[name] = MTGJSONCard(
                    name=card_data["name"],
                    oracle_text=card_data["oracle_text"],
                    type_line=card_data["type_line"],
                    mana_cost=card_data["mana_cost"],
                    cmc=card_data["cmc"],
                    colors=card_data["colors"],
                    arena_id=None,
                )

            logger.info(f"Loaded indexes: {len(self._arena_index)} arena_ids, {len(self._name_index)} names")
            return True

        except Exception as e:
            logger.warning(f"Failed to load indexes: {e}")
            return False

    def load(self, force_download: bool = False) -> bool:
        """Load the database, downloading if needed.

        Args:
            force_download: If True, re-download even if cache exists

        Returns:
            True if database loaded successfully, False otherwise.
        """
        if self._loaded and not force_download:
            return self._available

        # Try loading from pre-built indexes first (fast startup)
        if not force_download and self._load_indexes():
            self._loaded = True
            self._available = True
            return True

        # Check if we need to download
        if force_download or self._needs_update():
            if not self._download_data():
                # Try using stale cache if download failed
                if not self._cache_file.exists():
                    self._available = False
                    return False
                logger.warning("Using stale cache after download failure")

        # Load and build indexes
        try:
            logger.info("Loading MTGJSON data...")
            with open(self._cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            self._arena_index, self._name_index = self._build_indexes(data)
            self._save_indexes()

            self._loaded = True
            self._available = True
            logger.info(
                "MTGJSON loaded %d cards (%d with arena_id)",
                len(self._name_index),
                len(self._arena_index),
            )
            return True

        except Exception as e:
            logger.error(f"Failed to load MTGJSON data: {e}")
            self._available = False
            return False

    def get_card(self, arena_id: int) -> Optional[MTGJSONCard]:
        """Look up a card by MTGA arena_id.

        Args:
            arena_id: The MTGA GrpId / arena_id

        Returns:
            MTGJSONCard with card data, or None if not found.
        """
        if not self._available:
            return None

        return self._arena_index.get(arena_id)

    def get_card_by_name(self, name: str) -> Optional[MTGJSONCard]:
        """Look up a card by name (case-insensitive).

        Args:
            name: The card name to look up

        Returns:
            MTGJSONCard with card data, or None if not found.
        """
        if not self._available:
            return None

        # Strip HTML tags that sometimes appear in MTGA names
        clean_name = name
        if "<nobr>" in name:
            clean_name = name.replace("<nobr>", "").replace("</nobr>", "")

        return self._name_index.get(clean_name.lower())


# Global singleton instance
_mtgjson_db: Optional[MTGJSONDatabase] = None


def get_mtgjson() -> MTGJSONDatabase:
    """Get the global MTGJSON database instance.

    Lazily initializes and loads the database on first call.
    """
    global _mtgjson_db
    if _mtgjson_db is None:
        _mtgjson_db = MTGJSONDatabase()
        _mtgjson_db.load()
    return _mtgjson_db
