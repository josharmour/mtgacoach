"""Permanent regressions for the #84 final-bridge diagnostic explanation.

Adapted from the backlog-audit probes in
``2026-10-31/task/backlog-audit/84-review/test_84_false_trade_surface_review.py``.
The public-board fixture is the #120 owner's reconstruction of b717d4e1
06:43:58; no model inference or live game input is used.

Scope: the bridge guard may remove attackers after the model wrote its
reason. The submitted/recorded explanation must describe the declaration
that remains, while the model's original reason stays available as
``model_reasoning`` diagnostics. Combat decisions themselves are unchanged.
"""

import json
from types import SimpleNamespace
from unittest.mock import Mock

from tests.test_shang_attack_120_replay import incident

from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
from arenamcp.autopilot import AutopilotEngine

FALSE_REASON = "Attack; they likely won't trade a 4/4 for a 2/2"
STRATEGY = "Ramp toward Titan and big green payoffs while chipping in with Shang-Chi."


def proposed(state, reason=FALSE_REASON):
    name = next(c["name"] for c in state["battlefield"] if c["instance_id"] == 449)
    return GameAction(
        ActionType.DECLARE_ATTACKERS,
        attacker_names=[name],
        attacker_instance_ids=[449],
        attacker_targets={name: "Opponent"},
        reasoning=reason,
    )


def announce(action, state):
    engine = object.__new__(AutopilotEngine)
    engine._priority_window_signature = lambda _: ("same-request",)
    engine._config = SimpleNamespace(autoplay_speech="actions", enable_tts_preview=True)
    engine._notify = Mock()
    engine._speak_fn = Mock()
    engine._announce_submitted_action(action, state)
    visible = engine._notify.call_args.args[1]
    spoken = engine._speak_fn.call_args.args[0]
    reused = engine.get_reusable_advice(state)
    return visible, spoken, reused


def prepare(state, action):
    entries = [
        {"attackerInstanceId": iid, "damageRecipient": {"type": "Player", "playerSystemSeatId": 1}}
        for iid in action.attacker_instance_ids
    ]
    engine = object.__new__(AutopilotEngine)
    return engine._prepare_attack_submission(
        action,
        action.attacker_names,
        entries,
        state,
        {"attackers": state["decision_context"]["raw_attackers"]},
    )


def add_attacker(state, *, instance_id, name, power, toughness, keywords=(), oracle_text=""):
    state["battlefield"].append(
        {
            "instance_id": instance_id,
            "name": name,
            "power": power,
            "toughness": toughness,
            "controller_seat_id": 2,
            "is_tapped": False,
            "keywords": list(keywords),
            "card_types": ["Creature"],
            "type_line": "Creature — Test",
            "oracle_text": oracle_text,
        }
    )
    state["decision_context"]["legal_attacker_ids"].append(instance_id)
    state["decision_context"]["raw_attackers"].append(
        {
            "attackerInstanceId": instance_id,
            "mustAttack": False,
            "legalDamageRecipients": [{"type": "Player", "playerSystemSeatId": 1}],
        }
    )


def test_normal_planner_path_corrects_public_reason_and_memo(monkeypatch):
    state = incident()
    action = proposed(state)
    response = {
        "actions": [
            {
                "action_type": "declare_attackers",
                "attacker_names": action.attacker_names,
                "attacker_targets": action.attacker_targets,
                "reasoning": FALSE_REASON,
            }
        ],
        "overall_strategy": STRATEGY,
        "voice_advice": FALSE_REASON,
    }
    backend = Mock()
    backend.complete.return_value = json.dumps(response)
    planner = ActionPlanner(backend, timeout=1, land_drop_first=False)
    plan = planner.plan_actions(
        state,
        "decision_required",
        [f"Attack with: {action.attacker_names[0]} (2/2)", "Done (confirm attackers)"],
        state["decision_context"],
    )
    assert len(plan.actions) == 1
    fixed = plan.actions[0]
    assert fixed.attacker_names == [] and fixed.guard_hold
    assert "won't trade" not in fixed.reasoning
    assert "Black Widow" in fixed.reasoning
    assert "won't trade" not in plan.overall_strategy + plan.voice_advice
    assert FALSE_REASON in fixed.model_reasoning  # original is diagnostic only
    assert planner._turn_memo is plan
    assert "won't trade" not in planner._turn_memo.overall_strategy + planner._turn_memo.voice_advice
    submitted, entries = prepare(state, fixed)
    assert entries == []
    for surface in announce(submitted, state):
        assert "not attacking" in surface.lower()
        assert "won't trade" not in surface


def test_bridge_only_guard_blocks_attack_and_user_narration_omits_false_reason():
    state = incident()
    original = proposed(state)
    submitted, entries = prepare(state, original)
    assert entries == [] and submitted.attacker_instance_ids == []
    assert submitted.reasoning != FALSE_REASON
    assert "won't trade" not in submitted.reasoning
    assert "Shang-Chi" in submitted.reasoning and "Black Widow" in submitted.reasoning
    assert submitted.model_reasoning == FALSE_REASON
    for surface in announce(submitted, state):
        assert surface == "Not attacking."
        assert FALSE_REASON not in surface


