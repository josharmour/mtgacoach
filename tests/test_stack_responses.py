"""Protecting our permanent from an opponent's targeted spell (2026-10-18 pro game, match 3 turn 12).

The opponent cast Silence the Echo (#301) on Valgavoth, the Lifeline (#239);
the pro flashed in Teyo, Lightshield Expert in response, gave Valgavoth
hexproof, and the spell fizzled.
"""

from __future__ import annotations

from arenamcp.coach import CoachEngine
from arenamcp.stack_responses import shield_response_note, shield_responses

TEYO = {
    "instance_id": 263,
    "name": "Teyo, Lightshield Expert",
    "type_line": "Legendary Creature — Human Cleric",
    "mana_cost": "{1}{W}",
    "power": 1,
    "toughness": 1,
    "oracle_text": "Flash\nWhen Teyo enters, target permanent you control gains hexproof until end of turn. "
    "Put a +1/+1 counter on it if it's a creature. Put a loyalty counter on it if it's a planeswalker. "
    "(It can't be the target of spells or abilities your opponents control.)",
}
CHARM = {
    "instance_id": 264,
    "name": "Vigorbloom Charm",
    "type_line": "Instant",
    "mana_cost": "{G}{W}",
    "oracle_text": "Choose one —\n• Target permanent you control gains hexproof and indestructible until end of "
    "turn.\n• You draw a card and gain 3 life.\n• Put a +1/+1 counter on target creature you control. Then it "
    "fights target creature an opponent controls.",
}
# An indestructible-only answer: no help against exile.
STAND_FIRM = {
    "instance_id": 265,
    "name": "Stand Firm Test",
    "type_line": "Instant",
    "mana_cost": "{W}",
    "oracle_text": "Target creature you control gains indestructible until end of turn.",
}
SILENCE = {
    "instance_id": 301,
    "name": "Silence the Echo",
    "type_line": "Sorcery",
    "controller_seat_id": 1,
    "owner_seat_id": 1,
    "oracle_text": "As an additional cost to cast this spell, sacrifice a creature or planeswalker or pay {3}.\n"
    "Destroy target creature or planeswalker.",
    "targeting": [239],
}
VALGAVOTH = {
    "instance_id": 239,
    "name": "Valgavoth, the Lifeline",
    "type_line": "Legendary Creature — Elder Demon",
    "controller_seat_id": 2,
    "owner_seat_id": 2,
    "power": 4,
    "toughness": 4,
}


def _land(iid: int, name: str, kind: str, tapped: bool = False) -> dict:
    return {"instance_id": iid, "name": name, "type_line": f"Basic Land — {kind}", "controller_seat_id": 2,
            "owner_seat_id": 2, "is_tapped": tapped}  # fmt: skip


def _state(hand, *, stack=None, lands=None) -> dict:
    return {
        "local_seat_id": 2,
        "players": [{"seat_id": 1, "is_local": False}, {"seat_id": 2, "is_local": True}],
        "turn": {"turn_number": 12, "active_player": 1, "priority_player": 2, "phase": "Phase_Main1"},
        "battlefield": [
            VALGAVOTH,
            *(
                lands
                if lands is not None
                else [_land(205, "Plains", "Plains"), _land(201, "Forest", "Forest")]
            ),
        ],  # fmt: skip
        "stack": [SILENCE] if stack is None else stack,
        "hand": hand,
    }


def test_teyo_answers_silence_the_echo_on_valgavoth():
    (response,) = shield_responses(_state([TEYO]))
    assert (response.threat, response.target, response.answers, response.fizzles) == (
        "Silence the Echo", "Valgavoth, the Lifeline", ("Teyo, Lightshield Expert",), True,
    )  # fmt: skip
    note = shield_response_note(_state([TEYO]))
    assert note.startswith("RESPOND NOW: OPP Silence the Echo targets your Valgavoth, the Lifeline.")
    assert "Teyo, Lightshield Expert" in note


def test_a_modal_instant_with_a_hexproof_mode_answers_too():
    assert shield_responses(_state([CHARM]))[0].answers == ("Vigorbloom Charm",)


def test_no_note_without_the_mana_or_the_window():
    assert (
        shield_responses(
            _state([TEYO], lands=[_land(201, "Forest", "Forest"), _land(202, "Forest", "Forest")])
        )
        == []
    )
    assert shield_responses(_state([TEYO], stack=[])) == []
    ours = {**SILENCE, "controller_seat_id": 2, "owner_seat_id": 2}
    assert shield_responses(_state([TEYO], stack=[ours])) == []


def test_indestructible_does_not_answer_exile():
    assert shield_responses(_state([STAND_FIRM]))[0].answers == ("Stand Firm Test",)
    exile = {**SILENCE, "oracle_text": "Exile target creature."}
    assert shield_responses(_state([STAND_FIRM], stack=[exile])) == []


def test_the_note_reaches_the_prompts_stack_section():
    lines = CoachEngine._format_stack_section(CoachEngine.__new__(CoachEngine), _state([TEYO]), 2)
    assert lines[0] == "STACK (top resolves first):"
    assert any(line.startswith("RESPOND NOW:") for line in lines)
