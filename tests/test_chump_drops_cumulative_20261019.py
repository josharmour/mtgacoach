"""Chump blocks are dropped against what is still kept, never each against the full plan (#149).

2026-10-19 13:48 (match bd34f972 T9): at 14 life, Essence Channelers 8/7 and
6/5 (flying) attacked; the model chumped both ("chump both or take lethal 14").
The guard judged each chump alone ("stay at 8" / "stay at 6") and dropped both:
an empty SubmitBlockersReq, dead at exactly 14.
"""

from __future__ import annotations

from arenamcp.combat_strategy import wasteful_chump_blocks

THEM, ME = 1, 2
BIG, SMALL, CLAW, SLICKSHOT = 501, 502, 601, 602


def _creature(iid, name, power, toughness, seat, attacking=False, keywords=()):
    return {
        "instance_id": iid,
        "name": name,
        "type_line": "Creature",
        "power": power,
        "toughness": toughness,
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "keywords": list(keywords),
        "is_attacking": attacking,
        "object_kind": "CARD",
        "oracle_text": "",
    }


def _board(life=14):
    return {
        "local_seat_id": ME,
        "turn": {
            "turn_number": 9,
            "active_player": THEM,
            "phase": "Phase_Combat",
            "step": "Step_DeclareBlock",
        },
        "players": [
            {"seat_id": THEM, "life_total": 15, "is_local": False},
            {"seat_id": ME, "life_total": life, "is_local": True},
        ],
        "battlefield": [
            _creature(BIG, "Essence Channeler", 8, 7, THEM, attacking=True, keywords=["flying"]),
            _creature(SMALL, "Essence Channeler", 6, 5, THEM, attacking=True, keywords=["flying"]),
            _creature(CLAW, "Hired Claw", 1, 2, ME, keywords=["reach"]),
            _creature(SLICKSHOT, "Slickshot Show-Off", 1, 2, ME, keywords=["flying"]),
        ],
        "hand": [],
        "stack": [],
    }


def test_two_chumps_that_together_prevent_lethal_are_not_both_dropped():
    plan = {CLAW: BIG, SLICKSHOT: SMALL}
    dropped = wasteful_chump_blocks(_board(14), plan)
    assert CLAW not in dropped  # without the 8/7 block we are dead
    assert len(dropped) <= 1


def test_the_smaller_saving_goes_first_and_the_reason_states_the_combined_life():
    plan = {CLAW: BIG, SLICKSHOT: SMALL}
    dropped = wasteful_chump_blocks(_board(14), plan)
    if dropped:
        assert set(dropped) == {SLICKSHOT}
        assert "stay at 8 without the block" in dropped[SLICKSHOT]


def test_at_high_life_both_chumps_can_still_go():
    plan = {CLAW: BIG, SLICKSHOT: SMALL}
    assert set(wasteful_chump_blocks(_board(40), plan)) == {CLAW, SLICKSHOT}
