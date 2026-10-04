"""Replay the token-label/combined-attacker failures without live game actions."""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlanner, ActionType, GameAction
from arenamcp.autopilot_bridge import _BridgeSubmitMixin
from arenamcp.combat_identity import combat_identity_prompt, resolve_combatant

HOBBIT = "The Notary Hobbits"
PLAYER = {"type": "Player", "playerSystemSeatId": 2}


def board():
    return {
        "local_seat_id": 1,
        "players": [{"seat_id": 1, "is_local": True}, {"seat_id": 2, "is_local": False}],
        "battlefield": [
            {
                "name": HOBBIT,
                "instance_id": iid,
                "owner_seat_id": 1,
                "controller_seat_id": 1,
                "type_line": "Creature — Halfling",
                "power": 1,
                "toughness": 1,
                "object_kind": "CARD" if iid == 955 else "TOKEN",
            }
            for iid in (955, 1162, 1171)
        ]
        + [{"name": "Belladonna Took", "instance_id": 500, "controller_seat_id": 2}],
    }


def pilot(state, pending):
    engine = _BridgeSubmitMixin()
    engine._get_game_state = lambda: state
    engine._gre_bridge = Mock()
    engine._gre_bridge.get_pending_actions.return_value = pending
    engine._gre_bridge.submit_blockers.return_value = True
    engine._gre_bridge.submit_attackers_raw.return_value = {"ok": True}
    engine._attack_override = Mock(return_value=None)
    engine._log_execution_path = Mock()
    engine._gre_bridge_failed_methods = set()
    return engine


def parse(action, menu, state, context):
    planner = ActionPlanner.__new__(ActionPlanner)
    planner._last_menu = menu
    return planner._parse_response(json.dumps(action), menu, context, game_state=state)


def test_live_token_label_binds_board_copy_even_when_only_one_is_eligible(monkeypatch):
    monkeypatch.setattr("arenamcp.autopilot_bridge.time.sleep", lambda _: None)
    state = board()
    blockers = [{"blockerInstanceId": 1162, "attackerInstanceIds": [500]}]
    context = {"type": "declare_blockers", "raw_blockers": blockers}
    plan = parse(
        {"action_type": "declare_blockers", "blocker_assignments": {f"*{HOBBIT} #2": "Belladonna Took"}},
        [f"Block with: {HOBBIT}", "Done (confirm blockers)"],
        state,
        context,
    )
    assert len(plan.actions) == 1
    assert plan.actions[0].blocker_instance_assignments == {1162: 500}
    engine = pilot(
        state, {"has_pending": True, "request_class": "DeclareBlockersRequest", "blockers": blockers}
    )
    engine._gre_bridge.get_pending_actions.side_effect = [
        engine._gre_bridge.get_pending_actions.return_value,
        {"has_pending": False},
    ]
    assert engine._try_gre_bridge_blockers(plan.actions[0]).success
    engine._gre_bridge.submit_blockers.assert_called_once_with(
        [{"blockerInstanceId": 1162, "attackerInstanceIds": [500]}]
    )


def test_conflicting_board_and_menu_numbers_require_an_id():
    state = board()
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_combatant(f"{HOBBIT} #1", state, [1162, 1171], local_side=True)
    assert resolve_combatant(f"{HOBBIT} [id:1162]", state, [1162, 1171], local_side=True) == 1162


def test_tapped_board_copy_cannot_be_replaced_with_another_eligible_copy():
    with pytest.raises(ValueError):
        resolve_combatant(f"*{HOBBIT} #2", board(), [1171], local_side=True)


def test_duplicate_attacker_target_names_are_not_resolved_by_first_match():
    state = board()
    state["battlefield"].append({"name": "Belladonna Took", "instance_id": 501, "controller_seat_id": 2})
    with pytest.raises(ValueError):
        resolve_combatant("Belladonna Took", state, [500, 501], local_side=False)


@pytest.mark.parametrize("name", [HOBBIT, "Hei Bai, Forest Guardian"])
def test_ordinal_summary_parses_three_names_without_splitting_card_commas(name):
    planner = ActionPlanner.__new__(ActionPlanner)
    labels = [f"{name} #{i}" for i in (1, 2, 3)]
    action = planner._legal_action_to_action("Declare Attackers: " + ", ".join(labels))
    assert action.attacker_names == labels
    assert planner._split_creature_list("Hei Bai, Forest Guardian") == ["Hei Bai, Forest Guardian"]


@pytest.mark.parametrize("synthetic", [False, True])
def test_three_hobbits_pipeline_submits_three_distinct_ids(synthetic):
    state = board()
    raw = [{"attackerInstanceId": iid, "legalDamageRecipients": [PLAYER]} for iid in (955, 1162, 1171)]
    context = {"type": "declare_attackers", "raw_attackers": raw}
    state["decision_context"] = context
    labels = (
        [f"Creature {iid}" for iid in (955, 1162, 1171)]
        if synthetic
        else [f"{HOBBIT} #{i}" for i in (1, 2, 3)]
    )
    plan = parse(
        {"action_type": "declare_attackers", "attacker_names": labels, "target_names": ["Opponent"]},
        [f"Attack with: {HOBBIT} #{i}" for i in (1, 2, 3)],
        state,
        context,
    )
    assert len(plan.actions) == 1
    assert plan.actions[0].attacker_instance_ids == [955, 1162, 1171]
    engine = pilot(state, {"has_pending": True, "request_class": "DeclareAttackerRequest", "attackers": raw})
    assert engine._try_bridge_declare_attackers(plan.actions[0]).success
    submitted = engine._gre_bridge.submit_attackers_raw.call_args.args[0]
    assert [a["attackerInstanceId"] for a in submitted] == [955, 1162, 1171]


