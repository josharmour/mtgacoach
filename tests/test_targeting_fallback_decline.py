"""Regression tests for the 2026-07-05 own-Spirit destruction (P0 #2).

The controller-aware targeting fallback's forced-own branch deliberately
picked the least-power OWN permanent when a harmful source had no opponent
candidates ("throw away the least valuable one") and submitted it. Now it
returns the DECLINE_DECISION sentinel so the autopilot cancels the window
or pauses for manual instead.
"""

from arenamcp.action_planner import DECLINE_DECISION, ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision


def _planner() -> ActionPlanner:
    return ActionPlanner.__new__(ActionPlanner)  # no backend needed


def _decision(candidate_ids, source_label=""):
    return PendingDecision(
        request_id=(1, 1),
        request_type="SelectTargets",
        options=tuple(DecisionOption(option_id=f"tgt:{iid}", label=f"target {iid}") for iid in candidate_ids),
        min_select=1,
        max_select=1,
        source_label=source_label,
    )


def _state(own_ids=(), their_ids=(), oracle="destroy target creature"):
    battlefield = []
    for iid in own_ids:
        battlefield.append(
            {
                "instance_id": iid,
                "name": f"own-{iid}",
                "power": 1,
                "controller_seat_id": 1,
                "owner_seat_id": 1,
            }
        )
    for iid in their_ids:
        battlefield.append(
            {
                "instance_id": iid,
                "name": f"opp-{iid}",
                "power": 5,
                "controller_seat_id": 2,
                "owner_seat_id": 2,
            }
        )
    return {
        "players": [{"is_local": True, "seat_id": 1}, {"seat_id": 2}],
        "battlefield": battlefield,
        "stack": [{"instance_id": 900, "name": "Go-Shintai of Hidden Cruelty", "oracle_text": oracle}],
    }


def test_harmful_own_only_candidates_decline():
    p = _planner()
    picked = p._targeting_fallback_pick(_decision([607]), _state(own_ids=(607,)))
    assert picked == [DECLINE_DECISION]


def test_harmful_prefers_opponent_when_available():
    p = _planner()
    picked = p._targeting_fallback_pick(_decision([607, 812]), _state(own_ids=(607,), their_ids=(812,)))
    assert picked == ["tgt:812"]


def test_beneficial_still_targets_own():
    p = _planner()
    picked = p._targeting_fallback_pick(
        _decision([607]),
        _state(own_ids=(607,), oracle="target creature gains hexproof"),
    )
    assert picked == ["tgt:607"]


def test_plan_decision_options_passes_decline_through():
    p = _planner()
    p._timeout = 1.0

    class _Backend:
        def complete(self, *a, **k):
            raise RuntimeError("LLM down")

    p._backend = _Backend()
    picked = p.plan_decision_options(_decision([607]), _state(own_ids=(607,)))
    assert picked == [DECLINE_DECISION]


def test_controller_seat_id_takes_precedence_over_owner():
    # A stolen creature (owner=us, controller=opponent) is a legitimate
    # harmful target — the classifier must read controller, not owner.
    p = _planner()
    state = _state()
    state["battlefield"] = [
        {"instance_id": 607, "name": "stolen", "power": 3, "controller_seat_id": 2, "owner_seat_id": 1}
    ]
    picked = p._targeting_fallback_pick(_decision([607]), state)
    assert picked == ["tgt:607"]


def test_llm_decision_options_tolerates_prose_prefix():
    # P1-1: 0/5 typed-decision parses failed on 2026-07-05 because models
    # prefix prose before the JSON despite "reply ONLY with JSON".
    p = _planner()
    p._timeout = 1.0

    class _ProseBackend:
        def complete(self, *a, **k):
            return (
                "Based on the game state, you should remove the flyer. "
                '{"option_ids": ["tgt:812"], "reasoning": "biggest threat"}'
            )

    p._backend = _ProseBackend()
    picked = p.plan_decision_options(_decision([607, 812]), _state(own_ids=(607,), their_ids=(812,)))
    assert picked == ["tgt:812"]


class _OwnPickBackend:
    """LLM that picks the user's own creature for a harmful spell."""

    def complete(self, *a, **k):
        return '{"option_ids": ["tgt:607"], "reasoning": "remove threat"}'


def test_harmful_llm_pick_of_own_creature_overridden():
    # #38 (live 2026-07-06): LLM aimed Utter Insignificance at the user's
    # own Nessian Wanderer. With opponent candidates available, the gate
    # overrides to the opponent's biggest threat.
    p = _planner()
    p._timeout = 1.0
    p._backend = _OwnPickBackend()
    picked = p.plan_decision_options(_decision([607, 812]), _state(own_ids=(607,), their_ids=(812,)))
    assert picked == ["tgt:812"]


def test_harmful_llm_pick_own_only_declines():
    p = _planner()
    p._timeout = 1.0
    p._backend = _OwnPickBackend()
    picked = p.plan_decision_options(_decision([607]), _state(own_ids=(607,)))
    assert picked == [DECLINE_DECISION]


def test_beneficial_llm_pick_of_own_creature_kept():
    p = _planner()
    p._timeout = 1.0
    p._backend = _OwnPickBackend()
    picked = p.plan_decision_options(
        _decision([607, 812]),
        _state(own_ids=(607,), their_ids=(812,), oracle="target creature gains hexproof"),
    )
    assert picked == ["tgt:607"]


def test_target_options_labeled_with_controller():
    p = _planner()
    p._timeout = 1.0
    captured = {}

    class _CapturingBackend:
        def complete(self, system, user, *a, **k):
            captured["user"] = user
            return '{"option_ids": ["tgt:812"]}'

    p._backend = _CapturingBackend()
    p.plan_decision_options(_decision([607, 812]), _state(own_ids=(607,), their_ids=(812,)))
    assert "tgt:607: target 607 (YOURS)" in captured["user"]
    assert "tgt:812: target 812 (opponent's)" in captured["user"]


def _two_triggers_state(source_key):
    """Seam Rip's exile trigger and Optimistic Scavenger's +1/+1 trigger share the stack."""
    state = _state(own_ids=(311,), their_ids=(287,))
    state["stack"] = [
        {"instance_id": 901, "name": "Seam Rip ability", "controller_seat_id": 1,
         "oracle_text": "When this enchantment enters, exile target nonland permanent an opponent controls"},
        {"instance_id": 902, "name": "Optimistic Scavenger ability", "controller_seat_id": 1,
         "oracle_text": "Whenever an enchantment you control enters, put a +1/+1 counter on target creature."},
    ]
    state[source_key] = {"sourceId": 901} if source_key == "_bridge_request_payload" else {"source_id": 901}
    return state


def test_request_source_beats_top_of_stack_for_harm_classification():
    # 2026-09-24: Seam Rip's target was classified from the beneficial trigger
    # on top of the stack, so the correct enemy target (#287) was declined.
    p = _planner()
    for key in ("_bridge_request_payload", "decision_context"):
        state = _two_triggers_state(key)
        assert p._decision_source_is_harmful(_decision([311, 287]), state) is True
        assert p._targeting_fallback_pick(_decision([311, 287]), state) == ["tgt:287"]
