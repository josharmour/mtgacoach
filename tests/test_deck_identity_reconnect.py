"""Reconnect must not identify command-zone boons as changing decks."""

from copy import deepcopy

import pytest

from arenamcp.deck_strategy import commander_ids, deck_identity
from arenamcp.gamestate import GameState


def _designation(seat=1):
    return {
        "affectedIds": [seat],
        "type": ["AnnotationType_Designation"],
        "details": [
            {"key": key, "valueInt32": [value]}
            for key, value in (("grpid", 83081), ("CostIncrease", 2), ("DesignationType", 1))
        ],
    }


def test_reconnected_commander_identity_survives_alternating_visible_boards():
    # Windows Nov 9: the same deck alternated c8adb2c6 ([6]) and
    # 2317e3f8 ([6,83081]) as the bridge showed Laelia beside a BOON.
    gs = GameState()
    gs.local_seat_id = 1
    gs.deck_cards = [65385] * 91 + [71491, 91109, 98652, 87291, 87300, 87325, 87443, 87301]
    gs.capture_connection_deck()
    gs._process_annotations([_designation(), _designation()])
    gs.publish_snapshot()
    state = gs.get_published_snapshot()
    assert state["commander_grp_ids"] == [83081]
    assert state["commander_casts"] == {83081: 1}
    assert gs._connection_deck_metadata["commander_grp_ids"] == [83081]
    boon = {"grp_id": 6, "instance_id": 571, "owner_seat_id": 1, "object_kind": "BOON"}
    commander = {"grp_id": 83081, "instance_id": 568, "owner_seat_id": 1, "object_kind": "CARD"}
    identity = deck_identity(state)
    for command in ([boon], [boon, commander], [], [commander]):
        state["command"] = command
        assert commander_ids(state) == [83081]
        assert deck_identity(state) == identity
    # Persisted reconnect state and room restoration retain the designation.
    restored = GameState()
    assert restored.restore_checkpoint(gs.export_checkpoint())
    assert restored.commander_grp_ids == [83081]
    gs.restore_connection_deck(gs._connection_deck_metadata, "reconnected-match")
    assert gs.commander_grp_ids == [83081]
    gs.reset()
    assert gs.commander_grp_ids == []


@pytest.mark.parametrize("kind", ["BOON", "ABILITY", "EMBLEM", "TOKEN", "TRIGGER"])
def test_fallback_commander_identity_excludes_noncard_objects(kind):
    state = {
        "local_seat_id": 1,
        "deck_cards": [100] * 60,
        "command": [{"grp_id": 6, "owner_seat_id": 1, "object_kind": kind}],
    }
    assert commander_ids(state) == []
    assert deck_identity(state) == deck_identity({**state, "command": []})


def test_opponent_designation_does_not_claim_their_commander():
    gs = GameState()
    gs.local_seat_id = 1
    gs._process_annotations([_designation(2)])
    assert gs.commander_grp_ids == []
    assert gs.commander_casts == {}


def test_fallback_accepts_physical_commander_and_rejects_dungeon():
    card = {"grp_id": 83081, "owner_seat_id": 1, "object_kind": "CARD"}
    state = {"local_seat_id": 1, "command": [card]}
    assert commander_ids(state) == [83081]
    state = deepcopy(state)
    state["command"][0]["type_line"] = "Dungeon"
    assert commander_ids(state) == []
