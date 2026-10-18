"""A scripted object world for MacBridgeAdapter reflect batches (IL2CPP probe stand-in)."""

from __future__ import annotations

from typing import Any


class World:
    """Interprets reflect batches over scripted objects; records calls."""

    def __init__(self) -> None:
        self.objects: dict[int, dict[str, Any]] = {}
        self.classes: dict[int, str] = {}
        self.finds: dict[str, int] = {}
        self.results: dict[tuple[int, str], Any] = {}
        self.calls: list[tuple[int, str, list]] = []

    def add(self, handle: int, cls: str, **members: Any) -> int:
        self.objects[handle], self.classes[handle] = members, cls
        return handle

    def node(self, value: Any) -> Any:
        if isinstance(value, Obj):
            return {"$c": self.classes[value.handle], "$h": value.handle}
        if isinstance(value, list):
            return {"$n": len(value), "$items": [self.node(v) for v in value]}
        return value

    def target(self, ref: dict, results: list) -> int | None:
        if "h" in ref:
            return ref["h"]
        value = results[ref["ref"]]
        return value.get("$h") if isinstance(value, dict) else None

    def send(self, command: dict, timeout: float | None) -> dict:
        results: list[Any] = []
        for op in command["ops"]:
            kind = op["op"]
            if kind == "find":
                handle = self.finds.get(op["class"])
                results.append({"$c": op["class"], "$h": handle} if handle else {"$none": "not found"})
                continue
            handle = self.target(op["target"], results)
            if handle is None:
                if op.get("optional"):
                    results.append(None)
                    continue
                return {"ok": False, "error": f"null target for {op}"}
            members = self.objects[handle]
            if kind == "get":
                results.append(self.node(members.get(op["member"])))
            elif kind == "expect":
                if members.get(op["member"]) != op["equals"]:
                    return {"ok": False, "error": f"expect {op['member']} failed"}
                results.append(True)
            elif kind == "call":
                self.calls.append((handle, op["method"], op["args"]))
                result = self.results.get((handle, op["method"]))
                results.append(self.node(result(op["args"]) if callable(result) else result))
            elif kind == "set":
                members[op["member"]] = op["value"]
                results.append(None)
        return {"ok": True, "results": results}


class Obj:
    def __init__(self, handle: int) -> None:
        self.handle = handle
