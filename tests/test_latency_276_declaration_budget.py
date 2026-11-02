"""#276: a streaming high-effort plan must not outlive a ready low answer.

Historical signature (228fed6f/750806ad): ``decision.plan`` at high effort used
its full 15 s budget even though its first token arrived at 5.4 s, and the
coaching loop only resumed around 22 s. The retained traces are absent from this
checkout, so these are deterministic reproductions of the planner/proxy
mechanism on current master -- not live-service latency measurements.
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
    """A fake OpenAI client: the high leg streams slowly while low answers at once."""

    def __init__(self, low_text=DECLARATION, chunks=40, delay=0.05):
        self.low_text = low_text
        self.chunks = chunks
        self.delay = delay
        self.efforts = []
        self.first_call_at = None
        self.chat = NS(completions=NS(create=self._create))

    def with_options(self, **options):
        return self

    def _create(self, **params):
        if self.first_call_at is None:
            self.first_call_at = time.monotonic()
        effort = params["extra_body"]["chat_template_kwargs"].get("reasoning_effort")
        self.efforts.append(effort)
        if effort == "high":
            return self._streaming()
        return iter([chunk(self.low_text, finish="stop")])

    def _streaming(self):
        """A first token at once, then generation that outlives any short budget."""

        def generate():
            yield chunk("thinking", reasoning="weighing the attack")
            for _ in range(self.chunks):
                time.sleep(self.delay)
                yield chunk(" more")

        return generate()


def backend(low_text=DECLARATION):
    be = ProxyBackend(model=MODEL, base_url=URL)
    be._client = LegClient(low_text=low_text)
    return be


def plan(be, timeout):
    """One declaration decision; returns (plan, elapsed_s, setup_s before any call)."""
    planner = ActionPlanner(be, timeout=timeout, land_drop_first=False)
    trace = llm_trace.start()
    started = time.monotonic()
    try:
        result = planner.plan_actions(board(), "decision_required", MENU, {"type": "declare_attackers"})
        elapsed = time.monotonic() - started
        deadline = time.monotonic() + 2.0
        while len(trace.summary()["requests"]) < len(be._client.efforts) and time.monotonic() < deadline:
            time.sleep(0.02)
        summary = trace.summary()
    finally:
        llm_trace.finish(trace)
    setup = (be._client.first_call_at or started) - started
    return result, elapsed - setup, summary


def test_a_streaming_high_leg_cannot_hold_a_ready_low_declaration():
    be = backend()
    result, model_time, summary = plan(be, timeout=2.0)

    assert be._client.efforts == ["high", "low"], f"expected a high/low hedge, got {be._client.efforts}"
    assert result.actions and result.actions[0].action_type is ActionType.DECLARE_ATTACKERS
    assert result.actions[0].attacker_names == ["Odin"]
    assert result.fallback_reason == "", (
        f"a usable model declaration was replaced by {result.fallback_reason!r}"
    )
    assert model_time < 0.8, f"waited {model_time:.2f}s for a streaming high leg after low had answered"

    statuses = sorted(entry["status"] for entry in summary["requests"])
    assert statuses == ["cancelled", "ok"], summary["requests"]
    cancelled = next(entry for entry in summary["requests"] if entry["status"] == "cancelled")
    assert cancelled["ttft_ms"] is not None, (
        f"the cancelled leg had already produced its first token: {cancelled}"
    )
    assert cancelled["total_ms"] < 2000, cancelled


def test_a_streaming_high_leg_with_no_usable_low_stops_at_the_deadline():
    be = backend(low_text="not json")
    result, model_time, summary = plan(be, timeout=1.0)

    # The deterministic free attack (action_planner.py:2775) is the legal fallback here.
    assert result.fallback_reason == "planner_free_attack"
    assert [action.action_type for action in result.actions] == [ActionType.DECLARE_ATTACKERS]
    assert model_time < 1.35, f"the decision ran {model_time:.2f}s against its own 1.0s budget"

    cut = [entry for entry in summary["requests"] if entry["status"] in ("cancelled", "error")]
    assert len(cut) == 1, summary["requests"]
    assert cut[0]["ttft_ms"] is not None, f"only the budget may cut a leg that already streamed: {cut[0]}"
    assert cut[0]["attempts"] == 1
    assert 900 <= cut[0]["total_ms"] < 1600, f"the streaming leg was not cut at its budget: {cut[0]}"
