"""Names reach both announcers; speech formatting preserves facts and identities."""

import time
from dataclasses import replace
from unittest.mock import Mock

import pytest
from test_controller import Tts
from test_headless_booth import observation, source  # noqa: F401

from arenamcp.commentary.config import CommentaryConfig
from arenamcp.commentary.events import ObservationAdapter
from arenamcp.commentary.headless import HeadlessBooth, deterministic
from arenamcp.desktop.commentary_controller import CommentaryController
from arenamcp.player_names import pronounce_players, spoken_name

NAMES = {1: "Armour", 2: "mrpickle802"}


@pytest.mark.parametrize(
    "name,expected",
    [
        ("mrpickle802", "Mister pickle eight oh two"),
        ("MRPickle802", "Mister pickle eight oh two"),
        ("Armour", "Armour"),
        ("armour.tv", "Armour tee vee"),
        ("xXDragonSlayerXx", "Dragon slayer"),
        ("Dr_Strange007", "Doctor strange oh oh seven"),
        ("dr4gon99", "Dragon nine nine"),
        ("qxz802", "Kazaz eight oh two"),
        ("Éowyn42", "Éowyn four two"),
        ("Schmidt", "Schmidt"),
        ("Rhys", "Rhys"),
        ("Strengths", "Strengths"),
    ],
)
def test_natural_username_reading(name, expected):
    assert spoken_name(name) == expected


def test_speech_replacement_is_exact_and_does_not_rewrite_game_numbers():
    text = "mrpickle802 cast Opt; 802 damage is not established. Armour attacks."
    assert (
        pronounce_players(text, NAMES)
        == "Mister pickle eight oh two cast Opt; 802 damage is not established. Armour attacks."
    )
    assert (
        pronounce_players("notmrpickle802 and mrpickle802extra", NAMES)
        == "notmrpickle802 and mrpickle802extra"
    )
    assert pronounce_players("Armour's turn.", NAMES) == "Armour's turn."
    assert pronounce_players("istanbul casts Opt.", {1: "İstanbul"}) == "Istanbul casts Opt."


def named_observation():
    obs = observation()
    return replace(obs, public_state={**obs.public_state, "player_names": NAMES})


def test_projection_and_both_fallback_seats_use_public_display_names():
    obs = named_observation()
    state = {"match_id": obs.epoch.match_id, "game_number": 1, **obs.public_state}
    projected = ObservationAdapter().project(state)
    assert projected.public_state["player_names"] == NAMES
    for seat, name in NAMES.items():
        e = replace(obs.events[0], seat=seat)
        line = deterministic(replace(projected, events=(e,)), CommentaryConfig(mode="commentary"))[0]
        assert line.text == f"{name} casts Opt."
    old = {**state, "player_names": {}, "opponent_name": "Known rival", "opponent_seat_id": 2}
    assert ObservationAdapter().project(old).public_state["player_names"] == {2: "Known rival"}


@pytest.mark.usefixtures("source")
def test_native_generated_context_and_audit_keep_exact_identity():
    obs = named_observation()
    calls = []

    def rpc(system, context, audit):
        calls.append(audit)
        if audit:
            assert context["facts"]["state:player:1"]["name"] == "Armour"
            return {
                "checks": [
                    {"index": i, "supported": True, "unsupported_claim": "", "scope": "historical"}
                    for i in range(len(context["lines"]))
                ]
            }
        assert "display names verbatim" in system
        assert context["facts"]["state:player:2"]["name"] == "mrpickle802"
        key = context["new_events"][0]
        assert context["facts"][key]["player"] == "Armour"
        return {
            "turns": [{"role": "play_by_play", "text": "Armour casts Opt.", "refs": [key, "state:player:1"]}]
        }

    booth = HeadlessBooth(CommentaryConfig(mode="commentary", generative=True), rpc, time.monotonic)
    lines, status = booth.generate(obs)
    assert len(lines) == 1, status
    assert calls == [False, True]


@pytest.mark.parametrize("provider", ["kokoro", "chatterbox_remote"])
def test_desktop_pronounces_names_for_both_voice_engines_but_keeps_display(qapp, monkeypatch, provider):
    settings = {
        "tts_provider": provider,
        "autoplay_speech": "commentary",
        "speech_speed": 1.5,
        "_commentary_voice_catalog": {"voices": ["atlas", "mira"], "default": "atlas"},
    }
    monkeypatch.setattr("arenamcp.desktop.commentary_controller.get_settings", lambda: settings)
    tts, spoken = Tts(), Mock()
    c = CommentaryController(tts, Mock(), spoken)
    obs = named_observation()
    state = {"match_id": obs.epoch.match_id, "game_number": 1, **obs.public_state}
    try:
        c.observe(state)
        first = deterministic(replace(obs, events=(replace(obs.events[0], seat=2),)), c.config)[0]
        first = replace(first, snapshot_id=c.observation.snapshot_id)
        c.proposal(first.to_wire())
        assert spoken.call_args.args[0] == "mrpickle802 casts Opt."
        assert tts.request_speech.call_args.kwargs["text"] == "Mister pickle eight oh two casts Opt."
        assert tts.request_speech.call_args.kwargs["speed"] == 1.5
    finally:
        c.close()


def test_opaque_handle_keeps_readable_syllables_instead_of_spelling_letters():
    said = spoken_name("bnhjgsyeduligksr802")
    assert said.startswith("Banahajagasyeduli")
    assert said.endswith("eight oh two")
    assert "bee" not in said and "jay" not in said


@pytest.mark.usefixtures("source")
def test_real_worker_transports_names_and_keeps_heard_identity(request):
    import sys
    import threading

    from arenamcp.commentary.client import CommentaryClient

    obs = named_observation()
    config = CommentaryConfig(
        mode="commentary",
        generative=True,
        python=sys.executable,
        aoa_source=request.getfixturevalue("source"),
    )

    def rpc(system, context, audit):
        if audit:
            return {
                "checks": [
                    {"index": i, "supported": True, "unsupported_claim": "", "scope": "position"}
                    for i in range(len(context["lines"]))
                ]
            }
        key = context["new_events"][0]
        assert context["facts"]["state:player:2"]["name"] == "mrpickle802"
        return {
            "turns": [
                {"role": "play_by_play", "text": "Armour casts Opt.", "refs": [key, "state:player:1"]},
                {
                    "role": "color_analyst",
                    "text": "mrpickle802 remains at twenty life.",
                    "refs": ["state:player:2"],
                },
            ]
        }

    client = CommentaryClient()
    try:
        result = client.generate(obs, config, rpc, threading.Event())
        assert [u["text"] for u in result["utterances"]] == [
            "Armour casts Opt.",
            "mrpickle802 remains at twenty life.",
        ], result["status"]
        for line in result["utterances"]:
            client.delivered(line["utterance_id"], "finished")
    finally:
        client.close()
