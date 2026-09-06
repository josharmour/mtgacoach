"""Match Gating & Opponent Hand Imputation for MageZero RL evaluations.

Ensures the neural network is only queried for decks matching the trained
archetype (UWTempo v2), and eliminates 'hellbent opponent' bias by sampling
probable opponent hands from gauntlet card pools (matching XMage's shuffleUnknowns).
"""

from __future__ import annotations

import json
import logging
import random
from collections import Counter
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Canonical UWTempo training decklist (60 cards, 17 distinct)
UWTEMPO_DECK_COUNTS: dict[str, int] = {
    "Malcolm, Alluring Scoundrel": 4,
    "Island": 7,
    "Sheltered by Ghosts": 4,
    "Skrelv, Defector Mite": 4,
    "Combat Research": 4,
    "No More Lies": 4,
    "Adarkar Wastes": 4,
    "Seachrome Coast": 4,
    "Meticulous Archive": 4,
    "Shardmage's Rescue": 2,
    "Floodfarm Verge": 3,
    "Soul Partition": 2,
    "Negate": 2,
    "Kitsa, Otterball Elite": 4,
    "Bounce Off": 4,
    "Spell Pierce": 2,
    "Sleep-Cursed Faerie": 2,
}

_GAUNTLET_POOLS_PATH = Path(__file__).parent / "data" / "gauntlet_card_pools.json"


