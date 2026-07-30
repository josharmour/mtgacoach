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


# ── New primitive-based roles ──────────────────────────────────────────

HYMN_TO_TOURACH = """\
Name:Hymn to Tourach
ManaCost:B B
Types:Sorcery
A:SP$ Discard | ValidTgts$ Player | NumCards$ 2 | Mode$ Random | SpellDescription$ Target player discards two cards at random.
Oracle:Target player discards two cards at random.
"""

HEALING_SALVE = """\
Name:Healing Salve
ManaCost:W
Types:Instant
A:SP$ Charm | Choices$ DBGainLife,DBPrevent
SVar:DBGainLife:DB$ GainLife | LifeAmount$ 3 | SpellDescription$ You gain 3 life.
SVar:DBPrevent:DB$ PreventDamage | ValidTgts$ Player,Creature | NumDmg$ 3 | SpellDescription$ Prevent the next 3 damage that would be dealt to any target.
Oracle:Choose one —\\n• You gain 3 life.\\n• Prevent the next 3 damage that would be dealt to any target.
"""

ANIMATE_LAND = """\
Name:Animate Land
ManaCost:2 G
Types:Sorcery
A:SP$ Animate | Defined$ Land.YouCtrl | Power$ 3 | Toughness$ 4 | Types$ Creature | SpellDescription$ Target land becomes a 3/4 creature until end of turn.
SVar:PlayMain1:TRUE
Oracle:Target land becomes a 3/4 creature until end of turn.
"""

HIRED_CLAW = """\
Name:Hired Claw
ManaCost:1 R
Types:Creature Lizard Mercenary
PT:2/2
K:First Strike
A:AB$ PutCounter | Cost$ T | ValidTgts$ Creature | CounterType$ P1P1 | TargetMin$ 0 | TargetMax$ 1 | TgtPrompt$ Select target creature | CounterNum$ 1 | SpellDescription$ Put a +1/+1 counter on target creature.
Oracle:First strike\\n{T}: Put a +1/+1 counter on target creature.
"""


class TestNewPrimitiveRoles:
    def test_discard_is_disruption(self):
        """Hymn to Tourach forces opponent discard → DISRUPTION."""
        card, roles, unmatched = _classify(HYMN_TO_TOURACH)
        assert "DISRUPTION" in roles, f"Should be DISRUPTION, got {roles}"
        assert "REMOVAL" not in roles

    def test_discard_primitive(self):
        card, _, _ = _classify(HYMN_TO_TOURACH)
        prims = resolve_primitives(card)
        assert any(p.get("primitive") == "Discard" for p in prims)

    def test_gainlife_is_lifegain(self):
        """Healing Salve gains life via Charm → LIFEGAIN."""
        card, roles, unmatched = _classify(HEALING_SALVE)
        assert "LIFEGAIN" in roles, f"Should be LIFEGAIN, got {roles}"

    def test_lifegain_primitive_resolved(self):
        card, _, _ = _classify(HEALING_SALVE)
        prims = resolve_primitives(card)
        prim_names = {p.get("primitive") for p in prims if p.get("primitive")}
        assert "GainLife" in prim_names, f"GainLife not found: {prim_names}"

    def test_animate_is_animation(self):
        """Animate Land animates → ANIMATION."""
        card, roles, unmatched = _classify(ANIMATE_LAND)
        assert "ANIMATION" in roles, f"Should be ANIMATION, got {roles}"

    def test_putcounter_is_growth(self):
        """Hired Claw puts +1/+1 counters → GROWTH."""
        card, roles, unmatched = _classify(HIRED_CLAW)
        assert "GROWTH" in roles, f"Should be GROWTH, got {roles}"

    def test_hired_claw_creature_body(self):
        """Hired Claw is a 2/2 First Strike — not big enough for ATTACKER."""
        card, roles, unmatched = _classify(HIRED_CLAW)
        # 2/2 and First Strike — no creature-body roles
        assert "ATTACKER" not in roles
        assert "BLOCKER" not in roles
        assert "EVASION" not in roles


# ── Creature-body heuristic ───────────────────────────────────────────

BIG_CREATURE = """\
Name:Colossal Dreadmaw
ManaCost:4 G G
Types:Creature Dinosaur
PT:6/6
K:Trample
Oracle:Trample
"""

FLYING_CREATURE = """\
Name:Wind Drake
ManaCost:2 U
Types:Creature Drake
PT:2/2
K:Flying
Oracle:Flying
"""

