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
from dataclasses import asdict as _dc_asdict, dataclass, field as _dc_field
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


@dataclass
class OpponentHandSamples:
    """Sampled opponent-hand determinizations plus uncertainty metadata.

    ``samples[i]`` is a determinization of the opponent's hidden hand for the
    i-th evaluation row. Samples are HYPOTHESES drawn from an archetype-matched
    card pool — never facts about the opponent's actual hand.

    Attributes:
        samples: One hand (list of card names) per requested sample.
        num_samples: Requested number of samples actually returned.
        hand_count_known: True only when the producer reported an actual count.
            When False, ``samples`` are the fallback heuristic envelope and MUST
            NOT be presented as cards the opponent is known to hold.
        hand_count: The known count (including 0), or None when unknown.
        hand_count_tier: Producer fallback tier that resolved the count
            ('top_level'|'zones'|'player'|'unknown').
        revealed_cards: Opponent card identities already observed (distinct
            names, as reported by OpponentModel). Revealed cards are excluded
            from the sampling pool — one pool copy per revealed name — and
            never re-added (see ``revealed_multiplicity_respected``).
        revealed_multiplicity_respected: True when every revealed card was
            subtracted from the pool without exceeding available copies.
        pool_size: Total card copies available in the sampling pool AFTER
            subtracting revealed cards.
        pool_coverage_ratio: pool_size / max(hand_count, 1); values < 1.0 mean
            the pool cannot fully populate a real hand, so samples may repeat
            cards and are weaker evidence (see ``undersized_pool``).
        undersized_pool: True when the revealed-corrected pool holds fewer
            copies than the known hand count; deterministic sub-sampling of the
            largest-fitting prefix is used instead of fabricating with
            replacement from an exhausted pool.
        notes: Human-readable caveats for provenance / LLM surface.
    """

    samples: list[list[str]] = _dc_field(default_factory=list)
    num_samples: int = 0
    hand_count_known: bool = False
    hand_count: int | None = None
    hand_count_tier: str = "unknown"
    revealed_cards: list[str] = _dc_field(default_factory=list)
    revealed_multiplicity_respected: bool = True
    revealed_subtracted: list[str] = _dc_field(default_factory=list)
    pool_size: int = 0
    pool_coverage_ratio: float = 0.0
    undersized_pool: bool = False
    notes: list[str] = _dc_field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _dc_asdict(self)


def _resolve_opponent_hand_count_and_tier(
    game_state: dict[str, Any],
) -> tuple[int | None, str]:
    """Resolve the opponent's hand count with known-zero preserved.

    Mirrors the producer schema: top-level ``opponent_hand_count`` ->
    ``zones.opponent_hand_count`` -> the non-local player row, where a
    present-but-None ``hand_count`` falls through to ``cards_in_hand`` and
    both ``hand_size`` and ``hand_count`` are accepted aliases. A real 0 is a
    KNOWN count (hellbent), distinct from missing/unknown (which returns
    ``(None, 'unknown')``).

    NOTE: keep semantics aligned with
    ``MCTSEvaluator._resolve_opponent_hand_count``; this Dallas copy exists so
    gating does not import the evaluator, and the two are locked together by
    tests/test_opponent_hand_uncertainty.py.
    """
    top = game_state.get("opponent_hand_count")
    if top is not None:
        return _coerce_hand_count(top), "top_level"
    zones = game_state.get("zones") or {}
    if isinstance(zones, dict):
        zc = zones.get("opponent_hand_count")
        if zc is not None:
            return _coerce_hand_count(zc), "zones"
    local_seat = game_state.get("local_seat_id")
    if local_seat is None:
        for p in game_state.get("players") or []:
            if isinstance(p, dict) and p.get("is_local"):
                local_seat = p.get("seat_id")
                break
    for p in game_state.get("players") or []:
        if not isinstance(p, dict):
            continue
        if p.get("is_local") or (local_seat is not None and p.get("seat_id") == local_seat):
            continue
        for key in ("hand_count", "hand_size", "cards_in_hand"):
            val = p.get(key)
            if val is not None:
                return _coerce_hand_count(val), "player"
    return None, "unknown"


