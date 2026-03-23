"""Shared file-based caching utilities.

Provides a reusable FileCache class that handles cache directory creation,
cache path computation, TTL-based validity checking, and JSON read/write.
Used by edhrec, mtggoldfish, draftstats, and scryfall modules to avoid
duplicating cache logic.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class FileCache:
    """Generic file-based JSON cache with configurable TTL.

    Handles:
    - Cache directory creation (with parents)
    - Cache path computation from string keys (sanitised to filesystem-safe names)
    - Cache validity checking via file mtime + configurable TTL
    - Reading/writing JSON data with error handling

    Args:
        cache_dir: Directory where cache files are stored.
        ttl_seconds: Time-to-live in seconds for cached entries.
    """

    def __init__(self, cache_dir: Path, ttl_seconds: int) -> None:
        self.cache_dir = cache_dir
        self.ttl_seconds = ttl_seconds
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get_cache_path(self, key: str) -> Path:
        """Get cache file path for a given key.

        The key is sanitised so that only alphanumeric characters, hyphens,
        and underscores are kept; everything else becomes an underscore.
        """
        safe_key = "".join(
            c if c.isalnum() or c in ("-", "_") else "_" for c in key
        )
        return self.cache_dir / f"{safe_key}.json"

    def is_cache_valid(self, cache_path: Path) -> bool:
        """Check if a cache file exists and is younger than ttl_seconds."""
        if not cache_path.exists():
            return False
        age = time.time() - cache_path.stat().st_mtime
        return age < self.ttl_seconds

    def read(self, key: str) -> Optional[dict[str, Any]]:
        """Read data from cache if available and still valid.

        Returns:
            Parsed JSON dict, or None if the cache is missing/stale/corrupt.
        """
        cache_path = self.get_cache_path(key)
        if self.is_cache_valid(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to read cache for {key}: {e}")
        return None

    def write(self, key: str, data: dict[str, Any]) -> None:
        """Write data to the cache as JSON."""
        cache_path = self.get_cache_path(key)
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to write cache for {key}: {e}")