DEFENSIVE_CREATURE = """\
Name:Wall of Omens
ManaCost:1 W
Types:Creature Wall
PT:0/4
K:Defender
Oracle:Defender\\nWhen Wall of Omens enters, draw a card.
"""

VIGILANCE_CREATURE = """\
Name: Vigilant Griffin
ManaCost:2 W
Types:Creature Griffin
PT:2/2
K:Flying, Vigilance
Oracle:Flying, vigilance
"""


class TestCreatureBodyHeuristic:
    def test_big_creature_is_attacker(self):
        """Colossal Dreadmaw 6/6 Trample → ATTACKER + EVASION."""
        card, roles, unmatched = _classify(BIG_CREATURE)
        assert "ATTACKER" in roles, f"Should be ATTACKER, got {roles}"
        assert "EVASION" in roles, f"Should be EVASION (trample), got {roles}"

    def test_flying_creature_evasion(self):
        """Wind Drake 2/2 Flying → EVASION."""
        card, roles, unmatched = _classify(FLYING_CREATURE)
        assert "EVASION" in roles, f"Should be EVASION, got {roles}"

    def test_defensive_creature_blocker(self):
        """Wall of Omens 0/4 → BLOCKER."""
        card, roles, unmatched = _classify(DEFENSIVE_CREATURE)
        assert "BLOCKER" in roles, f"Should be BLOCKER, got {roles}"

    def test_vigilance_gives_blocker(self):
        """Vigilant Griffin 2/2 with Vigilance → BLOCKER + EVASION."""
        card, roles, unmatched = _classify(VIGILANCE_CREATURE)
        assert "BLOCKER" in roles, f"Vigilance should give BLOCKER, got {roles}"
        assert "EVASION" in roles, f"Flying should give EVASION, got {roles}"
        # 2/2 — power < 3, not ATTACKER
        assert "ATTACKER" not in roles

    def test_vanilla_bear_has_no_body_roles(self):
        """Runeclaw Bear 2/2 — not big, not evasive, not defensive → no body roles."""
        card, roles, unmatched = _classify(VANILLA_CREATURE)
        assert not roles, f"Runeclaw Bear should have no roles, got {roles}"


# ── Previously-unmapped rotation cards ────────────────────────────────

ADELINE_CATHAR = """\
Name:Adeline, Resplendent Cathar
ManaCost:1 W W
Types:Legendary Creature Human Knight
PT:*/4
K:Vigilance
S:Mode$ Continuous | CharacteristicDefining$ True | SetPower$ X | Description$ CARDNAME's power is equal to the number of creatures you control.
SVar:X:Count$Valid Creature.YouCtrl
T:Mode$ AttackersDeclared | AttackingPlayer$ You | Execute$ DBRepeat | TriggerZones$ Battlefield | TriggerDescription$ Whenever you attack, for each opponent, create a 1/1 white Human creature token that's tapped and attacking that player or a planeswalker they control.
SVar:DBRepeat:DB$ RepeatEach | RepeatPlayers$ Opponent | ChangeZoneTable$ True | RepeatSubAbility$ DBToken
SVar:DBToken:DB$ Token | TokenScript$ w_1_1_human | TokenTapped$ True | TokenAttacking$ RememberedPlayer & Valid Planeswalker.ControlledBy Remembered | TokenOwner$ You
DeckHas:Ability$Token
Oracle:Vigilance\\nAdeline, Resplendent Cathar's power is equal to the number of creatures you control.\\nWhenever you attack, for each opponent, create a 1/1 white Human creature token that's tapped and attacking that player or a planeswalker they control.
"""

HAUGHTY_DJINN = """\
Name:Haughty Djinn
ManaCost:1 U U
Types:Creature Djinn
PT:3/3
K:Flying
S:Mode$ Continuous | Affected$ Card.Self | AddSVar$ StolentDEX | Description$ CARDNAME's power is equal to the number of instant and sorcery cards in your graveyard.
SVar:StolentDEX:Count$Valid Graveyard YouOwn$Card.Instant,Sorcery
A:AB$ Dig | Cost$ U | DigNum$ 4 | ChangeNum$ 1 | DestinationZone$ Hand | DestinationZone2$ Graveyard | SpellDescription$ Look at the top four cards of your library. You may reveal an instant or sorcery card from among them and put it into your hand. Put the rest on the bottom of your library in a random order. Activate only as a sorcery.
Oracle:Flying\\nHaughty Djinn's power is equal to the number of instant and sorcery cards in your graveyard.\\n{U}: Look at the top four cards of your library. You may reveal an instant or sorcery card from among them and put it into your hand. Put the rest on the bottom of your library in a random order.
"""

