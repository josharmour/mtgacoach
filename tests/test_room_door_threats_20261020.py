"""Bug #112: the opponent's locked Room door and the creature it promises are visible.

Unholy Annex // Ritual Chamber's locked door hides a 6/6 flying Demon ("When you
unlock this door, create ..."). The board facts never flagged it, so the coach
planned against an enchantment that was about to become their biggest attacker.

Two defects sank the first attempt (737b8e1, reverted in f4932af) and are covered
here specifically:

- It split the Room's text on "---", which only Scryfall formatting has. On live
  mtgadb data each face carries its own text (``related_faces``), so the split
  named the UNLOCKED half as locked. The faces' own texts decide now; without
  faces the card text is scanned but only ever labelled a generic "door".
- With no unlock tracking it re-flagged the Demon AFTER it had already died.
  gamestate now marks a Room ``door_token_seen`` from game events (the token's
  ``objectSourceGrpId`` names its creator, ledger #185) and the mark is sticky,
  so a dead Demon stays spent.
"""

from __future__ import annotations

from arenamcp.board_assessment import _room_door_threats, _threats
from arenamcp.gamestate import GameState

US, THEM = 2, 1
ROOM_GRP = 9001
DEMON_GRP = 9002

ANNEX_TEXT = (
    "At the beginning of your end step, draw a card. If you control a Demon, each opponent loses 2 life "
    "and you gain 2 life. Otherwise, you lose 2 life.\n"
    "(You may cast either half. That door unlocks on the battlefield. As a sorcery, you may pay the mana "
    "cost of a locked door to unlock it.)"
)
RITUAL_TEXT = "When you unlock this door, create a 6/6 black Demon creature token with flying."

RELATED_FACES = [
    {"name": "Unholy Annex", "oracle_text": ANNEX_TEXT},
    {"name": "Ritual Chamber", "oracle_text": RITUAL_TEXT},
]


def _annex(**extra) -> dict:
    """The Room as mtgadb enrichment presents it: one face's text, no '---' anywhere."""
    card = {
        "instance_id": 310,
        "name": "Unholy Annex // Ritual Chamber",
        "controller_seat_id": THEM,
        "card_types": ["CardType_Enchantment"],
        "type_line": "Enchantment — Room",
        "oracle_text": ANNEX_TEXT,
        "related_faces": RELATED_FACES,
    }
    card.update(extra)
    return card


# --- the threat pass -----------------------------------------------------------------------------


def test_mtgadb_text_flags_the_locked_ritual_chamber_not_the_unlocked_half():
    """mtgadb-style data (no '---' separator): the locked half is named, the unlocked one is not."""
    threats = _threats([_annex()], opponent=THEM, theirs=[], our_air=False, our_count=0)
    assert len(threats) == 1
    assert threats[0].name == "Unholy Annex // Ritual Chamber"
    assert "locked Ritual Chamber: creates 6/6 flying Demon token when unlocked" in threats[0].why
    assert "Unholy Annex" not in threats[0].why
    # A 6/6 flyer we cannot block outscores most bodies on the board.
    assert threats[0].score > 10.0


def test_demon_already_dead_is_not_reflagged():
    """Once the promised token has been seen (sticky gamestate mark), the door stays spent.

    The Annex half itself still drains ("each opponent loses 2 life"), so the card
    keeps a threat entry — but the promised Demon is no longer part of it.
    """
    spent = _threats([_annex(door_token_seen=True)], opponent=THEM, theirs=[], our_air=False, our_count=0)
    live = _threats([_annex()], opponent=THEM, theirs=[], our_air=False, our_count=0)
    assert not any("locked" in t.why or "Demon token" in t.why for t in spent)
    assert any("locked Ritual Chamber" in t.why for t in live)
    assert all(t.score < live[0].score for t in spent)


