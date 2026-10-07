"""Untap tracking follows only what the GRE actually did.

bug_20261006_174855: our Seasoned Cryomancer aimed its tap-and-stun trigger at
the opponent's Unflinching Hortimancer (Ward {1}); the ward trigger countered
it, so no stun counter landed. Player.log then showed Hortimancer untapping in
the opponent's untap step (TappedUntappedPermanent tapped=0), yet the coach
logged "skipped 1 with untap prevention" and kept it tapped. The flag came
from the start of OUR turn: Hortimancer, still tapped from attacking, was
resent in that message, and any object still tapped during any untap step was
taken to be under a "doesn't untap" effect. Player.log omits isTapped when it
is false, so nothing ever cleared the flag.

The positive cases mirror Player.log 2026-10-06 18:01:50-18:02:00, where the
opponent's Cryomancer did stun two tapped creatures: the GRE removed a stun
counter in the next untap step and sent neither the objects nor a tap
annotation for them.
"""

import logging

from arenamcp.gamestate import GameState, create_game_state_handler

BATTLEFIELD = 28
US, THEM = 2, 1
HORTIMANCER = 226  # their Unflinching Hortimancer, Ward {1}
CRYOMANCER = 263  # our Seasoned Cryomancer
THEIR_LAND = 236
OUR_LAND = 230
STUN = 172  # GRE CounterType_Stun


def _deliver(state, game_state_message):
    handler = create_game_state_handler(state)
    handler(
        {
            "greToClientEvent": {
                "greToClientMessages": [
                    {
                        "type": "GREMessageType_GameStateMessage",
                        "systemSeatIds": [US],
                        "gameStateMessage": game_state_message,
                    }
                ]
            }
        }
    )


def _creature(instance_id, controller, *, tapped=None, grp_id=106248):
    obj = {
        "instanceId": instance_id,
        "grpId": grp_id,
        "type": "GameObjectType_Card",
        "zoneId": BATTLEFIELD,
        "visibility": "Visibility_Public",
        "ownerSeatId": controller,
        "controllerSeatId": controller,
        "cardTypes": ["CardType_Creature"],
        "power": {"value": 2},
        "toughness": {"value": 2},
    }
    if tapped is not None:  # Player.log omits isTapped when it is false
        obj["isTapped"] = tapped
    return obj


def _land(instance_id, controller, *, tapped=None):
    obj = {
        "instanceId": instance_id,
        "grpId": 106529,
        "type": "GameObjectType_Card",
        "zoneId": BATTLEFIELD,
        "visibility": "Visibility_Public",
        "ownerSeatId": controller,
        "controllerSeatId": controller,
        "cardTypes": ["CardType_Land"],
    }
    if tapped is not None:
        obj["isTapped"] = tapped
    return obj


def _tap_note(ann_id, instance_id, tapped):
    return {
        "id": ann_id,
        "affectedIds": [instance_id],
        "type": ["AnnotationType_TappedUntappedPermanent"],
        "details": [
            {"key": "tapped", "type": "KeyValuePairValueType_int32", "valueInt32": [1 if tapped else 0]}
        ],
    }


def _counter_note(ann_id, instance_id, counter_type, *, added, affector=377):
    return {
        "id": ann_id,
        "affectorId": affector,
        "affectedIds": [instance_id],
        "type": ["AnnotationType_CounterAdded" if added else "AnnotationType_CounterRemoved"],
        "details": [
            {"key": "counter_type", "type": "KeyValuePairValueType_int32", "valueInt32": [counter_type]},
            {"key": "transaction_amount", "type": "KeyValuePairValueType_int32", "valueInt32": [1]},
        ],
    }


def _turn(turn_number, active, *, step="Step_Upkeep"):
    return {
        "phase": "Phase_Beginning",
        "step": step,
        "turnNumber": turn_number,
        "activePlayer": active,
        "priorityPlayer": active,
    }


def _board(turn_number, active, objects):
    """A full state on the given turn with everything on one battlefield zone."""
    state = GameState()
    _deliver(
        state,
        {
            "type": "GameStateType_Full",
            "gameStateId": 1,
            "turnInfo": {"phase": "Phase_Main2", "turnNumber": turn_number, "activePlayer": active},
            "zones": [
                {
                    "zoneId": BATTLEFIELD,
                    "type": "ZoneType_Battlefield",
                    "visibility": "Visibility_Public",
                    "objectInstanceIds": [obj["instanceId"] for obj in objects],
                }
            ],
            "players": [
                {"systemSeatNumber": THEM, "lifeTotal": 20},
                {"systemSeatNumber": US, "lifeTotal": 14},
            ],
            "gameObjects": objects,
        },
    )
    return state


