"""#253: a schema-valid empty plan, a busy lane, and a missing first plan.

cd0b1978 / e53b79f6: long decision chains carried a schema-shaped answer that
had a role and nothing else, and the loop ran on a stale plan (or none) without
saying why. The empty answer must be rejected like any other unusable one, the
prior plan kept, stale work cancelled, and the reported reason must name what
actually happened rather than the service's first-token time.

The request policy itself (fast bounded first plan, shared JSON-retry budget,
post-completion cooldown) is pinned by tests/test_latency_budget_222_followup.py;
these tests pin the answer handling and the reporting that #253 turns on.
"""

from __future__ import annotations

import json
import logging
import threading

import pytest
from tests.test_game_plan import FakeBackend, _plan_json, _state

from arenamcp.game_plan import GamePlanManager

# A schema-valid answer with a role and no plan content: the shape cd0b1978 and
# e53b79f6 returned. It parses; it is not a plan.
ROLE_ONLY = json.dumps({"role": "aggressor", "role_reason": "we are ahead on board"})

SKIP_SENTINELS = [
    "[BACKEND ERROR] model server unavailable (backend down; background call skipped)",
    "[BACKEND ERROR] model server unavailable (circuit open; retry in 12s)",
    "[BACKEND ERROR] request cancelled (dropped)",
]


@pytest.fixture(autouse=True)
def _no_debug_writes(monkeypatch):
    """Keep unusable-answer diagnostics out of the user's runtime evidence directory."""
    monkeypatch.setattr("arenamcp.game_plan.save_unusable_answer", lambda *a, **k: None)


def _manager(responses) -> tuple[GamePlanManager, FakeBackend]:
    backend = FakeBackend(responses)
    backend.model = "current"  # the shipped hosted model: fast bounded first plan
    return GamePlanManager(backend), backend


def _formed(manager: GamePlanManager, turn: int = 3):
    plan = manager.maybe_reform(_state(turn=turn))
    assert plan is not None and not plan.is_empty()
    return plan


def test_a_schema_valid_empty_answer_is_rejected_and_retried_once(caplog):
    manager, backend = _manager([_plan_json(), ROLE_ONLY, _plan_json("updated line")])
    prior = _formed(manager)
    with caplog.at_level(logging.INFO, logger="arenamcp.game_plan"):
        plan = manager.maybe_reform(_state(turn=4), force=True)
    assert plan is not None and plan is not prior and plan.path == "updated line"
    # One bounded retry at low effort, not a third full call.
    assert [request[2]["reasoning_effort"] for request in backend.requests[1:]] == ["high", "low"]
    assert "NOT A USABLE GAME PLAN" in backend.requests[2][1]
    unusable = [record.getMessage() for record in caplog.records if "no usable plan" in record.getMessage()]
    assert len(unusable) == 1
    assert "role_reason" in unusable[0], "the rejected answer must be logged, not just counted"


def test_a_schema_valid_empty_answer_keeps_the_prior_plan_and_names_the_reason():
    manager, backend = _manager([_plan_json(), ROLE_ONLY, ROLE_ONLY])
    prior = _formed(manager)
    assert manager.maybe_reform(_state(turn=4), force=True) is prior
    assert len(backend.requests) == 3, "two unusable answers must not buy a third call"
    status = manager.plan_status(_state(turn=4))
    assert status["state"] == "ready"
    assert status["last_outcome"] == "no usable plan from the model"
    assert manager._refresh_failed == (4, "no usable plan from the model")
    block = manager.strategy_block(_state(turn=4))
    assert "PLAN NOT REFRESHED: the re-plan on turn 4 failed (no usable plan from the model)" in block, (
        "the kept plan must be flagged as stale in the decision prompt"
    )


@pytest.mark.parametrize("sentinel", SKIP_SENTINELS)
def test_a_busy_or_degraded_lane_keeps_the_prior_plan_without_a_retry(caplog, sentinel):
    manager, backend = _manager([_plan_json(), sentinel])
    prior = _formed(manager)
    with caplog.at_level(logging.INFO, logger="arenamcp.game_plan"):
        assert manager.maybe_reform(_state(turn=4), force=True) is prior
    assert len(backend.requests) == 2, "a call that was never made must not be retried"
    assert manager.plan_status(_state(turn=4))["last_outcome"] == ("call not made (model server unavailable)")
    assert any(
        "game-plan call not made (keeping prior plan)" in record.getMessage() for record in caplog.records
    )


def test_the_first_plan_is_requested_fast_and_reports_no_plan(caplog):
    manager, backend = _manager([ROLE_ONLY, ROLE_ONLY])
    with caplog.at_level(logging.WARNING, logger="arenamcp.game_plan"):
        assert manager.maybe_reform(_state(turn=4)) is None
        status = manager.plan_status(_state(turn=4))
    first, retry = backend.requests[0][2], backend.requests[1][2]
    # First availability is its own bounded request, not the deep refresh path.
    assert first["reasoning_effort"] == "none"
    assert 0 < first["request_timeout_s"] <= 20
    assert retry["reasoning_effort"] == "none"
    assert retry["request_timeout_s"] <= first["request_timeout_s"]
    assert status["state"] == "none"
    assert status["last_outcome"] == "no usable plan from the model"
    warning = [
        record.getMessage() for record in caplog.records if "No game plan on turn 4" in record.getMessage()
    ]
    assert warning and "no usable plan from the model" in warning[0]


def test_a_superseded_reform_discards_its_answer_and_keeps_the_prior_plan(caplog):
    cancel = threading.Event()

    class Cancelling(FakeBackend):
        def complete(self, system, user, max_tokens, **kwargs):
            cancel.set()  # the turn passed while the model was answering
            return super().complete(system, user, max_tokens, **kwargs)

    backend = Cancelling([_plan_json(), _plan_json("superseded line")])
    backend.model = "current"
    manager = GamePlanManager(backend)
    prior = _formed(manager)
    with caplog.at_level(logging.INFO, logger="arenamcp.game_plan"):
        assert manager.maybe_reform(_state(turn=4), force=True, _cancel=cancel) is prior
    assert len(backend.requests) == 2
    # The replacement owns the status: a superseded answer is not a failure.
    assert manager._plan_outcome == "ok"
    assert manager._refresh_failed is None
    assert any("Discarded a superseded game plan" in record.getMessage() for record in caplog.records)


def test_no_plan_still_gives_the_planner_a_bounded_strategy_block():
    manager = GamePlanManager(FakeBackend([]))
    block = manager.strategy_block(_state(turn=4))
    # Deterministic board math stands in for the missing plan; nothing raises.
    assert "STRATEGIC ROLE" in block
    assert "game plan T" not in block
