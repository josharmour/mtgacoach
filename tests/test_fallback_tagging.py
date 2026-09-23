"""WP-0.4: fallback plans carry a structured tag, not just a strategy prefix.

When the LLM fails or returns an unparseable/illegal action, ``ActionPlanner``'s
deterministic picker chooses instead. The only fallback signal used to be an
``"[auto-pick]"`` / ``"[land-drop-first]"`` prefix sniffed out of
``ActionPlan.overall_strategy`` — a human-facing debug string, so rewording it
silently stopped fallbacks from being identified. These tests pin the
structured ``fallback_reason`` tag.
"""

from __future__ import annotations

from arenamcp.action_planner import (
    FALLBACK_AUTO_PICK,
    FALLBACK_NO_ACTIONS,
    FALLBACK_PREFLIGHT_LAND_DROP,
    ActionPlan,
    ActionType,
    GameAction,
    plan_fallback_reason,
)


def _action() -> GameAction:
    return GameAction(action_type=ActionType.PASS_PRIORITY)


def _model_plan() -> ActionPlan:
    """A plan the model actually produced."""
    return ActionPlan(actions=[_action()], overall_strategy="Curve out with the two-drop")


def _fallback_plan() -> ActionPlan:
    """A plan the deterministic picker produced."""
    return ActionPlan(
        actions=[_action()],
        overall_strategy="[auto-pick] Pass",
        fallback_reason=FALLBACK_AUTO_PICK,
    )


# ---------------------------------------------------------------------------
# Producer: the structured tag
# ---------------------------------------------------------------------------


def test_model_plan_is_not_tagged():
    assert plan_fallback_reason(_model_plan()) == ""
    assert _model_plan().fallback is False


def test_fallback_plan_is_tagged():
    plan = _fallback_plan()
    assert plan.fallback is True
    assert plan_fallback_reason(plan) == FALLBACK_AUTO_PICK


def test_empty_plan_is_tagged_no_actions():
    assert plan_fallback_reason(ActionPlan()) == FALLBACK_NO_ACTIONS
    assert plan_fallback_reason(None) == FALLBACK_NO_ACTIONS


def test_structured_tag_survives_a_strategy_reword():
    """THE REGRESSION TEST for defect 1.

    A fallback whose human-facing strategy string no longer starts with
    "[auto-pick]" must still be tagged. Under the old prefix-sniffing
    implementation this returned "" — i.e. "the model decided" — and the record
    became an SFT positive.
    """
    reworded = ActionPlan(
        actions=[_action()],
        overall_strategy="Auto-selected the only legal play",  # no bracket tag
        fallback_reason=FALLBACK_AUTO_PICK,
    )
    assert plan_fallback_reason(reworded) == FALLBACK_AUTO_PICK, (
        "a fallback must stay tagged when its strategy text is reworded"
    )


def test_legacy_prefix_still_classified_for_untagged_plan_objects():
    """Plans predating ``fallback_reason`` still classify via the old prefix."""
    legacy_auto = ActionPlan(actions=[_action()], overall_strategy="[auto-pick] Pass")
    legacy_land = ActionPlan(actions=[_action()], overall_strategy="[land-drop-first] Play Island")
    assert plan_fallback_reason(legacy_auto) == FALLBACK_AUTO_PICK
    assert plan_fallback_reason(legacy_land) == FALLBACK_PREFLIGHT_LAND_DROP


def test_pick_salvage_is_not_a_fallback():
    """``[pick-salvage]`` is the model's own pick recovered from bad JSON."""
    salvaged = ActionPlan(actions=[_action()], overall_strategy="[pick-salvage] 2")
    assert plan_fallback_reason(salvaged) == ""
