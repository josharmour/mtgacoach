"""Tests for the native-Mac get_game_state reflect-op implementation."""

from __future__ import annotations

from typing import Any

from arenamcp.mac_game_state import (
    ZONE_GETTERS,
    assemble_state,
    build_state_ops,
    fetch_game_state,
)

MSG = "Wotc.Mtgo.Gre.External.Messaging."


def enum(name: str, value: int = 0) -> dict:
    return {"e": name, "v": value}


def listing(*values: Any) -> dict:
    return {"$c": "System.Collections.Generic.List<X>", "$h": 900, "$n": len(values), "$items": list(values)}


def sbi(value: int) -> dict:
    """StringBackedInt dump: struct with DefinedValue Nullable<int>."""
    return {"$c": "StringBackedInt", "$struct": True, "rawText": str(value), "definedValue": {"$": value}}


def nullable(value: int | None) -> Any:
    if value is None:
        return None
    return {"$c": "System.Nullable`1[System.UInt32]", "$struct": True, "hasValue": True, "value": value}


def card(instance: int, grp: int = 700, **extra: Any) -> dict:
    base = {
        "$c": "GreClient.Rules.MtgCardInstance",
        "$h": instance,
        "instanceId": instance,
        "baseGrpId": grp,
        "overlayGrpId": None,
        "objectType_": enum("Card"),
        "isTapped": False,
        "hasSummoningSickness": False,
        "attackState_": enum("None"),
        "blockState_": enum("None"),
        "attackTargetId": 0,
        "blockedByIds": listing(),
        "blockingIds": listing(),
        "damage": 0,
        "isDamagedThisTurn": False,
        "classLevel": 0,
        "isCopy": False,
        "copyObjectGrpId": 0,
        "cardTypes_": listing(enum("Creature")),
        "subtypes_": listing(enum("Human")),
        "colors_": listing(enum("White")),
        "counterDatas_": listing(),
        "colorProduction_": listing(),
        "targetIds_": listing(),
        "attachedToId": 0,
        "attachedWithIds_": listing(),
        "revealedToOpponent": False,
        "power": sbi(2),
        "toughness": sbi(2),
        "loyalty": None,
        "defense": None,
        "titleId": 0,
        "visibility_": enum("Public"),
        # Owner/Controller stubs (embedded expansions pruned by skip lists).
        "owner_": {"$c": "GreClient.Rules.MtgPlayer", "$h": 201, "instanceId": 51},
        "controller_": {"$c": "GreClient.Rules.MtgPlayer", "$h": 201, "instanceId": 51},
        # Zone back-pointer (skipped in real ops; present here for realism).
        "zone_": None,
        "faceDownState_": {
            "$c": "GreClient.CardData.FaceDownState",
            "$h": instance + 5000,
            "_reasonFaceDown_": enum("None"),
            "_reasonFaceDownSourceAbilityGrpId": 0,
            "_manaCost": -1,
            "_wasTurnedFaceUpFrom_": enum("None"),
            "_mutatedOverFaceDownReasonFaceDown_": enum("None"),
            "_mutatedOverFaceDownSourceAbilityGrpId": 0,
            "_copiedCardsReasonFaceDown_": enum("None"),
            "_copiedCardsReasonFaceDownSourceAbilityGrpId": 0,
        },
    }
    base.update(extra)
    return base


def zone(zone_id: int, zone_type: str, *cards: dict, card_ids: list[int] | None = None) -> dict:
    return {
        "$c": "GreClient.Rules.MtgZone",
        "$h": zone_id + 100,
        "type_": enum(zone_type),
        "id_": zone_id,
        "cardIds_": listing(*(card_ids or [c["instanceId"] for c in cards])),
        "visibleCards_": listing(*cards),
    }


def player(seat: int, local: bool = False, life: int = 20, **extra: Any) -> dict:
    base = {
        "$c": "GreClient.Rules.MtgPlayer",
        "$h": seat + 200,
        "instanceId": seat + 50,
        "controllerId_": seat,
        "clientPlayerEnum_": enum("LocalPlayer" if local else "Opponent"),
        "lifeTotal_": life,
        "startingLifeTotal_": life,
        "maxHandSize_": 7,
        "status_": enum("Ready"),
        "mulliganCount_": 0,
        "timeoutCount_": 0,
        "manaPool_": listing(),
        "commanderIds_": listing(),
        "dungeonState_": {
            "$c": "GreClient.Rules.DungeonData",
            "$struct": True,
            "dungeonGrpId_": 0,
            "dungeonInstanceId_": 0,
            "currentRoomGrpId_": 0,
            "completedDungeons_": listing(),
        },
    }
    base.update(extra)
    return base