def test_scryfall_combined_text_gets_a_generic_door_label():
    """Without related_faces the '---'-joined Scryfall text still surfaces the threat — unlabelled.

    Naming a half there is exactly what mislabelled the unlocked door last time.
    """
    card = _annex(oracle_text=f"{ANNEX_TEXT}\n---\n{RITUAL_TEXT}")
    del card["related_faces"]
    labels = [label for label, *_ in _room_door_threats(card)]
    assert labels == ["door"]
    threats = _threats([card], opponent=THEM, theirs=[], our_air=False, our_count=0)
    assert any("locked door: creates 6/6 flying Demon token when unlocked" in t.why for t in threats)


def test_both_doors_promising_tokens_flag_each_face():
    """A Room whose both halves make tokens reads each door from its own face text."""
    card = _annex(
        related_faces=[
            {"name": "Left", "oracle_text": RITUAL_TEXT},
            {"name": "Right", "oracle_text": RITUAL_TEXT},
        ]
    )
    specs = _room_door_threats(card)
    assert sorted(label for label, *_ in specs) == ["Left", "Right"]


# --- gamestate tracking --------------------------------------------------------------------------


ROOM_OBJECT = {
    "instanceId": 310,
    "grpId": ROOM_GRP,
    "type": "GameObjectType_Card",
    "zoneId": 28,
    "visibility": "Visibility_Public",
    "ownerSeatId": THEM,
    "controllerSeatId": THEM,
    "cardTypes": ["CardType_Enchantment"],
}  # fmt: skip

DEMON_TOKEN = {
    "instanceId": 311,
    "grpId": DEMON_GRP,
    "type": "GameObjectType_Token",
    "zoneId": 28,
    "visibility": "Visibility_Public",
    "ownerSeatId": THEM,
    "controllerSeatId": THEM,
    "cardTypes": ["CardType_Creature"],
    "subtypes": ["SubType_Demon"],
    "power": {"value": 6},
    "toughness": {"value": 6},
}  # fmt: skip

RABBIT_TOKEN = {
    "instanceId": 312,
    "grpId": 9003,
    "type": "GameObjectType_Token",
    "zoneId": 28,
    "visibility": "Visibility_Public",
    "ownerSeatId": THEM,
    "controllerSeatId": THEM,
    "cardTypes": ["CardType_Creature"],
    "subtypes": ["SubType_Rabbit"],
    "power": {"value": 1},
    "toughness": {"value": 1},
}  # fmt: skip


def _room_state(monkeypatch) -> GameState:
    from arenamcp import server

    monkeypatch.setattr(
        server,
        "enrich_with_oracle_text",
        lambda grp: (
            {
                "name": "Unholy Annex // Ritual Chamber",
                "type_line": "Enchantment — Room",
                "oracle_text": ANNEX_TEXT,
                "related_faces": RELATED_FACES,
            }
            if grp == ROOM_GRP
            else {}
        ),
    )
    state = GameState()
    state.update_from_message(
        {
            "type": "GameStateType_Full",
            "gameObjects": [ROOM_OBJECT],
            "zones": [
                {
                    "zoneId": 28,
                    "type": "ZoneType_Battlefield",
                    "ownerSeatId": THEM,
                    "objectInstanceIds": [310],
                }
            ],
        }
    )
    return state


def test_locked_room_starts_unspent(monkeypatch):
    state = _room_state(monkeypatch)
    assert state.room_door_token_seen == set()
    battlefields = state.get_published_snapshot()["zones"]["battlefield"]
    room = next(o for o in battlefields if o["instance_id"] == 310)
    assert room["is_room"] is True
    assert room["door_token_seen"] is False


def test_the_demons_arrival_spends_the_door_and_survives_its_death(monkeypatch):
    """The token's objectSourceGrpId names the Room; the mark outlives the token."""
    state = _room_state(monkeypatch)
    state.update_from_message(
        {
            "type": "GameStateType_Diff",
            "gameObjects": [{**DEMON_TOKEN, "objectSourceGrpId": ROOM_GRP}],
            "zones": [
                {
                    "zoneId": 28,
                    "type": "ZoneType_Battlefield",
                    "ownerSeatId": THEM,
                    "objectInstanceIds": [310, 311],
                }
            ],
        }
    )
    assert state.room_door_token_seen == {310}

    # The Demon dies and Arena deletes it.
    state.update_from_message(
        {
            "type": "GameStateType_Diff",
            "diffDeletedInstanceIds": [311],
            "zones": [
                {
                    "zoneId": 28,
                    "type": "ZoneType_Battlefield",
                    "ownerSeatId": THEM,
                    "objectInstanceIds": [310],
                }
            ],
        }
    )
    assert state.room_door_token_seen == {310}


