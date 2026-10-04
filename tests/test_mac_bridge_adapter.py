"""The native-Mac adapter must answer plugin commands exactly as the C# plugin does."""

from __future__ import annotations

from typing import Any

import pytest

from arenamcp.gre_bridge import GREBridge
from arenamcp.mac_bridge_adapter import SNAPSHOT_GETTERS, MacBridgeAdapter, casting_time_entries

MSG = "Wotc.Mtgo.Gre.External.Messaging."


def enum(name: str, value: int) -> dict:
    return {"e": name, "v": value}


def listing(*values: Any) -> dict:
    return {"$c": "System.Collections.Generic.List<X>", "$h": 900, "$n": len(values), "$items": list(values)}


def action(handle: int, kind: str, grp: int, instance: int, **extra: Any) -> dict:
    return {
        "$c": MSG + "Action",
        "$h": handle,
        "actionType_": enum(kind, 0),
        "grpId_": grp,
        "instanceId_": instance,
        "abilityGrpId_": 0,
        "sourceId_": 0,
        "assumeCanBePaidFor_": True,
        "manaCost_": listing(),
        "autoTapSolution_": None,
        "targets_": listing(),
        "highlight_": enum("None", 0),
        "shouldStop_": False,
        **extra,
    }


class FakeGame:
    """Answers reflect batches from a scripted pending request; records every op."""

    def __init__(
        self, request: dict | None, getters: dict | None = None, gsid: int = 40, msg: int = 7
    ) -> None:
        self.requests = [request]
        self.getters = getters or {}
        self.gsid, self.msg = gsid, msg
        self.batches: list[list[dict]] = []
        self.fail_on: str | None = None

    @property
    def request(self) -> dict | None:
        return self.requests[0]

    def send(self, command: dict, timeout: float | None) -> dict:
        assert command["action"] == "reflect_batch"
        ops = command["ops"]
        self.batches.append(ops)
        # A snapshot pops the next scripted request, so tests can script changes.
        if len(self.requests) > 1 and any(
            o["op"] == "get" and o.get("member") == "OriginalMessage" for o in ops
        ):
            current = self.requests.pop(0)
        else:
            current = self.requests[0]
        results: list[Any] = []
        for op in ops:
            if op["op"] == "pending":
                results.append(
                    current if current else {"$none": "no GameManager in the scene (not in a match)"}
                )
            elif op["op"] == "get" and op.get("member") == "OriginalMessage":
                results.append(
                    {"$c": MSG + "GREToClientMessage", "$h": 1, "gameStateId_": self.gsid, "msgId_": self.msg}
                    if current
                    else None
                )
            elif op["op"] == "get" and op.get("member") in SNAPSHOT_GETTERS:
                results.append(self.getters.get(op["member"]) if current else None)
            elif op["op"] == "expect_pending" and current and op["target"]["h"] != current["$h"]:
                return {"ok": False, "error": "stale: the pending request changed", "failed_op": len(results)}
            elif self.fail_on and op.get("method") == self.fail_on:
                return {"ok": False, "error": f"{self.fail_on} threw", "failed_op": len(results)}
            else:
                results.append(None)
        return {"ok": True, "results": results}

    def submits(self) -> list[list[dict]]:
        """Batches that were not plain snapshots."""
        return [b for b in self.batches if any(o["op"] == "expect_pending" for o in b)]


def adapter_for(game: FakeGame) -> MacBridgeAdapter:
    return MacBridgeAdapter(game.send)


def submitted_uint_values(batch: list[dict]) -> list[int]:
    """Interpret scratch managed collections as the old loaded bridge would.

    The old native array writer uses an eight-byte stride for four-byte uints.
    Emulating the values Arena receives catches the actual [id, 0] regression,
    including accidental extra capacity and an adapter-only marker leaking out.
    """
    results: list[Any] = []

    def resolve(arg: dict) -> Any:
        if "ref" in arg:
            return results[arg["ref"]]
        if "uint" in arg or "int" in arg:
            return arg.get("uint", arg.get("int"))
        if "list" in arg:
            corrupt = [part for value in arg["list"] for part in (resolve(value), 0)]
            return corrupt[: len(arg["list"])]
        return None

    for op in batch:
        result = None
        target = resolve(op.get("target", {}))
        if op["op"] == "new" and op["class"] == MSG + "SearchResp":
            result = {"ItemsFound": {"array": [], "Count": 0}}
        elif op["op"] == "get" and target is not None:
            assert op["depth"] == 0  # Never use the old probe's misleading array dump.
            result = target[op["member"]]
        elif op["op"] == "set" and op["member"] == "Capacity":
            capacity = resolve(op["value"])
            assert capacity >= target["Count"]
            target["array"] = (target["array"] + [0] * capacity)[:capacity]
        elif op.get("method") == "Add":
            count = target["Count"]
            if count == len(target["array"]):
                target["array"] += [0] * max(8, count)
            target["array"][count] = resolve(op["args"][0])
            target["Count"] += 1
        elif op["op"] == "expect" and "equals" in op:
            actual = len(target) if op["member"] == "Length" else target[op["member"]]
            assert actual == op["equals"]
        elif op.get("method", "").startswith("Submit"):
            result = resolve(op["args"][0])
            assert isinstance(result, list)
            return list(result)
        results.append(result)
    raise AssertionError("No uint collection submission")