HULLBREAKER_HORROR = """\
Name:Hullbreaker Horror
ManaCost:3 U U U
Types:Creature Kraken Horror
PT:7/8
K:Flash
K:Trample
A:AB$ ChangeZone | Cost$ 1 U | Origin$ Battlefield | Destination$ Hand | ValidTgts$ Card.nonLand+OppCtrl | TgtPrompt$ Select target nonland permanent an opponent controls | SpellDescription$ Return target nonland permanent an opponent controls to its owner's hand. Activate only as a sorcery.
T:Mode$ SpellCast | ValidCard$ Card | ValidActivatingPlayer$ Opponent | Execute$ TrigBounce | TriggerZones$ Battlefield | TriggerDescription$ Whenever you cast a spell, counter up to one target spell.
SVar:TrigBounce:DB$ ChangeZone | Origin$ Battlefield | Destination$ Hand | ValidTgts$ Permanent.nonLand+OppCtrl | TgtPrompt$ Select target nonland permanent an opponent controls
Oracle:Flash\\nTrample\\nWhenever you cast a spell, counter up to one target spell.\\n{1}{U}: Return target nonland permanent an opponent controls to its owner's hand. Activate only as a sorcery.
"""


class TestPreviouslyUnmapped:
    """Coverage for cards that were at 0 roles before the taxonomy expand."""

    def test_adeline_blocker_from_vigilance(self):
        """Adeline has Vigilance → BLOCKER (was previously unmapped)."""
        card, roles, unmatched = _classify(ADELINE_CATHAR)
        assert "BLOCKER" in roles, f"Adeline should be BLOCKER (vigilance), got {roles}"

    def test_haughty_djinn_evasion_and_dig(self):
        """Haughty Djinn 3/3 Flying → ATTACKER + EVASION + DRAW."""
        card, roles, unmatched = _classify(HAUGHTY_DJINN)
        assert "DRAW" in roles, f"Haughty Djinn should be DRAW (Dig), got {roles}"
        # Body heuristic always applies — 3/3 Flying → ATTACKER + EVASION
        assert "EVASION" in roles, f"Should get EVASION from Flying, got {roles}"
        assert "ATTACKER" in roles, f"Should get ATTACKER (power 3), got {roles}"

    def test_hullbreaker_horror_multirole(self):
        """Hullbreaker Horror 7/8 Trample Flash → ATTACKER + BLOCKER + EVASION + REMOVAL."""
        card, roles, unmatched = _classify(HULLBREAKER_HORROR)
        assert "REMOVAL" in roles, f"Should be REMOVAL (bounce), got {roles}"
        assert "ATTACKER" in roles, f"Should be ATTACKER (7 power), got {roles}"
        assert "EVASION" in roles, f"Should be EVASION (trample), got {roles}"
        assert "BLOCKER" in roles, f"Should be BLOCKER (8 > 7), got {roles}"


# ── Direct classify_creature_body unit tests ──────────────────────────

SMALL_NONCREATURE = """\
Name:Not a Creature
ManaCost:1
Types:Instant
Oracle:Not a creature.
"""


class TestClassifyCreatureBody:
    """Direct tests of classify_creature_body."""

    def test_noncreature_no_body_roles(self):
        card = parse_card_script(SMALL_NONCREATURE)
        from build_taxonomy import classify_creature_body

        roles = classify_creature_body(card)
        assert not roles, f"Non-creature should have no body roles, got {roles}"

    def test_attacker_threshold(self):
        card = parse_card_script(HULLBREAKER_HORROR)
        from build_taxonomy import classify_creature_body

        roles = classify_creature_body(card)
        assert "ATTACKER" in roles, f"7 power should be ATTACKER, got {roles}"
        assert "BLOCKER" in roles, f"8 toughness > 7 power should be BLOCKER, got {roles}"
        assert "EVASION" in roles, f"Trample should give EVASION, got {roles}"

    def test_body_plus_primitives_combined(self):
        """Hullbreaker Horror gets BOTH primitive and body roles."""
        card, roles, unmatched = _classify(HULLBREAKER_HORROR)
        assert "REMOVAL" in roles, f"Should be REMOVAL, got {roles}"
        assert "ATTACKER" in roles, f"Should be ATTACKER (7 power), got {roles}"
        assert "BLOCKER" in roles, f"Should be BLOCKER (8 > 7), got {roles}"
        assert "EVASION" in roles, f"Should be EVASION (trample), got {roles}"
        assert len(roles) >= 4, f"Should have 4+ roles (ATTACKER+EVASION+BLOCKER+REMOVAL), got {roles}"
