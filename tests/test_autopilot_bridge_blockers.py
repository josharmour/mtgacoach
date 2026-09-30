from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from arenamcp.action_planner import ActionType, GameAction
from arenamcp.autopilot_bridge import _BridgeSubmitMixin


@pytest.fixture(params=["The Notary Hobbits", "Soldier"])
def combat(monkeypatch, request):
    monkeypatch.setattr("arenamcp.autopilot_bridge.time.sleep", lambda seconds: None)
    blocker_name = request.param
    battlefield = [
        {"instance_id": 553, "name": "Frodo, Adventurous Hobbit"},
        {"instance_id": 683, "name": "Eldrazi Spawn"},
        *[{"instance_id": instance_id, "name": blocker_name} for instance_id in (714, 721, 722)],
    ]
    pending = {
        "has_pending": True,
        "request_class": "DeclareBlockersRequest",
        "bridge_runtime": "il2cpp-macos",
        "game_state_id": 269,
        "msg_id": 418,
        "blockers": [
            {
                "blockerInstanceId": instance_id,
                "attackerInstanceIds": [706, 669, 553, 710, 558],
                "selectedAttackerInstanceIds": [],
            }
            for instance_id in (683, 714, 721, 722)
        ],
    }
    bridge = SimpleNamespace(
        get_pending_actions=Mock(side_effect=[pending, {"has_pending": False}]),
        submit_blockers=Mock(return_value=True),
    )
    engine = SimpleNamespace(
        _gre_bridge=bridge,
        _get_game_state=lambda: {"battlefield": battlefield},
        _log_execution_path=Mock(),
        _gre_bridge_failed_methods=set(),
    )
    action = GameAction(
        action_type=ActionType.DECLARE_BLOCKERS,
        blocker_assignments={
            f"{blocker_name} #{ordinal}": "Frodo, Adventurous Hobbit" for ordinal in (1, 2, 3)
        },
    )
    return engine, action, pending


def test_same_named_creatures_submit_distinct_blockers(combat):
    engine, action, _pending = combat

    result = _BridgeSubmitMixin._try_gre_bridge_blockers(engine, action)

    assert result.success
    engine._gre_bridge.submit_blockers.assert_called_once_with(
        [{"blockerInstanceId": instance_id, "attackerInstanceIds": [553]} for instance_id in (714, 721, 722)]
    )


@pytest.mark.parametrize("selected_ids", [(), (714,), (714, 721, 722)])
def test_finalize_requires_all_intended_blocks_to_be_accepted(combat, selected_ids):
    engine, action, pending = combat
    refreshed = deepcopy(pending)
    refreshed["game_state_id"] += 1
    refreshed["msg_id"] += 1
    for blocker in refreshed["blockers"]:
        if blocker["blockerInstanceId"] in selected_ids:
            blocker["selectedAttackerInstanceIds"] = [553]
    engine._gre_bridge.get_pending_actions.side_effect = [pending, refreshed]

    result = _BridgeSubmitMixin._try_gre_bridge_blockers(engine, action)

    if len(selected_ids) == 3:
        assert result.success
        assert engine._gre_bridge.submit_blockers.call_count == 2
        assert engine._gre_bridge.submit_blockers.call_args == call([])
    else:
        assert result is None
        assert engine._gre_bridge.submit_blockers.call_count == 1


@pytest.mark.parametrize("suffix", ["", " #4"])
def test_ambiguous_or_missing_copy_never_submits(combat, suffix):
    engine, action, _pending = combat
    name = next(iter(action.blocker_assignments)).rsplit(" #", 1)[0] + suffix
    action.blocker_assignments = {name: "Frodo, Adventurous Hobbit"}

    assert _BridgeSubmitMixin._try_gre_bridge_blockers(engine, action) is None
    engine._gre_bridge.submit_blockers.assert_not_called()


def test_missing_legal_blockers_does_not_convert_plan_to_no_blocks(combat):
    engine, action, pending = combat
    pending["blockers"] = []

    assert _BridgeSubmitMixin._try_gre_bridge_blockers(engine, action) is None
    engine._gre_bridge.submit_blockers.assert_not_called()


def test_intentional_no_blocks_still_submits(combat):
    engine, action, _pending = combat
    action.blocker_assignments = {}

    assert _BridgeSubmitMixin._try_gre_bridge_blockers(engine, action).success
    engine._gre_bridge.submit_blockers.assert_called_once_with([])


def test_two_aliases_cannot_assign_the_same_blocker_twice(combat):
    engine, action, _pending = combat
    action.blocker_assignments = {
        "Eldrazi Spawn": "Frodo, Adventurous Hobbit",
        "Spawn": "Frodo, Adventurous Hobbit",
    }

    assert _BridgeSubmitMixin._try_gre_bridge_blockers(engine, action) is None
    engine._gre_bridge.submit_blockers.assert_not_called()


def test_failed_confirmation_is_not_reported_as_success(combat):
    engine, action, pending = combat
    refreshed = deepcopy(pending)
    refreshed["game_state_id"] += 1
    refreshed["msg_id"] += 1
    for blocker in refreshed["blockers"]:
        if blocker["blockerInstanceId"] != 683:
            blocker["selectedAttackerInstanceIds"] = [553]
    engine._gre_bridge.get_pending_actions.side_effect = [pending, refreshed]
    engine._gre_bridge.submit_blockers.side_effect = [True, False]

    assert _BridgeSubmitMixin._try_gre_bridge_blockers(engine, action) is None
    engine._log_execution_path.assert_not_called()


def test_local_selection_without_server_acknowledgment_does_not_finalize(combat):
    engine, action, pending = combat
    local_selection = deepcopy(pending)
    for blocker in local_selection["blockers"]:
        if blocker["blockerInstanceId"] != 683:
            blocker["selectedAttackerInstanceIds"] = [553]
    engine._gre_bridge.get_pending_actions.side_effect = [pending, local_selection]

    assert _BridgeSubmitMixin._try_gre_bridge_blockers(engine, action) is None
    assert engine._gre_bridge.submit_blockers.call_count == 1
