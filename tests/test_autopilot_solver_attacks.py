"""Combat-solver fallback for auto-confirmed DeclareAttackers windows.

Live finding 2026-06-06: autopilot submitted "no attackers" every combat
because the only attack source was turn-plan intent. When intent is
absent, `_solver_attack_names` now consults the combat solver and only
overrides the empty confirm when attacking is strictly profitable.
"""

import arenamcp.autopilot as autopilot_module
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine


class _DummyPlanner:
    _timeout = 0.1
    _backend = object()

    def get_recent_diagnostics(self):
        return []


class _DummyMapper:
    window_rect = (0, 0, 100, 100)
    cache_size = 0

    def refresh_window(self):
        return self.window_rect

    def get_button_coord(self, name):
        return None


class _DummyController:
    def focus_mtga_window(self):
        return None


class _DummyBridge:
    connected = False

    def connect(self):
        return False


def _make_engine(monkeypatch) -> AutopilotEngine:
    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: _DummyBridge())
    return AutopilotEngine(
        planner=_DummyPlanner(),
        mapper=_DummyMapper(),
        controller=_DummyController(),
        get_game_state=lambda: {},
        config=AutopilotConfig(dry_run=True),
    )


def _creature(name, seat, power, toughness, iid, tapped=False, oracle=""):
    return {
        "name": name,
        "instance_id": iid,
        "controller_seat_id": seat,
        "type_line": "Creature — Bear",
        "power": power,
        "toughness": toughness,
        "is_tapped": tapped,
        "oracle_text": oracle,
    }


def _state(battlefield, legal_attackers, your_life=20, opp_life=20):
    return {
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": your_life},
            {"seat_id": 2, "life_total": opp_life},
        ],
        "battlefield": battlefield,
        "decision_context": {"legal_attackers": legal_attackers},
    }


def test_solver_attacks_into_empty_board(monkeypatch):
    engine = _make_engine(monkeypatch)
    state = _state(
        [_creature("Bear Cub", 1, 3, 3, 10)],
        legal_attackers=["Bear Cub"],
        opp_life=10,
    )
    assert engine._solver_attack_names(state) == ["Bear Cub"]


def test_solver_declines_suicidal_attack(monkeypatch):
    engine = _make_engine(monkeypatch)
    state = _state(
        [
            _creature("Llanowar Elves", 1, 1, 1, 10),
            _creature("Colossal Wall", 2, 4, 6, 20),
        ],
        legal_attackers=["Llanowar Elves"],
    )
    assert engine._solver_attack_names(state) == []


def test_solver_noop_without_legal_attackers(monkeypatch):
    engine = _make_engine(monkeypatch)
    state = _state(
        [_creature("Bear Cub", 1, 3, 3, 10)],
        legal_attackers=[],
    )
    assert engine._solver_attack_names(state) == []


def test_optimal_attacks_material_fields_not_swapped():
    """Regression: optimal_attacks previously read optimal_blocks' material
    fields swapped, so an attacker dying to a block was scored as a KILL.
    A 1/1 swinging into an untapped 4/6 must register as losing material,
    never gaining it."""
    from arenamcp.combat_solver import optimal_attacks

    elf = {"name": "Llanowar Elves", "instance_id": 10, "power": 1,
           "toughness": 1, "oracle_text": ""}
    wall = {"name": "Colossal Wall", "instance_id": 20, "power": 4,
            "toughness": 6, "oracle_text": ""}
    plan = optimal_attacks([elf], [wall], 20, 20, [wall], [])
    assert plan is not None
    if plan.attacker_names:  # solver chose to attack anyway
        assert plan.blockers_killed_material == 0  # a 1/1 cannot kill a 4/6


def test_solver_attacks_for_lethal_through_blockers(monkeypatch):
    engine = _make_engine(monkeypatch)
    # 5/5 + 4/4 vs one 2/2 blocker, opp at 5: lethal gets through even
    # against the best block.
    state = _state(
        [
            _creature("Big One", 1, 5, 5, 10),
            _creature("Big Two", 1, 4, 4, 11),
            _creature("Chump", 2, 2, 2, 20),
        ],
        legal_attackers=["Big One", "Big Two"],
        opp_life=5,
    )
    names = engine._solver_attack_names(state)
    assert "Big One" in names or "Big Two" in names