def actions_request(*actions: dict, handle: int = 5) -> dict:
    return {
        "$c": "GreClient.Rules.ActionsAvailableRequest",
        "$h": handle,
        "Actions": listing(*actions),
        "_passAction": None,
    }


def test_no_pending_matches_plugin_shape():
    response = adapter_for(FakeGame(None)).handle({"action": "get_pending_actions"})
    assert response == {"ok": True, "has_pending": False, "request_type": None}


def test_actions_available_shape_and_identity():
    forest = action(11, "Play", 75553, 160)
    cast = action(
        12,
        "Cast",
        54163,
        161,
        autoTapSolution_={
            "$c": MSG + "AutoTapSolution",
            "$h": 13,
            "autoTapActions_": listing({"instanceId_": 150, "manaId_": 3}),
        },
    )
    game = FakeGame(
        actions_request(forest, cast),
        {"Type": enum("ActionsAvailable", 1), "CanPass": True, "CanCancel": False, "AllowUndo": False},
    )
    response = adapter_for(game).handle({"action": "get_pending_actions"})
    assert response["request_class"] == "ActionsAvailableRequest"
    assert response["request_type"] == "ActionsAvailable"
    assert response["game_state_id"] == 40 and response["msg_id"] == 7
    assert response["can_pass"] is True
    assert response["actions"][0] == {
        "actionType": "Play",
        "grpId": 75553,
        "instanceId": 160,
        "assumeCanBePaidFor": True,
    }
    assert response["actions"][1]["hasAutoTap"] is True
    assert response["actions"][1]["autoTapActions"] == [{"instanceId": 150, "manaId": 3}]


def test_get_game_state_reports_missing_game_like_the_plugin():
    # Outside a match the reflect batch finds no GameManager; the response
    # mirrors the C# plugin's error shape so GREBridge.get_game_state() logs
    # and returns None instead of treating the command as unsupported.
    response = adapter_for(FakeGame(None)).handle({"action": "get_game_state"})
    assert response["ok"] is False
    assert "GameManager" in response["error"]


def test_submit_action_is_identity_checked_and_atomic():
    game = FakeGame(actions_request(action(11, "Play", 75553, 160)), {"CanPass": True})
    response = adapter_for(game).handle({"action": "submit_action", "action_index": 0, "auto_pass": False})
    assert response == {
        "ok": True,
        "submitted_type": "Play",
        "submitted_grp_id": 75553,
        "submitted_instance_id": 160,
    }
    [submit] = game.submits()
    assert submit[1] == {"op": "expect_pending", "target": {"h": 5}}
    assert submit[2]["method"] == "SubmitAction"
    assert submit[2]["args"] == [{"h": 11}, {"bool": False}]


def test_expected_identity_mismatch_never_submits():
    game = FakeGame(actions_request(action(11, "Play", 75553, 160)))
    response = adapter_for(game).handle(
        {"action": "submit_action", "action_index": 0, "expected_instance_id": 999}
    )
    assert response["ok"] is False and "identity mismatch" in response["error"]
    assert game.submits() == []


def test_stale_request_is_rejected_by_the_game_side_check():
    game = FakeGame(actions_request(action(11, "Play", 75553, 160)))
    game.requests = [actions_request(action(11, "Play", 75553, 160)), actions_request(handle=6)]
    response = adapter_for(game).handle({"action": "submit_action", "action_index": 0})
    assert response["ok"] is False and "stale" in response["error"]


def test_pass_requires_can_pass():
    game = FakeGame(actions_request(), {"CanPass": False})
    assert adapter_for(game).handle({"action": "submit_pass"})["ok"] is False
    assert game.submits() == []


def test_blockers_declare_before_confirming_refreshed_selection():
    blocker = {
        "$c": MSG + "Blocker",
        "$h": 21,
        "blockerInstanceId_": 300,
        "attackerInstanceIds_": listing(400),
        "selectedAttackerInstanceIds_": listing(),
    }
    request = {"$c": "GreClient.Rules.DeclareBlockersRequest", "$h": 5, "AllBlockers": listing(blocker)}
    game = FakeGame(request)
    adapter = adapter_for(game)
    response = adapter.handle(
        {
            "action": "submit_blockers",
            "assignments": [{"blockerInstanceId": 300, "attackerInstanceIds": [400]}],
        }
    )
    assert response == {"ok": True, "submitted_type": "DeclareBlockers", "needs_finalize": True}
    [submit] = game.submits()
    types = [op["value"]["enum"] for op in submit if op["op"] == "set" and op["member"] == "Type"]
    assert types == ["DeclareBlockersResp"]
    invokes = [op for op in submit if op.get("method") == "Invoke"]
    assert len(invokes) == 1
    assert {
        "op": "call",
        "target": submit[[i for i, o in enumerate(submit) if o.get("method") == "Add"][0]]["target"],
        "method": "Add",
        "args": [{"uint": 400}],
        "depth": 0,
    } in submit

    response = adapter.handle({"action": "submit_blockers", "assignments": []})
    assert response["ok"] is True
    assert game.submits()[-1][-1]["method"] == "SubmitBlockers"


