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
        "Against Sky Guard, your 53-card library includes 3 copies of Plummet, 3 copies of Strong Bolt as potential removal.",
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


def test_real_pipe_supplies_verified_local_outs_whenever_announcers_are_enabled(monkeypatch):
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
    inventory = event["commentary_library_inventory"]
    assert sum(row["count"] for row in inventory) == event["private_library_removal"]["remaining"]
    p._commentary.offer.assert_called_once_with({**s, "commentary_library_inventory": inventory})
    assert "private_library_removal" not in s
    p._coach.settings["autoplay_speech"] = "actions"
    p.emit_game_state(s)
    assert "private_library_removal" not in p._emit.call_args.args[0]


def test_private_only_local_voice_no_history_cancel_on_revocation(qapp, monkeypatch):
    from PySide6.QtCore import QObject, Signal

    from arenamcp.desktop import commentary_controller

    class Tts(QObject):
        speechStatus = Signal(str, str)
        private_render_ready = True

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
        c.arbiter.set_muted(True)
        c.observe(s, private_removal=f)
        tts.request_speech.assert_not_called()
        assert not c.private_removal_played
        c.arbiter.set_muted(False)
        c.observe(s, private_removal=f)
        call = tts.request_speech.call_args.kwargs
        assert call["provider"] == "kokoro"
        assert call["private"] is True
        assert c.arbiter.active.role == "play_by_play"
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


@pytest.mark.parametrize(
    "change", ["identity-at-seed", "overlapping-zone-at-seed", "deleted-at-seed", "renamed-at-seed"]
)
def test_seed_refuses_contradictory_full_envelope(change):
    m = full()
    if change == "identity-at-seed":
        m["gameObjects"] = [{"instanceId": IDS[-1], "grpId": 15, "zoneId": 32}]
    elif change == "overlapping-zone-at-seed":
        m["zones"].append({"zoneId": 27, "type": "ZoneType_Battlefield", "objectInstanceIds": [IDS[-1]]})
    elif change == "deleted-at-seed":
        m["diffDeletedInstanceIds"] = [IDS[-1]]
    elif change == "renamed-at-seed":
        m["annotations"] = [
            {
                "type": ["AnnotationType_ObjectIdChanged"],
                "affectedIds": [IDS[-1]],
                "details": [
                    {"key": "orig_id", "valueInt32": [IDS[-1]]},
                    {"key": "new_id", "valueInt32": [999]},
                ],
            }
        ]
    assert DrawInventory.start(m, connection()) is None


def test_same_member_rewritten_without_overlay_abstains_until_new_game():
    g = opened()
    assert project(board(), g)
    for gsid, gid in [(3, 15), (4, 17)]:
        g.update_from_message(
            {
                "type": "GameStateType_Diff",
                "gameStateId": gsid,
                "prevGameStateId": gsid - 1,
                "gameObjects": [
                    {
                        "instanceId": IDS[-1],
                        "zoneId": 32,
                        "grpId": gid,
                        "ownerSeatId": 1,
                        "visibility": "Visibility_Private",
                        "viewers": [1],
                    }
                ],
            }
        )
        s = board()
        s["_log_game_state_id"] = gsid
        assert not project(s, g)


def test_public_gre_object_identity_required_even_when_overlay_omits_visibility():
    g = opened()
    s = board()
    g.game_objects[800].visibility = "Visibility_Private"
    assert not project(s, g)
    g.game_objects[800].visibility = "Visibility_Public"
    g.game_objects[800].grp_id = 251
    assert not project(s, g)


@pytest.mark.parametrize(
    "oracle",
    [
        "Prevent all damage that would be dealt to creatures.",
        "Creatures your opponents control get +0/+2.",
        "Creatures you control get +1/+1.",
    ],
)
def test_continuous_effect_rejects_candidate_except_with_proven_current_applicability(oracle):
    s = board()
    s["zones"]["battlefield"].append(
        {**card(261, "Other", "Enchantment", oracle), "instance_id": 812, "controller_seat_id": 1}
    )
    g = Mock()
    assert not project(s, g)
    g.commentary_removal_census.assert_not_called()


def test_burn_only_when_shown_effective_toughness_is_within_printed_amount():
    s = board()
    s["zones"]["battlefield"][0]["modified_toughness"] = 4
    g = opened()
    assert project(s, g) is None
    g.game_objects[800].modified_toughness = 4
    assert project(s, g)["answers"] == {"Plummet": 3}


