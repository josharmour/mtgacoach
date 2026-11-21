"""Regression tests for Sprint 3 Batch 3 (S3-7: Flash non-haste creature discipline & same-CMC stat tie-break)."""

from __future__ import annotations

from arenamcp.board_math_picks import (
    _creature_stat_tiebreak,
    _is_holdable_flash_creature,
    board_math_option_pick,
)
from arenamcp.decisions import DecisionOption, PendingDecision


def _land(iid: int, name: str = "Island") -> dict:
    return {
        "instance_id": iid,
        "name": name,
        "type_line": "Basic Land — Island",
        "oracle_text": "{T}: Add {U}.",
        "card_types": ["CardType_Land"],
        "subtypes": ["SubType_Island"],
        "controller_seat_id": 1,
        "owner_seat_id": 1,
        "is_tapped": False,
    }


def _cre(iid: int, name: str, cost: str, p: int, t: int, oracle: str = "") -> dict:
    return {
        "instance_id": iid,
        "name": name,
        "type_line": "Creature — Merfolk",
        "mana_cost": cost,
        "oracle_text": oracle,
        "card_types": ["CardType_Creature"],
        "power": p,
        "toughness": t,
        "controller_seat_id": 1,
        "owner_seat_id": 1,
    }


def test_s3_7_flash_creature_classification_and_controls():
    flash_cre = _cre(10, "Tide Ambusher", "{1}{U}", 2, 2, "Flash")
    flash_haste = _cre(11, "Swift Ambusher", "{1}{R}", 2, 2, "Flash, haste")
    sorcery_cre = _cre(12, "River Guard", "{1}{U}", 2, 2, "")
    flash_aura = {
        "instance_id": 13,
        "name": "Flash Cloak",
        "type_line": "Enchantment — Aura",
        "mana_cost": "{W}",
        "oracle_text": "Flash\nEnchant creature",
        "card_types": ["CardType_Enchantment"],
    }
    assert _is_holdable_flash_creature(flash_cre, "creature") is True
    assert _is_holdable_flash_creature(flash_haste, "creature") is False
    assert _is_holdable_flash_creature(sorcery_cre, "creature") is False
    assert _is_holdable_flash_creature(flash_aura, "other") is False


def test_s3_7_stat_tiebreak_prefers_stronger_same_cmc_creature():
    weak = _cre(20, "Plain Bear", "{1}{U}", 2, 2, "")
    strong = _cre(21, "Sky Lancer", "{1}{U}", 2, 2, "Flying, vigilance")
    assert _creature_stat_tiebreak(strong) > _creature_stat_tiebreak(weak)

    state = {
        "local_seat_id": 1,
        "turn": {
            "turn_number": 2,
            "active_player": 1,
            "priority_player": 1,
            "phase": "Phase_Main1",
            "step": "",
        },
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20, "lands_played": 1},
            {"seat_id": 2, "is_local": False, "life_total": 20, "lands_played": 0},
        ],
        "hand": [weak, strong],
        "battlefield": [_land(1), _land(2)],
        "stack": [],
        "graveyard": [],
        "exile": [],
    }
    # Put weak first in the menu to prove stat tie-break beats menu order
    dec = PendingDecision(
        (1, 1),
        "ActionsAvailable",
        (
            DecisionOption(
                "idx:0",
                "Cast Plain Bear",
                payable=True,
                meta={"actionType": "ActionType_Cast", "instanceId": 20},
            ),
            DecisionOption(
                "idx:1",
                "Cast Sky Lancer",
                payable=True,
                meta={"actionType": "ActionType_Cast", "instanceId": 21},
            ),
            DecisionOption("pass", "Pass"),
        ),
    )
    pick, _reason = board_math_option_pick(dec, state)
    assert pick == ["idx:1"]