@pytest.mark.parametrize(
    "assignments, error",
    [
        (
            [
                {"blockerInstanceId": 300, "attackerInstanceIds": [400]},
                {"blockerInstanceId": 300, "attackerInstanceIds": [400]},
            ],
            "Duplicate blocker",
        ),
        ([{"blockerInstanceId": 999, "attackerInstanceIds": [400]}], "not in AllBlockers"),
        ([{"blockerInstanceId": 300, "attackerInstanceIds": [999]}], "Illegal attackers"),
    ],
)
def test_invalid_blocker_assignments_never_submit_or_finalize(assignments, error):
    blocker = {
        "$c": MSG + "Blocker",
        "$h": 21,
        "blockerInstanceId_": 300,
        "attackerInstanceIds_": listing(400),
        "selectedAttackerInstanceIds_": listing(),
    }
    game = FakeGame(
        {"$c": "GreClient.Rules.DeclareBlockersRequest", "$h": 5, "AllBlockers": listing(blocker)}
    )

    response = adapter_for(game).handle({"action": "submit_blockers", "assignments": assignments})

    assert response["ok"] is False
    assert error in response["error"]
    assert game.submits() == []


def test_blocker_snapshot_exposes_accepted_selection():
    blocker = {
        "$c": MSG + "Blocker",
        "$h": 21,
        "blockerInstanceId_": 300,
        "attackerInstanceIds_": listing(400, 401),
        "selectedAttackerInstanceIds_": listing(401),
    }
    game = FakeGame(
        {"$c": "GreClient.Rules.DeclareBlockersRequest", "$h": 5, "AllBlockers": listing(blocker)}
    )

    response = adapter_for(game).handle({"action": "get_pending_actions"})

    assert response["blockers"][0]["selectedAttackerInstanceIds"] == [401]


def test_attacker_snapshot_resolves_selected_recipient_shared_reference():
    recipient = {
        "$c": MSG + "DamageRecipient",
        "$h": 31,
        "type_": enum("Player", 1),
        "playerSystemSeatId_": 2,
    }
    attacker = {
        "$c": MSG + "Attacker",
        "$h": 30,
        "attackerInstanceId_": 955,
        "legalDamageRecipients_": listing(recipient),
        "selectedDamageRecipient_": {"$h": 31, "$ref": True},
    }
    game = FakeGame(
        {"$c": "GreClient.Rules.DeclareAttackerRequest", "$h": 5, "QualifiedAttackers": listing(attacker)}
    )
    response = adapter_for(game).handle({"action": "get_pending_actions"})
    assert response["attackers"][0]["selectedDamageRecipient"]["playerSystemSeatId"] == 2


def test_attackers_two_step_flow():
    recipient = {"$c": MSG + "DamageRecipient", "$h": 31, "type_": enum("Player", 1)}
    attacker = {
        "$c": MSG + "Attacker",
        "$h": 30,
        "attackerInstanceId_": 500,
        "legalDamageRecipients_": listing(recipient),
        "selectedDamageRecipient_": None,
    }
    request = {"$c": "GreClient.Rules.DeclareAttackerRequest", "$h": 5, "Attackers": listing(attacker)}
    game = FakeGame(request)
    adapter = adapter_for(game)
    step1 = adapter.handle({"action": "submit_attackers", "attackers": [{"attackerInstanceId": 500}]})
    assert step1["needs_finalize"] is True and step1["declared_count"] == 1
    update = game.submits()[-1]
    assert {
        "op": "set",
        "target": {"h": 30},
        "member": "SelectedDamageRecipient",
        "value": {"h": 31},
    } in update
    assert update[-1]["method"] == "UpdateAttacker" and update[-1]["args"] == [{"list": [{"h": 30}]}]
    step2 = adapter.handle({"action": "submit_attackers", "attackers": []})
    assert step2["submitted_type"] == "DeclareAttackersSubmit"
    assert game.submits()[-1][-1]["method"] == "SubmitAttackers"


def test_attacker_acknowledgment_uses_mutable_declaration_not_qualified_menu():
    # Live 00:45 capture: the menu kept null selections while manual recovery
    # produced an actual declared selection. Attackers is the mutable list.
    player = {"$h": 31, "type_": enum("Player", 1), "playerSystemSeatId_": 2}
    walker = {"$h": 32, "type_": enum("PlanesWalker", 2), "planeswalkerInstanceId_": 1022}
    menu = [
        {
            "$h": handle,
            "attackerInstanceId_": iid,
            "legalDamageRecipients_": listing(player, walker),
            "selectedDamageRecipient_": None,
        }
        for handle, iid in ((40, 551), (41, 693), (42, 803))
    ]
    declared = [
        {
            **entry,
            "$h": entry["$h"] + 10,
            "selectedDamageRecipient_": {"$h": recipient["$h"], "$ref": True} if recipient else None,
        }
        for entry, recipient in zip(menu, (None, player, walker), strict=True)
    ]
    game = FakeGame(
        {
            "$c": "GreClient.Rules.DeclareAttackerRequest",
            "$h": 5,
            "QualifiedAttackers": listing(*menu),
            "Attackers": listing(*declared),
        }
    )
    response = adapter_for(game).handle({"action": "get_pending_actions"})
    assert [(a["attackerInstanceId"], a["selectedDamageRecipient"]) for a in response["attackers"]] == [
        (551, None),
        (693, {"type": "Player", "playerSystemSeatId": 2}),
        (803, {"type": "PlanesWalker", "planeswalkerInstanceId": 1022}),
    ]


