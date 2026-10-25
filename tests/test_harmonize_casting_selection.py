"""Nature's Rhythm Harmonize: bug_20261025_155111, CTO Selection after X=8."""

from unittest.mock import Mock

import pytest
from tests.mac_bridge_fakes import FakeGame, adapter_for, enum, listing, submitted_uint_values

from arenamcp.decisions import build_pending_decision, submit_option
from arenamcp.gre_bridge import GREBridge
from arenamcp.request_tracker import decision_fingerprint


def request():
    # Field layout verified against the live Arena IL2CPP classes; IDs and
    # bounds from castingTimeOptionReq in the user's report.
    return {
        "$c": "GreClient.Rules.CastingTimeOptionsRequest",
        "$h": 5,
        "ChildRequests": listing(
            {
                "$c": "GreClient.Rules.CastingTimeOption_SelectNRequest",
                "$h": 60,
                "GrpId": 188856,
                "AffectedId": 1168,
                "IsRequired": True,
                "SelectNRequest": {
                    "$c": "GreClient.Rules.SelectNRequest",
                    "$h": 61,
                    "MinSel": 0,
                    "MaxSel": 1,
                    "IdType": enum("IdType_InstanceId", 1),
                    "Ids": listing(970, 998, 999, 1021, 1022),
                    "MinWeight": -(2**31),
                    "MaxWeight": 2**31 - 1,
                },
            }
        ),
    }


def inner(req):
    return req["ChildRequests"]["$items"][0]["SelectNRequest"]


def setup(req=None):
    game = FakeGame(req or request(), {"CanCancel": True})
    adapter = adapter_for(game)
    poll = adapter.handle({"action": "get_pending_actions"})
    return game, adapter, poll


def command(poll, index):
    action = poll["actions"][index]
    return {
        "action": "submit_action",
        "action_index": index,
        "expected_game_state_id": poll["game_state_id"],
        "expected_msg_id": poll["msg_id"],
        "expected_child_index": action["childIndex"],
        "expected_choice_kind": "selection",
        "expected_option_index": action["optionIndex"],
        "expected_selection_ids": action["selectionIds"],
    }


@pytest.mark.parametrize("index,ids", [(0, []), (1, [970]), (5, [1022])])
def test_harmonize_choices_submit_to_nested_select_n(index, ids):
    game, adapter, poll = setup()
    assert len(poll["actions"]) == 6
    decision = build_pending_decision(poll, resolve_instance=lambda iid: "The Notary Hobbits")
    assert decision.request_type == "CastingTimeOptions"
    assert decision.selection_is_valid([f"idx:{index}"])
    assert "creature" in decision.find("idx:0").label
    assert "The Notary Hobbits [id:970]" in decision.find("idx:1").label
    result = adapter.handle(command(poll, index))
    assert result["ok"]
    ops = game.submits()[-1]
    assert ops[-1]["method"] == "SubmitSelection"
    assert ops[-1]["target"] == {"h": 61}
    assert submitted_uint_values(ops) == ids


def test_no_empty_choice_if_creature_required_and_no_incomplete_menu():
    req = request()
    inner(req)["MinSel"] = 1
    _, _, poll = setup(req)
    assert len(poll["actions"]) == 5
    assert all(a["selectionIds"] for a in poll["actions"])
    for change in ({"MaxSel": 2}, {"Ids": {"$items": [970], "$n": 5}}, {"IdType": enum("IdType_GrpId", 2)}):
        req = request()
        inner(req).update(change)
        assert setup(req)[2]["actions"] == []


def test_reordered_candidates_and_missing_identity_are_rejected():
    req = request()
    game, adapter, poll = setup(req)
    cmd = command(poll, 1)
    inner(req)["Ids"] = listing(998, 970, 999, 1021, 1022)
    assert not adapter.handle(cmd)["ok"]
    cmd.pop("expected_selection_ids")
    assert not adapter.handle(cmd)["ok"]
    assert game.submits() == []
    newer = setup(req)[2]
    assert decision_fingerprint(build_pending_decision(poll)) != decision_fingerprint(
        build_pending_decision(newer)
    )


def test_full_typed_bridge_transport_preserves_empty_and_selected_id_identity():
    for index in (0, 2):
        game, adapter, poll = setup()
        bridge = GREBridge.__new__(GREBridge)
        bridge._send_safe = Mock(side_effect=lambda cmd, adapter=adapter, **kw: adapter.handle(cmd))
        decision = build_pending_decision(poll)
        assert submit_option(bridge, decision, [f"idx:{index}"])
        assert (
            bridge._send_safe.call_args.args[0]["expected_selection_ids"]
            == poll["actions"][index]["selectionIds"]
        )
        assert len(game.submits()) == 1
