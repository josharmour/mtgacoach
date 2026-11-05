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