def test_desktop_session_does_not_republish_private_count_as_shared_game_state(qapp):
    from arenamcp.desktop.coach_session import CoachSession

    s = board()
    event = {"type": "game_state", "data": s, "private_library_removal": project(s, opened())}
    session = Mock()
    CoachSession._handle_process_event(session, event)
    session._speech.observe.assert_called_once_with(s, private_removal=event["private_library_removal"])
    session.gameStateChanged.emit.assert_called_once_with(s)
    assert session._last_game_state is s
    assert "private_library_removal" not in s


def test_tts_manager_requires_capable_local_worker_and_skips_os_fallback(qapp, monkeypatch):
    import json

    from PySide6.QtCore import QProcess

    from arenamcp.desktop.tts_manager import TtsManager

    manager = TtsManager()
    process = Mock()
    process.state.return_value = QProcess.Running
    manager._process = process
    manager._speak_via_say = Mock()
    manager._speak_fallback = Mock()
    manager._request_remote_speech = Mock()
    manager._request_chatterbox_speech = Mock()
    valid = True
    manager._handle_stdout_line('{"type":"ready"}')
    assert not manager.private_render_ready
    args = dict(
        text="PRIVATE_SENTINEL",
        voice_id="am_adam",
        voice_name="Adam",
        speed=1.0,
        speech_id="private-test",
        category="commentary",
        provider="kokoro",
        private=True,
        valid=lambda: valid,
    )
    manager.request_speech(**args)
    process.write.assert_not_called()
    manager._handle_stdout_line('{"type":"ready","private_render":true}')
    assert manager.private_render_ready
    for provider in (None, "chatterbox", "remote"):
        manager.request_speech(**{**args, "provider": provider})
    process.write.assert_not_called()
    manager._busy = True
    manager.request_speech(**args)
    valid = False
    manager._busy = False
    manager._dispatch_pending()
    process.write.assert_not_called()
    valid = True
    manager.request_speech(**args)
    request = json.loads(process.write.call_args.args[0])
    assert request["private"] is True and request["text"] == "PRIVATE_SENTINEL"
    assert manager._last_text == ""
    manager._handle_stdout_line(
        json.dumps(
            {"type": "error", "generation": request["generation"], "message": "Private Kokoro render failed."}
        )
    )
    manager._speak_via_say.assert_not_called()
    manager._speak_fallback.assert_not_called()
    manager._request_remote_speech.assert_not_called()
    manager._request_chatterbox_speech.assert_not_called()
    manager._process = None


def test_private_worker_success_and_failure_do_not_echo_renderer_or_pass_text_as_argument(
    monkeypatch, tmp_path, capfd, caplog
):
    import io
    import json
    import logging
    import os
    from pathlib import Path

    from arenamcp.desktop import tts_worker
    from arenamcp.tts import VoiceOutput

    secret = "PRIVATE_SENTINEL"

    def render(self, text):
        if text != "Ready":
            logging.getLogger("renderer").critical("Private text %s", text)
            os.write(2, text.encode())
            if text.endswith("fail"):
                raise ValueError(text)
        path = tmp_path / "render.wav"
        path.touch()
        return str(path), 1.0

    monkeypatch.setattr(VoiceOutput, "render_to_wav_file", render)
    monkeypatch.setattr(VoiceOutput, "warmup", lambda _: None)
    monkeypatch.setattr(tts_worker, "_lower_priority", lambda: None)
    monkeypatch.setattr(tts_worker, "_configure_logging", lambda: None)
    events = []
    monkeypatch.setattr(tts_worker, "_emit", events.append)
    commands = [
        dict(cmd="render", generation=i + 1, text=secret + suffix, private=True)
        for i, suffix in enumerate(["success", "fail"])
    ] + [dict(cmd="shutdown")]
    monkeypatch.setattr(tts_worker.sys, "stdin", io.StringIO("\n".join(map(json.dumps, commands))))
    assert tts_worker.main() == 0
    assert [e["type"] for e in events] == ["ready", "rendered", "error"]
    assert events[0]["private_render"] is True
    assert "text" not in events[1] and "traceback" not in events[2]
    assert secret not in repr(events) + str(capfd.readouterr()) + caplog.text
    Path(events[1]["path"]).unlink()


def test_ground_creature_only_counts_unrestricted_answers():
    s = board()
    s["zones"]["battlefield"][0].update(oracle_text="Vigilance", keywords=["vigilance"])
    assert project(s, opened())["answers"] == {"Strong Bolt": 3}


