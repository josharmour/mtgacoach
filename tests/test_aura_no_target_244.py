"""Ledger #244: qualify "nothing castable stops it" when casts are unmodelled.

Reconstructed public scenario (not a replay): at 4 life facing lethal, the
hand held Feather of Flight and Ethereal Armor and Arena offered both casts.
The line search cannot value either Aura ("its effect"), so they appear as
playable-but-unmodelled options; the role reason nevertheless claimed
"nothing castable stops it". That is an overclaim about plays the search
does not model, not proof that the Auras win the game.

The original #244 question (stacking Auras on one carrier) is not decided
here. This pins only the narrower prompt-path contradiction between offered
unmodelled casts and a role reason that denies any castable out.
"""

from __future__ import annotations

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import build_pending_decision

pytestmark = pytest.mark.usefixtures("unlimited_search_deadline")

LOCAL, OPP = 2, 1

# Verified repository Oracle text (BLB.catalog.json / DSK.catalog.json).
FEATHER = {
    "name": "Feather of Flight",
    "type_line": "Enchantment — Aura",
    "mana_cost": "{1}{W}",
    "oracle_text": (
        "Flash\nEnchant creature\nWhen this Aura enters, draw a card.\n"
        "Enchanted creature gets +1/+0 and has flying."
    ),
}
ETHEREAL = {
    "name": "Ethereal Armor",
    "type_line": "Enchantment — Aura",
    "mana_cost": "{W}",
    "oracle_text": (
        "Enchant creature\nEnchanted creature gets +1/+1 for each enchantment you control "
        "and has first strike."
    ),
}
RECRUIT_SPELL = {
    "name": "Test Recruit",
    "type_line": "Creature — Test",
    "mana_cost": "{W}",
    "power": 1,
    "toughness": 1,
    "oracle_text": "",
}


class _CapturingBackend:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, system, user, *args, **kwargs):
        self.prompts.append(user)
        return '{"option_ids": ["pass"]}'


def _land(instance_id: int) -> dict:
    return {
        "instance_id": instance_id,
        "name": "Plains",
        "type_line": "Basic Land — Plains",
        "card_types": ["Land"],
        "controller_seat_id": LOCAL,
        "owner_seat_id": LOCAL,
        "is_tapped": False,
    }


def _creature(instance_id: int, name: str, power: int, toughness: int, seat: int) -> dict:
    return {
        "instance_id": instance_id,
        "name": name,
        "type_line": "Creature — Test",
        "card_types": ["Creature"],
        "power": power,
        "toughness": toughness,
        "controller_seat_id": seat,
        "owner_seat_id": seat,
    }


def _aura(info: dict, instance_id: int) -> dict:
    return {
        "instance_id": instance_id,
        "owner_seat_id": LOCAL,
        "controller_seat_id": LOCAL,
        **info,
    }


def _spell(info: dict, instance_id: int) -> dict:
    return {
        "instance_id": instance_id,
        "owner_seat_id": LOCAL,
        "controller_seat_id": LOCAL,
        **info,
    }


def _state(*, our_creatures: tuple[dict, ...] = (), hand: list[dict] | None = None) -> dict:
    if hand is None:
        hand = [_aura(FEATHER, 20), _aura(ETHEREAL, 21)]
    return {
        "turn": {
            "turn_number": 10,
            "phase": "Phase_Main1",
            "active_player": LOCAL,
            "priority_player": LOCAL,
        },
        "local_seat_id": LOCAL,
        "players": [
            {"seat_id": LOCAL, "is_local": True, "life_total": 4},
            {"seat_id": OPP, "is_local": False, "life_total": 20},
        ],
        "battlefield": [
            _land(1),
            _land(2),
            _land(3),
            _land(4),
            *our_creatures,
            _creature(10, "Test Hill", 3, 3, OPP),
            _creature(11, "Test Bear", 2, 2, OPP),
        ],
        "hand": hand,
        "stack": [],
        "graveyard": [],
        "legal_actions": [],
    }


def _decision(*, cast_options: bool = False):
    actions = [{"actionType": "ActionType_Pass"}]
    if cast_options:
        actions = [
            {"actionType": "ActionType_Cast", "grpId": 91549, "instanceId": 20, "hasAutoTap": True},
            {"actionType": "ActionType_Cast", "grpId": 92065, "instanceId": 21, "hasAutoTap": True},
            {"actionType": "ActionType_Pass"},
        ]
    return build_pending_decision(
        {
            "has_pending": True,
            "request_type": "ActionsAvailable",
            "can_pass": True,
            "actions": actions,
        },
        resolve_name=lambda grp: {91549: FEATHER["name"], 92065: ETHEREAL["name"]}.get(grp, ""),
    )


def _prompt(state: dict, *, cast_options: bool = False) -> str:
    backend = _CapturingBackend()
    planner = ActionPlanner(backend=backend)
    planner._llm_decision_options(_decision(cast_options=cast_options), state)
    assert backend.prompts
    return backend.prompts[0]


def _line(prompt: str, prefix: str) -> str:
    return next(line for line in prompt.splitlines() if line.strip().startswith(prefix))


def test_role_reason_does_not_deny_offered_unmodelled_aura_casts():
    prompt = _prompt(
        _state(our_creatures=(_creature(9, "Test Recruit", 1, 1, LOCAL),)),
        cast_options=True,
    )
    # The contradiction under test: both Auras are offered and flagged as
    # unvalued by the search...
    assert "- idx:0: Cast Feather of Flight" in prompt
    assert "- idx:1: Cast Ethereal Armor" in prompt
    assert "[LINE: effect not modelled]" in prompt
    # ...so the role reason must not claim that nothing castable stops it.
    role = _line(prompt, "STRATEGIC ROLE")
    assert "dead in 2 turns" in role
    assert "nothing castable stops it" not in role
    assert "nothing modelled stops it" in role
    assert "not modelled: Feather of Flight, Ethereal Armor" in role


def test_unmodelled_aura_caveat_survives_without_our_own_creature():
    # Plain "Enchant creature" can target an opposing creature (Feather of
    # Flight draws on entry), and target legality can change after casting a
    # body. Absence of our own carrier is not proof that no legal target will
    # exist this turn, so the caveat must remain rather than be suppressed.
    prompt = _prompt(_state())
    facts = _line(prompt, "FACTS:")
    assert "not modelled: Feather of Flight, Ethereal Armor" in facts


def test_fully_modelled_position_keeps_the_existing_reason():
    # No unmodelled castable spells: the original wording is still accurate.
    prompt = _prompt(_state(our_creatures=(_creature(9, "Test Recruit", 1, 1, LOCAL),), hand=[]))
    role = _line(prompt, "STRATEGIC ROLE")
    assert "dead in 2 turns and nothing castable stops it" in role
    assert "not modelled" not in role


def test_deployed_cast_is_qualified_by_the_unmodelled_caveat():
    # A modelled cast is deployed and an unmodelled Aura remains: name both
    # rather than claiming the cast settles the position.
    prompt = _prompt(
        _state(
            our_creatures=(_creature(9, "Test Recruit", 1, 1, LOCAL),),
            hand=[_spell(RECRUIT_SPELL, 22), _aura(FEATHER, 20), _aura(ETHEREAL, 21)],
        )
    )
    role = _line(prompt, "STRATEGIC ROLE")
    assert "even after casting Test Recruit (T10)" in role
    assert "not modelled: Feather of Flight, Ethereal Armor" in role
    assert "nothing castable stops it" not in role
