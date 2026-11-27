"""Additional public metagame & decklist clients (AetherHub, MTGTop8, Moxfield).

Each client caches responses on disk under ``~/.arenamcp/cache/<source>/`` via
``FileCache`` (1-hour TTL) and degrades gracefully to an empty list when a
remote host is unreachable.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from arenamcp.cache_utils import FileCache

logger = logging.getLogger(__name__)

CACHE_DURATION = 3600  # 1 hour
REQUEST_TIMEOUT = 10

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

_CARD_LINE_RE = re.compile(r"^\s*(\d+)\s+(.+?)\s*$")
_MOXFIELD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")


def _utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _validate_https_url(url: str, allowed_hosts: set[str]) -> str | None:
    """Return *url* if it is an https URL on one of *allowed_hosts*, else None."""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc.lower() not in allowed_hosts:
        return None
    return url


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_plain_deck_text(text: str, *, is_brawl: bool = False) -> dict[str, Any]:
    """Parse an MTGO/Arena plain-text decklist export into pile lists.

    Handles both explicit section headers (``Deck``, ``Mainboard``,
    ``Sideboard``, ``Commander``, ``Companion``) and blank-line separated
    exports (MTGO style, where a trailing single-card section in Brawl is the
    commander).
    """
    mainboard: list[str] = []
    sideboard: list[str] = []
    commander: list[str] = []
    companion: list[str] = []

    raw_lines = text.splitlines()
    has_explicit_headers = any(
        line.strip().casefold() in {"deck", "mainboard", "sideboard", "commander", "companion"}
        for line in raw_lines
    )

    if has_explicit_headers:
        current = "mainboard"
        for raw in raw_lines:
            line = raw.strip()
            if not line:
                continue
            low = line.casefold()
            if low in {"deck", "mainboard"}:
                current = "mainboard"
                continue
            if low == "sideboard":
                current = "sideboard"
                continue
            if low == "commander":
                current = "commander"
                continue
            if low == "companion":
                current = "companion"
                continue
            if not _CARD_LINE_RE.match(line):
                continue
            if current == "mainboard":
                mainboard.append(line)
            elif current == "sideboard":
                sideboard.append(line)
            elif current == "commander":
                commander.append(line)
            elif current == "companion":
                companion.append(line)
    else:
        sections: list[list[str]] = []
        buf: list[str] = []
        for raw in raw_lines:
            line = raw.strip()
            if not line:
                if buf:
                    sections.append(buf)
                    buf = []
                continue
            if _CARD_LINE_RE.match(line):
                buf.append(line)
        if buf:
            sections.append(buf)

        if sections:
            mainboard = sections[0]
            if len(sections) >= 2:
                trailing = sections[1]
                if is_brawl and len(trailing) == 1:
                    commander = trailing
                else:
                    sideboard = trailing

    return {
        "mainboard": mainboard,
        "sideboard": sideboard,
        "commander": commander,
        "companion": companion,
        "checked_at": _utc_date(),
    }


class AetherHubClient:
    """Client for AetherHub's MTGA Assistant Arena tracker metagame lists."""

    BASE_URL = "https://aetherhub.com"
    ALLOWED_HOSTS = {"aetherhub.com", "www.aetherhub.com"}
    FORMAT_PATHS = {
        "standard": "/Metagame/Standard-BO1/",
        "brawl": "/Metagame/Historic-Brawl/",
        "historic": "/Metagame/Historic-BO1/",
    }

    def __init__(self, cache_dir: Path | None = None) -> None:
        self._cache = FileCache(
            cache_dir or (Path.home() / ".arenamcp" / "cache" / "aetherhub"),
            CACHE_DURATION,
        )
        self.headers = dict(_DEFAULT_HEADERS)

    def get_metagame(
        self, format_name: str, *, limit: int = 35, force_refresh: bool = False
    ) -> list[dict[str, Any]]:
        fmt = format_name.lower()
        path = self.FORMAT_PATHS.get(fmt)
        if not path:
            return []
        cache_key = f"metagame_{fmt}"
        if not force_refresh:
            cached = self._cache.read(cache_key)
            if isinstance(cached, dict) and isinstance(cached.get("decks"), list):
                return cached["decks"][:limit]

        url = urljoin(self.BASE_URL, path)
        try:
            resp = requests.get(url, headers=self.headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("AetherHub metagame fetch failed (%s): %s", fmt, exc)
            return []

        decks = self._parse_metagame_html(resp.text)
        if decks:
            self._cache.write(cache_key, {"decks": decks})
        return decks[:limit]

    def _parse_metagame_html(self, html_text: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html_text, "html.parser")
        raw_rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for tr in soup.select("tr"):
            link = tr.find("a", href=re.compile(r"/Metagame/[^/]+/Deck/[^\"']+-\d+$"))
            if not link:
                continue
            href = str(link.get("href") or "").strip()
            full_url = _validate_https_url(urljoin(self.BASE_URL, href), self.ALLOWED_HOSTS)
            if not full_url:
                continue
            id_match = re.search(r"-(\d+)$", urlsplit(full_url).path)
            if not id_match:
                continue
            deck_id = id_match.group(1)
            if deck_id in seen_ids:
                continue
            tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            name = ""
            for a_tag in tr.find_all("a", href=href):
                txt = a_tag.get_text(" ", strip=True)
                if txt:
                    name = txt
                    break
            if not name and len(tds) > 1:
                name = tds[1]
            if not name:
                continue
            matches = 0
            for cell in tds:
                m = re.search(r"([\d,]+)\s+matches?", cell, re.I)
                if m:
                    matches = int(m.group(1).replace(",", ""))
                    break
            seen_ids.add(deck_id)
            raw_rows.append(
                {
                    "name": name,
                    "deck_id": deck_id,
                    "url": full_url,
                    "matches": matches,
                }
            )

        total_matches = sum(r["matches"] for r in raw_rows)
        results: list[dict[str, Any]] = []
        for row in raw_rows:
            meta_share = (
                f"{(row['matches'] / total_matches) * 100:.1f}%"
                if total_matches > 0 and row["matches"] > 0
                else ""
            )
            results.append(
                {
                    "name": row["name"],
                    "url": row["url"],
                    "deck_id": row["deck_id"],
                    "meta_share": meta_share,
                    "matches": row["matches"],
                }
            )
        return results

    def get_deck_list(
        self, deck_url: str, *, is_brawl: bool = False, force_refresh: bool = False
    ) -> dict[str, Any]:
        valid_url = _validate_https_url(deck_url, self.ALLOWED_HOSTS)
        if not valid_url:
            raise ValueError(f"Rejected non-AetherHub URL: {deck_url!r}")
        id_match = re.search(r"-(\d+)$", urlsplit(valid_url).path.rstrip("/"))
        if not id_match:
            raise ValueError(f"Cannot extract AetherHub deck ID from {deck_url!r}")
        deck_id = id_match.group(1)
        cache_key = f"deck_{deck_id}_{'brawl' if is_brawl else 'std'}"
        if not force_refresh:
            cached = self._cache.read(cache_key)
            if isinstance(cached, dict) and cached.get("mainboard"):
                return cached

        export_url = f"{self.BASE_URL}/Deck/MtgoDeckExport/{deck_id}"
        resp = requests.get(export_url, headers=self.headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        parsed = _parse_plain_deck_text(resp.text, is_brawl=is_brawl)
        if not parsed["mainboard"]:
            raise ValueError(f"AetherHub export {deck_id} returned no cards")
        self._cache.write(cache_key, parsed)
        return parsed


class MTGTop8Client:
    """Client for MTGTop8 tournament metagame archetypes and recent top-8 lists."""

    BASE_URL = "https://www.mtgtop8.com"
    ALLOWED_HOSTS = {"www.mtgtop8.com", "mtgtop8.com"}
    FORMAT_CODES = {
        "standard": "ST",
        "historic": "HI",
    }

    def __init__(self, cache_dir: Path | None = None) -> None:
        self._cache = FileCache(
            cache_dir or (Path.home() / ".arenamcp" / "cache" / "mtgtop8"),
            CACHE_DURATION,
        )
        self.headers = dict(_DEFAULT_HEADERS)

    def get_metagame(
        self, format_name: str, *, limit: int = 20, force_refresh: bool = False
    ) -> list[dict[str, Any]]:
        code = self.FORMAT_CODES.get(format_name.lower())
        if not code:
            return []
        cache_key = f"metagame_{code}"
        if not force_refresh:
            cached = self._cache.read(cache_key)
            if isinstance(cached, dict) and isinstance(cached.get("decks"), list):
                return cached["decks"][:limit]

        url = f"{self.BASE_URL}/format?f={code}"
        try:
            resp = requests.get(url, headers=self.headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("MTGTop8 metagame fetch failed (%s): %s", code, exc)
            return []

        decks = self._parse_metagame_html(resp.text)
        if decks:
            self._cache.write(cache_key, {"decks": decks})
        return decks[:limit]

    def _parse_metagame_html(self, html_text: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html_text, "html.parser")
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for link in soup.find_all("a", href=re.compile(r"archetype\?a=\d+")):
            href = str(link.get("href") or "").strip()
            full_url = _validate_https_url(urljoin(self.BASE_URL + "/", href), self.ALLOWED_HOSTS)
            name = link.get_text(" ", strip=True)
            if not name or not full_url or full_url in seen:
                continue
            seen.add(full_url)
            meta_share = ""
            # MTGTop8 puts the link in a <div class="S14"> and the share % in a sibling <div>
            # inside the same wrapper <div>.
            parent_div = link.find_parent("div")
            wrapper = parent_div.find_parent("div") if parent_div is not None else None
            if wrapper is not None:
                sib_text = wrapper.get_text(" ", strip=True)
                pct = re.search(r"(\d+(?:\.\d+)?)\s*%", sib_text)
                if pct:
                    meta_share = f"{pct.group(1)}%"
            results.append(
                {
                    "name": name,
                    "url": full_url,
                    "meta_share": meta_share,
                }
            )
        return results

    def get_deck_list(self, archetype_url: str, *, force_refresh: bool = False) -> dict[str, Any]:
        valid_url = _validate_https_url(archetype_url, self.ALLOWED_HOSTS)
        if not valid_url:
            raise ValueError(f"Rejected non-MTGTop8 URL: {archetype_url!r}")
        arch_match = re.search(r"a=(\d+)", valid_url)
        if arch_match:
            cache_key = f"arch_{arch_match.group(1)}"
        else:
            digest = hashlib.sha256(valid_url.encode("utf-8")).hexdigest()[:16]
            cache_key = f"arch_{digest}"
        if not force_refresh:
            cached = self._cache.read(cache_key)
            if isinstance(cached, dict) and cached.get("mainboard"):
                return cached

        resp = requests.get(valid_url, headers=self.headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        deck_match = re.search(r"event\?e=\d+(?:&amp;|&)d=(\d+)", resp.text)
        if not deck_match:
            raise ValueError(f"No decklist found on MTGTop8 archetype page {valid_url!r}")
        deck_id = deck_match.group(1)
        export_resp = requests.get(
            f"{self.BASE_URL}/mtgo?d={deck_id}",
            headers=self.headers,
            timeout=REQUEST_TIMEOUT,
        )
        export_resp.raise_for_status()
        parsed = _parse_plain_deck_text(export_resp.text, is_brawl=False)
        if not parsed["mainboard"]:
            raise ValueError(f"MTGTop8 export d={deck_id} returned no cards")
        self._cache.write(cache_key, parsed)
        return parsed


class MoxfieldClient:
    """Client for Moxfield's public JSON API (popular/recently updated decks)."""

    API_BASE = "https://api2.moxfield.com"
    WEB_BASE = "https://moxfield.com/decks"
    ALLOWED_HOSTS = {"moxfield.com", "www.moxfield.com"}
    FORMAT_MAP = {
        "standard": "standard",
        "brawl": "historicBrawl",
        "historic": "historic",
    }

    def __init__(self, cache_dir: Path | None = None) -> None:
        self._cache = FileCache(
            cache_dir or (Path.home() / ".arenamcp" / "cache" / "moxfield"),
            CACHE_DURATION,
        )
        self.headers = dict(_DEFAULT_HEADERS)

    def get_metagame(
        self, format_name: str, *, limit: int = 15, force_refresh: bool = False
    ) -> list[dict[str, Any]]:
        mox_fmt = self.FORMAT_MAP.get(format_name.lower())
        if not mox_fmt:
            return []
        cache_key = f"search_{mox_fmt}"
        if not force_refresh:
            cached = self._cache.read(cache_key)
            if isinstance(cached, dict) and isinstance(cached.get("decks"), list):
                return cached["decks"][:limit]

        # Request a bit more than `limit` so we can skip incomplete brews.
        page_size = max(25, limit * 2)
        url = (
            f"{self.API_BASE}/v2/decks/search"
            f"?pageNumber=1&pageSize={page_size}"
            f"&sortType=updated&sortDirection=Descending&fmt={mox_fmt}"
        )
        try:
            resp = requests.get(url, headers=self.headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Moxfield search failed (%s): %s", mox_fmt, exc)
            return []

        if not isinstance(payload, dict):
            return []
        raw_items = payload.get("data")
        if not isinstance(raw_items, list):
            return []

        decks: list[dict[str, Any]] = []
        min_main = 99 if format_name.lower() == "brawl" else 60
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            pub_id = str(item.get("publicId") or "").strip()
            name = str(item.get("name") or "").strip()
            main_count = _safe_int(item.get("mainboardCount"), 0)
            if not pub_id or not _MOXFIELD_ID_RE.match(pub_id) or not name or main_count < min_main:
                continue
            decks.append(
                {
                    "name": name,
                    "public_id": pub_id,
                    "url": f"{self.WEB_BASE}/{pub_id}",
                    "meta_share": "",
                }
            )
        if decks:
            self._cache.write(cache_key, {"decks": decks})
        return decks[:limit]

    def get_deck_list(self, public_id_or_url: str, *, force_refresh: bool = False) -> dict[str, Any]:
        raw_id = public_id_or_url.strip()
        if "://" in raw_id:
            valid_url = _validate_https_url(raw_id, self.ALLOWED_HOSTS)
            if not valid_url:
                raise ValueError(f"Rejected non-Moxfield URL: {public_id_or_url!r}")
            pub_id = urlsplit(valid_url).path.rstrip("/").rsplit("/", 1)[-1]
        else:
            pub_id = raw_id.rstrip("/").rsplit("/", 1)[-1]
        if not pub_id or not _MOXFIELD_ID_RE.match(pub_id):
            raise ValueError(f"Invalid Moxfield deck identifier: {public_id_or_url!r}")
        cache_key = f"deck_{pub_id}"
        if not force_refresh:
            cached = self._cache.read(cache_key)
            if isinstance(cached, dict) and cached.get("mainboard"):
                return cached

        url = f"{self.API_BASE}/v3/decks/all/{pub_id}"
        resp = requests.get(url, headers=self.headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict):
            raise ValueError(f"Moxfield deck {pub_id} returned malformed JSON")
        boards = payload.get("boards")
        if not isinstance(boards, dict):
            raise ValueError(f"Moxfield deck {pub_id} missing boards payload")

        def _extract(board_key: str) -> list[str]:
            board = boards.get(board_key)
            if not isinstance(board, dict):
                return []
            cards = board.get("cards")
            if not isinstance(cards, dict):
                return []
            lines: list[str] = []
            for entry in cards.values():
                if not isinstance(entry, dict):
                    continue
                qty = _safe_int(entry.get("quantity"), 0)
                card = entry.get("card")
                if not isinstance(card, dict):
                    continue
                cname = str(card.get("name") or "").strip()
                if qty > 0 and cname:
                    lines.append(f"{qty} {cname}")
            return lines

        parsed = {
            "mainboard": _extract("mainboard"),
            "sideboard": _extract("sideboard"),
            "commander": _extract("commanders"),
            "companion": _extract("companions"),
            "checked_at": _utc_date(),
        }
        if not parsed["mainboard"]:
            raise ValueError(f"Moxfield deck {pub_id} returned no mainboard cards")
        self._cache.write(cache_key, parsed)
        return parsed


class ArchidektClient:
    """Client for Archidekt's public JSON API (Alchemy, Brawl, Explorer, Standard)."""

    API_BASE = "https://archidekt.com/api/decks"
    WEB_BASE = "https://archidekt.com/decks"
    ALLOWED_HOSTS = {"archidekt.com", "www.archidekt.com"}
    FORMAT_MAP = {
        "standard": (1, 60),
        "brawl": (20, 98),
        "alchemy": (18, 60),
        "explorer": (19, 60),
    }

    def __init__(self, cache_dir: Path | None = None) -> None:
        self._cache = FileCache(
            cache_dir or (Path.home() / ".arenamcp" / "cache" / "archidekt"),
            CACHE_DURATION,
        )
        self.headers = dict(_DEFAULT_HEADERS)

    def get_metagame(
        self, format_name: str, *, limit: int = 8, force_refresh: bool = False
    ) -> list[dict[str, Any]]:
        spec = self.FORMAT_MAP.get(format_name.lower())
        if not spec:
            return []
        fid, min_size = spec
        cache_key = f"search_{fid}"
        if not force_refresh:
            cached = self._cache.read(cache_key)
            if isinstance(cached, dict) and isinstance(cached.get("decks"), list):
                return cached["decks"][:limit]

        url = f"{self.API_BASE}/v3/?deckFormat={fid}&orderBy=-updatedAt&pageSize=20"
        try:
            resp = requests.get(url, headers=self.headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429:
                logger.info("Archidekt rate-limited (429) on format %s", fid)
                return []
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Archidekt search failed (%s): %s", fid, exc)
            return []

        raw_items = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(raw_items, list):
            return []
        decks: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            did = _safe_int(item.get("id"), 0)
            name = str(item.get("name") or "").strip()
            size = _safe_int(item.get("size"), 0)
            if did <= 0 or not name or size < min_size:
                continue
            decks.append(
                {
                    "name": name,
                    "deck_id": str(did),
                    "url": f"{self.WEB_BASE}/{did}",
                    "meta_share": "",
                }
            )
        if decks:
            self._cache.write(cache_key, {"decks": decks})
        return decks[:limit]

    def get_deck_list(
        self, id_or_url: str, *, is_brawl: bool = False, force_refresh: bool = False
    ) -> dict[str, Any]:
        raw = str(id_or_url or "").strip()
        if "://" in raw:
            valid = _validate_https_url(raw, self.ALLOWED_HOSTS)
            if not valid:
                raise ValueError(f"Rejected non-Archidekt URL: {id_or_url!r}")
            did = urlsplit(valid).path.rstrip("/").rsplit("/", 1)[-1]
        else:
            did = raw.rstrip("/").rsplit("/", 1)[-1]
        if not did.isdigit():
            raise ValueError(f"Invalid Archidekt deck identifier: {id_or_url!r}")

        cache_key = f"deck_{did}"
        if not force_refresh:
            cached = self._cache.read(cache_key)
            if isinstance(cached, dict) and cached.get("mainboard"):
                return cached

        resp = requests.get(f"{self.API_BASE}/{did}/", headers=self.headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict):
            raise ValueError(f"Archidekt deck {did} returned malformed JSON")

        included_cats = {
            str(cat.get("name") or "").casefold(): bool(cat.get("includedInDeck", True))
            for cat in (payload.get("categories") or [])
            if isinstance(cat, dict) and cat.get("name")
        }
        mainboard: list[str] = []
        sideboard: list[str] = []
        commander: list[str] = []
        companion: list[str] = []

        for item in payload.get("cards") or []:
            if not isinstance(item, dict):
                continue
            qty = _safe_int(item.get("quantity"), 0)
            if qty <= 0:
                continue
            cats = [str(c).casefold() for c in (item.get("categories") or [])]
            if any("maybeboard" in c for c in cats) or any(included_cats.get(c) is False for c in cats):
                continue
            card = item.get("card") or {}
            oracle = card.get("oracleCard") or {}
            cname = str(oracle.get("name") or card.get("name") or "").strip()
            if not cname:
                continue
            line = f"{qty} {cname}"
            if item.get("companion") or "companion" in cats:
                companion.append(line)
            elif "commander" in cats:
                commander.append(line)
            elif "sideboard" in cats and not is_brawl:
                sideboard.append(line)
            else:
                mainboard.append(line)

        if not mainboard:
            raise ValueError(f"Archidekt deck {did} returned no cards")
        parsed = {
            "mainboard": mainboard,
            "sideboard": sideboard,
            "commander": commander,
            "companion": companion,
            "checked_at": _utc_date(),
        }
        self._cache.write(cache_key, parsed)
        return parsed


class MTGAZoneClient:
    """Client for MTG Arena Zone public tier lists (Timeless, Alchemy, Historic, Explorer)."""

    BASE_URL = "https://mtgazone.com"
    ALLOWED_HOSTS = {"mtgazone.com", "www.mtgazone.com"}
    TIER_URLS = {
        "timeless": "https://mtgazone.com/timeless-bo1-metagame-tier-list/",
        "alchemy": "https://mtgazone.com/alchemy-bo1-metagame-tier-list/",
        "historic": "https://mtgazone.com/historic-bo1-metagame-tier-list/",
        "explorer": "https://mtgazone.com/explorer-bo1-metagame-tier-list/",
    }

    def __init__(self, cache_dir: Path | None = None) -> None:
        self._cache = FileCache(
            cache_dir or (Path.home() / ".arenamcp" / "cache" / "mtgazone"),
            CACHE_DURATION,
        )
        self.headers = dict(_DEFAULT_HEADERS)

    @staticmethod
    def _parse_tier_html(html_text: str, *, page_url: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html_text, "html.parser")
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for blk in soup.select("div.deck-block"):
            name_el = blk.select_one(".name, .deck-name")
            name = name_el.get_text(" ", strip=True) if name_el else ""
            if not name or name.lower() in seen:
                continue
            main: list[str] = []
            side: list[str] = []
            cmd: list[str] = []
            comp: list[str] = []
            for sec in blk.select("div.decklist"):
                cls = set(sec.get("class") or [])
                if "info" in cls:
                    continue
                target = (
                    side
                    if "sideboard" in cls
                    else (cmd if "commander" in cls else (comp if "companion" in cls else main))
                )
                for card in sec.select("[data-name][data-quantity]"):
                    cname = str(card.get("data-name") or "").strip()
                    qty = _safe_int(card.get("data-quantity"), 0)
                    if cname and qty > 0:
                        target.append(f"{qty} {cname}")
            if not main:
                continue
            seen.add(name.lower())
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or blk.get("id") or str(len(out))
            out.append(
                {
                    "name": name,
                    "url": f"{page_url.rstrip('/')}#{slug}",
                    "meta_share": "",
                    "deck": {
                        "mainboard": main,
                        "sideboard": side,
                        "commander": cmd,
                        "companion": comp,
                        "checked_at": _utc_date(),
                    },
                }
            )
        return out

    def get_metagame(self, format_name: str, *, force_refresh: bool = False) -> list[dict[str, Any]]:
        fmt = format_name.lower()
        page_url = self.TIER_URLS.get(fmt)
        if not page_url:
            return []
        cache_key = f"tier_{fmt}"
        if not force_refresh:
            cached = self._cache.read(cache_key)
            if isinstance(cached, dict) and isinstance(cached.get("entries"), list):
                return cached["entries"]
        try:
            resp = requests.get(page_url, headers=self.headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("MTGAZone %s tier list failed: %s", fmt, exc)
            return []

        parsed_rows = self._parse_tier_html(resp.text, page_url=page_url)
        entries: list[dict[str, Any]] = []
        for row in parsed_rows:
            deck_id = hashlib.sha256(row["url"].encode("utf-8")).hexdigest()[:16]
            self._cache.write(f"deck_{deck_id}", row["deck"])
            entries.append(
                {
                    "name": row["name"],
                    "url": row["url"],
                    "cache_key": f"deck_{deck_id}",
                    "meta_share": row["meta_share"],
                }
            )
        if entries:
            self._cache.write(cache_key, {"entries": entries})
        return entries

    def get_deck_list(self, url_or_key: str) -> dict[str, Any]:
        raw = str(url_or_key or "").strip()
        if raw.startswith("deck_"):
            cached = self._cache.read(raw)
            if isinstance(cached, dict) and cached.get("mainboard"):
                return cached
            raise ValueError(f"MTGAZone deck cache miss for {raw!r}")
        valid = _validate_https_url(raw, self.ALLOWED_HOSTS)
        if not valid:
            raise ValueError(f"Rejected non-MTGAZone URL: {url_or_key!r}")
        deck_id = hashlib.sha256(valid.encode("utf-8")).hexdigest()[:16]
        cached = self._cache.read(f"deck_{deck_id}")
        if isinstance(cached, dict) and cached.get("mainboard"):
            return cached
        raise ValueError(f"MTGAZone deck not cached for {url_or_key!r}")
