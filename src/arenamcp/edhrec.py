"""EDHREC integration for Commander metagame statistics.

Uses pyedhrec for detailed commander data and BeautifulSoup scraping
for trending commanders. Caches responses for 24 hours.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup
from pyedhrec import EDHRec

from arenamcp.cache_utils import FileCache

logger = logging.getLogger(__name__)

CACHE_DIR = Path.home() / ".arenamcp" / "cache" / "edhrec"
CACHE_DURATION = 86400  # 24 hours


class EDHRECClient:
    """Client for accessing EDHREC Commander metagame data."""

    BASE_URL = "https://edhrec.com"

    def __init__(self) -> None:
        """Initialize EDHREC client."""
        self._cache = FileCache(CACHE_DIR, CACHE_DURATION)
        self.edhrec_lib = EDHRec()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "ArenaMCP/1.0 (Educational MTG AI Project)"
        })

    def _read_cache(self, key: str) -> Optional[dict[str, Any]]:
        """Read data from cache if available and valid."""
        return self._cache.read(key)

    def _write_cache(self, key: str, data: dict[str, Any]) -> None:
        """Write data to cache."""
        self._cache.write(key, data)

    def get_commander_page(
        self, commander_name: str, force_refresh: bool = False
    ) -> dict[str, Any]:
        """Fetch EDHREC page data for a specific commander.

        Args:
            commander_name: Commander card name.
            force_refresh: Bypass cache.

        Returns:
            Dict with commander, url, cards, themes, meta keys.
        """
        cache_key = f"commander_{commander_name.lower().replace(' ', '-')}"

        if not force_refresh:
            cached = self._read_cache(cache_key)
            if cached:
                logger.debug(f"Using cached data for {commander_name}")
                return cached

        logger.info(f"Fetching EDHREC data for {commander_name}...")

        try:
            raw_data = self.edhrec_lib.get_commander_data(commander_name)
            if not raw_data:
                raise ValueError(f"No data returned for {commander_name}")

            container = raw_data.get("container", {})
            json_dict = container.get("json_dict", {})

            data = {
                "commander": commander_name,
                "url": (
                    f"{self.BASE_URL}/commanders/"
                    f"{commander_name.lower().replace(' ', '-').replace(',', '')}"
                ),
                "fetched_at": time.time(),
                "cards": self._parse_cardlists(json_dict.get("cardlists", [])),
                "themes": self._parse_themes(
                    raw_data.get("panels", {}).get("taglinks", [])
                ),
                "meta": self._parse_meta(json_dict.get("card", {})),
            }

            self._write_cache(cache_key, data)
            return data

        except Exception as e:
            logger.error(f"Error fetching EDHREC data for {commander_name}: {e}")
            return {"commander": commander_name, "error": str(e)}

    def _parse_cardlists(
        self, cardlists: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Parse cardlists from raw EDHREC JSON."""
        cards = []
        for cl in cardlists:
            category = cl.get("header", "Unknown")
            if category == "New Cards":
                continue
            for card in cl.get("cardviews", []):
                cards.append({
                    "name": card.get("name"),
                    "synergy": card.get("synergy"),
                    "inclusion": card.get("inclusion"),
                    "num_decks": card.get("num_decks"),
                    "category": category,
                })
        return cards

    def _parse_themes(
        self, taglinks: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Parse themes from raw EDHREC JSON."""
        return [
            {
                "name": tag.get("value"),
                "slug": tag.get("slug"),
                "url": f"{self.BASE_URL}/themes/{tag.get('slug', '')}",
            }
            for tag in taglinks
        ]

    def _parse_meta(self, card_info: dict[str, Any]) -> dict[str, Any]:
        """Parse metadata from raw EDHREC JSON."""
        return {
            "rank": card_info.get("rank"),
            "total_decks": card_info.get("num_decks"),
            "salt_score": card_info.get("salt"),
        }

    def get_top_commanders(
        self, timeframe: str = "week", force_refresh: bool = False
    ) -> list[dict[str, Any]]:
        """Get list of top/trending commanders.

        Args:
            timeframe: Time period ("week", "month").
            force_refresh: Bypass cache.

        Returns:
            List of commander dicts with name and url.
        """
        cache_key = f"top_commanders_{timeframe}"

        if not force_refresh:
            cached = self._read_cache(cache_key)
            if cached:
                logger.debug(f"Using cached top commanders for {timeframe}")
                return cached.get("commanders", [])

        url = f"{self.BASE_URL}/commanders"
        logger.info(f"Fetching top commanders from {url}...")

        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")

            # Extract __NEXT_DATA__
            script_tag = soup.find(
                "script", id="__NEXT_DATA__", type="application/json"
            )
            if not script_tag:
                logger.warning("Could not find __NEXT_DATA__ on commanders page")
                return []

            next_data = json.loads(script_tag.string)

            commanders = []
            seen_names: set[str] = set()

            try:
                data_section = (
                    next_data.get("props", {})
                    .get("pageProps", {})
                    .get("data", {})
                )
                container = data_section.get("container", {})
                json_dict = container.get("json_dict", {})
                cardlists = json_dict.get("cardlists", [])

                for cl in cardlists:
                    for card in cl.get("cardviews", []):
                        name = card.get("name")
                        if name and name not in seen_names:
                            commanders.append({
                                "name": name,
                                "url": f"{self.BASE_URL}{card.get('url', '')}",
                            })
                            seen_names.add(name)
            except Exception as e:
                logger.warning(f"Error parsing NEXT_DATA for commanders: {e}")

            # Fallback to link scraping
            if not commanders:
                logger.info("Falling back to simple link scraping...")
                links = soup.find_all(
                    "a",
                    href=lambda x: x and "/commanders/" in x if x else False,
                )
                for link in links:
                    href = link.get("href", "")
                    slug = href.split("/commanders/")[-1].split("/")[0]
                    if not slug or len(slug) <= 2:
                        continue
                    if any(x in slug for x in ("?", "#", "theme", "partner")):
                        continue
                    name = slug.replace("-", " ").title()
                    if name not in seen_names:
                        commanders.append({"name": name})
                        seen_names.add(name)

            data = {
                "commanders": commanders[:50],
                "fetched_at": time.time(),
            }
            self._write_cache(cache_key, data)
            return commanders[:50]

        except Exception as e:
            logger.error(f"Error fetching top commanders: {e}")
            return []
