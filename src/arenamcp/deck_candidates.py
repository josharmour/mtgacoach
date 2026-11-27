"""Collection-aware comparison of sourced decklists, without spending wildcards."""

from __future__ import annotations

import html
import logging
import math
import os
import pathlib
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

logger = logging.getLogger(__name__)

RARITIES = ("common", "uncommon", "rare", "mythic")


def parse_deck(lines: list[str]) -> Counter:
    cards = Counter()
    for line in lines:
        match = re.fullmatch(r"\s*(\d+)\s+(.+?)\s*", line)
        if match:
            count, name = match.groups()
            # Arena's optional set code / collector number is not part of a name.
            name = re.sub(r"\s+\([A-Za-z0-9]+\)\s+\S+$", "", name)
            if 0 < int(count) <= 250:
                cards[name] += int(count)
            else:
                raise ValueError(f"Invalid card count: {line}")
        elif line.strip() and line.strip().lower() not in ("deck", "sideboard", "commander", "companion"):
            raise ValueError(f"Unrecognized deck line: {line}")
    return cards


def collection_index(collection: dict[int, int], database) -> tuple[Counter, dict]:
    owned = Counter()
    info = {}
    for arena_id, count in collection.items():
        card = database.get_card_by_arena_id(int(arena_id))
        if card:
            owned[card.name] += int(count)
            info[card.name] = card
    return owned, info


def compare_collection(
    deck: dict, collection: dict[int, int], wildcards: dict, database, *, index=None
) -> dict:
    """Count owned copies across printings; never treat unknown cards as free.

    Main, sideboard and commander need separate copies. Basic lands require
    no wildcards. Prices are estimates; Arena verifies legal cards and costs
    during import.
    """
    main, side, commander, companion = _piles(deck, database)
    for name, count in companion.items():
        # The designation refers to the same physical copy in the sideboard.
        side[name] = max(side[name], count)
    required = main + side + commander
    owned, owned_info = index if index is not None else collection_index(collection, database)
    spend = Counter()
    missing = {}
    unknown = []
    get_raw = getattr(database, "get_raw_mtgadb", None)
    local_database = get_raw() if callable(get_raw) else None
    for name, count in required.items():
        shortfall = max(0, count - owned[name])
        if not shortfall:
            continue
        if name in {"Plains", "Island", "Swamp", "Mountain", "Forest"}:
            continue
        missing[name] = shortfall
        # Name lookups in MTGJSON may have neither rarity nor an Arena id.
        # Arena's local database knows the cheapest non-token printing.
        rarity = local_database.get_rarity_by_name(name) if local_database is not None else None
        if rarity not in RARITIES:
            card = (
                database.get_card_by_name(name)
                if isinstance(database, _ArenaPrices)
                else (owned_info.get(name) or database.get_card_by_name(name))
            )
            rarity = str(getattr(card, "rarity", "")).lower() if getattr(card, "arena_id", 0) else ""
        if rarity not in RARITIES:
            # A name-only catalog can shadow a later source's Arena printing.
            # Ask the remaining configured sources before marking it unresolved.
            sources = getattr(database, "sources", ())
            for source in sources if isinstance(sources, (list, tuple)) else ():
                card = source.get_card_by_name(name)
                candidate_rarity = str(getattr(card, "rarity", "")).lower()
                if getattr(card, "arena_id", 0) and candidate_rarity in RARITIES:
                    rarity = candidate_rarity
                    break
        if rarity not in RARITIES:
            unknown.append(name)
        else:
            spend[rarity] += shortfall
    shortfall_wildcards = {
        r: spend[r] - int(wildcards.get(r) or 0) for r in RARITIES if spend[r] > int(wildcards.get(r) or 0)
    }
    return {
        "missing": missing,
        "spend": dict(spend),
        "shortfall_wildcards": shortfall_wildcards,
        "unknown": unknown,
        "owned": bool(required) and not missing,
        "affordable": bool(required)
        and not unknown
        and all(spend[r] <= int(wildcards.get(r) or 0) for r in RARITIES),
    }


def bo1_variant(deck: dict, database=None) -> dict | None:
    """Return a Best-of-1 copy (main deck + optional companion) if a list has extra sideboard cards."""
    if not isinstance(deck, dict) or deck.get("commander"):
        return None
    _main, side, _cmd, companion = _piles(deck, database)
    if not side or side == companion:
        return None
    return {**deck, "sideboard": list(deck.get("companion") or [])}


