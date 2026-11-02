"""BUG-249: a first token is not a first answer, and a cancelled plan must not compound.

The retained trace (docs/latency-222-triage-20261101.md, #249) is "repeated
first-token delays and cancelled background plans persist across matches". The
controls that answer it are already merged:

- the proxy cuts its own stream at the request budget and never retries that
  cut (#222, d27b878); a stream that produced a token is slow-but-alive, so it
  degrades health without tripping the circuit;
- a hosted first plan runs at effort ``none`` inside
  ``_INITIAL_HOSTED_PLAN_TIMEOUT_S`` (20 s), and a reform that follows a
  cancellation is bounded the same way (#141/#223);
- a cancelled plan releases the strategic lane for the next match (#223).

What was never pinned is the distinction the trace turns on: reasoning tokens
satisfy the first-token deadline while producing no answer at all, so a plan
can spend its whole budget "alive" and still be cancelled with zero content
(the 650ae555 record: 45.3 s, 12,439 reasoning characters, 0 content). And
with no plan yet formed, repeated cuts must stay inside the fast bounded mode
instead of escalating to another full-effort call that will be cancelled again.
"""

from __future__ import annotations

import time

import pytest
from tests.proxy_fakes import chunk, make_backend

from arenamcp.action_planner import ActionPlanner
from arenamcp.backend_health import BackendHealth, HealthState
from arenamcp.backends.proxy import BackendError
from arenamcp.game_plan import GamePlanManager
from threat_boards_20261019 import ME, OPP, card, state

CUT = "LLM streaming time budget exhausted"


def _reasoning_only_past_the_budget(total_s: float = 1.5):
    """Reasoning arrives immediately; no content ever does."""

    def gen():
        stop = time.monotonic() + total_s
        while time.monotonic() < stop:
            time.sleep(0.02)
            yield chunk(reasoning="weighing the attack")

    return iter(gen())


def _board(turn: int = 6):
    return state(
        [card(901, "Grizzly Bears", ME, power=2, toughness=2)],
        match="d9a33232",
        local=ME,
        life=18,
        opp_life=18,
        turn=turn,
        active=OPP,
        phase="Phase_Main1",
        step="",
        their_hand=3,
        hand=[
            card(902, "Shock", ME, text="Shock deals 2 damage to any target."),
            card(903, "Mountain", ME),
            card(904, "Goblin Arsonist", ME, power=1, toughness=1),
        ],
    )


class _CutBackend:
    """The shape ProxyBackend raises when its own stream budget cut wins."""

    model = "current"

    def __init__(self):
        self.calls: list[tuple[str | None, float | None]] = []

    def complete(self, *args, **kwargs):
        self.calls.append((kwargs.get("reasoning_effort"), kwargs.get("request_timeout_s")))
        raise BackendError(CUT)


def test_reasoning_without_content_is_a_first_token_but_not_a_first_answer():
    be = make_backend([_reasoning_only_past_the_budget()])
    planner = ActionPlanner(be, timeout=0.4, land_drop_first=False)

    with pytest.raises(BackendError) as exc:
        planner._call_llm(
            "sys",
            "user",
            64,
            timeout_s=0.4,
            call_class="decision.plan",
            yield_background=False,
        )

    assert CUT in str(exc.value)
    assert exc.value.kind == "timeout"
    assert exc.value.first_token_seen is True  # reasoning arrived: slow, not dead
    assert len(be._client.calls) == 1  # our own cut is never retried

    metrics = be.last_request_metrics
    assert metrics["status"] == "error"
    assert metrics["attempts"] == 1
    # The distinction #249 turns on: a token arrived long before any answer.
    assert metrics["ttft_ms"] is not None and metrics["ttft_ms"] < 300
    assert metrics["first_content_ms"] is None
    assert metrics["stream_content_chars"] == 0
    assert metrics["stream_reasoning_chars"] > 0

    # Slow-but-alive: health sees it, the circuit stays closed for the next match.
    assert BackendHealth.instance().state is HealthState.DEGRADED
    assert be.circuit_snapshot()["consecutive_failures"] == 0


def test_repeated_cuts_with_no_plan_stay_in_the_fast_bounded_mode():
    backend = _CutBackend()
    mgr = GamePlanManager(backend)
    board = _board()

    for turn in (6, 7, 8):
        assert mgr._reform(board, turn) is None

    # Every attempt while no plan exists is cheap and bounded; none escalates to
    # another full-effort call that would outlive its turn and be cancelled again.
    assert len(backend.calls) == 3
    assert [effort for effort, _ in backend.calls] == ["none", "none", "none"]
    for _, timeout_s in backend.calls:
        assert timeout_s == pytest.approx(mgr._INITIAL_HOSTED_PLAN_TIMEOUT_S, abs=0.5)
        assert timeout_s < mgr._PLAN_TIMEOUT_S

    assert mgr._plan is None
    assert mgr._reform_failure == "call failed"
