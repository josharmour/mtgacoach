"""Tests for 3-tiered deck builder, wildcard inventories, and craft cost calculations."""

import pytest
from arenamcp.deck_builder import (
    CraftCost,
    DeckBuilderV2,
    TieredDeckSuggestion,
    WildcardInventory,
)


def test_wildcard_inventory_from_dict():
    """Test flex parsing of WildcardInventory from various dict formats."""
    # Standard lowercase
    inv1 = WildcardInventory.from_dict({"common": 10, "uncommon": 5, "rare": 2, "mythic": 1})
    assert inv1.common == 10
    assert inv1.uncommon == 5
    assert inv1.rare == 2
    assert inv1.mythic == 1

    # MTGA PlayerInventory style capitalized
    inv2 = WildcardInventory.from_dict({"wcCommon": 12, "wcUncommon": 8, "wcRare": 4, "wcMythic": 2})
    assert inv2.common == 12
    assert inv2.uncommon == 8
    assert inv2.rare == 4
    assert inv2.mythic == 2

    # Empty or None
    inv3 = WildcardInventory.from_dict(None)
    assert inv3.total == 0 if hasattr(inv3, "total") else (inv3.common + inv3.uncommon + inv3.rare + inv3.mythic) == 0


def test_craft_cost_calculation():
    """Test calculating craft cost for a deck against an owned card collection."""
    builder = DeckBuilderV2()

    deck_cards = {
        "Slickshot Show-Off": 4,
        "Monastery Swiftspear": 4,
        "Shock": 4,
        "Mountain": 20,
    }
    custom_rarities = {
        "Slickshot Show-Off": "rare",
        "Monastery Swiftspear": "uncommon",
        "Shock": "common",
    }

    # Player owns 2 Slickshots, 4 Swiftspears, 4 Shocks
    player_collection = {
        "Slickshot Show-Off": 2,
        "Monastery Swiftspear": 4,
        "Shock": 4,
    }

    cost = builder.calculate_craft_cost(deck_cards, player_collection, custom_rarities)
    assert cost.rare == 2  # Missing 2 Slickshot Show-Off
    assert cost.uncommon == 0
    assert cost.common == 0
    assert cost.mythic == 0
    assert cost.total == 2


def test_craft_cost_fits_in_inventory():
    """Test CraftCost.fits_in comparison logic."""
    cost = CraftCost(common=4, uncommon=2, rare=2, mythic=1)
    inv_sufficient = WildcardInventory(common=5, uncommon=5, rare=2, mythic=1)
    inv_insufficient = WildcardInventory(common=5, uncommon=5, rare=1, mythic=1)

    assert cost.fits_in(inv_sufficient) is True
    assert cost.fits_in(inv_insufficient) is False


def test_suggest_tiered_decks():
    """Test 3-tiered deck suggestions for an event format (Standard)."""
    builder = DeckBuilderV2()

    # User owns all cards for Mono Red Aggro EXCEPT 2 Slickshot Show-Off
    player_collection = {
        "Monastery Swiftspear": 4,
        "Slickshot Show-Off": 2,
        "Emberheart Challenger": 4,
        "Hired Claw": 4,
        "Lightning Strike": 4,
        "Shock": 4,
        "Monstrous Rage": 4,
        "Demonic Ruckus": 4,
        "Rockface Village": 4,
        "Urabrask's Forge": 3,
        "Torch the Tower": 4,
    }
    wildcards = {"rare": 5, "uncommon": 10, "common": 10, "mythic": 2}

    tiered = builder.suggest_tiered_decks(
        player_cards=player_collection,
        wildcards=wildcards,
        format_name="standard",
    )

    assert "0-Wildcard" in tiered
    assert "Budget Crafting" in tiered
    assert "Meta Top-Tier" in tiered

    # Budget Crafting should include Mono Red Aggro since missing 2 Rares fits in 5 Rare wildcards
    budget_decks = tiered["Budget Crafting"]
    assert len(budget_decks) > 0
    mono_red_budget = next((d for d in budget_decks if "Mono Red" in d.archetype), None)
    assert mono_red_budget is not None
    assert mono_red_budget.craft_cost.rare == 2
    assert mono_red_budget.fits_inventory is True

    # Meta Top-Tier should contain all format meta decks
    meta_decks = tiered["Meta Top-Tier"]
    assert len(meta_decks) >= 3
    assert any("Azorius" in d.archetype for d in meta_decks)
    assert any("Golgari" in d.archetype for d in meta_decks)
