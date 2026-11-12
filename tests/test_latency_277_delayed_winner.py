"""#277: a slow declaration leg must not delay a usable low-effort answer.

Historical signature (faa03ef3/bfd351f0): the high-effort declaration leg
exhausted its budget and only then did the low-effort answer stand and the
attack execute -- a delayed winner. On current master the bounded declaration
hedge (``action_planner.py:1162``, b352b8a, per-leg cancel events 70d9e2f)
returns the first usable answer and cancels the other leg, so a ready low answer
wins at once and the attack is submitted.

These tests drive the real ``ProxyBackend`` so queue/first-token/generation/
cancellation timing is recorded by ``llm_trace`` (#231) as in production. The
retained traces are absent from this checkout, so this is a deterministic
reproduction of the mechanism on current master, not a live latency sample.
"""

import json
import threading
import time
from types import SimpleNamespace as NS

from tests.proxy_fakes import MODEL, URL, chunk

from arenamcp import llm_trace
from arenamcp.action_planner import ActionPlanner, ActionType
from arenamcp.backends.proxy import ProxyBackend

DECLARATION = json.dumps({"action_type": "declare_attackers", "attacker_names": ["Odin"]})
MENU = ["Attack with: Odin (5/5)", "Done"]


def board():
    """Odin's five power against their five life: ``declaration_effort`` says high."""
    return {
        "local_seat_id": 1,
        "turn": {"turn_number": 4, "active_player": 1, "phase": "Phase_Combat"},
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20},
            {"seat_id": 2, "is_local": False, "life_total": 5},
        ],
        "battlefield": [
            {
                "instance_id": 21,
                "name": "Odin",
                "type_line": "Creature — God",
                "power": 5,
                "toughness": 5,
                "controller_seat_id": 1,
            },
        ],
        "_bridge_request_type": "DeclareAttackers",
    }


class LegClient:
    """A fake OpenAI client whose high leg is slow in one of three ways."""

    #: How long the low leg waits for the high leg to speak before answering.
    SYNC_WAIT_S = 5.0

    def __init__(self, high_mode):
        self.high_mode = high_mode
        self.finished = []
        self.first_call_at = None
        # Deterministic leg ordering (#277 CI): without a gate the ready low
        # answer could win before the high leg's thread reached its first
        # attempt, cancelling it with attempts=0 and no TTFT to assert on.
        # The low leg instead waits for proof that the high leg spoke.
        self.high_spoke = threading.Event()
        self.high_finished = threading.Event()
        self.low_wait_succeeded = False
        self.chat = NS(completions=NS(create=self._create))

    def with_options(self, **options):
        return self

    def _create(self, **params):
        if self.first_call_at is None:
            self.first_call_at = time.monotonic()
        effort = params["extra_body"]["chat_template_kwargs"].get("reasoning_effort")
        if effort != "high":
            return self._low()
        if self.high_mode == "slow_valid":
            return self._slow_valid()
        if self.high_mode == "invalid":
            return self._invalid()
        return self._streaming()

    def _low(self):
        """Answer at once -- but only once the losing leg has spoken."""
        gate = self.high_finished if self.high_mode == "invalid" else self.high_spoke
        self.low_wait_succeeded = gate.wait(self.SYNC_WAIT_S)
        return iter([chunk(DECLARATION, finish="stop")])

    def _slow_valid(self):
        """A valid declaration that would have been usable -- but only after 1.2 s."""

        def generate():
            yield chunk("thinking", reasoning="weighing the attack")
            # Resuming past this yield proves the proxy consumed the reasoning
            # chunk above and recorded its TTFT; only then may low answer.
            self.high_spoke.set()
            time.sleep(1.2)
            yield chunk(DECLARATION)
            self.finished.append("high")

        return generate()

    def _invalid(self):
        """A complete-but-unusable declaration."""

        def generate():
            yield chunk("not json", finish="stop")
            # Resuming past this yield proves the proxy consumed the whole
            # response; releasing low here keeps both legs tracing as ok.
            self.high_finished.set()

        return generate()

    def _streaming(self):
        """Generation that outlives any short budget without ever being usable."""

        def generate():
            yield chunk("thinking", reasoning="weighing the attack")
            # Same proof as _slow_valid: TTFT is recorded before low answers.
            self.high_spoke.set()
            for _ in range(60):
                time.sleep(0.05)
                yield chunk(" more")
            self.finished.append("high")

        return generate()


