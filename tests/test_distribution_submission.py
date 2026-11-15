"""Forgotten Ancient stalled at DistributionReq in bug_20261104_200450."""

from unittest.mock import Mock

import pytest
from tests.fakes import FakeBridge, make_engine
from tests.mac_bridge_fakes import MSG, FakeGame, adapter_for, listing


def request(**changes):
    return {
        "$c": "GreClient.Rules.DistributionRequest",
        "$h": 5,
        "Min": 0,
        "Max": 2,
        "MinPer": 0,
        "MaxPer": 2,
        "TargetIds": listing(745, 752, 788),
        "LegalTargetIds": listing(745, 752, 788),
        **changes,
    }


def decode_message(batch):
    """Execute scratch protobuf construction and return the OnSubmit message."""
    results = []

    def resolve(arg):
        if "ref" in arg:
            return results[arg["ref"]]
        return next((arg[k] for k in ("uint", "enum") if k in arg), None)

    for op in batch:
        result = None
        if op["op"] == "new":
            result = {"class": op["class"]}
            if op["class"] == MSG + "DistributionResp":
                result["Distributions"] = []
        elif op["op"] == "get" and op["member"] == "Distributions":
            result = resolve(op["target"])["Distributions"]
        elif op["op"] == "set":
            resolve(op["target"])[op["member"]] = resolve(op["value"])
        elif op.get("method") == "Add":
            resolve(op["target"]).append(resolve(op["args"][0]))
        elif op.get("method") == "Invoke":
            return resolve(op["args"][0])
        results.append(result)
    raise AssertionError("No response sent")


@pytest.mark.parametrize("split", [{"745": 2}, {"745": 1, "788": 1}, {}])
def test_distribution_sends_exact_split_and_request_identity(split):
    game = FakeGame(request(), gsid=230, msg=314)
    game.fail_on = "SubmitDistribution"  # The loaded Mac probe cannot marshal this call.
    result = adapter_for(game).handle({"action": "submit_distribution", "distributions": split})
    assert result == {"ok": True, "submitted_type": "Distribution", "target_count": len(split)}
    [batch] = game.submits()
    assert batch[1] == {"op": "expect_pending", "target": {"h": 5}}
    message = decode_message(batch)
    assert message["Type"] == "DistributionResp"
    assert message["GameStateId"] == 230
    assert message["RespId"] == 314
    assert message["DistributionResp"]["Distributions"] == [
        {"class": MSG + "Distribution", "InstanceId": int(iid), "Amount": amount}
        for iid, amount in split.items()
    ]


@pytest.mark.parametrize(
    "split, changes",
    [
        (None, {}),
        ({"745": -1}, {}),
        ({"745": 1.5}, {}),
        ({"745": True}, {}),
        ({"745": 3}, {}),
        ({"745": 2, "752": 1}, {}),
        ({"999": 2}, {}),
        ({"745": 2}, {"LegalTargetIds": listing(752)}),
        ({"745": 1}, {"Min": 2}),
        ({"745": 1, "752": 1}, {"MinPer": 2}),
        ({"745": 2}, {"MaxPer": 1}),
        ({"745": 1, "0745": 1}, {}),
    ],
)
def test_invalid_distribution_never_submits(split, changes):
    game = FakeGame(request(**changes))
    result = adapter_for(game).handle({"action": "submit_distribution", "distributions": split})
    assert result["ok"] is False
    assert game.submits() == []


def test_distribution_checks_expected_request_before_building_response():
    game = FakeGame(request(), gsid=231, msg=318)
    result = adapter_for(game).handle(
        {
            "action": "submit_distribution",
            "distributions": {"745": 2},
            "expected_game_state_id": 230,
            "expected_msg_id": 314,
        }
    )
    assert result["ok"] is False
    assert game.submits() == []


def test_distribution_rechecks_pending_request_at_submission():
    game = FakeGame(request())
    game.requests = [request(), request(**{"$h": 6})]
    result = adapter_for(game).handle({"action": "submit_distribution", "distributions": {"745": 2}})
    assert result["ok"] is False
    assert "stale" in result["error"]


@pytest.mark.parametrize("context_only", [False, True])
def test_distribution_never_uses_generic_auto_response(context_only):
    pending = {"has_pending": True, "request_type": "Distribution", "request_class": "DistributionRequest"}
    bridge = FakeBridge(pending)
    bridge.auto_respond = Mock(return_value=True)
    engine = make_engine(bridge=bridge)
    state = {
        "_bridge_request_type": "Distribution" if not context_only else "Unknown",
        "_bridge_request_class": "DistributionRequest" if not context_only else "UnknownRequest",
        "decision_context": {"type": "distribution"},
    }
    assert engine._try_interactive_safe_default(state, "watchdog") is False
    assert engine._try_auto_respond_escape(state, "stuck") is False
    bridge.auto_respond.assert_not_called()


