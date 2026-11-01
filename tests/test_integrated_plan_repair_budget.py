"""Latency and legality repairs must share the initial hosted plan's budget."""

import pytest
from tests.test_plan_availability_feedback import Backend
from tests.test_plan_mana_repair import BAD, GOOD, mana_state

from arenamcp.game_plan import GamePlanManager


@pytest.mark.parametrize("spent", [15.0, 20.0, 21.0])
def test_hosted_mana_repair_preserves_fast_effort_and_remaining_budget(monkeypatch, spent):
    clock = [100.0]
    monkeypatch.setattr("arenamcp.game_plan.time.monotonic", lambda: clock[0])

    class TimedBackend(Backend):
        model = "current"

        def complete(self, *args, **kwargs):
            answer = super().complete(*args, **kwargs)
            if len(self.requests) == 1:
                clock[0] += spent
            return answer

    backend = TimedBackend(BAD, GOOD)
    plan = GamePlanManager(backend, timeout=75)._reform(mana_state(), 9)
    assert backend.requests[0][1]["request_timeout_s"] == 20
    assert all(request[1]["reasoning_effort"] == "none" for request in backend.requests)
    assert plan is not None
    if spent < 20:
        assert len(backend.requests) == 2
        assert backend.requests[1][1]["request_timeout_s"] == 5
        assert plan.path == "Wait for a second green source"
        assert plan.effort == "none"
    else:
        assert len(backend.requests) == 1
        assert not plan.path
        assert plan.turn_plan[0]["cast"] == []


def test_hosted_json_retry_cannot_start_a_third_mana_repair():
    backend = Backend("{}", BAD, GOOD)
    backend.model = "current"
    plan = GamePlanManager(backend)._reform(mana_state(), 9)
    assert len(backend.requests) == 2
    assert all(request[1]["reasoning_effort"] == "none" for request in backend.requests)
    assert plan is not None and not plan.path
    assert plan.turn_plan[0]["cast"] == []