def test_stale_bound_attackers_are_rejected_instead_of_submitting_empty():
    engine = pilot(board(), {"has_pending": True, "request_class": "DeclareAttackerRequest", "attackers": []})
    action = GameAction(
        ActionType.DECLARE_ATTACKERS, attacker_names=["Creature 955"], attacker_instance_ids=[955]
    )
    assert engine._try_bridge_declare_attackers(action) is None
    engine._gre_bridge.submit_attackers_raw.assert_not_called()


def test_failed_color_choice_never_falls_back_to_blind_attack_all():
    planner = ActionPlanner.__new__(ActionPlanner)
    menu = ["Declare Attackers: " + ", ".join(f"{HOBBIT} #{i}" for i in (1, 2, 3)), f"Activate {HOBBIT} #1"]
    assert planner._fallback_plan('{"action_type":"modal_choice","modal_index":4}', menu).actions == []


@pytest.mark.parametrize("ack", ["same_request", "wrong_selection", "unreadable", "accepted"])
def test_native_attack_finalize_requires_server_acknowledgment(monkeypatch, ack):
    monkeypatch.setattr("arenamcp.autopilot_bridge.time.sleep", lambda _: None)
    raw = [{"attackerInstanceId": 955, "legalDamageRecipients": [PLAYER], "selectedDamageRecipient": None}]
    pending = {
        "has_pending": True,
        "request_class": "DeclareAttackerRequest",
        "bridge_runtime": "il2cpp-macos",
        "game_state_id": 10,
        "msg_id": 20,
        "attackers": raw,
    }
    refreshed = deepcopy(pending)
    refreshed.update(
        can_submit=True,
        game_state_id=11 if ack != "same_request" else 10,
        msg_id=21 if ack != "same_request" else 20,
    )
    if ack != "wrong_selection":
        refreshed["attackers"][0]["selectedDamageRecipient"] = PLAYER
    if ack == "unreadable":
        refreshed["attackers"][0]["selectedDamageRecipient"] = {"type": ""}
    engine = pilot(board(), pending)
    engine._gre_bridge.get_pending_actions.side_effect = [pending, refreshed]
    engine._gre_bridge.submit_attackers_raw.side_effect = [{"ok": True, "needs_finalize": True}, {"ok": True}]
    action = GameAction(
        ActionType.DECLARE_ATTACKERS, attacker_names=["Creature 955"], target_names=["Opponent"]
    )
    result = engine._try_bridge_declare_attackers(action)
    assert engine._gre_bridge.submit_attackers_raw.call_count == (2 if ack == "accepted" else 1)
    assert bool(result and result.success) == (ack == "accepted")


@pytest.mark.parametrize("accepted", [True, False])
def test_mixed_player_planeswalker_attack_waits_for_exact_acknowledgment(monkeypatch, accepted):
    sleeps = []
    monkeypatch.setattr("arenamcp.autopilot_bridge.time.sleep", sleeps.append)
    walker = {"type": "PlanesWalker", "planeswalkerInstanceId": 1022}
    state = board()
    state["battlefield"] = [
        {"instance_id": 693, "name": "Vaultborn Tyrant", "controller_seat_id": 1, "power": 6},
        {"instance_id": 803, "name": "Chomping Changeling", "controller_seat_id": 1, "power": 1},
        {"instance_id": 1022, "name": "Teferi, Hero of Dominaria", "controller_seat_id": 2},
    ]
    pending = {
        "has_pending": True,
        "request_class": "DeclareAttackerRequest",
        "bridge_runtime": "il2cpp-macos",
        "game_state_id": 467,
        "msg_id": 663,
        "can_submit": True,
        "attackers": [
            {
                "attackerInstanceId": iid,
                "legalDamageRecipients": [PLAYER, walker],
                "selectedDamageRecipient": None,
            }
            for iid in (693, 803)
        ],
    }
    acknowledgment = deepcopy(pending)
    acknowledgment.update(game_state_id=468, msg_id=665)
    acknowledgment["attackers"][0]["selectedDamageRecipient"] = PLAYER
    acknowledgment["attackers"][1]["selectedDamageRecipient"] = walker if accepted else PLAYER
    engine = pilot(state, pending)
    engine._gre_bridge.get_pending_actions.side_effect = [pending, pending] + [acknowledgment] * 5
    engine._gre_bridge.submit_attackers_raw.side_effect = [{"ok": True, "needs_finalize": True}, {"ok": True}]
    action = GameAction(
        ActionType.DECLARE_ATTACKERS,
        attacker_names=["Vaultborn Tyrant", "Chomping Changeling"],
        attacker_instance_ids=[693, 803],
        attacker_targets={
            "Vaultborn Tyrant": "Opponent",
            "Chomping Changeling": "Teferi, Hero of Dominaria [1022]",
        },
    )
    result = engine._try_bridge_declare_attackers(action)
    assert bool(result and result.success) is accepted
    assert engine._gre_bridge.submit_attackers_raw.call_count == (2 if accepted else 1)
    assert sum(sleeps) <= 1.5
    if accepted:
        confirmed = engine._gre_bridge.submit_attackers_raw.call_args
        assert confirmed.kwargs == {"expected_request_id": (468, 665), "finalize_only": True}
        assert confirmed.args[0] == [
            {"attackerInstanceId": 693, "damageRecipient": PLAYER},
            {"attackerInstanceId": 803, "damageRecipient": walker},
        ]


def test_prompt_exposes_instance_ids_for_eligible_blocks():
    text = combat_identity_prompt(
        board(), {"raw_blockers": [{"blockerInstanceId": 1162, "attackerInstanceIds": [500]}]}
    )
    assert f"*{HOBBIT} [id:1162]" in text
    assert "Belladonna Took [id:500]" in text
