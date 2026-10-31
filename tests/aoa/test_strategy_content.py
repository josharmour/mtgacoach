import copy
from dataclasses import replace

from test_headless_booth import observation

from arenamcp.commentary.config import CommentaryConfig
from arenamcp.commentary.headless import deterministic
from arenamcp.commentary.timeline import valid_for


def card(iid, name, oracle):
    return {
        "instance_id": iid,
        "grp_id": 100 + iid,
        "name": name,
        "oracle_text": oracle,
        "controller_seat_id": 1,
        "type_line": "Creature",
        "power": 1,
        "toughness": 3,
    }


def test_boros_analyst_explains_token_engine_and_survives_unrelated_draw():
    obs = observation()
    public = copy.deepcopy(obs.public_state)
    voice = card(20, "Voice of Victory", "Mobilize 2\nYour opponents can't cast spells during your turn.")
    bell = card(
        21,
        "Belladonna Took",
        "Whenever a token you control enters, you gain 1 life if this is the first time this ability has resolved this turn. If it's the second time, draw a card.",
    )
    public["zones"]["battlefield"] = [voice, bell]
    obs = replace(obs, public_state=public)
    lines = deterministic(obs, CommentaryConfig(mode="commentary", speakers=2))
    assert len(lines) == 2 and lines[1].role == "color_analyst"
    assert "Mobilize" in lines[1].text and "token-entry rewards" in lines[1].text
    assert "plan behind" not in lines[1].text
    changed = replace(obs, private_state={"hand": [card(30, "New draw", "")]})
    assert valid_for(lines[1], changed)
    public = copy.deepcopy(public)
    public["zones"]["battlefield"].pop()
    assert not valid_for(lines[1], replace(obs, public_state=public))
    repeated = deterministic(
        replace(obs, recent_commentary=(lines[1].text,)), CommentaryConfig(mode="commentary", speakers=2)
    )
    assert len(repeated) == 2 and repeated[1].text != lines[1].text


def test_monored_analysis_explains_prowess_timing_without_asserting_damage():
    obs = observation()
    public = copy.deepcopy(obs.public_state)
    public["zones"]["battlefield"] = [card(20, "Emberheart Challenger", "Prowess\nHaste")]
    lines = deterministic(replace(obs, public_state=public), CommentaryConfig(mode="commentary", speakers=2))
    assert len(lines) == 2
    assert "before combat" in lines[1].text and "can build" in lines[1].text
    assert "kills" not in lines[1].text and "dealt" not in lines[1].text


def test_no_engine_no_invented_strategy_and_face_down_identity_unused():
    obs = observation()
    public = copy.deepcopy(obs.public_state)
    public["zones"]["battlefield"] = [{**card(20, "Secret", "Mobilize 2"), "face_down": True}]
    lines = deterministic(replace(obs, public_state=public), CommentaryConfig(mode="commentary", speakers=2))
    assert all("Secret" not in line.text and "Mobilize" not in line.text for line in lines)


def test_actual_arbiter_delivers_analyst_after_phase_and_unrelated_draw():
    from test_opening_delivery import finish

    from arenamcp.desktop.speech_arbiter import SpeechArbiter

    obs = observation()
    public = copy.deepcopy(obs.public_state)
    public["zones"]["battlefield"] = [card(20, "Emberheart Challenger", "Prowess\nHaste")]
    obs = replace(obs, public_state=public)
    lines = deterministic(obs, CommentaryConfig(mode="commentary", speakers=2))
    current = [obs]
    played = []
    arbiter = SpeechArbiter(
        start=played.append,
        stop=lambda: None,
        status=lambda *a: None,
        valid=lambda u: valid_for(u, current[0]),
    )
    arbiter.reset(obs.epoch)
    for u in lines:
        arbiter.submit(u)
    public = copy.deepcopy(public)
    public["turn_info"]["phase"] = "Combat"
    current[0] = replace(
        obs, snapshot_id="later", public_state=public, private_state={"hand": [card(40, "Draw", "")]}
    )
    finish(arbiter)
    assert arbiter.active.role == "color_analyst"
    finish(arbiter)
    assert [u.role for u in played] == ["play_by_play", "color_analyst"]
    assert "before combat" in played[1].text


def test_compact_request_keeps_engine_rules_ahead_of_large_private_hand():
    import json

    from arenamcp.commentary.context import compact_context, generation_format, request_size
    from arenamcp.commentary.headless import SYSTEM

    facts = {
        "event:1": {"kind": "spell_cast", "data": {"grp_id": 1}},
        "state:board": {
            "objects": [
                {"grp_id": 120, "name": "Voice of Victory"},
                {"grp_id": 121, "name": "Belladonna Took"},
            ]
        },
        "card:120": {"name": "Voice of Victory", "rules": "Mobilize 2"},
        "card:121": {"name": "Belladonna Took", "rules": "Whenever a token you control enters, draw a card."},
        "private:hand:1": {"cards": [{"name": "Big rules", "oracle": "x" * 2000}] * 4},
    }
    result = compact_context(
        SYSTEM,
        {"facts": facts, "new_events": ["event:1"], "allowed_roles": ["play_by_play", "color_analyst"]},
    )
    assert all(k in result["facts"] for k in ("state:board", "card:120", "card:121"))
    assert (
        request_size(SYSTEM, result) + len(json.dumps(generation_format(), separators=(",", ":")).encode())
        <= 5900
    )


def test_report152400_stadium_suki_strategy_is_delivered_not_merely_queued():
    from test_opening_delivery import finish

    from arenamcp.desktop.speech_arbiter import SpeechArbiter

    # Public rules of the two established permanents in the supplied report;
    # no captured private hand or match identity is committed into this fixture.
    obs = observation()
    public = copy.deepcopy(obs.public_state)
    public["zones"]["battlefield"] = [
        card(20, "Stadium Headliner", "Mobilize 1"),
        card(
            21,
            "Suki, Courageous Rescuer",
            "Other creatures you control get +1/+0.\nWhenever another permanent you control leaves the battlefield during your turn, create a 1/1 white Ally creature token. This ability triggers only once each turn.",
        ),
    ]
    obs = replace(obs, public_state=public)
    lines = deterministic(obs, CommentaryConfig(mode="commentary", speakers=2))
    played = []
    a = SpeechArbiter(
        start=played.append, stop=lambda: None, status=lambda *a: None, valid=lambda u: valid_for(u, obs)
    )
    a.reset(obs.epoch)
    for line in lines:
        a.submit(line)
    finish(a)
    finish(a)
    assert len(played) == 2 and played[1].role == "color_analyst"
    assert "lasting value through Suki" in played[1].text
    assert "protecting that engine" in played[1].text
