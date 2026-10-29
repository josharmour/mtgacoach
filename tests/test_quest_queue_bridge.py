"""Quest queue contract over fake reflection; no Arena connection or mutations.

The success fake retains boxed value-type results. The shipped native probe's
older get implementation discards those; test_old_probe_fails_closed preserves
that distinction until the separate probe fix is deployed.
"""

from __future__ import annotations

from copy import deepcopy

import pytest
from tests.test_mac_bridge_deck_ops import FakeArena, guid_fields

from arenamcp.mac_bridge_adapter import BEPINEX_REFLECT_COMMANDS, MacBridgeAdapter

DECK_B_GUID = "a7ef9d14-c112-4cf4-b4e5-49e735d7d102"


class QueueArena(FakeArena):
    def __init__(self):
        super().__init__()
        self.deck_a["Summary"]["Format"] = "Standard"
        self.deck_b = deepcopy(self.deck_a)
        self.deck_b["$h"] = 110
        self.deck_b["Summary"]["$h"] = 111
        self.deck_b["Summary"]["DeckId"] = guid_fields(DECK_B_GUID)
        self.by_handle[110] = self.deck_b
        self.by_handle[111] = self.deck_b["Summary"]
        self.decks_manager["CachedDecks"]["$items"].append(self.deck_b)
        for deck in (self.deck_a, self.deck_b):
            boxed = deck["Summary"]["DeckId"]
            boxed["$c"] = "System.Guid"
            for key, width in (("_a", 32), ("_b", 16), ("_c", 16)):
                if boxed[key] >= 1 << (width - 1):
                    boxed[key] -= 1 << width
        game_object = self._obj(502, "GameObject", activeInHierarchy=True)
        self.home = self._obj(501, "HomePageContentController", gameObject=game_object)
        self.finds = {"HomePageContentController": self.home}
        self.batches = []
        self.keep_boxed = True
        self.method_error = None
        self.find_error = None
        self.before_submit = None
        self.validation_results = {
            deck["$h"]: self._obj(
                700 + index,
                "ClientSideDeckValidationResult",
                IsValid=True,
                HasUnOwnedCards=False,
                NumberOfUnownedUncraftableCards=0,
                NumberOfInvalidCards=0,
                NumberBannedCards=0,
                NumberEmergencyBannedCards=0,
                NumberBannedAsCommanderCards=0,
                NumberNonFormatCard=0,
            )
            for index, deck in enumerate((self.deck_a, self.deck_b))
        }
        self.validation_views = []
        self.validation_requests = []

    def send(self, command, timeout):
        assert command["action"] == "reflect_batch"
        self.batches.append(command["ops"])
        if any(op.get("method") == "JoinMatchMaking" for op in command["ops"]):
            if self.before_submit:
                self.before_submit()
        results, objects = [], []
        for op in command["ops"]:
            try:
                kind = op["op"]
                value = None
                if kind == "find":
                    if self.find_error == op["class"]:
                        raise AssertionError("class not found: " + op["class"])
                    value = self.finds.get(op["class"])
                elif kind == "static":
                    assert op["class"] == "WrapperController"
                    assert op["member"] == "Instance"
                    value = self.wrapper
                elif kind == "new":
                    assert op["class"] == "Wizards.Mtga.Decks.DeckViewInfo"
                    assert op["args"] == []
                    value = self._obj(800 + len(self.validation_views), "DeckViewInfo", deck=None)
                    self.validation_views.append(value)
                else:
                    target = self._resolve(op["target"], objects)
                    if target is None:
                        raise AssertionError("ref does not name an object result")
                    if kind == "get":
                        member = op["member"]
                        assert member in target or op.get("optional"), "no member " + member
                        value = target.get(member)
                    elif kind == "set":
                        assert target["$c"] == "DeckViewInfo" and op["member"] == "deck"
                        target["deck"] = self._resolve(op["value"], objects)
                    elif kind == "expect":
                        if "class" in op:
                            assert target["$c"].split(".")[-1] == op["class"], "class mismatch"
                        if "member" in op:
                            assert type(target.get(op["member"])) is type(op["equals"]), "type mismatch"
                            assert target.get(op["member"]) == op["equals"], "identity mismatch"
                        value = True
                    elif kind == "call":
                        args = [
                            self._resolve(arg, objects) if "ref" in arg or "h" in arg else arg
                            for arg in op.get("args", [])
                        ]
                        if op["method"] == "GetValidationForFormat":
                            assert target["$c"] == "DeckViewInfo"
                            assert args == [{"str": "Standard"}]
                            deck = target["deck"]
                            self.validation_requests.append((target, deck, args))
                            value = {
                                "$c": "DeckDisplayInfo",
                                "ValidationResult": self.validation_results[deck["$h"]],
                            }
                        elif op["method"] == "JoinMatchMaking":
                            assert target is self.home
                            assert isinstance(args[1], dict) and args[1].get("$c") == "System.Guid"
                            if self.method_error is not None:
                                raise AssertionError(self.method_error)
                            self.calls.append(("JoinMatchMaking", tuple(args)))
                        else:
                            value = self._invoke(target, op["method"], args)
                    else:
                        raise AssertionError("unexpected op " + kind)
                results.append(value)
                boxed = isinstance(value, dict) and value.get("$c") == "System.Guid"
                objects.append(None if boxed and not (self.keep_boxed and op.get("retain_value")) else value)
            except AssertionError as exc:
                return {"ok": False, "error": str(exc), "failed_op": len(results), "results": results}
        return {"ok": True, "results": results}


