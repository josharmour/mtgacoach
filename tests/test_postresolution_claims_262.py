"""#84/#262: same iid560 survives Headliner's observed two damage in ae86ca8d."""

from tests.test_tactical_timing_backlog import board, card

from arenamcp.postresolution_claims import surviving_damage_targets

REASON = "Seam Rip can only exile permanents with MV 2 or less, and Hexhaven (MV 6) is already dead; no legal target, so hold the mana."


def live_shape():
    state = board(0)
    state["turn"]["turn_number"] = 10
    state["battlefield"] = [
        card(560, "Hexhaven Invigorator", "", "Creature", seat=2, power=6, toughness=6, damage=4)
    ]
    state["recent_events"] = [dict(type="damage_dealt", turn=10, source_id=567, target_id=560, amount=2)]
    return state


def test_recorded_false_death_is_bound_to_surviving_object_and_actual_damage():
    reasons = surviving_damage_targets(live_shape(), REASON)
    assert len(reasons) == 1 and "#560" in reasons[0] and "4 damage on 6 toughness" in reasons[0]


def test_no_prediction_from_missing_damage_old_event_or_pending_stack():
    state = live_shape()
    state["battlefield"][0].pop("damage")
    assert not surviving_damage_targets(state, REASON)
    state = live_shape()
    state["recent_events"][0]["turn"] = 9
    assert not surviving_damage_targets(state, REASON)
    state = live_shape()
    state["stack"] = [card(600, "Pending damage", "", "Instant", seat=2)]
    assert not surviving_damage_targets(state, REASON)
    state = live_shape()
    state["battlefield"][0]["damage"] = 6
    assert not surviving_damage_targets(state, REASON)


def test_negation_namesake_and_returned_new_identity_are_preserved():
    state = live_shape()
    assert not surviving_damage_targets(state, "Hexhaven is not dead; it survived with four damage.")
    assert not surviving_damage_targets(state, "If Hexhaven is already dead, hold the mana.")
    state["battlefield"].append({**state["battlefield"][0], "instance_id": 561})
    assert not surviving_damage_targets(state, REASON)
    state = live_shape()
    state["battlefield"][0]["instance_id"] = 570
    assert not surviving_damage_targets(state, REASON)


def test_recheck_accepts_truthful_same_pass_or_changes_the_action():
    import json
    from unittest.mock import Mock

    from tests.test_tactical_timing_backlog import cast, menu

    from arenamcp.action_planner import ActionPlanner

    planner = ActionPlanner(backend=Mock())
    planner._last_decision_trace = {}
    decision = menu(cast(30))

    def parse(raw):
        data = json.loads(raw)
        return data, data["option_ids"]

    for picks, reason in (
        (["pass"], "Hexhaven survives with four damage; Seam Rip still cannot target its mana value6."),
        (["idx:30"], "Use a legal answer on the surviving creature."),
    ):
        ask = Mock(return_value=json.dumps({"option_ids": picks, "reasoning": reason}))
        revised, selected = planner._recheck_damage_facts(
            decision, live_shape(), {"reasoning": REASON}, ["pass"], ask, parse, ""
        )
        assert selected == picks and revised["reasoning"] == reason
        assert ask.call_count == 1


def test_repeated_false_death_or_failed_call_requests_current_board_fallback():
    import json
    from unittest.mock import Mock

    import pytest
    from tests.test_tactical_timing_backlog import cast, menu

    from arenamcp.action_planner import ActionPlanner, SecondLookError

    planner = ActionPlanner(backend=Mock())
    planner._last_decision_trace = {}

    def parse(raw):
        data = json.loads(raw)
        return data, data["option_ids"]

    for ask in (
        Mock(side_effect=RuntimeError("offline")),
        Mock(return_value=json.dumps({"option_ids": ["pass"], "reasoning": REASON})),
    ):
        with pytest.raises(SecondLookError):
            planner._recheck_damage_facts(
                menu(cast(30)), live_shape(), {"reasoning": REASON}, ["pass"], ask, parse, ""
            )
        assert planner._last_decision_trace["damage_fact_recheck"]["fallback"] == "current_board"


def test_dynamic_target_kill_claim_uses_actual_post_sacrifice_count():
    from tests.test_burn_timing_20261019 import _targets
    from tests.test_headliner_damage_84 import TARGET_REASON, factual_board

    from arenamcp.postresolution_claims import false_activation_target_kills

    state, source = factual_board()
    state["stack"] = [{**source, "instance_id": 622, "parent_instance_id": 567, "object_kind": "ABILITY"}]
    state["decision_context"] = {"source_id": 622, "source_parent_instance_id": 567}
    decision = _targets("Stadium Headliner", [560])
    facts = false_activation_target_kills(decision, state, ["tgt:560"], TARGET_REASON, 622)
    assert facts and "gives 2 damage" in facts[0]
    assert not false_activation_target_kills(
        decision, state, ["tgt:560"], "Put two damage on Hexhaven to set up the later removal.", 622
    )
    state["battlefield"][-1]["damage"] = 4
    assert not false_activation_target_kills(decision, state, ["tgt:560"], TARGET_REASON, 622)


def test_public_planner_falls_back_using_surviving_current_board(monkeypatch):
    from unittest.mock import Mock

    from tests.test_tactical_timing_backlog import cast, menu

    from arenamcp.action_planner import ActionPlanner

    state = live_shape()
    state["hand"] = [card(30, "Develop", "Draw a card.", "Sorcery", "{0}")]
    planner = ActionPlanner(backend=Mock())

    def model(decision, current):
        planner._last_decision_trace = {}
        return planner._recheck_damage_facts(
            decision,
            current,
            {"reasoning": REASON},
            ["pass"],
            Mock(side_effect=RuntimeError("offline")),
            Mock(),
            "",
        )

    monkeypatch.setattr(planner, "_llm_decision_options", model)
    seen = []

    def fallback(decision, current):
        seen.append(current)
        planner._last_decision_reasoning = "Pass with Hexhaven still alive; reassessed the current board."
        return ["pass"]

    monkeypatch.setattr(planner, "_board_math_fallback", fallback)
    assert planner.plan_decision_options(menu(cast(30)), state) == ["pass"]
    assert seen and seen[0]["battlefield"][0]["instance_id"] == 560
    assert planner.last_llm_failure == "set_aside"


def test_typed_target_prompt_exposes_dynamic_damage_and_rechecks_false_kill(monkeypatch):
    import json

    import pytest
    from tests.test_burn_timing_20261019 import _targets
    from tests.test_headliner_damage_84 import TARGET_REASON, factual_board
    from tests.test_typed_decision_context import _CapturingBackend

    from arenamcp.action_planner import ActionPlanner, SecondLookError

    state, source = factual_board()
    state["stack"] = [{**source, "instance_id": 622, "parent_instance_id": 567, "object_kind": "ABILITY"}]
    state["decision_context"] = {"source_id": 622, "source_parent_instance_id": 567}
    backend = _CapturingBackend(json.dumps({"option_ids": ["tgt:560"], "reasoning": TARGET_REASON}))
    planner = ActionPlanner(backend=backend)
    monkeypatch.setattr(planner, "_line_prompt_context", lambda *a: (None, "", {}))
    monkeypatch.setattr(planner, "_grounded_plan_block", lambda *a, **kw: "")
    with pytest.raises(SecondLookError):
        planner._llm_decision_options(_targets("Stadium Headliner", [560]), state)
    assert "ACTIVATION DAMAGE: 2 damage" in backend.prompts[0]
    assert sum("DAMAGE FACT RECHECK" in p for p in backend.prompts) == 1
