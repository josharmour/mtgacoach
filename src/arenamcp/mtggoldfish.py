"""MTGGoldfish scraper for fetching metagame data.

Scrapes metagame breakdowns and deck lists from MTGGoldfish.
Caches responses for 1 hour.
"""

import logging
import time
from pathlib import Path
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup

from arenamcp.cache_utils import FileCache

logger = logging.getLogger(__name__)

CACHE_DIR = Path.home() / ".arenamcp" / "cache" / "mtggoldfish"
CACHE_DURATION = 3600  # 1 hour


class MTGGoldfishClient:
    """Client for scraping metagame data from MTGGoldfish."""

    BASE_URL = "https://www.mtggoldfish.com"

    def __init__(self) -> None:
        """Initialize the client."""
        self._cache = FileCache(CACHE_DIR, CACHE_DURATION)
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/91.0.4472.124 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }

    def _read_cache(self, key: str) -> Optional[dict[str, Any]]:
        """Read data from cache if available and valid."""
        return self._cache.read(key)

    def _write_cache(self, key: str, data: dict[str, Any]) -> None:
        """Write data to cache."""
        self._cache.write(key, data)

    def get_metagame(
        self, format_name: str, force_refresh: bool = False
    ) -> list[dict[str, Any]]:
        """Get metagame breakdown for a specific format.

        Args:
            format_name: Format name (standard, modern, pioneer, legacy, pauper).
            force_refresh: Bypass cache.

        Returns:
            List of deck dicts with name, meta_share, url, colors.
        """
        format_name = format_name.lower()
        cache_key = f"metagame_{format_name}"

        if not force_refresh:
            cached = self._read_cache(cache_key)
            if cached:
                logger.debug(f"Using cached metagame for {format_name}")
                return cached.get("decks", [])

        url = f"{self.BASE_URL}/metagame/{format_name}#paper"
        logger.info(f"Scraping metagame from {url}...")

        try:
            response = requests.get(url, headers=self.headers, timeout=10)
            response.raise_for_status()

            soup = BeautifulSoup(response.content, "html.parser")
            decks = []

            tiles = soup.select(".archetype-tile")
            for tile in tiles[:12]:
                deck_data = self._parse_tile(tile)
                if deck_data:
                    decks.append(deck_data)

            data = {
                "decks": decks,
                "format": format_name,
                "fetched_at": time.time(),
            }
            self._write_cache(cache_key, data)

            logger.info(f"Found {len(decks)} decks in {format_name} metagame")
            return decks

        except Exception as e:
            logger.error(f"Error scraping MTGGoldfish: {e}")
            return []

    def _parse_tile(self, tile) -> Optional[dict[str, Any]]:
        """Parse a single archetype tile."""
        try:
            title_tag = tile.select_one(".deck-price-paper a")
            if not title_tag:
                title_tag = tile.select_one(".archetype-tile-title a")
            if not title_tag:
                return None

            name = title_tag.text.strip()
            relative_url = title_tag["href"]
            url = f"{self.BASE_URL}{relative_url}"

            share_tag = tile.select_one(".archetype-tile-statistic-value")
            meta_share = share_tag.text.strip() if share_tag else "N/A"

            colors = []
            manacost_tag = tile.select_one(".manacost-container")
            if manacost_tag:
                for img in manacost_tag.select("img"):
                    alt = img.get("alt", "")
                    if alt:
                        colors.append(alt)

            return {
                "name": name,
                "meta_share": meta_share,
                "url": url,
                "colors": colors,
            }
        except Exception:
            return None

    def get_deck_list(
        self, url: str, force_refresh: bool = False
    ) -> dict[str, Any]:
        """Scrape a specific deck page to get the full card list.

        Args:
            url: Full URL to the deck page.
            force_refresh: Bypass cache.

        Returns:
            Dict with mainboard and sideboard lists of formatted strings.
        """
        cache_key = f"deck_{url.split('/')[-1].split('#')[0]}"

        if not force_refresh:
            cached = self._read_cache(cache_key)
            if cached:
                logger.debug("Using cached deck list")
                return cached

        logger.info(f"Scraping deck list from {url}...")

        try:
            response = requests.get(url, headers=self.headers, timeout=10)
            response.raise_for_status()

            soup = BeautifulSoup(response.content, "html.parser")
            deck_data: dict[str, Any] = {"mainboard": [], "sideboard": []}

            # Try clipboard textarea first (cleaner)
            textarea = soup.select_one("textarea.copy-paste-box")
            if textarea:
                full_text = textarea.text.strip()
                current_section = "mainboard"
                for line in full_text.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    if line.lower() == "sideboard":
                        current_section = "sideboard"
                        continue
                    deck_data[current_section].append(line)
                return deck_data

            # Fallback to table parsing
            rows = soup.select("tr")
            current_section = "mainboard"
            for row in rows:
                qty_cell = row.select_one(".deck-col-qty")
                card_cell = row.select_one(".deck-col-card a")
                if not qty_cell or not card_cell:
                    header = row.select_one("th") or row.select_one("h3")
                    if header and "sideboard" in header.text.lower():
                        current_section = "sideboard"
                    continue
                qty = qty_cell.text.strip()
                name = card_cell.text.strip()
                deck_data[current_section].append(f"{qty} {name}")

            deck_data["fetched_at"] = time.time()
            deck_data["url"] = url
            self._write_cache(cache_key, deck_data)
            return deck_data

        except Exception as e:
            logger.error(f"Error getting deck list: {e}")
            return {"mainboard": [], "sideboard": [], "error": str(e)}