@pytest.mark.parametrize("declared", ["absent", "reference"])
def test_attacker_menu_selection_requires_membership_in_authoritative_declarations(declared):
    player = {"$h": 31, "type_": enum("Player", 1), "playerSystemSeatId_": 2}
    attacker = {
        "$h": 40,
        "attackerInstanceId_": 693,
        "legalDamageRecipients_": listing(player),
        "selectedDamageRecipient_": player,
    }
    game = FakeGame(
        {
            "$c": "GreClient.Rules.DeclareAttackerRequest",
            "$h": 5,
            "QualifiedAttackers": listing(attacker),
            "Attackers": listing(*([{"$h": 40, "$ref": True}] if declared == "reference" else [])),
        }
    )
    response = adapter_for(game).handle({"action": "get_pending_actions"})
    assert bool(response["attackers"][0]["selectedDamageRecipient"]) is (declared == "reference")


@pytest.mark.parametrize("change", ["none", "request", "recipient", "extra_attacker"])
def test_native_attack_confirmation_revalidates_request_and_exact_declared_selection(change):
    player = {"$h": 31, "type_": enum("Player", 1), "playerSystemSeatId_": 2}
    walker = {"$h": 32, "type_": enum("PlanesWalker", 2), "planeswalkerInstanceId_": 1022}
    attacker = {
        "$h": 40,
        "attackerInstanceId_": 803,
        "legalDamageRecipients_": listing(player, walker),
        "selectedDamageRecipient_": player if change == "recipient" else walker,
    }
    attackers = [attacker]
    if change == "extra_attacker":
        attackers.append({**attacker, "$h": 41, "attackerInstanceId_": 693})
    game = FakeGame(
        {"$c": "GreClient.Rules.DeclareAttackerRequest", "$h": 5, "Attackers": listing(*attackers)},
        {"CanSubmit": True},
        gsid=468,
        msg=665,
    )
    bridge = GREBridge()
    bridge._send_safe = adapter_for(game).handle
    result = bridge.submit_attackers_raw(
        [{"attackerInstanceId": 803, "damageRecipient": {"planeswalkerInstanceId": 1022}}],
        expected_request_id=(467 if change == "request" else 468, 665),
        finalize_only=True,
    )
    assert result["ok"] is (change == "none")
    assert len(game.submits()) == int(change == "none")
    if change == "none":
        assert game.submits()[0][-1]["method"] == "SubmitAttackers"
    if change == "extra_attacker":
        result = bridge.submit_attackers_raw(
            [{"attackerInstanceId": 803, "damageRecipient": {"planeswalkerInstanceId": 1022}}]
        )
        assert result["ok"] is False
        assert game.submits() == []


def test_unresolved_attacker_never_finalizes_an_empty_attack():
    request = {"$c": "GreClient.Rules.DeclareAttackerRequest", "$h": 5, "Attackers": listing()}
    game = FakeGame(request)
    result = adapter_for(game).handle(
        {"action": "submit_attackers", "attackers": [{"attackerInstanceId": 500}]}
    )
    assert result["ok"] is False
    assert game.submits() == []


def test_attackers_do_not_confirm_an_incomplete_target_choice():
    request = {"$c": "GreClient.Rules.DeclareAttackerRequest", "$h": 5, "Attackers": listing()}
    game = FakeGame(request, {"CanSubmit": False})
    result = adapter_for(game).handle({"action": "submit_attackers", "attackers": []})
    assert result["ok"] is False
    assert game.submits() == []


@pytest.mark.parametrize(
    "target_kind,target_id,expected_handle", [("player", 1, 31), ("planeswalker", 297, 32)]
)
@pytest.mark.parametrize("reversed_order", [False, True])
def test_attackers_select_requested_player_or_planeswalker(
    target_kind, target_id, expected_handle, reversed_order
):
    player = {"$h": 31, "type_": enum("Player", 1), "idCase_": enum("PlayerSystemSeatId", 2), "id_": 1}
    walker = {
        "$h": 32,
        "type_": enum("Planeswalker", 2),
        "idCase_": enum("PlaneswalkerInstanceId", 3),
        "id_": 297,
    }
    recipients = [walker, player] if reversed_order else [player, walker]
    attacker = {
        "$h": 30,
        "attackerInstanceId_": 500,
        "legalDamageRecipients_": listing(*recipients),
        "selectedDamageRecipient_": recipients[0],
    }
    game = FakeGame({"$c": "GreClient.Rules.DeclareAttackerRequest", "$h": 5, "Attackers": listing(attacker)})
    member = "playerSystemSeatId" if target_kind == "player" else "planeswalkerInstanceId"
    result = adapter_for(game).handle(
        {
            "action": "submit_attackers",
            "attackers": [{"attackerInstanceId": 500, "damageRecipient": {member: target_id}}],
        }
    )
    assert result["ok"] is True
    if recipients[0]["$h"] == expected_handle:
        assert result["submitted_type"] == "DeclareAttackersSubmit"
    else:
        assert result["needs_finalize"] is True
        assert game.submits()[-1][2]["value"] == {"h": expected_handle}