def _hortimancer_attacked_on_their_turn():
    # Turn 7 (theirs): Hortimancer attacked and stays tapped into our turn 8.
    return _board(
        7,
        THEM,
        [
            _creature(HORTIMANCER, THEM, tapped=True),
            _land(THEIR_LAND, THEM, tapped=True),
            _land(OUR_LAND, US, tapped=True),
        ],
    )


def test_ward_countered_stun_leaves_the_target_untapping_normally(caplog):
    state = _hortimancer_attacked_on_their_turn()

    # Turn 8 (ours): our land untaps. Hortimancer is resent still tapped, which
    # is normal: it is not its controller's untap step.
    _deliver(
        state,
        {
            "type": "GameStateType_Diff",
            "gameStateId": 171,
            "turnInfo": _turn(8, US),
            "gameObjects": [_creature(HORTIMANCER, THEM, tapped=True), _land(OUR_LAND, US)],
            "annotations": [_tap_note(500, OUR_LAND, tapped=False)],
        },
    )
    assert state.game_objects[HORTIMANCER].is_tapped
    assert HORTIMANCER not in state._untap_prevention
    assert not state.game_objects[OUR_LAND].is_tapped

    # Cryomancer's trigger targets Hortimancer; ward counters it. The GRE
    # records the target and the ability leaving, but no counter and no tap.
    _deliver(
        state,
        {
            "type": "GameStateType_Diff",
            "gameStateId": 185,
            "turnInfo": {"phase": "Phase_Main1", "turnNumber": 8, "activePlayer": US},
            "annotations": [
                {
                    "id": 520,
                    "affectorId": 274,
                    "affectedIds": [HORTIMANCER],
                    "type": ["AnnotationType_TargetSpec"],
                    "details": [{"key": "abilityGrpId", "valueInt32": [208426]}],
                },
                {
                    "id": 521,
                    "affectorId": HORTIMANCER,
                    "affectedIds": [275],
                    "type": ["AnnotationType_AbilityInstanceDeleted"],
                },
            ],
        },
    )
    assert state.game_objects[HORTIMANCER].counters == {}

    # Turn 9 (theirs): the GRE untaps Hortimancer (isTapped omitted = false).
    caplog.set_level(logging.INFO, logger="arenamcp.gamestate")
    _deliver(
        state,
        {
            "type": "GameStateType_Diff",
            "gameStateId": 198,
            "turnInfo": _turn(9, THEM),
            "gameObjects": [_creature(HORTIMANCER, THEM)],
            "annotations": [_tap_note(560, HORTIMANCER, tapped=False)],
        },
    )
    hortimancer = state.game_objects[HORTIMANCER]
    assert not hortimancer.is_tapped
    assert not hortimancer.counters
    assert HORTIMANCER not in state._untap_prevention
    assert "kept" not in caplog.text and "untap prevention" not in caplog.text


def test_blanket_untap_frees_the_target_when_the_message_has_no_annotations():
    state = _hortimancer_attacked_on_their_turn()
    _deliver(
        state,
        {
            "type": "GameStateType_Diff",
            "turnInfo": _turn(8, US),
            "gameObjects": [_creature(HORTIMANCER, THEM, tapped=True)],
        },
    )
    _deliver(state, {"type": "GameStateType_Diff", "turnInfo": _turn(9, THEM)})

    assert not state.game_objects[HORTIMANCER].is_tapped


def test_untap_annotation_clears_a_stale_prevention_flag():
    state = _hortimancer_attacked_on_their_turn()
    state._untap_prevention.add(HORTIMANCER)  # e.g. restored from an old checkpoint

    _deliver(
        state,
        {
            "type": "GameStateType_Diff",
            "turnInfo": _turn(8, THEM),
            "gameObjects": [_creature(HORTIMANCER, THEM)],
            "annotations": [_tap_note(600, HORTIMANCER, tapped=False)],
        },
    )

    assert not state.game_objects[HORTIMANCER].is_tapped
    assert HORTIMANCER not in state._untap_prevention