def test_unknown_annotation_or_persistent_replacement_of_library_member_stops_proof():
    for alias in ("annotations", "persistentAnnotations"):
        g = opened()
        g.update_from_message(
            {
                "type": "GameStateType_Diff",
                "gameStateId": 3,
                "prevGameStateId": 2,
                alias: [{"type": ["AnnotationType_NewTreatment"], "affectedIds": [IDS[-1]]}],
            }
        )
        s = board()
        s["_log_game_state_id"] = 3
        assert project(s, g) is None


def test_repeated_opening_envelope_cannot_rearm_a_failed_census():
    g = opened()
    g.update_from_message({"type": "GameStateType_Diff", "gameStateId": 5, "prevGameStateId": 4})
    g.update_from_message(full())
    assert g._draw_inventory is None


def _recorded_phase_event():
    # Exact GRE field shapes, with a local test id in place of its event number.
    return {
        "id": 3,
        "type": ["AnnotationType_PhaseOrStepModified"],
        "affectedIds": [0],
        "details": [
            {"key": "phase", "type": "KeyValuePairValueType_int32", "valueInt32": [1]},
            {"key": "step", "type": "KeyValuePairValueType_int32", "valueInt32": [0]},
        ],
    }


@pytest.mark.parametrize(
    ("field", "updates", "preserved"),
    [
        ("annotations", {}, True),
        ("annotations", {"affectedIds": [-5]}, False),
        ("annotations", {"affectedIds": [5, 5]}, False),
        ("annotations", {"affectedIds": [False]}, False),
        ("annotations", {"affectedIds": [0, 0]}, False),
        ("annotations", {"type": ["AnnotationType_NewTreatment"]}, False),
        ("annotations", {"details": []}, False),
        ("annotations", {"affectorId": IDS[-1]}, False),
        ("persistentAnnotations", {}, False),
    ],
)
def test_only_verified_transient_engine_phase_event_can_reference_zero(field, updates, preserved):
    g = opened()
    g.update_from_message(
        {
            "type": "GameStateType_Diff",
            "gameStateId": 3,
            "prevGameStateId": 2,
            field: [{**_recorded_phase_event(), **updates}],
        }
    )
    s = board()
    s["_log_game_state_id"] = 3
    if preserved:
        assert project(s, g)["remaining"] == 53
    else:
        assert project(s, g) is None


def meloku_board():
    snapshot = board()
    snapshot["zones"]["battlefield"][0].update(
        name="Meloku the Clouded Mirror",
        mana_cost="{4}{U}",
        type_line="Legendary Creature — Moonfolk Wizard",
        oracle_text="Flying\n{1}, Return a land you control to its owner's hand: Create a 1/1 blue Illusion creature token with flying.",
        toughness=4,
        is_tapped=False,
    )
    return snapshot


def removal_library():
    rows = {gid: dict(row) for gid, row in CARDS.items()}
    rows[17].update(
        name="Unsummon", mana_cost="{U}", oracle_text="Return target creature to its owner's hand."
    )
    rows[18].update(
        name="Abrade",
        mana_cost="{1}{R}",
        oracle_text="Choose one —\n• Abrade deals 3 damage to target creature.\n• Destroy target artifact.",
    )
    rows[19].update(name="Shock", mana_cost="{R}", oracle_text="Shock deals 2 damage to any target.")
    return rows


def test_exact_remaining_count_for_meloku_with_proven_seen_and_real_printed_spells(monkeypatch):
    from arenamcp import match_context
    from arenamcp.pipe_adapter import PipeAdapter

    catalog = removal_library()
    snapshot = meloku_board()
    g = opened()
    pipe = PipeAdapter()
    pipe._commentary = Mock()
    pipe._emit = Mock()
    pipe._coach = Mock()
    pipe._coach._autopilot = None
    pipe._coach._mcp._server.game_state = g
    pipe._coach.settings = {"autoplay_speech": "commentary", "commentary_private_information": True}
    monkeypatch.setattr(match_context, "prepared_card", catalog.get)
    pipe.emit_game_state(snapshot)
    fact = pipe._emit.call_args.args[0]["private_library_removal"]
    assert (fact["remaining"], fact["direct_removal_total"], fact["answers"]) == (
        53,
        5,
        {"Plummet": 3, "Unsummon": 2},
    )
    line = private_removal_line(fact, snapshot)[1]
    assert "5 cards among the 53 left" in line and "On a random draw, that’s about 9.4 percent" in line
    assert "private_library_removal" not in pipe._commentary.offer.call_args.args[0]
    g.update_from_message(next_draw())
    snapshot["_log_game_state_id"] = 3
    pipe.emit_game_state(snapshot)
    fact = pipe._emit.call_args.args[0]["private_library_removal"]
    assert (fact["remaining"], fact["direct_removal_total"], fact["answers"]) == (
        52,
        4,
        {"Plummet": 2, "Unsummon": 2},
    )


