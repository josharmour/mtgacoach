"""Collection-aware comparison of sourced decklists, without spending wildcards."""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone

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
        elif line.strip() and line.strip().lower() not in ("deck", "sideboard"):
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

    Main and sideboard need separate copies. Basic lands require no wildcards.
    Prices are estimates; Arena verifies legal cards and costs during import.
    """
    required = parse_deck(deck.get("mainboard", [])) + parse_deck(deck.get("sideboard", []))
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
            card = owned_info.get(name) or database.get_card_by_name(name)
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
    return {
        "missing": missing,
        "spend": dict(spend),
        "unknown": unknown,
        "owned": bool(required) and not missing,
        "affordable": bool(required)
        and not unknown
        and all(spend[r] <= int(wildcards.get(r, 0)) for r in RARITIES),
    }


def arena_export(deck: dict) -> str:
    main = parse_deck(deck.get("mainboard", []))
    side = parse_deck(deck.get("sideboard", []))
    if sum(main.values()) < 60:
        raise ValueError(
            "The source did not return a complete constructed deck (at least 60 main-deck cards)."
        )
    text = "Deck\n" + "\n".join(f"{count} {name}" for name, count in main.items())
    if side:
        text += "\n\nSideboard\n" + "\n".join(f"{count} {name}" for name, count in side.items())
    return text


def load_candidates(collection: dict, wildcards: dict) -> list[dict]:
    """Existing public Standard source; no invented win-rate statistics."""
    from arenamcp.card_db import get_card_database
    from arenamcp.mtggoldfish import MTGGoldfishClient

    client = MTGGoldfishClient()
    database = get_card_database()
    index = collection_index(collection, database)
    candidates = []
    for rank, entry in enumerate(client.get_metagame("standard")[:6], 1):
        deck = client.get_deck_list(entry["url"])
        try:
            exported = arena_export(deck)
        except ValueError:
            continue
        candidates.append(
            {
                **entry,
                **deck,
                "source_rank": rank,
                "source": "MTGGoldfish Standard (paper metagame)",
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "export": exported,
                "cost": compare_collection(deck, collection, wildcards, database, index=index),
            }
        )
    return candidates
