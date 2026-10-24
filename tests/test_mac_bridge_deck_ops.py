"""Feature #204: get_collection / update_deck / create_deck / craft over the bridge.

The fake transport executes the reflect ops against a tiny object model, so
the tests assert what Arena would actually see: which structs are removed from
which pile list, what UpdateDeck/CreateDeck receive, and that no wildcard is
ever redeemed without ``confirm``.
"""

from __future__ import annotations

from typing import Any

import pytest

from arenamcp.mac_bridge_adapter import MacBridgeAdapter

DECK_A_GUID = "07ef9d14-c112-4cf4-b4e5-49e735d7d101"


def guid_fields(hex_id: str) -> dict:
    import uuid

    raw = uuid.UUID(hex_id)
    a, b, c = raw.time_low, raw.time_mid, raw.time_hi_version
    out = {"_a": a, "_b": b, "_c": c}
    out.update({f"_{letter}": byte for letter, byte in zip("defghijk", raw.bytes[8:], strict=True)})
    return out


class FakeArena:
    """Executes reflect ops on a miniature WrapperController object graph."""

    def __init__(self) -> None:
        self.by_handle: dict[int, Any] = {}
        self.wrapper = self._obj(1, "WrapperController", DecksManager=None, InventoryManager=None)
        self.decks_manager = self._obj(2, "DecksManager", CachedDecks=None)
        self.inventory_manager = self._obj(3, "InventoryManager", Cards=None, Inventory=None)
        self.wrapper["DecksManager"] = self.decks_manager
        self.wrapper["InventoryManager"] = self.inventory_manager
        self.inventory_manager["Cards"] = {
            "$c": "Dictionary",
            "$h": 30,
            "_entries": {
                "$n": 3,
                "$items": [
                    {"key": 70103, "value": 4},
                    {"key": 70204, "value": 1},
                    {"key": 0, "value": 0},
                ],
            },
        }
        self.inventory_manager["Inventory"] = {
            "$c": "ClientPlayerInventory",
            "$h": 31,
            "wcCommon": 12,
            "wcUncommon": 9,
            "wcRare": 3,
            "wcMythic": 1,
            "gold": 1500,
            "gems": 250,
        }
        self.main_list = self._pile(41, [(70103, 4), (70204, 2)])
        self.side_list = self._pile(42, [(70103, 2)])
        self.by_handle[41] = self.main_list
        self.by_handle[42] = self.side_list
        summary = self._obj(11, "Client_DeckSummary", DeckId=guid_fields(DECK_A_GUID), Name="Burn")
        contents = self._obj(
            12,
            "Client_DeckContents",
            Piles={
                "$c": "Dictionary",
                "$h": 13,
                "_entries": {
                    "$items": [
                        {"key": {"e": "Main", "v": 0}, "value": self.main_list},
                        {"key": {"e": "Sideboard", "v": 1}, "value": self.side_list},
                    ]
                },
            },
        )
        self.deck_a = self._obj(10, "Client_Deck", Summary=summary, Contents=contents)
        cached = {"$c": "List", "$h": 20, "$items": [self.deck_a]}
        self.decks_manager["CachedDecks"] = cached
        self.by_handle[20] = cached
        self.calls: list[tuple[str, tuple]] = []

    def _obj(self, handle: int, cls: str, **fields: Any) -> dict:
        obj = {"$c": cls, "$h": handle, **fields}
        self.by_handle[handle] = obj
        return obj

    @staticmethod
    def _pile(handle: int, cards: list[tuple[int, int]]) -> dict:
        return {
            "$c": "List",
            "$h": handle,
            "$n": len(cards),
            "$items": [{"Id": grp, "Quantity": qty} for grp, qty in cards],
        }

    # -- op execution ---------------------------------------------------------
    def send(self, command: dict, timeout: float | None) -> dict:
        assert command["action"] == "reflect_batch"
        results: list[Any] = []
        objects: list[Any] = []
        for op in command["ops"]:
            kind = op["op"]
            try:
                if kind == "static":
                    assert op["class"] == "WrapperController" and op["member"] == "Instance"
                    results.append(self.wrapper)
                    objects.append(self.wrapper)
                elif kind == "get":
                    target = self._resolve(op["target"], objects)
                    member = op["member"]
                    value = target.get(member) if isinstance(target, dict) else None
                    results.append(value)
                    objects.append(value if isinstance(value, dict) else None)
                elif kind == "call":
                    target = self._resolve(op["target"], objects)
                    method = op["method"]
                    args = [self._resolve(a, objects) if isinstance(a, dict) and ("ref" in a or "h" in a) else a for a in op.get("args") or []]
                    value = self._invoke(target, method, args)
                    results.append(value)
                    objects.append(value if isinstance(value, dict) else None)
                elif kind == "new":
                    cls = op["class"].rsplit(".", 1)[-1]
                    if cls == "WildcardBulkRequest":
                        capacity = op["args"][0]["int"]
                        bulk = {"$c": "List", "$h": 200 + len(objects), "$n": 0, "$items": []}
                        value = {"$c": cls, "$h": 100 + len(objects), "bulkRequest": bulk}
                        self.by_handle[value["$h"]] = value
                        self.by_handle[bulk["$h"]] = bulk
                    elif cls == "Client_Deck":
                        source = self.by_handle[op["args"][0]["h"]]
                        summary = {
                            "$c": "Client_DeckSummary",
                            "$h": 300 + len(objects),
                            "DeckId": dict(source["Summary"]["DeckId"]),
                            "Name": source["Summary"]["Name"],
                        }
                        value = {"$c": cls, "$h": 100 + len(objects), "Summary": summary}
                        self.by_handle[value["$h"]] = value
                        self.by_handle[summary["$h"]] = summary
                    else:  # Client_DeckCard / CardAndQuantity boxed structs
                        value = {"$c": cls, "$h": 100 + len(objects), "_args": op.get("args") or []}
                        self.by_handle[value["$h"]] = value
                    results.append(value)
                    objects.append(value)
                elif kind == "set":
                    target = self._resolve(op["target"], objects)
                    target[op["member"]] = op["value"]["str"]
                    results.append(None)
                    objects.append(None)
                else:
                    raise AssertionError(f"unexpected op {kind}")
            except AssertionError as error:
                return {"ok": False, "error": str(error), "failed_op": len(results)}
        return {"ok": True, "results": results}

    def _resolve(self, target: dict, objects: list[Any]) -> Any:
        if "ref" in target:
            return objects[target["ref"]]
        if "h" in target:
            found = self.by_handle.get(int(target["h"]))
            if found is None:
                raise AssertionError(f"unknown handle {target['h']}")
            return found
        raise AssertionError(f"unresolvable target {target}")

    def _invoke(self, target: Any, method: str, args: list[Any]) -> Any:
        if target is self.decks_manager and method == "GetAllCachedDecks":
            return self.decks_manager["CachedDecks"]
        if target is self.inventory_manager and method == "Coroutine_RedeemWildcards":
            self.calls.append(("Coroutine_RedeemWildcards", (args[0],)))
            return {"$c": "IEnumerator", "$h": 77}
        if target is self.wrapper and method == "StartCoroutine":
            self.calls.append(("StartCoroutine", (args[0]["$h"],)))
            return None
        if target is self.decks_manager and method in ("UpdateDeck", "CreateDeck"):
            self.calls.append((method, tuple(args)))
            return None
        if method in ("Remove", "Add") and isinstance(target, dict) and "$items" in target:
            entry = args[0]
            first, second = entry["_args"][0], entry["_args"][1]
            card = {"Id": first.get("uint", first.get("int")), "Quantity": second.get("uint", second.get("int"))}
            if method == "Remove":
                target["$items"].remove(card)
            else:
                target["$items"].append(card)
            target["$n"] = len(target["$items"])
            self.calls.append((method, (target["$h"], card["Id"], card["Quantity"])))
            return None
        raise AssertionError(f"unexpected call {method} on {target}")


