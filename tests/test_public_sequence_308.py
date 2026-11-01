"""Mechanism regressions for audit G04/#308; original packet is unavailable."""

from tests.test_tactical_timing_backlog import board, card, cast, menu

from arenamcp.public_sequence import TriggerUseMemory, public_sequence_concerns


def state():
    s = board(2)
    s.update(match_id="test-game", opponent_hand_count=2)
    s["battlefield"] = [
        card(
            70,
            "Draw engine",
            "Whenever one or more other creatures you control with power 2 or less enter, draw a card. This ability triggers only once each turn.",
            "Enchantment",
        ),
        card(
            80,
            "Draw punisher",
            "Whenever an opponent draws a card except the first one they draw in each of their draw steps, this creature deals 1 damage to any target. Then amass Orcs 1.",
            "Creature",
            seat=2,
        ),
        card(
            90,
            "Protected creature",
            "Discard a card: This creature gains indestructible until end of turn. Tap it.",
            "Creature",
            seat=2,
            power=3,
            toughness=1,
        ),
    ]
    s["hand"] = [
        card(30, "Burn", "Burn deals 3 damage to any target. You gain 3 life.", "Instant", "{R}{W}"),
        card(31, "Small creature", "", "Creature", "{R}", power=1, toughness=1),
    ]
    return s


def test_public_discard_response_refutes_guaranteed_kill_without_forcing_target():
    s = state()
    reasons = public_sequence_concerns(
        menu(cast(30)), s, ["idx:30"], "Burn kills Protected creature and gains 3 life."
    )
    assert len(reasons) == 1 and "does not guarantee" in reasons[0]
    assert "lifegain" in reasons[0] and "discard" in reasons[0]
    assert not public_sequence_concerns(
        menu(cast(30)), s, ["idx:30"], "Burn forces a discard for indestructible and gains life."
    )
    s["opponent_hand_count"] = 0
    assert not public_sequence_concerns(menu(cast(30)), s, ["idx:30"], "Burn kills Protected creature.")


def test_unknown_hand_and_exile_do_not_invent_protection_solution():
    s = state()
    s.pop("opponent_hand_count")
    assert not public_sequence_concerns(menu(cast(30)), s, ["idx:30"], "Burn kills Protected creature.")
    s["opponent_hand_count"] = 2
    s["hand"][0]["oracle_text"] = "Exile target creature."
    assert not public_sequence_concerns(menu(cast(30)), s, ["idx:30"], "Burn removes Protected creature.")


def test_low_power_entry_requires_comparison_against_public_draw_punisher():
    reasons = public_sequence_concerns(
        menu(cast(31)), state(), ["idx:31"], "Develop Small creature as a repeatable engine."
    )
    assert len(reasons) == 1 and "If its once-per-turn" in reasons[0] and "amass Orcs 1" in reasons[0]
    assert not public_sequence_concerns(
        menu(cast(31)),
        state(),
        ["idx:31"],
        "Accept Draw punisher's trigger for the card; our protection preserves the creature.",
    )
    s = state()
    s["hand"][1]["power"] = 3
    assert not public_sequence_concerns(menu(cast(31)), s, ["idx:31"], "Develop the creature.")


def test_trigger_memory_requires_observed_matching_parent_link():
    s = state()
    memory = TriggerUseMemory()
    s["stack"] = [
        card(
            100,
            "Draw ability",
            "Whenever one or more other creatures you control with power 2 or less enter, draw a card.",
            "Ability",
            parent_instance_id=70,
        )
    ]
    assert memory.observe(s) == {70}
    s["stack"] = []
    assert memory.observe(s) == {70}
    reasons = public_sequence_concerns(
        menu(cast(31)),
        s,
        ["idx:31"],
        "Trigger Draw engine for another draw.",
        used_draw_triggers=memory.observe(s),
    )
    assert len(reasons) == 1 and "already observed" in reasons[0]
    s["turn"]["turn_number"] += 1
    assert not memory.observe(s)


