"""Card seats from the native-Mac bridge (mac_game_state -> server publish).

The client links every card's Owner/Controller to the MtgPlayer whose
InstanceId is that seat (GreInterface.cs: InstanceId = SystemSeatNumber,
card.Owner = GetPlayerById(ownerSeatId)). A probe zone op dumps the first
reference to each player in full and later ones as ``{"$h", "$ref": true}``,
so most cards carry only a handle. Before the fix the Mac payload had no
owner_id/controller_id, so seats came only from the Player.log lookup; when
that missed (the log GameState resets at game end) every card was seatless
and board_model gave no board math (14 of 59 bridge-named bug reports).
"""

from __future__ import annotations

from typing import Any

import pytest

import arenamcp.gre_bridge as gre_bridge_module
import arenamcp.server as server_module
from arenamcp.board_model import build_board_model
from arenamcp.mac_game_state import _card_entry, build_state_ops, fetch_game_state

PLAYER = "GreClient.Rules.MtgPlayer"


def enum(name: str, value: int = 0) -> dict:
    return {"e": name, "v": value}


def player_node(seat: int, handle: int, *, controller: int | None = None, local: bool = False) -> dict:
    return {
        "$c": PLAYER,
        "$h": handle,
        "InstanceId": seat,
        "ControllerId": seat if controller is None else controller,
        "ClientPlayerEnum": enum("LocalPlayer" if local else "Opponent"),
        "LifeTotal": 20,
        "Status": enum("InGame"),
    }


class ProbeDump:
    """Reflect-batch results shaped like probe.cpp's encoder output.

    Fresh ``$ref`` set per op; handles global to the batch (handle_for keys the
    object pointer). ``players`` maps seat -> full player node.
    """

    def __init__(self, players: list[dict], *, active_seat: int | None = None):
        self.players = players
        self.by_seat = {p["InstanceId"]: p for p in players}
        self.active_seat = active_seat
        self.zones: dict[str, list[dict]] = {}

    def zone(self, key: str, cards: list[tuple[int, int, int]], **card_fields: Any) -> ProbeDump:
        seen: set[int] = set()

        def ref(seat: int) -> dict:
            # probe.cpp Encoder::object: "$h" is always written (0 without GC handles),
            # then any object already in this op's seen_ set (keyed by its pointer,
            # here its seat) collapses to a "$ref" stub, whether or not it has a handle.
            node = self.by_seat[seat]
            if seat in seen:
                return {"$c": PLAYER, "$h": node["$h"], "$ref": True}
            seen.add(seat)
            return dict(node)

        nodes = []
        for instance_id, owner, controller in cards:
            node = {
                "$c": "GreClient.Rules.MtgCardInstance",
                "$h": 100000 + instance_id,
                "InstanceId": instance_id,
                "BaseGrpId": 7000 + instance_id,
                "ObjectType": enum("Card"),
                **card_fields,
            }
            # MtgCardInstance declares Controller (line 49) before Owner (line 51).
            node["Controller"] = ref(controller)
            node["Owner"] = ref(owner)
            nodes.append(node)
        self.zones[key] = nodes
        return self

    def results(self) -> tuple[list[Any], dict[str, int]]:
        ops, index = build_state_ops()
        results: list[Any] = [None] * len(ops.ops)
        results[index["game_manager"]] = {"$c": "GameManager", "$h": 1}
        results[index["state"]] = {"$c": "GreClient.Rules.MtgGameState", "$h": 2}
        results[index["players"]] = {"$c": "List", "$h": 3, "$items": [dict(p) for p in self.players]}
        results[index["GameWideTurn"]] = 12
        results[index["CurrentPhase"]] = enum("Main1")
        results[index["CurrentStep"]] = enum("None")
        for offset, (key, cards) in enumerate(self.zones.items()):
            results[index[key]] = {
                "$c": "GreClient.Rules.MtgZone",
                "$h": 40 + offset,
                "Id": 28 + offset,
                "VisibleCards": {"$c": "List", "$h": 60 + offset, "$items": cards},
            }
        if self.active_seat is not None:
            active = self.by_seat[self.active_seat]
            results[index["active_player"]] = {"$c": PLAYER, "$h": active["$h"], "$more": True}
        return results, index

    def fetch(self) -> dict:
        results, _ = self.results()
        response = fetch_game_state(lambda command, timeout: {"ok": True, "results": results}, None)
        assert response["ok"], response
        return response


def seats_of(cards: list[dict], owner_key: str = "owner_id", controller_key: str = "controller_id") -> dict:
    return {c["instance_id"]: (c.get(owner_key), c.get(controller_key)) for c in cards}


def publish(
    monkeypatch, bridge_state: dict, log_zones: dict | None = None, local: int = 2, opp: int | None = 1
) -> dict:
    """What server._get_bridge_overlay publishes; ``log_zones`` is the Player.log snapshot."""
    monkeypatch.setattr(server_module, "enrich_with_oracle_text", lambda grp_id: {})

    class Bridge:
        connected = True

        def get_game_state(self):
            return bridge_state

        def get_timer_state(self):
            return {}

    monkeypatch.setattr(gre_bridge_module, "get_bridge", lambda: Bridge())
    return server_module._get_bridge_overlay({}, [], log_zones or {}, local, opp)


