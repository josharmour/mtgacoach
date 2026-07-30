"""Tests for wp3-taxonomy: Forge card script parsing and role classification."""

import os
import sys

# Ensure tools/training/taxonomy is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "training", "taxonomy"))

from annotate import annotate_menu_row
from build_taxonomy import (
    classify_card,
    parse_card_script,
    resolve_primitives,
)

# ── Fixture: known Forge card scripts ──────────────────────────────────

DOOM_BLADE = """\
Name:Doom Blade
ManaCost:1 B
Types:Instant
A:SP$ Destroy | ValidTgts$ Creature.nonBlack | TgtPrompt$ Select target nonblack creature | SpellDescription$ Destroy target nonblack creature.
Oracle:Destroy target nonblack creature.
"""

COUNTERSPELL = """\
Name:Counterspell
ManaCost:U U
Types:Instant
A:SP$ Counter | TargetType$ Spell | ValidTgts$ Card | SpellDescription$ Counter target spell.
Oracle:Counter target spell.
"""

DEMONIC_TUTOR = """\
Name:Demonic Tutor
ManaCost:1 B
Types:Sorcery
A:SP$ ChangeZone | Origin$ Library | Destination$ Hand | ChangeType$ Card | ChangeNum$ 1 | Mandatory$ True | StackDescription$ SpellDescription | SpellDescription$ Search your library for a card, put that card into your hand, then shuffle.
Oracle:Search your library for a card, put that card into your hand, then shuffle.
"""

LIGHTNING_HELIX = """\
Name:Lightning Helix
ManaCost:R W
Types:Instant
A:SP$ DealDamage | ValidTgts$ Any | NumDmg$ 3 | SubAbility$ DBGainLife | SpellDescription$ CARDNAME deals 3 damage to any target and you gain 3 life.
SVar:DBGainLife:DB$ GainLife | LifeAmount$ 3
Oracle:Lightning Helix deals 3 damage to any target and you gain 3 life.
"""

VANILLA_CREATURE = """\
Name:Runeclaw Bear
ManaCost:1 G
Types:Creature Bear
PT:2/2
Oracle:A bear with sharp claws. Not much else.
"""

ABZAN_CHARM = """\
Name:Abzan Charm
ManaCost:W B G
Types:Instant
A:SP$ Charm | Choices$ DBExile,DBDraw,DBCounters
SVar:DBExile:DB$ ChangeZone | ValidTgts$ Creature.powerGE3 | TgtPrompt$ Choose target creature with power 3 or greater | Origin$ Battlefield | Destination$ Exile | SpellDescription$ Exile target creature with power 3 or greater.
SVar:DBDraw:DB$ Draw | NumCards$ 2 | SubAbility$ DBLoseLife | SpellDescription$ You draw two cards and you lose 2 life.
SVar:DBCounters:DB$ PutCounter | ValidTgts$ Creature | CounterType$ P1P1 | CounterNum$ 2 | TargetMin$ 1 | TargetMax$ 2 | DividedAsYouChoose$ 2 | SpellDescription$ Distribute two +1/+1 counters among one or two target creatures.
SVar:DBLoseLife:DB$ LoseLife | LifeAmount$ 2 | Defined$ You | StackDescription$ None
Oracle:Choose one —\n• Exile target creature with power 3 or greater.\n• You draw two cards and you lose 2 life.\n• Distribute two +1/+1 counters among one or two target creatures.
"""

MANA_LEAK = """\
Name:Mana Leak
ManaCost:1 U
Types:Instant
A:SP$ Counter | TargetType$ Spell | TgtPrompt$ Select target spell | ValidTgts$ Card | UnlessCost$ 3 | SpellDescription$ Counter target spell unless its controller pays {3}.
Oracle:Counter target spell unless its controller pays {3}.
"""

