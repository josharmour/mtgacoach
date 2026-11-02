"""#232: a combat declaration must use a ready model answer, not the fallback.

Historical signature (c63678d8, 2026-10-26 10:04): DeclareBlockers planning
timed out and SAFE-DEFAULT BLOCKS threw Surrak and Goreclaw plus both Notary
bodies into a lethal board; the retained trace also showed the low-effort leg
answering while the hedge kept waiting for high (#222 F1). These are
deterministic reproductions of the planner mechanism on current master, not
live-service latency measurements.
"""

import json
import threading
import time

from arenamcp.action_planner import FALLBACK_LLM_UNAVAILABLE, ActionPlanner, ActionType

MENU = ["Block with: Spider (1/3)", "Done (confirm blockers)"]
CONTEXT = {"type": "declare_blockers", "attackers": ["Odin"]}
DECLARATION = json.dumps({"action_type": "declare_blockers", "blocker_assignments": {"Spider": "Odin"}})


def board():
    """Their 5-power attacker against our 3 life: ``declaration_effort`` says high."""
    return {
        "local_seat_id": 1,
        "turn": {"turn_number": 6, "active_player": 2, "phase": "Phase_Combat"},
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 3},
            {"seat_id": 2, "is_local": False, "life_total": 20},
        ],
        "battlefield": [
            {
                "instance_id": 11,
                "name": "Spider",
                "type_line": "Creature — Spider",
                "power": 1,
                "toughness": 3,
                "controller_seat_id": 1,
            },
            {
                "instance_id": 12,
                "name": "Odin",
                "type_line": "Creature — God",
                "power": 5,
                "toughness": 5,
                "controller_seat_id": 2,
                "is_attacking": True,
            },
        ],
        "_bridge_request_type": "DeclareBlockers",
    }


def test_a_ready_block_declaration_is_submitted_before_the_slow_high_leg():
    high_started = threading.Event()
    high_cancelled = threading.Event()
    efforts = []

    class Backend:
        def complete(self, system, user, max_tokens, **kwargs):
            effort = kwargs.get("reasoning_effort")
            efforts.append(effort)
            if effort == "high":
                high_started.set()
                if kwargs["cancel_event"].wait(3):
                    high_cancelled.set()
                raise TimeoutError("high exhausted its budget")
            assert high_started.wait(1), "both declaration legs must race"
            return DECLARATION

    planner = ActionPlanner(Backend(), timeout=5, land_drop_first=False)
    started = time.monotonic()
    plan = planner.plan_actions(board(), "decision_required", MENU, CONTEXT)
    elapsed = time.monotonic() - started

    assert efforts == ["high", None], f"expected a high/low hedge, got {efforts}"
    assert plan.actions and plan.actions[0].action_type is ActionType.DECLARE_BLOCKERS
    assert plan.fallback_reason == "", f"a usable model declaration was replaced by {plan.fallback_reason!r}"
    assert elapsed < 1.5, f"waited {elapsed:.2f}s for the high leg after low had answered"
    assert high_cancelled.wait(1), "the losing high-effort call must be cancelled"

    diag = planner.get_recent_diagnostics()[-1]
    assert diag["effort"].startswith("5 attacking against our 3 life")
    assert diag["elapsed_ms"] < elapsed * 1000 + 500


def test_both_block_legs_timing_out_still_yield_the_legal_fallback():
    class Backend:
        def complete(self, system, user, max_tokens, **kwargs):
            kwargs["cancel_event"].wait(2)
            raise TimeoutError("no first token")

    planner = ActionPlanner(Backend(), timeout=0.4, land_drop_first=False)
    started = time.monotonic()
    plan = planner.plan_actions(board(), "decision_required", MENU, CONTEXT)
    elapsed = time.monotonic() - started

    # The autopilot's own legal fallback (safe-default blocks) still runs; this
    # item only guarantees the model gets its bounded chance first.
    assert plan.actions == []
    assert plan.fallback_reason == FALLBACK_LLM_UNAVAILABLE
    assert plan.fallback_detail == "timeout"
    assert elapsed < 2.0
