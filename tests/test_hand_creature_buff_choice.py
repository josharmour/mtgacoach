"""Lasting buffs onto a creature card in hand (pro YFRA game, 2026-10-18, match 5 turn 10).

Dina, Flesh Sower died in combat; her trigger ("choose a creature card in your
hand. It perpetually gains trample and gets +X/+X, where X is Dina's power")
was a SelectNReq over ids [162, 200, 284, 298] - Rescue Girl, Ghalta, Ruric
Thar and Loot. The pro picked Ghalta (SelectNResp ids [200]). The generic
SelectN path had no scorer and left it to the model.
"""

from __future__ import annotations

from arenamcp.decisions import DecisionOption, PendingDecision, best_hand_creature_choice

DINA = (
    "Lifelink\nWhenever you gain life, Dina perpetually gets +1/+1.\nWhen Dina dies, choose a creature card in "
    "your hand. It perpetually gains trample and gets +X/+X, where X is Dina's power."
)


def _hand_card(iid, name, cost, power, toughness):
    return {"instance_id": iid, "name": name, "mana_cost": cost, "type_line": "Legendary Creature — X",
            "power": power, "toughness": toughness, "owner_seat_id": 2, "controller_seat_id": 2}  # fmt: skip


HAND = [
    _hand_card(162, "Rescue Girl, First Responder", "{2}{W}", 2, 3),
    _hand_card(200, "Ghalta the Unstoppable", "{8}{G}", 8, 8),
    _hand_card(284, "Ruric Thar, Magecrusher", "{5}{G}{G}", 7, 7),
    _hand_card(298, "Loot, the Nexus", "{2}{G}", 1, 3),
]


def _decision(ids=(162, 200, 284, 298), label="Dina, Flesh Sower"):
    return PendingDecision(
        (262, 375), "SelectN", tuple(DecisionOption(f"sel:{i}", f"Option {i}") for i in ids), source_label=label
    )


def _state(hand=HAND):
    dina = {"instance_id": 311, "name": "Dina, Flesh Sower", "oracle_text": DINA, "type_line": "Legendary Creature"}
    return {"hand": list(hand), "graveyard": [dina], "battlefield": [], "local_seat_id": 2}


def test_dina_trigger_goes_on_ghalta():
    picked, why = best_hand_creature_choice(_decision(), _state())
    assert picked == ["sel:200"], why


def test_other_selections_are_left_to_the_model():
    assert best_hand_creature_choice(_decision(label="Some Discard Spell"), _state()) == ([], "")
    # an option that is not a known creature card in hand
    assert best_hand_creature_choice(_decision(ids=(162, 999)), _state()) == ([], "")
