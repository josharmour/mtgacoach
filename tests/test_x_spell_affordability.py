"""X choices are capped by our mana, and a 0/0 X creature is not cast at X=0.

bug_20261012_231415 (23:13:51-23:14:14, our turn 2, one Forest untapped): Arena
offered "Cast Goldvein Hydra [OK]" ({X}{G}; payable at X=0 only). The model cast
it and chose X=1 ("~2 mana available"), PayCosts had no autotap solution and was
cancelled, and cast / X=1 / cancel repeated until the turn ended.
"""

from __future__ import annotations

from arenamcp.decisions import DecisionOption, PendingDecision, build_pending_decision
from arenamcp.play_safety import affordable_x, exact_mana_total, filter_play_options

ME, THEM = 1, 2

HYDRA_TEXT = (
    "Vigilance\nTrample\nHaste\nThis creature enters with X +1/+1 counters on it.\n"
    "When this creature dies, create a number of tapped Treasure tokens equal to its power."
)


def _hydra(instance_id: int = 245) -> dict:
    return {
        "instance_id": instance_id,
        "grp_id": 90513,
        "name": "Goldvein Hydra",
        "controller_seat_id": ME,
        "owner_seat_id": ME,
        "type_line": "Creature — Hydra",
        "mana_cost": "{X}{G}",
        "power": 0,
        "toughness": 0,
        "oracle_text": HYDRA_TEXT,
    }


def _forest(instance_id: int, tapped: bool = False) -> dict:
    return {
        "instance_id": instance_id,
        "name": "Forest",
        "controller_seat_id": ME,
        "type_line": "Basic Land — Forest",
        "is_tapped": tapped,
        "oracle_text": "({T}: Add {G}.)",
    }


def _state(lands: int = 1, extra=(), hand=None, stack=None) -> dict:
    return {
        "local_seat_id": ME,
        "turn": {"turn_number": 2, "active_player": ME, "priority_player": ME, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": ME, "is_local": True, "life_total": 25},
            {"seat_id": THEM, "is_local": False, "life_total": 25},
        ],
        "battlefield": [_forest(443 + i) for i in range(lands)] + list(extra),
        "hand": list(hand if hand is not None else [_hydra()]),
        "stack": list(stack or []),
        "graveyard": [],
        "exile": [],
        "command": [],
    }


def _cast_menu():
    return build_pending_decision(
        {
            "has_pending": True,
            "request_type": "ActionsAvailable",
            "can_pass": True,
            "actions": [
                {"actionType": "ActionType_Cast", "grpId": 90513, "instanceId": 245, "hasAutoTap": True},
                {"actionType": "ActionType_Pass"},
            ],
        },
        resolve_name=lambda grp: "Goldvein Hydra",
    )


def _x_menu(values=range(0, 6), source_id: int = 300):
    return PendingDecision(
        request_id=(25, 45),
        request_type="CastingTimeOptions",
        options=tuple(
            DecisionOption(
                f"cto:{v}",
                f"X = {v}",
                meta={
                    "choiceKind": "numeric_input",
                    "childIndex": 0,
                    "numericValue": v,
                    "sourceId": source_id,
                },
            )
            for v in values
        ),
        can_cancel=True,
    )


# --- the live turn ----------------------------------------------------------


def test_the_hydra_is_not_cast_with_one_land():
    kept = filter_play_options(_cast_menu(), _state(lands=1))
    assert [o.option_id for o in kept.options] == ["pass"]


def test_no_x_value_survives_with_one_land():
    state = _state(lands=1, hand=[], stack=[_hydra(300)])
    assert affordable_x(state, _hydra(300)) == 0
    assert filter_play_options(_x_menu(), state).options == ()


def test_with_four_lands_x_runs_one_to_three():
    state = _state(lands=4, hand=[], stack=[_hydra(300)])
    kept = filter_play_options(_x_menu(), state)
    assert [o.meta["numericValue"] for o in kept.options] == [1, 2, 3]
    assert [o.option_id for o in filter_play_options(_cast_menu(), _state(lands=4)).options] == [
        "idx:0",
        "pass",
    ]


def test_source_falls_back_to_our_x_spell_on_the_stack():
    state = _state(lands=2, hand=[], stack=[_hydra(300)])
    kept = filter_play_options(_x_menu(source_id=0), state)
    assert [o.meta["numericValue"] for o in kept.options] == [1]


# --- the cap stays out of the way when the mana count is not exact ---------------


def test_variable_sources_and_reducers_remain_unknown_but_fixed_output_is_bounded():
    rofellos = {
        "instance_id": 560,
        "name": "Rofellos, Llanowar Emissary",
        "controller_seat_id": ME,
        "type_line": "Legendary Creature — Elf Druid",
        "turn_entered_battlefield": 1,
        "oracle_text": "{oT}: Add {oG} for each Forest you control.",
    }
    medallion = {
        "instance_id": 452,
        "name": "Emerald Medallion",
        "controller_seat_id": ME,
        "type_line": "Artifact",
        "oracle_text": "Green spells you cast cost {o1} less to cast.",
    }
    sol_ring = {
        "instance_id": 470,
        "name": "Sol Ring",
        "controller_seat_id": ME,
        "type_line": "Artifact",
        "oracle_text": "{T}: Add {C}{C}.",
    }
    sprawl = {
        "instance_id": 655,
        "name": "Utopia Sprawl",
        "controller_seat_id": ME,
        "type_line": "Enchantment — Aura",
        "oracle_text": "Whenever enchanted Forest is tapped for mana, its controller adds an additional one mana "
        "of the chosen color.",
    }
    fixed_state = _state(lands=1, extra=[sol_ring], hand=[], stack=[_hydra(300)])
    assert exact_mana_total(fixed_state) == 3
    assert [o.meta["numericValue"] for o in filter_play_options(_x_menu(), fixed_state).options] == [1, 2]
    for extra in (rofellos, medallion, sprawl):
        state = _state(lands=1, extra=[extra], hand=[], stack=[_hydra(300)])
        assert exact_mana_total(state) is None, extra["name"]
        kept = filter_play_options(_x_menu(), state)
        # Only the dying X=0 goes; every larger X is the model's call.
        assert [o.meta["numericValue"] for o in kept.options] == [1, 2, 3, 4, 5], extra["name"]
        assert [
            o.option_id for o in filter_play_options(_cast_menu(), _state(lands=1, extra=[extra])).options
        ] == [
            "idx:0",
            "pass",
        ]


def test_opponent_permanents_do_not_make_our_mana_inexact():
    their_ring = {
        "instance_id": 900,
        "name": "Sol Ring",
        "controller_seat_id": THEM,
        "type_line": "Artifact",
        "oracle_text": "{T}: Add {C}{C}.",
    }
    assert exact_mana_total(_state(lands=1, extra=[their_ring])) == 1


def test_tapped_lands_do_not_count():
    state = _state(lands=0, extra=[_forest(443), _forest(444, tapped=True)], hand=[], stack=[_hydra(300)])
    assert affordable_x(state, _hydra(300)) == 0


def test_non_creature_x_spells_keep_x_zero():
    blaze = {
        "instance_id": 301,
        "name": "Blaze",
        "controller_seat_id": ME,
        "type_line": "Sorcery",
        "mana_cost": "{X}{R}",
        "oracle_text": "Blaze deals X damage to any target.",
    }
    state = _state(lands=2, hand=[], stack=[blaze])
    kept = filter_play_options(_x_menu(source_id=301), state)
    assert [o.meta["numericValue"] for o in kept.options] == [0, 1]