# bug_20261006_175501 (Mac bridge, GameOver after their T15 combat): the log
# GameState had reset, every published card was seatless. Seats verified
# against Player.log's gameObjects for match bec097ac. Local seat 2.
BUG_175501 = [
    (221, 1, 1), (223, 2, 2), (225, 1, 1), (226, 1, 1), (230, 2, 2), (236, 1, 1), (251, 2, 2),
    (255, 1, 1), (276, 2, 2), (278, 1, 1), (296, 2, 2), (299, 1, 1), (359, 2, 2), (367, 1, 1),
    (368, 1, 1), (395, 2, 2), (398, 2, 2), (408, 1, 1), (411, 1, 1),
]  # fmt: skip


def real_board() -> ProbeDump:
    players = [player_node(1, 9001), player_node(2, 9002, local=True)]
    return ProbeDump(players, active_seat=1).zone("battlefield", BUG_175501)


def test_every_card_of_a_real_seatless_board_gets_its_seat(monkeypatch):
    bridge = real_board().fetch()
    cards = bridge["zones"]["battlefield"]["cards"]
    # One full dump per player in the op; the other 36 references are $ref stubs.
    refs = [node[member] for node in real_board().zones["battlefield"] for member in ("Owner", "Controller")]
    assert sum("$ref" not in ref for ref in refs) == 2
    truth = {iid: (owner, controller) for iid, owner, controller in BUG_175501}
    assert seats_of(cards) == truth

    published = publish(monkeypatch, bridge)  # log GameState reset: no fallback
    assert seats_of(published["battlefield"], "owner_seat_id", "controller_seat_id") == truth
    assert (published["local_seat_id"], published["opponent_seat_id"]) == (2, 1)
    assert published["turn"]["active_player"] == 1


def test_the_bridge_seat_beats_a_stale_log_seat_and_fills_cards_the_log_missed(monkeypatch):
    # bug_20261004_001406 / _002903: one fresh permanent the log had not seen yet.
    bridge = ProbeDump([player_node(1, 9001, local=True), player_node(2, 9002)]).zone(
        "battlefield", [(500, 1, 1), (501, 2, 2), (502, 2, 2)]
    )
    log_zones = {
        "battlefield": [
            {"instance_id": 500, "owner_seat_id": 1, "controller_seat_id": 1},
            {"instance_id": 501, "owner_seat_id": 2, "controller_seat_id": 1},  # stale
        ]
    }
    published = publish(monkeypatch, bridge.fetch(), log_zones, local=1, opp=2)
    assert seats_of(published["battlefield"], "owner_seat_id", "controller_seat_id") == {
        500: (1, 1),
        501: (2, 2),
        502: (2, 2),
    }


def test_a_stolen_permanent_keeps_owner_and_controller_apart():
    bridge = ProbeDump([player_node(1, 9001, local=True), player_node(2, 9002)]).zone(
        "battlefield", [(600, 2, 1), (601, 1, 2), (602, 2, 2)]
    )
    assert seats_of(bridge.fetch()["zones"]["battlefield"]["cards"]) == {
        600: (2, 1),
        601: (1, 2),
        602: (2, 2),
    }


# bug_20261004_092517: we (seat 2) cast Emrakul, the Promised End and control the
# opponent's T12. The bridge read MtgPlayer.ControllerId as the seat, so both
# players were published as seat 2, active_player 2 and opponent_seat_id None.
BUG_092517 = [
    (446, 2, 2), (448, 1, 1), (450, 2, 2), (451, 2, 2), (457, 1, 1), (458, 1, 1), (465, 2, 2),
    (556, 1, 1), (557, 1, 1), (563, 2, 2), (565, 1, 1), (566, 1, 1), (576, 2, 2), (584, 2, 2),
    (600, 2, 2), (609, 2, 2),
]  # fmt: skip


def test_a_controlled_turn_keeps_each_players_seat(monkeypatch):
    players = [player_node(1, 9001, controller=2), player_node(2, 9002, local=True)]
    bridge = ProbeDump(players, active_seat=1).zone("battlefield", BUG_092517).fetch()
    assert [(p["seat_id"], p["is_local"]) for p in bridge["players"]] == [(1, False), (2, True)]
    assert bridge["turn"]["active_player"] == 1
    assert seats_of(bridge["zones"]["battlefield"]["cards"]) == {i: (o, c) for i, o, c in BUG_092517}

    published = publish(monkeypatch, bridge, local=2, opp=None)
    assert (published["local_seat_id"], published["opponent_seat_id"]) == (2, 1)
    assert sorted(p["seat_id"] for p in published["players"]) == [1, 2]


