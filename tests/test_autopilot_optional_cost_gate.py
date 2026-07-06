"""Regression tests for the 2026-07-05 Go-Shintai self-destruction.

Live chain: an end-step PayCostsReq from Go-Shintai of Hidden Cruelty's
optional trigger ("you may pay {1} ... destroy target creature with
toughness X or less") was blind auto-paid, all opponent creatures exceeded
the toughness cap, and the forced SelectTargets destroyed the user's own
1/1 Spirit. The gate: only blind auto-pay costs of actions the autopilot
itself just submitted; out-of-band cancellable harmful triggers get a
pay/decline decision (decline when the LLM is unavailable).
"""

import time

import arenamcp.autopilot as autopilot_module
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine


class _DummyBridge:
    connected = False

    def connect(self):
        return False


class _NoLLMPlanner:
    """Planner whose pay/decline LLM path is unavailable."""

    def plan_pay_or_decline(self, name, oracle, game_state):
        return None


class _PayPlanner:
    def plan_pay_or_decline(self, name, oracle, game_state):
        return True


class _DeclinePlanner:
    def plan_pay_or_decline(self, name, oracle, game_state):
        return False


def _engine(monkeypatch, planner=None) -> AutopilotEngine:
    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: _DummyBridge())
    return AutopilotEngine(
        planner=planner or _NoLLMPlanner(),
        mapper=None,
        controller=None,
        get_game_state=lambda: {},
        config=AutopilotConfig(dry_run=False),
    )


def _go_shintai_state(can_cancel=True):
    return {
        "turn": {"turn_number": 14},
        "_bridge_can_cancel": can_cancel,
        "decision_context": {"type": "pay_costs", "sourceId": 900},
        "stack": [
            {
                "instance_id": 900,
                "name": "Go-Shintai of Hidden Cruelty",
                "oracle_text": (
                    "you may pay {1}. when you do, destroy target creature "
                    "with toughness x or less"
                ),
            }
        ],
        "battlefield": [],
        "local_seat_id": 1,
    }


def test_out_of_band_harmful_cost_declined_when_llm_unavailable(monkeypatch):
    eng = _engine(monkeypatch)
    reason = eng._should_decline_optional_cost(_go_shintai_state())
    assert reason is not None
    assert "declining conservatively" in reason


def test_out_of_band_harmful_cost_respects_llm_decline(monkeypatch):
    eng = _engine(monkeypatch, planner=_DeclinePlanner())
    reason = eng._should_decline_optional_cost(_go_shintai_state())
    assert reason is not None
    assert "LLM chose decline" in reason


def test_out_of_band_harmful_cost_pays_when_llm_says_pay(monkeypatch):
    eng = _engine(monkeypatch, planner=_PayPlanner())
    assert eng._should_decline_optional_cost(_go_shintai_state()) is None


def test_own_fresh_cast_always_auto_pays(monkeypatch):
    # The normal casting flow: autopilot cast something seconds ago; the
    # PayCosts belongs to it. No LLM call, no decline.
    eng = _engine(monkeypatch)
    eng._last_cast_submitted = (14, "rampant growth")
    eng._last_cast_submitted_ts = time.monotonic()
    assert eng._should_decline_optional_cost(_go_shintai_state()) is None


def test_stale_own_cast_does_not_bless_the_cost(monkeypatch):
    # A cast from 2 minutes ago is not this window's source (live incident:
    # Rampant Growth at 23:00:26 vs the trigger at 23:01:01).
    eng = _engine(monkeypatch)
    eng._last_cast_submitted = (12, "rampant growth")
    eng._last_cast_submitted_ts = time.monotonic() - 120.0
    assert eng._should_decline_optional_cost(_go_shintai_state()) is not None


def test_non_cancellable_cost_keeps_auto_pay(monkeypatch):
    # No pay/decline choice exists — nothing to gate.
    eng = _engine(monkeypatch)
    assert eng._should_decline_optional_cost(_go_shintai_state(can_cancel=False)) is None


def test_benign_trigger_keeps_auto_pay(monkeypatch):
    eng = _engine(monkeypatch)
    state = _go_shintai_state()
    state["stack"][0]["oracle_text"] = "you may pay {2}. if you do, draw a card."
    assert eng._should_decline_optional_cost(state) is None
