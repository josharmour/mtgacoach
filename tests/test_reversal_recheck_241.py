"""BUG-241: a timed-out reversal re-check must not preserve an outage fallback pass (2026-11-02).

Symptom (ledger #241): "A timed-out reversal re-check preserves a fallback pass over an
otherwise usable action." Evidence commits 4106994f, c7b1185c, 0a0b80ab: while the model
server was failing, the planner's deterministic fallback passed; the next decision on the
same board chose a legal action, the reversal re-check ran out its budget, and
``_reversal_check_failed`` kept the fallback pass instead of the fresh choice.

Two merged controls answer it, and this file pins them end to end:

- provenance: ``DecisionMemory.record(..., model_unavailable=...)`` (70d9e2f) marks a pass
  that came from a model-server failure, and ``reversal()`` never treats it as a strategic
  hold, so no re-check is sent at all;
- board signature: ``reversal()`` only matches a pass recorded on the same board (eb72e38),
  so a changed board is not a reversal either.

The re-check also shares the first call's deadline (``ask`` clamps its budget to
``decision_deadline``), so a timed-out re-check cannot renew the decision's budget. A genuine
model hold still gets its re-check: that is ledger #151's behaviour, not this bug.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision

HOLD = "Holding Proft: discarding our threshold closer to kill a 2/1 is a bad trade"


def card(instance_id, name, zone_types=("Creature",), oracle="", owner=1):
    return {
        "instance_id": instance_id,
        "name": name,
        "card_types": list(zone_types),
        "oracle_text": oracle,
        "owner_seat_id": owner,
        "controller_seat_id": owner,
    }


PROFT = card(
    123,
    "Proft, Sinister Mastermind",
    oracle="Threshold — ... {B}, Discard this card: Target creature gets -3/-1 until end of turn.",
)
ISLAND = card(124, "Island", ("Land",), owner=1)


def state(battlefield=(), hand=(PROFT, ISLAND), turn=5):
    return {
        "match_id": "m241",
        "turn": {"turn_number": turn},
        "local_seat_id": 1,
        "players": [{"seat_id": 1, "life": 20, "is_local": True}, {"seat_id": 2, "life": 20}],
        "hand": list(hand),
        "battlefield": list(battlefield),
        "graveyard": [],
        "exile": [],
        "stack": [],
    }


BOARD = state(battlefield=[card(1, "Ghoul", owner=2)])
CHANGED = state(battlefield=[card(1, "Ghoul", owner=2), card(9, "Rat", owner=2)])

LAND = DecisionOption("idx:4", "Play Land: Island", meta={"actionType": "ActionType_Play", "instanceId": 124})


def menu():
    return PendingDecision(
        request_id=(1, 1),
        request_type="ActionsAvailable",
        options=(LAND, DecisionOption("pass", "Pass")),
        can_pass=True,
    )


def _reply(option_id: str, reasoning: str) -> str:
    return json.dumps({"option_ids": [option_id], "reasoning": reasoning})


class _Model:
    """Low effort takes the activation; high effort answers ``high`` or runs out its budget."""

    def __init__(self, high: str | None = None, first_call_s: float = 0.0):
        self.high = high
        self.first_call_s = first_call_s
        self.efforts: list = []
        self.prompts: list[str] = []
        self.timeouts: list[float] = []
        self.lock = threading.Lock()

    def complete(self, system, user, *args, **kwargs):
        if "PENDING DECISION" not in user:
            return "{}"
        effort = kwargs.get("reasoning_effort")
        with self.lock:
            first = not self.efforts
            self.efforts.append(effort)
            self.prompts.append(user)
            self.timeouts.append(float(kwargs.get("request_timeout_s") or 0.0))
        if first and self.first_call_s:
            time.sleep(self.first_call_s)
        if effort != "high":
            return _reply("idx:4", "The Island is our land drop; the Ghoul is not worth a card.")
        if self.high is None:
            raise TimeoutError("LLM streaming time budget exhausted")
        return self.high


def _planner(model: _Model, *, timeout: float = 5.0) -> ActionPlanner:
    return ActionPlanner(model, timeout=timeout, land_drop_first=False)


def _record_pass(planner: ActionPlanner, *, unavailable: bool) -> None:
    planner._memory().record(BOARD, menu(), ["pass"], HOLD, model_unavailable=unavailable)


@pytest.fixture(autouse=True)
def _short_budget(monkeypatch):
    monkeypatch.setattr(ActionPlanner, "_CRITICAL_DECISION_BUDGET_S", 0.3)


def test_an_outage_pass_is_not_a_reversal_and_the_fresh_choice_stands(caplog):
    model = _Model()
    planner = _planner(model)
    _record_pass(planner, unavailable=True)
    with caplog.at_level(logging.INFO, logger="arenamcp.action_planner"):
        assert planner.plan_decision_options(menu(), BOARD) == ["idx:4"]
    assert model.efforts == [None]  # one call: no re-check was sent for the outage pass
    assert not any("REVERSAL CHECK" in prompt for prompt in model.prompts)
    assert "reverses an earlier pass this turn" not in caplog.text
    assert "reversal_check" not in planner.get_last_decision_trace()


def test_a_genuine_model_hold_still_gets_the_recheck(caplog):
    model = _Model(high=None)
    planner = _planner(model)
    _record_pass(planner, unavailable=False)
    with caplog.at_level(logging.INFO, logger="arenamcp.action_planner"):
        assert planner.plan_decision_options(menu(), BOARD) == ["pass"]
    assert model.efforts == [None, "high"]
    assert "REVERSAL CHECK" in model.prompts[1]
    assert "reverses an earlier pass this turn" in caplog.text
    assert planner.get_last_decision_trace()["reversal_check"]["kept"] == "earlier pass"


def test_a_changed_board_is_not_a_reversal():
    model = _Model()
    planner = _planner(model)
    _record_pass(planner, unavailable=False)
    assert planner.plan_decision_options(menu(), CHANGED) == ["idx:4"]
    assert model.efforts == [None]
    assert not any("REVERSAL CHECK" in prompt for prompt in model.prompts)


def test_the_recheck_cannot_renew_the_first_calls_deadline(monkeypatch):
    monkeypatch.setattr(ActionPlanner, "_CRITICAL_DECISION_BUDGET_S", 5.0)
    model = _Model(high=None, first_call_s=0.7)
    planner = _planner(model, timeout=1.0)
    _record_pass(planner, unavailable=False)
    assert planner.plan_decision_options(menu(), BOARD) == ["pass"]
    assert len(model.timeouts) == 2
    first, recheck = model.timeouts
    # The re-check gets what is left of the first call's deadline (about 0.3 s here), never a
    # fresh critical budget of its own.
    assert recheck < first
    assert recheck < 0.6
