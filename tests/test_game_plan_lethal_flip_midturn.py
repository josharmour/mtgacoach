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
    monkeypatch.setattr(mgr, "_strategic_key", lambda state, sig=None: (key_holder["value"], (), (), ()))
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
        mgr._key_change_reason(base, (("defender", False, False, False), (), (), frozenset())) == "role flip"
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


def test_role_flip_folds_into_the_kept_plan_without_a_model_call(sync_reforms, monkeypatch):
    # E5b: a role flip never reforms — the live role is rendered into every
    # decision prompt, so observe() folds it into the kept plan in code and
    # no model call is spent, on our turn or the opponent's.
    be = FakeBackend([_plan_json()])
    key = {"value": ("aggressor", False, False, False)}
    mgr = _manager_with_key(monkeypatch, be, key)
    assert mgr.request_reform(_state(turn=5)) is True
    assert be.calls == 1

    # Role flipped aggressor -> defender mid-turn: folded in code, no call.
    key["value"] = ("defender", False, False, False)
    assert mgr.request_reform(_state(turn=5)) is False
    assert be.calls == 1
    assert mgr.current is not None and mgr.current.role == "defender"

    # The fold persists: the opponent's turn does not repeat it either.
    assert mgr.request_reform(_state(turn=5, active=2)) is False
    assert be.calls == 1
    assert mgr.current is not None and mgr.current.role == "defender"


def test_race_defender_noise_never_reforms(sync_reforms, monkeypatch):
    be = FakeBackend([_plan_json()])
    key = {"value": ("race", False, False, False)}
    mgr = _manager_with_key(monkeypatch, be, key)
    assert mgr.request_reform(_state(turn=5)) is True
    key["value"] = ("defender", False, False, False)
    assert mgr.request_reform(_state(turn=5)) is False
    assert mgr.request_reform(_state(turn=5, active=2)) is False
    assert be.calls == 1


def test_lethal_replans_inside_cooldown_despite_simultaneous_stall(sync_reforms, monkeypatch):
    be = FakeBackend([_plan_json(), _plan_json("stop lethal")])
    key = {"value": ("defender", False, False, False)}
    mgr = _manager_with_key(monkeypatch, be, key)
    del mgr._REFRESH_INTERVAL_S
    assert mgr.request_reform(_state())
    mgr._stall_count = mgr._STALL_REFORM_THRESHOLD
    key["value"] = ("defender", False, True, False)
    assert mgr.request_reform(_state(pending=True))
    assert mgr.current.path == "stop lethal"
    assert not mgr.request_reform(_state())
    assert be.calls == 2


def test_same_match_reload_keeps_a_lethal_baseline(sync_reforms, monkeypatch):
    key = {"value": ("aggressor", False, False, False)}
    mgr = _manager_with_key(monkeypatch, FakeBackend([_plan_json()]), key)
    assert mgr.request_reform(_state())
    saved = json.loads(json.dumps(mgr.export_for_reload()))
    be = FakeBackend([_plan_json("play defense after reload")])
    restored = _manager_with_key(monkeypatch, be, key)
    del restored._REFRESH_INTERVAL_S
    restored.restore_after_reload(saved, _state())
    assert not restored.request_reform(_state())
    key["value"] = ("defender", False, True, True)
    assert restored.request_reform(_state())
    assert restored.current.path == "play defense after reload"


@pytest.mark.parametrize("lethal", [True, False])
def test_legacy_handoff_keeps_the_signature_and_rechecks_visible_lethal(sync_reforms, monkeypatch, lethal):
    key = {"value": ("aggressor", False, False, False)}
    old = _manager_with_key(monkeypatch, FakeBackend([_plan_json()]), key)
    assert old.request_reform(_state())
    saved = json.loads(json.dumps(old.export_for_reload()))
    del saved["last_key"]
    key["value"] = ("defender", False, lethal, False)
    be = FakeBackend([_plan_json("defend against visible lethal")])
    restored = _manager_with_key(monkeypatch, be, key)
    del restored._REFRESH_INTERVAL_S
    restored.restore_after_reload(saved, _state())
    assert restored._last_sig == old._last_sig
    assert restored.request_reform(_state()) is lethal
    assert not restored.request_reform(_state())
    assert be.calls == int(lethal)


def test_legacy_restore_without_board_math_detects_later_lethal(sync_reforms, monkeypatch):
    key = {"value": ("defender", False, False, False)}
    old = _manager_with_key(monkeypatch, FakeBackend([_plan_json()]), key)
    assert old.request_reform(_state())
    saved = old.export_for_reload()
    del saved["last_key"]
    key["value"] = None
    be = FakeBackend([_plan_json("stop new lethal")])
    restored = _manager_with_key(monkeypatch, be, key)
    del restored._REFRESH_INTERVAL_S
    restored.restore_after_reload(saved, _state())
    key["value"] = ("defender", False, False, False)
    assert not restored.request_reform(_state())
    key["value"] = ("defender", False, True, False)
    assert restored.request_reform(_state())
    assert be.calls == 1
