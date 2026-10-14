"""Optional online research for a set's draft strategy (setting ``draft_research_online``).

The set primer is local-only by default: 17lands data, card rules and the
model's synthesis. With the setting on, this module also gathers what Limited
players have written about the set -- draft guides, set reviews, tier lists --
and hands a short digest to the primer synthesis as community opinion to weigh
against the data. The 2026-10-13 comparison against online guides found the
local primer missing signpost uncommons, misreading format speed, and giving
three archetypes the wrong engine; guides and reviews carry exactly that.

Pages come from a web search restricted to Limited-strategy sites. Web text is
untrusted: it is only ever prompt data for the synthesis, whose answers are
validated against the set (unknown card names are dropped). Results are cached
per set for RESEARCH_MAX_AGE_S. Any failure returns an empty digest.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

logger = logging.getLogger(__name__)

RESEARCH_DIR = Path.home() / ".arenamcp" / "cache" / "set_research"
RESEARCH_MAX_AGE_S = 3 * 24 * 3600
SEARCH_URL = "https://html.duckduckgo.com/html/?q={query}"
SCRYFALL_SET_URL = "https://api.scryfall.com/sets/{code}"
HEADERS = {
    "User-Agent": "mtgacoach/3.5 (+https://github.com/josharmour/mtgacoach)",
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
}
# Sites that publish Limited strategy; anything else in the results is skipped.
TRUSTED_SITES = (
    "draftsim.com",
    "mtgazone.com",
    "17lands.com",
    "lrcast.com",
    "cardgamebase.com",
    "flipsidegaming.com",
    "magic.wizards.com",
    "scrollvault.net",
    "preconforge.com",
    "cardkingdom.com",
    "tcgplayer.com",
    "channelfireball.com",
    "starcitygames.com",
    "nizzahon.com",
    "mtggoldfish.com",
    "hipstersofthecoast.com",
)
QUERIES = (
    "{name} draft guide archetypes",
    "{name} limited set review best commons",
    "{name} draft tier list color pairs",
)
MAX_SOURCES = 4
SOURCE_CHARS = 2400
DIGEST_CHARS = 8000
TIMEOUT_S = 15.0
# Paragraphs worth keeping: they talk about archetypes, picks or colors.
_RELEVANT = re.compile(
    r"\b(?:archetype|signpost|common|uncommon|removal|pick|tier|aggro|control|tempo|splash|"
    r"synerg|payoff|enabler|bomb|speed|white|blue|black|red|green|azorius|dimir|rakdos|gruul|"
    r"selesnya|orzhov|izzet|golgari|boros|simic)",
    re.IGNORECASE,
)


def _get(url: str, session: Any, timeout: float = TIMEOUT_S) -> str:
    response = session.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.text


def set_name(set_code: str, session: Any) -> str:
    """The set's English name from Scryfall ("FRA" -> "Reality Fracture"), or the code."""
    try:
        data = json.loads(_get(SCRYFALL_SET_URL.format(code=set_code.lower()), session))
        return str(data.get("name") or set_code)
    except Exception as exc:
        logger.debug("Scryfall set name for %s unavailable: %s", set_code, exc)
        return set_code


def _result_links(page: str) -> list[str]:
    """Target URLs from a DuckDuckGo HTML result page."""
    links = []
    for href in re.findall(r'class="result__a"[^>]*href="([^"]+)"', page):
        href = html.unescape(href)
        if "uddg=" in href:
            target = parse_qs(urlparse(href).query).get("uddg", [""])[0]
            href = unquote(target)
        if href.startswith("http"):
            links.append(href)
    return links


def _trusted(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host == site or host.endswith("." + site) for site in TRUSTED_SITES)


def page_text(page: str) -> tuple[str, str]:
    """(title, readable paragraphs) from an HTML page, scripts and navigation removed."""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", page, re.IGNORECASE | re.DOTALL)
    title = html.unescape(re.sub(r"\s+", " ", title_match.group(1))).strip() if title_match else ""
    body = re.sub(r"(?is)<(script|style|nav|header|footer|aside|form|noscript)[^>]*>.*?</\1>", " ", page)
    blocks = re.split(r"(?i)</?(?:p|li|h[1-6]|tr|div|br)[^>]*>", body)
    paragraphs = []
    for block in blocks:
        text = html.unescape(re.sub(r"<[^>]+>", " ", block))
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) >= 40 and _RELEVANT.search(text):
            paragraphs.append(text)
    return title, "\n".join(dict.fromkeys(paragraphs))


def _cache_path(set_code: str, cache_dir: Path) -> Path:
    return cache_dir / f"{set_code.upper()}.json"


def fetch_set_research(
    set_code: str,
    *,
    session: Any = None,
    cache_dir: Path = RESEARCH_DIR,
    max_sources: int = MAX_SOURCES,
) -> dict[str, Any]:
    """{"digest": text, "sources": [{"title", "url"}], "set_name"} for the synthesis; empty on failure."""
    empty: dict[str, Any] = {"digest": "", "sources": [], "set_name": set_code}
    path = _cache_path(set_code, cache_dir)
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - float(cached.get("fetched_at") or 0) < RESEARCH_MAX_AGE_S and cached.get("digest"):
            return cached
    except (OSError, ValueError):
        pass
    if session is None:
        import requests

        session = requests.Session()
    name = set_name(set_code, session)
    seen: list[str] = []
    for template in QUERIES:
        query = quote_plus(template.format(name=f'"{name}"' if name != set_code else set_code))
        try:
            links = _result_links(_get(SEARCH_URL.format(query=query), session))
        except Exception as exc:
            logger.info("Set research search failed (%s): %s", template, exc)
            continue
        for link in links:
            if _trusted(link) and link not in seen:
                seen.append(link)
        if len(seen) >= max_sources * 2:
            break
    sources, parts, total = [], [], 0
    for url in seen:
        if len(sources) >= max_sources or total >= DIGEST_CHARS:
            break
        try:
            title, text = page_text(_get(url, session))
        except Exception as exc:
            logger.info("Set research page skipped %s: %s", url, exc)
            continue
        if name.lower() not in (title + " " + text[:4000]).lower():
            continue  # a search hit about some other set
        excerpt = text[: min(SOURCE_CHARS, DIGEST_CHARS - total)]
        if len(excerpt) < 200:
            continue
        sources.append({"title": title[:120], "url": url})
        parts.append(f"## {title[:120]} ({urlparse(url).netloc})\n{excerpt}")
        total += len(excerpt)
    if not parts:
        logger.info("Set research for %s found no usable sources", set_code)
        return empty
    result = {
        "digest": "\n\n".join(parts),
        "sources": sources,
        "set_name": name,
        "fetched_at": time.time(),
    }
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        logger.debug("Set research cache not written: %s", exc)
    logger.info("Set research for %s: %d sources, %d chars", set_code, len(sources), total)
    return result