BOUNCE_OFF = """\
Name:Bounce Off
ManaCost:U
Types:Instant
A:SP$ ChangeZone | ValidTgts$ Creature,Vehicle | TgtPrompt$ Select target creature or Vehicle | Origin$ Battlefield | Destination$ Hand | SpellDescription$ Return target creature or Vehicle to its owner's hand.
Oracle:Return target creature or Vehicle to its owner's hand.
"""

DESTROY_EVIL = """\
Name:Destroy Evil
ManaCost:1 W
Types:Instant
A:SP$ Charm | Choices$ DBDestroyCreature,DBDestroyEnchantment
SVar:DBDestroyCreature:DB$ Destroy | ValidTgts$ Creature.toughnessGE4 | TgtPrompt$ Select target creature with toughness 4 or greater | SpellDescription$ Destroy target creature with toughness 4 or greater.
SVar:DBDestroyEnchantment:DB$ Destroy | ValidTgts$ Enchantment | SpellDescription$ Destroy target enchantment.
Oracle:Choose one —\n• Destroy target creature with toughness 4 or greater.\n• Destroy target enchantment.
"""

DFC_CARD = """\
Name:Unholy Annex
ManaCost:2 B
Types:Enchantment Room
T:Mode$ Phase | Phase$ End of Turn | ValidPlayer$ You | TriggerZones$ Battlefield | Execute$ TrigDraw | TriggerDescription$ At the beginning of your end step, draw a card.
SVar:TrigDraw:DB$ Draw | SubAbility$ DBBranch
AlternateMode:Split

ALTERNATE

Name:Ritual Chamber
ManaCost:3 B B
Types:Enchantment Room
T:Mode$ UnlockDoor | ValidPlayer$ You | ValidCard$ Card.Self | ThisDoor$ True | Execute$ TrigToken
SVar:TrigToken:DB$ Token | TokenScript$ b_6_6_demon_flying | TokenOwner$ You
"""


# ── Tests ──────────────────────────────────────────────────────────────


def _classify(text):
    """Helper: parse + classify a card script."""
    card = parse_card_script(text)
    roles, unmatched = classify_card(card)
    return card, roles, unmatched


class TestDoomBlade:
    def test_removal_role(self):
        card, roles, unmatched = _classify(DOOM_BLADE)
        assert "REMOVAL" in roles, f"Doom Blade should be REMOVAL, got {roles}"
        assert "Destroy" in card["primitives"][0]["primitive"]

    def test_no_extra_roles(self):
        card, roles, unmatched = _classify(DOOM_BLADE)
        assert len(roles) == 1, f"Doom Blade should have 1 role, got {roles}"

    def test_annotate(self):
        taxonomy = {
            "Doom Blade": {
                "roles": ["REMOVAL"],
                "primitives": ["Destroy"],
                "types": "Instant",
                "mana_cost": "1 B",
            }
        }
        result = annotate_menu_row("Doom Blade", taxonomy)
        assert "Doom Blade" in result
        assert "REMOVAL" in result

    def test_meta(self):
        card, _, _ = _classify(DOOM_BLADE)
        assert card["name"] == "Doom Blade"
        assert card["mana_cost"] == "1 B"
        assert card["types"] == "Instant"


class TestCounterspell:
    def test_counter_role(self):
        card, roles, unmatched = _classify(COUNTERSPELL)
        assert "COUNTER" in roles, f"Counterspell should be COUNTER, got {roles}"

    def test_multi_cost(self):
        card, _, _ = _classify(COUNTERSPELL)
        assert card["mana_cost"] == "U U"

    def test_annotate(self):
        taxonomy = {
            "Counterspell": {
                "roles": ["COUNTER"],
                "primitives": ["Counter"],
                "types": "Instant",
                "mana_cost": "U U",
            }
        }
        result = annotate_menu_row("Counterspell", taxonomy)
        assert "COUNTER" in result
        assert "counter target spell" in result.lower()