def test_a_body_match_spends_the_door_when_arena_omits_the_creator(monkeypatch):
    """No objectSourceGrpId: a 6/6 Demon on their side still reads as the door's token."""
    state = _room_state(monkeypatch)
    anonymous = {k: v for k, v in DEMON_TOKEN.items() if k != "objectSourceGrpId"}
    state.update_from_message(
        {
            "type": "GameStateType_Diff",
            "gameObjects": [anonymous],
            "zones": [
                {
                    "zoneId": 28,
                    "type": "ZoneType_Battlefield",
                    "ownerSeatId": THEM,
                    "objectInstanceIds": [310, 311],
                }
            ],
        }
    )
    assert state.room_door_token_seen == {310}


def test_an_unrelated_token_does_not_spend_the_door(monkeypatch):
    """A 1/1 Rabbit is nobody's door token; the promise stays live."""
    state = _room_state(monkeypatch)
    state.update_from_message(
        {
            "type": "GameStateType_Diff",
            "gameObjects": [RABBIT_TOKEN],
            "zones": [
                {
                    "zoneId": 28,
                    "type": "ZoneType_Battlefield",
                    "ownerSeatId": THEM,
                    "objectInstanceIds": [310, 312],
                }
            ],
        }
    )
    assert state.room_door_token_seen == set()


def test_the_snapshots_room_flags_reach_the_serialized_board(monkeypatch):
    """server._serialize_snapshot_obj passes the door state through to the threat pass."""
    from arenamcp import server

    state = _room_state(monkeypatch)
    state.update_from_message(
        {
            "type": "GameStateType_Diff",
            "gameObjects": [{**DEMON_TOKEN, "objectSourceGrpId": ROOM_GRP}],
            "zones": [
                {
                    "zoneId": 28,
                    "type": "ZoneType_Battlefield",
                    "ownerSeatId": THEM,
                    "objectInstanceIds": [310, 311],
                }
            ],
        }
    )
    raw = next(o for o in state.get_published_snapshot()["zones"]["battlefield"] if o["instance_id"] == 310)
    serialized = server._serialize_snapshot_obj(raw)
    assert serialized["is_room"] is True
    assert serialized["door_token_seen"] is True
    # And the threat pass agrees: the spent door flags nothing.
    assert _room_door_threats(serialized) == []


def test_unspent_room_serialized_via_snapshot_and_bridge_surfaces_locked_door_threat(monkeypatch):
    # Unspent Room serialized via _serialize_snapshot_obj and _serialize_bridge_card preserves related_faces.
    from arenamcp import server

    state = _room_state(monkeypatch)
    raw = next(o for o in state.get_published_snapshot()["zones"]["battlefield"] if o["instance_id"] == 310)
    snap_ser = server._serialize_snapshot_obj(raw)
    assert snap_ser["related_faces"] == RELATED_FACES
    snap_threats = _threats([snap_ser], opponent=THEM, theirs=[], our_air=False, our_count=0)
    assert any(
        "locked Ritual Chamber: creates 6/6 flying Demon token when unlocked" in t.why for t in snap_threats
    )

    bridge_ser = server._serialize_bridge_card(
        {
            "instance_id": 310,
            "grp_id": ROOM_GRP,
            "controller_id": THEM,
            "card_types": ["CardType_Enchantment"],
        },
        {310: raw},
    )
    assert bridge_ser["is_room"] is True
    assert not bridge_ser.get("door_token_seen")
    assert bridge_ser["related_faces"] == RELATED_FACES
    bridge_threats = _threats([bridge_ser], opponent=THEM, theirs=[], our_air=False, our_count=0)
    assert any(
        "locked Ritual Chamber: creates 6/6 flying Demon token when unlocked" in t.why for t in bridge_threats
    )