def test_exact_zero_requires_every_identity_and_printed_target_condition():
    catalog = removal_library()
    for gid in (15, 17, 18, 19):
        catalog[gid] = dict(
            card(gid, "Cast Down", "Instant", "Destroy target nonlegendary creature."), mana_cost="{1}{B}"
        )
    s = meloku_board()
    game = opened()
    fact = private_removal_fact(s, game, allow_private=True, card_lookup=catalog.get)
    assert fact["direct_removal_total"] == 0 and fact["answers"] == {}
    assert "no direct removal" in private_removal_line(fact, s)[1]
    del catalog[19]
    assert private_removal_fact(s, game, allow_private=True, card_lookup=catalog.get) is None
    catalog[15] = CARDS[15]
    partial = private_removal_fact(s, game, allow_private=True, card_lookup=catalog.get)
    assert partial["answers"] == {"Plummet": 3} and "direct_removal_total" not in partial
    assert "percent" not in private_removal_line(partial, s)[1]
    assert private_removal_line({**partial, "direct_removal_total": 2}, s) is None


def test_modal_artifact_mode_marked_damage_and_changed_print():
    from arenamcp.commentary.private_outs import _answers_creature

    c = removal_library()[18]
    target = meloku_board()["zones"]["battlefield"][0]
    assert _answers_creature(c, target) is False and _answers_creature(c, dict(target, damage=1)) is True
    assert _answers_creature(c, dict(target, type_line="Artifact Creature — Golem", toughness=10)) is True
    assert _answers_creature(c, dict(target, modified_toughness=6, damage=1)) is False
    assert _answers_creature(c, dict(target, damage=False)) is None
    assert _answers_creature(dict(c, oracle_text=c["oracle_text"] + " Only once each turn."), target) is None
    assert (
        _answers_creature(
            card(15, "Go for the Throat", "Instant", "Destroy target nonartifact creature."),
            dict(target, type_line="Artifact Creature — Golem"),
        )
        is False
    )


def test_only_highest_public_threat_and_no_target_evidence_overrides_restrictions():
    from arenamcp.commentary.private_outs import _public_removal_threat

    s = meloku_board()
    target = s["zones"]["battlefield"][0]
    small = {**board()["zones"]["battlefield"][0], "instance_id": 801, "power": 1, "toughness": 1}
    s["zones"]["battlefield"].append(small)
    assert _public_removal_threat(s) is target
    small["modified_power"] = 12
    assert _public_removal_threat(s) is small
    s["zones"]["battlefield"][1] = dict(target, instance_id=801)
    assert _public_removal_threat(s) is None
    for change in (
        {"granted_abilities": [8]},
        {"oracle_text": target["oracle_text"] + "\nWard {2}"},
        {"ability_grp_ids": [141939]},
    ):
        s["zones"]["battlefield"] = [{**target, **change}]
        assert _public_removal_threat(s) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("card_types", ["CardType_Creature", "CardType_Artifact"]),
        ("modified_toughness", 0),
        ("damage", 1),
        ("granted_abilities", [808]),
        ("is_tapped", True),
    ],
)
def test_raw_public_revision_conflict_disqualifies_even_matching_ids(field, value):
    g = opened()
    setattr(g.game_objects[800], field, value)
    assert project(board(), g) is None


def test_targeted_enchantment_exile_and_side_effects_use_entire_print():
    from arenamcp.commentary.private_outs import _answers_creature

    target = meloku_board()["zones"]["battlefield"][0]
    banish = dict(
        card(
            22,
            "Banishing Light",
            "Enchantment",
            "When this enchantment enters, exile target nonland permanent an opponent controls until this enchantment leaves the battlefield.",
        ),
        mana_cost="{2}{W}",
    )
    assert _answers_creature(banish, target) is True
    assert _answers_creature(banish, dict(target, type_line="Land Creature — Forest Dryad")) is False
    assert (
        _answers_creature(
            dict(banish, oracle_text=banish["oracle_text"].replace("nonland", "noncreature")), target
        )
        is None
    )
    assert _answers_creature(dict(banish, related_faces=[{"name": "Other"}]), target) is None