class TestDemonicTutor:
    def test_tutor_role(self):
        card, roles, unmatched = _classify(DEMONIC_TUTOR)
        assert "TUTOR" in roles, f"Demonic Tutor should be TUTOR, got {roles}"

    def test_primitives(self):
        card, _, _ = _classify(DEMONIC_TUTOR)
        prims = resolve_primitives(card)
        prim_names = [p["primitive"] for p in prims if p.get("primitive")]
        assert "ChangeZone" in prim_names


class TestMultiRole:
    def test_lightning_helix(self):
        """Lightning Helix deals damage AND gains life — REMOVAL via DealDamage."""
        card, roles, unmatched = _classify(LIGHTNING_HELIX)
        assert "REMOVAL" in roles, f"Helix should be REMOVAL, got {roles}"

    def test_helix_subability(self):
        """Verify sub-abilities are resolved correctly."""
        card, _, _ = _classify(LIGHTNING_HELIX)
        prims = resolve_primitives(card)
        prim_names = {p.get("primitive") for p in prims if p.get("primitive")}
        assert "DealDamage" in prim_names, f"Helix should have DealDamage, got {prim_names}"
        assert "GainLife" in prim_names, f"Helix should have GainLife (sub-ability), got {prim_names}"

    def test_abzan_charm(self):
        """Charm with multiple modes including exile (REMOVAL) and draw."""
        card, roles, unmatched = _classify(ABZAN_CHARM)
        assert "REMOVAL" in roles, f"Abzan Charm should be REMOVAL via exile mode, got {roles}"
        assert "DRAW" in roles, f"Abzan Charm should be DRAW via draw mode, got {roles}"

    def test_charm_primitives_resolved(self):
        """Verify Charm choices are resolved into individual primitives."""
        card, _, _ = _classify(ABZAN_CHARM)
        prims = resolve_primitives(card)
        prim_names = {p.get("primitive") for p in prims if p.get("primitive")}
        assert "ChangeZone" in prim_names, f"Exile mode ChangeZone not found: {prim_names}"
        assert "Draw" in prim_names, f"Draw mode not found: {prim_names}"
        assert "PutCounter" in prim_names, f"PutCounter mode not found: {prim_names}"
        assert "LoseLife" in prim_names, f"LoseLife sub-sub-ability not found: {prim_names}"

    def test_destroy_evil(self):
        """Charm with only Destroy modes = REMOVAL."""
        card, roles, unmatched = _classify(DESTROY_EVIL)
        assert "REMOVAL" in roles, f"Destroy Evil should be REMOVAL, got {roles}"
        assert len(roles) == 1, f"Destroy Evil should have 1 role, got {roles}"


class TestUnmapped:
    def test_vanilla_creature(self):
        """Vanilla creature with no A: lines should have empty roles."""
        card, roles, unmatched = _classify(VANILLA_CREATURE)
        assert not roles, f"Vanilla creature should have 0 roles, got {roles}"

    def test_vanilla_no_primitives(self):
        """Vanilla creature should have empty primitives list."""
        card, _, _ = _classify(VANILLA_CREATURE)
        assert not card["primitives"], f"Vanilla creature should have no primitives, got {card['primitives']}"


class TestAnnotate:
    def test_known_card(self):
        taxonomy = {
            "Doom Blade": {
                "roles": ["REMOVAL"],
                "primitives": ["Destroy"],
                "types": "Instant",
                "mana_cost": "1 B",
            }
        }
        result = annotate_menu_row("Doom Blade", taxonomy)
        assert "Cast" in result
        assert "Doom Blade" in result

    def test_unknown_card(self):
        result = annotate_menu_row("Fake Card Name", {})
        assert result == "Fake Card Name"

    def test_no_role_card(self):
        taxonomy = {
            "Runeclaw Bear": {"roles": [], "primitives": [], "types": "Creature Bear", "mana_cost": "1 G"}
        }
        result = annotate_menu_row("Runeclaw Bear", taxonomy)
        assert result == "Runeclaw Bear"

    def test_multi_role_annotate(self):
        taxonomy = {
            "Abzan Charm": {
                "roles": ["DRAW", "REMOVAL"],
                "primitives": ["ChangeZone", "Draw", "LoseLife", "PutCounter"],
                "types": "Instant",
                "mana_cost": "W B G",
            }
        }
        result = annotate_menu_row("Abzan Charm", taxonomy)
        assert "DRAW" in result or "REMOVAL" in result
        assert "Abzan Charm" in result
        assert "Cast" in result


