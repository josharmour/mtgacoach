"""#342 future-answer correction and shared factual target checks."""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from tests.test_tactical_timing_backlog import board, card, cast, craft_board, menu

from arenamcp.action_planner import ActionPlanner
from arenamcp.removal_windows import invalid_transform_hold, target_eligibility

HOLD = "Hold Seam Rip for the Throne of the Grim Captain craft; Kiora is not worth this answer."


def test_recorded_hold_claim_has_legal_current_answer_but_protected_future_face():
    state = craft_board()
    window = invalid_transform_hold(menu(cast(30)), state, ["pass"], HOLD)
    assert window and window.target_id == 91 and window.option_id == "idx:30"
    assert window.face == "The Grim Captain"
    assert "prerequisites" in window.reason


@pytest.mark.parametrize(
    "reason",
    [
        "Hold Seam Rip for a more threatening enchantment; craft may never be payable.",
        "Do not hold Seam Rip for the Throne of the Grim Captain craft.",
        "Hold Seam Rip until next turn if Throne of the Grim Captain cannot craft.",
        "Attack for lethal now, then keep Seam Rip.",
    ],
)
def test_legitimate_or_conditional_hold_is_not_overridden(reason):
    assert invalid_transform_hold(menu(cast(30)), craft_board(), ["pass"], reason) is None


def test_unknown_or_illegal_current_target_cannot_trigger_removal_fallback():
    original = craft_board()
    for field, value in [("mana_cost", "{3}"), ("oracle_text", "Hexproof\nCraft {4}"), ("related_faces", [])]:
        state = deepcopy(original)
        state["battlefield"][-1][field] = value
        assert invalid_transform_hold(menu(cast(30)), state, ["pass"], HOLD) is None
    assert invalid_transform_hold(menu(cast(30, False)), original, ["pass"], HOLD) is None
    original["battlefield"][-1]["oracle_text"] += "\nWard {9}"
    assert invalid_transform_hold(menu(cast(30)), original, ["pass"], HOLD) is None


def test_recheck_can_keep_pass_with_a_different_concrete_reason(monkeypatch):
    planner = ActionPlanner(backend=Mock())
    planner._last_decision_trace = {}
    monkeypatch.setattr("arenamcp.action_planner._lethal_this_turn", lambda state: False)
    ask = Mock(
        return_value=json.dumps(
            {
                "option_ids": ["pass"],
                "reasoning": "Hold for the opposing enchantment engine; its next trigger matters more.",
            }
        )
    )

    def parse(raw):
        data = json.loads(raw)
        return data, data["option_ids"]

    data, picks = planner._repair_invalid_transform_hold(
        menu(cast(30)), craft_board(), {"reasoning": HOLD}, ["pass"], ask, parse, "menu"
    )
    assert picks == ["pass"] and "engine" in data["reasoning"]
    assert ask.call_count == 1


@pytest.mark.parametrize("failed", [False, True])
def test_repeated_invalid_hold_or_failed_recheck_takes_verified_current_window(monkeypatch, failed):
    planner = ActionPlanner(backend=Mock())
    planner._last_decision_trace = {}
    monkeypatch.setattr("arenamcp.action_planner._lethal_this_turn", lambda state: False)
    ask = (
        Mock(side_effect=RuntimeError("offline"))
        if failed
        else Mock(return_value=json.dumps({"option_ids": ["pass"], "reasoning": HOLD}))
    )

    def parse(raw):
        data = json.loads(raw)
        return data, data["option_ids"]

    data, picks = planner._repair_invalid_transform_hold(
        menu(cast(30)), craft_board(), {"reasoning": HOLD}, ["pass"], ask, parse, "menu"
    )
    assert picks == ["idx:30"] and "Throne of the Grim Captain #91" in data["reasoning"]
    assert planner._last_decision_trace["removal_window_recheck"]["fallback"] is True
    assert ask.call_count == 1


def test_immediate_combat_win_preserves_pass(monkeypatch):
    planner = ActionPlanner(backend=Mock())
    monkeypatch.setattr("arenamcp.action_planner._lethal_this_turn", lambda state: True)
    ask = Mock()
    _, picks = planner._repair_invalid_transform_hold(
        menu(cast(30)), craft_board(), {"reasoning": HOLD}, ["pass"], ask, Mock(), "menu"
    )
    assert picks == ["pass"] and not ask.called


