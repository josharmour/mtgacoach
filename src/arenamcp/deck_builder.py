"""Deck builder for MTGA draft pools using 17lands card ratings.

Suggests deck configurations based on GIHWR (Games In Hand Win Rate)
and archetype constraints (Aggro/Midrange/Control).
Adapted from Voice Assistant's DeckBuilderV2 to use mtgacoach data sources.
"""

import logging
from collections import Counter
from dataclasses import dataclass
from typing import Optional

from typing import Any, Optional, Union

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
class WildcardInventory:
    """Player's available wildcards by rarity (from PlayerInventory)."""
    common: int = 0
    uncommon: int = 0
    rare: int = 0
    mythic: int = 0

    @classmethod
    def from_dict(cls, data: Union["WildcardInventory", dict[str, Any], None]) -> "WildcardInventory":
        if isinstance(data, cls):
            return data
        if not isinstance(data, dict):
            return cls()
        return cls(
            common=int(data.get("common", data.get("Common", data.get("wcCommon", 0)))),
            uncommon=int(data.get("uncommon", data.get("Uncommon", data.get("wcUncommon", 0)))),
            rare=int(data.get("rare", data.get("Rare", data.get("wcRare", 0)))),
            mythic=int(data.get("mythic", data.get("Mythic", data.get("wcMythic", 0)))),
        )


@dataclass
class CraftCost:
    """Wildcard craft requirements for a deck."""
    common: int = 0
    uncommon: int = 0
    rare: int = 0
    mythic: int = 0

    @property
    def total(self) -> int:
        return self.common + self.uncommon + self.rare + self.mythic

    def fits_in(self, inventory: WildcardInventory) -> bool:
        return (
            self.common <= inventory.common
            and self.uncommon <= inventory.uncommon
            and self.rare <= inventory.rare
            and self.mythic <= inventory.mythic
        )


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


@dataclass
class TieredDeckSuggestion:
    """Deck suggestion categorized by budget/crafting tier."""
    tier: str  # "0-Wildcard", "Budget Crafting", "Meta Top-Tier"
    archetype: str
    main_colors: str
    color_pair_name: str
    maindeck: dict[str, int]
    sideboard: dict[str, int]
    lands: dict[str, int]
    avg_gihwr: float
    score: float
    craft_cost: CraftCost
    is_fully_owned: bool
    fits_inventory: bool

    @property
    def name(self) -> str:
        return f"{self.color_pair_name} {self.archetype}" if self.color_pair_name not in self.archetype else self.archetype

    def to_arena_import(self) -> str:
        """Format as standard MTGA import string (Deck + Sideboard)."""
        lines = ["Deck"]
        full_main = dict(self.maindeck)
        for land, count in self.lands.items():
            full_main[land] = full_main.get(land, 0) + count
        for card, count in full_main.items():
            lines.append(f"{count} {card}")
        if self.sideboard:
            lines.append("\nSideboard")
            for card, count in self.sideboard.items():
                lines.append(f"{count} {card}")
        return "\n".join(lines)


