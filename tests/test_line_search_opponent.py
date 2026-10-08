"""The paranoid opponent's attack policy is valued after our crackback, and sides stay put.

bug_20261007_182945 (18:29:21 Board facts, their T13 upkeep): "attack ...: survives
(life 8, 6, 5, 5; opponent at 5); hold: survives (life 8, 7, 5, 4; opponent at 8)" —
24 life gone in three of our turns from four 1-power attackers against two 1/4
fliers and a 2/3. The policy worst for us was chosen on our life alone, so
their every creature attacked every turn (``all``) and, tapped, blocked nothing
on ours. bug_20261007_183539 (18:35, "1 vs 3 power") is the sided-right control:
our Paradox Shaper 1/3 against their Graft Surgeon 3/3.
"""

from __future__ import annotations

from copy import deepcopy

import pytest
from tests import strategic_states as fx

from arenamcp import board_assessment as ba
from arenamcp.board_model import build_board_model

REAL_BOARDS = (
    "BUG_135027", "BUG_174855", "BUG_180436", "BUG_183358", "BUG_182945", "BUG_182945_T13_UPKEEP", "BUG_183539",
    "SLOW_212608", "SLOW_202111",
)  # fmt: skip


def _assess(source: dict):
    ba._CACHE.clear()
    return ba.assess(deepcopy(source))


def _power(model, hand_bodies: dict[str, int]) -> dict[str, int]:
    powers = {b["name"]: int(b["power"]) for b in model.ours}
    return {**hand_bodies, **powers}


def test_their_attack_policy_is_valued_after_our_crackback_on_the_logged_board():
    source = fx.BUG_182945_T13_UPKEEP
    model = build_board_model(deepcopy(source))
    assert (model.our_life, model.opp_life, model.our_turn) == (10, 24, False)
    assert sorted(b["name"] for b in model.theirs) == [
        "Hallway Heckler", "Screeching Soulbreaker", "Screeching Soulbreaker"
    ]  # fmt: skip
    assessment = _assess(source)
    result = assessment.line_search
    assert result is not None and not result.truncated

    # Every line: their life never drops by more than the power we attacked with (no
    # damage from anywhere else is modelled on this board), and the three turns of attacks
    # can't take 24 below 13 — before the fix the best line read "opponent at 5".
    power = _power(model, {"Traxos, Academy Guardian": 1, "Void Extrapolator": 2})
    for line in result.lines:
        opp_life = model.opp_life
        for step in line.steps:
            if step.opp_life_after is None:
                continue
            assert opp_life - step.opp_life_after <= sum(power[name] for name in step.attack), line.summary()
            opp_life = step.opp_life_after
        assert opp_life >= 13, line.summary()
    best = result.best
    assert best.outcome == "alive" and best.lives() == [8, 8, 8, 8]
    assert [s.opp_life_after for s in best.steps] == [21, None, 16]
    # Their fliers no longer tap out every turn into our five bodies for two damage.
    assert all(step.policy != "all" for step in best.steps), [s.policy for s in best.steps]
    assert "opponent at 16" in result.posture_reason and "opponent at 15" in result.posture_reason
    assert "opponent at 5" not in assessment.role_reason and "opponent at 8" not in assessment.role_reason


def test_the_report_board_itself_keeps_their_life_near_26():
    # 18:29:45: Apex Witchstalker resolved (26 life, a 6/4 menace blocker): at most a few through.
    result = _assess(fx.BUG_182945).line_search
    assert result.best.outcome == "alive"
    assert min(s.opp_life_after for s in result.best.steps if s.opp_life_after is not None) >= 22


@pytest.mark.parametrize("name", REAL_BOARDS)
def test_ours_and_theirs_follow_the_local_seat(name):
    source = getattr(fx, name)
    model = build_board_model(deepcopy(source))
    local, opponent = source["local_seat_id"], source["opponent_seat_id"]
    cards = {c["instance_id"]: c for c in source["battlefield"]}
    assert (model.local, model.opponent) == (local, opponent)
    assert {cards[b["instance_id"]]["controller_seat_id"] for b in model.ours} <= {local}
    assert {cards[b["instance_id"]]["controller_seat_id"] for b in model.theirs} <= {opponent}
    assert {c["owner_seat_id"] for c in source["hand"]} <= {local}
    local_player = next(p for p in source["players"] if p.get("is_local"))
    assert local_player["seat_id"] == local and model.our_life == local_player["life_total"]


def test_the_18_35_board_is_sided_right_and_the_clocks_follow():
    source = fx.BUG_183539
    model = build_board_model(deepcopy(source))
    assert [b["name"] for b in model.ours] == ["Paradox Shaper"]
    assert [(b["name"], b["power"], b["toughness"]) for b in model.theirs] == [("Graft Surgeon", 3, 3)]
    cards = {c["instance_id"]: c for c in source["battlefield"]}
    assert cards[model.theirs[0]["instance_id"]]["grp_id"] not in source["deck_cards"]
    assert cards[model.ours[0]["instance_id"]]["grp_id"] in source["deck_cards"]
    assessment = _assess(source)
    assert (assessment.our_power, assessment.their_power) == (1, 3)
    assert assessment.our_clock is None and assessment.their_clock == 8


def _mirrored(source: dict) -> dict:
    """The same board seen from the other seat."""
    mirror = deepcopy(source)
    mirror["local_seat_id"], mirror["opponent_seat_id"] = source["opponent_seat_id"], source["local_seat_id"]
    for player in mirror["players"]:
        player["is_local"] = player["seat_id"] == mirror["local_seat_id"]
    mirror["hand"] = []
    return mirror


@pytest.mark.parametrize("name", ["BUG_183539", "BUG_182945_T13_UPKEEP", "SLOW_212608"])
def test_the_clock_simulation_is_symmetric(name):
    # Seen from their seat, our clock is their clock and vice versa: the attacker's
    # "everyone or hold back what dies" and the defender's kill-blocks apply to both sides.
    source = getattr(fx, name)
    ours = ba._clock_facts(build_board_model(deepcopy(source)))
    theirs = ba._clock_facts(build_board_model(_mirrored(source)))
    assert (ours.our_clock, ours.their_clock) == (theirs.their_clock, theirs.our_clock)
    assert (ours.our_lives, ours.their_lives) == (theirs.their_lives, theirs.our_lives)
