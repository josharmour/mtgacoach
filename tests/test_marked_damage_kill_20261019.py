"""Burn that finishes a damaged creature is a kill (ledger #109).

2026-10-19 12:31:48 (match e453dfa3 T16): Lightning Strike had put 3 damage on
a 4/5 Hearth Elemental; Iroh's Demonstration's 4 damage at it was judged
"kills no legal opposing target" because damage_would_kill answered None for
any damaged creature, and the target was declined.
"""

from arenamcp.play_safety import damage_would_kill


def _elemental(**extra):
    return dict({"name": "Hearth Elemental", "power": 4, "toughness": 5}, **extra)


def test_marked_damage_makes_a_smaller_burn_lethal():
    assert damage_would_kill(_elemental(damaged_this_turn=True, damage=3), 4) is True
    assert damage_would_kill(_elemental(damaged_this_turn=True, damage=3), 2) is True


def test_marked_damage_that_still_leaves_it_alive_is_a_no():
    assert damage_would_kill(_elemental(damaged_this_turn=True, damage=1), 3) is False


def test_unknown_marked_damage_stays_unknown():
    assert damage_would_kill(_elemental(damaged_this_turn=True), 3) is None


def test_combat_still_makes_it_unknown():
    assert damage_would_kill(_elemental(is_attacking=True, damaged_this_turn=True, damage=3), 1) is None


def test_undamaged_creature_unchanged():
    assert damage_would_kill(_elemental(), 4) is False
    assert damage_would_kill(_elemental(), 5) is True


def test_marked_damage_without_damaged_this_turn_flag_counts():
    """2026-11-26 Unagi 5/5 with damage=3 in snapshot (damaged_this_turn False): 4 damage kills."""
    assert damage_would_kill(_elemental(damage=3, damaged_this_turn=False), 4) is True
    # Control: insufficient total damage still returns False.
    assert damage_would_kill(_elemental(damage=1, damaged_this_turn=False), 3) is False
    # Control: insufficient total damage while in combat remains None until combat settles.
    assert damage_would_kill(_elemental(damage=1, damaged_this_turn=False, is_blocking=True), 3) is None


def test_gamestate_preserves_damaged_this_turn_when_damage_positive():
    from arenamcp.gamestate import GameState

    gs = GameState()
    gs._update_game_object(
        {"instanceId": 342, "grpId": 97352, "type": "GameObjectType_Card", "zoneId": 28, "damage": 3}
    )
    assert gs.game_objects[342].damage == 3
    assert gs.game_objects[342].damaged_this_turn is True

    # Explicit damage=0 resets both damage and damaged_this_turn.
    gs._update_game_object({"instanceId": 342, "type": "GameObjectType_Card", "zoneId": 28, "damage": 0})
    assert gs.game_objects[342].damage == 0
    assert gs.game_objects[342].damaged_this_turn is False

    # Cleanup full object without damage resets damaged_this_turn too.
    gs._update_game_object({"instanceId": 342, "type": "GameObjectType_Card", "zoneId": 28, "damage": 2})
    gs._update_turn_info({"turnNumber": 10, "activePlayer": 1, "step": "Step_Cleanup"})
    gs._update_game_object({"instanceId": 342, "type": "GameObjectType_Card", "zoneId": 28})
    assert gs.game_objects[342].damage == 0
    assert gs.game_objects[342].damaged_this_turn is False

    # Turn rollover resets stale damage and damaged_this_turn even without a cleanup resend.
    gs._update_game_object({"instanceId": 342, "type": "GameObjectType_Card", "zoneId": 28, "damage": 3})
    gs._update_turn_info({"turnNumber": 11, "activePlayer": 2, "priorityPlayer": 2})
    assert gs.game_objects[342].damage == 0
    assert gs.game_objects[342].damaged_this_turn is False