#: Every scenario races exactly one high leg against one low leg.
TRACED_LEGS = 2


def plan(high_mode, timeout=2.0):
    """One declaration decision; returns (plan, model_time_s, trace summary)."""
    be = ProxyBackend(model=MODEL, base_url=URL)
    be._client = LegClient(high_mode)
    planner = ActionPlanner(be, timeout=timeout, land_drop_first=False)
    trace = llm_trace.start()
    started = time.monotonic()
    try:
        result = planner.plan_actions(board(), "decision_required", list(MENU), {"type": "declare_attackers"})
        elapsed = time.monotonic() - started
        deadline = time.monotonic() + 2.0
        # One entry per completed leg -- not per create() call seen so far,
        # which raced a loser's cancellation bookkeeping.
        while len(trace.summary()["requests"]) < TRACED_LEGS and time.monotonic() < deadline:
            time.sleep(0.02)
        summary = trace.summary()
    finally:
        llm_trace.finish(trace)
    assert len(summary["requests"]) == TRACED_LEGS, (
        f"expected {TRACED_LEGS} traced legs within budget: {summary['requests']}"
    )
    assert be._client.low_wait_succeeded, f"the low leg answered without observing the losing {high_mode} leg"
    setup = (be._client.first_call_at or started) - started
    return result, elapsed - setup, summary


def assert_the_attack_executed(result):
    assert result.actions and result.actions[0].action_type is ActionType.DECLARE_ATTACKERS
    assert result.actions[0].attacker_names == ["Odin"]
    assert result.fallback_reason == "", (
        f"a usable model declaration was replaced by {result.fallback_reason!r}"
    )


def test_a_ready_low_answer_wins_before_the_slow_valid_high_answer_finishes():
    result, model_time, summary = plan("slow_valid")

    assert model_time < 0.8, f"waited {model_time:.2f}s for a slow high leg after low had answered"
    assert_the_attack_executed(result)

    statuses = sorted(entry["status"] for entry in summary["requests"])
    assert statuses == ["cancelled", "ok"], summary["requests"]
    assert summary["cancelled"] == 1
    cancelled = next(entry for entry in summary["requests"] if entry["status"] == "cancelled")
    assert cancelled["ttft_ms"] is not None, f"the losing leg had already streamed: {cancelled}"
    assert cancelled["attempts"] == 1


def test_a_fast_invalid_high_answer_does_not_displace_the_valid_low_answer():
    result, model_time, summary = plan("invalid")

    assert model_time < 0.8, f"waited {model_time:.2f}s for an unusable high answer"
    assert_the_attack_executed(result)

    statuses = sorted(entry["status"] for entry in summary["requests"])
    assert statuses == ["ok", "ok"], summary["requests"]
    assert summary["cancelled"] == 0


def test_a_streaming_high_leg_is_cut_while_the_low_answer_stands():
    result, model_time, summary = plan("streaming")

    assert model_time < 0.8, f"waited {model_time:.2f}s for a streaming high leg after low had answered"
    assert_the_attack_executed(result)

    statuses = sorted(entry["status"] for entry in summary["requests"])
    assert statuses == ["cancelled", "ok"], summary["requests"]
    cancelled = next(entry for entry in summary["requests"] if entry["status"] == "cancelled")
    assert cancelled["ttft_ms"] is not None, f"only a streaming leg can be cut here: {cancelled}"
