"""Non-mana costs use the engine's contribution weights, not a card count."""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import build_pending_decision, decision_from_dict, decision_to_dict, submit_option
from arenamcp.request_tracker import decision_fingerprint


def _poll(**changes):
    return {
        "has_pending": True,
        "request_type": "SelectN",
        "request_class": "PayCostsRequest",
        "payment_selection": True,
        "select_n_ids": [763, 771, 875],
        "select_n_weights": [3, 1, 4],
        "select_n_min": 4,
        "select_n_max": 2147483647,
        "select_n_min_weight": -2147483648,
        "select_n_max_weight": 2147483647,
        **changes,
    }


def test_crew_requirement_is_total_power_not_four_creatures():
    decision = build_pending_decision(_poll(), resolve_instance=lambda instance_id: f"Creature {instance_id}")
    assert decision.min_select == 0
    assert decision.max_select == 3
    assert decision.min_weight == 4
    assert decision.selection_is_valid(["sel:875"])
    assert decision.selection_is_valid(["sel:763", "sel:771"])
    assert not decision.selection_is_valid(["sel:763"])
    assert not decision.selection_is_valid(["sel:763", "sel:763"])
    assert decision.options[0].label == "Creature 763"
    assert decision_from_dict(decision_to_dict(decision)) == decision


def test_explicit_weight_bounds_also_enforce_creature_count():
    decision = build_pending_decision(
        _poll(select_n_min=2, select_n_max=2, select_n_min_weight=4, select_n_max_weight=5)
    )
    assert decision.selection_is_valid(["sel:763", "sel:771"])
    assert not decision.selection_is_valid(["sel:875"])
    assert not decision.selection_is_valid(["sel:763", "sel:875"])


@pytest.mark.parametrize("chosen", [["sel:875"], ["sel:763", "sel:771"]])
def test_planner_accepts_legal_crew_selection_and_explains_weights(chosen):
    import json

    backend = Mock()
    backend.complete.return_value = json.dumps({"option_ids": chosen})
    decision = build_pending_decision(_poll())
    assert ActionPlanner(backend=backend).plan_decision_options(decision, {}) == chosen
    prompt = backend.complete.call_args.args[1]
    assert "Required total contribution: 4" in prompt
    assert "contribution: 3" in prompt
    assert "Summoning-sick creatures may crew" in prompt


def test_invalid_model_selection_falls_back_to_a_valid_payment():
    backend = Mock()
    backend.complete.return_value = '{"option_ids": ["sel:763"]}'
    decision = build_pending_decision(_poll())
    selected = ActionPlanner(backend=backend).plan_decision_options(decision, {})
    assert decision.selection_is_valid(selected)


def test_executor_rejects_underpaid_or_repeated_creature_ids():
    bridge = Mock()
    decision = build_pending_decision(_poll())
    assert not submit_option(bridge, decision, ["sel:763"])
    assert not submit_option(bridge, decision, ["sel:763", "sel:763"])
    bridge.submit_selection.assert_not_called()
    assert submit_option(bridge, decision, ["sel:875"])
    bridge.submit_selection.assert_called_once_with([875])


def test_payment_identity_changes_when_contributions_change():
    decision = build_pending_decision(_poll())
    option = replace(decision.options[0], meta={"weight": 1})
    changed = replace(decision, options=(option, *decision.options[1:]))
    assert decision_fingerprint(changed) != decision_fingerprint(decision)


def test_incomplete_weights_are_not_guessed():
    assert build_pending_decision(_poll(select_n_weights=[3])) is None


def test_autopilot_pays_non_mana_cost_without_autotap_or_cancellation():
    from arenamcp.autopilot import AutopilotConfig, AutopilotEngine

    planner = Mock()
    planner.plan_decision_options.return_value = ["sel:875"]
    engine = AutopilotEngine(planner=planner, config=AutopilotConfig(dry_run=False))
    engine._gre_bridge = Mock(connected=True)
    engine._gre_bridge.get_pending_actions.return_value = _poll()
    engine._should_decline_optional_cost = Mock(return_value="")

    assert engine._try_typed_decision_path({}, "decision_required") is True
    engine._gre_bridge.submit_selection.assert_called_once_with([875])
    engine._gre_bridge.submit_auto_tap.assert_not_called()
    engine._gre_bridge.cancel_action.assert_not_called()


def test_unresolvable_payment_pauses_instead_of_retrying_crew_forever():
    from arenamcp.autopilot import AutopilotConfig, AutopilotEngine

    planner = Mock()
    planner.plan_decision_options.return_value = []
    engine = AutopilotEngine(planner=planner, config=AutopilotConfig(dry_run=False))
    engine._gre_bridge = Mock(connected=True)
    engine._gre_bridge.get_pending_actions.return_value = _poll()
    engine._should_decline_optional_cost = Mock(return_value="")
    engine._pause_for_manual = Mock()

    assert engine._try_typed_decision_path({}, "decision_required") is True
    engine._pause_for_manual.assert_called_once()
    engine._gre_bridge.cancel_action.assert_not_called()
    engine._gre_bridge.submit_selection.assert_not_called()


def test_failed_payment_submission_does_not_fall_back_to_mana_cancellation():
    from arenamcp.autopilot import AutopilotConfig, AutopilotEngine

    planner = Mock()
    planner.plan_decision_options.return_value = ["sel:875"]
    engine = AutopilotEngine(planner=planner, config=AutopilotConfig(dry_run=False))
    engine._gre_bridge = Mock(connected=True)
    engine._gre_bridge.get_pending_actions.return_value = _poll()
    engine._gre_bridge.submit_selection.return_value = False
    engine._should_decline_optional_cost = Mock(return_value="")
    engine._pause_for_manual = Mock()

    assert engine._try_typed_decision_path({}, "decision_required") is True
    engine._pause_for_manual.assert_called_once()
    engine._gre_bridge.cancel_action.assert_not_called()
