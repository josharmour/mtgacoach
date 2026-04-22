"""Draft card evaluation logic shared between MCP server and standalone.

This module contains the composite scoring logic for draft picks, combining:
- 17lands GIH win rate data
- Card type/mechanic value scoring
- Per-color-pair dynamic scoring (inspired by untapped.gg Draftsmith)
- Weighted color commitment tracking
- Synergy detection with picked cards
- Tier classification (WEAK/BRONZE/SILVER/GOLD/FIRE)
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from arenamcp.scryfall import ScryfallCache, ScryfallCard
from arenamcp.draftstats import DraftStatsCache, ColorPairStats
from arenamcp.mtgadb import MTGADatabase

logger = logging.getLogger(__name__)


# Tier thresholds (scores are on 0-~100 scale; these correspond to the 0-55
# UGG scale used by Draftsmith after our linear rescale).
# 17lands WR range is ~48-62% in limited, so we map score to tiers as follows:
TIER_THRESHOLDS = [
    (0, "WEAK"),
    (45, "BRONZE"),
    (55, "SILVER"),
    (70, "GOLD"),
    (85, "FIRE"),
]

# 10 viable two-color pairs (WUBRG order)
TWO_COLOR_PAIRS = [
    "WU", "WB", "WR", "WG",
    "UB", "UR", "UG",
    "BR", "BG",
    "RG",
]


def get_tier(score: float) -> str:
    """Map a score to a Draftsmith-style tier label."""
    tier = "WEAK"
    for threshold, label in TIER_THRESHOLDS:
        if score >= threshold:
            tier = label
    return tier


@dataclass
class CardEvaluation:
    """Evaluation result for a draft pick candidate."""
    grp_id: int
    name: str
    score: float
    gih_wr: Optional[float]
    reason: str
    all_reasons: list[str]
    tier: str = "WEAK"
    # Per-color-pair score for "what if I committed to pair X?" analysis.
    # Keys are normalized WUBRG pair strings like "WU", "BG".
    per_pair_scores: dict[str, float] = field(default_factory=dict)
    best_pair: Optional[str] = None


def get_deck_colors(
    picked_cards: list[int],
    scryfall: ScryfallCache
) -> set[str]:
    """Determine deck colors from picked cards.

    Args:
        picked_cards: List of grp_ids already picked
        scryfall: Scryfall cache for card lookups

    Returns:
        Set of color letters (W, U, B, R, G) in the deck
    """
    colors = set()
    for grp_id in picked_cards:
        card = scryfall.get_card_by_arena_id(grp_id)
        if card and card.colors:
            colors.update(card.colors)
    return colors


def compute_color_commitment(
    picked_cards: list[int],
    scryfall: ScryfallCache,
    draft_stats: Optional[DraftStatsCache] = None,
    set_code: str = "",
) -> dict[str, float]:
    """Compute a weighted signal for each color indicating commitment strength.

    Each picked card contributes its GIH WR (or a default) to its colors,
    weighted by the card's strength (stronger picks = stronger signals).
    Unlike `get_deck_colors`, this tracks *how much* each color has been
    committed to, not just whether any card of that color was picked.

    Returns:
        Dict mapping color letter (W, U, B, R, G) to a float weight.
        Higher = more invested in that color.
    """
    signal: dict[str, float] = {c: 0.0 for c in "WUBRG"}
    if not picked_cards:
        return signal

    for grp_id in picked_cards:
        card = scryfall.get_card_by_arena_id(grp_id)
        if not card or not card.colors:
            continue
        # Weight by card's win rate if we can get it, else 0.55 as default
        weight = 0.55
        if draft_stats and set_code:
            stats = draft_stats.get_draft_rating(card.name, set_code)
            if stats and stats.gih_wr:
                weight = stats.gih_wr
        # A "strong pick" (>55% WR) contributes more than a weak one
        strength = max(0.0, weight - 0.48) * 10  # Scale roughly to [0, 1.5]
        # Split signal equally among the card's colors (multicolor cards
        # contribute less per color)
        per_color = strength / max(1, len(card.colors))
        for color in card.colors:
            if color in signal:
                signal[color] += per_color

    return signal


def score_card_for_pair(
    card_colors: set[str],
    pair: str,
    base_score: float,
    pick_depth: int,
    pair_popularity: float = 1.0,
) -> float:
    """Score a card assuming you commit to a specific two-color pair.

    Args:
        card_colors: Card's color identity (subset of WUBRG)
        pair: Two-letter color pair (e.g. "WU")
        base_score: Base card score (GIH WR * 100 + other bonuses)
        pick_depth: How many cards have been picked (0 = P1P1)
        pair_popularity: Multiplier for overall pair strength in the format

    Returns:
        Adjusted score for this pair scenario
    """
    pair_set = set(pair)
    extras = card_colors - pair_set
    inside = card_colors & pair_set

    # Commitment scaling based on pick depth (matches untapped's "dynamic"
    # behavior: later picks commit harder to a pair)
    if pick_depth <= 3:
        on_bonus = 5.0
        splash_penalty = -3.0
        off_penalty = -10.0
    elif pick_depth <= 8:
        on_bonus = 10.0
        splash_penalty = -6.0
        off_penalty = -18.0
    elif pick_depth <= 20:
        on_bonus = 16.0
        splash_penalty = -12.0
        off_penalty = -30.0
    else:
        on_bonus = 22.0
        splash_penalty = -20.0
        off_penalty = -45.0

    # Colorless cards fit anywhere
    if not card_colors:
        return base_score * pair_popularity + on_bonus * 0.5

    if not extras:
        # Entirely in the pair — best case
        fit_bonus = on_bonus
        if len(inside) == 2:
            # Dual-color card fits perfectly in this pair
            fit_bonus *= 1.4
    elif len(extras) == 1 and len(inside) >= 1:
        # One color off — splashable
        fit_bonus = splash_penalty
    else:
        # Way off (2+ colors outside pair, or entirely off-color)
        fit_bonus = off_penalty

    return base_score * pair_popularity + fit_bonus


def get_card_type_score(type_line: str, oracle_text: str) -> tuple[float, str]:
    """Score card by type and mechanics.

    Args:
        type_line: Card type line (e.g., "Creature - Human Wizard")
        oracle_text: Card oracle text

    Returns:
        Tuple of (score, reason) where score is 0-20 and reason explains it
    """
    oracle_lower = oracle_text.lower() if oracle_text else ""
    type_lower = type_line.lower() if type_line else ""

    # Removal detection
    removal_words = ["destroy", "exile", "damage", "fights", "-x/-x", "murder", "kill"]
    if any(word in oracle_lower for word in removal_words) and "creature" in oracle_lower:
        return (15.0, "removal")

    # Card draw
    if "draw" in oracle_lower and "card" in oracle_lower:
        return (10.0, "card draw")

    # Bombs (planeswalkers, big effects)
    if "planeswalker" in type_lower:
        return (20.0, "planeswalker")

    # Evasion
    if any(word in oracle_lower for word in ["flying", "menace", "trample", "unblockable"]):
        return (8.0, "evasion")

    # Creatures are decent baseline
    if "creature" in type_lower:
        return (5.0, "creature")

    # Lands
    if "land" in type_lower and "basic" not in type_lower:
        return (3.0, "fixing")

    return (0.0, "")


def check_synergy(
    card: ScryfallCard,
    picked_cards: list[int],
    scryfall: ScryfallCache
) -> tuple[float, str]:
    """Check for synergies with picked cards.

    Scans ALL picked cards (not just recent) to detect:
    - Direct card name references
    - Tribal density (creature type overlap)
    - Mechanic density (shared keyword/mechanic themes)
    - Archetype themes (enchantments-matter, spells-matter, etc.)

    Returns the highest-scoring synergy found.
    """
    if not picked_cards or not card:
        return (0.0, "")

    card_oracle = (card.oracle_text or "").lower()
    card_types = (card.type_line or "").lower()

    best_score = 0.0
    best_reason = ""

    # Build picked card profiles (cache oracle text and types)
    picked_profiles: list[tuple[str, str, str]] = []  # (name, oracle, types)
    for grp_id in picked_cards:
        picked = scryfall.get_card_by_arena_id(grp_id)
        if picked:
            picked_profiles.append((
                picked.name,
                (picked.oracle_text or "").lower(),
                (picked.type_line or "").lower(),
            ))

    if not picked_profiles:
        return (0.0, "")

    # 1. Direct name reference (strongest synergy)
    for name, _, _ in picked_profiles:
        if name.lower() in card_oracle:
            return (12.0, f"synergy with {name}")

    # 2. Tribal density — count how many picked creatures share a type
    creature_types = [
        "goblin", "elf", "merfolk", "zombie", "vampire", "human",
        "wizard", "warrior", "eldrazi", "faerie", "rat", "spider",
        "knight", "soldier", "beast", "elemental", "angel", "demon",
        "dragon", "dinosaur", "cat", "dog", "bird", "squirrel",
        "skeleton", "spirit", "rogue", "cleric", "shaman", "druid",
        "pirate", "scout", "mole", "badger", "sphinx",
    ]
    for tribe in creature_types:
        if tribe in card_types or tribe in card_oracle:
            tribe_count = sum(
                1 for _, oracle, types in picked_profiles
                if tribe in types or tribe in oracle
            )
            if tribe_count >= 3:
                score = 10.0
                reason = f"{tribe} tribal ({tribe_count} in deck)"
            elif tribe_count >= 1:
                score = 5.0
                reason = f"{tribe} synergy"
            else:
                continue
            if score > best_score:
                best_score = score
                best_reason = reason

    # 3. Mechanic/keyword density — shared draft archetypes
    mechanics = [
        "energy", "adapt", "proliferate", "counter", "token",
        "graveyard", "sacrifice", "mill", "discard", "deathtouch",
        "lifegain", "life", "enchant", "aura", "equipment", "equip",
        "modified", "role", "food", "treasure", "clue", "blood",
        "flashback", "warp", "harmonize", "earthbend", "eerie",
        "room", "threshold", "delirium", "constellation",
        "+1/+1 counter", "flying", "defender",
        # Secrets of Strixhaven (SOS)
        "prepare", "increment", "paradigm",
        "infusion", "opus", "repartee", "converge",
    ]
    for mech in mechanics:
        if mech in card_oracle or mech in card_types:
            mech_count = sum(
                1 for _, oracle, types in picked_profiles
                if mech in oracle or mech in types
            )
            if mech_count >= 4:
                score = 10.0
                reason = f"{mech} theme ({mech_count} in deck)"
            elif mech_count >= 2:
                score = 6.0
                reason = f"{mech} synergy"
            elif mech_count >= 1:
                score = 3.0
                reason = f"{mech} synergy"
            else:
                continue
            if score > best_score:
                best_score = score
                best_reason = reason

    # 4. Archetype themes — enchantments-matter, spells-matter, go-wide
    enchantment_count = sum(1 for _, _, types in picked_profiles if "enchantment" in types)
    if ("enchantment" in card_types or "enchant" in card_oracle) and enchantment_count >= 2:
        score = 8.0 if enchantment_count >= 4 else 5.0
        reason = f"enchantments theme ({enchantment_count} in deck)"
        if score > best_score:
            best_score = score
            best_reason = reason

    instant_sorcery_count = sum(
        1 for _, _, types in picked_profiles
        if "instant" in types or "sorcery" in types
    )
    if ("instant" in card_types or "sorcery" in card_types) and instant_sorcery_count >= 3:
        score = 6.0
        reason = f"spells theme ({instant_sorcery_count} in deck)"
        if score > best_score:
            best_score = score
            best_reason = reason

    return (best_score, best_reason)


def _compute_pair_affinities(
    commitment: dict[str, float],
    pair_stats: dict[str, ColorPairStats],
) -> dict[str, float]:
    """Compute how strongly each two-color pair fits picks + meta.

    Combines:
      - User's committed colors (from picked cards)
      - 17lands color-pair format win rate (popularity/strength)

    Returns dict mapping pair → affinity score (higher = better fit).
    """
    # Normalize commitment signal to [0, 1] range per-color (total can exceed 1)
    total = sum(commitment.values())
    normalized = {c: (v / total if total > 0 else 0.0) for c, v in commitment.items()}

    # Baseline pair win rate: average across all pairs (fallback)
    if pair_stats:
        wrs = [s.win_rate for s in pair_stats.values() if s.games >= 50]
        baseline_wr = sum(wrs) / len(wrs) if wrs else 0.53
    else:
        baseline_wr = 0.53

    affinities = {}
    for pair in TWO_COLOR_PAIRS:
        # User commitment to this pair's colors
        user_signal = normalized[pair[0]] + normalized[pair[1]]

        # Format strength
        stats = pair_stats.get(pair)
        wr = stats.win_rate if stats and stats.games >= 50 else baseline_wr
        # Scale wr relative to baseline: > baseline → bonus, < baseline → penalty
        meta_bonus = (wr - baseline_wr) * 100  # -5 to +5 typically

        # Affinity is commitment-weighted meta strength
        affinities[pair] = user_signal * 30 + meta_bonus
    return affinities


def evaluate_pack(
    cards_in_pack: list[int],
    picked_cards: list[int],
    set_code: str,
    scryfall: ScryfallCache,
    draft_stats: Optional[DraftStatsCache] = None,
    mtgadb: Optional[MTGADatabase] = None,
) -> list[CardEvaluation]:
    """Evaluate all cards in a pack with composite per-color-pair scoring.

    For each card, computes a score for each of the 10 two-color pairs
    (inspired by untapped.gg Draftsmith), then picks the best pair that
    matches the user's committed colors and the format's strong archetypes.

    Args:
        cards_in_pack: List of grp_ids in current pack
        picked_cards: List of grp_ids already picked
        set_code: Set code for 17lands lookup (e.g., "MH3")
        scryfall: Scryfall cache for card data
        draft_stats: Optional 17lands stats cache
        mtgadb: Optional MTGA database for card names

    Returns:
        List of CardEvaluation sorted by score (highest first). Each
        evaluation includes per_pair_scores and a best_pair recommendation.
    """
    pick_depth = len(picked_cards)

    # Weighted commitment signal per color (stronger than binary on/off)
    commitment = compute_color_commitment(picked_cards, scryfall, draft_stats, set_code)

    # 17lands color-pair stats (per-pair format win rates)
    pair_stats: dict[str, ColorPairStats] = {}
    if draft_stats and set_code:
        pair_stats = draft_stats.get_color_pair_stats(set_code)

    # Affinity per pair based on user commitment + format strength
    pair_affinities = _compute_pair_affinities(commitment, pair_stats)

    # Baseline pair WR for score_card_for_pair multiplier
    if pair_stats:
        wrs = [s.win_rate for s in pair_stats.values() if s.games >= 50]
        baseline_wr = sum(wrs) / len(wrs) if wrs else 0.53
    else:
        baseline_wr = 0.53

    # Pre-compute graph synergy recommendations once for the whole pack
    graph_rec_dict: dict[str, float] = {}
    try:
        from arenamcp.synergy import get_synergy_graph
        sg = get_synergy_graph()
        if sg is not None and picked_cards:
            picked_names = []
            for pid in picked_cards:
                pc = scryfall.get_card_by_arena_id(pid)
                if pc:
                    picked_names.append(pc.name)
            if picked_names:
                recs = sg.get_cluster_recommendations(picked_names, top_n=30)
                graph_rec_dict = dict(recs)
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"Graph synergy lookup error: {e}")

    evaluations = []

    for grp_id in cards_in_pack:
        card = scryfall.get_card_by_arena_id(grp_id)

        # Fall back to MTGA database for name if Scryfall fails
        if not card and mtgadb and mtgadb.available:
            mtga_card = mtgadb.get_card(grp_id)
            if mtga_card:
                card_name = mtga_card.name
            else:
                continue
        elif not card:
            continue
        else:
            card_name = card.name

        # ---- Compute base (color-agnostic) score ----
        base_score = 0.0
        reasons = []

        # 17lands GIH win rate
        gih_wr = None
        if set_code and draft_stats:
            stats = draft_stats.get_draft_rating(card_name, set_code)
            if stats and stats.gih_wr:
                gih_wr = stats.gih_wr
                base_score += gih_wr * 100
                reasons.append(f"{int(gih_wr * 100)}% WR")

        # Card type value (fallback/supplement to 17lands)
        if card:
            type_score, type_reason = get_card_type_score(
                card.type_line, card.oracle_text
            )
            if not gih_wr:
                # Without 17lands data, differentiate cards by rarity + CMC +
                # stats so scores aren't clustered at the same value.
                rarity_map = {"common": 0, "uncommon": 4, "rare": 10, "mythic": 15}
                rarity_score = rarity_map.get(getattr(card, "rarity", ""), 0)

                # CMC curve: prefer 2-4 drops in most drafts
                cmc = getattr(card, "cmc", 0) or 0
                if "creature" in (card.type_line or "").lower():
                    if 2 <= cmc <= 4:
                        cmc_score = 3
                    elif cmc == 5 or cmc == 1:
                        cmc_score = 1
                    else:
                        cmc_score = -1
                    # Power/toughness rough quality signal
                    try:
                        p = int(getattr(card, "power", "0") or "0")
                        t = int(getattr(card, "toughness", "0") or "0")
                        stat_score = min(6, (p + t) // 2)
                    except (ValueError, TypeError):
                        stat_score = 0
                else:
                    cmc_score = 0
                    stat_score = 0

                base_score += 35 + type_score + rarity_score + cmc_score + stat_score
                if rarity_score >= 10:
                    reasons.append(f"{card.rarity}")
            if type_reason and type_reason != "creature":
                reasons.append(type_reason)

            # Synergy bonus (keyword/tribal)
            syn_score, syn_reason = check_synergy(card, picked_cards, scryfall)
            if syn_score:
                base_score += syn_score
                reasons.append(syn_reason)

            # Graph-based synergy bonus
            graph_score = graph_rec_dict.get(card_name, 0.0)
            if graph_score > 0:
                bonus = min(graph_score * 15, 15.0)
                base_score += bonus
                reasons.append(f"graph synergy +{bonus:.0f}")

        card_colors = set(card.colors) if card and card.colors else set()

        # ---- Compute per-color-pair scores ----
        # For each pair, ask: "If I committed to this pair, how good is this card?"
        # The card's final score is the BEST of (affinity-weighted) per-pair scores,
        # since we pick the pair the card points us toward.
        per_pair: dict[str, float] = {}
        for pair in TWO_COLOR_PAIRS:
            pair_mult = 1.0
            ps = pair_stats.get(pair)
            if ps and ps.games >= 50:
                # Scale score by how good this pair is relative to baseline
                pair_mult = ps.win_rate / baseline_wr  # ~0.95 - 1.05 range
            pair_score = score_card_for_pair(
                card_colors=card_colors,
                pair=pair,
                base_score=base_score,
                pick_depth=pick_depth,
                pair_popularity=pair_mult,
            )
            # Weight by user's commitment affinity to this pair
            weighted = pair_score + pair_affinities[pair] * (pick_depth / 45.0)
            per_pair[pair] = weighted

        # Best pair for this card
        best_pair = max(per_pair, key=per_pair.get) if per_pair else None
        final_score = per_pair[best_pair] if best_pair else base_score

        if best_pair and pick_depth >= 3:
            # Attach color context reason based on best pair
            if card_colors.issubset(set(best_pair)):
                if card_colors:
                    reasons.append(f"fits {best_pair}")
            elif card_colors & set(best_pair):
                reasons.append(f"splash in {best_pair}")

        tier = get_tier(final_score)
        best_reason = reasons[-1] if reasons else ""

        evaluations.append(CardEvaluation(
            grp_id=grp_id,
            name=card_name,
            score=final_score,
            gih_wr=gih_wr,
            reason=best_reason,
            all_reasons=reasons,
            tier=tier,
            per_pair_scores=per_pair,
            best_pair=best_pair,
        ))

    evaluations.sort(key=lambda e: e.score, reverse=True)
    return evaluations


def format_pick_recommendation(
    evaluations: list[CardEvaluation],
    pack_number: int,
    pick_number: int,
    num_recommendations: int = 1,
) -> str:
    """Format spoken recommendation for top picks.

    Args:
        evaluations: Evaluated cards sorted by score
        pack_number: Current pack number (1-3)
        pick_number: Current pick in pack
        num_recommendations: Cards picked per round (1 for normal drafts,
            2 for PickTwo drafts). Controls whether advice says "Take X"
            or "Take X and Y".

    Returns:
        Human-readable recommendation string for TTS
    """
    if not evaluations:
        return f"Pack {pack_number}, Pick {pick_number}. No cards found."

    pack_pick = f"Pack {pack_number}, Pick {pick_number}."
    top1 = evaluations[0]
    r1 = f", {top1.reason}" if top1.reason else ""

    if num_recommendations >= 2 and len(evaluations) >= 2:
        # PickTwo draft — recommend a pair
        top2 = evaluations[1]
        r2 = f", {top2.reason}" if top2.reason else ""
        return f"{pack_pick} Take {top1.name}{r1} and {top2.name}{r2}."

    # Normal draft — single best pick, mention runner-up for context
    if len(evaluations) >= 2:
        runner = evaluations[1]
        gap = top1.score - runner.score
        # Only mention runner-up if it's close (within 8 points)
        if gap < 8:
            return f"{pack_pick} Take {top1.name}{r1}. Close with {runner.name}."
    return f"{pack_pick} Take {top1.name}{r1}."
