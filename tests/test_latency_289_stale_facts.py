"""#289 (ledger 220b8546): a slow typed low answer and stale facts must not compound.

The historical window reported the low leg at 4.051 s while the high leg exhausted
its budget and the loop stalled 19 s; a big-board fallback and a stale strategy
then compounded the outcome. The latency half is pinned here: a usable low typed
answer wins the hedge at its own latency and the high leg is cancelled rather than
awaited. The freshness half is pinned too: an answer naming an option that is no
longer legal is filtered out and replaced by a legal deterministic fallback, so a
stale availability fact cannot reach submission.
"""

import time

from tests.test_targeting_fallback_decline import _decision, _state

from arenamcp import action_planner
from arenamcp.action_planner import ActionPlanner


def test_a_usable_low_typed_answer_is_not_held_for_the_high_leg(monkeypatch):
    monkeypatch.setattr(action_planner, "critical_decision", lambda *a, **k: "removal")

    class Backend:
        def __init__(self):
            self.calls = []

        def complete(self, *args, **kwargs):
            effort = kwargs.get("reasoning_effort")
            self.calls.append(effort)
            if effort == "high":
                cancel = kwargs.get("cancel_event")
                assert cancel is not None and cancel.wait(2), "usable low answer was held for high"
                raise TimeoutError("cancelled")
            time.sleep(0.2)
            return '{"option_ids": ["tgt:812"], "reasoning": "kill the threat"}'

    class Warm:
        def complete(self, *args, **kwargs):
            return '{"option_ids": ["tgt:812"], "reasoning": "warm"}'

    # Warm the prompt-construction imports so the timed region measures the hedge.
    ActionPlanner(Warm(), timeout=1.0)._plan_decision_options(
        _decision([607, 812]), _state(own_ids=(607,), their_ids=(812,))
    )

    backend = Backend()
    planner = ActionPlanner(backend, timeout=1.0)
    planner._sized_budget = lambda *a: 1.0
    planner._last_decision_trace = {}
    started = time.monotonic()
    chosen = planner._plan_decision_options(_decision([607, 812]), _state(own_ids=(607,), their_ids=(812,)))
    elapsed = time.monotonic() - started

    assert chosen == ["tgt:812"]
    assert elapsed < 0.9, f"low answer took {elapsed:.2f}s against a 1.0s budget"
    assert set(backend.calls) == {"high", None}
    hedge = planner._last_decision_trace["hedge"]
    assert hedge["winner"] == "low"
    assert hedge["wait_after_low_ms"] == 0


def test_a_stale_option_id_is_replaced_by_a_legal_fallback(monkeypatch):
    monkeypatch.setattr(action_planner, "critical_decision", lambda *a, **k: "")

    class Backend:
        def complete(self, *args, **kwargs):
            return '{"option_ids": ["tgt:999"], "reasoning": "kill the stale threat"}'

    planner = ActionPlanner(Backend(), timeout=1.0)
    planner._last_decision_trace = {}
    decision = _decision([607, 812])
    chosen = planner._plan_decision_options(decision, _state(own_ids=(607,), their_ids=(812,)))

    assert "tgt:999" not in chosen
    assert chosen and set(chosen) <= set(decision.option_ids())