FORMAT_META_TEMPLATES: dict[str, list[dict[str, Any]]] = {
    "standard": [
        {
            "name": "Mono Red Aggro",
            "colors": "R",
            "archetype": "Aggro",
            "maindeck": {
                "Monastery Swiftspear": 4,
                "Slickshot Show-Off": 4,
                "Emberheart Challenger": 4,
                "Hired Claw": 4,
                "Lightning Strike": 4,
                "Shock": 4,
                "Monstrous Rage": 4,
                "Demonic Ruckus": 4,
                "Mountain": 20,
                "Rockface Village": 4,
            },
            "sideboard": {"Urabrask's Forge": 3, "Torch the Tower": 4},
            "rarities": {
                "Slickshot Show-Off": "rare",
                "Emberheart Challenger": "rare",
                "Rockface Village": "rare",
                "Hired Claw": "rare",
                "Monastery Swiftspear": "uncommon",
                "Lightning Strike": "uncommon",
                "Demonic Ruckus": "uncommon",
                "Monstrous Rage": "uncommon",
                "Shock": "common",
                "Mountain": "common",
                "Urabrask's Forge": "rare",
                "Torch the Tower": "uncommon",
            },
            "score": 88.0,
        },
        {
            "name": "Azorius Control",
            "colors": "WU",
            "archetype": "Control",
            "maindeck": {
                "No More Lies": 4,
                "Get Lost": 3,
                "Sunfall": 4,
                "Temporary Lockdown": 3,
                "Deducate": 4,
                "Memory Deluge": 3,
                "The Wandering Emperor": 3,
                "Plains": 8,
                "Island": 8,
                "Meticulous Archive": 4,
                "Restless Anchorage": 4,
                "Adarkar Wastes": 4,
            },
            "sideboard": {"Negate": 3, "Elspeth's Smite": 3},
            "rarities": {
                "No More Lies": "uncommon",
                "Get Lost": "rare",
                "Sunfall": "rare",
                "Temporary Lockdown": "rare",
                "Deducate": "common",
                "Memory Deluge": "rare",
                "The Wandering Emperor": "mythic",
                "Meticulous Archive": "rare",
                "Restless Anchorage": "rare",
                "Adarkar Wastes": "rare",
                "Plains": "common",
                "Island": "common",
                "Negate": "common",
                "Elspeth's Smite": "uncommon",
            },
            "score": 86.5,
        },
        {
            "name": "Golgari Midrange",
            "colors": "BG",
            "archetype": "Midrange",
            "maindeck": {
                "Deep-Cavern Bat": 4,
                "Mosswood Dreadknight": 4,
                "Glissa Sunslayer": 3,
                "Preacher of the Schism": 4,
                "Go for the Throat": 4,
                "Cut Down": 3,
                "Liliana of the Veil": 2,
                "Swamp": 8,
                "Forest": 7,
                "Restless Cottage": 4,
                "Llanowar Wastes": 4,
                "Underground Mortuary": 3,
            },
            "sideboard": {"Duress": 3, "Tranquil Frillback": 3},
            "rarities": {
                "Deep-Cavern Bat": "uncommon",
                "Mosswood Dreadknight": "rare",
                "Glissa Sunslayer": "rare",
                "Preacher of the Schism": "rare",
                "Go for the Throat": "uncommon",
                "Cut Down": "uncommon",
                "Liliana of the Veil": "mythic",
                "Restless Cottage": "rare",
                "Llanowar Wastes": "rare",
                "Underground Mortuary": "rare",
                "Swamp": "common",
                "Forest": "common",
                "Duress": "common",
                "Tranquil Frillback": "rare",
            },
            "score": 85.0,
        },
    ]
}


