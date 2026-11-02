"""#273: a precombat protection trick is what makes a doomed attack worth declaring.

The symptom is "attack sequencing neglects available protection or pumps that
change blocking/lethal". The merged guard already answers the first half: an
affordable instant-speed trick that could change the combat keeps the attack,
and the same attack is held when that trick is not actually available. These
cases pin that contrast at the bridge's submission point, where every attack
passes before it is sent, and keep the controls honest:

* protection available (untapped Plains, {W} instant in hand) -> attack kept;
* no trick at all -> the attacker is held (the hold control);
* trick in hand but its land tapped -> held (unavailable protection);
* trick in hand but only a Mountain untapped -> held (wrong color);
* protection at sorcery speed -> held (no legal combat window);
* two attackers, one doomed -> only the doomed one is held, so the guard never
  turns a partial attack into an all-in or a full hold.

The board is a reconstructed precombat main, not a replay of a hidden packet:
the historical #273 packet is absent from this checkout.
"""

from __future__ import annotations

import pytest

from arenamcp.action_planner import ActionType, GameAction
from arenamcp.autopilot_bridge import _BridgeSubmitMixin
from arenamcp.combat_strategy import losing_attackers

GODS_WILLING = {
    "instance_id": 503,
    "name": "Gods Willing",
    "oracle_text": (
        "Target creature you control gains protection from the color of your choice until end of turn. Scry 1."
    ),
    "type_line": "Instant",
    "card_types": ["Instant"],
    "mana_cost": "{W}",
    "owner_seat_id": 2,
    "controller_seat_id": 2,
}
SORCERY_PROTECTION = {
    **GODS_WILLING,
    "instance_id": 505,
    "name": "Shielded Passage",
    "type_line": "Sorcery",
    "card_types": ["Sorcery"],
}
FACE = {"type": "DamageRecType_Player", "playerSystemSeatId": 1}


def land(identity: int, name: str, seat: int, *, tapped: bool = False) -> dict:
    symbol = "W" if name == "Plains" else "R"
    return {
        "instance_id": identity,
        "name": name,
        "oracle_text": f"({{T}}: Add {{{symbol}}}.)",
        "type_line": f"Basic Land — {name}",
        "card_types": ["Land"],
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "is_tapped": tapped,
    }


def creature(identity: int, name: str, seat: int, power: int, toughness: int, **extra) -> dict:
    return {
        "instance_id": identity,
        "name": name,
        "oracle_text": "",
        "type_line": "Creature",
        "card_types": ["Creature"],
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "power": power,
        "toughness": toughness,
        "is_tapped": False,
        **extra,
    }


def precombat_main(*, hand=(), plains_tapped: bool = False, mountain_only: bool = False) -> dict:
    """Our precombat main: a 2/2 and a 5/5 against their untapped white 4/4."""
    source = land(502, "Mountain" if mountain_only else "Plains", 2, tapped=plains_tapped)
    return {
        "match_id": "reconstructed-273",
        "turn": {"turn_number": 5, "active_player": 2, "priority_player": 2, "phase": "Main1", "step": ""},
        "local_seat_id": 2,
        "players": [
            {"seat_id": 1, "life_total": 20, "is_local": False, "mana_pool": {}},
            {"seat_id": 2, "life_total": 20, "is_local": True, "mana_pool": {}},
        ],
        "battlefield": [
            source,
            creature(500, "Grizzly Bears", 2, 2, 2),
            creature(504, "Hill Giant", 2, 5, 5),
            creature(501, "Serra Angel", 1, 4, 4, colors=["White"], type_line="Creature — Angel"),
        ],
        "hand": list(hand),
    }


def pending(state: dict) -> dict:
    return {
        "attackers": [
            {"attackerInstanceId": card["instance_id"], "legalDamageRecipients": [FACE]}
            for card in state["battlefield"]
            if card.get("controller_seat_id") == state["local_seat_id"] and card.get("power")
        ]
    }


def submit(state: dict, ids: list[int]) -> list[int]:
    """Run the bridge's own submission pass and return the attackers it sends."""
    names = [f"Creature {identity}" for identity in ids]
    entries = [{"attackerInstanceId": identity, "damageRecipient": FACE} for identity in ids]
    action = GameAction(
        action_type=ActionType.DECLARE_ATTACKERS,
        attacker_names=names,
        attacker_instance_ids=list(ids),
        attacker_targets={name: FACE for name in names},
    )
    submitted, kept = _BridgeSubmitMixin()._prepare_attack_submission(
        action, names, entries, state, pending(state)
    )
    assert [entry["attackerInstanceId"] for entry in kept] == submitted.attacker_instance_ids
    return submitted.attacker_instance_ids


def test_protection_trick_with_mana_keeps_the_precombat_attack():
    # The supported sequence: {W} instant in hand and an untapped Plains. The
    # guard keeps the attack because the trick can change this combat.
    state = precombat_main(hand=[GODS_WILLING])
    assert losing_attackers(state, [500]) == {}
    assert submit(state, [500]) == [500]


def test_without_the_trick_the_same_attack_is_held():
    # Hold control: identical board and mana, no trick. The 2/2 only feeds the
    # untapped 4/4 and dies for nothing.
    state = precombat_main()
    losing = losing_attackers(state, [500])
    assert set(losing) == {500}
    assert "kill it and survive" in losing[500]
    assert submit(state, [500]) == []


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("its land is tapped", {"hand": [GODS_WILLING], "plains_tapped": True}),
        ("only a Mountain is untapped", {"hand": [GODS_WILLING], "mountain_only": True}),
        ("protection is sorcery-speed", {"hand": [SORCERY_PROTECTION]}),
    ],
)
def test_unavailable_protection_is_not_credited(label: str, kwargs: dict):
    # Unavailable-protection controls: the card is in hand but cannot be cast
    # in this combat window with this mana, so the attack is held exactly as if
    # the trick were absent.
    state = precombat_main(**kwargs)
    assert set(losing_attackers(state, [500])) == {500}, label
    assert submit(state, [500]) == [], label


def test_only_the_doomed_attacker_is_held():
    # No all-in forcing: the 5/5 beats the 4/4 blocker and still attacks; only
    # the 2/2 that feeds it is held.
    state = precombat_main()
    assert set(losing_attackers(state, [500, 504])) == {500}
    assert submit(state, [500, 504]) == [504]


def test_the_same_partial_attack_is_kept_whole_when_protection_is_available():
    # With the trick affordable both attackers are sent: the guard does not
    # thin an attack it has already judged worth declaring.
    state = precombat_main(hand=[GODS_WILLING])
    assert losing_attackers(state, [500, 504]) == {}
    assert submit(state, [500, 504]) == [500, 504]
