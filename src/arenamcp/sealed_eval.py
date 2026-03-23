"""Sealed pool analysis for deck building recommendations.

Analyzes a sealed pool using 17lands win rate data to suggest
the best color combinations and cards to build around.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Color combinations for 2-color decks
TWO_COLOR_PAIRS = [
    ("W", "U"), ("W", "B"), ("W", "R"), ("W", "G"),
    ("U", "B"), ("U", "R"), ("U", "G"),
    ("B", "R"), ("B", "G"),
    ("R", "G"),
]

COLOR_NAMES = {
    "W": "White",
    "U": "Blue",
    "B": "Black",
    "R": "Red",
    "G": "Green",
}


@dataclass
class ColorAnalysis:
    """Analysis of a color or color pair in a sealed pool."""
    colors: tuple[str, ...]
    card_count: int
    creature_count: int
    avg_win_rate: float
    best_cards: list[dict[str, Any]]  # Top cards by win rate
    playable_count: int  # Cards good enough to play (>50% WR)


@dataclass
class SealedAnalysis:
    """Complete analysis of a sealed pool."""
    set_code: str
    total_cards: int
    color_analyses: list[ColorAnalysis]
    recommended_build: ColorAnalysis
    splash_candidates: list[dict[str, Any]]  # High WR off-color cards
    top_cards: list[dict[str, Any]]  # Best cards in pool overall


def analyze_sealed_pool(
    pool_cards: list[dict[str, Any]],
    set_code: str,
    draft_stats: Optional[Any] = None
) -> SealedAnalysis:
    """Analyze a sealed pool and recommend a build.

    Args:
        pool_cards: List of card dicts with name, colors, type_line, gih_wr, etc.
        set_code: The set code for 17lands lookups
        draft_stats: Optional DraftStatsCache for additional lookups

    Returns:
        SealedAnalysis with recommendations
    """
    if not pool_cards:
        return SealedAnalysis(
            set_code=set_code,
            total_cards=0,
            color_analyses=[],
            recommended_build=ColorAnalysis((), 0, 0, 0.0, [], 0),
            splash_candidates=[],
            top_cards=[],
        )

    # Group cards by color identity
    cards_by_color: dict[str, list[dict]] = defaultdict(list)
    colorless_cards: list[dict] = []

    for card in pool_cards:
        colors = card.get("colors", [])
        if not colors:
            colorless_cards.append(card)
        else:
            for color in colors:
                cards_by_color[color].append(card)

    # Analyze each 2-color pair
    color_analyses: list[ColorAnalysis] = []

    for color1, color2 in TWO_COLOR_PAIRS:
        # Get cards in these colors (including gold cards)
        pair_cards = []
        seen_names = set()

        for card in pool_cards:
            card_colors = set(card.get("colors", []))
            # Include if mono-color in pair, or gold card within pair
            if card_colors and card_colors.issubset({color1, color2}):
                if card.get("name") not in seen_names:
                    pair_cards.append(card)
                    seen_names.add(card.get("name"))

        # Add colorless cards
        for card in colorless_cards:
            if card.get("name") not in seen_names:
                pair_cards.append(card)
                seen_names.add(card.get("name"))

        if not pair_cards:
            continue

        # Calculate stats
        win_rates = [c.get("gih_wr", 0) or 0 for c in pair_cards]
        avg_wr = sum(win_rates) / len(win_rates) if win_rates else 0

        creature_count = sum(
            1 for c in pair_cards
            if "creature" in c.get("type_line", "").lower()
        )

        playable_count = sum(1 for wr in win_rates if wr > 0.50)

        # Sort by win rate for best cards
        sorted_cards = sorted(pair_cards, key=lambda c: c.get("gih_wr", 0) or 0, reverse=True)
        best_cards = sorted_cards[:5]

        analysis = ColorAnalysis(
            colors=(color1, color2),
            card_count=len(pair_cards),
            creature_count=creature_count,
            avg_win_rate=avg_wr,
            best_cards=best_cards,
            playable_count=playable_count,
        )
        color_analyses.append(analysis)

    # Sort by playable count, then by average win rate
    color_analyses.sort(key=lambda a: (a.playable_count, a.avg_win_rate), reverse=True)

    # Best build is the top color pair
    recommended = color_analyses[0] if color_analyses else ColorAnalysis((), 0, 0, 0.0, [], 0)

    # Find splash candidates (high WR cards outside main colors)
    splash_candidates = []
    if recommended.colors:
        main_colors = set(recommended.colors)
        for card in pool_cards:
            card_colors = set(card.get("colors", []))
            # Off-color card with high win rate
            if card_colors and not card_colors.issubset(main_colors):
                wr = card.get("gih_wr", 0) or 0
                if wr > 0.55:  # Only splash really good cards
                    splash_candidates.append(card)

        splash_candidates.sort(key=lambda c: c.get("gih_wr", 0) or 0, reverse=True)
        splash_candidates = splash_candidates[:3]  # Top 3 splash options

    # Top cards overall
    all_sorted = sorted(pool_cards, key=lambda c: c.get("gih_wr", 0) or 0, reverse=True)
    top_cards = all_sorted[:10]

    return SealedAnalysis(
        set_code=set_code,
        total_cards=len(pool_cards),
        color_analyses=color_analyses[:5],  # Top 5 color pairs
        recommended_build=recommended,
        splash_candidates=splash_candidates,
        top_cards=top_cards,
    )


def format_sealed_recommendation(analysis: SealedAnalysis) -> str:
    """Format sealed analysis as spoken advice.

    Args:
        analysis: SealedAnalysis from analyze_sealed_pool

    Returns:
        Human-readable recommendation string
    """
    if not analysis.color_analyses:
        return "Unable to analyze pool - no card data available."

    rec = analysis.recommended_build
    if not rec.colors:
        return "Unable to determine best colors."

    color_names = "/".join(COLOR_NAMES.get(c, c) for c in rec.colors)
    wr_pct = f"{rec.avg_win_rate * 100:.0f}%" if rec.avg_win_rate else "N/A"

    lines = [
        f"Build {color_names}.",
        f"{rec.playable_count} playables, {rec.creature_count} creatures, {wr_pct} average win rate.",
    ]

    # Best cards
    if rec.best_cards:
        best_names = [c.get("name", "?") for c in rec.best_cards[:3]]
        lines.append(f"Key cards: {', '.join(best_names)}.")

    # Splash suggestion
    if analysis.splash_candidates:
        splash = analysis.splash_candidates[0]
        splash_name = splash.get("name", "?")
        splash_wr = splash.get("gih_wr", 0) or 0
        lines.append(f"Consider splashing {splash_name} at {splash_wr*100:.0f}% win rate.")

    # Alternative
    if len(analysis.color_analyses) > 1:
        alt = analysis.color_analyses[1]
        alt_colors = "/".join(COLOR_NAMES.get(c, c) for c in alt.colors)
        lines.append(f"Alternative: {alt_colors} with {alt.playable_count} playables.")

    return " ".join(lines)


def format_sealed_detailed(analysis: SealedAnalysis) -> str:
    """Format detailed sealed analysis for display.

    Args:
        analysis: SealedAnalysis from analyze_sealed_pool

    Returns:
        Multi-line detailed breakdown
    """
    if not analysis.color_analyses:
        return "Unable to analyze pool."

    lines = [
        f"=== SEALED POOL ANALYSIS ({analysis.total_cards} cards) ===",
        "",
        "COLOR PAIR RANKINGS:",
    ]

    for i, ca in enumerate(analysis.color_analyses[:5], 1):
        color_names = "/".join(COLOR_NAMES.get(c, c) for c in ca.colors)
        wr_pct = f"{ca.avg_win_rate * 100:.1f}%" if ca.avg_win_rate else "N/A"
        rec_marker = " << RECOMMENDED" if i == 1 else ""
        lines.append(
            f"  {i}. {color_names}: {ca.playable_count} playables, "
            f"{ca.creature_count} creatures, {wr_pct} avg WR{rec_marker}"
        )

    rec = analysis.recommended_build
    if rec.best_cards:
        lines.append("")
        lines.append("TOP CARDS IN RECOMMENDED BUILD:")
        for card in rec.best_cards[:5]:
            name = card.get("name", "?")
            wr = card.get("gih_wr", 0) or 0
            wr_pct = f"{wr * 100:.1f}%" if wr else "N/A"
            lines.append(f"  - {name} ({wr_pct})")

    if analysis.splash_candidates:
        lines.append("")
        lines.append("SPLASH CANDIDATES:")
        for card in analysis.splash_candidates:
            name = card.get("name", "?")
            colors = card.get("colors", [])
            color_str = "".join(colors)
            wr = card.get("gih_wr", 0) or 0
            lines.append(f"  - {name} [{color_str}] ({wr * 100:.1f}%)")

    return "\n".join(lines)
