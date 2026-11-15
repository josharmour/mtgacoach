"""MTGGoldfish scraper for fetching metagame data.

Scrapes metagame breakdowns and deck lists from MTGGoldfish.
Caches responses for 1 hour.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from arenamcp.cache_utils import FileCache

logger = logging.getLogger(__name__)

CACHE_DIR = Path.home() / ".arenamcp" / "cache" / "mtggoldfish"
CACHE_DURATION = 3600  # 1 hour

_EXPORT_CALL_RE = re.compile(r"initializeDeckComponents\(\s*'[^']*',\s*'[^']*',\s*(\"(?:\\.|[^\"\\])*\")")


def _declared_commander(trailer: str) -> str:
    """Return the commander name the page declares after the export payload.

    Brawl deck pages pass the commander as a later argument of the same
    ``initializeDeckComponents`` call, after the format name, e.g.
    ``..., false, 'brawl', '', 'Rofellos, Llanowar Emissary', ...``.
    Non-commander pages pass an empty string there.
    """
    string = r"'(?:\\.|[^'\\])*'"
    match = re.match(
        rf"\s*,\s*{string}\s*,\s*(?:true|false)\s*,\s*'brawl'\s*,\s*{string}\s*,\s*'((?:\\.|[^'\\])*)'",
        trailer[:500],
        re.I,
    )
    if not match:
        return ""
    # Only these two JavaScript escapes are needed for an English card name.
    return re.sub(r"\\(['\\])", r"\1", match[1]).strip()


def _line_card_name(line: str) -> str:
    """Return the card name of a ``'<quantity> <name>'`` list line."""
    match = re.match(r"\s*\d+\s+(.+?)\s*$", line)
    return match.group(1) if match else ""


def _designate_commander(deck_data: dict[str, Any], commander_name: str) -> None:
    """Move the page-declared commander card out of the mainboard list.

    The exported payload lists the commander as an ordinary mainboard line;
    the page's own trailing arguments are what say it is the commander. Only
    an unambiguous single match is moved — anything else stays where it is.
    """
    if not commander_name:
        return
    wanted = commander_name.casefold()
    if deck_data.get("commander"):
        if (
            len(deck_data["commander"]) != 1
            or _line_card_name(deck_data["commander"][0]).casefold() != wanted
        ):
            raise ValueError("Commander metadata disagrees with the public list")
        return
    matches = [line for line in deck_data["mainboard"] if _line_card_name(line).casefold() == wanted]
    if len(matches) == 1:
        deck_data["mainboard"].remove(matches[0])
        deck_data["commander"].append(matches[0])


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

    def _read_cache(self, key: str) -> dict[str, Any] | None:
        """Read data from cache if available and valid."""
        return self._cache.read(key)

    def _write_cache(self, key: str, data: dict[str, Any]) -> None:
        """Write data to cache."""
        self._cache.write(key, data)

    def get_metagame(self, format_name: str, force_refresh: bool = False) -> list[dict[str, Any]]:
        """Get metagame breakdown for a specific format.

        Args:
            format_name: Format name (standard, modern, pioneer, legacy, pauper).
            force_refresh: Bypass cache.

        Returns:
            Every archetype the page lists — the main paper-metagame tiles and
            the page's budget deck lists — as dicts with name, meta_share,
            url, colors and a ``budget`` flag. Budget tiles carry no metagame
            share (their first statistic is a tabletop price), so their
            ``meta_share`` is empty.
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

            # Every archetype the page lists, including its budget deck
            # section: paper popularity alone favours expensive lists, and a
            # customer with few wildcards needs the budget builds too.
            for tile in soup.select(".archetype-tile"):
                in_budget = tile.find_parent(class_="budget-decks-container") is not None
                deck_data = self._parse_tile(tile, budget=in_budget)
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

    def _parse_tile(self, tile, budget: bool = False) -> dict[str, Any] | None:
        """Parse a single archetype tile.

        ``budget`` marks tiles from the page's budget deck section. Their
        first statistic value is a tabletop price, not a metagame share, so
        the share is left empty rather than mislabelled.
        """
        try:
            title_tag = tile.select_one(".deck-price-paper a")
            if not title_tag:
                title_tag = tile.select_one(".archetype-tile-title a")
            if not title_tag:
                return None

            name = title_tag.text.strip()
            relative_url = title_tag["href"]
            url = urljoin(self.BASE_URL, relative_url)
            source = urlsplit(url)
            if (
                source.scheme != "https"
                or source.netloc != "www.mtggoldfish.com"
                or not re.fullmatch(r"/(?:archetype/[\w-]+|deck/[0-9]+)/?", source.path)
            ):
                return None

            share_tag = tile.select_one(".archetype-tile-statistic-value")
            # The statistic value also carries the sampled deck count, e.g.
            # "12.0% (441)" on the live page; keep only the percentage.
            share_text = share_tag.get_text(" ", strip=True) if share_tag else ""
            share_match = re.match(r"\d+(?:\.\d+)?%", share_text)
            meta_share = "" if budget else (share_match.group(0) if share_match else "N/A")

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
                "budget": budget,
            }
        except Exception:
            return None

    def get_deck_list(self, url: str, force_refresh: bool = False) -> dict[str, Any]:
        """Scrape a specific deck page to get the full card list.

        Args:
            url: Full URL to the deck page.
            force_refresh: Bypass cache.

        Returns:
            Dict with mainboard and sideboard lists of formatted strings.
        """
        cache_key = "deck_" + urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]

        if not force_refresh:
            cached = self._read_cache(cache_key)
            if cached and cached.get("mainboard"):
                logger.debug("Using cached deck list")
                # Entries cached before commander tracking have no key.
                cached.setdefault("commander", [])
                if not cached.get("checked_at"):
                    try:
                        saved_at = self._cache.get_cache_path(cache_key).stat().st_mtime
                        cached["checked_at"] = datetime.fromtimestamp(saved_at, timezone.utc).isoformat()
                    except OSError:
                        pass
                return cached

        logger.info(f"Scraping deck list from {url}...")

        try:
            response = requests.get(url, headers=self.headers, timeout=10)
            response.raise_for_status()

            soup = BeautifulSoup(response.content, "html.parser")
            deck_data: dict[str, Any] = {
                "mainboard": [],
                "sideboard": [],
                "commander": [],
                "companion": [],
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }

            # The current public page renders its list from a URL-encoded
            # argument to initializeDeckComponents. Read data only; never run JS.
            for script in soup.find_all("script"):
                text = script.get_text()
                match = _EXPORT_CALL_RE.search(text)
                if match:
                    exported = unquote(json.loads(match.group(1)))
                    commander_name = _declared_commander(text[match.end() :])
                    from arenamcp.deck_candidates import split_export

                    deck_data.update(split_export(exported))
                    _designate_commander(deck_data, commander_name)
                    if deck_data["mainboard"]:
                        deck_data.update(fetched_at=time.time(), url=url)
                        self._write_cache(cache_key, deck_data)
                        return deck_data

            # Try clipboard textarea first (cleaner)
            textarea = soup.select_one("textarea.copy-paste-box")
            if textarea:
                from arenamcp.deck_candidates import split_export

                deck_data.update(split_export(textarea.text))
                deck_data.update(fetched_at=time.time(), url=url)
                self._write_cache(cache_key, deck_data)
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
                    elif header and "companion" in header.text.lower():
                        current_section = "companion"
                    elif header and "commander" in header.text.lower():
                        current_section = "commander"
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
