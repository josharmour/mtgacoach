"""A combat guard's hold is not undone by the bridge (#75).

2026-10-19 T16 (match abde2a07, 11:33:57): at 8 life against 15, with their
whole board tapped, the model planned "Race: swing 3 with the pumped
challenger". The crackback guard held Emberheart Challenger back ("the
planned attack leaves us at -3 after their best attack ...; holding
everything back keeps us at -1") and the log said "PLAN: not attacking".
70 ms later the bridge declared it anyway (``declared_count 1``):
``_explicit_no_attack`` saw a fallback (no model choice) whose turn plan
still had the attack step being executed, so ``combat_choice`` put the
attacker back. The board below is that DeclareAttackers window, from the
match's Player.log.
"""

from __future__ import annotations

import copy
import logging
from unittest.mock import Mock

from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
from arenamcp.autopilot_bridge import _BridgeSubmitMixin
from arenamcp.combat_strategy import combat_choice

US, THEM = 1, 2
EMBERHEART_TEXT = (
    "Haste\nProwess (Whenever you cast a noncreature spell, this creature gets +1/+1 until end of turn.)\n"
    "Valiant — Whenever this creature becomes the target of a spell or ability you control for the first "
    "time each turn, exile the top card of your library. Until end of turn, you may play that card."
)
ADHERENTS_TEXT = "Trample\nThis creature gets +1/+1 for each creature you control and each creature card in your graveyard."
DIGNITARY_TEXT = (
    "As an additional cost to cast this spell, behold an Elf or pay {2}.\n{T}: Add {G}{G}. Activate only if "
    "there is an Elf card in your graveyard."
)
PLAYER = {"type": "DamageRecType_Player", "playerSystemSeatId": THEM}


def creature(iid, name, power, toughness, seat, text="", *, type_line="Creature", **extra):
    return {
        "instance_id": iid,
        "name": name,
        "type_line": type_line,
        "card_types": ["CardType_Creature"],
        "power": power,
        "toughness": toughness,
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "oracle_text": text,
        "is_tapped": False,
        **extra,
    }


def land(iid, name, seat, *, tapped=False, color="R"):
    return {
        "instance_id": iid,
        "name": name,
        "type_line": f"Basic Land — {name}",
        "card_types": ["CardType_Land"],
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "oracle_text": f"({{T}}: Add {{{color}}}.)",
        "is_tapped": tapped,
    }


def turn_16_board() -> dict:
    """11:33:54: Tectonic Hazard resolved (prowess: Challenger 3/3), their attackers from T15 still tapped."""
    return {
        "turn": {
            "turn_number": 16,
            "active_player": US,
            "priority_player": US,
            "phase": "Phase_Combat",
            "step": "Step_DeclareAttack",
        },
        "local_seat_id": US,
        "opponent_seat_id": THEM,
        "players": [
            {"seat_id": US, "life_total": 8, "is_local": True, "mana_pool": {}},
            {"seat_id": THEM, "life_total": 15, "is_local": False, "mana_pool": {}},
        ],
        "battlefield": [
            creature(
                377,
                "Emberheart Challenger",
                3,
                3,
                US,
                EMBERHEART_TEXT,
                type_line="Creature — Mouse Warrior",
                keywords=["haste"],
                turn_entered_battlefield=14,
            ),
            land(281, "Mountain", US, tapped=True),
            *(land(iid, "Mountain", US) for iid in (290, 305, 329, 342)),
            creature(
                363,
                "Moon-Vigil Adherents",
                7,
                7,
                THEM,
                ADHERENTS_TEXT,
                type_line="Creature — Elf Druid",
                keywords=["trample"],
                is_tapped=True,
                damage=1,
            ),
            creature(
                324,
                "Lys Alana Dignitary",
                2,
                3,
                THEM,
                DIGNITARY_TEXT,
                type_line="Creature — Elf Advisor",
                is_tapped=True,
                damage=1,
            ),
            creature(
                375,
                "Elf",
                2,
                2,
                THEM,
                type_line="Token Creature — Elf Warrior",
                is_tapped=True,
                damage=1,
                object_kind="TOKEN",
            ),
            land(381, "Overgrown Tomb", THEM, tapped=True, color="G"),
            *(land(iid, "Forest", THEM, tapped=True, color="G") for iid in (279, 299, 320)),
            land(285, "Swamp", THEM, color="B"),
        ],
        "hand": [],
        "graveyard": [],
        "exile": [],
        "stack": [],
        "zones": {"opponent_hand_count": 2, "opponent_library_count": 45},
        "decision_context": {
            "type": "declare_attackers",
            "legal_attackers": ["Emberheart Challenger"],
            "legal_attacker_ids": [377],
            "raw_attackers": [{"attackerInstanceId": 377, "legalDamageRecipients": [PLAYER]}],
        },
        "_bridge_connected": True,
        "_bridge_request_type": "DeclareAttackers",
        "_bridge_request_class": "DeclareAttackersRequest",
    }


