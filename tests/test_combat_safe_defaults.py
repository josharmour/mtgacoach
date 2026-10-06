"""Combat safe defaults and the chump-block guard (2026-10-06 field reports).

- 15:23:16: Declare Blockers with a legal no-block and a legal block went
  MANUAL REQUIRED because the planner returned no actions and the safe default
  refused every DeclareBlockers request.
- 13:55:57 (match 3da54de9 G2 T10): at 20 life Mindseeker Oculus (2/1)
  chump-blocked a trampling 4/4 Beast, saving one damage for a creature.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

import arenamcp.autopilot_bridge as autopilot_bridge
from arenamcp.action_planner import ActionType, GameAction
from arenamcp.autopilot import AutopilotEngine
from arenamcp.autopilot_progress import DecisionProgressGuard
from arenamcp.combat_strategy import safe_default_blocks, wasteful_chump_blocks

LOCAL, OPPONENT = 2, 1


def creature(iid, name, power, toughness, seat, **extra):
    return {
        "instance_id": iid,
        "name": name,
        "type_line": "Creature",
        "power": power,
        "toughness": toughness,
        "controller_seat_id": seat,
        **extra,
    }


def board(life, battlefield, blockers, hand=None):
    return {
        "players": [
            {"seat_id": LOCAL, "is_local": True, "life_total": life},
            {"seat_id": OPPONENT, "is_local": False, "life_total": 20},
        ],
        "local_seat_id": LOCAL,
        "turn": {
            "turn_number": 10,
            "active_player": OPPONENT,
            "phase": "Phase_Combat",
            "step": "Step_DeclareBlock",
        },
        "battlefield": battlefield,
        "hand": hand or [],
        "decision_context": {
            "type": "declare_blockers",
            "raw_blockers": blockers,
            "legal_blocker_ids": [b["blockerInstanceId"] for b in blockers],
        },
        "_bridge_request_type": "DeclareBlockers",
        "_bridge_request_class": "DeclareBlockersRequest",
        "_bridge_can_pass": False,
    }


BEAST = creature(348, "Beast", 4, 4, OPPONENT, is_attacking=True, oracle_text="Trample", keywords=["trample"])
OCULUS = creature(
    355,
    "Mindseeker Oculus",
    2,
    1,
    LOCAL,
    oracle_text="When this creature enters, empower Jace 4. (Put four loyalty counters on a Jace token.)",
)
OCULUS_BLOCKS = [{"blockerInstanceId": 355, "attackerInstanceIds": [348], "maxAttackers": 1}]


# --- chump-block guard ----------------------------------------------------------


def test_trample_chump_at_twenty_life_is_dropped():
    state = board(20, [BEAST, OCULUS], OCULUS_BLOCKS)
    dropped = wasteful_chump_blocks(state, {355: 348})
    assert set(dropped) == {355}
    assert "save 1 damage" in dropped[355]


def test_chump_that_prevents_lethal_or_near_lethal_is_kept():
    assert wasteful_chump_blocks(board(4, [BEAST, OCULUS], OCULUS_BLOCKS), {355: 348}) == {}
    # 9 life: 4 trample leaves 5 (near-lethal) without the block.
    assert wasteful_chump_blocks(board(9, [BEAST, OCULUS], OCULUS_BLOCKS), {355: 348}) == {}


def test_blocks_with_value_are_never_dropped():
    state = board(20, [BEAST, OCULUS], OCULUS_BLOCKS)
    deathtouch = {**OCULUS, "oracle_text": "Deathtouch", "keywords": ["deathtouch"]}
    state["battlefield"] = [BEAST, deathtouch]
    assert wasteful_chump_blocks(state, {355: 348}) == {}  # the Beast dies
    wall = creature(355, "Wall", 0, 6, LOCAL)
    state["battlefield"] = [BEAST, wall]
    assert wasteful_chump_blocks(state, {355: 348}) == {}  # the blocker survives
    trader = creature(355, "Trader", 4, 4, LOCAL)
    state["battlefield"] = [BEAST, trader]
    assert wasteful_chump_blocks(state, {355: 348}) == {}  # a trade
    dies_payoff = {**OCULUS, "oracle_text": "When this creature dies, draw a card."}
    state["battlefield"] = [BEAST, dies_payoff]
    assert wasteful_chump_blocks(state, {355: 348}) == {}


def test_affordable_pump_trick_keeps_the_block():
    island = {
        "instance_id": 900,
        "name": "Island",
        "type_line": "Basic Land — Island",
        "controller_seat_id": LOCAL,
    }
    trick = {
        "instance_id": 901,
        "name": "Giant Growth",
        "type_line": "Instant",
        "mana_cost": "{G}",
        "oracle_text": "Target creature gets +3/+3 until end of turn.",
    }
    state = board(20, [BEAST, OCULUS, island], OCULUS_BLOCKS, hand=[trick])
    assert wasteful_chump_blocks(state, {355: 348}) == {}


def test_unknown_power_keeps_the_block():
    unknown = {**BEAST, "power": None}
    assert wasteful_chump_blocks(board(20, [unknown, OCULUS], OCULUS_BLOCKS), {355: 348}) == {}


def jace(loyalty):
    return {
        "instance_id": 360,
        "name": "Jace",
        "type_line": "Legendary Planeswalker — Jace",
        "controller_seat_id": LOCAL,
        "loyalty": loyalty,
    }


def test_chump_that_may_save_a_planeswalker_is_kept():
    # The incident: Jace at 1 loyalty dies to the 3 trample damage anyway.
    assert set(wasteful_chump_blocks(board(20, [BEAST, OCULUS, jace(1)], OCULUS_BLOCKS), {355: 348})) == {355}
    # At 4 loyalty the block (3 through) is what keeps an attacked Jace alive.
    assert wasteful_chump_blocks(board(20, [BEAST, OCULUS, jace(4)], OCULUS_BLOCKS), {355: 348}) == {}
    assert wasteful_chump_blocks(board(20, [BEAST, OCULUS, jace(None)], OCULUS_BLOCKS), {355: 348}) == {}
    at_jace = {**BEAST, "attack_target_id": 360}
    assert wasteful_chump_blocks(board(20, [at_jace, OCULUS, jace(1)], OCULUS_BLOCKS), {355: 348}) == {}
    at_us = {**BEAST, "attack_target_id": LOCAL}
    assert set(wasteful_chump_blocks(board(20, [at_us, OCULUS, jace(4)], OCULUS_BLOCKS), {355: 348})) == {355}


# --- safe default declaration -------------------------------------------------------


def test_safe_default_declares_no_blocks_when_not_threatened():
    assignments, reason = safe_default_blocks(board(20, [BEAST, OCULUS], OCULUS_BLOCKS))
    assert assignments == {}
    assert "no blocks" in reason


def test_safe_default_blocks_to_survive_lethal():
    attackers = [creature(299 + i, f"Raider {i}", 3, 3, OPPONENT, is_attacking=True) for i in range(2)]
    blocker = creature(322, "Dark Matter Manipulator", 1, 2, LOCAL)
    blocks = [{"blockerInstanceId": 322, "attackerInstanceIds": [299, 300]}]
    assignments, reason = safe_default_blocks(board(5, [*attackers, blocker], blocks))
    assert list(assignments) == [322]
    assert "3 damage at 5 life" in reason


def test_safe_default_takes_a_clearly_good_block():
    attacker = creature(299, "Void Extrapolator", 2, 2, OPPONENT, is_attacking=True)
    blocker = creature(322, "Big Wall", 3, 5, LOCAL)
    blocks = [{"blockerInstanceId": 322, "attackerInstanceIds": [299]}]
    assignments, _ = safe_default_blocks(board(20, [attacker, blocker], blocks))
    assert assignments == {322: 299}


def test_safe_default_refuses_an_unverifiable_board():
    assert safe_default_blocks({"decision_context": {"type": "declare_blockers"}}) is None
    unknown = {**BEAST, "power": None}
    assert safe_default_blocks(board(20, [unknown, OCULUS], OCULUS_BLOCKS)) is None


def test_safe_default_with_no_legal_block_declares_none():
    assignments, _ = safe_default_blocks(board(20, [BEAST, OCULUS], [{"blockerInstanceId": 355}]))
    assert assignments == {}


# --- engine wiring ------------------------------------------------------------------


class FakeBridge:
    def __init__(self, pending):
        self.connected = True
        self.pending = pending
        self.blocker_calls = []
        self.attacker_calls = []

    def connect(self):
        return True

    def get_pending_actions(self):
        return self.pending

    def submit_blockers(self, assignments):
        self.blocker_calls.append(assignments)
        return True

    def submit_attackers_raw(self, entries, **kwargs):
        self.attacker_calls.append(entries)
        return {"ok": True}

    def auto_respond(self):
        pytest.fail("combat requests must never be auto-responded")


def engine_for(bridge, state):
    engine = AutopilotEngine.__new__(AutopilotEngine)
    engine._gre_bridge = bridge
    engine._abort_event = threading.Event()
    engine._progress_guard = DecisionProgressGuard()
    engine._progress_game_state = {}
    engine._progress_last_poll = None
    engine._config = MagicMock(dry_run=False)
    engine._path_stats = {}
    engine._gre_bridge_failed_methods = set()
    engine._advice_recorder = None
    engine._get_game_state = lambda: state
    return engine


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(autopilot_bridge.time, "sleep", lambda seconds: None)


def blockers_pending(blocks):
    return {
        "has_pending": True,
        "request_type": "DeclareBlockers",
        "request_class": "DeclareBlockersRequest",
        "blockers": blocks,
    }


def test_planner_failure_on_blockers_submits_a_checked_no_block():
    state = board(20, [BEAST, OCULUS], OCULUS_BLOCKS)
    bridge = FakeBridge(blockers_pending(OCULUS_BLOCKS))
    engine = engine_for(bridge, state)
    assert engine._try_interactive_safe_default(state, "decision_required") is True
    assert bridge.blocker_calls == [[]]


def test_planner_failure_on_lethal_blockers_submits_the_survival_block():
    attackers = [creature(299 + i, f"Raider {i}", 3, 3, OPPONENT, is_attacking=True) for i in range(2)]
    blocker = creature(322, "Dark Matter Manipulator", 1, 2, LOCAL)
    blocks = [{"blockerInstanceId": 322, "attackerInstanceIds": [299, 300]}]
    state = board(5, [*attackers, blocker], blocks)
    bridge = FakeBridge(blockers_pending(blocks))
    engine = engine_for(bridge, state)
    assert engine._try_interactive_safe_default(state, "decision_required") is True
    assert bridge.blocker_calls[0] == [{"blockerInstanceId": 322, "attackerInstanceIds": [299]}]


def test_unverifiable_blockers_still_wait_for_the_user():
    bridge = FakeBridge(blockers_pending([]))
    engine = engine_for(bridge, {})
    assert engine._try_interactive_safe_default({"_bridge_request_type": "DeclareBlockers"}, "x") is False
    assert bridge.blocker_calls == []


def test_bridge_blocker_submission_drops_the_trample_chump():
    state = board(20, [BEAST, OCULUS], OCULUS_BLOCKS)
    bridge = FakeBridge(blockers_pending(OCULUS_BLOCKS))
    engine = engine_for(bridge, state)
    action = GameAction(
        ActionType.DECLARE_BLOCKERS,
        blocker_assignments={"Mindseeker Oculus [id:355]": "*Beast [id:348]"},
        blocker_instance_assignments={355: 348},
    )
    result = engine._try_gre_bridge_blockers(action)
    assert result.success
    assert bridge.blocker_calls == [[]]
    assert result.submitted_action.blocker_assignments == {}


def test_bridge_blocker_submission_keeps_a_lethal_saving_chump():
    state = board(4, [BEAST, OCULUS], OCULUS_BLOCKS)
    bridge = FakeBridge(blockers_pending(OCULUS_BLOCKS))
    engine = engine_for(bridge, state)
    action = GameAction(
        ActionType.DECLARE_BLOCKERS,
        blocker_assignments={"Mindseeker Oculus [id:355]": "*Beast [id:348]"},
        blocker_instance_assignments={355: 348},
    )
    assert engine._try_gre_bridge_blockers(action).success
    assert bridge.blocker_calls[0] == [{"blockerInstanceId": 355, "attackerInstanceIds": [348]}]


def test_planner_failure_on_attackers_declares_no_attackers():
    pending = {
        "has_pending": True,
        "request_type": "DeclareAttackers",
        "request_class": "DeclareAttackerRequest",
        "attackers": [
            {
                "attackerInstanceId": 322,
                "legalDamageRecipients": [{"type": "DamageRecType_Player", "playerSystemSeatId": OPPONENT}],
            }
        ],
    }
    state = {
        "_bridge_request_type": "DeclareAttackers",
        "_bridge_request_class": "DeclareAttackerRequest",
        "decision_context": {"type": "declare_attackers"},
    }
    bridge = FakeBridge(pending)
    engine = engine_for(bridge, state)
    assert engine._try_interactive_safe_default(state, "decision_required") is True
    assert bridge.attacker_calls == [[]]


def test_attack_safe_default_never_confirms_a_pending_selection():
    pending = {
        "has_pending": True,
        "request_type": "DeclareAttackers",
        "request_class": "DeclareAttackerRequest",
        "attackers": [
            {
                "attackerInstanceId": 322,
                "legalDamageRecipients": [{"type": "DamageRecType_Player", "playerSystemSeatId": OPPONENT}],
                "selectedDamageRecipient": {"type": "DamageRecType_Player", "playerSystemSeatId": OPPONENT},
            }
        ],
    }
    state = {"_bridge_request_type": "DeclareAttackers", "decision_context": {"type": "declare_attackers"}}
    bridge = FakeBridge(pending)
    engine = engine_for(bridge, state)
    assert engine._try_interactive_safe_default(state, "decision_required") is False
    assert bridge.attacker_calls == []
