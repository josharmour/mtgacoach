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
# Bounded proof windows asserted by the tests themselves (distinct from the
# fakes' internal safety margins below).
FIRST_CHUNK_PROOF_S = 2.0
PARKED_RELEASE_PROOF_S = 2.0


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
    PARKED_HIGH_RELEASE_WAIT_S = 5.0

    def __init__(
        self,
        low_text=DECLARATION,
        both_stream=False,
        low_awaits_high_first_chunk=True,
        parks_high_before_request=False,
    ):
        self.low_text = low_text
        self.both_stream = both_stream
        self.low_awaits_high_first_chunk = low_awaits_high_first_chunk
        self.parks_high_before_request = parks_high_before_request
        self.efforts = []
        self.high_first_chunk = threading.Event()
        # Whether the bounded wait actually observed high's first chunk; the
        # tests require True so a silent timeout cannot masquerade as proof.
        self.low_saw_high_first_chunk = None
        self.high_gate = threading.Event()
        self.parked_high_released = threading.Event()
        self.chat = NS(completions=NS(create=self._create))

    def with_options(self, **options):
        if not self.parks_high_before_request:
            return self
        outer = self

        class _ParkedClient:
            """Hold a high request BEFORE it is issued until the gate opens."""

            chat = NS(completions=NS(create=outer._parked_create))

        return _ParkedClient()

    def _parked_create(self, **params):
        effort = params["extra_body"]["chat_template_kwargs"].get("reasoning_effort")
        if effort != "high":
            return self._create(**params)
        # Parked ahead of any request: while parked there is no attempt to
        # count and no effort to record -- run 43177283352's legitimate
        # cancelled-before-first-request shape, made deterministic instead of
        # scheduler-dependent.
        opened = self.high_gate.wait(self.PARKED_HIGH_RELEASE_WAIT_S)
        self.parked_high_released.set()
        if not opened:
            return iter([])  # bounded even if the gate is never released
        self.efforts.append(effort)
        # A short finite stream: once released, the belated leg must end
        # promptly instead of lingering past the test.
        return iter([chunk("thinking", reasoning="late"), chunk(self.low_text, finish="stop")])

    def _create(self, **params):
        effort = params["extra_body"]["chat_template_kwargs"].get("reasoning_effort")
        self.efforts.append(effort)
        if self.both_stream or effort == "high":
            return self._streaming()
        if self.low_awaits_high_first_chunk:
            # #43177283352: low must not outrun the high leg's first streamed
            # token, or the hedge cancels high before its request and the
            # both-efforts assertion measures the scheduler instead of the
            # hedge. Await proof that high emitted its first stream chunk,
            # bounded so a wedged high leg can still not stall the answer;
            # the outcome is recorded for the test to require.
            self.low_saw_high_first_chunk = self.high_first_chunk.wait(self.HIGH_FIRST_CHUNK_WAIT_S)
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
    # The low leg's bounded wait must have actually observed high's first
    # stream chunk -- a silent timeout would leave the scenario measuring the
    # scheduler again instead of the hedge.
    assert be._client.low_saw_high_first_chunk is True
    assert elapsed < PROMPT_BUILD_ALLOWANCE_S
    assert result.fallback_reason == ""
    assert [action.action_type for action in result.actions] == [ActionType.DECLARE_ATTACKERS]


def test_an_instant_low_answer_may_win_before_high_even_requests():
    """A high leg parked before its request is cancelled unawaited; low still plans.

    Run 43177283352 caught the legitimate half of the hedge: when low answers
    immediately the scheduler may cancel high before its first request
    (metrics ``attempts: 0``), so ``efforts`` legitimately holds only ``low``.
    The gate parks high ahead of any request -- no attempt exists to count --
    making that ordering deterministic instead of scheduler-dependent. The
    parked leg is then released under a bounded proof that it wakes promptly,
    and the belated answer can neither stall nor alter the decided plan.
    """
    be = backend(low_awaits_high_first_chunk=False, parks_high_before_request=True)
    result, elapsed = plan(be, 6.0)
    # While parked ahead of its request the high leg can record nothing, so at
    # plan time only low ever issued -- the run's attempts == 0 shape.
    assert be._client.efforts == ["low"]
    assert elapsed < PROMPT_BUILD_ALLOWANCE_S
    assert result.fallback_reason == ""
    assert [action.action_type for action in result.actions] == [ActionType.DECLARE_ATTACKERS]
    # Cleanup: release the parked leg and require -- bounded -- that it wakes
    # promptly instead of stalling past the test.
    be._client.high_gate.set()
    assert be._client.parked_high_released.wait(PARKED_RELEASE_PROOF_S)
    assert [action.action_type for action in result.actions] == [ActionType.DECLARE_ATTACKERS]
