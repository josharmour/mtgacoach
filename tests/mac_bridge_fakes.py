"""Fakes for the native-Mac bridge: scripted pending requests for reflect batches.

FakeGame answers the adapter's ``reflect_batch`` commands from a scripted
pending request (scene-level commands use tests/mac_world.World instead).
"""

from __future__ import annotations

from typing import Any

from arenamcp.mac_bridge_adapter import SNAPSHOT_GETTERS, MacBridgeAdapter

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


def actions_request(*actions: dict, handle: int = 5) -> dict:
    return {
        "$c": "GreClient.Rules.ActionsAvailableRequest",
        "$h": handle,
        "Actions": listing(*actions),
        "_passAction": None,
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
        # Requests (by handle) the player already answered: Submit() nulls OnSubmit,
        # so the probe's Invoke on it fails "ref does not name an object result".
        self.answered: set[int] = set()

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
            elif op.get("method") == "Invoke" and current and current["$h"] in self.answered:
                return {"ok": False, "error": "ref does not name an object result", "failed_op": len(results)}
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