@pytest.fixture
def arena() -> FakeArena:
    return FakeArena()


@pytest.fixture
def adapter(arena: FakeArena) -> MacBridgeAdapter:
    return MacBridgeAdapter(arena.send)


def test_get_collection_reads_cards_and_balances(adapter: MacBridgeAdapter):
    response = adapter.handle({"action": "get_collection"}, timeout=5.0)
    assert response["ok"] is True
    assert response["cards"] == {70103: 4, 70204: 1}
    assert response["wildcards"] == {"common": 12, "uncommon": 9, "rare": 3, "mythic": 1}
    assert response["gold"] == 1500 and response["gems"] == 250


def test_update_deck_rewrites_piles_and_saves_with_updated(adapter: MacBridgeAdapter, arena: FakeArena):
    response = adapter.handle(
        {
            "action": "update_deck",
            "deck_id": DECK_A_GUID,
            "piles": {"main": [[70103, 3], [70204, 3], [70999, 1]], "sideboard": []},
        },
        timeout=5.0,
    )
    assert response["ok"] is True and response["submitted_type"] == "DeckUpdate"
    # main: Bolt 4->3 (remove+add), Shock 2->3 (remove+add), new card added;
    # sideboard emptied.
    assert arena.main_list["$items"] == [
        {"Id": 70103, "Quantity": 3},
        {"Id": 70204, "Quantity": 3},
        {"Id": 70999, "Quantity": 1},
    ]
    assert arena.side_list["$items"] == []
    saved = [call for call in arena.calls if call[0] == "UpdateDeck"]
    deck_arg, action_arg = saved[0][1]
    assert deck_arg["$h"] == 10 and action_arg == {"enum": "Updated"}


