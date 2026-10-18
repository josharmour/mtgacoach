"""The model's own decision not to attack is honoured (live autopilot game 2026-10-18 13:56:20-25).

Turn 9: the planner chose "Done (confirm attackers)" from
['Attack with: Diviner of Victory (1/1)', 'Done (confirm attackers)'] with the
intent "Keep Sphinx as the evasive clock; preserve Diviner for blocking Loot".
The bridge's Done handler swapped in the solver's pick (issues #398-#402
protection), Diviner of Victory attacked and died. Only the authoritative
overrides (all in, lethal on board) may replace an explicit no-attack; an
auto-confirmed or fallback Done keeps the solver.
"""

from __future__ import annotations

from unittest.mock import Mock

from arenamcp.action_planner import ActionPlan, ActionType, GameAction
from arenamcp.autopilot_bridge import _BridgeSubmitMixin

STATE = {
    "_bridge_connected": True,
    "_bridge_request_type": "DeclareAttackers",
    "_bridge_request_class": "DeclareAttackersRequest",
    "turn": {"turn_number": 9, "active_player": 1, "phase": "Phase_Combat", "step": "Step_DeclareAttack"},
    "players": [
        {"seat_id": 1, "is_local": True, "life_total": 20},
        {"seat_id": 2, "is_local": False, "life_total": 20},
    ],
    "battlefield": [
        {
            "instance_id": 204,
            "name": "Diviner of Victory",
            "controller_seat_id": 1,
            "owner_seat_id": 1,
            "type_line": "Creature — Human Wizard",
            "power": 1,
            "toughness": 1,
        },
        {
            "instance_id": 288,
            "name": "Sphinx of False Conclusions",
            "controller_seat_id": 1,
            "owner_seat_id": 1,
            "type_line": "Creature — Sphinx",
            "power": 4,
            "toughness": 4,
            "turn_entered_battlefield": 9,
        },
    ],  # fmt: skip
    "decision_context": {
        "type": "declare_attackers",
        "legal_attackers": ["Diviner of Victory"],
        "legal_attacker_ids": [204],
    },  # fmt: skip
}
DIVINER = GameAction(
    action_type=ActionType.DECLARE_ATTACKERS, attacker_names=["Diviner of Victory"], target_names=["Opponent"]
)


def _pilot(*, attack_planned: bool = False, authoritative: GameAction | None = None):
    pilot = _BridgeSubmitMixin()
    pilot._gre_bridge = Mock()
    pilot._gre_bridge.connect.return_value = True
    pilot._gre_bridge.submit_attackers.return_value = True
    pilot._log_execution_path = Mock()
    pilot._gre_bridge_failed_methods = set()
    pilot._planner = Mock()
    pilot._planner.has_pending_attack_intent.return_value = attack_planned
    pilot._solver_attack_action = Mock(return_value=GameAction(**{**DIVINER.__dict__}))
    pilot._authoritative_attack_action = Mock(return_value=authoritative)
    pilot._try_bridge_declare_attackers = Mock(side_effect=lambda action: ("declared", action))
    return pilot


def _done(model_choice: bool) -> GameAction:
    return GameAction(
        ActionType.CLICK_BUTTON,
        card_name="done",
        reasoning="Keep Sphinx as the evasive clock; preserve Diviner for blocking Loot",
        model_choice=model_choice,
    )


def test_the_models_done_with_attackers_offered_is_no_attack():
    pilot = _pilot()
    result = pilot._try_gre_bridge(_done(True), dict(STATE))
    assert result is not None and result.success
    pilot._gre_bridge.submit_attackers.assert_called_once_with([])
    pilot._try_bridge_declare_attackers.assert_not_called()
    pilot._solver_attack_action.assert_not_called()
    assert result.submitted_action.attacker_names == []
    assert ActionPlan(actions=[result.submitted_action]).spoken_actions() == "Don't attack."


def test_lethal_or_all_in_still_overrides_an_explicit_hold():
    lethal = GameAction(
        **{**DIVINER.__dict__, "override_note": "lethal on board, so the solver's attack goes ahead"}
    )
    pilot = _pilot(authoritative=lethal)
    kind, submitted = pilot._try_gre_bridge(_done(True), dict(STATE))
    assert kind == "declared" and submitted.attacker_names == ["Diviner of Victory"]
    pilot._gre_bridge.submit_attackers.assert_not_called()
    spoken = ActionPlan(actions=[submitted]).spoken_actions()
    assert spoken.startswith("Attack with Diviner of Victory") and "lethal on board" in spoken


def test_an_auto_confirmed_done_keeps_the_398_solver_protection():
    pilot = _pilot()
    kind, submitted = pilot._try_gre_bridge(_done(False), dict(STATE))
    assert kind == "declared" and submitted.attacker_names == ["Diviner of Victory"]
    assert "combat solver picked them" in ActionPlan(actions=[submitted]).spoken_actions()


def test_a_done_while_the_turn_plan_still_attacks_is_a_confirm():
    pilot = _pilot(attack_planned=True)
    kind, _submitted = pilot._try_gre_bridge(_done(True), dict(STATE))
    assert kind == "declared"
    pilot._solver_attack_action.assert_called_once()


def test_the_turn_9_board_has_no_authoritative_override():
    pilot = _BridgeSubmitMixin()
    pilot._solver_attack_action = Mock(return_value=DIVINER)
    assert pilot._authoritative_attack_action(dict(STATE)) is None