def test_aura_requires_its_own_distinct_legal_enchant_target():
    from arenamcp.commentary.private_outs import _answers_creature

    s = meloku_board()
    threat = s["zones"]["battlefield"][0]
    ossification = dict(
        card(
            22,
            "Ossification",
            "Enchantment — Aura",
            "Enchant basic land you control\nWhen this Aura enters, exile target creature or planeswalker an opponent controls until this Aura leaves the battlefield.",
        ),
        mana_cost="{1}{W}",
    )
    assert _answers_creature(ossification, threat, local_creature=True) is None
    assert _answers_creature(ossification, threat, local_basic=True) is True
    sheltered = dict(
        card(
            23,
            "Sheltered by Ghosts",
            "Enchantment — Aura",
            "Enchant creature you control\nWhen this Aura enters, exile target nonland permanent an opponent controls until this Aura leaves the battlefield.\nEnchanted creature gets +1/+0 and has lifelink and ward {o2}.",
        ),
        mana_cost="{1}{W}",
    )
    assert _answers_creature(sheltered, threat, local_basic=True) is None
    assert _answers_creature(sheltered, threat, local_creature=True) is True


def test_all_named_answers_in_local_fact_with_bounded_voice_line():
    s = meloku_board()
    answers = {f"{'A' * 82} {i}": 1 for i in range(10)}
    fact = dict(
        match_id="match",
        game_number=1,
        seat=1,
        state_id=2,
        threat_id=800,
        threat="Meloku the Clouded Mirror",
        remaining=30,
        answers=answers,
        direct_removal_total=10,
    )
    text = private_removal_line(fact, s)[1]
    assert len(text) <= 600 and "10 cards among the 30 left" in text and "including" in text


def test_mana_value_capped_exile_never_treats_unknown_as_zero():
    from arenamcp.commentary.private_outs import _answers_creature

    targeted = dict(
        card(
            22,
            "Seam Rip",
            "Enchantment",
            "When this enchantment enters, exile target nonland permanent an opponent controls with mana value 2 or less until this enchantment leaves the battlefield.",
        ),
        mana_cost="{W}",
    )
    target = meloku_board()["zones"]["battlefield"][0]
    assert _answers_creature(targeted, target) is False
    assert _answers_creature(targeted, dict(target, mana_cost="{1}{U}")) is True
    assert _answers_creature(targeted, dict(target, mana_cost="{1}{U}{U}")) is False
    assert _answers_creature(targeted, dict(target, mana_cost="")) is None
    assert _answers_creature(targeted, dict(target, mana_cost="{2/U}")) is None
    assert (
        _answers_creature(targeted, dict(target, type_line="Land Creature — Forest Dryad", mana_cost="{0}"))
        is False
    )


def test_current_positive_proof_of_empty_library_is_distinct_from_unavailable():
    game = opened()
    final_draw = next_draw()
    # The local player draws the whole remaining library. Every departing
    # instance appears by its unchanged id in the own private hand.
    remaining = [15] * 3 + [16] * 44 + [17] * 2 + [18] * 3 + [19]
    final_draw["zones"][0]["objectInstanceIds"] = []
    final_draw["zones"][1]["objectInstanceIds"] = IDS
    final_draw["gameObjects"] = [
        dict(draw_opening()["gameObjects"][0], instanceId=iid, grpId=gid, overlayGrpId=gid)
        for iid, gid in zip(IDS[7:], remaining, strict=True)
    ]
    final_draw["annotations"] = [dict(final_draw["annotations"][1], affectedIds=IDS[7:])]
    game.update_from_message(final_draw)
    s = board()
    s["_log_game_state_id"] = 3
    fact = project(s, game)
    assert fact["remaining"] == 0 and fact["direct_removal_total"] == 0 and fact["answers"] == {}
    assert "0-card library" in private_removal_line(fact, s)[1]
    game.log_summarized_at_gsid = 3
    assert project(s, game) is None


@pytest.mark.parametrize(
    "resolved, admitted", [(None, False), ("Hexproof", False), ("Ward {2}", False), ("Flying", True)]
)
def test_private_outs_require_resolved_printed_abilities_even_without_arena_data(
    monkeypatch, resolved, admitted
):
    from types import SimpleNamespace

    from arenamcp.commentary import private_outs

    s = meloku_board()
    target = s["zones"]["battlefield"][0]
    target["ability_grp_ids"] = [987654]
    monkeypatch.setattr(
        private_outs, "get_card_database", lambda: SimpleNamespace(get_ability_text=lambda _: resolved)
    )
    assert (private_outs._public_removal_threat(s) is target) == admitted


