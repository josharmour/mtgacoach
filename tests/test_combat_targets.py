import json
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import (
    ActionPlan,
    ActionPlanner,
    ActionType,
    GameAction,
    game_action_to_schema_json,
)
from arenamcp.autopilot_bridge import _BridgeSubmitMixin
from arenamcp.combat_targets import attack_target_prompt, choose_recipient


def state():
    recipients = [
        {"type": "Player", "playerSystemSeatId": 1},
        {"type": "Planeswalker", "planeswalkerInstanceId": 297},
    ]
    return {
        "players": [{"seat_id": 2, "is_local": True}, {"seat_id": 1, "is_local": False}],
        "battlefield": [
            {"name": "Beast", "instance_id": 288},
            {"name": "Pia", "instance_id": 238},
            {"name": "Jace", "instance_id": 297, "type_line": "Planeswalker"},
        ],
        "decision_context": {
            "type": "declare_attackers",
            "raw_attackers": [
                {"attackerInstanceId": identity, "legalDamageRecipients": recipients}
                for identity in (288, 238)
            ],
        },
    }


def engine():
    result = _BridgeSubmitMixin()
    result._get_game_state = state
    result._find_instance_id = lambda name, battlefield, local: next(
        (card["instance_id"] for card in battlefield if card["name"] == name), None
    )
    result._gre_bridge = Mock()
    result._gre_bridge.get_pending_actions.return_value = {
        "has_pending": True,
        "request_class": "DeclareAttackerRequest",
        "attackers": state()["decision_context"]["raw_attackers"],
    }
    result._gre_bridge.submit_attackers_raw.return_value = {"ok": True}
    result._attack_override = Mock(return_value=None)
    result._log_execution_path = Mock()
    return result


def test_target_mapping_round_trips_and_speaks_recipients():
    action = GameAction(
        action_type=ActionType.DECLARE_ATTACKERS,
        attacker_names=["Beast", "Pia"],
        attacker_targets={"Beast": "Jace [297]", "Pia": "Opponent"},
    )
    planner = ActionPlanner.__new__(ActionPlanner)
    parsed = planner._parse_action(json.loads(game_action_to_schema_json(action))["actions"][0])
    assert parsed.attacker_targets == action.attacker_targets
    assert "Jace [297]" in str(parsed)
    assert planner._is_action_legal(parsed, ["Attack with: Beast", "Attack with: Pia"])
    plan = ActionPlan(actions=[parsed])
    assert "Jace" in plan.spoken_actions()


def test_prompt_names_the_legal_player_and_planeswalker():
    snapshot = state()
    prompt = attack_target_prompt(snapshot, snapshot["decision_context"])
    assert "Beast: Opponent; Jace [297]" in prompt


def test_attack_speech_uses_submitted_recipient_not_conflicting_prose():
    planner = ActionPlanner.__new__(ActionPlanner)
    response = json.dumps(
        {
            "actions": [
                {
                    "action_type": "declare_attackers",
                    "attacker_names": ["Beast"],
                    "attacker_targets": {"Beast": "Jace [297]"},
                }
            ],
            "voice_advice": "Attack the opponent with Beast.",
        }
    )
    plan = planner._parse_response(response, ["Attack with: Beast"])
    assert plan.voice_advice == "Attack Jace with Beast."
    assert plan.actions[0].attacker_targets == {"Beast": "Jace [297]"}
    assert "opponent" not in plan.voice_advice


@pytest.mark.parametrize("method", ["_try_bridge_declare_attackers", "_try_gre_bridge_attackers"])
def test_split_attack_preserves_each_recipient(method):
    pilot = engine()
    action = GameAction(
        action_type=ActionType.DECLARE_ATTACKERS,
        attacker_names=["Beast", "Pia"],
        attacker_targets={"Beast": "Jace [297]", "Pia": "Opponent"},
    )
    assert getattr(pilot, method)(action).success
    submit = (
        pilot._gre_bridge.submit_attackers_raw
        if method == "_try_bridge_declare_attackers"
        else pilot._gre_bridge.submit_attackers
    )
    entries = submit.call_args.args[0]
    assert entries[0]["damageRecipient"]["planeswalkerInstanceId"] == 297
    assert entries[1]["damageRecipient"]["playerSystemSeatId"] == 1
    pilot._attack_override.assert_not_called()


@pytest.mark.parametrize("target", ["", "Missing walker", "You"])
def test_missing_or_illegal_target_does_not_silently_attack_face(target):
    pilot = engine()
    action = GameAction(
        action_type=ActionType.DECLARE_ATTACKERS,
        attacker_names=["Beast"],
        target_names=[target] if target else [],
    )
    assert pilot._try_bridge_declare_attackers(action) is None
    pilot._gre_bridge.submit_attackers_raw.assert_not_called()


def test_current_bridge_recipients_override_stale_log_targets():
    pilot = engine()
    pilot._gre_bridge.get_pending_actions.return_value["attackers"][0]["legalDamageRecipients"] = [
        {"type": "Player", "playerSystemSeatId": 1}
    ]
    action = GameAction(
        action_type=ActionType.DECLARE_ATTACKERS, attacker_names=["Beast"], attacker_targets={"Beast": "Jace"}
    )
    assert pilot._try_bridge_declare_attackers(action) is None
    pilot._gre_bridge.submit_attackers_raw.assert_not_called()


def test_empty_done_uses_joint_solver_and_preserves_split_targets():
    pilot = engine()
    snapshot = state()
    for card, power in zip(snapshot["battlefield"][:2], (3, 4), strict=True):
        card.update(type_line="Creature", power=power, toughness=power, owner_seat_id=2, controller_seat_id=2)
    snapshot["battlefield"][2]["counters"] = {"Loyalty": 3}
    pilot._get_game_state = lambda: snapshot
    result = pilot._try_bridge_declare_attackers(GameAction(action_type=ActionType.DECLARE_ATTACKERS))
    assert result.success
    entries = {
        entry["attackerInstanceId"]: entry["damageRecipient"]
        for entry in pilot._gre_bridge.submit_attackers_raw.call_args.args[0]
    }
    assert entries[288]["planeswalkerInstanceId"] == 297
    assert entries[238]["playerSystemSeatId"] == 1
    pilot._attack_override.assert_not_called()


def test_partial_attacker_resolution_never_submits_partial_attack():
    pilot = engine()
    action = GameAction(
        action_type=ActionType.DECLARE_ATTACKERS, attacker_names=["Beast", "Gone"], target_names=["Opponent"]
    )
    assert pilot._try_bridge_declare_attackers(action) is None
    pilot._gre_bridge.submit_attackers_raw.assert_not_called()


def test_failed_finalization_is_not_reported_as_success(monkeypatch):
    monkeypatch.setattr("arenamcp.autopilot_bridge.time.sleep", lambda delay: None)
    pilot = engine()
    pilot._gre_bridge.submit_attackers_raw.side_effect = [{"ok": True, "needs_finalize": True}, {"ok": False}]
    action = GameAction(
        action_type=ActionType.DECLARE_ATTACKERS, attacker_names=["Beast"], target_names=["Jace"]
    )
    assert pilot._try_bridge_declare_attackers(action) is None


def test_duplicate_planeswalker_names_require_exact_identity():
    snapshot = state()
    snapshot["battlefield"].append({"name": "Jace", "instance_id": 298})
    legal = [{"planeswalkerInstanceId": 297}, {"planeswalkerInstanceId": 298}]
    with pytest.raises(ValueError):
        choose_recipient("Jace", legal, snapshot)
    assert choose_recipient("Jace [298]", legal, snapshot) == legal[1]