def test_controller_ids_stay_the_seats_when_instance_ids_are_not():
    # A dump whose InstanceIds do not name the ControllerIds (tests/test_mac_game_state's
    # fixture shape) keeps the plugin's ControllerId seats; cards follow them.
    players = [player_node(51, 9001, controller=1, local=True), player_node(52, 9002, controller=2)]
    bridge = ProbeDump(players).zone("battlefield", [(700, 51, 51), (701, 52, 52), (702, 51, 52)]).fetch()
    assert [p["seat_id"] for p in bridge["players"]] == [1, 2]
    assert seats_of(bridge["zones"]["battlefield"]["cards"]) == {700: (1, 1), 701: (2, 2), 702: (1, 2)}


def test_an_unresolved_reference_leaves_the_player_log_seat(monkeypatch):
    players = [player_node(1, 9001, local=True), player_node(2, 9002)]
    dump = ProbeDump(players).zone("battlefield", [(800, 1, 1), (801, 2, 2)])
    for node in dump.zones["battlefield"]:
        node["Owner"] = node["Controller"] = {"$c": PLAYER, "$h": 4242, "$ref": True}  # unknown handle
    cards = dump.fetch()["zones"]["battlefield"]["cards"]
    assert all("owner_id" not in c and "controller_id" not in c for c in cards)

    log_zones = {"battlefield": [{"instance_id": 800, "owner_seat_id": 1, "controller_seat_id": 1}]}
    published = publish(monkeypatch, dump.fetch(), log_zones, local=1, opp=2)
    assert seats_of(published["battlefield"], "owner_seat_id", "controller_seat_id") == {
        800: (1, 1),
        801: (None, None),
    }


def test_a_dump_without_handles_never_merges_the_players():
    # handle_for returns 0 when the probe has no GC handles: "$h": 0 identifies nothing.
    # The encoder still collapses a repeated player to {"$h": 0, "$ref": true}
    # (review 2026-10-07): such a seat is left to the Player.log fallback, never guessed.
    players = [player_node(1, 0, local=True), player_node(2, 0)]
    dump = ProbeDump(players, active_seat=2).zone("battlefield", [(900, 1, 1), (901, 2, 2)])
    assert [node["Owner"] for node in dump.zones["battlefield"]] == [
        {"$c": PLAYER, "$h": 0, "$ref": True}
    ] * 2  # Controller came first in full
    bridge = dump.fetch()
    assert bridge["turn"]["active_player"] == 0  # unknown, not the last player's seat
    cards = bridge["zones"]["battlefield"]["cards"]
    assert seats_of(cards) == {900: (None, 1), 901: (None, 2)}
    assert all("owner_id" not in card for card in cards)  # no key: the log seat stays

    collapsed = ProbeDump(players).zone("battlefield", [(902, 1, 1)])
    collapsed.zones["battlefield"][0]["Owner"] = {"$c": PLAYER, "$h": 0, "$ref": True}
    card = collapsed.fetch()["zones"]["battlefield"]["cards"][0]
    assert "owner_id" not in card and card["controller_id"] == 1


def test_card_entry_without_players_gives_no_seat_keys():
    node = real_board().zones["battlefield"][0]
    entry = _card_entry(node)
    assert "owner_id" not in entry and "controller_id" not in entry
    # Unchanged entity ids: a $ref stub has no InstanceId, so they alone never give seats.
    assert (entry["controller_entity_id"], entry["owner_entity_id"]) == (1, 0)


@pytest.mark.parametrize("seat_keys", [True, False])
def test_windows_payload_is_published_as_before(monkeypatch, seat_keys):
    card = {"instance_id": 501, "grp_id": 1001, "object_type": "Card"}
    if seat_keys:
        card.update(owner_id=2, controller_id=2)
    bridge_state = {
        "ok": True,
        "players": [{"seat_id": 1, "is_local": True}, {"seat_id": 2, "is_local": False}],
        "zones": {"battlefield": {"cards": [card]}},
    }
    log_zones = {"battlefield": [{"instance_id": 501, "owner_seat_id": 1, "controller_seat_id": 1}]}
    published = publish(monkeypatch, bridge_state, log_zones, local=1, opp=2)
    expected = (2, 2) if seat_keys else (1, 1)
    assert seats_of(published["battlefield"], "owner_seat_id", "controller_seat_id") == {501: expected}


def test_the_seated_board_gives_board_math_again(monkeypatch):
    power = {"RawText": "2"}
    dump = ProbeDump([player_node(1, 9001, local=True), player_node(2, 9002)], active_seat=1).zone(
        "battlefield", [(1000, 1, 1), (1001, 2, 2), (1002, 2, 2)], Power=power, Toughness=power
    )
    monkeypatch.setattr(server_module, "enrich_with_oracle_text", lambda grp_id: {})
    bridge = dump.fetch()
    for card in bridge["zones"]["battlefield"]["cards"]:
        card["card_types"] = ["CardType_Creature"]
    state = publish(monkeypatch, bridge, local=1, opp=2)
    model = build_board_model(state)
    assert model is not None
    assert (len(model.ours), len(model.theirs)) == (1, 2)

    for card in bridge["zones"]["battlefield"]["cards"]:  # the payload before the fix
        card.pop("owner_id"), card.pop("controller_id")
    assert build_board_model(dict(publish(monkeypatch, bridge, local=1, opp=2), turn=state["turn"])) is None