def test_stun_counter_the_gre_placed_keeps_the_permanent_tapped_for_one_untap(caplog):
    # Our turn 8: Hortimancer is untapped; this time the stun resolves.
    state = _board(8, US, [_creature(HORTIMANCER, THEM), _land(THEIR_LAND, THEM, tapped=True)])
    _deliver(
        state,
        {
            "type": "GameStateType_Diff",
            "turnInfo": {"phase": "Phase_Main1", "turnNumber": 8, "activePlayer": US},
            "gameObjects": [_creature(HORTIMANCER, THEM, tapped=True)],
            "annotations": [
                _tap_note(700, HORTIMANCER, tapped=True),
                _counter_note(701, HORTIMANCER, STUN, added=True),
            ],
        },
    )
    assert state.game_objects[HORTIMANCER].counters == {"Stun": 1}

    # Their turn 9: the GRE removes the stun counter instead of untapping it and
    # resends neither the object nor a tap annotation; their land untaps.
    caplog.set_level(logging.INFO, logger="arenamcp.gamestate")
    _deliver(
        state,
        {
            "type": "GameStateType_Diff",
            "turnInfo": _turn(9, THEM),
            "gameObjects": [_land(THEIR_LAND, THEM)],
            "annotations": [
                _tap_note(710, THEIR_LAND, tapped=False),
                _counter_note(711, HORTIMANCER, STUN, added=False, affector=9017),
            ],
        },
    )
    hortimancer = state.game_objects[HORTIMANCER]
    assert hortimancer.is_tapped
    assert "Stun" not in hortimancer.counters
    assert not state.game_objects[THEIR_LAND].is_tapped
    assert f"{HORTIMANCER} stun counter" in caplog.text

    # Their next untap step (turn 11): no counter left, so it untaps.
    _deliver(state, {"type": "GameStateType_Diff", "turnInfo": _turn(10, US)})
    assert state.game_objects[HORTIMANCER].is_tapped
    _deliver(
        state,
        {
            "type": "GameStateType_Diff",
            "turnInfo": _turn(11, THEM),
            "gameObjects": [_creature(HORTIMANCER, THEM)],
            "annotations": [_tap_note(800, HORTIMANCER, tapped=False)],
        },
    )
    assert not state.game_objects[HORTIMANCER].is_tapped


def test_stun_removed_in_its_untap_step_keeps_it_tapped_even_if_the_stun_was_missed():
    # 18:01:50 shape: our creature, tapped from attacking, was stunned on their
    # turn, but suppose the CounterAdded was never seen.
    state = _board(15, THEM, [_creature(297, US, tapped=True)])
    assert 297 not in state._untap_prevention  # held only by the stun below

    _deliver(
        state,
        {
            "type": "GameStateType_Diff",
            "gameStateId": 390,
            "turnInfo": _turn(16, US),
            "annotations": [_counter_note(977, 297, STUN, added=False, affector=9017)],
        },
    )

    assert state.game_objects[297].is_tapped


def test_permanent_still_tapped_after_its_own_untap_step_stays_tapped_until_the_gre_untaps_it():
    # Blossombind-style "doesn't untap": the GRE resends it still tapped in
    # its controller's own untap step.
    state = _board(7, THEM, [_creature(CRYOMANCER, US, tapped=True, grp_id=106264)])
    _deliver(
        state,
        {
            "type": "GameStateType_Diff",
            "turnInfo": _turn(8, US),
            "gameObjects": [_creature(CRYOMANCER, US, tapped=True, grp_id=106264)],
        },
    )
    assert state.game_objects[CRYOMANCER].is_tapped
    assert CRYOMANCER in state._untap_prevention

    # Next untap step with no word from the GRE: still held.
    _deliver(state, {"type": "GameStateType_Diff", "turnInfo": _turn(9, THEM)})
    _deliver(state, {"type": "GameStateType_Diff", "turnInfo": _turn(10, US)})
    assert state.game_objects[CRYOMANCER].is_tapped

    # The effect ends and the GRE untaps it.
    _deliver(
        state,
        {
            "type": "GameStateType_Diff",
            "turnInfo": _turn(12, US),
            "gameObjects": [_creature(CRYOMANCER, US, grp_id=106264)],
            "annotations": [_tap_note(900, CRYOMANCER, tapped=False)],
        },
    )
    assert not state.game_objects[CRYOMANCER].is_tapped
    assert CRYOMANCER not in state._untap_prevention


def test_loyalty_counter_annotation_does_not_double_the_loyalty_field():
    jace = {
        "instanceId": 242,
        "grpId": 106555,
        "type": "GameObjectType_Token",
        "zoneId": BATTLEFIELD,
        "ownerSeatId": THEM,
        "controllerSeatId": THEM,
        "cardTypes": ["CardType_Planeswalker"],
        "loyalty": {"value": 2},
    }
    state = _board(5, THEM, [])
    _deliver(
        state,
        {
            "type": "GameStateType_Diff",
            "gameObjects": [jace],
            "annotations": [_counter_note(245, 242, 7, added=True, affector=241)],
        },
    )

    assert state.game_objects[242].counters.get("Loyalty") == 2
