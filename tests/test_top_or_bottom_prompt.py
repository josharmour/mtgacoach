"""Top-or-bottom optional prompts (pro YFRA game, 2026-10-18, match 1).

Clash of Elements: "Choose target nonland permanent. Its owner may put it on
top of their library. If they do, Clash of Elements deals 2 damage to them.
If they didn't ..., they put it on the bottom." OptionalActionMessage
sourceId 296 (Clash), recipientIds [251] (Garruk) on turn 12: the pro
accepted (top, 2 damage) and redrew and recast Garruk on turn 13. On turn 14
(recipient 276, the Beast token) the pro declined.
"""

from __future__ import annotations

from tests.boards_pro_yfra_2026_10_18 import CARDS, M1_T11, card

from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.optional_prompts import top_or_bottom_choice

CLASH = {
    "name": "Clash of Elements",
    "type_line": "Instant",
    "mana_cost": "{1}{U}{R}",
    "oracle_text": "Choose target nonland permanent. Its owner may put it on top of their library. If they "
    "do, Clash of Elements deals 2 damage to them. If they didn't put the card on top of their library, "
    "they put it on the bottom.",
}


def _decision(source: int, recipient: int, names: list[str]) -> PendingDecision:
    meta = {"sourceId": source, "recipients": [recipient], "promptCardIds": [source, recipient],
            "promptCards": names}  # fmt: skip
    return PendingDecision(
        (307, 432),
        "OptionalAction",
        (
            DecisionOption("optional:accept", "Accept", meta=meta),
            DecisionOption("optional:decline", "Decline"),
        ),
    )


def _state(life: int = 19):
    state = {**M1_T11, "stack": [{**CLASH, "instance_id": 296, "controller_seat_id": 1, "owner_seat_id": 1}]}
    state["players"] = [dict(p) for p in M1_T11["players"]]
    state["players"][1]["life_total"] = life
    return state


def test_garruk_goes_on_top_and_we_take_the_damage():
    chosen, why = top_or_bottom_choice(_decision(296, 251, ["Clash of Elements", "Garruk"]), _state())
    assert chosen == "optional:accept", why
    assert "Garruk" in why


def test_a_token_goes_to_the_bottom():
    chosen, why = top_or_bottom_choice(_decision(296, 276, ["Clash of Elements", "Beast"]), _state())
    assert chosen == "optional:decline", why


def test_low_life_leaves_it_to_the_model():
    assert top_or_bottom_choice(_decision(296, 251, ["Clash of Elements", "Garruk"]), _state(life=6)) == (
        "",
        "",
    )


def test_cheap_permanent_is_not_worth_the_damage():
    state = _state()
    state["battlefield"] = list(state["battlefield"]) + [card(500, "Emergency Phytomedic", 2)]
    assert CARDS["Emergency Phytomedic"]["mana_cost"] == "{G/W}"
    assert top_or_bottom_choice(_decision(296, 500, ["Clash of Elements", "Phytomedic"]), state) == ("", "")
