"""Mana reachable by sequencing (2026-10-11 turn 10: Emrakul was castable, the planner saw no [OK])."""

from copy import deepcopy

import pytest

from arenamcp.mana_potential import mana_reach_note, turn_mana_potential

HOBBITS = (
    "When The Notary Hobbits enter, if they're not a token, create two tokens that are copies of them, "
    "except the tokens aren't legendary.\n{oT}: Add {oC} for each Halfling you control."
)


def _perm(iid, name, type_line, oracle="", **extra):
    card = {
        "instance_id": iid,
        "grp_id": iid * 10,
        "name": name,
        "type_line": type_line,
        "oracle_text": oracle,
        "owner_seat_id": 1,
        "controller_seat_id": 1,
        "is_tapped": False,
        "turn_entered_battlefield": 2,
        "card_types": [t for t in ("Land", "Creature", "Enchantment") if t.lower() in type_line.lower()],
        "object_kind": "CARD",
        "counters": {},
    }
    card.update(extra)
    return card


def _hand(iid, name, cost, type_line, oracle=""):
    return {
        "instance_id": iid,
        "grp_id": iid * 10,
        "name": name,
        "mana_cost": cost,
        "type_line": type_line,
        "oracle_text": oracle,
        "card_types": [t for t in ("Land", "Creature") if t.lower() in type_line.lower()],
    }


@pytest.fixture
def turn10():
    """Start of our turn 10 after the token copy died: 4 untapped lands, 2 Hobbits, a Spawn, Exploration."""
    return {
        "local_seat_id": 1,
        "turn": {"turn_number": 10, "active_player": 1, "priority_player": 1, "phase": "Main1", "step": ""},
        "players": [{"seat_id": 1, "is_local": True, "life_total": 16, "lands_played": 0, "mana_pool": {}}],
        "stack": [],
        "battlefield": [
            _perm(
                445,
                "Boseiju, Who Endures",
                "Legendary Land",
                "{oT}: Add {oG}.\nChannel — {o1oG}, Discard this card: Destroy target artifact.",
            ),
            _perm(449, "Forest", "Basic Land — Forest", "({T}: Add {G}.)", color_production=["Green"]),
            _perm(482, "Forest", "Basic Land — Forest", "({T}: Add {G}.)", color_production=["Green"]),
            _perm(
                453,
                "Library of Alexandria",
                "Land",
                "{oT}: Add {oC}.\n{oT}: Draw a card. Activate only if you have exactly seven cards in hand.",
            ),
            _perm(
                478,
                "Utopia Sprawl",
                "Enchantment — Aura",
                "Enchant Forest\nAs this Aura enters, choose a color.\nWhenever enchanted Forest is tapped for mana, its controller adds an additional one mana of the chosen color.",
                attached_to_id=449,
            ),
            _perm(
                475, "Exploration", "Enchantment", "You may play an additional land on each of your turns."
            ),
            _perm(
                466,
                "Eldrazi Spawn",
                "Token Creature — Eldrazi Spawn",
                "Sacrifice this token: Add {oC}.",
                power=0,
                toughness=1,
                object_kind="TOKEN",
                turn_entered_battlefield=4,
            ),
            _perm(
                577,
                "The Notary Hobbits",
                "Legendary Creature — Halfling Advisor",
                HOBBITS,
                power=1,
                toughness=1,
                turn_entered_battlefield=8,
            ),
            _perm(
                585,
                "The Notary Hobbits",
                "Legendary Creature — Halfling Advisor",
                HOBBITS,
                power=1,
                toughness=1,
                turn_entered_battlefield=8,
                object_kind="TOKEN",
                is_copy=True,
            ),
        ],
        "hand": [
            _hand(248, "Surrak and Goreclaw", "{4}{G}{G}", "Legendary Creature — Human Bear"),
            _hand(
                452,
                "Emrakul, the World Anew",
                "{12}",
                "Legendary Creature — Eldrazi",
                "When you cast this spell, gain control of all creatures target player controls.",
            ),
            _hand(461, "Vaultborn Tyrant", "{5}{G}{G}", "Creature — Dinosaur"),
            _hand(
                576,
                "Badgermole Cub",
                "{1}{G}",
                "Creature — Badger Mole",
                "When this creature enters, earthbend 1.\nWhenever you tap a creature for mana, add an additional {oG}.",
            ),
            _hand(590, "Forest", "", "Basic Land — Forest", "({T}: Add {G}.)"),
        ],
    }


