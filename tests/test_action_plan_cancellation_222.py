"""A caller deadline must stop non-hedged network work, not only its waiter."""

import threading
import time

import pytest
from tests.test_game_plan import _state

from arenamcp.action_planner import ActionPlanner


@pytest.mark.parametrize("repair", [False, True])
def test_action_plan_timeout_cancels_its_cooperative_backend(monkeypatch, repair):
    release = threading.Event()
    blocked = threading.Event()
    stopped = threading.Event()
    calls = []

    class Backend:
        def complete(self, *args, **kwargs):
            calls.append(kwargs)
            if repair and len(calls) == 1:
                return "not JSON"
            cancel = kwargs.get("cancel_event")
            blocked.set()
            if cancel is not None and cancel.wait(5):
                stopped.set()
            else:
                release.wait(5)
            return '{"actions":[]}'

    planner = ActionPlanner(Backend(), timeout=5, land_drop_first=False)
    wait_for_model = planner._wait_for_model
    waits = 0

    def expire_in_flight(future, deadline):
        nonlocal waits
        waits += 1
        if repair and waits == 1:
            return wait_for_model(future, deadline)
        # Expire the real waiter after the specific worker is running. An
        # expired budget before repair admission legitimately starts no worker.
        assert blocked.wait(2), "expected the chosen backend call to be in flight"
        return wait_for_model(future, time.monotonic())

    monkeypatch.setattr(planner, "_wait_for_model", expire_in_flight)
    monkeypatch.setattr("arenamcp.action_planner.declaration_effort", lambda *a: ("", ""))
    try:
        planner.plan_actions(
            _state(turn=3),
            "decision_required",
            ["Attack with: Spider", "Done"],
            {"type": "declare_attackers"},
        )
        assert stopped.wait(3.0), "timed-out action work must not keep running beside later decisions"
        assert len(calls) == (2 if repair else 1)
    finally:
        release.set()


def test_successful_initial_call_does_not_cancel_the_repairs_fresh_token(monkeypatch):
    tokens = []

    class Backend:
        def complete(self, *args, **kwargs):
            cancel = kwargs.get("cancel_event")
            assert cancel is not None and not cancel.is_set()
            tokens.append(cancel)
            if len(tokens) == 1:
                return "not JSON"
            return '{"actions":[{"action_type":"declare_attackers","attacker_names":[]}]}'

    # This checks token ownership, not a one-second scheduling deadline. Keep
    # the normal planning budget so a busy CI worker can schedule the repair;
    # the tests above independently exercise short-deadline cancellation.
    planner = ActionPlanner(Backend(), timeout=5, land_drop_first=False)
    monkeypatch.setattr("arenamcp.action_planner.declaration_effort", lambda *a: ("", ""))
    plan = planner.plan_actions(
        _state(turn=3),
        "decision_required",
        ["Attack with: Spider", "Done"],
        {"type": "declare_attackers"},
    )
    assert len(plan.actions) == 1, (plan.fallback_reason, len(tokens), [t.is_set() for t in tokens])
    assert len(tokens) == 2 and tokens[0] is not tokens[1]
    assert not any(token.is_set() for token in tokens)
