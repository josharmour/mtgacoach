"""Sealed pool analysis (sealed_eval.py): the live Sealed coaching path.

standalone.py asks server.get_sealed_pool, which runs analyze_sealed_pool on
the opened pool and speaks format_sealed_recommendation (and shows
format_sealed_detailed). A small synthetic pool: deep blue-red with a gold
card, a black bomb to splash, thin weak green and one colorless artifact.
"""

from __future__ import annotations

from arenamcp.sealed_eval import analyze_sealed_pool, format_sealed_detailed, format_sealed_recommendation


def card(name, colors, gih, type_line="Creature — Wizard"):
    return {"name": name, "colors": list(colors), "gih_wr": gih, "type_line": type_line}


POOL = [
    card("Sky Drake", "U", 0.58),
    card("Sky Drake", "U", 0.58),  # a second copy: one name per pair
    card("Tide Adept", "U", 0.56),
    card("Mind Twist Spell", "U", 0.54, type_line="Instant"),
    card("Ember Cub", "R", 0.55),
    card("Firebolt", "R", 0.57, type_line="Sorcery"),
    card("Forge Pup", "R", 0.49),
    card("Izzet Charm", "UR", 0.61, type_line="Instant"),
    card("Grave Tyrant", "B", 0.63),
    card("Bog Rat", "B", 0.47),
    card("Grove Bear", "G", 0.48),
    card("Moss Elk", "G", 0.46),
    card("Iron Golem", "", 0.52, type_line="Artifact Creature — Golem"),
]


def test_recommends_the_deepest_pair_and_splashes_the_off_color_bomb():
    analysis = analyze_sealed_pool(POOL, "TST")
    rec = analysis.recommended_build
    assert rec.colors == ("U", "R")
    # Unique names in U/R plus the colorless artifact: 8 cards, 7 above 50%, 5 creatures.
    assert (rec.card_count, rec.playable_count, rec.creature_count) == (8, 7, 5)
    assert [c["name"] for c in rec.best_cards[:3]] == ["Izzet Charm", "Sky Drake", "Firebolt"]
    # Off-color and above 55%: only the bomb (Bog Rat is too weak to splash).
    assert [c["name"] for c in analysis.splash_candidates] == ["Grave Tyrant"]
    assert analysis.top_cards[0]["name"] == "Grave Tyrant" and analysis.total_cards == len(POOL)
    assert len(analysis.color_analyses) == 5
    ranking = [(a.playable_count, a.avg_win_rate) for a in analysis.color_analyses]
    assert ranking == sorted(ranking, reverse=True)


def test_spoken_recommendation_names_colors_key_cards_splash_and_an_alternative():
    text = format_sealed_recommendation(analyze_sealed_pool(POOL, "TST"))
    assert text.startswith("Build Blue/Red. 7 playables, 5 creatures, ")
    assert "Key cards: Izzet Charm, Sky Drake, Firebolt." in text
    assert "Consider splashing Grave Tyrant at 63% win rate." in text
    assert "Alternative: " in text


def test_detailed_breakdown_marks_the_recommended_pair_and_lists_splashes():
    lines = format_sealed_detailed(analyze_sealed_pool(POOL, "TST")).splitlines()
    assert lines[0] == f"=== SEALED POOL ANALYSIS ({len(POOL)} cards) ==="
    assert lines[3].startswith("  1. Blue/Red: 7 playables, 5 creatures") and lines[3].endswith(
        "<< RECOMMENDED"
    )
    assert "  - Grave Tyrant [B] (63.0%)" in lines
    assert "  - Izzet Charm (61.0%)" in lines


def test_an_empty_pool_says_it_cannot_analyze():
    analysis = analyze_sealed_pool([], "TST")
    assert analysis.total_cards == 0 and analysis.recommended_build.colors == ()
    assert format_sealed_recommendation(analysis) == "Unable to analyze pool - no card data available."
    assert format_sealed_detailed(analysis) == "Unable to analyze pool."