def game_manager(state_handle: int = 77) -> dict:
    return {"$c": "Core.GameManager", "$h": 10}


def state_node(handle_id: int = 77) -> dict:
    return {"$c": "GreClient.Rules.MtgGameState", "$h": handle_id}


class FakeProbe:
    """Simulates probe.cpp run_batch semantics for the state batch.

    Fresh encoder per op; ``get`` resolves property getters OR fields by C#
    name (camelCase backing fields in these fixtures); ``find`` returns the
    scripted GameManager; ``$ref`` collapse is modeled by returning the same
    node object (assembly keys on ``$h``, not identity).
    """

    def __init__(self, manager: dict | None, zones: dict[str, Any], players: list[dict], scalars: dict[str, Any]):
        self.manager = manager
        self.zones = zones
        self.players = players
        self.scalars = scalars
        self.batches: list[list[dict]] = []

    def send(self, command: dict, timeout: float | None) -> dict:
        assert command["action"] == "reflect_batch"
        ops = command["ops"]
        self.batches.append(ops)
        results: list[Any] = []
        objects: list[Any] = []
        for op in ops:
            kind = op["op"]
            if kind == "find":
                results.append(self.manager)
                objects.append(self.manager)
                continue
            target_idx = op.get("target", {}).get("ref")
            target = objects[target_idx] if target_idx is not None and target_idx < len(objects) else None
            member = op.get("member", "")
            if kind == "get":
                if target is self.manager and member == "CurrentGameState":
                    node = self.scalars.get("__state__")
                    results.append(node)
                    objects.append(node)
                    continue
                if target is self.scalars.get("__state__"):
                    if member in [g for g, _ in ZONE_GETTERS]:
                        node = self.zones.get(member)
                        results.append(node)
                        objects.append(node)
                        continue
                    if member == "Players":
                        node = listing(*self.players)
                        results.append(node)
                        objects.append(node)
                        continue
                    if member in ("ActivePlayer", "DecidingPlayer"):
                        node = self.scalars.get(member.lower())
                        results.append(node)
                        objects.append(node)
                        continue
                    if member in self.scalars:
                        results.append(self.scalars[member])
                        objects.append(None)
                        continue
                results.append(None)
                objects.append(None)
            else:
                results.append(None)
                objects.append(None)
        return {"ok": True, "results": results}


def scripted_probe() -> tuple[FakeProbe, dict[str, Any]]:
    """A two-player board with one attacking creature and a tapped land."""
    local = player(1, local=True, life=17)
    opp = player(2, life=22)
    bear = card(300, grp=12345, attackState_=enum("Declared"), attackTargetId=2)
    land = card(301, grp=75556, isTapped=True)
    bf = zone(31, "Battlefield", bear, land)
    stack_z = zone(28, "Stack")
    hand = zone(33, "Hand", card(303, grp=555))
    opp_hand = zone(34, "Hand")
    lg = zone(35, "Graveyard")
    og = zone(36, "Graveyard")
    exile_z = zone(37, "Exile")
    command_z = zone(38, "Command")
    zones = {
        "Battlefield": bf,
        "Stack": stack_z,
        "LocalHand": hand,
        "OpponentHand": opp_hand,
        "LocalGraveyard": lg,
        "OpponentGraveyard": og,
        "Exile": exile_z,
        "Command": command_z,
    }
    scalars = {
        "__state__": state_node(77),
        "Id": 40,
        "GameWideTurn": 3,
        "CurrentPhase": enum("Combat"),
        "CurrentStep": enum("DeclareAttack"),
        "Stage": enum("Play"),
        "activeplayer": {"$c": "GreClient.Rules.MtgPlayer", "$h": 201},
        "decidingplayer": {"$c": "GreClient.Rules.MtgPlayer", "$h": 201},
    }
    probe = FakeProbe(game_manager(77), zones, [local, opp], scalars)
    expected = {
        "turn": {
            "turn_number": 3,
            "phase": "Combat",
            "step": "DeclareAttack",
            "stage": "Play",
            "game_state_id": 40,
            "active_player": 1,
            "deciding_player": 1,
        },
        "players": [
            {"seat_id": 1, "life_total": 17, "is_local": True, "status": "Ready",
             "mulligan_count": 0, "timeout_count": 0, "entity_id": 51},
            {"seat_id": 2, "life_total": 22, "is_local": False, "status": "Ready",
             "mulligan_count": 0, "timeout_count": 0, "entity_id": 52},
        ],
    }
    return probe, expected


