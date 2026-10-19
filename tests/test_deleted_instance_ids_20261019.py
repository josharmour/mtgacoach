"""Ledger #89 (2026-10-19 match c8ea2b6c): resolved stack items stayed on the coach's stack.

Player.log (player_match.log): gsid 438 put Essence Channeler's trigger 406 on
the stack; gsid 440 resolved it, sent the stack zone WITHOUT objectInstanceIds
and listed ``diffDeletedInstanceIds: [406]``. Nothing read that field and
``_update_zone`` keeps a zone's old ids when they are missing, so 406 was
still on the coach's stack at game end (final_state in the match bundle).

The messages below are the log's own, trimmed of their ``actions`` and
``timers`` lists.
"""

from __future__ import annotations

from arenamcp.gamestate import GameState, ZoneType


def _obj(iid: int, grp: int, zone: int, seat: int, kind: str = "Card", **extra) -> dict:
    return {
        "instanceId": iid, "grpId": grp, "type": f"GameObjectType_{kind}", "zoneId": zone,
        "visibility": "Visibility_Public", "ownerSeatId": seat, "controllerSeatId": seat, **extra,
    }  # fmt: skip


def _zone(zone_id: int, kind: str, ids: list[int] | None, owner: int | None = None) -> dict:
    zone = {"zoneId": zone_id, "type": f"ZoneType_{kind}", "visibility": "Visibility_Public"}
    if owner is not None:
        zone["ownerSeatId"] = owner
    if ids is not None:
        zone["objectInstanceIds"] = ids
    return zone


def _ann(aid: int, kind: str, affected: list[int], affector: int | None = None, **details) -> dict:
    ann = {"id": aid, "affectedIds": affected, "type": [f"AnnotationType_{kind}"]}
    if affector is not None:
        ann["affectorId"] = affector
    ann["details"] = [
        {"key": key, "type": "KeyValuePairValueType_int32", "valueInt32": [value]}
        if isinstance(value, int)
        else {"key": key, "type": "KeyValuePairValueType_string", "valueString": [value]}
        for key, value in details.items()
    ]
    return ann


CREATURE = {"cardTypes": ["CardType_Creature"], "power": {"value": 1}, "toughness": {"value": 1}}
TURN_19 = {
    "phase": "Phase_Combat", "step": "Step_BeginCombat", "turnNumber": 19, "activePlayer": 2,
    "priorityPlayer": 2, "decisionPlayer": 2,
}  # fmt: skip


def _game() -> GameState:
    """The board before gsid 436: Leonin Vanguard (380) and Essence Channeler (391) on their side."""
    state = GameState()
    state.set_local_seat_id(1)
    state.update_from_message(
        {
            "type": "GameStateType_Full",
            "gameStateId": 435,
            "turnInfo": TURN_19,
            "players": [{"systemSeatNumber": 1, "lifeTotal": 9}, {"systemSeatNumber": 2, "lifeTotal": 11}],
            "zones": [
                _zone(27, "Stack", None),
                _zone(28, "Battlefield", [380, 391, 286]),
                _zone(30, "Limbo", None),
                _zone(31, "Hand", [365], owner=1),
                _zone(33, "Graveyard", None, owner=1),
                _zone(37, "Graveyard", None, owner=2),
            ],
            "gameObjects": [
                _obj(380, 93648, 28, 2, **CREATURE),
                _obj(391, 91548, 28, 2, **CREATURE),
                _obj(286, 65385, 28, 1, cardTypes=["CardType_Land"]),
                _obj(365, 97417, 31, 1, cardTypes=["CardType_Sorcery"]),
            ],
        }
    )
    return state


# gsid 436: Leonin Vanguard's beginning-of-combat trigger 405 goes on the stack.
GSID_436 = {
    "type": "GameStateType_Diff", "gameStateId": 436, "prevGameStateId": 435, "turnInfo": TURN_19,
    "zones": [_zone(27, "Stack", [405])],
    "gameObjects": [_obj(405, 119166, 27, 2, "Ability", objectSourceGrpId=93648, parentId=380)],
    "annotations": [_ann(1215, "AbilityInstanceCreated", [405], 380, source_zone=28)],
}  # fmt: skip
# gsid 438: 405 resolves (deleted), Essence Channeler's lifegain trigger 406 goes on the stack.
GSID_438 = {
    "type": "GameStateType_Diff", "gameStateId": 438, "prevGameStateId": 437, "turnInfo": TURN_19,
    "zones": [_zone(27, "Stack", [406])],
    "gameObjects": [_obj(406, 92970, 27, 2, "Ability", objectSourceGrpId=91548, parentId=391)],
    "annotations": [
        _ann(1216, "ResolutionStart", [405], 405, grpid=119166),
        _ann(1221, "AbilityInstanceCreated", [406], 391, source_zone=28),
        _ann(1222, "ResolutionComplete", [405], 405, grpid=119166),
        _ann(1223, "AbilityInstanceDeleted", [405], 380),
    ],
    "diffDeletedInstanceIds": [405],
}  # fmt: skip
# gsid 440: 406 resolves; the stack zone arrives without objectInstanceIds.
GSID_440 = {
    "type": "GameStateType_Diff", "gameStateId": 440, "prevGameStateId": 439, "turnInfo": TURN_19,
    "zones": [_zone(27, "Stack", None)],
    "gameObjects": [_obj(391, 91548, 28, 2, **{**CREATURE, "power": {"value": 5}, "toughness": {"value": 4}})],
    "annotations": [
        _ann(1224, "ResolutionStart", [406], 406, grpid=92970),
        _ann(1226, "CounterAdded", [391], 406, counter_type=1, transaction_amount=1),
        _ann(1231, "ResolutionComplete", [406], 406, grpid=92970),
        _ann(1232, "AbilityInstanceDeleted", [406], 391),
    ],
    "diffDeletedInstanceIds": [406],
}  # fmt: skip