@pytest.mark.parametrize("target", [None, {"planeswalkerInstanceId": 999}, {"type": "Planeswalker"}])
def test_ambiguous_or_illegal_attack_recipient_never_submits(target):
    recipients = [{"$h": 31, "playerSystemSeatId_": 1}, {"$h": 32, "planeswalkerInstanceId_": 297}]
    attacker = {"$h": 30, "attackerInstanceId_": 500, "legalDamageRecipients_": listing(*recipients)}
    game = FakeGame({"$c": "GreClient.Rules.DeclareAttackerRequest", "$h": 5, "Attackers": listing(attacker)})
    result = adapter_for(game).handle(
        {"action": "submit_attackers", "attackers": [{"attackerInstanceId": 500, "damageRecipient": target}]}
    )
    assert result["ok"] is False
    assert game.submits() == []


def targets_request(handle: int, selected: int) -> dict:
    target = {"$c": MSG + "Target", "$h": 41, "targetInstanceId_": 700, "highlight_": enum("Hot", 2)}
    selection = {
        "$c": MSG + "TargetSelection",
        "$h": 40,
        "targetIdx_": 1,
        "minTargets_": 1,
        "maxTargets_": 1,
        "selectedTargets_": selected,
        "targets_": listing(target),
    }
    return {
        "$c": "GreClient.Rules.SelectTargetsRequest",
        "$h": handle,
        "SourceId": 77,
        "TargetSelections": listing(selection),
    }


def test_targets_select_then_commit_on_the_updated_request():
    game = FakeGame(targets_request(5, 0))
    # snapshot for the command, then the deferred loop sees the same request once
    # and then the GRE's updated request (new object, slot satisfied).
    game.requests = [
        targets_request(5, 0),
        targets_request(5, 0),
        targets_request(9, 1),
        targets_request(9, 1),
    ]
    response = adapter_for(game).handle({"action": "submit_targets", "target_instance_id": 700})
    assert response["ok"] is True and response["finalized"] is True
    selection_batch, commit_batch = game.submits()
    assert [op["value"]["enum"] for op in selection_batch if op.get("member") == "Type"] == [
        "SelectTargetsResp"
    ]
    assert {
        "op": "set",
        "target": {"ref": 2},
        "member": "TargetInstanceId",
        "value": {"uint": 700},
    } in selection_batch
    assert commit_batch[1] == {"op": "expect_pending", "target": {"h": 9}}
    assert [op["value"]["enum"] for op in commit_batch if op.get("member") == "Type"] == ["SubmitTargetsReq"]


def counted_targets_request(handle, selected=()):
    request = targets_request(handle, len(selected))
    selection = request["TargetSelections"]["$items"][0]
    selection["minTargets_"] = selection["maxTargets_"] = 2
    selection["targets_"] = listing(
        *[
            {
                "$c": MSG + "Target",
                "$h": instance_id,
                "targetInstanceId_": instance_id,
                "legalAction_": enum("Unselect", 2) if instance_id in selected else enum("Select", 1),
                "highlight_": enum("Cold", 1) if instance_id == 10 else enum("Hot", 2),
            }
            for instance_id in (10, 20, 30)
        ]
    )
    return request


def test_counted_targets_send_both_explicit_ids_in_one_slot_before_commit():
    original = counted_targets_request(5)
    acknowledged = counted_targets_request(9, (20, 30))
    game = FakeGame(original)
    game.requests = [original, original, acknowledged, acknowledged]
    response = adapter_for(game).handle({"action": "submit_targets", "target_instance_ids": [20, 30]})
    assert response["ok"] and response["finalized"]
    assert response["targets_selected"] == 2
    selection_batch, commit_batch = game.submits()
    assert [op["value"]["uint"] for op in selection_batch if op.get("member") == "TargetInstanceId"] == [
        20,
        30,
    ]
    assert sum(op.get("class") == MSG + "TargetSelection" for op in selection_batch) == 1
    assert [op["value"]["enum"] for op in commit_batch if op.get("member") == "Type"] == ["SubmitTargetsReq"]


@pytest.mark.parametrize("preferred", [[20], [999], [20, 20]])
def test_counted_targets_incomplete_plan_never_submits_or_autofills_own_target(preferred):
    game = FakeGame(counted_targets_request(5))
    response = adapter_for(game).handle({"action": "submit_targets", "target_instance_ids": preferred})
    assert not response["ok"]
    assert game.submits() == []


@pytest.mark.parametrize("selected", [(), (20,)])
def test_targets_not_acknowledged_complete_are_never_finalized(monkeypatch, selected):
    monkeypatch.setattr("arenamcp.mac_bridge_adapter.TARGET_COMMIT_TIMEOUT_S", 0.08)
    original = counted_targets_request(5)
    updated = counted_targets_request(9, selected)
    game = FakeGame(original)
    game.requests = [original, original, updated, updated]
    response = adapter_for(game).handle({"action": "submit_targets", "target_instance_ids": [20, 30]})
    assert not response["ok"]
    assert "not acknowledged complete" in response["error"]
    assert len(game.submits()) == 1


