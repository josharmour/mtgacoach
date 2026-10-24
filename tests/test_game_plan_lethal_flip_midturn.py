"""#223: mid-turn reform triggers wait for the opponent's turn; a genuine lethal flip does not.

The 2026-10-23 audit adopted plan: run the deep reform on the opponent's turn
planning our next turn, queue mid-turn triggers (resumed after a decision /
stale plan / role flip) to the end of the turn, keep the first quick low-effort
plan — but a lethal flip (we can suddenly win or suddenly die) still replans
the current turn immediately, because waiting could cost the game.
"""

import json
from types import SimpleNamespace

import pytest

from arenamcp.game_plan import GamePlanManager


class FakeBackend:
    """Records calls and returns a scripted response per call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def complete(self, system, user, max_tokens, **kwargs):
        self.calls += 1
        if self._responses:
            return self._responses.pop(0)
        return "{}"


def _plan_json(path="race for lethal ~T6"):
    return json.dumps(
        {
            "win_conditions": ["aggro beatdown"],
            "path": path,
            "threat": "opponent flyers",
            "develop_next": "deploy a 2-drop",
        }
    )


def _state(turn=5, active=1, pending=False):
    state = {
        "players": [
            {"is_local": True, "seat_id": 1, "life_total": 20, "hand_size": 5},
            {"is_local": False, "seat_id": 2, "life_total": 20, "hand_size": 5},
        ],
        "turn": {"turn_number": turn, "active_player": active, "phase": "Phase_Main1"},
        "battlefield": [],
    }
    if pending:
        state["pending_decision"] = {"type": "optional_action"}
    return state


@pytest.fixture
def sync_reforms(monkeypatch):
    """Run request_reform's background thread inline so tests see its result."""
    def fake_thread(target=None, **kwargs):
        return SimpleNamespace(start=target)

    monkeypatch.setattr("arenamcp.game_plan.threading.Thread", fake_thread)


def _manager_with_key(monkeypatch, backend, key_holder):
    mgr = GamePlanManager(backend)
    mgr._REFRESH_INTERVAL_S = 0.0  # instance shadow: tests fire back-to-back
    monkeypatch.setattr(
        mgr, "_strategic_key", lambda state, sig=None: (key_holder["value"], (), (), ())
    )
    return mgr


def test_key_change_reason_splits_lethal_flip_from_role_flip():
    mgr = GamePlanManager(FakeBackend([]))
    base = (("aggressor", False, False, False), (), (), frozenset())

    # A lethal flag moved: we can suddenly win or die.
    assert (
        mgr._key_change_reason(base, (("aggressor", False, True, False), (), (), frozenset()))
        == "lethal flip"
    )
    assert (
        mgr._key_change_reason(base, (("aggressor", False, False, True), (), (), frozenset()))
        == "lethal flip"
    )
    # Only the role moved (beyond the race<->defender noise pair).
    assert (
        mgr._key_change_reason(base, (("defender", False, False, False), (), (), frozenset()))
        == "role flip"
    )
    # Race <-> defender is noise: no reform at all.
    assert (
        mgr._key_change_reason(
            (("race", False, False, False), (), (), frozenset()),
            (("defender", False, False, False), (), (), frozenset()),
        )
        == ""
    )


def test_lethal_flip_replans_our_turn_immediately(sync_reforms, monkeypatch):
    be = FakeBackend([_plan_json(), _plan_json("kill them this turn")])
    key = {"value": ("aggressor", False, False, False)}
    mgr = _manager_with_key(monkeypatch, be, key)

    # First plan on our turn (quick low-effort call) — unchanged behaviour.
    assert mgr.request_reform(_state(turn=5)) is True
    assert be.calls == 1

    # The board math now says the opponent can kill us (or we them): the reform
    # starts mid-turn instead of waiting for the opponent's turn.
    key["value"] = ("aggressor", False, True, True)
    assert mgr.request_reform(_state(turn=5)) is True
    assert be.calls == 2
    assert mgr.current.path == "kill them this turn"


def test_lethal_flip_starts_even_with_a_decision_pending(sync_reforms, monkeypatch):
    be = FakeBackend([_plan_json(), _plan_json("stabilise now")])
    key = {"value": ("defender", False, False, False)}
    mgr = _manager_with_key(monkeypatch, be, key)
    assert mgr.request_reform(_state(turn=3)) is True
    assert be.calls == 1

    key["value"] = ("defender", True, False, True)  # we are dead in two
    # Not a routine reason: it does not wait for a quiet moment either.
    assert mgr.request_reform(_state(turn=3, pending=True)) is True
    assert be.calls == 2


def test_role_flip_on_our_turn_waits_for_the_opponents_turn(sync_reforms, monkeypatch):
    be = FakeBackend([_plan_json(), _plan_json("plan B")])
    key = {"value": ("aggressor", False, False, False)}
    mgr = _manager_with_key(monkeypatch, be, key)
    assert mgr.request_reform(_state(turn=5)) is True
    assert be.calls == 1

    # Role flipped aggressor -> defender mid-turn: queued, not started.
    key["value"] = ("defender", False, False, False)
    assert mgr.request_reform(_state(turn=5)) is False
    assert be.calls == 1

    # The queued trigger fires on the opponent's turn (planning our next one).
    assert mgr.request_reform(_state(turn=5, active=2)) is True
    assert be.calls == 2
    assert mgr.current.path == "plan B"


def test_race_defender_noise_never_reforms(sync_reforms, monkeypatch):
    be = FakeBackend([_plan_json()])
    key = {"value": ("race", False, False, False)}
    mgr = _manager_with_key(monkeypatch, be, key)
    assert mgr.request_reform(_state(turn=5)) is True
    key["value"] = ("defender", False, False, False)
    assert mgr.request_reform(_state(turn=5)) is False
    assert mgr.request_reform(_state(turn=5, active=2)) is False
    assert be.calls == 1