def test_death_ability_and_new_object_do_not_spend_draw_trigger():
    s = state()
    memory = TriggerUseMemory()
    s["stack"] = [
        card(
            100,
            "Death ability",
            "When Draw engine dies, return it to the battlefield.",
            "Ability",
            parent_instance_id=70,
        )
    ]
    assert not memory.observe(s)
    s["stack"][0]["oracle_text"] = (
        "Whenever one or more other creatures you control with power 2 or less enter, draw a card."
    )
    assert memory.observe(s) == {70}
    s["battlefield"][0]["instance_id"] = 71
    reasons = public_sequence_concerns(
        menu(cast(31)),
        s,
        ["idx:31"],
        "Trigger Draw engine for another draw.",
        used_draw_triggers=memory.observe(s),
    )
    assert not any("already observed" in r for r in reasons)
    s["match_id"] = "new-game"
    assert not memory.observe(s)


def test_spent_trigger_does_not_project_draw_punishment_without_draw_claim():
    assert not public_sequence_concerns(
        menu(cast(31)), state(), ["idx:31"], "Deploy a useful blocker.", used_draw_triggers={70}
    )


def test_visible_activation_lock_abstains_from_protection_availability_claim():
    s = state()
    s["battlefield"].append(
        card(99, "Activation lock", "Your opponents can't activate abilities of creatures.", "Creature")
    )
    assert not public_sequence_concerns(menu(cast(30)), s, ["idx:30"], "Burn kills Protected creature.")


def test_planner_recheck_can_change_sequence_or_accept_explicit_tradeoff():
    import json
    from unittest.mock import Mock

    from arenamcp.action_planner import ActionPlanner

    planner = ActionPlanner(backend=Mock())
    planner._last_decision_trace = {}
    s = state()

    def parse(raw):
        data = json.loads(raw)
        return data, data["option_ids"]

    decision = menu(cast(30), cast(31))
    original = {"reasoning": "Develop Small creature for the draw."}
    ask = Mock(
        return_value=json.dumps(
            {
                "option_ids": ["idx:30"],
                "reasoning": "Burn removes Draw punisher before committing our draw creature.",
            }
        )
    )
    data, picks = planner._recheck_public_sequence(decision, s, original, ["idx:31"], ask, parse, "")
    assert picks == ["idx:30"] and ask.call_count == 1
    ask = Mock(
        return_value=json.dumps(
            {
                "option_ids": ["idx:31"],
                "reasoning": "Accept Draw punisher damage and amass for the card; this creature is expendable.",
            }
        )
    )
    _, picks = planner._recheck_public_sequence(decision, s, original, ["idx:31"], ask, parse, "")
    assert picks == ["idx:31"]


def test_failed_recheck_records_unresolved_risk_without_inventing_target_override():
    from unittest.mock import Mock

    from arenamcp.action_planner import ActionPlanner

    planner = ActionPlanner(backend=Mock())
    planner._last_decision_trace = {}
    data, picks = planner._recheck_public_sequence(
        menu(cast(30)),
        state(),
        {"reasoning": "Burn kills Protected creature."},
        ["idx:30"],
        Mock(side_effect=RuntimeError("offline")),
        Mock(),
        "",
    )
    assert picks == ["idx:30"]
    assert "unresolved" in data["reasoning"] and "does not guarantee" in data["reasoning"]
    assert planner._last_decision_trace["public_sequence_recheck"]["unresolved"]


def test_public_plan_observes_trigger_even_when_internal_path_returns_early(monkeypatch):
    from unittest.mock import Mock

    from arenamcp.action_planner import ActionPlanner

    s = state()
    s["stack"] = [
        card(
            100,
            "Draw ability",
            "Whenever one or more other creatures you control with power 2 or less enter, draw a card.",
            "Ability",
            parent_instance_id=70,
        )
    ]
    planner = ActionPlanner(backend=Mock())

    def answer(*args):
        planner._last_decision_trace = {}
        planner._last_decision_reasoning = "Let the draw resolve."
        return ["pass"]

    monkeypatch.setattr(planner, "_plan_decision_options", answer)
    planner.plan_decision_options(menu(cast(31)), s)
    s["stack"] = []
    assert planner._public_trigger_uses.observe(s) == {70}