def test_s3_7_greedy_hold_defers_flash_creature_but_casts_flash_haste_creature(monkeypatch):
    """When line_search.best holds, a greedy Flash non-haste creature passes, while Flash+Haste still casts."""
    from types import SimpleNamespace

    import arenamcp.board_assessment as ba

    flash_cre = _cre(40, "Tide Ambusher", "{1}{U}", 2, 2, "Flash")
    flash_haste = _cre(41, "Swift Ambusher", "{1}{U}", 2, 2, "Flash, haste")
    base_state = {
        "local_seat_id": 1,
        "turn": {
            "turn_number": 3,
            "active_player": 1,
            "priority_player": 1,
            "phase": "Phase_Main1",
            "step": "",
        },
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20, "lands_played": 1},
            {"seat_id": 2, "is_local": False, "life_total": 20, "lands_played": 0},
        ],
        "battlefield": [_land(1), _land(2)],
        "stack": [],
        "graveyard": [],
        "exile": [],
    }
    first_step = SimpleNamespace(
        casts=(), land="", post_combat=(), castable=("Tide Ambusher", "Swift Ambusher"), attack=()
    )
    fake_ls = SimpleNamespace(
        baseline=SimpleNamespace(
            steps=[
                SimpleNamespace(
                    casts=("Tide Ambusher", "Swift Ambusher"),
                    land="",
                    post_combat=(),
                    castable=("Tide Ambusher", "Swift Ambusher"),
                )
            ]
        )
    )
    monkeypatch.setattr(
        ba,
        "assess",
        lambda _st: SimpleNamespace(
            our_turn=True,
            lookahead=[first_step],
            line_search=fake_ls,
            unmodelled=(),
        ),
    )

    dec_flash = PendingDecision(
        (1, 3),
        "ActionsAvailable",
        (
            DecisionOption(
                "idx:0",
                "Cast Tide Ambusher",
                payable=True,
                meta={"actionType": "ActionType_Cast", "instanceId": 40},
            ),
            DecisionOption("pass", "Pass"),
        ),
    )
    pick_flash, _ = board_math_option_pick(dec_flash, {**base_state, "hand": [flash_cre]})
    assert pick_flash == ["pass"]

    dec_haste = PendingDecision(
        (1, 4),
        "ActionsAvailable",
        (
            DecisionOption(
                "idx:0",
                "Cast Swift Ambusher",
                payable=True,
                meta={"actionType": "ActionType_Cast", "instanceId": 41},
            ),
            DecisionOption("pass", "Pass"),
        ),
    )
    pick_haste, _ = board_math_option_pick(dec_haste, {**base_state, "hand": [flash_haste]})
    assert pick_haste == ["idx:0"]


def test_s3_7_sorcery_creature_cast_before_flash_non_haste_creature():
    flash_cre = _cre(30, "Tide Ambusher", "{2}{U}", 3, 2, "Flash")
    sorcery_cre = _cre(31, "River Guard", "{1}{U}", 2, 2, "")
    state = {
        "local_seat_id": 1,
        "turn": {
            "turn_number": 5,
            "active_player": 1,
            "priority_player": 1,
            "phase": "Phase_Main1",
            "step": "",
        },
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20, "lands_played": 1},
            {"seat_id": 2, "is_local": False, "life_total": 20, "lands_played": 0},
        ],
        "hand": [flash_cre, sorcery_cre],
        "battlefield": [_land(1), _land(2), _land(3), _land(4), _land(5)],
        "stack": [],
        "graveyard": [],
        "exile": [],
    }
    dec = PendingDecision(
        (1, 2),
        "ActionsAvailable",
        (
            DecisionOption(
                "idx:0",
                "Cast Tide Ambusher",
                payable=True,
                meta={"actionType": "ActionType_Cast", "instanceId": 30},
            ),
            DecisionOption(
                "idx:1",
                "Cast River Guard",
                payable=True,
                meta={"actionType": "ActionType_Cast", "instanceId": 31},
            ),
            DecisionOption("pass", "Pass"),
        ),
    )
    pick, _reason = board_math_option_pick(dec, state)
    assert pick == ["idx:1"]