def test_update_deck_without_changes_skips_the_save(adapter: MacBridgeAdapter, arena: FakeArena):
    response = adapter.handle(
        {"action": "update_deck", "deck_id": DECK_A_GUID, "piles": {"main": [[70103, 4], [70204, 2]]}},
        timeout=5.0,
    )
    assert response["ok"] is True and response.get("unchanged") is True
    assert not [call for call in arena.calls if call[0] == "UpdateDeck"]


def test_update_deck_refuses_an_empty_main_pile(adapter: MacBridgeAdapter):
    response = adapter.handle(
        {"action": "update_deck", "deck_id": DECK_A_GUID, "piles": {"main": []}}, timeout=5.0
    )
    assert response["ok"] is False and "empty main" in response["error"]


def test_update_deck_reports_an_unknown_deck(adapter: MacBridgeAdapter):
    response = adapter.handle(
        {"action": "update_deck", "deck_id": "00000000-0000-0000-0000-000000000000", "piles": {"main": [[1, 1]]}},
        timeout=5.0,
    )
    assert response["ok"] is False and "No cached deck" in response["error"]


def test_create_deck_clones_renames_and_calls_created(adapter: MacBridgeAdapter, arena: FakeArena):
    response = adapter.handle(
        {"action": "create_deck", "source_deck_id": DECK_A_GUID, "name": "Burn v2"}, timeout=5.0
    )
    assert response["ok"] is True and response["submitted_type"] == "DeckCreate"
    created = [call for call in arena.calls if call[0] == "CreateDeck"][0]
    copy, action, force_copy = created[1]
    assert copy["$c"] == "Client_Deck" and copy["Summary"]["Name"] == "Burn v2"
    assert copy["Summary"]["DeckId"] == arena.deck_a["Summary"]["DeckId"]  # cloned from the source
    assert action == {"str": "Cloned"} and force_copy == {"bool": True}
    assert arena.deck_a["Summary"]["Name"] == "Burn"  # the source keeps its name


def test_craft_builds_the_bulk_request_and_starts_the_coroutine(adapter: MacBridgeAdapter, arena: FakeArena):
    response = adapter.handle(
        {"action": "craft", "cards": [{"grp_id": 70999, "count": 2}, {"grp_id": 70888, "count": 1}], "confirm": True},
        timeout=5.0,
    )
    assert response["ok"] is True and response["submitted_type"] == "WildcardRedeem"
    assert response["total_crafted"] == 3
    redeem = [call for call in arena.calls if call[0] == "Coroutine_RedeemWildcards"]
    assert redeem and redeem[0][1][0]["$c"] == "WildcardBulkRequest"
    started = [call for call in arena.calls if call[0] == "StartCoroutine"]
    assert started and started[0][1] == (77,)


def test_craft_never_spends_without_confirmation(adapter: MacBridgeAdapter):
    response = adapter.handle({"action": "craft", "cards": [{"grp_id": 70999, "count": 1}]}, timeout=5.0)
    assert response["ok"] is False and "confirm=true" in response["error"]


def test_pipe_commands_reach_the_deck_library_and_emit_results():
    """The desktop → pipe → coach → bridge round trip for the four commands."""
    import time
    from unittest.mock import Mock

    from arenamcp.pipe_adapter import PipeAdapter

    pipe = PipeAdapter()
    events: list[dict] = []
    pipe._emit = events.append  # type: ignore[method-assign]
    coach = Mock()
    pipe._coach = coach

    def settled() -> bool:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if coach.collection_async.called and coach.update_deck_async.called and \
               coach.create_deck_async.called and coach.craft_async.called:
                return True
            time.sleep(0.02)
        return False

    pipe._dispatch({"cmd": "get_collection"})
    pipe._dispatch({"cmd": "update_deck", "deck_id": "a", "piles": {"main": [[1, 1]]}})
    pipe._dispatch({"cmd": "create_deck", "source_deck_id": "a", "name": "N"})
    pipe._dispatch({"cmd": "craft", "cards": [{"grp_id": 1, "count": 2}]})
    assert settled(), "the dispatcher did not run every deck command"
    coach.update_deck_async.assert_called_once_with("a", {"main": [[1, 1]]})
    coach.create_deck_async.assert_called_once_with("a", "N")
    coach.craft_async.assert_called_once_with([{"grp_id": 1, "count": 2}])

    pipe.emit_collection({"ok": True, "cards": {}})
    pipe.emit_deck_op({"ok": False, "error": "x"})
    assert events[-2] == {"type": "collection", "ok": True, "cards": {}}
    assert events[-1] == {"type": "deck_op", "ok": False, "error": "x"}

    pipe._coach = object()  # no library support: an honest error event
    pipe._handle_deck_bridge("collection_async")
    assert events[-1]["type"] == "deck_op" and events[-1]["ok"] is False