def test_bridge_only_diagnostic_explanation_matches_no_attack(monkeypatch):
    state = incident()
    original = proposed(state)
    submitted, entries = prepare(state, original)
    assert entries == []
    packet = Mock()
    monkeypatch.setattr("arenamcp.match_packets.get_current_packet", lambda: packet)
    engine = object.__new__(AutopilotEngine)
    engine._get_game_state = lambda: state
    engine._current_plan = ActionPlan(actions=[original], overall_strategy=STRATEGY)
    engine._record_combat_decision(
        "DeclareAttackers",
        original,
        SimpleNamespace(submitted_action=submitted, method="mocked GRE bridge"),
    )
    saved = packet.add_combat_decision.call_args.kwargs
    assert saved["chosen"] == ["No attacks"]
    assert saved["explanation"] == submitted.reasoning
    assert "won't trade" not in saved["explanation"], saved
    assert saved["trace"]["model_reasoning"] == FALSE_REASON


def test_genuine_trade_control_keeps_attack_and_its_truthful_reason():
    state = incident()
    state["hand"] = []
    shang = next(c for c in state["battlefield"] if c["instance_id"] == 449)
    shang.update(power=4, toughness=4, keywords=["first strike", "deathtouch"])
    action = proposed(state, "A block by Black Widow trades both creatures with first strike and deathtouch.")
    submitted, entries = prepare(state, action)
    assert len(entries) == 1 and submitted.attacker_instance_ids == [449]
    assert submitted.reasoning == action.reasoning
    assert submitted.model_reasoning == ""
    for surface in announce(submitted, state):
        assert "Attacking with Shang-Chi" in surface and "Not attacking" not in surface


def test_partial_removal_describes_remaining_attackers_and_held_back_reason():
    state = incident()
    add_attacker(state, instance_id=900, name="Test Flyer", power=1, toughness=1, keywords=["flying"])
    shang_name = next(c["name"] for c in state["battlefield"] if c["instance_id"] == 449)
    action = GameAction(
        ActionType.DECLARE_ATTACKERS,
        attacker_names=[shang_name, "Test Flyer"],
        attacker_instance_ids=[449, 900],
        attacker_targets={shang_name: "Opponent", "Test Flyer": "Opponent"},
        reasoning=FALSE_REASON,
    )
    submitted, entries = prepare(state, action)
    assert [entry["attackerInstanceId"] for entry in entries] == [900]
    assert submitted.attacker_names == ["Test Flyer"]
    assert submitted.reasoning.startswith("Attacking with Test Flyer")
    assert "held back" in submitted.reasoning.lower()
    assert shang_name in submitted.reasoning and "Black Widow" in submitted.reasoning
    assert "won't trade" not in submitted.reasoning
    assert submitted.model_reasoning == FALSE_REASON


def test_unproductive_removal_explains_zero_power_hold():
    state = incident()
    add_attacker(
        state,
        instance_id=903,
        name="Test Wall",
        power=0,
        toughness=4,
        oracle_text="Defender",
    )
    shang_name = next(c["name"] for c in state["battlefield"] if c["instance_id"] == 449)
    action = GameAction(
        ActionType.DECLARE_ATTACKERS,
        attacker_names=[shang_name, "Test Wall"],
        attacker_instance_ids=[449, 903],
        attacker_targets={shang_name: "Opponent", "Test Wall": "Opponent"},
        reasoning=FALSE_REASON,
    )
    submitted, entries = prepare(state, action)
    assert entries == []
    assert submitted.reasoning.startswith("Held back")
    assert "zero power" in submitted.reasoning
    assert shang_name in submitted.reasoning and "Black Widow" in submitted.reasoning
    assert submitted.model_reasoning == FALSE_REASON


def test_mandatory_addition_replaces_hold_reason_and_preserves_model_reasoning():
    state = incident()
    state["decision_context"]["raw_attackers"][0]["mustAttack"] = True
    hold_reason = "Hold all creatures; no attacks."
    action = GameAction(ActionType.DECLARE_ATTACKERS, reasoning=hold_reason)
    submitted, entries = prepare(state, action)
    assert [entry["attackerInstanceId"] for entry in entries] == [449]
    assert submitted.attacker_instance_ids == [449]
    assert submitted.reasoning != hold_reason
    assert "Attacking with Shang-Chi" in submitted.reasoning
    assert "must attack" in submitted.reasoning.lower()
    assert "held back" not in submitted.reasoning.lower()
    assert submitted.model_reasoning == hold_reason


def test_repeated_preparation_preserves_original_model_reasoning():
    state = incident()
    original = proposed(state)
    first, _ = prepare(state, original)
    second, _ = prepare(state, first)
    again, _ = prepare(state, original)
    assert first.model_reasoning == FALSE_REASON
    assert second.model_reasoning == FALSE_REASON
    assert second.reasoning == first.reasoning
    assert again.model_reasoning == FALSE_REASON