def test_mismatched_target_acknowledgment_never_finalizes():
    original = counted_targets_request(5)
    updated = counted_targets_request(9, (10, 20))
    game = FakeGame(original)
    game.requests = [original, original, updated, updated]
    response = adapter_for(game).handle({"action": "submit_targets", "target_instance_ids": [20, 30]})
    assert not response["ok"]
    assert "different targets" in response["error"]
    assert len(game.submits()) == 1


def test_target_shape_preserves_action_and_filters_selected_targets():
    game = FakeGame(counted_targets_request(5, (20,)))
    response = adapter_for(game).handle({"action": "get_pending_actions"})
    assert [candidate["targetInstanceId"] for candidate in response["target_candidates"]] == [10, 30]
    assert response["target_selections"][0]["targets"][1]["legalAction"] == "Unselect"


def test_casting_time_entries_mirror_plugin_payloads():
    modal = {
        "$c": "GreClient.Rules.CastingTimeOption_ModalRequest",
        "$h": 60,
        "SourceId": 12,
        "ModalOptions": listing(1001, 1002),
        "AbilityGrpId": 5,
        "Min": 1,
        "Max": 1,
        "OtherSelection": listing(),
    }
    done = {
        "$c": "GreClient.Rules.CastingTimeOption_DoneRequest",
        "$h": 61,
        "SourceId": 12,
        "ManaCost": listing(),
    }
    x = {
        "$c": "GreClient.Rules.CastingTimeOption_NumericInputRequest",
        "$h": 62,
        "Min": 0,
        "Max": 3,
        "StepSize": 0,
        "DisallowedValues": listing(1),
        "DisallowEven": False,
        "DisallowOdd": False,
        "GrpId": 9,
    }
    request = {
        "$c": "GreClient.Rules.CastingTimeOptionRequest",
        "$h": 5,
        "ChildRequests": listing(modal, done, x),
    }
    entries = casting_time_entries(request)
    kinds = [
        (e["payload"]["choiceKind"], e["payload"].get("optionIndex"), e["payload"].get("numericValue"))
        for e in entries
    ]
    assert kinds == [
        ("modal", 0, None),
        ("modal", 1, None),
        ("done", None, None),
        ("numeric_input", None, 0),
        ("numeric_input", None, 2),
        ("numeric_input", None, 3),
    ]
    assert entries[1]["method"] == "SubmitModal"
    game = FakeGame(request, {"CanCancel": True})
    response = adapter_for(game).handle({"action": "submit_action", "action_index": 1})
    assert response["submitted_choice_kind"] == "modal" and response["submitted_option_index"] == 1
    submit = game.submits()[-1]
    assert submit[2] == {"op": "expect", "target": {"h": 60}, "class": "CastingTimeOption_ModalRequest"}
    assert submit[-1]["method"] == "SubmitModal"
    assert submitted_uint_values(submit) == [1002]


def test_casting_time_identity_mismatch_never_submits():
    modal = {
        "$c": "GreClient.Rules.CastingTimeOption_ModalRequest",
        "$h": 60,
        "ModalOptions": listing(1001, 1002),
    }
    request = {"$c": "GreClient.Rules.CastingTimeOptionRequest", "$h": 5, "ChildRequests": listing(modal)}
    game = FakeGame(request)
    response = adapter_for(game).handle(
        {
            "action": "submit_action",
            "action_index": 1,
            "expected_choice_kind": "modal",
            "expected_option_index": 0,
        }
    )
    assert response["ok"] is False and "identity mismatch" in response["error"]
    assert game.submits() == []


def test_stale_optional_request_never_accepts_a_new_prompt():
    request = {"$c": "GreClient.Rules.OptionalActionMessageRequest", "$h": 5}
    game = FakeGame(request, gsid=40, msg=8)
    response = adapter_for(game).handle(
        {"action": "submit_optional", "accept": True, "expected_game_state_id": 40, "expected_msg_id": 7}
    )
    assert not response["ok"]
    assert "stale" in response["error"]
    assert game.submits() == []


def test_stale_casting_window_never_submits_even_if_the_mode_index_matches():
    modal = {"$c": "GreClient.Rules.CastingTimeOption_ModalRequest", "$h": 60, "ModalOptions": listing(1001)}
    request = {"$c": "GreClient.Rules.CastingTimeOptionRequest", "$h": 5, "ChildRequests": listing(modal)}
    game = FakeGame(request, gsid=41)
    response = adapter_for(game).handle(
        {"action": "submit_action", "action_index": 0, "expected_game_state_id": 40, "expected_msg_id": 7}
    )
    assert not response["ok"]
    assert "stale" in response["error"]
    assert game.submits() == []


