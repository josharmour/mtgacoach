"""Regression for report193910: analyst tails lost to unrelated live updates."""

import copy
from dataclasses import replace

import pytest
from test_headless_booth import observation
from test_opening_delivery import finish
from test_strategy_content import card

from arenamcp.commentary.config import CommentaryConfig
from arenamcp.commentary.headless import deterministic
from arenamcp.commentary.timeline import valid_for
from arenamcp.desktop.speech_arbiter import SpeechArbiter


def board_observation(*, opponent=True, lands=False):
    obs = observation()
    public = copy.deepcopy(obs.public_state)
    public["zones"]["battlefield"] = [card(20, "Creature A", "")]
    if opponent:
        public["zones"]["battlefield"].append({**card(21, "Creature B", ""), "controller_seat_id": 2})
    if lands:
        public["zones"]["battlefield"] = [
            {**card(i, "Land", ""), "type_line": "Land"} for i in range(30, 33)
        ] + [{**card(40, "Land", ""), "type_line": "Land", "controller_seat_id": 2}]
    return replace(obs, public_state=public)


def lines_for(obs):
    return deterministic(obs, CommentaryConfig(mode="commentary", speakers=2))


@pytest.mark.parametrize("opponent,lands", [(True, False), (False, False), (False, True)])
def test_analyst_finishes_across_irrelevant_updates(opponent, lands):
    obs = board_observation(opponent=opponent, lands=lands)
    lines = lines_for(obs)
    assert len(lines) == 2 and lines[1].role == "color_analyst"
    current, played, stopped = [obs], [], []
    a = SpeechArbiter(
        start=played.append,
        stop=lambda: stopped.append(True),
        status=lambda *a: None,
        clock=lambda: obs.arrived_at + 1,
        valid=lambda u: valid_for(u, current[0]),
    )
    a.reset(obs.epoch)
    for line in lines:
        a.submit(line)
    public = copy.deepcopy(obs.public_state)
    public["players"][1]["life_total"] = 13
    public["zones"]["stack"] = []
    public["turn_info"]["turn_number"] += 1
    public["zones"]["battlefield"][0]["is_tapped"] = True
    current[0] = replace(obs, snapshot_id="new", public_state=public, private_state={"hand": []})
    finish(a)
    assert a.active == lines[1]
    a.playback_status(lines[1].utterance_id, "started", epoch=obs.epoch)
    public = copy.deepcopy(public)
    public["players"][0]["life_total"] = 17
    current[0] = replace(current[0], public_state=public)
    a.tick()  # Used to cut the analyst immediately after a life/stack update.
    assert a.active == lines[1] and not stopped
    a.playback_status(lines[1].utterance_id, "finished", epoch=obs.epoch)
    assert a.active is None and [u.role for u in played] == ["play_by_play", "color_analyst"]


@pytest.mark.parametrize("change", ["remove", "control", "type", "unknown", "name"])
def test_material_board_change_still_invalidates_analysis(change):
    obs = board_observation()
    tail = lines_for(obs)[1]
    public = copy.deepcopy(obs.public_state)
    board = public["zones"]["battlefield"]
    if change == "remove":
        board.pop()
    elif change == "control":
        board[1]["controller_seat_id"] = 1
    elif change == "type":
        board[1]["type_line"] = "Enchantment"
    elif change == "unknown":
        board[1]["face_down"] = True
    else:
        public["player_names"] = {1: "Different player", 2: "Opponent"}
    assert not valid_for(tail, replace(obs, public_state=public))


def test_absent_opponent_getting_creature_and_land_loss_invalidate():
    obs = board_observation(opponent=False)
    public = copy.deepcopy(obs.public_state)
    public["zones"]["battlefield"].append({**card(22, "Blocker", ""), "controller_seat_id": 2})
    assert not valid_for(lines_for(obs)[1], replace(obs, public_state=public))
    obs = board_observation(lands=True)
    public = copy.deepcopy(obs.public_state)
    public["zones"]["battlefield"].pop(0)
    assert not valid_for(lines_for(obs)[1], replace(obs, public_state=public))


def test_expiry_epoch_and_failed_anchor_guards_remain():
    obs = board_observation()
    first, tail = lines_for(obs)
    assert not valid_for(tail, replace(obs, epoch=replace(obs.epoch, game_number=2)))
    for expired in (False, True):
        played = []
        a = SpeechArbiter(
            start=played.append,
            stop=lambda: None,
            status=lambda *a: None,
            clock=lambda expired=expired: tail.expires_at + 1 if expired else obs.arrived_at + 1,
            valid=lambda u: valid_for(u, obs),
        )
        a.reset(obs.epoch)
        a.submit(first)
        a.submit(tail)
        if not expired:
            a.playback_status(first.utterance_id, "failed", epoch=obs.epoch)
        assert tail not in played