class TestManaLeak:
    def test_counter_role(self):
        card, roles, unmatched = _classify(MANA_LEAK)
        assert "COUNTER" in roles, f"Mana Leak should be COUNTER, got {roles}"

    def test_unless_cost(self):
        """Mana Leak has UnlessCost — still a counter."""
        card, _, _ = _classify(MANA_LEAK)
        prims = resolve_primitives(card)
        assert any(p.get("UnlessCost") == "3" for p in prims if p.get("primitive") == "Counter")


class TestBounce:
    def test_bounce_is_removal(self):
        """Bounce Off returns creature to hand — should be REMOVAL."""
        card, roles, unmatched = _classify(BOUNCE_OFF)
        assert "REMOVAL" in roles, f"Bounce Off should be REMOVAL, got {roles}"

    def test_bounce_primitives(self):
        card, _, _ = _classify(BOUNCE_OFF)
        prims = resolve_primitives(card)
        bounce = [p for p in prims if p.get("primitive") == "ChangeZone" and p.get("Destination") == "Hand"]
        assert bounce, "Bounce Off should have ChangeZone to Hand primitive"


class TestDFC:
    def test_dfc_names_collected(self):
        """DFC cards should collect all Name: lines."""
        card = parse_card_script(DFC_CARD)
        assert len(card["all_names"]) == 2
        assert "Unholy Annex" in card["all_names"]
        assert "Ritual Chamber" in card["all_names"]

    def test_dfc_first_name(self):
        """DFC's 'name' should be the first (front face) name."""
        card = parse_card_script(DFC_CARD)
        assert card["name"] == "Unholy Annex"

    def test_dfc_no_a_lines(self):
        """DFC with only T: lines (triggered) should have no A: primitives."""
        card = parse_card_script(DFC_CARD)
        assert not card["primitives"], f"Expected no A: primitives, got {card['primitives']}"


class TestParseEdgeCases:
    def test_empty_script(self):
        card = parse_card_script("")
        assert card["name"] == ""

    def test_basename(self):
        card = parse_card_script("Name:Test\nManaCost:R\nTypes:Instant\nA:SP$ DealDamage")
        assert card["name"] == "Test"
        assert card["mana_cost"] == "R"
        assert card["types"] == "Instant"

    def test_comments_ignored(self):
        card = parse_card_script(
            "Name:Test\n# This is a comment\nManaCost:G\n# Another comment\nTypes:Sorcery"
        )
        assert card["name"] == "Test"
        assert card["mana_cost"] == "G"
        assert card["types"] == "Sorcery"


class TestManaLeakAnnotation:
    def test_annotate_with_taxonomy(self):
        """Integration test: build taxonomy dict and annotate."""
        taxonomy = {}
        for name, script in [
            ("Doom Blade", DOOM_BLADE),
            ("Counterspell", COUNTERSPELL),
            ("Mana Leak", MANA_LEAK),
        ]:
            card, roles, _ = _classify(script)
            taxonomy[name] = {
                "roles": sorted(roles),
                "primitives": sorted(
                    set(p.get("primitive", "") for p in resolve_primitives(card) if p.get("primitive"))
                ),
                "types": card["types"],
                "mana_cost": card["mana_cost"],
            }

        assert annotate_menu_row("Doom Blade", taxonomy) != "Doom Blade"
        assert annotate_menu_row("Counterspell", taxonomy) != "Counterspell"
        assert annotate_menu_row("Mana Leak", taxonomy) != "Mana Leak"
        assert annotate_menu_row("Unknown", taxonomy) == "Unknown"