def test_turn_ten_reaches_thirteen_with_the_cub_first_and_emrakul_is_named(turn10):
    potential = turn_mana_potential(turn10)
    # 4 lands + Sprawl + the Forest drop = 6; two Hobbits tap for 2 each; the Spawn for 1.
    assert potential.base_total == 11
    assert potential.enabler["name"] == "Badgermole Cub"
    assert potential.total_with_enabler == 12  # +2 creature taps +1 earthbent land -2 cost: exactly Emrakul
    note = mana_reach_note(turn10, {452: False, 248: True, 461: True, 576: True})
    assert note.startswith("MANA REACHABLE THIS TURN")
    assert "up to 12 = lands 6 (incl. Play Land: Forest; aura bonus +1)" in note
    assert "creature taps 4 (The Notary Hobbits 2, The Notary Hobbits 2)" in note
    assert "sacrifice Eldrazi Spawn +1" in note
    assert "Badgermole Cub cast first ({1}{G} from land mana): +3" in note
    assert "Unlocks (not [OK] in this window): Emrakul, the World Anew {12}." in note
    assert (
        "Order: Play Land: Forest -> Cast Badgermole Cub (pay with lands) -> earthbend an untapped land"
        in note
    )
    assert note.rstrip().endswith(
        "-> Cast Emrakul, the World Anew. Spending mana on any other spell first forfeits this."
    )


def test_cards_arena_already_marks_payable_need_no_note(turn10):
    turn10["hand"] = [c for c in turn10["hand"] if c["name"] != "Emrakul, the World Anew"]
    assert mana_reach_note(turn10, {248: True, 461: True, 576: True}) == ""


def test_land_drop_alone_can_be_the_gap(turn10):
    """Nine mana now, ten with the Forest in hand: a 7-drop is already [OK]; a {10} spell is unlocked by the land."""
    turn10["hand"] = [
        _hand(900, "Ten Drop", "{10}", "Creature — Giant"),
        _hand(590, "Forest", "", "Basic Land — Forest", "({T}: Add {G}.)"),
    ]
    note = mana_reach_note(turn10, {900: False})
    assert "Unlocks (not [OK] in this window): Ten Drop {10}." in note
    assert (
        "Badgermole" not in note
        and "Order: Play Land: Forest -> tap creatures and sacrifice last -> Cast Ten Drop." in note
    )


def test_no_note_off_our_main_phase_or_when_nothing_is_reachable(turn10):
    away = deepcopy(turn10)
    away["turn"]["active_player"] = 2
    assert turn_mana_potential(away) is None and mana_reach_note(away) == ""
    combat = deepcopy(turn10)
    combat["turn"]["phase"] = "Combat"
    assert mana_reach_note(combat) == ""
    turn10["hand"] = [_hand(901, "Fifteen Drop", "{15}", "Creature — Giant")]
    assert mana_reach_note(turn10, {901: False}) == ""


def test_summoning_sick_creatures_and_tapped_lands_do_not_count(turn10):
    for card in turn10["battlefield"]:
        if card["name"] == "The Notary Hobbits":
            card["turn_entered_battlefield"] = 10
        if card["name"] == "Library of Alexandria":
            card["is_tapped"] = True
    potential = turn_mana_potential(turn10)
    assert potential.base_total == 6  # 3 lands + Sprawl + Forest drop + Spawn
    assert potential.enabler is None  # no creature taps to amplify
