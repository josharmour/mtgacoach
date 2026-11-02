"""#283: a stream that never finishes must not stall coaching past its budget.

Historical signature (retained audit entry #283): one streaming budget
exhaustion produced a 15-18 s coaching-loop stall while the companion match
still fell back to correct board math. The retained trace is absent from this
checkout, so these are deterministic reproductions of the current
planner/proxy mechanism -- not live-service latency measurements.
"""

import json
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

    def __init__(self, low_text=DECLARATION, both_stream=False):
        self.low_text = low_text
        self.both_stream = both_stream
        self.efforts = []
        self.chat = NS(completions=NS(create=self._create))

    def with_options(self, **options):
        return self

    def _create(self, **params):
        effort = params["extra_body"]["chat_template_kwargs"].get("reasoning_effort")
        self.efforts.append(effort)
        if self.both_stream or effort == "high":
            return self._streaming()
        return iter([chunk(self.low_text, finish="stop")])

    def _streaming(self):
        """A first token at once, then generation that outlives any short budget."""

        def generate():
            yield chunk("thinking", reasoning="weighing the attack")
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