@pytest.fixture
def arena():
    return QueueArena()


def queue(arena, **overrides):
    return MacBridgeAdapter(arena.send).handle(
        {"action": "queue_match", "event": "Play", "deck_id": DECK_B_GUID, **overrides}, timeout=10.0
    )


def test_exact_second_cached_guid_is_passed_as_ref_with_free_event(arena):
    result = queue(arena)
    assert result == {"ok": True, "submitted_type": "QueueMatch", "event": "Play", "deck_id": DECK_B_GUID}
    assert "queued" not in result
    assert len(arena.calls) == 1
    method, (event, boxed_guid) = arena.calls[0]
    assert method == "JoinMatchMaking" and event == {"str": "Play"}
    assert boxed_guid is arena.deck_b["Summary"]["DeckId"]
    batch = arena.batches[-1]
    call = batch[-1]
    guid_op = batch[call["args"][1]["ref"]]
    assert guid_op["op"] == "get" and guid_op["member"] == "DeckId"
    assert guid_op["retain_value"] is True
    summary_op = batch[guid_op["target"]["ref"]]
    assert summary_op["member"] == "Summary" and summary_op["target"] == {"h": 110}
    assert len(arena.validation_requests) == 2
    assert all(deck is arena.deck_b for _, deck, _ in arena.validation_requests)
    assert arena.validation_views[0] is not arena.validation_views[1]
    assert [op["class"] for batch in arena.batches for op in batch if op["op"] == "new"] == [
        "Wizards.Mtga.Decks.DeckViewInfo",
        "Wizards.Mtga.Decks.DeckViewInfo",
    ]
    assert all(
        op["member"] == "deck" and op["value"] == {"h": 110}
        for batch in arena.batches
        for op in batch
        if op["op"] == "set"
    )


def assert_no_queue_or_craft(arena):
    assert not arena.calls
    assert not any(
        op.get("method") in {"JoinMatchMaking", "Coroutine_RedeemWildcards", "StartCoroutine"}
        for batch in arena.batches
        for op in batch
    )


@pytest.mark.parametrize(
    "fields,reason",
    [
        ({"NumberBannedCards": 1}, "NumberBannedCards=1"),
        ({"NumberNonFormatCard": 2}, "NumberNonFormatCard=2"),
        ({"NumberLessThanRequiredInMain": 5}, "NumberLessThanRequiredInMain=5"),
        ({"HasUnOwnedCards": True}, "unowned cards"),
        (
            {"HasUnOwnedCards": True, "NumberOfUnownedUncraftableCards": 1},
            "NumberOfUnownedUncraftableCards=1",
        ),
        ({}, "IsValid=false"),
    ],
)
def test_native_invalid_deck_stops_before_queue_or_craft(arena, fields, reason):
    arena.validation_results[110].update(IsValid=False, **fields)
    result = queue(arena)
    assert result["ok"] is False
    assert "Native Standard deck validation rejected" in result["error"]
    assert reason in result["error"]
    assert_no_queue_or_craft(arena)
    # The first cached deck remains valid; validation must use the selected one.
    assert arena.validation_results[10]["IsValid"] is True
    assert len(arena.validation_requests) == 1
    assert arena.validation_requests[0][1] is arena.deck_b


