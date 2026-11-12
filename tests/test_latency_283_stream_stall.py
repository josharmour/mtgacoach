"""#283: a stream that never finishes must not stall coaching past its budget.

Historical signature (retained audit entry #283): one streaming budget
exhaustion produced a 15-18 s coaching-loop stall while the companion match
still fell back to correct board math. The retained trace is absent from this
checkout, so these are deterministic reproductions of the current
planner/proxy mechanism -- not live-service latency measurements.
"""

import json
import threading
import time
from types import SimpleNamespace as NS

from tests.proxy_fakes import MODEL, URL, chunk

from arenamcp.action_planner import ActionPlanner, ActionType
from arenamcp.backends.proxy import ProxyBackend

DECLARATION = json.dumps({"action_type": "declare_attackers", "attacker_names": ["Odin"]})
MENU = ["Attack with: Odin (5/5)", "Done"]
# Prompt construction happens before the model deadline starts (#275 measured
# about one second warm); it is not part of the streaming budget.
PROMPT_BUILD_ALLOWANCE_S = 3.0


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


class StreamClient:
    """Effort-aware fake client whose ``high`` leg streams until its budget cuts it."""

    # Bounded waits that keep the fakes from hanging if a leg is never
    # scheduled; they are safety margins, never part of the happy path.
    HIGH_FIRST_CHUNK_WAIT_S = 2.0
    HIGH_GATE_WAIT_S = 5.0

    def __init__(self, low_text=DECLARATION, both_stream=False, gated_high=False):
        self.low_text = low_text
        self.both_stream = both_stream
        self.gated_high = gated_high
        self.efforts = []
        self.high_first_chunk = threading.Event()
        self.high_gate = threading.Event()
        self.chat = NS(completions=NS(create=self._create))

    def with_options(self, **options):
        return self

    def _create(self, **params):
        effort = params["extra_body"]["chat_template_kwargs"].get("reasoning_effort")
        if self.gated_high and effort == "high":
            # Hold the request until released, so a fast low answer can win
            # before the high leg ever issues one (the CI run's legitimate
            # shape) without depending on scheduler timing.
            if not self.high_gate.wait(self.HIGH_GATE_WAIT_S):
                return iter([])
        self.efforts.append(effort)
        if self.both_stream or effort == "high":
            return self._streaming()
        # #43177283352: low must not outrun the high leg's first streamed
        # token, or the hedge cancels high before its request and the
        # both-efforts assertion measures the scheduler instead of the hedge.
        # Await proof that high emitted its first stream chunk, bounded so a
        # wedged high leg can still not stall the answer.
        self.high_first_chunk.wait(self.HIGH_FIRST_CHUNK_WAIT_S)
        return iter([chunk(self.low_text, finish="stop")])

    def _streaming(self):
        """A first token at once, then generation that outlives any short budget."""

        def generate():
            yield chunk("thinking", reasoning="weighing the attack")
            # The consumer came back for more: the first chunk was emitted.
            self.high_first_chunk.set()
            for _ in range(400):
                time.sleep(0.02)
                yield chunk(" more")

        return generate()


def backend(**kwargs):
    be = ProxyBackend(model=MODEL, base_url=URL)
    be._client = StreamClient(**kwargs)
    return be


def plan(be, timeout):
    """One declaration decision; returns (plan, elapsed_s)."""
    planner = ActionPlanner(be, timeout=timeout, land_drop_first=False)
    started = time.monotonic()
    result = planner.plan_actions(board(), "decision_required", MENU, {"type": "declare_attackers"})
    return result, time.monotonic() - started


def test_a_stream_that_never_finishes_is_cut_at_the_deadline():
    """Both legs stream past their budget: the plan returns at its own deadline."""
    be = backend(both_stream=True)
    result, elapsed = plan(be, 1.0)
    assert elapsed >= 0.9
    assert elapsed < 1.0 + PROMPT_BUILD_ALLOWANCE_S
    assert result.fallback_reason == "llm_unavailable"
    # Holding every attacker back is legal; no partial stream text is submitted.
    assert result.actions == []


def test_a_streaming_high_leg_cannot_outlive_a_ready_low_answer():
    """#277's hedge fix holds here too: low answers at once and high is not awaited."""
    be = backend()
    result, elapsed = plan(be, 6.0)
    assert sorted(be._client.efforts) == ["high", "low"]
    assert elapsed < PROMPT_BUILD_ALLOWANCE_S
    assert result.fallback_reason == ""
    assert [action.action_type for action in result.actions] == [ActionType.DECLARE_ATTACKERS]


def test_an_instant_low_answer_may_win_before_high_even_requests():
    """A high leg held before its request is cancelled unawaited; low still plans.

    Run 43177283352 caught the legitimate half of the hedge: when low answers
    immediately the scheduler may cancel high before its first request
    (metrics ``attempts: 0``), so ``efforts`` legitimately holds only ``low``.
    The gate makes that ordering deterministic instead of scheduler-dependent,
    and releasing the stranded leg afterwards proves cancellation still
    reclaims it without stalling or resurrecting the plan.
    """
    be = backend(gated_high=True)
    result, elapsed = plan(be, 6.0)
    assert be._client.efforts == ["low"]
    assert elapsed < PROMPT_BUILD_ALLOWANCE_S
    assert result.fallback_reason == ""
    assert [action.action_type for action in result.actions] == [ActionType.DECLARE_ATTACKERS]
    # The cancelled leg must drain promptly once released, never stall.
    be._client.high_gate.set()
    drained = time.monotonic() + 2.0
    while len(be._client.efforts) < 2 and time.monotonic() < drained:
        time.sleep(0.01)
    assert len(be._client.efforts) == 2
