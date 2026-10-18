"""Spoken deck explanations: two sentences grounded in the cards each counted build plays."""

from __future__ import annotations

import re

from arenamcp.limited_deck import (
    _deck_cards,
    deck_choice_summary,
    fallback_deck,
    validate_deck,
)


def card(grp_id, name, cost, *, gih, type_line="Creature — Wizard", text="Vigilance", rarity="common"):
    return {
        "grp_id": grp_id,
        "name": name,
        "mana_cost": cost,
        "type_line": type_line,
        "oracle_text": text,
        "rarity": rarity,
        "gih_wr": gih,
    }


def izzet_pool():
    """Blue flyers and red burn, a deep green creature base, and white chaff."""
    blue = [card(i, f"Sky Drake {i}", "{2}{U}", gih=0.58, text="Flying") for i in range(1, 9)]
    blue += [card(10 + i, f"Tide Adept {i}", "{1}{U}", gih=0.56) for i in range(1, 5)]
    red = [
        card(
            20 + i,
            f"Spark Volley {i}",
            "{1}{R}",
            gih=0.59,
            type_line="Instant",
            text="Spark Volley deals 3 damage to target creature.",
        )
        for i in range(1, 5)
    ]
    red += [card(30 + i, f"Ember Cub {i}", "{1}{R}", gih=0.55) for i in range(1, 7)]
    red.append(
        card(
            40,
            "Undertow",
            "{U}",
            gih=0.57,
            type_line="Instant",
            text="Return target creature to its owner's hand.",
        )
    )
    green = [card(50 + i, f"Grove Bear {i}", "{1}{G}", gih=0.555) for i in range(1, 13)]
    green += [
        card(
            70 + i,
            f"Wild Brawl {i}",
            "{1}{G}",
            gih=0.56,
            type_line="Sorcery",
            text="Target creature you control fights target creature you don't control.",
        )
        for i in range(1, 3)
    ]
    white = [card(80 + i, f"Plain Squire {i}", "{W}", gih=0.49) for i in range(1, 7)]
    return blue + red + green + white


def sentences(text: str) -> list[str]:
    return [part for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part]


def test_each_counted_build_is_legal_and_explained_in_two_grounded_sentences():
    pool = izzet_pool()
    build = fallback_deck(pool)
    options = build["deck_options"]
    assert len(options) >= 2
    for option in options:
        # Every option is itself a validated, castable 40-card build from the pool.
        assert option["total_cards"] == 40 and 15 <= option["land_count"] <= 19
        validate_deck(option, pool, source="heuristic")
        text = deck_choice_summary(option, pool)
        assert len(sentences(text)) == 2, text
        assert text.startswith("I built ") and "Option" not in text
        deck = _deck_cards(option, pool)
        creatures = sum("Creature" in c["type_line"] for c in deck)
        assert f"with {creatures} creature or token spells" in text
        named = re.search(r"led by (.+?), with", text).group(1).split(" and ")
        assert all(name in {c["name"] for c in deck} for name in named)
        # Card statistics are never presented as a deck win rate.
        assert "%" not in text and "win rate" not in text.lower()


def test_interaction_is_split_into_removal_and_tempo_from_rules_text():
    pool = izzet_pool()
    build = fallback_deck(pool)
    izzet = next(o for o in build["deck_options"] if set(o["basic_lands"]) >= {"U", "R"})
    text = deck_choice_summary(izzet, pool)
    assert "4 removal spells and 1 bounce, tap, or counter spell." in text