def test_build_state_ops_single_batch():
    ops, index = build_state_ops()
    kinds = [op["op"] for op in ops.ops]
    assert kinds[0] == "find"
    assert kinds[1] == "get"
    # Every zone getter targets the state by ref, not by handle.
    zone_ops = [op for op in ops.ops if op.get("member") in [g for g, _ in ZONE_GETTERS]]
    assert len(zone_ops) == len(ZONE_GETTERS)
    assert all(op["target"]["ref"] == index["state"] for op in zone_ops)
    assert index["battlefield"] == 2
    assert index["players"] == 2 + len(ZONE_GETTERS)


def test_full_board_assembly():
    probe, expected = scripted_probe()
    response = fetch_game_state(probe.send, None)
    assert response["ok"] is True
    assert response["turn"] == expected["turn"]
    players = response["players"]
    assert len(players) == 2
    for got, want in zip(players, expected["players"], strict=True):
        for key, value in want.items():
            assert got[key] == value, f"{key}: {got.get(key)!r} != {value!r}"
    zones = response["zones"]
    assert set(zones) == {key for _, key in ZONE_GETTERS}
    bf = zones["battlefield"]
    assert bf["zone_id"] == 31
    instances = [c["instance_id"] for c in bf["cards"]]
    assert instances == [300, 301]
    bear = bf["cards"][0]
    assert bear["grp_id"] == 12345
    assert bear["is_attacking"] is True
    assert bear["attack_target_id"] == 2
    assert bear["power"] == 2 and bear["toughness"] == 2
    land = bf["cards"][1]
    assert land["is_tapped"] is True
    hand = zones["local_hand"]
    assert hand["cards"][0]["grp_id"] == 555
    # Combat maps derived from card state.
    assert response["attack_info"] == {"300": "2"}


def test_no_game_returns_plugin_error_shape():
    probe = FakeProbe(None, {}, [], {})
    response = fetch_game_state(probe.send, None)
    assert response == {"ok": False, "error": "GameManager not found"}


def test_no_active_state_returns_plugin_error_shape():
    probe = FakeProbe(game_manager(), {}, [], {})
    response = fetch_game_state(probe.send, None)
    assert response == {"ok": False, "error": "No active game state"}


def test_missing_zone_is_absent_not_fatal():
    probe, _ = scripted_probe()
    probe.zones["Command"] = None
    response = fetch_game_state(probe.send, None)
    assert response["ok"] is True
    assert "command" not in response["zones"]
    assert "battlefield" in response["zones"]


def test_counters_and_attachments_shaped():
    local = player(1, local=True)
    opp = player(2)
    buffed = card(
        400,
        grp=600,
        counterDatas_=listing({"$c": "GreClient.Rules.CounterData", "$struct": True, "type_": enum("+1/+1"), "count_": 2}),
        attachedToId=401,
        attachedWithIds_=listing(),
        targetIds_=listing(402),
        damage=3,
        isDamagedThisTurn=True,
        hasSummoningSickness=True,
        faceDownState_={"$c": "GreClient.CardData.FaceDownState", "$h": 5400, "reasonFaceDown_": enum("Morph")},
    )
    aura = card(401, grp=601)
    bf = zone(31, "Battlefield", buffed, aura)
    zones = {name: (bf if name == "Battlefield" else zone(90, "Stack")) for name, _ in ZONE_GETTERS}
    scalars = {
        "__state__": state_node(7),
        "Id": 9,
        "GameWideTurn": 1,
        "CurrentPhase": enum("Main1"),
        "CurrentStep": enum("None"),
        "Stage": enum("Play"),
        "activeplayer": None,
        "decidingplayer": None,
    }
    probe = FakeProbe(game_manager(), zones, [local, opp], scalars)
    response = fetch_game_state(probe.send, None)
    cards = {c["instance_id"]: c for c in response["zones"]["battlefield"]["cards"]}
    buffed_row = cards[400]
    assert buffed_row["counters"] == {"+1/+1": 2}
    assert buffed_row["attached_to_id"] == 401
    assert buffed_row["damage"] == 3
    assert buffed_row["damaged_this_turn"] is True
    assert buffed_row["summoning_sickness"] is True
    assert buffed_row["face_down"] is True


