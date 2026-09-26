"""Solver override for planned attacks.

2026-09-24, two real matches:
- Opponent at 2 with Screaming Nemesis 3/3, Sun Titan 6/6 and Dwalin 2/1
  untapped: the planner swung three 1/1 Notary Hobbits "for lethal
  pressure" and all three died.
- Opponent at 5 with Witch's Familiar 2/3 and Savage Gorger 2/2 untapped:
  the planner swung only the 11/10 Spider (one chump). All three attackers
  would have killed both blockers and dealt 2.
"""

import arenamcp.autopilot as autopilot_module
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine

LOCAL, OPP = 1, 2


class _DummyBridge:
    connected = False

    def connect(self):
        return False


def _engine(monkeypatch) -> AutopilotEngine:
    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: _DummyBridge())
    return AutopilotEngine(planner=None, get_game_state=lambda: {}, config=AutopilotConfig(dry_run=True))


def _creature(iid, name, seat, power, toughness, oracle="", tapped=False) -> dict:
    return {
        "instance_id": iid,
        "name": name,
        "type_line": "Creature",
        "power": power,
        "toughness": toughness,
        "oracle_text": oracle,
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "is_tapped": tapped,
    }


def _state(battlefield, legal_ids, your_life, opp_life) -> dict:
    return {
        "players": [
            {"seat_id": LOCAL, "is_local": True, "life_total": your_life},
            {"seat_id": OPP, "is_local": False, "life_total": opp_life},
        ],
        "battlefield": battlefield,
        "decision_context": {"type": "declare_attackers", "legal_attacker_ids": legal_ids},
    }


def _hobbit_board(opp_blockers):
    hobbits = [_creature(700 + i, "The Notary Hobbits", LOCAL, 1, 1) for i in range(3)]
    return hobbits + opp_blockers, [700, 701, 702]


HOBBITS = ["The Notary Hobbits #1", "The Notary Hobbits #2", "The Notary Hobbits #3"]


def test_hobbits_into_three_blockers_are_held_back(monkeypatch):
    engine = _engine(monkeypatch)
    board, legal = _hobbit_board(
        [
            _creature(11, "Screaming Nemesis", OPP, 3, 3),
            _creature(12, "Sun Titan", OPP, 6, 6, "Vigilance"),
            _creature(13, "Dwalin, Weaponmaster", OPP, 2, 1),
        ]
    )
    assert engine._attack_override(HOBBITS, _state(board, legal, 11, 2)) == []


def test_lone_spider_becomes_the_full_swing(monkeypatch):
    engine = _engine(monkeypatch)
    board = [
        _creature(291, "Spider", LOCAL, 11, 10, "Reach"),
        _creature(354, "Spider", LOCAL, 2, 1, "Reach"),
        _creature(296, "Optimistic Scavenger", LOCAL, 5, 5),
        _creature(355, "Optimistic Scavenger", LOCAL, 1, 1),
        _creature(333, "Skyward Spider", LOCAL, 2, 2, "Ward {2}\nThis creature has flying as long as it's modified."),
        _creature(326, "Witch's Familiar", OPP, 2, 3),
        _creature(312, "Savage Gorger", OPP, 2, 2, "Flying"),
        _creature(303, "Scathe Zombies", OPP, 2, 2, tapped=True),
    ]
    got = engine._attack_override(["Spider #1"], _state(board, [291, 296, 333], 18, 5))
    assert got == ["Spider #1", "Optimistic Scavenger #1", "Skyward Spider"]


def test_good_planned_attack_is_kept(monkeypatch):
    engine = _engine(monkeypatch)
    board, legal = _hobbit_board([_creature(11, "Squirrel", OPP, 1, 1)])
    assert engine._attack_override(HOBBITS, _state(board, legal, 11, 5)) is None


def test_unresolvable_attacker_is_left_alone(monkeypatch):
    engine = _engine(monkeypatch)
    board, legal = _hobbit_board([_creature(11, "Sun Titan", OPP, 6, 6)])
    assert engine._attack_override(["Grizzly Bears"], _state(board, legal, 11, 5)) is None
