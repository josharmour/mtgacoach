"""Tests for Draft / Event auto-queue mode across bridge, driver, runtime, IPC, and UI."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

from tests.mac_world import Obj, World

from arenamcp.draft_event import DraftEventDriver
from arenamcp.event_queue import (
    EntryApprovalStore,
    EventQueueConfig,
    choose_constructed_event_deck,
    choose_jumpstart_packet,
)
from arenamcp.mac_bridge_adapter import BEPINEX_REFLECT_COMMANDS, MacBridgeAdapter
from arenamcp.pipe_adapter import PipeAdapter
from arenamcp.standalone_auto_queue import _AutoQueueMixin
from arenamcp.standalone_draft_event import _DraftEventMixin

DECK_GUID_1 = "a7ef9d14-c112-4cf4-b4e5-49e735d7d102"
DECK_GUID_2 = "b7ef9d14-c112-4cf4-b4e5-49e735d7d203"


def _scripted_bridge(screens: list[dict], **replies):
    commands: list[tuple[str, dict]] = []
    screen_iter = iter(screens)
    seq_iters = {k: iter(v) for k, v in replies.items() if isinstance(v, list)}

    class _Bridge:
        connected = True

        def draft_command(self, action, **fields):
            commands.append((action, fields))
            if action == "get_screen":
                return next(screen_iter, {"ok": True})
            if action in seq_iters:
                val = next(seq_iters[action], {"ok": False})
                return {"ok": True, **val} if "ok" not in val else val
            reply = replies.get(action, {"ok": True})
            if callable(reply):
                return reply(fields)
            return {"ok": True, **reply} if isinstance(reply, dict) and "ok" not in reply else reply

    return _Bridge(), commands


def _guid_fields(guid_str: str) -> dict:
    import uuid

    u = uuid.UUID(guid_str)
    parts = (u.time_low, u.time_mid, u.time_hi_version, *u.bytes[8:])
    return {f"_{name}": val for name, val in zip("abcdefghijk", parts, strict=True)}


def _fee(world: World, h: int, currency: str, qty: int, uses: bool = True, ref: str = "") -> Obj:
    return Obj(
        world.add(
            h,
            "EventEntryFeeInfo",
            CurrencyType={"e": currency, "v": 0},
            Quantity=qty,
            UsesRemaining=uses,
            ReferenceId=ref,
        )
    )


def _event_context(
    world: World,
    base_h: int,
    *,
    name: str,
    public_name: str = "",
    format_type: str = "Draft",
    module: str = "Join",
    behavior: str = "EventPage",
    joinability: str = "Open",
    is_precon: bool = False,
    deck_fmt: str = "Standard",
    wins: int = 0,
    losses: int = 0,
    games: int = 0,
    unclaimed: bool = False,
    fees: list[dict] | None = None,
    loss_type: str = "LossSlots",
    loss_games: int = 3,
    max_prize_wins: int = 7,
    cumulative: bool = False,
    state: str = "Active",
) -> dict:
    fee_list = world.add(base_h + 1, "List", **{"$items": fees or []})
    info = world.add(
        base_h + 2,
        "EventInfo",
        InternalEventName=name,
        EventState={"e": state, "v": 1},
        FormatType={"e": format_type, "v": 1},
        IsPreconEvent=is_precon,
        SkipDeckValidation=is_precon,
        EntryFees=Obj(fee_list),
    )
    loss_disp = world.add(
        base_h + 3,
        "LossDetailsDisplayData",
        LossDetailsType={"e": loss_type, "v": 1},
        Games=loss_games,
    )
    chest = world.add(base_h + 4, "EventChestDescription", Wins=max_prize_wins)
    chests = world.add(base_h + 5, "List", **{"$items": [Obj(chest)] if max_prize_wins > 0 else []})
    track = world.add(base_h + 6, "ObjectiveTrackData", ChestDescriptions=Obj(chests))
    comp_data = world.add(
        base_h + 7,
        "EventComponentData",
        LossDetailsDisplay=Obj(loss_disp),
        CumulativeObjectiveTrack=Obj(track) if cumulative else None,
        ByCourseObjectiveTrack=None if cumulative else Obj(track),
    )
    ux = world.add(
        base_h + 8,
        "EventUXInfo",
        PublicEventName=public_name,
        TitleLocKey="",
        EventBladeBehavior={"e": behavior, "v": 1},
        DeckSelectFormat=deck_fmt,
        Joinability={"e": joinability, "v": 1},
        Prizes=None,
        EventComponentData=Obj(comp_data),
    )
    course = world.add(base_h + 9, "CourseData", CurrentModule={"e": module, "v": 1})
    pe = world.add(
        base_h + 10,
        "PlayerEvent",
        EventInfo=Obj(info),
        EventUXInfo=Obj(ux),
        CourseData=Obj(course),
        CurrentWins=wins,
        CurrentLosses=losses,
        GamesPlayed=games,
        HasUnclaimedRewards=unclaimed,
        WinCondition={"e": "BestOfOne", "v": 1},
    )
    return world.add(base_h + 11, "EventContext", PlayerEvent=Obj(pe))


class _EventWorld(World):
    def __init__(self) -> None:
        super().__init__()
        self.statics: dict[str, int] = {}

    def node_depth(self, value: Any, depth: int = 0) -> Any:
        if isinstance(value, Obj):
            h = value.handle
            out: dict[str, Any] = {"$c": self.classes[h], "$h": h}
            if depth > 0:
                for k, v in self.objects[h].items():
                    if k == "$items" and isinstance(v, list):
                        out["$items"] = [self.node_depth(item, depth - 1) for item in v]
                        out["$n"] = len(v)
                    else:
                        out[k] = self.node_depth(v, depth - 1)
            return out
        if isinstance(value, list):
            return {"$n": len(value), "$items": [self.node_depth(v, depth) for v in value]}
        return value

    def send(self, command: dict, timeout: float | None) -> dict:
        results: list = []
        for op in command["ops"]:
            kind = op["op"]
            if kind == "find":
                h = self.finds.get(op["class"])
                results.append({"$c": op["class"], "$h": h} if h else {"$none": "not found"})
            elif kind == "static":
                key = f"{op['class']}.{op['member']}"
                h = self.statics.get(key)
                results.append({"$c": op["class"], "$h": h} if h else {"$none": "not found"})
            elif kind == "new":
                return {"ok": False, "error": "new not supported in _EventWorld"}
            else:
                h = self.target(op["target"], results)
                if h is None:
                    if op.get("optional"):
                        results.append(None)
                        continue
                    return {"ok": False, "error": f"null target for {op}"}
                members = self.objects[h]
                depth = int(op.get("depth") or 0)
                if kind == "get":
                    if op["member"] not in members and op.get("optional"):
                        results.append(None)
                    else:
                        results.append(self.node_depth(members.get(op["member"]), depth))
                elif kind == "expect":
                    actual = members.get(op["member"])
                    if isinstance(actual, dict) and "e" in actual and isinstance(op["equals"], str):
                        ok = actual["e"] == op["equals"]
                    else:
                        ok = actual == op["equals"]
                    if not ok:
                        return {"ok": False, "error": f"expect {op['member']} failed"}
                    results.append(True)
                elif kind == "call":
                    args = op.get("args", [])
                    self.calls.append((h, op["method"], args))
                    if op["method"] == "get_Item" and "$items" in members:
                        result = members["$items"][args[0]["int"]]
                    else:
                        raw_fn = self.results.get((h, op["method"]))
                        result = raw_fn(args) if callable(raw_fn) else raw_fn
                    results.append(self.node_depth(result, depth))
                elif kind == "set":
                    members[op["member"]] = op["value"]
                    results.append(None)
        return {"ok": True, "results": results}


def _events_world() -> _EventWorld:
    world = _EventWorld()
    inv = world.add(
        10,
        "ClientPlayerInventory",
        gold=12000,
        gems=800,
        draftTokens=1,
        sealedTokens=0,
        CustomTokens=None,
    )
    inv_mgr = world.add(11, "InventoryManager", Inventory=Obj(inv))

    c_human = _event_context(
        world,
        100,
        name="PremierDraft_TST_20261126",
        public_name="Premier Draft: Test",
        format_type="Draft",
        module="PayEntry",
        fees=[
            _fee(world, 120, "Gold", 10000),
            _fee(world, 121, "Gem", 1500),
            _fee(world, 122, "DraftToken", 1),
        ],
    )
    c_bot = _event_context(
        world,
        200,
        name="QuickDraft_TST_20261126",
        public_name="Quick Draft: Test",
        format_type="Draft",
        module="Draft",
        wins=2,
        losses=1,
        fees=[_fee(world, 220, "Gold", 5000)],
    )
    c_sealed = _event_context(
        world,
        300,
        name="Sealed_TST_20261126",
        public_name="Sealed Test",
        format_type="Sealed",
        module="PayEntry",
        fees=[_fee(world, 320, "Gem", 2000)],
    )
    c_mwm = _event_context(
        world,
        400,
        name="Midweek_Magic_TST",
        public_name="Midweek Magic",
        format_type="Constructed",
        module="Join",
        is_precon=True,
        loss_type="PlayUntilEventEnds",
        loss_games=0,
        max_prize_wins=3,
        cumulative=True,
        fees=[_fee(world, 420, "Free", 0)],
    )
    c_jump = _event_context(
        world,
        500,
        name="JumpIn_TST",
        public_name="Jump In!",
        format_type="Constructed",
        module="Join",
        loss_type="PlayUntilEventEnds",
        loss_games=0,
        max_prize_wins=1,
        cumulative=True,
        fees=[_fee(world, 520, "Gold", 1000)],
    )
    c_queue = _event_context(
        world,
        600,
        name="Ladder",
        public_name="Standard Ranked",
        format_type="Constructed",
        module="Join",
        behavior="Queue",
        fees=[_fee(world, 620, "Free", 0)],
    )
    c_closed = _event_context(
        world,
        700,
        name="Closed_Event",
        public_name="Closed Event",
        format_type="Constructed",
        module="Join",
        joinability="Closed",
        fees=[_fee(world, 720, "Free", 0)],
    )
    c_direct = _event_context(
        world,
        800,
        name="DirectGame",
        public_name="Direct Match",
        format_type="Constructed",
        module="Join",
        fees=[_fee(world, 820, "Free", 0)],
    )

    contexts = world.add(
        20,
        "List",
        **{
            "$items": [
                Obj(c_human),
                Obj(c_bot),
                Obj(c_sealed),
                Obj(c_mwm),
                Obj(c_jump),
                Obj(c_queue),
                Obj(c_closed),
                Obj(c_direct),
            ]
        },
    )
    ev_mgr = world.add(21, "EventManager", EventContexts=Obj(contexts))
    by_name = {
        "PremierDraft_TST_20261126": c_human,
        "QuickDraft_TST_20261126": c_bot,
        "Sealed_TST_20261126": c_sealed,
        "Midweek_Magic_TST": c_mwm,
        "JumpIn_TST": c_jump,
    }
    world.results[(ev_mgr, "GetEventContext")] = lambda args: Obj(by_name[args[0]["str"]])
    world.statics["WrapperController.Instance"] = world.add(
        30, "WrapperController", EventManager=Obj(ev_mgr), InventoryManager=Obj(inv_mgr)
    )
    return world


def test_list_available_events_classifies_filters_and_checks_affordability():
    world = _events_world()
    res = MacBridgeAdapter(world.send).handle({"action": "list_available_events"})
    assert res["ok"] is True
    assert res["wallet"]["gold"] == 12000
    assert res["wallet"]["gems"] == 800
    assert res["wallet"]["draft_tokens"] == 1

    by_name = {ev["event_name"]: ev for ev in res["events"]}
    assert set(by_name) == {
        "PremierDraft_TST_20261126",
        "QuickDraft_TST_20261126",
        "Sealed_TST_20261126",
        "Midweek_Magic_TST",
        "JumpIn_TST",
    }
    assert by_name["PremierDraft_TST_20261126"]["category"] == "human_draft"
    premier_fees = {f["currency_type"]: f for f in by_name["PremierDraft_TST_20261126"]["entry_fees"]}
    assert premier_fees["Gold"]["affordable"] is True
    assert premier_fees["Gem"]["affordable"] is False
    assert premier_fees["DraftToken"]["affordable"] is True

    assert by_name["QuickDraft_TST_20261126"]["category"] == "bot_draft"
    assert by_name["QuickDraft_TST_20261126"]["in_progress"] is True
    assert by_name["Sealed_TST_20261126"]["category"] == "sealed"
    assert by_name["Midweek_Magic_TST"]["is_free"] is True
    assert by_name["Midweek_Magic_TST"]["max_prize_wins"] == 3
    assert by_name["JumpIn_TST"]["category"] == "jumpstart"


def test_get_screen_detects_table_ready_packet_deck_select_and_rewards_panel():
    world = World()
    world.finds["Wotc.Mtga.Wrapper.Draft.TableDraftQueueContentController"] = world.add(
        10, "TableDraftQueueContentController"
    )
    world.finds["Wotc.Mtga.Wrapper.PacketSelect.PacketSelectContentController"] = world.add(
        11, "PacketSelectContentController"
    )
    world.finds["ConstructedDeckSelectController"] = world.add(12, "ConstructedDeckSelectController")
    container = world.add(13, "GameObject", activeInHierarchy=False)
    world.finds["Core.Meta.MainNavigation.Store.ContentControllerRewards"] = world.add(
        14, "ContentControllerRewards", _container=Obj(container)
    )
    adapter = MacBridgeAdapter(world.send)
    scr = adapter.handle({"action": "get_screen"})
    assert scr["ok"] is True
    assert scr["table_draft_queue"] is True
    assert scr["packet_select"] is True
    assert scr["constructed_deck_select"] is True
    assert scr["rewards_panel"] is False

    world.objects[container]["activeInHierarchy"] = True
    scr2 = adapter.handle({"action": "get_screen"})
    assert scr2["rewards_panel"] is True


def test_table_draft_ready_clicks_ready_only_when_needed():
    world = World()
    btn_go = world.add(20, "GameObject", activeInHierarchy=True)
    btn = world.add(21, "CustomButton", Interactable=True, gameObject=Obj(btn_go))
    ctrl = world.add(
        22,
        "TableDraftQueueContentController",
        _draftId="draft-pod-1",
        _isLocalPlayerReady=False,
        _prevNumInPod=8,
        _readyButton=Obj(btn),
    )
    world.finds["Wotc.Mtga.Wrapper.Draft.TableDraftQueueContentController"] = ctrl
    adapter = MacBridgeAdapter(world.send)

    state = adapter.handle({"action": "get_table_draft_state"})
    assert state["needs_ready"] is True

    res = adapter.handle({"action": "ready_table_draft"})
    assert res["ok"] is True and res["submitted_type"] == "TableDraftReady"
    assert (22, "HandleReadyButtonClicked", []) in world.calls

    # Subsequent call when already ready is a safe no-op.
    world.objects[ctrl]["_isLocalPlayerReady"] = True
    res2 = adapter.handle({"action": "ready_table_draft"})
    assert res2["ok"] is True and res2.get("already_ready") is True
    assert len(world.calls) == 1


def test_enter_event_enforces_free_and_paid_guards():
    world = _events_world()
    adapter = MacBridgeAdapter(world.send)

    # Free Midweek Magic enters when require_free=True.
    ok_free = adapter.handle(
        {
            "action": "enter_event",
            "event_name": "Midweek_Magic_TST",
            "currency_type": "Free",
            "expected_quantity": 0,
            "require_free": True,
        }
    )
    assert ok_free["ok"] is True
    assert any(m == "JoinAndPay" for _h, m, _a in world.calls)

    # Paid Premier Draft is blocked when require_free=True.
    blocked = adapter.handle(
        {
            "action": "enter_event",
            "event_name": "PremierDraft_TST_20261126",
            "currency_type": "Gold",
            "expected_quantity": 10000,
            "require_free": True,
        }
    )
    assert blocked["ok"] is False
    assert "without explicit approval" in blocked["error"]

    # Unaffordable Gems entry is blocked even when require_free=False.
    unaffordable = adapter.handle(
        {
            "action": "enter_event",
            "event_name": "PremierDraft_TST_20261126",
            "currency_type": "Gem",
            "expected_quantity": 1500,
            "require_free": False,
        }
    )
    assert unaffordable["ok"] is False
    assert "unaffordable" in unaffordable["error"].lower()

    # Approved affordable Gold entry succeeds.
    ok_paid = adapter.handle(
        {
            "action": "enter_event",
            "event_name": "PremierDraft_TST_20261126",
            "currency_type": "Gold",
            "expected_quantity": 10000,
            "require_free": False,
        }
    )
    assert ok_paid["ok"] is True


def test_entry_approval_store_is_single_use_and_exact():
    now = [100.0]
    store = EntryApprovalStore(clock=lambda: now[0])
    tok = store.mint("PremierDraft_TST", "Gold", 10000)
    assert store.consume(tok, "PremierDraft_TST", "Gem", 1500) is False
    assert store.consume(tok, "OtherEvent", "Gold", 10000) is False
    assert store.consume(tok, "PremierDraft_TST", "Gold", 10000) is True
    # Single-use: second consume fails.
    assert store.consume(tok, "PremierDraft_TST", "Gold", 10000) is False


def test_jumpstart_and_constructed_deck_choosers():
    opts = [
        {"packet_id": "p1", "name": "Boros", "colors": ["R", "W"]},
        {"packet_id": "p2", "name": "Vampires", "colors": ["B"]},
        {"packet_id": "p3", "name": "Goblins", "colors": ["R"]},
    ]
    first = choose_jumpstart_packet(opts, [])
    assert first is not None and len(first["colors"]) == 1
    second = choose_jumpstart_packet(
        [
            {"packet_id": "p4", "name": "Merfolk", "colors": ["U"]},
            {"packet_id": "p5", "name": "Zombies", "colors": ["B"]},
        ],
        [{"packet_id": "p2", "name": "Vampires", "colors": ["B"]}],
    )
    assert second is not None and second["packet_id"] == "p5"

    decks = [
        {"deck_id": DECK_GUID_1, "name": "Hist Deck", "format": "Historic"},
        {"deck_id": DECK_GUID_2, "name": "Std Deck", "format": "Standard"},
    ]
    picked = choose_constructed_event_deck(decks, "Standard")
    assert picked is not None and picked["deck_id"] == DECK_GUID_2


def test_driver_paid_draft_lifecycle_and_table_ready_then_prompts():
    bridge, commands = _scripted_bridge(
        [
            {"ok": True, "home": True},
            {"ok": True, "event_page": True},
            {"ok": True, "table_draft_queue": True},
            {"ok": True, "event_page": True},
            {"ok": True, "rewards_panel": True},
            {"ok": True, "event_page": True},
        ],
        get_event_page=[
            {
                "is_open": True,
                "event_name": "PremierDraft_TST",
                "display_name": "Premier Draft",
                "module": "PayEntry",
                "is_free": False,
                "current_wins": 0,
                "current_losses": 0,
                "max_prize_wins": 7,
                "entry_fees": [{"currency_type": "Gold", "quantity": 10000, "affordable": True}],
            },
            {
                "is_open": True,
                "event_name": "PremierDraft_TST",
                "display_name": "Premier Draft",
                "module": "ClaimPrize",
                "is_free": False,
                "current_wins": 7,
                "current_losses": 1,
                "max_prize_wins": 7,
                "entry_fees": [{"currency_type": "Gold", "quantity": 10000, "affordable": True}],
            },
            {
                "is_open": True,
                "event_name": "PremierDraft_TST",
                "display_name": "Premier Draft",
                "module": "Complete",
                "is_free": False,
                "current_wins": 7,
                "current_losses": 1,
                "max_prize_wins": 7,
                "entry_fees": [{"currency_type": "Gold", "quantity": 10000, "affordable": True}],
            },
        ],
        get_table_draft_state={"is_open": True, "needs_ready": True, "draft_id": "pod-1"},
        ready_table_draft={"ok": True, "submitted_type": "TableDraftReady"},
        enter_event={"ok": True, "submitted_type": "EventEnter"},
        dismiss_rewards_panel={"ok": True, "submitted_type": "RewardsClaimDismiss"},
        go_to_event={"ok": True},
        event_play={"ok": True},
    )
    approvals = EntryApprovalStore()
    token = approvals.mint("PremierDraft_TST", "Gold", 10000)
    cfg = EventQueueConfig(
        event_name="PremierDraft_TST",
        display_name="Premier Draft",
        category="human_draft",
        is_free=False,
        entry_currency="Gold",
        entry_quantity=10000,
    )
    completed: list[dict] = []
    driver = DraftEventDriver(
        bridge_fn=lambda: bridge,
        tracker_fn=lambda: None,
        primer_service=SimpleNamespace(get=lambda *_: None, quick=lambda *_: None, ensure=lambda *_: None),
        event_mode_fn=lambda: cfg,
        consume_entry_approval_fn=lambda ev, cur, qty: approvals.consume(token, ev, cur, qty),
        on_run_complete_fn=completed.append,
    )
    driver.set_enabled(True)
    for _ in range(6):
        driver._step()

    actions = [c[0] for c in commands]
    assert "go_to_event" in actions
    assert "enter_event" in actions
    assert "ready_table_draft" in actions
    assert "dismiss_rewards_panel" in actions
    assert driver.run.finished is True
    assert driver.run.awaiting_reentry_decision is True
    assert len(completed) == 1
    assert completed[0]["outcome"] == "won_main_prize"
    assert completed[0]["wins"] == 7


def test_driver_free_event_auto_reenters_when_enabled_and_stops_on_win_no_gate_prize():
    # 1. Free repeatable course returning to Join with reenter_free=True
    bridge, commands = _scripted_bridge(
        [
            {"ok": True, "event_page": True},
            {"ok": True, "event_page": True},
        ],
        get_event_page=[
            {
                "is_open": True,
                "event_name": "Free_Event_TST",
                "module": "WinLossGate",
                "is_free": True,
                "current_wins": 3,
                "current_losses": 0,
                "max_prize_wins": 3,
            },
            {
                "is_open": True,
                "event_name": "Free_Event_TST",
                "module": "Join",
                "is_free": True,
                "current_wins": 0,
                "current_losses": 0,
                "max_prize_wins": 3,
            },
        ],
        event_play={"ok": True},
        enter_event={"ok": True},
    )
    cfg = EventQueueConfig(
        event_name="Free_Event_TST",
        is_free=True,
        entry_currency="Free",
        entry_quantity=0,
        reenter_free=True,
    )
    completed: list[dict] = []
    driver = DraftEventDriver(
        bridge_fn=lambda: bridge,
        tracker_fn=lambda: None,
        primer_service=SimpleNamespace(get=lambda *_: None, quick=lambda *_: None, ensure=lambda *_: None),
        event_mode_fn=lambda: cfg,
        on_run_complete_fn=completed.append,
    )
    driver.set_enabled(True)
    driver._step()
    driver._step()
    assert (
        "enter_event",
        {
            "event_name": "Free_Event_TST",
            "currency_type": "Free",
            "expected_quantity": 0,
            "event_choice": "",
            "require_free": True,
            "timeout": 10.0,
        },
    ) in commands
    assert completed == []

    # 2. WinNoGate (Midweek Magic) stops and asks at main prize (3 wins)
    bridge2, commands2 = _scripted_bridge(
        [{"ok": True, "event_page": True}],
        get_event_page={
            "is_open": True,
            "event_name": "Midweek_Magic_TST",
            "display_name": "Midweek Magic",
            "module": "WinNoGate",
            "is_free": True,
            "current_wins": 3,
            "current_losses": 1,
            "max_prize_wins": 3,
            "has_unclaimed_rewards": False,
        },
    )
    cfg2 = EventQueueConfig(event_name="Midweek_Magic_TST", is_free=True, stop_at_main_prize=True)
    completed2: list[dict] = []
    driver2 = DraftEventDriver(
        bridge_fn=lambda: bridge2,
        tracker_fn=lambda: None,
        primer_service=SimpleNamespace(get=lambda *_: None, quick=lambda *_: None, ensure=lambda *_: None),
        event_mode_fn=lambda: cfg2,
        on_run_complete_fn=completed2.append,
    )
    driver2.set_enabled(True)
    driver2._step()
    assert not any(c[0] == "event_play" for c in commands2)
    assert driver2.run.finished is True
    assert completed2 and completed2[0]["outcome"] == "won_main_prize"


def test_driver_jumpstart_submits_packets_and_constructed_submits_deck():
    bridge, commands = _scripted_bridge(
        [{"ok": True, "packet_select": True}, {"ok": True, "constructed_deck_select": True}],
        get_packet_options={
            "is_open": True,
            "options": [
                {"packet_id": "pkt_a", "name": "Elves", "colors": ["G"]},
                {"packet_id": "pkt_b", "name": "Soldiers", "colors": ["W", "U"]},
            ],
            "submitted": [],
        },
        submit_packet={"ok": True},
        get_constructed_deck_select={
            "is_open": True,
            "event_name": "Standard_Event",
            "deck_select_format": "Standard",
            "decks": [{"deck_id": DECK_GUID_1, "name": "Mono Red", "format": "Standard"}],
        },
        select_event_deck={"ok": True},
    )
    cfg = EventQueueConfig(event_name="JumpIn_TST", is_free=True, deck_id=DECK_GUID_1)
    driver = DraftEventDriver(
        bridge_fn=lambda: bridge,
        tracker_fn=lambda: None,
        primer_service=SimpleNamespace(get=lambda *_: None, quick=lambda *_: None, ensure=lambda *_: None),
        event_mode_fn=lambda: cfg,
    )
    driver.set_enabled(True)
    driver._step()
    driver._step()
    assert ("submit_packet", {"packet_id": "pkt_a", "timeout": 8.0}) in commands
    assert (
        "select_event_deck",
        {"event_name": "Standard_Event", "deck_id": DECK_GUID_1, "timeout": 10.0},
    ) in commands


def test_auto_queue_draft_mode_skips_recent_match_and_loss_guard():
    class _FakeSettings(dict):
        def set(self, key, val):
            self[key] = val

    class _Runtime(_DraftEventMixin, _AutoQueueMixin):
        def __init__(self):
            self.settings = _FakeSettings({"auto_queue_mode": "draft", "auto_queue_enabled": True})
            self._auto_queue_enabled = True
            self._autopilot_dry_run = False
            self._autopilot_afk = False
            self.ui = SimpleNamespace(status=lambda *_: None, log=lambda *_: None)

        def _autopilot_control_status(self):
            return "AP:ON"

        def _check_grind_availability(self):
            return True

    rt = _Runtime()
    nav = Mock()
    rt._auto_queue_navigator = nav
    # Even with fake bridge unavailable check mocked out, draft mode bypasses AutoQueueNavigator.
    import arenamcp.bridge_readiness as br

    orig = br.automation_blocker
    br.automation_blocker = lambda *_: ""
    try:
        assert rt._poll_auto_queue({}) is False
    finally:
        br.automation_blocker = orig
    nav.set_enabled.assert_called_with(False)

    # Disabling auto-queue clears unused paid approvals.
    tok = rt._event_approval_store().mint("PremierDraft_TST", "Gold", 10000)
    rt._pending_entry_token = tok
    rt.set_auto_queue(False)
    assert rt._consume_event_entry_approval("PremierDraft_TST", "Gold", 10000) is False


def test_pipe_adapter_and_dialogs_round_trip(qtbot=None):
    emitted: list[dict] = []
    adapter = PipeAdapter.__new__(PipeAdapter)
    adapter._emit = lambda payload: emitted.append(payload)
    adapter._coach = SimpleNamespace(
        set_auto_queue_mode=Mock(),
        list_available_events=Mock(),
        configure_event_queue=Mock(),
        respond_event_reentry=Mock(),
    )
    adapter._dispatch({"cmd": "set_auto_queue_mode", "mode": "draft"})
    adapter._coach.set_auto_queue_mode.assert_called_once_with("draft")
    adapter._dispatch({"cmd": "configure_event_queue", "config": {"event_name": "QuickDraft_TST"}})
    adapter._coach.configure_event_queue.assert_called_once_with({"event_name": "QuickDraft_TST"})
    adapter._dispatch(
        {
            "cmd": "respond_event_reentry",
            "request_id": "req1",
            "action": "reenter",
            "currency": "Gold",
            "quantity": 5000,
        }
    )
    adapter._coach.respond_event_reentry.assert_called_once_with("req1", "reenter", "Gold", 5000)

    adapter.emit_available_events({"ok": True, "events": []})
    adapter.emit_event_queue_state({"mode": "draft", "enabled": True})
    adapter.emit_event_reentry_prompt({"request_id": "req1", "event_name": "QuickDraft_TST"})
    assert [e["type"] for e in emitted] == [
        "available_events",
        "event_queue_state",
        "event_reentry_prompt",
    ]

    # Verify Qt dialogs populate and emit signals cleanly.
    from PySide6.QtWidgets import QApplication

    from arenamcp.desktop.event_mode_dialog import EventModeDialog, EventReentryDialog

    _app = QApplication.instance() or QApplication([])
    dlg = EventModeDialog()
    dlg.set_decks([{"deck_id": DECK_GUID_1, "name": "Boros", "format": "Standard"}])
    dlg.update_events(
        {
            "ok": True,
            "wallet": {"gold": 12000, "gems": 800, "draft_tokens": 1, "sealed_tokens": 0},
            "events": [
                {
                    "event_name": "Midweek_Magic_TST",
                    "display_name": "Midweek Magic",
                    "category": "precon_event",
                    "is_free": True,
                    "in_progress": False,
                    "max_prize_wins": 3,
                    "loss_details_type": "PlayUntilEventEnds",
                    "entry_fees": [
                        {"currency_type": "Free", "quantity": 0, "is_free": True, "affordable": True}
                    ],
                }
            ],
        }
    )
    configured: list[dict] = []
    dlg.eventConfigured.connect(configured.append)
    dlg._on_start_clicked()
    assert len(configured) == 1
    assert configured[0]["event_name"] == "Midweek_Magic_TST"
    assert configured[0]["confirm_paid"] is False

    reentry = EventReentryDialog(
        {
            "request_id": "r42",
            "event_name": "PremierDraft_TST",
            "display_name": "Premier Draft",
            "outcome": "won_main_prize",
            "wins": 7,
            "losses": 1,
            "is_free": False,
            "entry_fees": [
                {"currency_type": "Gold", "quantity": 10000, "is_free": False, "affordable": True}
            ],
        }
    )
    responses: list[tuple] = []
    reentry.responded.connect(lambda *args: responses.append(args))
    reentry._finish("reenter", "Gold", 10000)
    assert responses == [("r42", "reenter", "Gold", 10000)]

    # Verify BEPINEX_REFLECT_COMMANDS includes all new commands.
    for cmd_name in (
        "list_available_events",
        "get_table_draft_state",
        "ready_table_draft",
        "enter_event",
        "dismiss_rewards_panel",
        "get_constructed_deck_select",
        "select_event_deck",
        "submit_event_choice",
        "get_packet_options",
        "submit_packet",
        "resign_event",
    ):
        assert cmd_name in BEPINEX_REFLECT_COMMANDS


def test_non_home_screen_and_autoplay_off_guidance():
    # 1. DraftEventDriver on a non-Home screen and on a different event_page gives clear Home guidance.
    statuses: list[str] = []
    bridge, _ = _scripted_bridge(
        [
            {"ok": True},  # e.g. Decks / Store screen (home=False, event_page=False)
            {"ok": True, "event_page": True},  # on a different event's page
        ],
        get_event_page=[
            {
                "is_open": True,
                "event_name": "QuickDraft_Other",
                "display_name": "Quick Draft",
                "module": "Join",
            }
        ],
    )
    cfg = EventQueueConfig(
        event_name="PremierDraft_TST",
        display_name="Premier Draft",
        category="human_draft",
        is_free=False,
        entry_currency="Gold",
        entry_quantity=10000,
    )
    driver = DraftEventDriver(
        bridge_fn=lambda: bridge,
        tracker_fn=lambda: None,
        primer_service=SimpleNamespace(get=lambda *_: None, quick=lambda *_: None, ensure=lambda *_: None),
        status_fn=statuses.append,
        event_mode_fn=lambda: cfg,
    )
    driver.set_enabled(True)
    driver._step()
    assert any("Home" in s and "Premier Draft" in s for s in statuses)

    driver._step()
    assert any("Quick Draft" in s and "click Home" in s for s in statuses)

    # 2. Control: when on Home screen, driver opens target event instead of emitting non-Home warning.
    home_statuses: list[str] = []
    home_bridge, home_cmds = _scripted_bridge(
        [{"ok": True, "home": True}],
        go_to_event={"ok": True},
    )
    home_driver = DraftEventDriver(
        bridge_fn=lambda: home_bridge,
        tracker_fn=lambda: None,
        primer_service=SimpleNamespace(get=lambda *_: None, quick=lambda *_: None, ensure=lambda *_: None),
        status_fn=home_statuses.append,
        event_mode_fn=lambda: cfg,
    )
    home_driver.set_enabled(True)
    home_driver._step()
    assert any(c[0] == "go_to_event" for c in home_cmds)
    assert not any("Waiting for Arena's Home screen" in s for s in home_statuses)

    # 3. _DraftEventMixin surfaces Autoplay-off blocker into AUTO_QUEUE_DETAIL,
    #    and AutoQueueNavigator idle Home wait tells user to click Home.
    emitted_status: list[tuple[str, str]] = []

    class _FakeSettings(dict):
        def set(self, k, v):
            self[k] = v

    class _Rt(_DraftEventMixin, _AutoQueueMixin):
        def __init__(self):
            self.settings = _FakeSettings({"auto_queue_mode": "draft", "auto_queue_enabled": True})
            self._auto_queue_enabled = True
            self._autopilot_enabled = False
            self._autopilot_dry_run = False
            self._autopilot_afk = False
            self._event_queue_config = cfg
            self.ui = SimpleNamespace(status=lambda k, v: emitted_status.append((k, v)), log=lambda *_: None)

        def _autopilot_control_status(self):
            return "AP:OFF"

    rt = _Rt()
    assert rt._poll_draft_event() is False
    assert any(k == "AUTO_QUEUE_DETAIL" and "Autoplay on" in v for k, v in emitted_status)

    from arenamcp.auto_queue import AutoQueueNavigator

    nav_statuses: list[str] = []
    nav = AutoQueueNavigator(
        backend=None,
        get_game_state=lambda: {},
        status_fn=nav_statuses.append,
    )
    nav._idle_grind = True
    nav._stage = "home"
    nav._progress_at = 10**12  # fresh wait (not timed out)
    idle_bridge = SimpleNamespace(
        connected=True,
        get_match_end=lambda: {"is_open": False, "home_active": False},
        leave_match=lambda: True,
    )
    assert nav._leave_result_via_bridge(idle_bridge, 1, None) is True
    assert any("click Home in Arena" in s for s in nav_statuses)


def test_list_available_events_reports_on_home_and_validates_constructed_decks():
    from arenamcp.event_bridge import _fee_affordable

    # Currency normalization: 'Gems' and 'CurrencyType_Gold', plus non-zero Free/None rejected.
    wallet = {"gold": 5000, "gems": 1000, "draft_tokens": 0, "sealed_tokens": 0, "custom_tokens": {}}
    assert _fee_affordable("Gems", 1000, True, "", wallet) is True
    assert _fee_affordable("Gems", 1500, True, "", wallet) is False
    assert _fee_affordable("CurrencyType_Gold", 5000, True, "", wallet) is True
    assert _fee_affordable("None", 100, True, "", wallet) is False

    world = _events_world()
    # Add a Standard Constructed event and a StandardBrawl + Standard deck via DecksManager.
    c_std = _event_context(
        world,
        900,
        name="Standard_Event_TST",
        public_name="Standard Event",
        format_type="Constructed",
        module="Join",
        deck_fmt="Standard",
        fees=[_fee(world, 920, "Gold", 2500)],
    )
    ev_mgr_h = world.objects[30]["EventManager"].handle
    list_h = world.objects[ev_mgr_h]["EventContexts"].handle
    world.objects[list_h]["$items"].append(Obj(c_std))

    brawl_sum = world.add(
        950,
        "ClientDeckSummary",
        DeckId=Obj(world.add(951, "Guid", **_guid_fields(DECK_GUID_1))),
        Name="Brawl Deck",
        Format="StandardBrawl",
    )
    std_sum_ok = world.add(
        952,
        "ClientDeckSummary",
        DeckId=Obj(world.add(953, "Guid", **_guid_fields(DECK_GUID_2))),
        Name="Valid Std",
        Format="Standard",
    )
    d_brawl = world.add(954, "Client_Deck", Summary=Obj(brawl_sum))
    d_std = world.add(955, "Client_Deck", Summary=Obj(std_sum_ok))
    cached_list = world.add(956, "List", **{"$items": [Obj(d_brawl), Obj(d_std)]})
    dm = world.add(957, "DecksManager")
    world.results[(dm, "GetAllCachedDecks")] = Obj(cached_list)
    world.objects[30]["DecksManager"] = Obj(dm)

    # Without HomePageContentController -> on_home=False; with DeckViewInfo validation unavailable -> format fallback.
    res_no_home = MacBridgeAdapter(world.send).handle({"action": "list_available_events"})
    assert res_no_home["ok"] is True
    assert res_no_home["on_home"] is False
    std_ev = next(ev for ev in res_no_home["events"] if ev["event_name"] == "Standard_Event_TST")
    assert [d["deck_id"] for d in std_ev["eligible_decks"]] == [DECK_GUID_2]

    # Now add HomePageContentController -> on_home=True.
    world.finds["HomePageContentController"] = world.add(960, "HomePageContentController")
    res_home = MacBridgeAdapter(world.send).handle({"action": "list_available_events"})
    assert res_home["on_home"] is True


def test_format_matching_and_event_eligibility_gating_and_controls():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from arenamcp.desktop.event_mode_dialog import EventModeDialog
    from arenamcp.event_queue import event_eligibility, format_matches

    # Strict format matching avoids 'Standard' matching 'StandardBrawl' and empty deck format.
    assert format_matches("Standard", "Standard") is True
    assert format_matches("Pioneer", "Explorer") is True
    assert format_matches("Standard", "StandardBrawl") is False
    assert format_matches("Standard", "") is False
    # No fallback to illegal format decks in choose_constructed_event_deck.
    assert (
        choose_constructed_event_deck(
            [{"deck_id": DECK_GUID_1, "name": "Brawl", "format": "StandardBrawl"}], "Standard"
        )
        is None
    )

    # UI gating: unaffordable event, missing-fee non-free event, and no-deck constructed event
    # must all be unselectable (NoItemFlags) and disable Start, while in-progress/affordable/matching pass.
    _app = QApplication.instance() or QApplication([])
    dlg = EventModeDialog()
    dlg.set_decks([{"deck_id": DECK_GUID_1, "name": "Hist", "format": "Historic", "complete": True}])
    dlg.update_events(
        {
            "ok": True,
            "on_home": False,
            "wallet": {"gold": 1000, "gems": 100, "draft_tokens": 0, "sealed_tokens": 0},
            "events": [
                {
                    "event_name": "Sealed_Expensive",
                    "display_name": "Expensive Sealed",
                    "category": "sealed",
                    "is_free": False,
                    "in_progress": False,
                    "entry_fees": [
                        {"currency_type": "Gem", "quantity": 2000, "is_free": False, "affordable": False}
                    ],
                },
                {
                    "event_name": "No_Fees_Mystery",
                    "display_name": "Mystery Event",
                    "category": "human_draft",
                    "is_free": False,
                    "in_progress": False,
                    "entry_fees": [],
                },
                {
                    "event_name": "Standard_Challenge",
                    "display_name": "Standard Challenge",
                    "category": "constructed_event",
                    "is_free": True,
                    "in_progress": False,
                    "deck_select_format": "Standard",
                    "entry_fees": [
                        {"currency_type": "Free", "quantity": 0, "is_free": True, "affordable": True}
                    ],
                },
                {
                    "event_name": "QuickDraft_Active",
                    "display_name": "Quick Draft (Active)",
                    "category": "bot_draft",
                    "is_free": False,
                    "in_progress": True,
                    "current_wins": 1,
                    "current_losses": 0,
                    "entry_fees": [
                        {"currency_type": "Gold", "quantity": 5000, "is_free": False, "affordable": False}
                    ],
                },
            ],
        }
    )

    items_by_event = {}
    for i in range(dlg.event_list.count()):
        item = dlg.event_list.item(i)
        data = item.data(Qt.UserRole)
        if isinstance(data, dict):
            items_by_event[data["event_name"]] = item

    # Disabled unselectable events:
    assert items_by_event["Sealed_Expensive"].flags() == Qt.NoItemFlags
    assert "Insufficient funds" in items_by_event["Sealed_Expensive"].text()
    assert items_by_event["No_Fees_Mystery"].flags() == Qt.NoItemFlags
    assert "Entry fee unavailable" in items_by_event["No_Fees_Mystery"].text()
    assert items_by_event["Standard_Challenge"].flags() == Qt.NoItemFlags
    assert "No qualifying Standard deck" in items_by_event["Standard_Challenge"].text()

    # Control: in-progress paid event IS eligible and auto-selected, and on_home=False shows banner.
    assert items_by_event["QuickDraft_Active"].flags() != Qt.NoItemFlags
    assert dlg._selected_event()["event_name"] == "QuickDraft_Active"
    assert dlg.start_btn.isEnabled() is True
    assert dlg.start_btn.text() == "Resume Event"
    assert "Home" in dlg.status_label.text() and not dlg.status_label.isHidden()

    # Direct check: force-rendering an ineligible event disables Start button cleanly and blocks _on_start_clicked.
    emitted: list[dict] = []
    dlg.eventConfigured.connect(emitted.append)
    dlg._render_selected_event(items_by_event["Standard_Challenge"].data(Qt.UserRole))
    assert dlg.start_btn.isEnabled() is False
    assert "No qualifying Standard deck" in dlg.start_btn.text()
    assert dlg.deck_combo.currentText() == "No qualifying deck found"
    assert event_eligibility(items_by_event["Standard_Challenge"].data(Qt.UserRole), dlg._decks)[0] is False

    # Adding a qualifying Standard deck unlocks Standard_Challenge dynamically!
    dlg.set_decks(
        [
            {"deck_id": DECK_GUID_1, "name": "Hist", "format": "Historic", "complete": True},
            {"deck_id": DECK_GUID_2, "name": "Std Winners", "format": "Standard", "complete": True},
        ]
    )
    items_after = {}
    for i in range(dlg.event_list.count()):
        item = dlg.event_list.item(i)
        data = item.data(Qt.UserRole)
        if isinstance(data, dict):
            items_after[data["event_name"]] = item
    assert items_after["Standard_Challenge"].flags() != Qt.NoItemFlags

    # Backend configure_event_queue rejects unaffordable or no-deck events when live cache knows them.
    rt_statuses: list[tuple[str, str]] = []

    class _CfgRt(_DraftEventMixin):
        def __init__(self):
            self._last_available_events = {
                "Sealed_Expensive": items_after["Sealed_Expensive"].data(Qt.UserRole),
                "Standard_Challenge": items_after["Standard_Challenge"].data(Qt.UserRole),
            }
            self.ui = SimpleNamespace(status=lambda k, v: rt_statuses.append((k, v)), log=lambda *_: None)

        def collect_decks(self):
            return {}, ""

    cfg_rt = _CfgRt()
    import pytest

    with pytest.raises(ValueError, match="Insufficient funds"):
        cfg_rt.configure_event_queue(
            {
                "event_name": "Sealed_Expensive",
                "is_free": False,
                "entry_currency": "Gem",
                "entry_quantity": 2000,
                "confirm_paid": True,
            }
        )
    with pytest.raises(ValueError, match="No qualifying Standard deck"):
        cfg_rt.configure_event_queue(
            {
                "event_name": "Standard_Challenge",
                "is_free": True,
                "entry_currency": "Free",
                "entry_quantity": 0,
            }
        )
    assert any(k == "AUTO_QUEUE_DETAIL" and "Cannot start" in v for k, v in rt_statuses)