@pytest.mark.parametrize(
    ("expected", "sent"),
    [
        (
            {"actionType": "Play", "grpId": 75553, "instanceId": 160},
            {"expected_instance_id": 160, "expected_grp_id": 75553, "expected_action_type": "Play"},
        ),
        (
            {"choiceKind": "modal", "optionIndex": 1},
            {"expected_choice_kind": "modal", "expected_option_index": 1},
        ),
        (None, {}),
    ],
)
def test_submit_by_index_carries_the_chosen_identity(expected, sent):
    bridge = GREBridge()
    commands: list[dict] = []
    bridge._send_safe = lambda cmd, timeout=None: commands.append(cmd) or {"ok": True}  # type: ignore[method-assign]
    assert bridge.submit_action_by_index(3, expected=expected)
    assert commands == [{"action": "submit_action", "action_index": 3, "auto_pass": False, **sent}]


def test_auto_tap_uses_the_pay_costs_child():
    solution = {"$c": MSG + "AutoTapSolution", "$h": 71}
    child = {"$c": "GreClient.Rules.AutoTapActionsRequest", "$h": 70, "Solutions": listing(solution)}
    request = {"$c": "GreClient.Rules.PayCostsRequest", "$h": 5, "AutoTapActions": child}
    game = FakeGame(request)
    assert adapter_for(game).handle({"action": "submit_auto_tap"})["ok"] is True
    assert game.submits()[-1][-1] == {
        "op": "call",
        "target": {"h": 70},
        "method": "SubmitSolution",
        "args": [{"h": 71}],
        "depth": 0,
    }


def _weighted_cost_request():
    selection = {
        "$c": "GreClient.Rules.SelectNRequest",
        "$h": 72,
        "Ids": listing(763, 771, 875),
        "Weights": listing(3, 1, 4),
        "IdType": enum("InstanceId", 1),
        "MinSel": 4,
        "MaxSel": 2147483647,
        "MinWeight": -2147483648,
        "MaxWeight": 2147483647,
    }
    return {
        "$c": "GreClient.Rules.PayCostsRequest",
        "$h": 5,
        "EffectCost": {
            "$c": "GreClient.Rules.EffectCostRequest",
            "$h": 71,
            "CostSelection": selection,
        },
    }


def test_non_mana_payment_exposes_weighted_selection_not_mana_autopay():
    game = FakeGame(_weighted_cost_request(), {"Type": enum("PayCostsReq", 1)})
    response = adapter_for(game).handle({"action": "get_pending_actions"})
    assert response["request_type"] == "SelectN"
    assert response["request_class"] == "PayCostsRequest"
    assert response["payment_selection"] is True
    assert response["select_n_ids"] == [763, 771, 875]
    assert response["select_n_weights"] == [3, 1, 4]
    assert response["select_n_min"] == 4


@pytest.mark.parametrize("ids", [[875], [763, 771]])
def test_non_mana_payment_submits_to_nested_selection_child(ids):
    game = FakeGame(_weighted_cost_request())
    response = adapter_for(game).handle({"action": "submit_selection", "ids": ids})
    assert response["ok"] is True
    submit = game.submits()[-1]
    assert submit[-1]["target"] == {"h": 72}
    assert submit[-1]["method"] == "SubmitSelection"
    assert submitted_uint_values(submit) == ids


@pytest.mark.parametrize("ids", [[], [763], [763, 763], [999]])
def test_non_mana_payment_rejects_invalid_selection_without_mutation(ids):
    game = FakeGame(_weighted_cost_request())
    response = adapter_for(game).handle({"action": "submit_selection", "ids": ids})
    assert response["ok"] is False
    assert not game.submits()


def test_unknown_commands_are_reported_not_guessed():
    response = adapter_for(FakeGame(None)).handle({"action": "queue_bot_match"})
    assert response == {
        "ok": False,
        "unsupported": True,
        "error": "not supported by the macOS IL2CPP bridge: queue_bot_match",
    }


def test_submission_failure_is_a_command_error():
    game = FakeGame(actions_request(action(11, "Play", 75553, 160)))
    game.fail_on = "SubmitAction"
    response = adapter_for(game).handle({"action": "submit_action", "action_index": 0})
    assert response == {"ok": False, "error": "SubmitAction threw"}


@pytest.mark.parametrize("action_name", ["ping", "reflect_batch"])
def test_bridge_routes_plugin_commands_through_the_adapter(action_name):
    bridge = GREBridge()
    calls: list[str] = []
    bridge._send_command_raw = lambda cmd, timeout=None: calls.append(cmd["action"]) or {"ok": True}  # type: ignore[method-assign]
    game = FakeGame(None)
    bridge._mac_adapter = MacBridgeAdapter(game.send)
    assert bridge._send_command({"action": action_name}) == {"ok": True}
    assert calls == [action_name]
    assert bridge._send_command({"action": "get_pending_actions"})["has_pending"] is False
    assert calls == [action_name]  # answered by the adapter via reflect batches
    assert len(game.batches) == 1


def choose_two_request():
    return {
        "$c": "GreClient.Rules.CastingTimeOptionRequest",
        "$h": 5,
        "ChildRequests": listing(
            {
                "$c": "GreClient.Rules.CastingTimeOption_ModalRequest",
                "$h": 60,
                "ModalOptions": listing(149502, 149503, 149504, 149505),
                "Min": 2,
                "Max": 2,
            }
        ),
    }