def split_export(text: str) -> dict:
    """Read Arena section headers in any order, including Commander-first exports."""
    headers = {
        "deck": "mainboard",
        "commander": "commander",
        "companion": "companion",
        "sideboard": "sideboard",
    }
    deck = {key: [] for key in headers.values()}
    pile = "mainboard"
    for raw in text.splitlines():
        line = raw.strip()
        if line.casefold() in headers:
            pile = headers[line.casefold()]
        elif line:
            deck[pile].append(line)
    return deck


def _piles(deck, database=None):
    piles = []
    for section in ("mainboard", "sideboard", "commander", "companion"):
        cards = parse_deck(deck.get(section, []))
        if isinstance(database, _ArenaPrices):
            canonical = Counter()
            for name, quantity in cards.items():
                canonical[database.canonical(name)] += quantity
            cards = canonical
        piles.append(cards)
    return piles


def arena_export(deck: dict, *, format_name: str = "", database=None) -> str:
    main, side, commander, companion = _piles(deck, database)
    if sum(main.values()) < 60 and not (commander and sum(main.values()) == 59):
        raise ValueError("The source did not return a complete constructed deck (at least 60 cards).")
    if companion and sum(companion.values()) != 1:
        raise ValueError("An Arena companion designation must contain one card.")
    if format_name == "brawl" or commander:
        if sum(commander.values()) != 1 or sum(main.values()) not in (
            {99} if format_name == "brawl" else {59, 99}
        ):
            raise ValueError("A Brawl export requires one commander and its complete main deck.")
        if main.keys() & commander.keys():
            raise ValueError("A Brawl commander cannot also appear in the main deck.")
        if side and side != companion:
            raise ValueError("Brawl has no sideboard beyond a designated companion.")
    if format_name == "standard" and commander:
        raise ValueError("A Standard deck cannot have a commander.")
    if companion:
        name = next(iter(companion))
        side[name] = max(side[name], 1)
    if sum(side.values()) > 15:
        raise ValueError("An Arena constructed sideboard may contain at most fifteen cards.")
    piles = (("Commander", commander), ("Companion", companion), ("Deck", main), ("Sideboard", side))
    return "\n\n".join(
        label + "\n" + "\n".join(f"{n} {name}" for name, n in pile.items()) for label, pile in piles if pile
    )


@dataclass(frozen=True, slots=True)
class _DeckPrint:
    arena_id: int
    name: str
    rarity: str


def _name_key(name: str) -> str:
    text = name.casefold().replace("’", "'")
    # Public lists and the native catalog spell face separators and spaced ellipses differently.
    text = re.sub(r"\s+/{2,3}\s+", " // ", text)
    text = re.sub(r"(?<=\S)\s*\.\s+\.\s+\.", "...", text)
    return "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))


class _ArenaPrices:
    """Offline canonical identities and cheapest printed rarities; ambiguous aliases never match."""

    def __init__(self, rows):
        self.by_id = {}
        self.by_name = {}
        self.aliases = {}
        for arena_id, localized, rarity in rows:
            name = html.unescape(re.sub(r"</?nobr>", "", localized, flags=re.I))
            print_ = _DeckPrint(arena_id, name, rarity)
            self.by_id[arena_id] = print_
            previous = self.by_name.get(name)
            if previous is None or RARITIES.index(rarity) < RARITIES.index(previous.rarity):
                self.by_name[name] = print_
            key = _name_key(name)
            if self.aliases.setdefault(key, name) != name:
                self.aliases[key] = None

    def get_card_by_arena_id(self, arena_id):
        return self.by_id.get(arena_id)

    def get_card_by_name(self, name):
        return self.by_name.get(name) or self.by_name.get(self.aliases.get(_name_key(name)))

    def canonical(self, name):
        card = self.get_card_by_name(name)
        return card.name if card else name


def pricing_database():
    """The private collection and any pasted names are resolved entirely on device."""
    from arenamcp.mtgadb import MTGADatabase

    native = MTGADatabase()
    try:
        return _ArenaPrices(native.deck_price_prints())
    finally:
        native.close()


