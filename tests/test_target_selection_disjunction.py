"""Regression tests for issue #482 — the coach told the player to target an
opponent's creature with Planar Incision when the play was to blink their own
Hei Bai (which is what the player did instead).

From the field report (bug_20260813_205059.json), the coach offered only:
    Select target: Talisman of Unity (YOURS), Select target: Talisman of Progress (YOURS)
while the GRE's real candidates were Hexing Squelcher (OPP), both Talismans,
Hei Bai (YOURS), Bre of Clan Stoutarm (OPP) and Bruse Tarl (OPP) — and the
advice was "Target Bruse Tarl, with Planar Incision".

Two defects:
  1. `_infer_target_requirements` matched only the first type in a chained
     target clause, so "target artifact or creature" dropped every creature.
  2. "Exile ... then return it to the battlefield" was classified as removal,
     so the ranking preferred an opponent's creature — but blinking an
     opponent's creature just hands it back with a +1/+1 counter.
"""

from arenamcp.rules_engine import RulesEngine

PLANAR_INCISION = (
    "Exile target artifact or creature, then return it to the battlefield "
    "under its owner's control with a +1/+1 counter on it."
)


def _board():
    """The reported board. Local player is seat 2."""
    return [
        {
            "instance_id": 469,
            "name": "Hei Bai, Forest Guardian",
            "type_line": "Legendary Creature — Bear Spirit",
            "owner_seat_id": 2,
            "controller_seat_id": 2,
            "power": 4,
            "toughness": 4,
            "oracle_text": "When Hei Bai enters, reveal cards from the top of your library.",
        },
        {
            "instance_id": 455,
            "name": "Talisman of Progress",
            "type_line": "Artifact",
            "owner_seat_id": 2,
            "controller_seat_id": 2,
        },
        {
            "instance_id": 458,
            "name": "Talisman of Unity",
            "type_line": "Artifact",
            "owner_seat_id": 2,
            "controller_seat_id": 2,
        },
        {
            "instance_id": 593,
            "name": "Bruse Tarl, Boorish Herder",
            "type_line": "Legendary Creature — Human Ally",
            "owner_seat_id": 1,
            "controller_seat_id": 1,
            "power": 3,
            "toughness": 3,
        },
        {
            "instance_id": 576,
            "name": "Bre of Clan Stoutarm",
            "type_line": "Legendary Creature — Giant Warrior",
            "owner_seat_id": 1,
            "controller_seat_id": 1,
            "power": 4,
            "toughness": 4,
        },
        {
            "instance_id": 450,
            "name": "Hexing Squelcher",
            "type_line": "Creature — Goblin Sorcerer",
            "owner_seat_id": 1,
            "controller_seat_id": 1,
            "power": 2,
            "toughness": 2,
        },
    ]


def _state(oracle, source_card="Planar Incision"):
    return {
        "battlefield": _board(),
        "local_seat_id": 2,
        "players": [
            {"seat_id": 2, "is_local": True},
            {"seat_id": 1, "is_local": False},
        ],
        "decision_context": {
            "type": "target_selection",
            "source_card": source_card,
            "source_oracle_text": oracle,
            "source_id": 605,
        },
    }


# --- defect 1: chained target types -----------------------------------------


def test_chained_target_types_keeps_every_type():
    types = RulesEngine._infer_target_requirements(PLANAR_INCISION)["types"]
    assert types == {"artifact", "creature"}


def test_chained_target_types_various_phrasings():
    infer = RulesEngine._infer_chained_target_types
    assert infer("destroy target artifact or enchantment") == {"artifact", "enchantment"}
    assert infer("destroy target artifact, creature, or enchantment") == {
        "artifact",
        "creature",
        "enchantment",
    }
    assert infer("destroy target creature or planeswalker") == {"creature", "planeswalker"}


def test_single_type_clause_is_left_alone():
    """A clause with one type must not pick up neighbouring words."""
    infer = RulesEngine._infer_chained_target_types
    assert infer("destroy target creature an opponent controls with power 2 or less") == set()
    assert infer("target opponent discards a card") == set()
    # The dedicated phrase checks still own the single-type case.
    assert RulesEngine._infer_target_requirements("destroy target creature")["types"] == {"creature"}


def test_creatures_are_offered_as_targets_again():
    """The reported bug: only the two Talismans were offered."""
    actions = RulesEngine._get_target_selection_actions(_state(PLANAR_INCISION))
    assert any("Hei Bai" in a for a in actions), actions


# --- defect 2: blink is not removal -----------------------------------------


def test_blink_prefers_your_own_permanent():
    """Blinking your own creature re-triggers its ETB; blinking the
    opponent's just hands it back with a +1/+1 counter."""
    actions = RulesEngine._get_target_selection_actions(_state(PLANAR_INCISION))
    assert actions[0] == "Select target: Hei Bai, Forest Guardian (YOURS)", actions
    assert not any("(OPP)" in a for a in actions), actions


def test_real_removal_still_prefers_the_opponent():
    """The blink carve-out must not disarm actual removal."""
    actions = RulesEngine._get_target_selection_actions(
        _state("Destroy target creature.", source_card="Murder")
    )
    assert actions, actions
    assert "(OPP)" in actions[0], actions


def test_exile_without_return_is_still_removal():
    actions = RulesEngine._get_target_selection_actions(
        _state("Exile target creature.", source_card="Swords to Plowshares")
    )
    assert "(OPP)" in actions[0], actions
