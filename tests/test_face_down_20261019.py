"""Ledger #123 (2026-10-19 match 17a32d51, 12:55-12:57): a face-down creature read as "Double strike".

The opponent cast Fugitive Codebreaker face down with Disguise on T8. Player.log
gsid 177/179 sent it as grpId 3 (Arena's face-down card,
``CardTitleProvider.FACE_DOWN_GRPID``) with ``isFacedown``, a 2/2 whose only
ability is ward {2} (141939), plus a FaceDown annotation with REASON 6
(``ReasonFaceDown_Disguise``). The snapshot looked grpId 3 up in the card
table, found nothing, and fell back to the ability table, where id 3 is
"Double strike": "Ability (ID: 3)", a double striker. Their clock read "dead
in 2" (two Wild Rides made it 5/2) and T13's Shock went at it ("Kill the 2/2
double-striker"). The objects below are Player.log's own.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import arenamcp.server as server
from arenamcp.combat_keywords import has_combat_keyword
from arenamcp.gamestate import GameState
from arenamcp.mac_game_state import _card_entry

# gsid 179: resolved onto the battlefield (zone 28).
CODEBREAKER = {
    "instanceId": 334, "grpId": 3, "type": "GameObjectType_Card", "zoneId": 28,
    "visibility": "Visibility_Public", "ownerSeatId": 2, "controllerSeatId": 2,
    "cardTypes": ["CardType_Creature"], "power": {"value": 2}, "toughness": {"value": 2},
    "hasSummoningSickness": True, "viewers": [2], "overlayGrpId": 3, "isFacedown": True,
    "uniqueAbilities": [{"id": 314, "grpId": 141939}],
}  # fmt: skip
# gsid 177's persistent annotation: why it is face down.
FACE_DOWN = {
    "id": 487, "affectorId": 334, "affectedIds": [334], "type": ["AnnotationType_FaceDown"],
    "details": [
        {"key": "REASON", "type": "KeyValuePairValueType_int32", "valueInt32": [6]},
        {"key": "abilityGrpId", "type": "KeyValuePairValueType_int32", "valueInt32": [307]},
    ],
}  # fmt: skip
# gsid 184: Wild Ride resolved, +3/+0 and haste (ability 9).
WILD_RIDE = {
    **CODEBREAKER, "power": {"value": 5}, "abilityOriginalCardGrpIds": [95653],
    "uniqueAbilities": [{"id": 314, "grpId": 141939}, {"id": 317, "grpId": 9}],
}  # fmt: skip
del WILD_RIDE["hasSummoningSickness"]


class _CardDB:
    """The MTGA database as it answered on this machine: no card 3, ability 3 is "Double strike"."""

    abilities = {3: "Double strike", 9: "Haste", 141939: "Ward {o2}", 307: "Disguise"}

    def get_card_by_arena_id(self, grp_id):
        return None

    def get_ability_text(self, grp_id):
        return self.abilities.get(int(grp_id))


@pytest.fixture(autouse=True)
def card_db(monkeypatch):
    monkeypatch.setattr(server, "_get_card_db", lambda: _CardDB())
    monkeypatch.setattr(server, "game_state", SimpleNamespace(game_objects={}))


def _log_state(*objects):
    state = GameState()
    state.set_local_seat_id(1, source=3)
    state.update_from_message(
        {"type": "GameStateType_Diff", "gameStateId": 179, "gameObjects": [CODEBREAKER],
         "zones": [{"zoneId": 28, "type": "ZoneType_Battlefield", "objectInstanceIds": [334]}],
         "persistentAnnotations": [FACE_DOWN]}
    )  # fmt: skip
    for gsid, obj in enumerate(objects, start=184):
        state.update_from_message({"type": "GameStateType_Diff", "gameStateId": gsid, "gameObjects": [obj]})
    return state


def test_the_log_object_is_face_down_with_its_reason():
    obj = _log_state().game_objects[334]
    assert obj.face_down and obj.face_down_reason == 6
    row = obj.to_dict()
    assert row["face_down"] is True and row["face_down_reason"] == 6


def test_the_snapshot_names_a_face_down_2_2_with_only_its_own_abilities():
    row = server._serialize_snapshot_obj(_log_state().game_objects[334].to_dict())
    assert row["name"] == "Face-down 2/2 (Disguise)"
    assert row["type_line"] == "Creature" and (row["power"], row["toughness"]) == (2, 2)
    assert row["oracle_text"] == "Ward {o2}" and row["face_down"] is True
    assert "Ability (ID: 3)" not in str(row) and not has_combat_keyword(row, "double strike")


def test_wild_ride_pumps_it_and_grants_haste_but_no_double_strike():
    obj = _log_state(WILD_RIDE).game_objects[334]
    assert obj.face_down and obj.face_down_reason == 6  # the reason survives the re-sent object
    row = server._serialize_snapshot_obj(obj.to_dict())
    assert row["name"] == "Face-down 2/2 (Disguise)" and (row["power"], row["toughness"]) == (5, 2)
    assert row["oracle_text"] == "Ward {o2}\nHaste" and row["keywords"] == ["haste"]
    assert not has_combat_keyword(row, "double strike")


def test_turned_face_up_it_is_no_longer_face_down():
    face_up = {**CODEBREAKER, "grpId": 89042, "overlayGrpId": 89042, "power": {"value": 2},
               "toughness": {"value": 1}}  # fmt: skip
    del face_up["isFacedown"]
    obj = _log_state(face_up).game_objects[334]
    assert not obj.face_down and obj.face_down_reason is None and "face_down" not in obj.to_dict()


def test_grp_id_3_is_never_the_ability_double_strike():
    info = server.enrich_with_oracle_text(3)
    assert info["name"] == "Face-down 2/2" and info["oracle_text"] == "" and info["type_line"] == "Creature"
    # A card object the card table does not know is unknown, not an ability's text.
    card = server.card_object_info({"grp_id": 307, "object_kind": "CARD"})
    assert card["name"] == "Unknown (307)" and card["oracle_text"] == ""
    # An ability object is still read in the ability table.
    ability = server._serialize_snapshot_obj({"instance_id": 9, "grp_id": 3, "object_kind": "ABILITY"})
    assert ability["oracle_text"] == "Double strike"


def test_the_bridge_card_is_named_face_down_too():
    lookup = {334: _log_state().game_objects[334].to_dict()}
    bridge = {"instance_id": 334, "grp_id": 3, "owner_id": 2, "controller_id": 2, "object_type": "Card",
              "power": 2, "toughness": 2, "card_types": ["Creature"], "face_down": True,
              "face_down_reason": "Disguise"}  # fmt: skip
    row = server._serialize_bridge_card(bridge, lookup)
    assert row["name"] == "Face-down 2/2 (Disguise)" and row["oracle_text"] == "Ward {o2}"
    assert row["face_down"] is True and row["face_down_reason"] == "Disguise"
    # Without the log's object: the bridge's own flag and reason still name it.
    row = server._serialize_bridge_card(bridge, {})
    assert row["name"] == "Face-down 2/2 (Disguise)" and "Double strike" not in row["oracle_text"]


def test_the_mac_bridge_reports_why_a_card_is_face_down():
    node = {
        "$c": "GreClient.Rules.MtgCardInstance",
        "instanceId": 334,
        "baseGrpId": 3,
        "objectType_": {"e": "Card", "v": 0},
        "faceDownState_": {
            "$c": "GreClient.CardData.FaceDownState",
            "reasonFaceDown_": {"e": "Disguise", "v": 6},
        },
    }
    entry = _card_entry(node)
    assert entry["face_down"] is True and entry["face_down_reason"] == "Disguise" and entry["grp_id"] == 3
    assert server.face_down_reason_name(entry["face_down_reason"]) == "Disguise"
    assert server.face_down_reason_name(6) == server.face_down_reason_name("ReasonFaceDown_Disguise")


def test_the_log_snapshot_and_object_names_agree():
    state = _log_state()
    server.game_state.game_objects = state.game_objects
    battlefield = state.get_snapshot()["zones"]["battlefield"]
    assert [card["name"] for card in battlefield] == ["Face-down 2/2 (Disguise)"]
    assert battlefield[0]["oracle_text"] == "Ward {o2}"
    assert state._resolve_object_display_name(state.game_objects[334]) == "Face-down 2/2 (Disguise)"
    assert state._resolve_card_name(3) == "Face-down 2/2"
