"""Regression table for _is_planner_action_stale_vs_bridge.

A planned step is stale when the bridge's pending request can no longer take
it: the window changed between planning and submission. Stale steps are
silently skipped and re-planned instead of flipping autopilot to MANUAL
REQUIRED and auto-filing bridge_submit_failed noise. Shapes:

  0. No pending request at all: the priority window closed during planning
     (#191 select_target, #194 re-cast Michelangelo, #187 search race).
  1. PLAY_LAND against ActionsAvailable with no Play entry: the land drop was
     already used (#136 #137 #139 #140).
  2. PLAY_LAND/CAST_SPELL against a different request type (SelectN, Search,
     SelectTargets, PayCosts): a new decision window displaced the step (#189).
  2a. ACTIVATE_ABILITY against a non-ActionsAvailable request (#392 Mutagen,
     #407 Utter Insignificance; 2026-07-29).
  3. DECLARE_ATTACKERS against a non-combat request.
  5. PASS/RESOLVE against a request that has no pass (bug_20260610_121152:
     a PayCostsReq opened mid-plan when the user cast Sapling Nursery).
  7. ASSIGN_DAMAGE/DISTRIBUTE against any other request: only one request
     family takes them (2026-10-18 18:18:26: assign_damage reached the
     commander's return-to-command-zone OptionalAction).

A step against the request that takes it is never stale: a failure there is
a real bridge problem and must still surface MANUAL REQUIRED.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from tests.commander_return_states import COMMANDER_RETURN_POLL, assign_damage_state
from tests.fakes import FakeBridge, make_engine

from arenamcp.action_planner import ActionType, GameAction
from arenamcp.autopilot import AutopilotEngine

NO_REQUEST = (None, None)
ACTIONS_AVAILABLE = ("ActionsAvailable", "ActionsAvailableRequest")
SELECT_N = ("SelectN", "SelectNRequest")
SEARCH = ("Search", "SearchRequest")
SELECT_TARGETS = ("SelectTargets", "SelectTargetsRequest")
PAY_COSTS = ("PayCosts", "PayCostsRequest")
PAY_COSTS_REQ = ("PayCosts", "PayCostsReq")
CASTING_TIME = ("CastingTimeOptions", "CastingTimeOptionRequest")
DECLARE_ATTACKERS = ("DeclareAttackersReq", "DeclareAttackersRequest")
DECLARE_ATTACKER = ("DeclareAttackers", "DeclareAttackerRequest")
ASSIGN_DAMAGE = ("AssignDamage", "AssignDamageRequest")
DISTRIBUTION = ("Distribution", "DistributionRequest")
OPTIONAL_ACTION = ("OptionalAction", "OptionalActionMessageRequest")

PLAY_LAND = GameAction(action_type=ActionType.PLAY_LAND, card_name="Escape Tunnel")
CAST = GameAction(action_type=ActionType.CAST_SPELL, card_name="Lightning Bolt")
TARGET = GameAction(action_type=ActionType.SELECT_TARGET, target_names=["Llanowar Elves"])
ATTACK = GameAction(action_type=ActionType.DECLARE_ATTACKERS, attacker_names=["Goblin Guide"])
SELECT = GameAction(action_type=ActionType.SELECT_N, select_card_names=["Forest"])
SEARCH_LIBRARY = GameAction(action_type=ActionType.SEARCH_LIBRARY, select_card_names=["Forest"])
PASS = GameAction(action_type=ActionType.PASS_PRIORITY)
RESOLVE = GameAction(action_type=ActionType.RESOLVE)
ACTIVATE = GameAction(
    action_type=ActionType.ACTIVATE_ABILITY, card_name="Mutagen", target_names=["Michelangelo"]
)
ASSIGN = GameAction(action_type=ActionType.ASSIGN_DAMAGE, card_name="Mossborn Hydra")
DISTRIBUTE = GameAction(action_type=ActionType.DISTRIBUTE, distribution={"Llanowar Elves": 2})

WITH_PLAY = [{"actionType": "Play", "grpId": 12345}, {"actionType": "Cast", "grpId": 67890}]
CAST_ONLY = [{"actionType": "Cast", "grpId": 67890}]

CASES = [
    # (id, action, (request_type, request_class), bridge actions or None, stale)
    ("0-play_land-no_request", PLAY_LAND, NO_REQUEST, None, True),
    ("0-cast-no_request-#194", CAST, NO_REQUEST, None, True),
    ("0-select_target-no_request-#191", TARGET, NO_REQUEST, None, True),
    ("0-search-no_request-#187", SEARCH_LIBRARY, NO_REQUEST, None, True),
    ("0-activate-no_request", ACTIVATE, NO_REQUEST, None, True),
    ("1-play_land-actions_without_play", PLAY_LAND, ACTIONS_AVAILABLE, CAST_ONLY, True),
    ("ok-play_land-actions_with_play", PLAY_LAND, ACTIONS_AVAILABLE, WITH_PLAY, False),
    ("2-play_land-select_n", PLAY_LAND, SELECT_N, [], True),
    ("2-cast-search", CAST, SEARCH, [], True),
    ("2-play_land-select_targets", PLAY_LAND, SELECT_TARGETS, None, True),
    ("2-play_land-pay_costs", PLAY_LAND, PAY_COSTS, None, True),
    ("2a-activate-select_targets-#392", ACTIVATE, SELECT_TARGETS, [], True),
    ("2a-activate-declare_attackers-#407", ACTIVATE, DECLARE_ATTACKER, [], True),
    ("2a-activate-pay_costs", ACTIVATE, PAY_COSTS_REQ, None, True),
    ("ok-activate-actions_available", ACTIVATE, ACTIONS_AVAILABLE, [], False),
    ("3-attack-actions_available", ATTACK, ACTIONS_AVAILABLE, None, True),
    ("ok-attack-declare_attackers", ATTACK, DECLARE_ATTACKERS, None, False),
    ("select_target-actions_available", TARGET, ACTIONS_AVAILABLE, None, True),
    ("ok-select_target-select_targets", TARGET, SELECT_TARGETS, None, False),
    ("select_n-actions_available", SELECT, ACTIONS_AVAILABLE, None, True),
    ("ok-select_n-select_n", SELECT, SELECT_N, None, False),
    ("ok-search-search", SEARCH_LIBRARY, SEARCH, None, False),
    ("5-pass-pay_costs", PASS, PAY_COSTS_REQ, None, True),
    ("5-resolve-casting_time", RESOLVE, CASTING_TIME, None, True),
    ("ok-pass-actions_available", PASS, ACTIONS_AVAILABLE, None, False),
    ("7-assign_damage-optional_action", ASSIGN, OPTIONAL_ACTION, None, True),
    ("7-assign_damage-actions_available", ASSIGN, ACTIONS_AVAILABLE, None, True),
    ("ok-assign_damage-assign_damage", ASSIGN, ASSIGN_DAMAGE, None, False),
    ("7-distribute-select_targets", DISTRIBUTE, SELECT_TARGETS, None, True),
    ("ok-distribute-distribution", DISTRIBUTE, DISTRIBUTION, None, False),
]


@pytest.mark.parametrize(
    ("action", "pending", "bridge_actions", "stale"),
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
def test_planner_action_stale_vs_bridge(action, pending, bridge_actions, stale):
    engine = AutopilotEngine.__new__(AutopilotEngine)
    state = {"_bridge_request_type": pending[0], "_bridge_request_class": pending[1]}
    if bridge_actions is not None:
        state["_bridge_actions"] = bridge_actions
    assert engine._is_planner_action_stale_vs_bridge(action, state) is stale


# --- what happens to a stale combat step ------------------------------------


@pytest.mark.parametrize("can_pass", [True, False])
def test_a_combat_step_in_a_main_phase_window_passes_toward_combat(can_pass):
    """Declaring attackers while Arena still offers Main 1's actions: pass priority to
    move on to the attack step (the plan comes back there), or skip and re-plan
    when the window has no pass."""
    window = {
        "ok": True,
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "request_class": "ActionsAvailableRequest",
        "actions": [{"actionType": "Pass"}],
        "can_pass": can_pass,
    }
    bridge = FakeBridge(window, connected=True)
    bridge.submit_pass = Mock(return_value=True)
    engine = make_engine(bridge)
    engine._try_gre_bridge = lambda action, state: None  # the attack itself is not submittable here
    state = {
        "turn": {"turn_number": 5, "phase": "Phase_Main1", "step": ""},
        "players": [{"seat_id": 1, "is_local": True}],
        "_bridge_connected": True,
    }

    result = engine._execute_action(
        GameAction(action_type=ActionType.DECLARE_ATTACKERS, attacker_names=["Bear"]), state
    )

    assert result.success
    if can_pass:
        bridge.submit_pass.assert_called_once()
        assert result.error == "GRE bridge (advance-to-combat)"
        assert result.submitted_action.action_type == ActionType.PASS_PRIORITY
    else:
        bridge.submit_pass.assert_not_called()
        assert result.error == "GRE bridge (stale-skip)"


# --- a damage split that reaches a different request ------------------------


def test_assign_damage_against_the_commander_return_is_skipped_for_a_replan():
    """18:18:26: the live request is the OptionalAction; the plan is stale, not a bridge gap."""
    engine = make_engine(FakeBridge(COMMANDER_RETURN_POLL, connected=True))
    engine._pause_for_manual = Mock()
    engine._report_fallback_bug = Mock()

    result = engine._execute_action(ASSIGN, assign_damage_state())

    assert result.success
    assert result.error == "GRE bridge (stale-skip)"
    engine._pause_for_manual.assert_not_called()
    engine._report_fallback_bug.assert_not_called()