def play_distribution(split, *, legal=(22, 33), targets=(22, 33), minimum=4):
    from arenamcp.action_planner import ActionType, GameAction

    game = FakeGame(
        request(
            TargetIds=listing(*targets),
            LegalTargetIds=listing(*legal),
            Min=minimum,
            Max=4,
            MinPer=1,
            MaxPer=4,
        )
    )
    adapter = adapter_for(game)
    pending = adapter.handle({"action": "get_pending_actions"})
    bridge = FakeBridge(pending)

    def submit(amounts):
        return adapter.handle({"action": "submit_distribution", "distributions": amounts})["ok"]

    bridge.submit_distribution = Mock(side_effect=submit)
    engine = make_engine(bridge=bridge)
    board = [
        {"instance_id": 11, "name": "Token"},
        {"instance_id": 22, "name": "Guard"},
        {"instance_id": 33, "name": "Token"},
    ]
    result = engine._try_gre_bridge_distribute(
        GameAction(action_type=ActionType.DISTRIBUTE, distribution=split), {"battlefield": board}
    )
    return result, game, bridge


def test_split_is_submitted_to_the_chosen_copy_through_native_adapter():
    result, game, _ = play_distribution({"Guard": 3, "Token": 1})
    assert result and result.success
    [batch] = game.submits()
    entries = decode_message(batch)["DistributionResp"]["Distributions"]
    assert {e["InstanceId"]: e["Amount"] for e in entries} == {22: 3, 33: 1}


def test_ambiguous_legal_copies_require_an_instance_id():
    result, _, bridge = play_distribution({"Token": 4}, legal=(11, 33), targets=(11, 33))
    assert result is None
    bridge.submit_distribution.assert_not_called()
    result, game, _ = play_distribution({"33": 4}, legal=(11, 33), targets=(11, 33))
    assert (
        result
        and decode_message(game.submits()[0])["DistributionResp"]["Distributions"][0]["InstanceId"] == 33
    )


@pytest.mark.parametrize(
    "split",
    [
        {"Guard": 4, "Unrelated": 1},
        {"Guard": 4, "Token": -1},
        {"Guard": 1.9, "Token": 3},
        {"Guard": 2, "22": 2},
    ],
)
def test_malformed_or_colliding_split_is_never_partially_sent(split):
    result, _, bridge = play_distribution(split)
    assert result is None
    bridge.submit_distribution.assert_not_called()


def test_explicit_empty_distribution_keeps_optional_counters_and_omission_is_not_a_choice():
    from arenamcp.action_planner import ActionPlanner

    planner = ActionPlanner(backend=Mock())
    assert planner._parse_action({"action_type": "distribute"}) is None
    chosen = planner._parse_action({"action_type": "distribute", "distribution": {}})
    result, game, _ = play_distribution(chosen.distribution, minimum=0)
    assert result and decode_message(game.submits()[0])["DistributionResp"]["Distributions"] == []
    result, _, bridge = play_distribution({}, minimum=4)
    assert result is None
    bridge.submit_distribution.assert_not_called()


def test_bridge_only_distribution_menu_reaches_the_planner_prompt():
    from arenamcp.action_planner import ActionPlanner
    from arenamcp.gre_bridge import enrich_snapshot_from_pending_response

    state = {
        "local_seat_id": 1,
        "zones": {
            "battlefield": [
                {"name": "Twin", "instance_id": iid, "controller_seat_id": 1} for iid in (21, 22, 23)
            ]
        },
        "turn": {"turn_number": 4},
    }
    poll = adapter_for(
        FakeGame(
            request(
                Min=0,
                Max=4,
                MinPer=1,
                MaxPer=3,
                TargetIds=listing(21, 22, 23),
                LegalTargetIds=listing(22, 23),
            )
        )
    ).handle({"action": "get_pending_actions"})
    enrich_snapshot_from_pending_response(state, poll, bridge_connected=True)
    planner = ActionPlanner(backend=Mock())
    prompt = planner._build_action_prompt(state, "Distribute")
    assert "Distribute 0 to 4" in prompt and "Per positive target: 1 to 3" in prompt
    assert "[22] Twin" in prompt and "[23] Twin" in prompt and "[21] Twin" not in prompt
