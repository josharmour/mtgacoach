"""Icy Reception on a stale stack (2026-10-06 13:52:06, match 3da54de9 G1 T10).

Theoretical Necromancer was cast and resolved in one Player.log batch
(gameStateIds 220-222); turn 10 began at 226 and the ActionsAvailableReq at 230
offered Play:Island, so the stack was empty. The coach still fired
"stack_spell_opponent" on a board showing Necromancer on the stack, the planner
cast Icy Reception "to counter" it, Arena offered only the -5/-0 mode (the
counter mode was excluded), and the card was spent in our own main phase.
"""

from __future__ import annotations

import pytest

from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.gamestate import GameObject, GameState, Player, Zone, ZoneType
from arenamcp.gamestate_decisions import _handle_actions_available
from arenamcp.play_safety import filter_play_options, power_only_debuff_wasted, unsafe_play_reason

ICY_RECEPTION = {
    "instance_id": 351,
    "name": "Icy Reception",
    "type_line": "Instant",
    "oracle_text": "Choose one — \n•Counter target creature or legendary spell unless its controller pays {o3}. \n"
    "•Target creature gets -5/-0 until end of turn.",
}
NECROMANCER_ON_STACK = {"instance_id": 366, "name": "Theoretical Necromancer", "controller_seat_id": 1}
KEEPER = {
    "instance_id": 293,
    "name": "Keeper of the Quiet Hour",
    "type_line": "Artifact Creature — Chimera",
    "power": 3,
    "toughness": 2,
    "controller_seat_id": 1,
}
CAST_ICY = {"actionType": "Cast", "instanceId": 351, "grpId": 106256, "autoTapActions": [{"instanceId": 287}]}
PLAY_ISLAND = {"actionType": "Play", "instanceId": 370, "grpId": 106529}


def state(*, stack=(), menu=None, phase="Phase_Main1", battlefield=(KEEPER,)):
    snapshot = {
        "local_seat_id": 2,
        "players": [{"seat_id": 2, "is_local": True}, {"seat_id": 1, "is_local": False}],
        "turn": {"turn_number": 10, "active_player": 2, "phase": phase},
        "hand": [ICY_RECEPTION],
        "battlefield": [dict(card) for card in battlefield],
        "stack": [dict(entry) for entry in stack],
    }
    if menu is not None:
        snapshot["_bridge_actions"] = menu
    return snapshot


def test_counter_mode_needs_a_spell_on_the_live_stack():
    stale = state(stack=[NECROMANCER_ON_STACK], menu=[CAST_ICY, PLAY_ISLAND])
    reason = power_only_debuff_wasted(ICY_RECEPTION, stale)
    assert "nothing to counter" in reason
    assert unsafe_play_reason(stale, ICY_RECEPTION, "ActionType_Cast", CAST_ICY) == reason


def test_a_real_opposing_spell_still_allows_the_counter():
    responding = state(stack=[NECROMANCER_ON_STACK], menu=[CAST_ICY], phase="Phase_Main1")
    assert power_only_debuff_wasted(ICY_RECEPTION, responding) == ""


def test_an_opposing_ability_cannot_be_countered():
    trigger = {**NECROMANCER_ON_STACK, "object_kind": "ABILITY"}
    assert power_only_debuff_wasted(ICY_RECEPTION, state(stack=[trigger], menu=[CAST_ICY]))


def test_power_debuff_needs_combat_not_stale_attack_flags():
    stale_flags = state(battlefield=[{**KEEPER, "is_attacking": True}], phase="Phase_Main2", menu=[CAST_ICY])
    assert "never kills" in power_only_debuff_wasted(ICY_RECEPTION, stale_flags)
    in_combat = state(battlefield=[{**KEEPER, "is_attacking": True}], phase="Phase_Combat", menu=[CAST_ICY])
    assert power_only_debuff_wasted(ICY_RECEPTION, in_combat) == ""
    own_attacker = {**KEEPER, "controller_seat_id": 2, "is_attacking": True}
    assert power_only_debuff_wasted(ICY_RECEPTION, state(battlefield=[own_attacker], phase="Phase_Combat"))


def test_typed_menu_withholds_the_misfire():
    decision = PendingDecision(
        (230, 1),
        "ActionsAvailable",
        (
            DecisionOption("idx:0", "Cast Icy Reception", True, CAST_ICY),
            DecisionOption("idx:1", "Play land: Island", None, PLAY_ISLAND),
            DecisionOption("pass", "Pass", None, {}),
        ),
        can_pass=True,
    )
    kept = filter_play_options(decision, state(stack=[NECROMANCER_ON_STACK]))
    assert [option.option_id for option in kept.options] == ["idx:1", "pass"]


def casting_options(can_cancel=True):
    return PendingDecision(
        (231, 1),
        "CastingTimeOptions",
        (
            DecisionOption(
                "idx:0",
                "Mode 1: Target creature gets -5/-0 until end of turn.",
                meta={"actionType": "CastingTimeOption", "choiceKind": "modal", "grpId": 2125},
            ),
        ),
        can_cancel=can_cancel,
    )


def test_main_phase_power_only_mode_is_declined_so_the_cast_is_cancelled():
    assert filter_play_options(casting_options(), state()).options == ()
    # Nothing to cancel into: leave the only legal answer alone.
    assert len(filter_play_options(casting_options(can_cancel=False), state()).options) == 1
    in_combat = state(battlefield=[{**KEEPER, "is_attacking": True}], phase="Phase_Combat")
    assert len(filter_play_options(casting_options(), in_combat).options) == 1


@pytest.fixture
def island_info(monkeypatch):
    from arenamcp import server

    monkeypatch.setattr(server, "get_card_info", lambda grp_id: {"name": "Island"})


def test_land_play_offer_clears_ghost_stack_entries(island_info):
    gs = GameState()
    gs.local_seat_id = 2
    gs.players[2] = Player(seat_id=2)
    gs.zones[27] = Zone(zone_id=27, zone_type=ZoneType.STACK, object_instance_ids=[366])
    gs.game_objects[366] = GameObject(instance_id=366, grp_id=1, zone_id=27, owner_seat_id=1)
    assert [obj.instance_id for obj in gs.stack] == [366]

    _handle_actions_available(
        gs,
        {"actionsAvailableReq": {"actions": [{"actionType": "ActionType_Play", "grpId": 106529}]}},
    )
    assert gs.stack == []


def test_instant_speed_menu_keeps_the_stack(island_info):
    gs = GameState()
    gs.zones[27] = Zone(zone_id=27, zone_type=ZoneType.STACK, object_instance_ids=[366])
    gs.game_objects[366] = GameObject(instance_id=366, grp_id=1, zone_id=27, owner_seat_id=1)
    _handle_actions_available(gs, {"actionsAvailableReq": {"actions": [{"actionType": "ActionType_Pass"}]}})
    assert [obj.instance_id for obj in gs.stack] == [366]