def _guarded_plan(state: dict) -> ActionPlan:
    plan = ActionPlan(
        actions=[
            GameAction(
                ActionType.DECLARE_ATTACKERS,
                attacker_names=["Emberheart Challenger"],
                attacker_instance_ids=[377],
                target_names=["Opponent"],
                reasoning="All their blockers are tapped; pump is wasted if it stays back.",
            )
        ],
        overall_strategy="Race: swing 3 with the pumped challenger while their board is tapped.",
    )
    ActionPlanner.__new__(ActionPlanner)._check_attack_crackback(plan, state, state["decision_context"])
    # autopilot.py: a plan with a fallback_reason is no model choice.
    plan.actions[0].model_choice = not plan.fallback_reason
    return plan


def _pilot(state: dict) -> _BridgeSubmitMixin:
    pilot = _BridgeSubmitMixin()
    bridge = Mock()
    bridge.connect.return_value = True
    bridge.get_pending_actions.return_value = {
        "ok": True,
        "has_pending": True,
        "request_class": "DeclareAttackersRequest",
        "attackers": [{"attackerInstanceId": 377, "legalDamageRecipients": [PLAYER]}],
        "bridge_runtime": "il2cpp-macos",
        "game_state_id": 402,
        "msg_id": 1,
    }
    bridge.submit_attackers_raw.return_value = {"ok": True, "submitted_type": "DeclareAttackersSubmit"}
    pilot._gre_bridge = bridge
    pilot._get_game_state = lambda: copy.deepcopy(state)
    pilot._log_execution_path = Mock()
    pilot._gre_bridge_failed_methods = set()
    # The locked turn plan's attack step is the one being executed.
    pilot._planner = Mock()
    pilot._planner.has_pending_attack_intent.return_value = True
    return pilot


def test_the_crackback_guards_hold_is_a_decided_hold(caplog):
    state = turn_16_board()
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        plan = _guarded_plan(state)
    assert "leaves us at -3 after their best attack" in caplog.text
    assert "holding everything back keeps us at -1" in caplog.text
    action = plan.actions[0]
    assert action.attacker_names == [] and plan.fallback_reason == "planner_crackback_guard"
    assert action.guard_hold is True and action.model_choice is False
    # The solver would attack: this is what put Emberheart back at 11:33:57.45.
    assert list(combat_choice(state).assignments) == [377]


def test_the_bridge_honours_the_guard_hold_instead_of_re_adding_the_attacker():
    state = turn_16_board()
    action = _guarded_plan(state).actions[0]
    pilot = _pilot(state)
    assert pilot._explicit_no_attack(action) is True

    result = pilot._try_bridge_declare_attackers_inner(action)

    assert result is not None and result.success
    pilot._gre_bridge.submit_attackers_raw.assert_called_once_with([])
    pilot._gre_bridge.submit_attackers.assert_not_called()
    assert result.submitted_action.attacker_names == []


def test_without_the_guard_mark_the_same_fallback_is_still_refilled():
    # The #398-#402 protection stands for an empty fallback no guard decided on.
    state = turn_16_board()
    action = _guarded_plan(state).actions[0]
    action.guard_hold = False
    assert _pilot(state)._explicit_no_attack(action) is False


def test_all_in_still_beats_a_guard_hold():
    state = turn_16_board()
    action = _guarded_plan(state).actions[0]
    pilot = _pilot(state)
    all_in = GameAction(
        ActionType.DECLARE_ATTACKERS, attacker_names=["Emberheart Challenger"], target_names=["Opponent"]
    )
    pilot._all_in_attack_action = Mock(return_value=all_in)
    pilot._attack_entries = Mock(return_value=[])
    pilot._prepare_attack_submission = Mock(return_value=(all_in, []))
    pilot._try_bridge_declare_attackers_inner(action)
    pilot._attack_entries.assert_called_once()
    assert pilot._attack_entries.call_args.args[1] == ["Emberheart Challenger"]