def test_invalid_other_deck_does_not_block_selected_valid_deck(arena):
    arena.validation_results[10]["IsValid"] = False
    assert queue(arena)["ok"] is True


@pytest.mark.parametrize("valid", [None, 0, 1, "true", "false", {}, {"bool": True}])
def test_native_unknown_validity_fails_closed(arena, valid):
    arena.validation_results[110]["IsValid"] = valid
    result = queue(arena)
    assert result["ok"] is False and "validation unknown" in result["error"]
    assert_no_queue_or_craft(arena)


@pytest.mark.parametrize("missing", ["display", "validation", "IsValid"])
def test_native_missing_validation_objects_or_getter_fail_closed(arena, missing):
    if missing == "IsValid":
        del arena.validation_results[110]["IsValid"]
    elif missing == "validation":
        arena.validation_results[110] = None

    def send(command, timeout):
        if missing == "display" and any(
            op.get("method") == "GetValidationForFormat" for op in command["ops"]
        ):
            return {"ok": False, "error": "ref does not name an object result", "failed_op": 3}
        return arena.send(command, timeout)

    result = MacBridgeAdapter(send).handle({"action": "queue_match", "event": "Play", "deck_id": DECK_B_GUID})
    assert result["ok"] is False and "validation unavailable" in result["error"]
    assert "rebuild" not in result["error"]
    assert_no_queue_or_craft(arena)


@pytest.mark.parametrize("failure", ["unsupported", "exception", "incomplete", "wrong_shape"])
def test_native_validation_transport_failures_are_bounded_and_stop_submission(arena, failure):
    def send(command, timeout):
        if any(op.get("method") == "GetValidationForFormat" for op in command["ops"]):
            if failure == "exception":
                raise TimeoutError("validation timed out")
            if failure == "unsupported":
                return {"ok": False, "error": "no method GetValidationForFormat/1 " + "x" * 2000}
            if failure == "wrong_shape":
                return {"ok": True, "results": {"IsValid": True}}
            return {"ok": True, "results": [None, None, None, {"IsValid": True}]}
        return arena.send(command, timeout)

    result = MacBridgeAdapter(send).handle({"action": "queue_match", "event": "Play", "deck_id": DECK_B_GUID})
    assert result["ok"] is False and "validation" in result["error"]
    assert len(result["error"]) < 500
    assert_no_queue_or_craft(arena)


def test_native_rejection_reasons_are_bounded_raw_scalars(arena):
    validation = arena.validation_results[110]
    validation.update({key: 10**100 for key in validation if key.startswith("Number")})
    validation.update(IsValid=False, HasUnOwnedCards=True, TooltipText="x" * 10000)
    result = queue(arena)
    assert result["ok"] is False
    assert "HasUnOwnedCards=true" in result["error"]
    assert "NumberOfUnownedUncraftableCards=999999" in result["error"]
    assert len(result["error"]) < 500
    assert_no_queue_or_craft(arena)


@pytest.mark.parametrize("valid", [False, None, 1, "true"])
def test_submission_frame_revalidates_changed_deck(arena, valid):
    def mutate():
        arena.validation_results[110]["IsValid"] = valid

    arena.before_submit = mutate
    result = queue(arena)
    assert result["ok"] is False and "validation changed or is unknown" in result["error"]
    assert not arena.calls
    assert len(arena.validation_views) == 2


@pytest.mark.parametrize(
    "deck_id",
    [
        None,
        "",
        110,
        "Burn",
        "bad-guid",
        DECK_B_GUID.upper(),
        " " + DECK_B_GUID,
        "00000000-0000-0000-0000-000000000000",
    ],
)
def test_requires_exact_explicit_guid_without_default_deck(arena, deck_id):
    assert queue(arena, deck_id=deck_id)["ok"] is False
    assert not arena.calls and not arena.batches


def test_missing_explicit_id_does_not_default(arena):
    result = MacBridgeAdapter(arena.send).handle({"action": "queue_match", "event": "Play"})
    assert not result["ok"] and not arena.batches


def test_deck_must_belong_to_current_cache(arena):
    arena.decks_manager["CachedDecks"]["$items"] = [arena.deck_a]
    result = queue(arena)
    assert not result["ok"] and "found 0" in result["error"]
    assert not arena.calls