@pytest.mark.parametrize(
    "printed,native,accepted",
    [
        ("When this creature dies, draw a card.", "When CARDNAME dies, draw a card.", True),
        ("When Meloku dies, draw a card.", "When CARDNAME dies, draw a card.", True),
        ("When Meloku dies, draw a card.", "When CARDNAME dies, draw two cards.", False),
        ("When this land dies, draw a card.", "When CARDNAME dies, draw a card.", False),
    ],
)
def test_native_self_reference_preserves_all_other_printed_text(monkeypatch, printed, native, accepted):
    from types import SimpleNamespace

    from arenamcp.commentary import private_outs

    target = dict(
        meloku_board()["zones"]["battlefield"][0],
        name="Meloku, Test Print",
        oracle_text=printed,
        ability_grp_ids=[17],
    )
    monkeypatch.setattr(
        private_outs, "get_card_database", lambda: SimpleNamespace(get_ability_text=lambda _: native)
    )
    assert private_outs._verified_native_abilities(target) == accepted


@pytest.mark.parametrize("extra", [[987654], [None], [{"grpId": False}], [{"grpId": -1}], [True], ["bad"]])
def test_private_count_withheld_on_divergent_or_malformed_gre_ability_lists(extra, monkeypatch):
    from types import SimpleNamespace

    from arenamcp.commentary import private_outs

    monkeypatch.setattr(
        private_outs,
        "get_card_database",
        lambda: SimpleNamespace(get_ability_text=lambda gid: "Flying" if gid == 8 else None),
    )
    s = board()
    s["zones"]["battlefield"][0]["ability_grp_ids"] = [8]
    g = opened()

    def update(**fields):
        g._update_game_object(dict(instanceId=800, **fields))

    update(uniqueAbilities=[{"id": 42, "grpId": 8}], abilities=[8, *extra])
    obj = g.game_objects[800]
    assert obj.native_abilities_incomplete and obj.ability_grp_ids == [8]
    assert obj.to_dict()["native_abilities_incomplete"]
    assert project(s, g) is None
    update(uniqueAbilities=[{"id": 42, "grpId": 8}])
    assert g.game_objects[800].native_abilities_incomplete
    update(uniqueAbilities=[{"id": 42, "grpId": 8}], abilities=[8])
    assert not g.game_objects[800].native_abilities_incomplete
    assert project(s, g) is not None


@pytest.mark.parametrize(
    ("name", "cost", "type_line", "rules", "answers"),
    [
        ("Helpful Hunter", "{1}{W}", "Creature — Cat", "When this creature enters, draw a card.", False),
        (
            "Ethereal Armor",
            "{W}",
            "Enchantment — Aura",
            "Enchant creature\nEnchanted creature gets +1/+1 for each enchantment you control and has first strike.",
            False,
        ),
        (
            "Cooped Up",
            "{1}{W}",
            "Enchantment — Aura",
            "Enchant creature\nEnchanted creature can't attack or block.\n{o2oW}: Exile enchanted creature.",
            True,
        ),
        (
            "Trapped in the Screen",
            "{2}{W}",
            "Enchantment",
            "Ward {o2}\nWhen this enchantment enters, exile target artifact, creature, or enchantment an opponent "
            "controls until this enchantment leaves the battlefield.",
            True,
        ),
        ("Pearl Medallion", "{2}", "Artifact", "White spells you cast cost {o1} less to cast.", False),
    ],
)
def test_local_census_print_review_is_whole_source_and_does_not_certify_a_board(
    name, cost, type_line, rules, answers
):
    from arenamcp.commentary.private_outs import _answers_creature, _public_removal_threat

    printed = dict(card(42, name, type_line, rules), mana_cost=cost)
    snapshot = meloku_board()
    target = snapshot["zones"]["battlefield"][0]
    assert _answers_creature(printed, target) is answers
    assert _answers_creature(dict(printed, oracle_text=rules + "\nDestroy target creature."), target) is None
    assert _answers_creature(dict(printed, linked_face_ids=[999]), target) is None
    snapshot["zones"]["battlefield"].append(dict(target, **printed, controller_seat_id=1, instance_id=555))
    if name == "Helpful Hunter":
        assert _public_removal_threat(snapshot) is target
        snapshot["zones"]["battlefield"][-1]["granted_abilities"] = ["ward"]
    assert _public_removal_threat(snapshot) is None


def test_conditional_entry_land_must_have_only_mana_abilities():
    from arenamcp.commentary.private_outs import _answers_creature

    land = card(
        42,
        "Test Orchard",
        "Land",
        "This land enters tapped unless you control a basic land.\n{oT}: Add {oG} or {oW}.",
    )
    target = meloku_board()["zones"]["battlefield"][0]
    assert _answers_creature(land, target) is False
    land["oracle_text"] += "\n{oT}: Destroy target creature with flying."
    assert _answers_creature(land, target) is None