def _load_gauntlet_pools() -> dict[str, list[str]]:
    if _GAUNTLET_POOLS_PATH.is_file():
        try:
            with open(_GAUNTLET_POOLS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.debug("Failed loading gauntlet pools: %s", e)
    return {}


_GAUNTLET_POOLS = _load_gauntlet_pools()


def compute_count_weighted_jaccard(
    deck_a: Counter[str] | dict[str, int],
    deck_b: Counter[str] | dict[str, int],
) -> float:
    """Calculate count-weighted Jaccard similarity between two deck distributions.

    sim = sum(min(count_a, count_b)) / sum(max(count_a, count_b))
    """
    all_keys = set(deck_a.keys()) | set(deck_b.keys())
    if not all_keys:
        return 0.0

    intersect_sum = 0
    union_sum = 0
    for k in all_keys:
        ca = deck_a.get(k, 0)
        cb = deck_b.get(k, 0)
        intersect_sum += min(ca, cb)
        union_sum += max(ca, cb)

    return float(intersect_sum) / float(union_sum) if union_sum > 0 else 0.0


def extract_hero_deck_cards(game_state: dict[str, Any]) -> list[str]:
    """Extract known hero card names from game state snapshot."""
    cards: list[str] = []
    if not isinstance(game_state, dict):
        return cards

    # 1. Check if full deck_cards / deck_list was captured at match start
    raw_deck = (
        game_state.get("deck_cards")
        or game_state.get("deck_list")
        or game_state.get("deck")
    )
    if isinstance(raw_deck, list) and raw_deck:
        for item in raw_deck:
            if isinstance(item, str) and item.strip():
                cards.append(item.strip())
            elif isinstance(item, dict) and item.get("name"):
                cards.append(str(item["name"]).strip())
            elif isinstance(item, int) and item > 0:
                # Resolve GRP ID to name via mtgadb if available
                try:
                    from arenamcp.mtgadb import MtgaDB

                    resolved = MtgaDB.get_card_by_grp_id(item)
                    if resolved and resolved.get("name"):
                        cards.append(resolved["name"])
                except Exception:
                    pass

        if len(cards) >= 30:
            return cards

    local_seat = game_state.get("local_seat_id")
    if local_seat is None:
        for p in game_state.get("players", []):
            if isinstance(p, dict) and p.get("is_local"):
                local_seat = p.get("seat_id")
                break
    if local_seat is None:
        local_seat = 1

    # Extract card names across hero zones
    for zone_name in ("hand", "battlefield", "graveyard", "exile", "command", "library"):
        for c in game_state.get(zone_name) or []:
            if isinstance(c, dict):
                ctrl = c.get("controller_seat_id") or c.get("owner_seat_id")
                if ctrl is None or ctrl == local_seat:
                    name = str(c.get("name") or "").strip()
                    if name:
                        cards.append(name)

    return cards


def is_hero_deck_gated(
    game_state: dict[str, Any],
    threshold: float = 0.60,
) -> tuple[bool, float, str]:
    """Check if hero deck matches UWTempo sufficiently to activate neural evaluations.

    Returns:
        (is_active, similarity_score, eval_source_label)
    """
    # 1. Format gating: only 60-card Constructed can pass the Standard UWTempo gate
    from arenamcp.format_profile import detect_format_profile

    raw_fmt = game_state.get("format_profile") or game_state.get("format")
    if isinstance(raw_fmt, dict):
        family = raw_fmt.get("family", "")
        deck_size = raw_fmt.get("deck_size", 0)
    elif hasattr(raw_fmt, "family"):
        family = getattr(raw_fmt, "family", "")
        deck_size = getattr(raw_fmt, "deck_size", 0)
    else:
        profile = detect_format_profile(game_state)
        family = profile.family
        deck_size = profile.deck_size

    if family in ("brawl", "limited") or deck_size in (40, 99, 100):
        return False, 0.0, "Tactical Heuristic Lookahead"

    hero_cards = extract_hero_deck_cards(game_state)
    if not hero_cards:
        return False, 0.0, "Tactical Heuristic Lookahead"

    hero_counter = Counter(hero_cards)

    # When full deck is available (>= 40 cards), compute standard count-weighted Jaccard
    if len(hero_cards) >= 40:
        sim = compute_count_weighted_jaccard(hero_counter, UWTEMPO_DECK_COUNTS)
        if sim >= threshold:
            return True, sim, "MageZero UWTempo v2"
        return False, sim, "Tactical Heuristic Lookahead"

    # When only in-match revealed cards are available (< 40 cards),
    # compute precision of observed cards against UWTempo (at least 3 cards seen)
    matching_cards = sum(
        min(count, UWTEMPO_DECK_COUNTS.get(c, 0)) for c, count in hero_counter.items()
    )
    precision = matching_cards / len(hero_cards)
    distinct_seen = len([c for c in hero_counter if c in UWTEMPO_DECK_COUNTS])

    if precision >= 0.75 and distinct_seen >= 2:
        return True, round(precision, 2), "MageZero UWTempo v2"

    return False, round(precision, 2), "Tactical Heuristic Lookahead"


def sample_opponent_hands(
    game_state: dict[str, Any],
    num_samples: int = 8,
    seed: int | None = None,
) -> list[list[str]]:
    """Sample candidate opponent hands from gauntlet card pools to eliminate hellbent bias.

    Matches the determinization performed by XMage's ComputerPlayerMCTS.shuffleUnknowns().
    """
    if seed is not None:
        random.seed(seed)

    # Determine opponent hand size
    local_seat = game_state.get("local_seat_id")
    if local_seat is None:
        local_seat = 1

    opp_hand_count = 0
    for p in game_state.get("players", []):
        if isinstance(p, dict) and not p.get("is_local") and p.get("seat_id") != local_seat:
            opp_hand_count = int(p.get("cards_in_hand") or p.get("hand_count") or 0)
            break

    if opp_hand_count <= 0:
        return [[] for _ in range(num_samples)]

    # Determine archetype card pool
    from arenamcp.opponent_model import OpponentModel

    opp_profile = OpponentModel.classify(game_state)
    archetype = opp_profile.archetype.lower()

    # Match archetype to gauntlet deck pool
    pool: list[str] = []
    if "mono-red" in archetype:
        pool = _GAUNTLET_POOLS.get("Standard-MonoR", [])
    elif "mono-blue" in archetype:
        pool = _GAUNTLET_POOLS.get("Standard-MonoU", [])
    elif "mono-black" in archetype:
        pool = _GAUNTLET_POOLS.get("Standard-MonoB", [])
    elif "mono-white" in archetype:
        pool = _GAUNTLET_POOLS.get("Standard-MonoW", [])
    elif "mono-green" in archetype:
        pool = _GAUNTLET_POOLS.get("Standard-MonoG", [])
    elif "uw" in archetype or "azorius" in archetype:
        pool = _GAUNTLET_POOLS.get("UW Control", []) or list(UWTEMPO_DECK_COUNTS.keys())

    if not pool:
        # Fallback: aggregate all gauntlet pools
        combined: list[str] = []
        for p_list in _GAUNTLET_POOLS.values():
            combined.extend(p_list)
        pool = combined or list(UWTEMPO_DECK_COUNTS.keys())

    # Subtract revealed cards from available pool
    pool_counter = Counter(pool)
    for c in opp_profile.revealed_cards:
        if pool_counter[c] > 0:
            pool_counter[c] -= 1

    remaining_pool = list(pool_counter.elements())
    if len(remaining_pool) < opp_hand_count:
        remaining_pool = pool

    samples: list[list[str]] = []
    for _ in range(num_samples):
        if len(remaining_pool) >= opp_hand_count:
            sample = random.sample(remaining_pool, opp_hand_count)
        else:
            sample = random.choices(remaining_pool, k=opp_hand_count)
        samples.append(sample)

    return samples
