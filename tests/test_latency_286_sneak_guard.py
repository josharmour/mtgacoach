"""BUG-286: a failed high-effort declaration must not stall the deterministic Sneak guard.

Ledger #286 (match 012cb47): the high declaration exhausted its budget before
the deterministic Sneak guard converted lethal; an illegal pass/fallback retry
is a distinct case. These tests pin current master behavior on the Leonardo
board from ledger #156/#159: the declaration hedge is bounded by the planner
deadline, a failed declaration returns the llm_unavailable fallback without
actions, and the bridge's authoritative attack override still finds the Sneak
line in milliseconds afterwards. The guard itself runs before any model call
(``ActionPlanner.sneak_policy``), so it cannot be stalled by a slow answer.
"""

import json
import time

from tests import leonardo_boards_20261019 as lb

from arenamcp.action_planner import FALLBACK_LLM_UNAVAILABLE, ActionPlanner, ActionType
from arenamcp.autopilot_bridge import _BridgeSubmitMixin

T12_IDS = list(lb.HARES_T12) + list(lb.RABBITS_T12)
MENU = [f"Attack with: Hare Apparent #{i} (2/2)" for i in range(1, 6)]
MENU += [f"Attack with: Rabbit #{i} (1/1)" for i in range(1, 11)]
MENU.append("Done (confirm attackers)")
CONTEXT = {"type": "declare_attackers", "legal_attacker_ids": T12_IDS}
LOW = json.dumps(
    {
        "actions": [
            {
                "action_type": "declare_attackers",
                "attacker_names": ["all"],
                "attacker_targets": {"all": "Opponent"},
            }
        ]
    }
)


def declare_state():
    """The T12 declare-attackers window: 20 power on the menu against 17 life."""
    state = lb.t12_main1()
    state["turn"]["phase"] = "Phase_Combat"
    state["turn"]["step"] = "Step_DeclareAttack"
    state["decision_context"] = dict(CONTEXT)
    return state


class Backend:
    """Duck-typed planner backend keyed by reasoning effort."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def complete(self, system_prompt, user_message, max_tokens=0, *, reasoning_effort=None, **kwargs):
        self.calls.append(reasoning_effort)
        answer = self.answers.get(reasoning_effort)
        if callable(answer):
            return answer(kwargs.get("cancel_event"))
        if isinstance(answer, BaseException):
            raise answer
        return answer


def slow(cancel_event, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cancel_event is not None and cancel_event.is_set():
            raise TimeoutError("cancelled")
        time.sleep(0.01)
    return LOW


def authoritative(state):
    return _BridgeSubmitMixin._authoritative_attack_action(object.__new__(_BridgeSubmitMixin), state)


def ids_of(action):
    """The guard names each attacker by instance ID (ledger #208)."""
    return {int(name.rsplit("[id:", 1)[1].rstrip("]")) for name in action.attacker_names}


def test_a_failed_high_declaration_still_reaches_the_sneak_guard():
    backend = Backend({"high": lambda cancel: slow(cancel), None: TimeoutError("budget")})
    planner = ActionPlanner(backend, timeout=0.6, land_drop_first=False)
    state = declare_state()
    started = time.monotonic()
    plan = planner.plan_actions(state, "decision_required", MENU, CONTEXT)
    elapsed = time.monotonic() - started
    assert plan.fallback_reason == FALLBACK_LLM_UNAVAILABLE
    assert plan.actions == []
    assert elapsed < 2.0
    started = time.monotonic()
    action = authoritative(state)
    assert time.monotonic() - started < 0.5
    assert action is not None and action.action_type == ActionType.DECLARE_ATTACKERS
    assert ids_of(action) == set(T12_IDS)
    assert "Sneak" in action.reasoning


def test_a_timely_low_declaration_is_not_overridden_by_the_guard():
    backend = Backend({"high": lambda cancel: slow(cancel), None: LOW})
    planner = ActionPlanner(backend, timeout=1.0, land_drop_first=False)
    state = declare_state()
    plan = planner.plan_actions(state, "decision_required", MENU, CONTEXT)
    assert len(plan.actions) == 1
    assert sorted(plan.actions[0].attacker_instance_ids) == sorted(T12_IDS)
    # The override is only consulted when the plan did not attack; when it is
    # consulted it must name the same attackers, never a weaker declaration.
    action = authoritative(state)
    assert action is not None and ids_of(action) == set(T12_IDS)


def test_the_guard_still_declines_when_no_line_is_lethal():
    state = declare_state()
    state["players"][1]["life_total"] = 60
    state["players"][0]["life_total"] = 40
    assert authoritative(state) is None