@pytest.mark.parametrize(
    ("text", "toughness", "marked", "answers"),
    [
        ("This spell deals 3 damage to each opponent.", 1, 0, False),
        (
            "Damage can't be prevented this turn. This spell deals 3 damage to up to one target creature or planeswalker.",
            4,
            1,
            True,
        ),
        ("This spell deals 1 damage to each opponent and each creature they control.", 2, 0, False),
        ("This spell deals 1 damage to each opponent and each creature they control.", 2, 1, True),
        ("This spell deals 2 damage to up to one target creature with flying.", 1, 0, False),
        ("This spell deals 3 damage to each attacking creature.", 1, 0, None),
    ],
)
def test_complete_burn_print_requires_an_eligible_public_creature(text, toughness, marked, answers):
    from arenamcp.commentary.private_outs import _answers_creature

    target = dict(card(23, "Test Bear", "Creature — Bear", "Haste"), toughness=toughness, damage=marked)
    spell = card(24, "Example", "Sorcery", text)
    assert _answers_creature(spell, target) is answers
    assert _answers_creature(dict(spell, oracle_text=text + " if you control a Wizard."), target) is None
    assert _answers_creature(dict(spell, linked_face_ids=[50]), target) is None


@pytest.mark.parametrize(
    ("toughness", "marked", "answers"),
    [(2, 0, True), (3, 1, True), (3, 0, None), (5, 0, False), (3, -1, None)],
)
def test_kicked_only_damage_requires_unproved_additional_payment(toughness, marked, answers):
    from arenamcp.commentary.private_outs import _answers_creature

    text = "Kicker {o3}\nTest Bolt deals 2 damage to any target. If this spell was kicked, it deals 4 damage instead."
    bolt = card(24, "Test Bolt", "Instant", text)
    target = dict(card(23, "Test Bear", "Creature — Bear", "Haste"), toughness=toughness, damage=marked)
    assert _answers_creature(bolt, target) is answers
    assert _answers_creature(dict(bolt, oracle_text=text + "\nDraw a card."), target) is None


@pytest.mark.parametrize(
    "text",
    [
        "Flying\nPlot {o1oU}\nWhenever you cast a noncreature spell, this creature gets +1/+1 until end of turn.",
        "Prowess\nWhenever this creature becomes the target of a spell or ability you control for the first time each turn, exile the top card of your library. Until end of turn, you may play that card.",
        "Whenever you attack with one or more Birds, this creature deals 2 damage to target opponent.",
        "{1}{G}: Put a +1/+1 counter on this creature.",
        "At the beginning of your end step, return this creature to its owner's hand.\n{o1oR}: This creature gets +1/+0 until end of turn.",
    ],
)
def test_library_only_self_and_opponent_rules_require_complete_print(text):
    from arenamcp.commentary.private_outs import _answers_creature, _simple_text

    creature = card(24, "Test Bird", "Creature — Bird", text)
    target = dict(card(23, "Test Bear", "Creature — Bear", "Haste"), toughness=2)
    assert _answers_creature(creature, target) is False
    assert not _simple_text(creature)
    assert (
        _answers_creature(
            dict(creature, oracle_text=text + "\nWhen this creature enters, destroy target creature."), target
        )
        is None
    )
    assert _answers_creature(dict(creature, related_faces=[{"name": "Other"}]), target) is None


@pytest.mark.parametrize("native", [False, True])
def test_every_mode_requires_a_complete_damage_fact_and_only_one_mode_can_apply(native):
    from arenamcp.commentary.private_outs import _answers_creature

    lines = [
        "This spell deals 1 damage to each creature your opponents control.",
        "This spell deals 4 damage to target creature.",
    ]
    modes = [f"•<indent=4%>{line} </indent>" if native else f"• {line}" for line in lines]
    text = "Choose one — \n" + "\n".join(modes)
    spell = card(24, "Example", "Sorcery", text)
    target = dict(card(23, "Test Bear", "Creature — Bear", "Haste"), toughness=4)
    assert _answers_creature(spell, target) is True
    assert _answers_creature(dict(spell, type_line="Sorcery — Lesson"), target) is True
    assert _answers_creature(dict(spell, type_line="Sorcery — Lesson", linked_face_ids=[7]), target) is None
    assert _answers_creature(dict(spell, type_line="Kindred Sorcery — Goblin"), target) is None
    assert _answers_creature(spell, dict(target, toughness=5)) is False
    assert (
        _answers_creature(dict(spell, oracle_text=text.replace("Choose one", "Choose two")), target) is None
    )
    assert _answers_creature(dict(spell, oracle_text=text + "\n• Sacrifice a creature."), target) is None
    assert (
        _answers_creature(card(25, "Example", "Creature — Bear", "Trample\nPlayers can't gain life."), target)
        is False
    )


