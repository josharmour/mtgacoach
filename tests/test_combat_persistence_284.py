"""#270/#307: retain the engine, lose the creature; no forced-block policy.

Innocence facts are from gameplay-ultra-audit.json G03, d1bad78e T7:
Innocence275 2/1 was allowed to block Kaito247 4/5 at 16 life. The
original full packet is no longer present; this reconstructs the recorded
combat facts, not hidden responses or a claim that the match was winnable.
"""

from copy import deepcopy

import pytest

from arenamcp.combat_solver import _resolve_attacker
from arenamcp.death_triggers import death_trigger_notes
from arenamcp.persistent_returns import returned_enchantment

INNOCENCE = (
    "Lifelink\nWhenever one or more other creatures you control with power 2 or less enter, "
    "draw a card. This ability triggers only once each turn.\n"
    "When Enduring Innocence dies, if it was a creature, return it to the battlefield "
    "under its owner's control. It's an enchantment. (It's not a creature.)"
)


def innocence(**changes):
    return {
        "instance_id": 275,
        "name": "Enduring Innocence",
        "oracle_text": INNOCENCE,
        "type_line": "Enchantment Creature — Sheep Glimmer",
        "card_types": ["CardType_Enchantment", "CardType_Creature"],
        "subtypes": ["Sheep", "Glimmer"],
        "power": 2,
        "toughness": 1,
        "owner_seat_id": 2,
        "controller_seat_id": 2,
        **changes,
    }


def kaito(**changes):
    return {
        "instance_id": 247,
        "name": "Kaito, Bane of Nightmares",
        "type_line": "Creature — Ninja",
        "power": 4,
        "toughness": 5,
        "controller_seat_id": 1,
        "is_attacking": True,
        **changes,
    }


def test_recorded_innocence_block_prevents_four_gains_two_and_keeps_draw_engine():
    attacker, blocker = kaito(), innocence()
    fight = _resolve_attacker(attacker, [blocker])
    assert fight.damage_through == 0 and fight.blocker_life_gained == 2
    assert fight.blockers_died == [blocker] and not fight.attacker_died
    engine = returned_enchantment(fight.blockers_died[0])
    assert engine["card_types"] == ["CardType_Enchantment"]
    assert engine["power"] is None and "draw a card" in engine["oracle_text"]
    assert 16 - fight.damage_through + fight.blocker_life_gained == 18
    assert 16 - _resolve_attacker(attacker, []).damage_through == 12


def test_first_strike_does_not_invent_innocence_lifelink():
    fight = _resolve_attacker(kaito(oracle_text="First strike"), [innocence()])
    assert fight.blocker_life_gained == 0
    assert returned_enchantment(fight.blockers_died[0]) is not None


def test_return_is_fresh_and_owner_controlled_without_mutating_snapshot():
    old = innocence(
        controller_seat_id=1,
        counters={"+1/+1": 3},
        damage=1,
        is_tapped=True,
        is_attacking=True,
        is_blocking=True,
        attachments=[13],
    )
    before = deepcopy(old)
    new = returned_enchantment({"_card": old})
    assert old == before
    assert new["controller_seat_id"] == 2
    assert not new["counters"] and not new["attachments"] and not new["subtypes"]
    assert new["damage"] == 0
    assert not any(new[k] for k in ("is_tapped", "is_attacking", "is_blocking"))
    assert returned_enchantment(new) is None  # no recurring creature-death return


@pytest.mark.parametrize(
    "changes",
    [
        {"card_types": ["CardType_Enchantment"]},
        {"card_types": []},
        {"is_token": True},
        {"object_kind": "GameObjectType_Token"},
        {"type_line": "Token Enchantment Creature"},
        {"is_phased_out": True},
        {"face_down": True},
        {"is_copy": True},
        {"oracle_text": "Lifelink"},
    ],
)
def test_no_return_for_noncreature_tokens_absent_or_lost_abilities(changes):
    assert returned_enchantment(innocence(**changes)) is None


def test_legacy_snapshot_without_current_types_still_supports_return():
    assert returned_enchantment(innocence(card_types=None)) is not None


def test_block_prompt_distinguishes_body_from_draw_engine():
    from arenamcp.coach import CoachEngine

    ours, theirs = [innocence()], [kaito()]
    local = {"seat_id": 2, "is_local": True, "life_total": 16}
    context = {
        "type": "declare_blockers",
        "legal_blocker_ids": [275],
        "attacker_ids": [247],
        "raw_blockers": [{"blockerInstanceId": 275, "attackerInstanceIds": [247], "maxAttackers": 1}],
    }
    state = {
        "players": [local, {"seat_id": 1, "life_total": 20}],
        "battlefield": ours + theirs,
        "decision_context": context,
    }
    lines = CoachEngine.__new__(CoachEngine)._format_block_combat(
        ours, theirs, local, 7, "Combat", set(), context, state
    )
    prompt = "\n".join(lines)
    assert "Enduring Innocence [id:275]" in prompt
    assert "noncreature enchantment retaining its draw engine" in prompt
    assert "creature body is lost, but the engine is not removed" in prompt
    assert "if it dies as a creature and its return trigger resolves" in prompt


def test_tenacity_combat_notes_do_not_promise_destroying_combo():
    tenacity = innocence(
        name="Enduring Tenacity",
        oracle_text=INNOCENCE.replace("Enduring Innocence", "Enduring Tenacity").replace(
            "Whenever one or more other creatures you control with power 2 or less enter, "
            "draw a card. This ability triggers only once each turn.",
            "Whenever you gain life, target opponent loses that much life.",
        ),
    )
    notes = "\n".join(death_trigger_notes([tenacity], 1))
    assert "their control" in notes and "retaining its lifegain-drain engine" in notes


def test_suppression_notes_do_not_promise_return():
    suppressor = {"oracle_text": "Creatures dying don't cause abilities to trigger."}
    notes = "\n".join(death_trigger_notes([innocence(), suppressor], 2))
    assert "suppression prevents its return trigger" in notes
    assert "retaining" not in notes


def test_unknown_controller_is_not_labeled_ours():
    note = death_trigger_notes([innocence(owner_seat_id=None, controller_seat_id=None)], 2)[0]
    assert "its owner's control" in note and "under your control" not in note


def test_unknown_local_seat_does_not_invent_a_side():
    note = death_trigger_notes([innocence()], None)[0]
    assert "its owner's control" in note and "their control" not in note


def test_phased_out_suppression_does_not_disable_return_note():
    suppressor = {"oracle_text": "Creatures dying don't cause abilities to trigger.", "is_phased_out": True}
    assert "retaining its draw engine" in death_trigger_notes([innocence(), suppressor], 2)[0]
