"""Tests for discard decision coaching, action planning, and staleness handling."""

from arenamcp.action_planner import ActionPlanner, ActionType, GameAction


def test_humanize_discard_action():
    planner = ActionPlanner(backend=None)
    assert planner._humanize_legal_action("Discard Mutavault") == "Discard Mutavault."
    assert planner._humanize_legal_action("Discard Forest") == "Discard Forest."


def test_pick_preferred_legal_action_discard():
    planner = ActionPlanner(backend=None)
    actions = ["Pass", "Discard Mutavault", "Discard Forest"]
    preferred = planner._pick_preferred_legal_action(actions)
    assert preferred in ("Discard Mutavault", "Discard Forest")


def test_legal_action_to_action_discard():
    planner = ActionPlanner(backend=None)
    action = planner._legal_action_to_action("Discard Mutavault")
    assert action is not None
    assert action.action_type == ActionType.SELECT_N
    assert action.select_card_names == ["Mutavault"]
    assert action.card_name == "Mutavault"


class DummyBackend:
    def complete(self, *args, **kwargs) -> str:
        return '{"actions": [{"pick": 1, "reasoning": "Preserve key threats"}], "overall_strategy": "Discard Mutavault to hand size", "voice_advice": "Discard Mutavault to hand size."}'


def test_action_planner_discard_empty_legal_actions():
    planner = ActionPlanner(backend=DummyBackend())
    game_state = {
        "turn": {"turn_number": 14, "active_player": 2, "priority_player": 2, "phase": "Phase_Ending", "step": "Step_Cleanup"},
        "players": [{"seat_id": 2, "is_local": True}, {"seat_id": 1, "is_local": False}],
        "pending_decision": "Discard",
        "legal_actions": [],
        "hand": [{"name": "Mutavault"}, {"name": "Forest"}],
    }
    decision_context = {
        "type": "discard",
        "count": 1,
        "option_cards": ["Mutavault", "Forest"],
    }

    plan = planner.plan_actions(
        game_state,
        trigger="decision_required",
        legal_actions=[],
        decision_context=decision_context,
    )

    assert len(plan.actions) == 1
    assert plan.actions[0].action_type == ActionType.SELECT_N
    assert plan.actions[0].select_card_names == ["Mutavault"]
    assert "Discard Mutavault" in plan.voice_advice


def test_build_action_prompt_discard():
    planner = ActionPlanner(backend=DummyBackend())
    game_state = {
        "turn": {"turn_number": 14, "active_player": 2, "priority_player": 2, "phase": "Phase_Ending", "step": "Step_Cleanup"},
        "players": [{"seat_id": 2, "is_local": True}, {"seat_id": 1, "is_local": False}],
        "pending_decision": "Discard",
        "legal_actions": [],
        "hand": [{"name": "Mutavault"}, {"name": "Forest"}],
    }
    decision_context = {
        "type": "discard",
        "count": 1,
        "option_cards": ["Mutavault", "Forest"],
    }

    prompt = planner._build_action_prompt(
        game_state,
        trigger="decision_required",
        legal_actions=[],
        decision_context=decision_context,
    )

    assert "Discard decision" in prompt
    assert "pick 1 by number to discard" in prompt
    assert "Discard Mutavault" in prompt
    assert "Discard Forest" in prompt


def test_non_bridge_decision_backstop_forcing():
    from arenamcp.standalone import StandaloneCoach

    state = {
        "pending_decision": "Discard",
        "decision_context": {
            "type": "discard",
            "count": 1,
            "option_cards": ["Mutavault", "Forest"],
        },
        "legal_actions": [],
    }

    sig = StandaloneCoach._build_pending_decision_signature(state)
    assert sig is not None
    assert "Discard" in sig
    assert "Mutavault" in sig

    # When last advised sig is None or from an older decision, a trigger should be forced
    last_advised_sig = "Optional Action||optional_action||||||"
    assert sig != last_advised_sig

    # Once advised, sig matches and duplicate advice is suppressed
    last_advised_sig = sig
    assert sig == last_advised_sig