def _coerce_hand_count(value: Any) -> int | None:
    """Coerce a producer hand-count field to a non-negative int, else None."""
    if isinstance(value, bool):
        return None
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, count)


def sample_opponent_hands_meta(
    game_state: dict[str, Any],
    num_samples: int = 8,
    seed: int | None = None,
) -> OpponentHandSamples:
    """Sample candidate opponent hands WITHOUT claiming to know hidden cards.

    Determinization matches XMage's ComputerPlayerMCTS.shuffleUnknowns(): when
    the opponent's hand count is known, draw that many cards from an
    archetype-matched, revealed-corrected pool using a LOCAL RNG (global RNG
    state is never touched). Known zero produces empty hands. Missing data is
    reported unknown — no fabricated count, no fabricated determinization.
    """
    rng = random.Random(seed)
    meta = OpponentHandSamples(num_samples=max(0, num_samples))

    hand_count, tier = _resolve_opponent_hand_count_and_tier(game_state)
    meta.hand_count = hand_count
    meta.hand_count_tier = tier
    meta.hand_count_known = hand_count is not None

    from arenamcp.opponent_model import OpponentModel

    opp_profile = OpponentModel.classify(game_state)
    meta.revealed_cards = list(opp_profile.revealed_cards)

    if hand_count is None:
        meta.notes.append(
            "opponent hand size unknown; no hand determinization performed "
            "(samples are empty placeholders, not facts)"
        )
        meta.samples = [[] for _ in range(meta.num_samples)]
        return meta

    if hand_count == 0:
        meta.notes.append("opponent known hellbent (0 cards in hand); empty hands")
        meta.samples = [[] for _ in range(meta.num_samples)]
        return meta

    # Determine archetype card pool
    archetype = opp_profile.archetype.lower()
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

    # Subtract revealed cards with multiplicity: a revealed card consumes one
    # pool copy and never returns once its copies are exhausted. Matching is
    # case-insensitive (OpponentModel reports lowercase names, pools are
    # mixed-case); the earlier exact-match code silently subtracted nothing.
    pool_counter: Counter[str] = Counter(pool)
    pool_by_lower: dict[str, str] = {}
    for name in pool_counter:
        pool_by_lower.setdefault(str(name).lower(), str(name))
    exhausted: list[str] = []
    for c in opp_profile.revealed_cards:
        canonical = pool_by_lower.get(str(c).lower())
        if canonical is not None and pool_counter.get(canonical, 0) > 0:
            pool_counter[canonical] -= 1
            meta.revealed_subtracted.append(canonical)
        else:
            exhausted.append(c)
    meta.revealed_multiplicity_respected = not exhausted
    if exhausted:
        meta.notes.append(
            "revealed cards beyond archetype-pool copies were not re-added"
        )

    remaining_pool = list(pool_counter.elements())
    meta.pool_size = len(remaining_pool)
    meta.pool_coverage_ratio = round(len(remaining_pool) / hand_count, 4)

    if len(remaining_pool) < hand_count:
        # Undersized pool: draw the largest-fitting deterministic subset rather
        # than re-using exhausted cards (random.choices would do exactly that).
        # Deterministic order: most-common-first, name as tie-break.
        meta.undersized_pool = True
        meta.notes.append(
            "revealed-corrected pool smaller than hand count; samples are "
            "partial-hypotheses, not full determinizations"
        )
        base = sorted(remaining_pool, key=lambda s: (pool_counter.get(s, 0), s), reverse=True)
        sample = base[:hand_count]
        meta.samples = [list(sample) for _ in range(meta.num_samples)]
        return meta

    meta.samples = [rng.sample(remaining_pool, hand_count) for _ in range(meta.num_samples)]
    return meta


def sample_opponent_hands(
    game_state: dict[str, Any],
    num_samples: int = 8,
    seed: int | None = None,
) -> list[list[str]]:
    """Sample opponent-hand determinizations (backward-compatible wrapper).

    Callers that need the uncertainty metadata should call
    :func:`sample_opponent_hands_meta` instead; this returns just the sample
    hands list in the original shape.
    """
    return sample_opponent_hands_meta(game_state, num_samples=num_samples, seed=seed).samples