def test_target_checker_reads_modes_and_plains_cap_separately():
    state = board(3)
    target = card(
        50, "Drana and Linvala", "", "Legendary Creature", "{1}{W}{W}{B}", seat=2, power=3, toughness=4
    )
    arms = card(
        30,
        "Lay Down Arms",
        "Exile target creature with mana value less than or equal to the number of Plains you control.",
        "Sorcery",
        "{W}",
    )
    assert target_eligibility(arms, target, state) is False
    state["battlefield"].append(card(4, "Plains", "{T}: Add {W}.", "Basic Land — Plains"))
    assert target_eligibility(arms, target, state) is True
    evil = card(
        31,
        "Destroy Evil",
        "Choose one —\n• Destroy target creature with toughness 4 or greater.\n• Destroy target enchantment.",
        "Instant",
        "{1}{W}",
    )
    ash = card(51, "Zul Ashur", "", "Creature", "{1}{B}", seat=2, power=2, toughness=2)
    assert target_eligibility(evil, ash, state) is False
    assert target_eligibility(evil, target, state) is True
    assert target_eligibility(evil, card(52, "Anthem", "", "Enchantment", seat=2), state) is True


def test_typed_choice_cannot_repeat_the_invalid_hold_after_recheck(monkeypatch):
    from tests.test_typed_decision_context import _CapturingBackend

    backend = _CapturingBackend(json.dumps({"option_ids": ["pass"], "reasoning": HOLD}))
    planner = ActionPlanner(backend=backend)
    monkeypatch.setattr(planner, "_line_prompt_context", lambda *a: (None, "", {}))
    monkeypatch.setattr(planner, "_grounded_plan_block", lambda *a, **kw: "")
    monkeypatch.setattr("arenamcp.action_planner._lethal_this_turn", lambda state: False)
    chosen = planner._llm_decision_options(menu(cast(30)), craft_board())
    assert chosen == ["idx:30"]
    assert sum("REMOVAL WINDOW RECHECK" in p for p in backend.prompts) == 1
    assert len(backend.prompts) <= 3  # existing low/high hedge plus one repair
    assert "Throne of the Grim Captain #91" in planner._last_decision_reasoning


def test_conditional_hexproof_and_visible_bypass_are_not_unconditional_windows():
    state = craft_board()
    state["battlefield"][-1]["related_faces"][0]["oracle_text"] = "Hexproof from blue"
    assert invalid_transform_hold(menu(cast(30)), state, ["pass"], HOLD) is None
    state = craft_board()
    state["battlefield"].append(
        card(60, "Exception", "You may target creatures as though they didn't have hexproof.", "Artifact")
    )
    assert invalid_transform_hold(menu(cast(30)), state, ["pass"], HOLD) is None


def test_current_types_and_phasing_control_targeting_and_plains_caps():
    state = board(3)
    source = card(
        30,
        "Lay Down Arms",
        "Exile target creature with mana value less than or equal to the number of Plains you control.",
        "Sorcery",
        "{W}",
    )
    heliod = card(
        99,
        "Heliod, Sun-Crowned",
        "Indestructible",
        "Legendary Enchantment Creature — God",
        "{2}{W}",
        seat=2,
        card_types=["CardType_Enchantment"],
    )
    assert target_eligibility(source, heliod, state) is False
    heliod["card_types"] = ["CardType_Creature", "CardType_Enchantment"]
    assert target_eligibility(source, heliod, state) is True
    state["battlefield"][0]["is_phased_out"] = True
    assert target_eligibility(source, heliod, state) is False
    state["battlefield"][0]["is_phased_out"] = False
    state["battlefield"][0]["subtypes"] = ["SubType_Island"]
    assert target_eligibility(source, heliod, state) is False
    state["battlefield"][0].pop("subtypes")
    heliod["is_phased_out"] = True
    assert target_eligibility(source, heliod, state) is False


def test_missing_rules_or_restricted_stats_stay_unknown():
    state = board(3)
    source = card(30, "Answer", "Exile target creature.", "Instant", "{W}")
    target = card(50, "Unknown target", "", "Creature", "{2}{G}", seat=2, power=2, toughness=2)
    target.pop("oracle_text")
    assert target_eligibility(source, target, state) is None
    source["oracle_text"] = "Exile target creature with mana value 3 or less."
    target["oracle_text"] = ""
    target.pop("mana_cost")
    assert target_eligibility(source, target, state) is None