def _build_candidate_row(
    entry: dict,
    deck: dict,
    *,
    rank: int,
    fmt_low: str,
    label: str,
    source_label: str,
    source_site: str,
    collection: dict,
    wildcards: dict,
    database,
    index,
) -> dict | None:
    try:
        exported = arena_export(deck, format_name=fmt_low, database=database)
        bo1 = bo1_variant(deck, database=database)
        bo1_export = arena_export(bo1, format_name=fmt_low, database=database) if bo1 is not None else None
    except (ValueError, OSError, KeyError) as exc:
        logger.warning(
            "Skipping %s source %r (%s): %s",
            label,
            entry.get("name"),
            entry.get("url"),
            exc,
        )
        return None
    cost = compare_collection(deck, collection, wildcards, database, index=index)
    row = {
        **entry,
        **deck,
        "source_rank": rank,
        "format": label,
        "source": source_label,
        "source_site": source_site,
        "checked_at": deck.get("checked_at") or "unavailable",
        "export": exported,
        "cost": cost,
    }
    if bo1 is not None and bo1_export is not None:
        row["bo1_export"] = bo1_export
        row["bo1_cost"] = compare_collection(bo1, collection, wildcards, database, index=index)
    return row


def _fetch_mtggoldfish_entries(client, fmt_low: str) -> list[tuple[dict, str]]:
    entries = list(client.get_metagame(fmt_low))
    if fmt_low in ("standard", "brawl"):
        seen_urls = {str(e.get("url") or "") for e in entries}
        try:
            for extra in client.get_metagame(f"{fmt_low}/full"):
                url = str(extra.get("url") or "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    entries.append(extra)
        except Exception as exc:
            logger.debug("Full %s metagame unavailable: %s", fmt_low, exc)
    arena_meta_label = "paper metagame" if fmt_low == "standard" else "Arena metagame"
    label = FORMATS.get(fmt_low, fmt_low.title())
    return [
        (
            entry,
            f"MTGGoldfish {label} ({'budget list' if entry.get('budget') else arena_meta_label})",
        )
        for entry in entries
    ]


def load_candidates(
    collection: dict,
    wildcards: dict,
    formats: tuple[str, ...] = ("standard", "brawl"),
    *,
    sources: tuple[str, ...] = ("mtggoldfish",),
) -> list[dict]:
    """Load public metagame deck candidates across the requested formats and sources.

    Supported ``sources``: ``"mtggoldfish"``, ``"aetherhub"``, ``"mtgtop8"``,
    and ``"moxfield"``. Defaults to ``("mtggoldfish",)`` for backward
    compatibility; pass ``ALL_SOURCES`` to aggregate all four. Ordered cheapest
    to craft first; no invented win-rate statistics.
    """
    from concurrent.futures import ThreadPoolExecutor

    from arenamcp.meta_sources import (
        AetherHubClient,
        ArchidektClient,
        MoxfieldClient,
        MTGAZoneClient,
        MTGTop8Client,
    )
    from arenamcp.mtggoldfish import MTGGoldfishClient

    database = pricing_database()
    index = collection_index(collection, database)
    active_sources = tuple(s.lower() for s in sources)
    candidates: list[dict] = []

    goldfish = MTGGoldfishClient() if "mtggoldfish" in active_sources else None
    aetherhub = AetherHubClient() if "aetherhub" in active_sources else None
    mtgtop8 = MTGTop8Client() if "mtgtop8" in active_sources else None
    moxfield = MoxfieldClient() if "moxfield" in active_sources else None
    archidekt = ArchidektClient() if "archidekt" in active_sources else None
    mtgazone = MTGAZoneClient() if "mtgazone" in active_sources else None

    # Gather work items: (rank, fmt_low, label, source_site, source_label, entry, fetch_fn)
    work: list[tuple[int, str, str, str, str, dict, object]] = []
    for fmt in formats:
        fmt_low = fmt.lower()
        label = FORMATS.get(fmt_low, fmt.title())

        if goldfish is not None:
            try:
                for rank, (entry, src_label) in enumerate(_fetch_mtggoldfish_entries(goldfish, fmt_low), 1):
                    work.append(
                        (
                            rank,
                            fmt_low,
                            label,
                            "MTGGoldfish",
                            src_label,
                            entry,
                            lambda e=entry, c=goldfish: c.get_deck_list(e["url"]),
                        )
                    )
            except Exception as exc:
                logger.warning("MTGGoldfish %s metagame failed: %s", fmt_low, exc)

        if aetherhub is not None:
            try:
                is_brawl = fmt_low == "brawl"
                src_label = f"AetherHub {label} (Arena tracker)"
                for rank, entry in enumerate(aetherhub.get_metagame(fmt_low), 1):
                    work.append(
                        (
                            rank,
                            fmt_low,
                            label,
                            "AetherHub",
                            src_label,
                            entry,
                            lambda e=entry, c=aetherhub, b=is_brawl: c.get_deck_list(e["url"], is_brawl=b),
                        )
                    )
            except Exception as exc:
                logger.warning("AetherHub %s metagame failed: %s", fmt_low, exc)

        if mtgtop8 is not None:
            try:
                src_label = f"MTGTop8 {label} (tournament)"
                for rank, entry in enumerate(mtgtop8.get_metagame(fmt_low), 1):
                    work.append(
                        (
                            rank,
                            fmt_low,
                            label,
                            "MTGTop8",
                            src_label,
                            entry,
                            lambda e=entry, c=mtgtop8: c.get_deck_list(e["url"]),
                        )
                    )
            except Exception as exc:
                logger.warning("MTGTop8 %s metagame failed: %s", fmt_low, exc)

        if moxfield is not None:
            try:
                src_label = f"Moxfield {label} (community)"
                for rank, entry in enumerate(moxfield.get_metagame(fmt_low), 1):
                    work.append(
                        (
                            rank,
                            fmt_low,
                            label,
                            "Moxfield",
                            src_label,
                            entry,
                            lambda e=entry, c=moxfield: c.get_deck_list(e.get("public_id") or e["url"]),
                        )
                    )
            except Exception as exc:
                logger.warning("Moxfield %s search failed: %s", fmt_low, exc)

        if archidekt is not None:
            try:
                is_brawl = fmt_low == "brawl"
                src_label = f"Archidekt {label} (community)"
                for rank, entry in enumerate(archidekt.get_metagame(fmt_low), 1):
                    work.append(
                        (
                            rank,
                            fmt_low,
                            label,
                            "Archidekt",
                            src_label,
                            entry,
                            lambda e=entry, c=archidekt, b=is_brawl: c.get_deck_list(
                                e.get("deck_id") or e["url"], is_brawl=b
                            ),
                        )
                    )
            except Exception as exc:
                logger.warning("Archidekt %s search failed: %s", fmt_low, exc)

        if mtgazone is not None:
            try:
                src_label = f"MTG Arena Zone {label} (tier list)"
                for rank, entry in enumerate(mtgazone.get_metagame(fmt_low), 1):
                    work.append(
                        (
                            rank,
                            fmt_low,
                            label,
                            "MTG Arena Zone",
                            src_label,
                            entry,
                            lambda e=entry, c=mtgazone: c.get_deck_list(e.get("cache_key") or e["url"]),
                        )
                    )
            except Exception as exc:
                logger.warning("MTGAZone %s tier list failed: %s", fmt_low, exc)

    def _run_item(item):
        rank, fmt_low, label, source_site, src_label, entry, fetch_fn = item
        try:
            deck = fetch_fn()
        except Exception as exc:
            logger.warning(
                "Skipping %s source %r (%s): %s",
                label,
                entry.get("name"),
                entry.get("url"),
                exc,
            )
            return None
        return _build_candidate_row(
            entry,
            deck,
            rank=rank,
            fmt_low=fmt_low,
            label=label,
            source_label=src_label,
            source_site=source_site,
            collection=collection,
            wildcards=wildcards,
            database=database,
            index=index,
        )

    # Deduplicate work items by URL before dispatching to the thread pool so
    # two workers can never read/write the same per-deck cache file at once.
    deduped_work = []
    seen_work_urls: set[str] = set()
    for item in work:
        entry_url = str(item[5].get("url") or "")
        if entry_url and entry_url in seen_work_urls:
            continue
        if entry_url:
            seen_work_urls.add(entry_url)
        deduped_work.append(item)

    if len(active_sources) == 1 and active_sources[0] == "mtggoldfish":
        # Preserve sequential execution for single-source / unit-test callers.
        rows = [_run_item(item) for item in deduped_work]
    else:
        with ThreadPoolExecutor(max_workers=6) as pool:
            rows = list(pool.map(_run_item, deduped_work))

    for row in rows:
        if row is not None:
            candidates.append(row)

    candidates.sort(key=cost_order_key)
    return candidates


# The same sample-size floor as the per-deck autopilot results view: a win
# rate below twenty decided games is noise, not signal, and is never shown.
ARENA_MIN_GAMES = 20

FORMATS = {
    "standard": "Standard",
    "brawl": "Brawl",
    "historic": "Historic",
    "alchemy": "Alchemy",
    "timeless": "Timeless",
    "explorer": "Explorer",
}
ALL_SOURCES = ("aetherhub", "mtggoldfish", "mtgtop8", "moxfield", "archidekt", "mtgazone")


def normalize_deck_name(name: str) -> str:
    """Fold punctuation and case so 'Mono-Blue Flash' matches 'Mono Blue Flash'."""
    return " ".join(re.findall(r"[^\W_]+", _name_key(name or "")))


def _entry_format_labels(entry_formats) -> set[str]:
    from arenamcp.desktop.performance_tab import _canonical_queue

    return {FORMATS.get(_canonical_queue(queue), "unsupported") for queue in entry_formats or ()}


def cost_order_key(candidate: dict) -> tuple:
    """Cheapest to craft first; decks with unresolvable cards sort last."""
    cost = candidate.get("cost") or {}
    unknown = 1 if cost.get("unknown") else 0
    spend = cost.get("spend") or {}
    return (unknown, sum(spend.values()), candidate.get("source_rank", 999))


def rank_candidates(candidates: list[dict], results: list[dict] | None) -> list[dict]:
    """Attach the fleet's own autopilot results to matching candidates.

    ``results`` holds per-deck aggregates as published by the per-deck
    results view (name, games, wins, losses, win_rate, formats). Only decks
    with at least ARENA_MIN_GAMES decided games and a measured rate carry an
    ``arena`` entry — a rate is never invented for a deck without data.

    A rate attaches only when the archetype AND format identity are
    unambiguous: the recorded bucket must have been played in exactly the
    candidate's format (a Standard archetype never borrows Brawl games or
    vice versa) and exactly one measured bucket may relate to the candidate
    by exact normalized name. Anything ambiguous — several same-format buckets with related
    names, or a bucket whose queue names span more than one format — gets no
    rate rather than a possibly wrong one.
    """
    measured = [
        e
        for e in (results or [])
        if type(e.get("games")) is int and e["games"] >= ARENA_MIN_GAMES and e.get("win_rate") is not None
    ]
    ranked = []
    for candidate in candidates:
        candidate = {k: v for k, v in candidate.items() if k not in {"arena", "arena_sample"}}
        name = normalize_deck_name(candidate.get("name", ""))
        matches = [
            e
            for e in measured
            if name
            and name == normalize_deck_name(e.get("name", ""))
            and _entry_format_labels(e.get("formats")) == {candidate.get("format")}
        ]
        fleet = [e for e in matches if e.get("sample_source") == "fleet"]
        if fleet:
            matches = fleet  # The shared measurement can overlap this machine's games.
        if len(matches) == 1:
            match = matches[0]
            wins, losses, rate = match.get("wins"), match.get("losses"), match["win_rate"]
            if (
                type(wins) is int
                and type(losses) is int
                and min(wins, losses) >= 0
                and wins + losses == match["games"]
                and type(rate) in (int, float)
                and 0 <= rate <= 1
                and math.isclose(rate, wins / match["games"], abs_tol=0.00005)
            ):
                candidate.update(
                    arena={"games": match["games"], "win_rate": rate},
                    arena_sample=match.get("sample_source", ""),
                )
        ranked.append(candidate)
    measured_ranked = [c for c in ranked if c.get("arena")]
    measured_ranked.sort(key=lambda c: (-c["arena"]["win_rate"], cost_order_key(c)))
    return measured_ranked + [c for c in ranked if not c.get("arena")]


def local_autopilot_results() -> list[dict]:
    """Per-deck autopilot results from this machine's recorded games.

    Integrates with the per-deck results work (P1): the records are read the
    same way the Match History tab reads them — the full history, oldest
    first — and aggregated by P1's ``autopilot_deck_results``, so the rates
    shown here are exactly the rates the tab publishes for the same decks.
    Failures are loud: a broken history or a missing aggregator raises, it
    never silently degrades to "no data".
    """
    from arenamcp.desktop.performance_tab import autopilot_deck_results
    from arenamcp.match_history import MatchHistory

    history = MatchHistory()
    records = list(reversed(history.get_recent(history.total_games)))
    return autopilot_deck_results(records)


_FLEET_REVIEW_MODULE = None


def _load_fleet_review():
    """Import W1's fleet review tool from this checkout's tools/ directory.

    The fleet's own win-rate table lives in the shared match-bundle corpus
    that W1 mines; loading it lazily keeps this module independent of the
    tool until the fleet path is actually used. A missing tool is a broken
    prerequisite and raises — it never silently degrades to "no data".
    """
    global _FLEET_REVIEW_MODULE
    if _FLEET_REVIEW_MODULE is not None:
        return _FLEET_REVIEW_MODULE
    import importlib.util

    if importlib.util.find_spec("arenamcp.fleet_review") is not None:
        from arenamcp import fleet_review

        _FLEET_REVIEW_MODULE = fleet_review
        return fleet_review

    root = pathlib.Path(__file__).resolve().parents[2]
    tool = root / "tools" / "fleet_review.py"
    if not tool.is_file():
        raise FileNotFoundError(f"W1 fleet review tool not found: {tool}")
    spec = importlib.util.spec_from_file_location("arenamcp_fleet_review", tool)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load W1 fleet review tool: {tool}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _FLEET_REVIEW_MODULE = module
    return module


def fleet_autopilot_results(bundles_dir: str | None = None) -> list[dict]:
    """Per-archetype autopilot results from the fleet's shared match bundles.

    The actual fleet source is W1's ``tools/fleet_review.py`` mining the
    explicitly configured shared corpus of uploaded match bundles,
    folded per match like M1 so a multi-game match counts once. Each row
    keeps its recorded Arena queue name so consumers can require an
    unambiguous format match before attaching a rate, and the twenty-game
    floor applies to decided matches exactly as it does for local results.
    Failures are loud: a missing tool or corpus directory raises.
    """
    from arenamcp.desktop.performance_tab import MIN_GAMES_FOR_WIN_RATE

    directory = bundles_dir or os.environ.get("MTGACOACH_FLEET_BUNDLE_DIR")
    if not directory:
        raise FileNotFoundError("no shared fleet corpus configured")
    fleet_review = _load_fleet_review()
    if not os.path.isdir(directory):
        raise FileNotFoundError(f"fleet match-bundle corpus not found: {directory}")
    # The fleet tool's path order and final game determine the match result.
    # Fold before filtering. A manual/undecided final game cannot turn a prior
    # autopilot game into a measured match win.
    matches = {}
    for path in fleet_review.iter_bundle_paths(directory):
        bundle = fleet_review.load_bundle(path)
        if not fleet_review.is_match_bundle(bundle) or not bundle.get("match_id"):
            continue
        final = bundle.get("final_state") or {}
        matches[bundle["match_id"]] = (
            (bundle.get("settings_subset") or {}).get("autopilot_enabled") is True,
            final.get("format_name"),
            fleet_review.archetype_label(bundle),
            bundle.get("result"),  # The last game alone cannot settle a multi-game match.
        )
    buckets = {}
    for auto, queue, name, outcome in matches.values():
        if auto and name and name != fleet_review.UNKNOWN:
            counts = buckets.setdefault((queue or "", name), Counter())
            if outcome in ("win", "loss"):
                counts[outcome] += 1
    entries = []
    for (queue, name), counts in buckets.items():
        games = counts["win"] + counts["loss"]
        entries.append(
            {
                "name": name,
                "games": games,
                "wins": counts["win"],
                "losses": counts["loss"],
                "formats": [queue] if queue else [],
                "win_rate": counts["win"] / games if games >= MIN_GAMES_FOR_WIN_RATE else None,
            }
        )
    entries.sort(key=lambda e: (-e["games"], e["name"]))
    return entries
