"""Private Coach path from sanitized Nov 14 GRE initial/draw shapes to local speech."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from arenamcp.commentary.config import CommentaryConfig
from arenamcp.commentary.library_inventory import DrawInventory
from arenamcp.commentary.private_outs import private_removal_fact, private_removal_line
from arenamcp.gamestate import GameState


def card(gid, name, type_line, oracle):
    return dict(grp_id=gid, name=name, type_line=type_line, mana_cost="{1}", oracle_text=oracle)


CARDS = {
    15: card(15, "Plummet", "Instant", "Destroy target creature with flying."),
    16: card(16, "Forest", "Basic Land — Forest", ""),
    17: card(17, "Combat Trick", "Instant", "Target creature gains reach until end of turn."),
    18: card(18, "Strong Bolt", "Instant", "Strong Bolt deals 3 damage to any target."),
    19: card(19, "Weak Bolt", "Instant", "Weak Bolt deals 1 damage to any target."),
}
DECK = [15] * 4 + [16] * 50 + [17] * 2 + [18] * 3 + [19]
IDS = list(range(101, 161))


def full():
    return {
        "type": "GameStateType_Full",
        "gameStateId": 1,
        "gameInfo": {"matchID": "match", "gameNumber": 1, "stage": "GameStage_Start"},
        "zones": [
            {"zoneId": 31, "ownerSeatId": 1, "type": "ZoneType_Hand"},
            {"zoneId": 32, "ownerSeatId": 1, "type": "ZoneType_Library", "objectInstanceIds": IDS},
        ],
    }


def draw_opening():
    drawn = [15] + [16] * 6
    return {
        "type": "GameStateType_Diff",
        "gameStateId": 2,
        "prevGameStateId": 1,
        "zones": [
            {"zoneId": 32, "ownerSeatId": 1, "type": "ZoneType_Library", "objectInstanceIds": IDS[7:]},
            {"zoneId": 31, "ownerSeatId": 1, "type": "ZoneType_Hand", "objectInstanceIds": IDS[:7]},
        ],
        "gameObjects": [
            dict(
                instanceId=i,
                grpId=gid,
                overlayGrpId=gid,
                zoneId=31,
                ownerSeatId=1,
                controllerSeatId=1,
                type="GameObjectType_Card",
                visibility="Visibility_Private",
                viewers=[1],
            )
            for i, gid in zip(IDS[:7], drawn, strict=True)
        ],
    }


def next_draw():
    msg = draw_opening()
    msg.update(gameStateId=3, prevGameStateId=2)
    msg["zones"][0]["objectInstanceIds"] = IDS[8:]
    msg["zones"][1]["objectInstanceIds"] = [*IDS[:7], 500]
    msg["gameObjects"] = [dict(msg["gameObjects"][0], instanceId=500, grpId=15, overlayGrpId=15)]
    msg["annotations"] = [
        {
            "type": ["AnnotationType_ObjectIdChanged"],
            "affectedIds": [IDS[7]],
            "details": [{"key": "orig_id", "valueInt32": [IDS[7]]}, {"key": "new_id", "valueInt32": [500]}],
        },
        {
            "type": ["AnnotationType_ZoneTransfer"],
            "affectedIds": [500],
            "details": [
                {"key": "category", "valueString": ["Draw"]},
                {"key": "zone_src", "valueInt32": [32]},
                {"key": "zone_dest", "valueInt32": [31]},
            ],
        },
    ]
    return msg


def connection():
    return {"match_id": "match", "local_seat_id": 1, "seat_source": 2, "deck_cards": list(DECK)}


def opened():
    game = GameState()
    game.set_local_seat_id(1, source=2)
    game.deck_cards = list(DECK)
    game._awaiting_connection_match_id = "match"
    game.capture_connection_deck()
    game.update_from_message(full())
    opening = draw_opening()
    opening["gameObjects"].append(
        {
            "instanceId": 800,
            "grpId": 250,
            "zoneId": 28,
            "ownerSeatId": 2,
            "controllerSeatId": 2,
            "type": "GameObjectType_Card",
            "visibility": "Visibility_Public",
            "cardTypes": ["CardType_Creature"],
        }
    )
    opening["zones"].append({"zoneId": 28, "type": "ZoneType_Battlefield", "objectInstanceIds": [800]})
    game.update_from_message(opening)
    # Public board proof arrives in the same GRE message as the draw on the
    # replayed path. Populate its parser fields, without using the card DB.
    return game


def board():
    flyer = {
        **card(250, "Sky Guard", "Creature — Bird", "Flying"),
        "instance_id": 800,
        "controller_seat_id": 2,
        "owner_seat_id": 2,
        "power": 2,
        "toughness": 3,
        "keywords": ["flying"],
    }
    return {
        "match_id": "match",
        "game_number": 1,
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "_log_game_state_id": 2,
        "zones": {"battlefield": [flyer], "stack": []},
        "turn": {"turn_number": 2},
        "players": [{"seat_id": 1, "is_local": True}, {"seat_id": 2}],
    }


def project(s, g, allow=True):
    return private_removal_fact(s, g, allow_private=allow, card_lookup=CARDS.get)


def test_opening_then_renamed_draw_exact_private_named_answers():
    """100 Nov 14 archives had the same 7-ID opening draw and normal later draws reassign IDs."""
    g = opened()
    s = board()
    f = project(s, g)
    assert (f["remaining"], f["answers"]) == (53, {"Plummet": 3, "Strong Bolt": 3})
    assert private_removal_line(f, s) == (
        800,
        "Against Sky Guard, your 53-card library still has 3 copies of Plummet, 3 copies of Strong Bolt for creature removal.",
    )
    assert "outs" not in private_removal_line(f, s)[1]
    g.update_from_message(next_draw())
    assert project(s, g) is None
    s["_log_game_state_id"] = 3
    f = project(s, g)
    assert (f["remaining"], f["answers"]) == (52, {"Plummet": 2, "Strong Bolt": 3})
    assert "_draw_inventory" not in g.get_published_snapshot()
    assert "private_library_removal" not in s


@pytest.mark.parametrize(
    "change",
    [
        {"match_id": "old"},
        {"game_number": 2},
        {"local_seat_id": 2},
        {"_log_game_state_id": 99},
        {"_bridge_connected": True, "_bridge_game_state_id": 3},
    ],
)
def test_stale_or_foreign_boundaries(change):
    g = opened()
    s = board()
    s.update(change)
    assert project(s, g) is None


@pytest.mark.parametrize(
    "change",
    [
        {"face_down": True},
        {"keywords": ["hexproof", "flying"]},
        {"counters": {"Shield": 1}},
        {"oracle_text": "Flying\nWard {2}"},
        {"is_phased_out": True},
        {"attached_to_id": 901},
        {"power": 0},
        {"granted_abilities": ["indestructible"]},
        {"visibility": "Visibility_Private"},
    ],
)
def test_hidden_or_conditional_threat_declines(change):
    s = board()
    s["zones"]["battlefield"][0].update(change)
    assert project(s, opened()) is None


def test_unsupported_inventory_never_yields_zero_or_guessed_answer():
    g = opened()
    s = board()
    assert not private_removal_fact(s, g, allow_private=True, card_lookup=lambda _: CARDS[17])
    assert not project(s, g, False)
    s["zones"]["battlefield"].append(dict(s["zones"]["battlefield"][0], instance_id=801))
    assert not project(s, g)


@pytest.mark.parametrize(
    "scenario",
    [
        "gap",
        "new member",
        "return",
        "missing conversion",
        "wrong seat",
        "copy",
        "previous id mismatch",
        "wrong viewer",
        "wrong source",
        "missing objects",
        "duplicate id",
        "deleted live id",
        "malformed zone list",
        "different zone",
        "released without draw",
    ],
)
def test_unsupported_transition_abstains(scenario):
    t = DrawInventory.start(full(), connection())
    assert t.advance(draw_opening())
    m = next_draw()
    if scenario == "gap":
        m["gameStateId"] = 4
    elif scenario == "new member":
        m["zones"][0]["objectInstanceIds"] = [*IDS[8:], 999]
    elif scenario == "return":
        m["zones"][0]["objectInstanceIds"] = [*IDS[8:], IDS[0]]
    elif scenario == "missing conversion":
        m["annotations"] = m["annotations"][1:]
    elif scenario == "wrong seat":
        m["gameObjects"][0]["ownerSeatId"] = 2
    elif scenario == "copy":
        m["gameObjects"][0]["overlayGrpId"] = 17
    elif scenario == "previous id mismatch":
        m["prevGameStateId"] = 1
    elif scenario == "wrong viewer":
        m["gameObjects"][0]["viewers"] = [2]
    elif scenario == "wrong source":
        m["annotations"][1]["details"][1]["valueInt32"] = [55]
    elif scenario == "missing objects":
        m["gameObjects"] = []
    elif scenario == "duplicate id":
        m["zones"][0]["objectInstanceIds"] = [*IDS[8:], IDS[-1]]
    elif scenario == "deleted live id":
        m["diffDeletedInstanceIds"] = [IDS[-1]]
    elif scenario == "malformed zone list":
        m["zones"] = m["zones"][0]
    elif scenario == "different zone":
        m["zones"][0]["zoneId"] = 98
    elif scenario == "released without draw":
        m["annotations"] = m["annotations"][:1]
    before = (t.members, t.state_id, t.remaining.copy())
    assert not t.advance(m)
    assert (t.members, t.state_id, t.remaining) == before


def test_game_bound_seed_reconnect_and_summarized_window():
    for changes in (
        {"seat_source": 1},
        {"local_seat_id": 2},
        {"match_id": "previous"},
        {"deck_cards": DECK[:-1]},
    ):
        assert DrawInventory.start(full(), {**connection(), **changes}) is None
    g = opened()
    g.log_summarized_at_gsid = 2
    assert project(board(), g) is None
    g.log_summarized_at_gsid = -1
    g.capture_connection_deck()
    assert project(board(), g) is None


def test_real_pipe_only_passes_fact_to_desktop_with_explicit_permission(monkeypatch):
    from arenamcp import match_context
    from arenamcp.pipe_adapter import PipeAdapter

    p = PipeAdapter()
    p._commentary = Mock()
    p._emit = Mock()
    p._coach = Mock()
    p._coach._autopilot = None
    p._coach.settings = {"autoplay_speech": "commentary", "commentary_private_information": True}
    p._coach._mcp._server.game_state = opened()
    monkeypatch.setattr("arenamcp.commentary.context.deck_overview", lambda *_: None)
    monkeypatch.setattr(match_context, "prepared_card", CARDS.get)
    s = board()
    p.emit_game_state(s)
    event = p._emit.call_args.args[0]
    assert event["private_library_removal"]["answers"] == {"Plummet": 3, "Strong Bolt": 3}
    assert event["data"] is s
    p._commentary.offer.assert_called_once_with(s)
    assert "private_library_removal" not in s
    p._coach.settings["commentary_private_information"] = False
    p.emit_game_state(s)
    assert "private_library_removal" not in p._emit.call_args.args[0]


def test_private_only_local_voice_no_history_cancel_on_revocation(qapp, monkeypatch):
    from PySide6.QtCore import QObject, Signal

    from arenamcp.desktop import commentary_controller

    class Tts(QObject):
        speechStatus = Signal(str, str)

    tts = Tts()
    tts.request_speech = Mock()
    tts.stop_speech = Mock()
    spoken = Mock()
    sent = Mock()
    settings = {"tts_provider": "kokoro"}
    monkeypatch.setattr(commentary_controller, "get_settings", lambda: settings)
    c = commentary_controller.CommentaryController(tts, sent, spoken)
    s = board()
    f = project(s, opened())
    c.configure(CommentaryConfig(mode="commentary", private_information=True).to_wire())
    try:
        c.observe(s, private_removal=f)
        call = tts.request_speech.call_args.kwargs
        assert call["provider"] == "kokoro"
        assert call["valid"]()
        assert "Plummet" in call["text"]
        spoken.assert_not_called()
        sent.assert_not_called()
        c.observe(s)
        assert not call["valid"]()
        tts.stop_speech.assert_called_once()
        c.observe(s, private_removal=f)
        assert tts.request_speech.call_count == 1
        c.configure(CommentaryConfig(mode="commentary", private_information=False).to_wire())
        assert not call["valid"]()
        c.observe(s, private_removal=f)
        assert tts.request_speech.call_count == 1
        c.configure(CommentaryConfig(mode="commentary", private_information=True).to_wire())
        settings["tts_provider"] = "chatterbox"
        s2 = deepcopy(s)
        s2["match_id"] = "new"
        c.observe(s2, private_removal={**f, "match_id": "new"})
        assert tts.request_speech.call_count == 1
    finally:
        c.close()