def test_whole_print_exile_in_the_private_library_census():
    target = board()["zones"]["battlefield"][0]
    land_search = (
        "Exile target creature. Its controller may search their library for a basic land card, "
        "put that card onto the battlefield tapped, then shuffle."
    )
    until_departure = (
        "When this enchantment enters, exile target nonland permanent an opponent controls "
        "until this enchantment leaves the battlefield."
    )
    from arenamcp.commentary.private_outs import _answers_creature

    lookup = {
        **CARDS,
        17: card(17, "Optional search", "Instant", land_search),
        19: card(19, "Temporary exile", "Enchantment", until_departure),
    }
    game = opened()
    fact = private_removal_fact(board(), game, allow_private=True, card_lookup=lookup.get)
    assert fact["answers"] == {"Optional search": 2, "Plummet": 3, "Strong Bolt": 3, "Temporary exile": 1}
    assert (fact["direct_removal_total"], fact["remaining"]) == (9, 53)
    assert _answers_creature(lookup[19], dict(target, type_line="Land Creature — Forest Dryad")) is False
    for spell in (lookup[17], lookup[19]):
        assert _answers_creature(dict(spell, linked_face_ids=[809]), target) is None
        assert (
            _answers_creature(dict(spell, oracle_text=spell["oracle_text"] + "\nFlashback {3}"), target)
            is None
        )
    assert _answers_creature(dict(lookup[19], type_line="Enchantment — Aura"), target) is None
    assert (
        _answers_creature(
            dict(lookup[17], oracle_text="Exile target creature unless its controller pays {2}."), target
        )
        is None
    )


def test_plains_bound_exile_follows_current_control_and_subtype():
    game, snapshot = opened(), board()
    text = (
        "Exile target creature with mana value less than or equal to the number of Plains you control. "
        "Its controller gains 3 life."
    )
    spell = card(17, "Measured exile", "Sorcery", text)
    lookup = {**CARDS, 17: spell}

    def fact():
        return private_removal_fact(snapshot, game, allow_private=True, card_lookup=lookup.get)

    assert fact()["direct_removal_total"] == 6
    row = dict(
        card(251, "Test dual", "Land — Plains Island", "{T}: Add {W} or {U}."),
        instance_id=801,
        controller_seat_id=2,
        owner_seat_id=2,
        is_tapped=True,
    )
    raw = {
        "instanceId": 801,
        "grpId": 251,
        "zoneId": 28,
        "controllerSeatId": 2,
        "ownerSeatId": 2,
        "type": "GameObjectType_Card",
        "visibility": "Visibility_Public",
        "isTapped": True,
        "cardTypes": ["CardType_Land"],
        "subtypes": ["SubType_Plains", "SubType_Island"],
    }
    snapshot["zones"]["battlefield"].append(row)

    def advance(state_id):
        game.update_from_message(
            {
                "type": "GameStateType_Diff",
                "gameStateId": state_id,
                "prevGameStateId": state_id - 1,
                "zones": [{"zoneId": 28, "type": "ZoneType_Battlefield", "objectInstanceIds": [800, 801]}],
                "gameObjects": [raw],
            }
        )
        snapshot["_log_game_state_id"] = state_id

    advance(3)
    assert fact()["direct_removal_total"] == 6
    row["controller_seat_id"] = raw["controllerSeatId"] = 1
    advance(4)
    assert fact()["answers"][spell["name"]] == 2
    assert fact()["direct_removal_total"] == 8
    target = snapshot["zones"]["battlefield"][0]
    target["mana_cost"] = "{1}{B}"
    assert fact()["direct_removal_total"] == 6
    target["mana_cost"] = ""
    assert "direct_removal_total" not in fact()
    target["mana_cost"] = "{1}"
    sparse = {"instanceId": 801, "zoneId": 28, "isTapped": True}
    full_land, raw = raw, sparse
    advance(5)
    assert fact()["answers"][spell["name"]] == 2
    raw = full_land
    del raw["subtypes"]
    advance(6)
    assert "direct_removal_total" not in fact()
    raw["subtypes"] = ["SubType_Island"]
    advance(7)
    assert "direct_removal_total" not in fact()
    row["type_line"] = "Land — Island"
    assert fact()["direct_removal_total"] == 6
    lookup[17] = dict(spell, oracle_text=text + " Draw a card.")
    assert "direct_removal_total" not in fact()