def multi_mode_command(request=None):
    entries = casting_time_entries(request or choose_two_request())
    return {
        "action": "submit_casting_options",
        "action_indices": [1, 2],
        "expected_options": [entries[index]["payload"] for index in (1, 2)],
        "expected_game_state_id": 40,
        "expected_msg_id": 7,
    }


def test_choose_two_bridge_pipeline_submits_one_complete_modal_array():
    from arenamcp.decisions import build_pending_decision, submit_option

    game = FakeGame(choose_two_request(), {"Type": enum("CastingTimeOptions", 1)})
    adapter = adapter_for(game)
    bridge = GREBridge()
    bridge._send_safe = adapter.handle
    poll = adapter.handle({"action": "get_pending_actions"})
    decision = build_pending_decision(poll)
    assert decision.min_select == decision.max_select == 2
    assert submit_option(bridge, decision, ["idx:1", "idx:2"])
    submissions = game.submits()
    assert len(submissions) == 1
    modal_calls = [operation for operation in submissions[0] if operation.get("method") == "SubmitModal"]
    assert len(modal_calls) == 1
    assert submitted_uint_values(submissions[0]) == [149503, 149504]


@pytest.mark.parametrize("ids", [[480, 475], [671, 692], [480], []])
def test_search_preserves_exact_uint_ids_with_old_loaded_probe(ids):
    request = {
        "$c": "GreClient.Rules.SearchRequest",
        "$h": 5,
        "Options": listing(480, 475, 671, 692),
        "Min": 0,
        "Max": 2,
    }
    game = FakeGame(request)
    assert adapter_for(game).handle({"action": "submit_selection", "ids": ids})["ok"]
    [batch] = game.submits()
    assert submitted_uint_values(batch) == ids
    assert request["Options"]["$items"] == [480, 475, 671, 692]
    assert batch[1] == {"op": "expect_pending", "target": {"h": 5}}


@pytest.mark.parametrize(
    ("request_class", "command"),
    [
        ("SelectNRequest", {"action": "submit_selection", "ids": [480, 475]}),
        ("OrderRequest", {"action": "submit_order", "ids": [480, 475]}),
        ("SelectNGroupRequest", {"action": "submit_select_n_group", "ids": [480, 475]}),
    ],
)
def test_other_uint_collection_submissions_preserve_both_ids(request_class, command):
    game = FakeGame({"$c": "GreClient.Rules." + request_class, "$h": 5})
    assert adapter_for(game).handle(command)["ok"]
    assert submitted_uint_values(game.submits()[-1]) == [480, 475]


def test_uint_collection_setup_failure_never_reports_submission_success():
    game = FakeGame({"$c": "GreClient.Rules.SearchRequest", "$h": 5, "Options": listing(480, 475), "Max": 2})
    game.fail_on = "Add"
    response = adapter_for(game).handle({"action": "submit_selection", "ids": [480, 475]})
    assert response == {"ok": False, "error": "Add threw"}


@pytest.mark.parametrize(
    "fault", ["one_mode", "too_many", "duplicate", "stale_request", "reordered_modes", "mixed_child"]
)
def test_invalid_multi_mode_response_never_sends_any_mode(fault):
    request = choose_two_request()
    command = multi_mode_command(request)
    if fault == "one_mode":
        command["action_indices"] = [1]
        command["expected_options"] = command["expected_options"][:1]
    elif fault == "too_many":
        entries = casting_time_entries(request)
        command["action_indices"] = [0, 1, 2]
        command["expected_options"] = [entry["payload"] for entry in entries[:3]]
    elif fault == "duplicate":
        command["action_indices"] = [1, 1]
    elif fault == "stale_request":
        command["expected_msg_id"] = 6
    elif fault == "reordered_modes":
        request["ChildRequests"]["$items"][0]["ModalOptions"] = listing(149502, 149504, 149503, 149505)
    elif fault == "mixed_child":
        second = {
            "$c": "GreClient.Rules.CastingTimeOption_ModalRequest",
            "$h": 61,
            "ModalOptions": listing(2000, 2001),
            "Min": 1,
            "Max": 1,
        }
        request["ChildRequests"]["$items"].append(second)
        entries = casting_time_entries(request)
        command["action_indices"] = [1, 4]
        command["expected_options"] = [entries[index]["payload"] for index in (1, 4)]
    game = FakeGame(request)
    response = adapter_for(game).handle(command)
    assert not response["ok"]
    assert game.submits() == []


def test_legacy_single_mode_submit_refuses_required_choose_two():
    game = FakeGame(choose_two_request())
    response = adapter_for(game).handle({"action": "submit_action", "action_index": 1})
    assert not response["ok"]
    assert "multiple modes" in response["error"]
    assert game.submits() == []


def test_old_plugin_rejects_multi_modes_without_single_mode_fallback():
    bridge = GREBridge()
    commands = []
    bridge._send_safe = lambda command: commands.append(command) or {"ok": False, "error": "Unknown command"}
    assert not bridge.submit_casting_options([1, 2], expected=[{}, {}])
    assert [command["action"] for command in commands] == ["submit_casting_options"]