def _stack_ids(state: GameState) -> list[int]:
    return [obj.instance_id for obj in state.stack]


def test_a_resolved_trigger_leaves_the_stack_when_arena_deletes_it():
    state = _game()
    state.update_from_message(GSID_436)
    assert _stack_ids(state) == [405]
    state.update_from_message(GSID_438)
    assert _stack_ids(state) == [406] and 405 not in state.game_objects
    state.update_from_message(GSID_440)
    assert _stack_ids(state) == [], "406 resolved at gsid 440 and was deleted"
    assert 406 not in state.game_objects
    assert all(406 not in zone.object_instance_ids for zone in state.zones.values())
    # The published snapshot the coach reads agrees.
    assert state.get_published_snapshot()["zones"]["stack"] == []
    # Its source stays on the battlefield, grown by the resolved counter.
    battlefield = {obj.instance_id: obj for obj in state.battlefield}
    assert 391 in battlefield and battlefield[391].power == 5


def test_zone_changes_keep_their_new_ids_when_the_old_ones_are_deleted():
    """gsids 308 / 313 / 375: Iroh's Demonstration cast from hand (365 -> 373 on the stack), resolved
    into the graveyard (373 -> 376) killing Healer's Hawk (367 -> 377); Arena deletes the old ids
    from Limbo two turns later."""
    state = _game()
    state.update_from_message(
        {
            "type": "GameStateType_Diff", "gameStateId": 307, "turnInfo": {**TURN_19, "turnNumber": 14},
            "zones": [_zone(28, "Battlefield", [380, 391, 286, 367])],
            "gameObjects": [_obj(367, 68475, 28, 2, **CREATURE)],
        }
    )  # fmt: skip
    state.update_from_message(
        {
            "type": "GameStateType_Diff", "gameStateId": 308,
            "zones": [_zone(27, "Stack", [373]), _zone(30, "Limbo", [365]), _zone(31, "Hand", [], owner=1)],
            "gameObjects": [_obj(365, 97417, 30, 1, cardTypes=["CardType_Sorcery"]),
                            _obj(373, 97417, 27, 1, cardTypes=["CardType_Sorcery"])],
            "annotations": [_ann(856, "ObjectIdChanged", [365], orig_id=365, new_id=373),
                            _ann(857, "ZoneTransfer", [373], zone_src=31, zone_dest=27, category="CastSpell")],
        }
    )  # fmt: skip
    assert _stack_ids(state) == [373]
    state.update_from_message(
        {
            "type": "GameStateType_Diff", "gameStateId": 313,
            "zones": [_zone(27, "Stack", None), _zone(28, "Battlefield", [380, 391, 286]),
                      _zone(30, "Limbo", [367, 373, 365]), _zone(33, "Graveyard", [376], owner=1),
                      _zone(37, "Graveyard", [377], owner=2)],
            "gameObjects": [_obj(367, 68475, 30, 2, **CREATURE), _obj(373, 97417, 30, 1),
                            _obj(376, 97417, 33, 1, cardTypes=["CardType_Sorcery"]),
                            _obj(377, 68475, 37, 2, **CREATURE)],
            "annotations": [_ann(877, "ResolutionComplete", [373], 373, grpid=97417),
                            _ann(878, "ObjectIdChanged", [373], 1, orig_id=373, new_id=376),
                            _ann(879, "ZoneTransfer", [376], 1, zone_src=27, zone_dest=33, category="Resolve"),
                            _ann(880, "ObjectIdChanged", [367], orig_id=367, new_id=377),
                            _ann(881, "ZoneTransfer", [377], zone_src=28, zone_dest=37, category="SBA_Damage")],
        }
    )  # fmt: skip
    assert _stack_ids(state) == []
    changed = [e["details"] for e in state.recent_events if e.get("type") == "object_id_changed"]
    assert {"orig_id": 373, "new_id": 376} in changed and {"orig_id": 367, "new_id": 377} in changed
    # gsid 375: Limbo emptied of them (here without the zone list, so only the delete does it).
    state.update_from_message(
        {"type": "GameStateType_Diff", "gameStateId": 375, "diffDeletedInstanceIds": [365, 367, 373]}
    )
    for old in (365, 367, 373):
        assert old not in state.game_objects
        assert all(old not in zone.object_instance_ids for zone in state.zones.values())
    graveyards = {obj.instance_id for obj in state.graveyard}
    assert graveyards == {376, 377}
    assert {obj.instance_id for obj in state.battlefield} == {380, 391, 286}


def test_a_message_without_deletions_changes_nothing():
    state = _game()
    state.update_from_message(GSID_436)
    before = dict(state.game_objects)
    state.update_from_message({"type": "GameStateType_Diff", "gameStateId": 437})
    assert state.game_objects == before and _stack_ids(state) == [405]
    assert state.zones[27].zone_type == ZoneType.STACK
