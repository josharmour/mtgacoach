"""A follow-up model opinion must not start another full decision window."""

import concurrent.futures
import threading
import time

import pytest
from tests.test_targeting_fallback_decline import _decision, _state

from arenamcp.action_planner import ActionPlanner


def prepare(monkeypatch, backend, budget):
    from arenamcp import action_planner

    planner = ActionPlanner(backend, timeout=budget)
    monkeypatch.setattr(action_planner, "critical_decision", lambda *a, **k: "")
    monkeypatch.setattr(planner, "_sized_budget", lambda *a: budget)
    return planner


def test_second_opinion_only_receives_remaining_budget(monkeypatch):
    budgets = []

    class Backend:
        def complete(self, *args, **kwargs):
            budgets.append(kwargs["request_timeout_s"])
            if len(budgets) == 1:
                time.sleep(0.04)
            return '{"option_ids": ["tgt:812"]}'

    planner = prepare(monkeypatch, Backend(), 1.0)

    def second(decision, state, data, chosen, concerns, ask, parse, message, answered_high):
        return parse(ask(message, effort="high", budget=15.0))

    monkeypatch.setattr(planner, "_second_look", second)
    assert planner._llm_decision_options(_decision([607, 812]), _state(own_ids=(607,), their_ids=(812,))) == [
        "tgt:812"
    ]
    assert len(budgets) == 2
    assert 0 < budgets[1] < budgets[0] - 0.03


def test_slow_legacy_typed_backend_is_bounded(monkeypatch):
    release = threading.Event()

    class Backend:
        def complete(self, *args, **kwargs):
            release.wait(2)
            return '{"option_ids": ["tgt:812"]}'

    planner = prepare(monkeypatch, Backend(), 0.05)
    started = time.monotonic()
    try:
        with pytest.raises(concurrent.futures.TimeoutError):
            planner._llm_decision_options(_decision([607, 812]), _state(own_ids=(607,), their_ids=(812,)))
        assert time.monotonic() - started < 1.0
    finally:
        release.set()
