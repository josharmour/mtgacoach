"""#341: a failed trigger selection must not defer the following scry to the watchdog.

9aaa1fee T6: Group 173/236 succeeded, SelectN 178/243 expired during submit,
then Group 181/248 offered the same revealed card 231. Legacy planning kept
the expired ids 337/338 and missed the native group_instance_ids field.
The watchdog eventually answered after about 96 seconds; no timeout loss
or required manual intervention is established by this regression.
"""

import copy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from tests.fakes import TypedBridge, make_engine, planner_with, target_state
from tests.mac_bridge_fakes import FakeGame, adapter_for

from arenamcp.action_planner import ActionType, GameAction
from arenamcp.autopilot import AutopilotState
from arenamcp.decisions import build_pending_decision, submit_option
from arenamcp.gre_bridge import GREBridge

RAW = json.loads((Path(__file__).parent / "fixtures/scry_transition_341.json").read_text())


def poll(index):
    message = RAW[index]["message"]
    group = message.get("groupReq")
    select = message.get("selectNReq")
    result = {
        "has_pending": True,
        "request_type": "Group" if group else "SelectN",
        "request_class": "GroupRequest" if group else "SelectNRequest",
        "game_state_id": message["gameStateId"],
        "msg_id": message["msgId"],
        "can_cancel": False,
    }
    if group:
        result.update(
            group_instance_ids=group["instanceIds"],
            group_specs=group["groupSpecs"],
            group_context=group["context"],
        )
    else:
        result.update(
            select_n_ids=select["ids"],
            select_n_min=select["minSel"],
            select_n_max=select["maxSel"],
            select_n_context=select["context"],
        )
    return copy.deepcopy(result)


class Bridge(TypedBridge):
    def submit_group(self, groups, *, expected_request_id=None):
        self.submitted.append((groups, expected_request_id))
        return True


def engine_for(pending, answer):
    bridge = Bridge(pending)
    planner = planner_with(json.dumps({"option_ids": answer}))
    engine = make_engine(bridge, planner)
    engine._game_plan_mgr = None
    engine._pause_for_manual = Mock()
    return engine, bridge, planner


@pytest.mark.parametrize("idle_gap", [False, True])
def test_expired_trigger_selection_yields_and_next_scry_of_same_card_is_answered(idle_gap):
    engine, bridge, planner = engine_for(poll(2), ["grp:231:top"])
    assert engine._try_typed_decision_path(target_state(), "decision_required") is True
    assert bridge.submitted[-1][1] == (173, 236)
    bridge.poll_resp = poll(3)
    planner._backend.response = '{"option_ids": ["sel:337", "sel:338"]}'

    def expired(ids):
        assert ids == [337, 338]
        bridge.poll_resp = {"has_pending": False} if idle_gap else poll(4)
        return False

    bridge.submit_selection = expired
    assert engine._try_typed_decision_path(target_state(), "combat_attackers") is True
    assert engine.state == AutopilotState.IDLE
    engine._pause_for_manual.assert_not_called()
    assert len(bridge.submitted) == 1
    # No global tracker reset: new request identity permits a second scry of 231.
    bridge.poll_resp = poll(4)
    planner._backend.response = '{"option_ids": ["grp:231:top"]}'
    assert engine._try_typed_decision_path(target_state(), "decision_required") is True
    groups, identity = bridge.submitted[-1]
    assert identity == (181, 248)
    assert groups == [
        {"ids": [231], "zone": "Library", "sub_zone": "Top"},
        {"ids": [], "zone": "Library", "sub_zone": "Bottom"},
    ]
    assert len(bridge.submitted) == 2
    engine._pause_for_manual.assert_not_called()


def test_unchanged_failed_selection_still_allows_existing_recovery():
    engine, bridge, _ = engine_for(poll(3), ["sel:337"])
    bridge.submit_selection = lambda ids: False
    assert engine._try_typed_decision_path(target_state(), "decision_required") is None


def test_failed_selection_with_unreadable_latest_poll_does_not_reuse_stale_plan():
    engine, bridge, _ = engine_for(poll(3), ["sel:337"])

    def fail(ids):
        bridge.get_pending_actions = Mock(side_effect=RuntimeError("transient bridge read"))
        return False

    bridge.submit_selection = fail
    assert engine._try_typed_decision_path(target_state(), "decision_required") is True
    engine._pause_for_manual.assert_not_called()


@pytest.mark.parametrize("position", ["top", "bottom"])
def test_native_scry_legacy_fallback_uses_current_ids_and_spec_order(position):
    pending = poll(4)
    pending["group_specs"].reverse()
    engine, bridge, _ = engine_for(pending, [])
    action = GameAction(action_type=ActionType.SELECT_N, scry_position=position)
    assert engine._try_gre_bridge_scry(action, target_state()).success
    groups, identity = bridge.submitted[-1]
    assert identity == (181, 248)
    assert groups[0]["sub_zone"] == "Bottom"
    assert groups[1]["sub_zone"] == "Top"
    assert next(g for g in groups if g["sub_zone"].lower() == position)["ids"] == [231]


@pytest.mark.parametrize("names", [["Option 337", "Option 338"], []])
def test_stale_select_n_names_or_absent_intent_never_bottom_the_new_scry(names):
    engine, bridge, _ = engine_for(poll(4), [])
    action = GameAction(action_type=ActionType.SELECT_N, select_card_names=names)
    assert engine._try_gre_bridge_scry(action, target_state()) is None
    assert not bridge.submitted


def test_scry_bottom_intent_cannot_be_applied_to_surveil_destination():
    pending = poll(4)
    pending["group_specs"][1] = {"upperBound": 1, "zoneType": "Graveyard"}
    pending["group_context"] = "Surveil"
    engine, bridge, _ = engine_for(pending, [])
    action = GameAction(action_type=ActionType.SELECT_N, scry_position="bottom")
    assert engine._try_gre_bridge_scry(action, target_state()) is None
    assert not bridge.submitted


def test_named_revealed_card_uses_only_current_ids_even_if_repeated_in_snapshot():
    engine, bridge, _ = engine_for(poll(4), [])
    card = {"instance_id": 231, "name": "Warleader's Call"}
    state = {"scry_cards": [card], "revealed": [card]}
    action = GameAction(action_type=ActionType.SELECT_N, select_card_names=["Warleader's Call"])
    assert engine._try_gre_bridge_scry(action, state).success
    assert bridge.submitted[-1][0][0]["ids"] == [231]


@pytest.mark.parametrize("current", [False, True])
def test_group_submit_rejects_next_request_even_when_revealed_card_is_identical(current):
    decision = build_pending_decision(poll(2))
    gsid, msg = decision.request_id if current else (181, 248)
    game = FakeGame({"$c": "GreClient.Rules.GroupRequest", "$h": 5}, gsid=gsid, msg=msg)
    bridge = GREBridge()
    bridge._send_safe = adapter_for(game).handle
    assert submit_option(bridge, decision, ["grp:231:top"]) is current
    assert bool(game.submits()) is current
