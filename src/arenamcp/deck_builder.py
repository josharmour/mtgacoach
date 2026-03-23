"""Deck builder for MTGA draft pools using 17lands card ratings.

Suggests deck configurations based on GIHWR (Games In Hand Win Rate)
and archetype constraints (Aggro/Midrange/Control).
Adapted from Voice Assistant's DeckBuilderV2 to use ArenaMCP data sources.
"""

import logging
from collections import Counter
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class CardRating:
    """Card performance metrics."""
    name: str
    color: str
    rarity: str
    gih_win_rate: float
    avg_taken_at: float
    cmc: int = 0
    is_creature: bool = False
    type_line: str = ""
    oracle_text: str = ""


@dataclass
class DeckSuggestion:
    """Represents a suggested deck configuration."""
    archetype: str  # "Aggro", "Midrange", or "Control"
    main_colors: str
    color_pair_name: str
    maindeck: dict[str, int]
    sideboard: dict[str, int]
    lands: dict[str, int]
    avg_gihwr: float
    penalty: float
    score: float


class DeckBuilderV2:
    """Builds deck suggestions from a draft pool using 17lands data.

    Uses ArenaMCP's DraftStatsCache for GIHWR and enrich_with_oracle_text
    for card metadata, replacing the Voice Assistant's SQLite approach.
    """

    ARCHETYPES = {
        "Aggro": {
            "lands": 16,
            "min_creatures": 17,
            "max_avg_cmc": 2.40,
            "curve_requirements": {1: 4, 2: 8},
        },
        "Midrange": {
            "lands": 17,
            "min_creatures": 15,
            "max_avg_cmc": 3.04,
            "curve_requirements": {2: 6, 3: 5},
        },
        "Control": {
            "lands": 18,
            "min_creatures": 10,
            "max_avg_cmc": 3.68,
            "curve_requirements": {4: 3, 5: 2},
        },
    }

    COLOR_PAIR_NAMES = {
        "W": "Mono White", "U": "Mono Blue", "B": "Mono Black",
        "R": "Mono Red", "G": "Mono Green",
        "WU": "Azorius", "WB": "Orzhov", "WR": "Boros", "WG": "Selesnya",
        "UB": "Dimir", "UR": "Izzet", "UG": "Simic",
        "BR": "Rakdos", "BG": "Golgari", "RG": "Gruul",
    }

    BASIC_LANDS = {
        "Plains": "W", "Island": "U", "Swamp": "B",
        "Mountain": "R", "Forest": "G",
    }

    def __init__(self, draft_stats=None, enrich_fn=None) -> None:
        """Initialize deck builder.

        Args:
            draft_stats: DraftStatsCache instance for GIHWR lookups.
            enrich_fn: Function(grp_id) -> dict for card metadata.
                       Expected to return {name, oracle_text, type_line, mana_cost}.
        """
        self.draft_stats = draft_stats
        self.enrich_fn = enrich_fn

    def _get_card_info(self, grp_id: int, set_code: str) -> Optional[CardRating]:
        """Get card rating info from draft stats + enrichment.

        Args:
            grp_id: Arena card ID.
            set_code: Set code for 17lands lookup.

        Returns:
            CardRating or None if card not found.
        """
        # Get card metadata via enrichment
        if not self.enrich_fn:
            return None

        card_data = self.enrich_fn(grp_id)
        name = card_data.get("name", "")
        if not name or "Unknown" in name:
            return None

        type_line = card_data.get("type_line", "")
        mana_cost = card_data.get("mana_cost", "")

        # Parse colors from mana cost
        colors = ""
        for c in "WUBRG":
            if f"{{{c}}}" in mana_cost:
                colors += c

        # Parse CMC from mana cost
        cmc = 0
        for c in mana_cost.replace("{", "").replace("}", ""):
            if c.isdigit():
                cmc += int(c)
            elif c in "WUBRGX":
                cmc += 1

        is_creature = "creature" in type_line.lower() if type_line else False

        # Get 17lands stats
        gih_wr = 0.0
        avg_taken = 99.0
        rarity = ""
        if self.draft_stats and set_code:
            stats = self.draft_stats.get_draft_rating(name, set_code)
            if stats:
                gih_wr = stats.gih_wr or 0.0
                avg_taken = stats.alsa or 99.0

        return CardRating(
            name=name,
            color=colors,
            rarity=rarity,
            gih_win_rate=gih_wr,
            avg_taken_at=avg_taken,
            cmc=cmc,
            is_creature=is_creature,
            type_line=type_line,
            oracle_text=card_data.get("oracle_text", ""),
        )

    def suggest_deck(
        self,
        drafted_grp_ids: list[int],
        set_code: str,
        top_n: int = 3,
    ) -> list[DeckSuggestion]:
        """Suggest deck configurations based on drafted cards.

        Args:
            drafted_grp_ids: List of grp_ids (arena card IDs) from draft pool.
            set_code: Set code for 17lands lookups (e.g., "MH3").
            top_n: Number of suggestions to return.

        Returns:
            List of DeckSuggestion sorted by score (highest first).
        """
        if not drafted_grp_ids:
            return []

        # Get ratings for all drafted cards, resolving names
        card_ratings: dict[str, CardRating] = {}
        grp_to_name: dict[int, str] = {}
        name_counts: Counter = Counter()

        for grp_id in drafted_grp_ids:
            if grp_id in grp_to_name:
                # Already resolved this grp_id
                name_counts[grp_to_name[grp_id]] += 1
                continue

            rating = self._get_card_info(grp_id, set_code.upper())
            if rating and rating.name not in self.BASIC_LANDS:
                card_ratings[rating.name] = rating
                grp_to_name[grp_id] = rating.name
                name_counts[rating.name] += 1

        if not card_ratings:
            logger.warning(f"No card ratings found for {set_code}")
            return []

        logger.info(f"Building decks from {len(card_ratings)} rated cards")

        # Determine viable color pairs
        color_scores: Counter = Counter()
        for card_name, rating in card_ratings.items():
            for color in rating.color:
                color_scores[color] += name_counts[card_name] * max(rating.gih_win_rate, 0.01)

        viable_pairs = self._get_viable_color_pairs(color_scores)[:5]

        suggestions = []
        for colors in viable_pairs:
            for archetype_name in ("Aggro", "Midrange", "Control"):
                suggestion = self._build_archetype_deck(
                    card_counts=name_counts,
                    card_ratings=card_ratings,
                    colors=colors,
                    archetype_name=archetype_name,
                )
                if suggestion:
                    suggestions.append(suggestion)

        suggestions.sort(key=lambda s: s.score, reverse=True)
        return suggestions[:top_n]

    def _get_viable_color_pairs(self, color_scores: Counter) -> list[str]:
        """Determine viable color pairs from color scores."""
        pairs = []

        # Mono-color
        for color in "WUBRG":
            if color in color_scores:
                pairs.append(color)

        # Two-color combinations
        sorted_colors = [c for c, _ in color_scores.most_common(3)]
        for i, c1 in enumerate(sorted_colors):
            for c2 in sorted_colors[i + 1:]:
                pairs.append("".join(sorted([c1, c2])))

        pairs.sort(
            key=lambda p: sum(color_scores.get(c, 0) for c in p), reverse=True
        )
        return pairs

    def _build_archetype_deck(
        self,
        card_counts: Counter,
        card_ratings: dict[str, CardRating],
        colors: str,
        archetype_name: str,
    ) -> Optional[DeckSuggestion]:
        """Build a deck for specific archetype and colors."""
        archetype = self.ARCHETYPES[archetype_name]

        # Filter cards to color pair (colorless always fits)
        available_cards = []
        for card_name, rating in card_ratings.items():
            if not rating.color or all(c in colors for c in rating.color):
                available_cards.append((card_name, rating))

        # Sort by GIHWR descending
        available_cards.sort(key=lambda x: x[1].gih_win_rate, reverse=True)

        # Build maindeck
        maindeck: dict[str, int] = {}
        sideboard: dict[str, int] = {}
        total_cards = 0
        target_nonlands = 40 - archetype["lands"]

        creature_count = 0
        total_cmc = 0

        for card_name, rating in available_cards:
            if total_cards >= target_nonlands:
                sideboard[card_name] = card_counts[card_name]
                continue

            copies = min(card_counts[card_name], target_nonlands - total_cards)
            if copies > 0:
                maindeck[card_name] = copies
                total_cards += copies
                if rating.is_creature:
                    creature_count += copies
                total_cmc += rating.cmc * copies

                remaining = card_counts[card_name] - copies
                if remaining > 0:
                    sideboard[card_name] = remaining

        if total_cards == 0:
            return None

        # Calculate lands
        lands = self._suggest_lands(colors, archetype["lands"])

        # Calculate metrics
        total_gihwr = sum(
            card_ratings[card].gih_win_rate * count
            for card, count in maindeck.items()
        )
        avg_gihwr = total_gihwr / total_cards if total_cards > 0 else 0.0

        # Calculate archetype penalties
        penalty = 0.0
        avg_cmc = total_cmc / total_cards if total_cards > 0 else 0.0

        if creature_count < archetype["min_creatures"]:
            penalty += (archetype["min_creatures"] - creature_count) * 0.005
        if avg_cmc > archetype["max_avg_cmc"]:
            penalty += (avg_cmc - archetype["max_avg_cmc"]) * 0.02

        score = avg_gihwr - penalty

        return DeckSuggestion(
            archetype=archetype_name,
            main_colors=colors,
            color_pair_name=self._get_color_pair_name(colors),
            maindeck=maindeck,
            sideboard=sideboard,
            lands=lands,
            avg_gihwr=avg_gihwr,
            penalty=penalty,
            score=score,
        )

    def _suggest_lands(self, colors: str, total_lands: int) -> dict[str, int]:
        """Suggest basic land distribution."""
        land_map = {
            "W": "Plains", "U": "Island", "B": "Swamp",
            "R": "Mountain", "G": "Forest",
        }

        if len(colors) == 0:
            return {"Plains": total_lands}
        elif len(colors) == 1:
            return {land_map[colors]: total_lands}
        else:
            per_color = total_lands // len(colors)
            lands: dict[str, int] = {}
            for i, color in enumerate(colors):
                if i == len(colors) - 1:
                    lands[land_map[color]] = total_lands - sum(lands.values())
                else:
                    lands[land_map[color]] = per_color
            return lands

    def _get_color_pair_name(self, colors: str) -> str:
        """Get human-readable color pair name."""
        return self.COLOR_PAIR_NAMES.get(colors, colors)
