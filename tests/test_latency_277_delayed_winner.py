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

    def __init__(self, high_mode):
        self.high_mode = high_mode
        self.efforts = []
        self.finished = []
        self.first_call_at = None
        self.chat = NS(completions=NS(create=self._create))

    def with_options(self, **options):
        return self

    def _create(self, **params):
        if self.first_call_at is None:
            self.first_call_at = time.monotonic()
        effort = params["extra_body"]["chat_template_kwargs"].get("reasoning_effort")
        self.efforts.append(effort)
        if effort != "high":
            return iter([chunk(DECLARATION, finish="stop")])
        if self.high_mode == "slow_valid":
            return self._slow_valid()
        if self.high_mode == "invalid":
            return iter([chunk("not json", finish="stop")])
        return self._streaming()

    def _slow_valid(self):
        """A valid declaration that would have been usable -- but only after 1.2 s."""

        def generate():
            yield chunk("thinking", reasoning="weighing the attack")
            time.sleep(1.2)
            yield chunk(DECLARATION)
            self.finished.append("high")

        return generate()

    def _streaming(self):
        """Generation that outlives any short budget without ever being usable."""

        def generate():
            yield chunk("thinking", reasoning="weighing the attack")
            for _ in range(60):
                time.sleep(0.05)
                yield chunk(" more")
            self.finished.append("high")

        return generate()


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
        while len(trace.summary()["requests"]) < len(be._client.efforts) and time.monotonic() < deadline:
            time.sleep(0.02)
        summary = trace.summary()
    finally:
        llm_trace.finish(trace)
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
