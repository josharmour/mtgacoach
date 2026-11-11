"""Busy admission is deferred, not a failed plan or a consumed threat refresh."""

import pytest
from tests.test_game_plan import FakeBackend, _plan_json, _state

from arenamcp.game_plan import GamePlanManager

DROPPED = "[BACKEND ERROR] request cancelled (dropped)"
DOWN = "[BACKEND ERROR] model server unavailable (backend down; background call skipped)"


@pytest.mark.parametrize("reason", ["first plan", "new opposing threat"])
def test_busy_then_free_lane_retries_after_short_cooldown(monkeypatch, plan_threads, reason):
    now = [100.0]
    monkeypatch.setattr("arenamcp.game_plan.time.monotonic", lambda: now[0])
    backend = FakeBackend([DROPPED, _plan_json("fresh line")])
    manager = GamePlanManager(backend)
    state = _state(turn=4)
    state["turn"]["active_player"] = 2
    manager.observe(state)
    prior = manager._parse(_plan_json("prior line"), 3) if reason != "first plan" else None
    manager._plan = prior
    monkeypatch.setattr(manager, "_should_reform", lambda *a, **k: reason)

    assert manager.request_reform(state)
    plan_threads[-1].join(timeout=2)
    assert not plan_threads[-1].is_alive()
    assert manager.current is prior
    assert manager._plan_outcome == "call deferred (background lane busy)"
    assert manager._threat_turn == 0
    now[0] += 1.9
    assert not manager.request_reform(state), "busy admission must not hot-loop"
    now[0] += 0.2
    assert manager.request_reform(state), "lane drop must not cause a 15-second blackout"
    plan_threads[-1].join(timeout=2)
    assert manager.current is not None and manager.current.path == "fresh line"
    assert backend.calls == 2
    if reason == "new opposing threat":
        assert manager._threat_turn == 4
        now[0] += 16
        assert not manager.request_reform(state), "a sent threat refresh still consumes its turn slot"


@pytest.mark.parametrize("responses", [[DOWN], ["malformed", DROPPED]])
def test_service_failure_or_dropped_repair_keeps_normal_cooldown(monkeypatch, plan_threads, responses):
    now = [100.0]
    monkeypatch.setattr("arenamcp.game_plan.time.monotonic", lambda: now[0])
    monkeypatch.setattr("arenamcp.game_plan.save_unusable_answer", lambda *a, **k: None)
    backend = FakeBackend(responses)
    manager = GamePlanManager(backend)
    state = _state(turn=4)
    state["turn"]["active_player"] = 2
    monkeypatch.setattr(manager, "_should_reform", lambda *a, **k: "new opposing threat")
    assert manager.request_reform(state)
    plan_threads[-1].join(timeout=2)
    assert manager._plan_outcome != manager._LANE_DEFERRED
    assert manager._threat_turn == 4
    now[0] += 2.1
    assert not manager.request_reform(state)
    assert backend.calls == len(responses)


def test_cancelled_admission_cannot_rearm_threat_refresh(monkeypatch, plan_threads):
    class Cancelled(FakeBackend):
        def complete(self, *args, **kwargs):
            kwargs["cancel_event"].set()
            return super().complete(*args, **kwargs)

    manager = GamePlanManager(Cancelled([DROPPED]))
    state = _state(turn=4)
    state["turn"]["active_player"] = 2
    monkeypatch.setattr(manager, "_should_reform", lambda *a, **k: "new opposing threat")
    assert manager.request_reform(state)
    plan_threads[-1].join(timeout=2)
    assert manager._threat_turn == 4
    assert manager._plan_outcome != manager._LANE_DEFERRED