def test_duplicate_identity_fails_closed(arena):
    arena.decks_manager["CachedDecks"]["$items"].append(arena.deck_b)
    assert not queue(arena)["ok"] and not arena.calls


@pytest.mark.parametrize(
    "event", [None, "", "Ladder", "AIBotMatch", "Play_Brawl_Historic", "PremierDraft", "play"]
)
def test_only_explicit_free_standard_play(arena, event):
    assert not queue(arena, event=event)["ok"]
    assert not arena.calls and not arena.batches


@pytest.mark.parametrize("fmt", [None, "", "Historic", "Alchemy", "Brawl", "standard"])
def test_cached_format_is_authoritative(arena, fmt):
    arena.deck_b["Summary"]["Format"] = fmt
    assert not queue(arena, format="Standard")["ok"] and not arena.calls


def test_conflicting_requested_format_fails_closed(arena):
    assert not queue(arena, format="Historic")["ok"] and not arena.batches


def test_empty_main_fails_closed(arena):
    arena.deck_b["Contents"]["Piles"]["_entries"]["$items"][0]["value"]["$items"] = []
    assert not queue(arena)["ok"] and not arena.calls


def test_home_is_required(arena):
    arena.finds.clear()
    assert not queue(arena)["ok"] and not arena.calls


@pytest.mark.parametrize(
    "key,cls",
    [pair for pair in MacBridgeAdapter._SCREENS if pair[0] not in {"home", "deck_builder"}]
    + [("game", "GameManager")],
)
def test_home_does_not_override_blocking_screens(arena, key, cls):
    arena.finds[cls] = arena._obj(600, cls)
    result = queue(arena)
    assert not result["ok"] and key in result["error"] and not arena.calls


@pytest.mark.parametrize("active", [True, None, 0, "false"])
def test_active_or_unknown_editor_blocks(arena, active):
    arena.finds["DeckBuilderWidget"] = arena._obj(600, "DeckBuilderWidget", _isActive=active)
    assert not queue(arena)["ok"] and not arena.calls


def test_persistent_inactive_editor_is_allowed(arena):
    arena.finds["DeckBuilderWidget"] = arena._obj(600, "DeckBuilderWidget", _isActive=False)
    assert queue(arena)["ok"]


@pytest.mark.parametrize("change", ["home", "activity", "format", "guid"])
def test_submission_frame_rechecks_home_format_and_identity(arena, change):
    def mutate():
        if change == "home":
            arena.finds.clear()
        elif change == "activity":
            arena.home["gameObject"]["activeInHierarchy"] = False
        elif change == "format":
            arena.deck_b["Summary"]["Format"] = "Historic"
        else:
            arena.deck_b["Summary"]["DeckId"]["_k"] += 1

    arena.before_submit = mutate
    assert not queue(arena)["ok"] and not arena.calls


@pytest.mark.parametrize("error", ["no method JoinMatchMaking/2", "JoinMatchMaking threw", "unsupported"])
def test_method_errors_fail_without_fallback_or_retry(arena, error):
    arena.method_error = error
    result = queue(arena)
    assert result == {"ok": False, "error": error}
    assert not arena.calls
    assert sum(op.get("method") == "JoinMatchMaking" for batch in arena.batches for op in batch) == 1


def test_unsupported_screen_lookup_fails_closed(arena):
    arena.find_error = "MatchEndScene"
    assert not queue(arena)["ok"] and not arena.calls


def test_old_probe_fails_closed(arena):
    arena.keep_boxed = False
    result = queue(arena)
    assert result["ok"] is False and "rebuild and restart the native probe" in result["error"]
    assert not arena.calls


@pytest.mark.parametrize("action", ["get_collection", "create_deck", "update_deck"])
def test_bepinex_routes_existing_deck_handlers_through_reflection(action):
    adapter = MacBridgeAdapter(lambda *_: pytest.fail("routing must not send"), runtime="bepinex")
    assert action in BEPINEX_REFLECT_COMMANDS
    assert adapter.handles({"action": action})
    assert callable(getattr(adapter, "_cmd_" + action))


def test_bepinex_keeps_plugin_queue_and_no_craft_route():
    adapter = MacBridgeAdapter(lambda *_: pytest.fail("routing must not send"), runtime="bepinex")
    assert not adapter.handles({"action": "queue_match"})
    assert not adapter.handles({"action": "craft"})
