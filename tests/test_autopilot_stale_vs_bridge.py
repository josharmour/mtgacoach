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

A step against the request that takes it is never stale: a failure there is
a real bridge problem and must still surface MANUAL REQUIRED.
"""

from __future__ import annotations

import pytest

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