class DeckBuilderV2:
    """Builds deck suggestions from a draft pool using 17lands data.

    Uses mtgacoach's DraftStatsCache for GIHWR and enrich_with_oracle_text
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

        # AI Monte Carlo Auto-Optimizer (Hill Climbing Permutations)
        import random
        best_maindeck = maindeck.copy()
        best_sideboard = sideboard.copy()
        best_lands = archetype["lands"]

        def eval_deck(md: dict[str, int]) -> float:
            tc = sum(md.values())
            if tc == 0: return -100.0
            gihwr = sum(card_ratings[c].gih_win_rate * ct for c, ct in md.items()) / tc
            cc = sum(ct for c, ct in md.items() if card_ratings[c].is_creature)
            tcmc = sum(card_ratings[c].cmc * ct for c, ct in md.items())
            acmc = tcmc / tc
            p = 0.0
            if cc < archetype["min_creatures"]: p += (archetype["min_creatures"] - cc) * 0.005
            if acmc > archetype["max_avg_cmc"]: p += (acmc - archetype["max_avg_cmc"]) * 0.02
            return gihwr - p

        best_score = eval_deck(best_maindeck)

        all_pool_count = sum(maindeck.values()) + sum(sideboard.values())
        if all_pool_count >= 22:
            for _ in range(5000):
                test_lands = random.choice([15, 16, 17, 18])
                target_nl = 40 - test_lands
                if all_pool_count < target_nl:
                    continue

                md_list = []
                for c, ct in best_maindeck.items(): md_list.extend([c] * ct)
                sb_list = []
                for c, ct in best_sideboard.items(): sb_list.extend([c] * ct)

                # Adjust for land count changes
                curr_nl = len(md_list)
                while curr_nl > target_nl and md_list:
                    c = random.choice(md_list)
                    md_list.remove(c)
                    sb_list.append(c)
                    curr_nl -= 1
                while curr_nl < target_nl and sb_list:
                    c = random.choice(sb_list)
                    sb_list.remove(c)
                    md_list.append(c)
                    curr_nl += 1

                # Mutate: Swap 1 to 3 cards
                num_swaps = random.randint(1, 3)
                for _ in range(num_swaps):
                    if md_list and sb_list:
                        out_c = random.choice(md_list)
                        in_c = random.choice(sb_list)
                        md_list.remove(out_c)
                        md_list.append(in_c)
                        sb_list.remove(in_c)
                        sb_list.append(out_c)

                # Tally and Evaluate
                from collections import Counter
                new_md = dict(Counter(md_list))
                new_sb = dict(Counter(sb_list))
                score = eval_deck(new_md)

                if score > best_score:
                    best_score = score
                    best_maindeck = new_md
                    best_sideboard = new_sb
                    best_lands = test_lands

        maindeck = best_maindeck
        sideboard = best_sideboard
        archetype["lands"] = best_lands
        total_cards = sum(maindeck.values())

        # Recalculate metrics from optimized deck
        creature_count = sum(ct for c, ct in maindeck.items() if card_ratings[c].is_creature)
        total_cmc = sum(card_ratings[c].cmc * ct for c, ct in maindeck.items())

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

    def _normalize_collection(self, player_cards: dict[Union[int, str], int]) -> dict[str, int]:
        """Normalize player collection (GetPlayerCardsV3) to card_name -> count owned."""
        owned: Counter = Counter()
        if not player_cards:
            return {}

        for key, count in player_cards.items():
            qty = max(0, int(count))
            if qty == 0:
                continue

            # If key is string card name
            if isinstance(key, str) and not key.isdigit():
                owned[key] += qty
                continue

            # If key is grp_id
            grp_id = int(key)
            name = None
            if self.enrich_fn:
                info = self.enrich_fn(grp_id)
                if info and info.get("name") and "Unknown" not in info["name"]:
                    name = info["name"]

            if name:
                owned[name] += qty
            else:
                owned[str(grp_id)] += qty

        return dict(owned)

    def _resolve_card_rarity(
        self, card_name: str, custom_rarities: Optional[dict[str, str]] = None
    ) -> str:
        """Resolve rarity for a card ('common', 'uncommon', 'rare', 'mythic')."""
        if custom_rarities and card_name in custom_rarities:
            return custom_rarities[card_name].lower()

        if self.draft_stats:
            stats = self.draft_stats.get_draft_rating(card_name, "")
            if stats and stats.rarity:
                return stats.rarity.lower()

        if card_name in self.BASIC_LANDS:
            return "common"

        return "common"

    def calculate_craft_cost(
        self,
        deck_cards: dict[str, int],
        owned_cards: dict[str, int],
        custom_rarities: Optional[dict[str, str]] = None,
    ) -> CraftCost:
        """Calculate missing wildcards needed to build deck_cards from owned_cards."""
        craft = CraftCost()

        for card_name, required in deck_cards.items():
            if card_name in self.BASIC_LANDS:
                continue

            owned = owned_cards.get(card_name, 0)
            missing = max(0, required - owned)
            if missing <= 0:
                continue

            rarity = self._resolve_card_rarity(card_name, custom_rarities)
            if rarity in ("mythic", "mythic rare"):
                craft.mythic += missing
            elif rarity == "rare":
                craft.rare += missing
            elif rarity in ("uncommon", "uc"):
                craft.uncommon += missing
            else:
                craft.common += missing

        return craft

    def suggest_tiered_decks(
        self,
        player_cards: Optional[dict[Union[int, str], int]] = None,
        wildcards: Optional[Union[WildcardInventory, dict[str, Any]]] = None,
        format_name: str = "standard",
        candidate_decks: Optional[list[dict[str, Any]]] = None,
        draft_grp_ids: Optional[list[int]] = None,
        set_code: Optional[str] = None,
        top_n_per_tier: int = 3,
    ) -> dict[str, list[TieredDeckSuggestion]]:
        """Suggest 3-tiered deck configurations for any event format.

        Tiers:
          - 0-Wildcard Decks (100% buildable from owned collection)
          - Budget Crafting (fits within user's exact wildcard count)
          - Meta Top-Tier (top format archetypes with exact wildcard craft costs)

        Args:
            player_cards: GetPlayerCardsV3 collection map (grp_id/name -> count).
            wildcards: PlayerInventory wildcard counts (Common/Uncommon/Rare/Mythic).
            format_name: Format string (e.g. "standard", "pioneer", "alchemy").
            candidate_decks: Optional custom deck templates.
            draft_grp_ids: Optional list of drafted card IDs (for limited format).
            set_code: Optional set code for 17lands lookups.
            top_n_per_tier: Max deck suggestions per tier.

        Returns:
            Dict mapping tier names to lists of TieredDeckSuggestion.
        """
        from dataclasses import replace

        owned_cards = self._normalize_collection(player_cards or {})
        wc_inv = WildcardInventory.from_dict(wildcards)
        fmt_key = format_name.lower()

        candidates: list[dict[str, Any]] = []
        if candidate_decks:
            candidates.extend(candidate_decks)

        if draft_grp_ids:
            draft_suggestions = self.suggest_deck(draft_grp_ids, set_code or "", top_n=5)
            for ds in draft_suggestions:
                candidates.append({
                    "name": f"{ds.color_pair_name} {ds.archetype}",
                    "colors": ds.main_colors,
                    "archetype": ds.archetype,
                    "maindeck": ds.maindeck,
                    "sideboard": ds.sideboard,
                    "lands": ds.lands,
                    "score": ds.score,
                    "avg_gihwr": ds.avg_gihwr,
                })

        meta_templates = FORMAT_META_TEMPLATES.get(
            fmt_key, FORMAT_META_TEMPLATES.get("standard", [])
        )
        for t in meta_templates:
            if not any(c.get("name") == t["name"] for c in candidates):
                candidates.append(t)

        zero_wc: list[TieredDeckSuggestion] = []
        budget: list[TieredDeckSuggestion] = []
        meta_top: list[TieredDeckSuggestion] = []

        for cand in candidates:
            main = cand.get("maindeck", {})
            side = cand.get("sideboard", {})
            lands = cand.get("lands", {})
            rarities = cand.get("rarities", {})

            full_cards: Counter = Counter(main)
            full_cards.update(side)

            craft_cost = self.calculate_craft_cost(
                dict(full_cards), owned_cards, rarities
            )
            is_fully_owned = (craft_cost.total == 0)
            fits_inventory = craft_cost.fits_in(wc_inv)

            score = cand.get("score", 75.0)
            avg_gihwr = cand.get("avg_gihwr", 0.55)
            colors = cand.get("colors", "")
            archetype_name = cand.get("archetype", cand.get("name", "Custom"))

            base_sug = TieredDeckSuggestion(
                tier="",
                archetype=cand.get("name", archetype_name),
                main_colors=colors,
                color_pair_name=self._get_color_pair_name(colors),
                maindeck=main,
                sideboard=side,
                lands=lands,
                avg_gihwr=avg_gihwr,
                score=score,
                craft_cost=craft_cost,
                is_fully_owned=is_fully_owned,
                fits_inventory=fits_inventory,
            )

            meta_top.append(replace(base_sug, tier="Meta Top-Tier"))

            if is_fully_owned:
                zero_wc.append(replace(base_sug, tier="0-Wildcard"))

            if fits_inventory:
                budget.append(replace(base_sug, tier="Budget Crafting"))

        zero_wc.sort(key=lambda s: s.score, reverse=True)
        budget.sort(key=lambda s: (s.craft_cost.total, -s.score))
        meta_top.sort(key=lambda s: s.score, reverse=True)

        return {
            "0-Wildcard": zero_wc[:top_n_per_tier],
            "Budget Crafting": budget[:top_n_per_tier],
            "Meta Top-Tier": meta_top[:top_n_per_tier],
        }

