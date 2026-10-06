"""Draft-event autoplay: set primer, pick ranking, course tracking, bridge hands, driver."""

from __future__ import annotations

import json
import time
from collections import Counter
from types import SimpleNamespace
from typing import Any

import pytest

from arenamcp.draft_autopick import choose_picks, pool_lane, rank_pack
from arenamcp.draft_event import DraftEventDriver, deck_entries
from arenamcp.event_course import CourseTracker
from arenamcp.mac_bridge_adapter import MacBridgeAdapter
from arenamcp.parser import LogParser
from arenamcp.set_primer import (
    SetPrimer,
    SetPrimerService,
    data_primer,
    mana_value,
    merge_synthesis,
    synthesize,
)

# ---------------------------------------------------------------------------
# A small 17lands-shaped set: two good blue cards, two good red, filler, a trap.
# ---------------------------------------------------------------------------


def rating(grp_id, name, color, rarity="C", gih=0.55, ata=6.0, games=1000):
    return {
        "mtga_id": grp_id,
        "name": name,
        "color": color,
        "rarity": {"C": "common", "U": "uncommon", "R": "rare", "M": "mythic"}[rarity],
        "types": ["Creature"],
        "ever_drawn_win_rate": gih,
        "drawn_improvement_win_rate": gih - 0.55,
        "avg_seen": ata + 1,
        "avg_pick": ata,
        "opening_hand_win_rate": gih,
        "ever_drawn_game_count": games,
    }


RATINGS = [
    rating(1, "Blue Ace", "U", "U", 0.62, 2.0),
    rating(2, "Blue Common", "U", "C", 0.58, 4.0),
    rating(3, "Red Ace", "R", "U", 0.61, 2.0),
    rating(4, "Red Common", "R", "C", 0.57, 4.0),
    rating(5, "Izzet Signpost", "UR", "U", 0.60, 3.0),
    rating(6, "Shiny Trap", "B", "R", 0.45, 1.5),
    *[rating(10 + i, f"Filler {i}", "WUBRG"[i % 5], "C", 0.50 + (i % 4) * 0.01, 8.0) for i in range(20)],
]
PAIRS = {
    "UR": SimpleNamespace(win_rate=0.58, games=9000),
    "WB": SimpleNamespace(win_rate=0.52, games=7000),
    "BG": SimpleNamespace(win_rate=0.55, games=8000),
}


@pytest.fixture
def primer() -> SetPrimer:
    return data_primer("TST", RATINGS, PAIRS, lambda grp, name: {"oracle_text": f"{name} text", "cmc": 3.0})


def test_data_primer_scores_cards_and_ranks_archetypes(primer):
    assert primer.card(1).baseline > primer.card(10).baseline > primer.card(6).baseline
    izzet = primer.archetype("RU")
    assert izzet["tier"] == 1 and "Izzet Signpost" in izzet["payoffs"]
    assert {"Blue Common", "Red Common"} <= set(izzet["key_commons"])
    assert [trap["card"] for trap in primer.traps] == ["Shiny Trap"]
    assert SetPrimer.from_json(primer.to_json()).card(5).name == "Izzet Signpost"


def test_mana_value_reads_arena_and_scryfall_costs():
    assert mana_value("o2oUoU") == 4.0
    assert mana_value("{X}{R}{R}") == 2.0
    assert mana_value("") is None


def test_model_synthesis_is_validated_against_the_set(primer):
    merged = merge_synthesis(
        primer,
        {
            "overview": "Tempo format.",
            "speed": "fast",
            "archetypes": [
                {
                    "colors": "UR",
                    "name": "Spells",
                    "plan": "Cast spells.",
                    "tier": 1,
                    "payoffs": ["Izzet Signpost", "Invented Card"],
                    "enablers": ["Blue Common"],
                },
                {"colors": "WB", "name": "Drain", "plan": "Drain.", "payoffs": ["Filler 0"]},
                {"colors": "BG", "name": "Grind", "plan": "Grind.", "payoffs": ["Filler 2"]},
                {"colors": "XX", "name": "Bogus"},
            ],
            "synergy_notes": [
                {"cards": ["Blue Common", "Izzet Signpost"], "why": "cheap spells"},
                {"cards": ["Invented Card", "Blue Ace"], "why": "made up"},
            ],
            "pick_principles": ["Removal early"],
            "traps": [{"card": "Shiny Trap", "why": "slow"}, {"card": "Nope", "why": "x"}],
        },
    )
    izzet = merged.archetype("UR")
    assert izzet["payoffs"] == ["Izzet Signpost"] and izzet["enablers"] == ["Blue Common"]
    assert len(merged.synergy_notes) == 1 and merged.source == "model" and merged.speed == "fast"
    assert [trap["card"] for trap in merged.traps] == ["Shiny Trap"]
    with pytest.raises(ValueError):
        merge_synthesis(primer, {"archetypes": [{"colors": "UR", "payoffs": []}]})


class FakeBackend:
    def __init__(self, answers):
        self.answers = answers
        self.calls = 0

    def complete(self, system, message, max_tokens, **kwargs):
        self.calls += 1
        for marker, answer in self.answers.items():
            if marker in message:
                return answer() if callable(answer) else answer
        return "not json"


def test_per_archetype_synthesis_merges_valid_pieces_and_keeps_data_for_the_rest(primer):
    izzet = json.dumps(
        {
            "name": "Izzet Spells",
            "plan": "Cheap spells.",
            "tier": 1,
            "payoffs": ["Izzet Signpost"],
            "enablers": ["Blue Common", "Red Common"],
            "synergy_notes": [{"cards": ["Blue Common", "Izzet Signpost"], "why": "spells"}],
        }
    )
    fmt = json.dumps(
        {
            "overview": "Fast.",
            "speed": "fast",
            "pick_principles": ["Two drops"],
            "traps": [{"card": "Shiny Trap", "why": "bad"}],
        }
    )
    backend = FakeBackend({"Archetype Izzet (UR)": izzet, "Two-color win rates": fmt})
    result = synthesize(primer, backend, workers=1)
    assert result.archetype("UR")["plan"] == "Cheap spells."
    assert result.archetype("WB")["plan"] == ""  # failed piece keeps the data version
    assert result.overview == "Fast." and result.synergy_notes and result.source == "model"


def test_primer_service_builds_once_caches_to_disk_and_offers_a_quick_version(tmp_path):
    service = SetPrimerService(
        backend_fn=lambda: None,
        ratings_fn=lambda code: RATINGS,
        color_stats_fn=lambda code: PAIRS,
        cache_dir=tmp_path,
    )
    assert service.get("TST") is None
    assert service.quick("TST").card(1).name == "Blue Ace"
    built = service.build_now("tst")
    assert built is not None and (tmp_path / "TST.json").exists()
    fresh = SetPrimerService(
        backend_fn=lambda: None, ratings_fn=None, color_stats_fn=None, cache_dir=tmp_path
    )
    assert fresh.get("TST").card(3).name == "Red Ace"


# ---------------------------------------------------------------------------
# Pick ranking
# ---------------------------------------------------------------------------


def test_ranking_takes_quality_early_and_stays_in_lane_late(primer):
    first = rank_pack([1, 4, 12], [], primer, pack_number=1, pick_number=1)
    assert first[0].grp_id == 1
    blue_pool = [1, 2, 1, 2, 5, 1, 2, 1, 2, 1, 2, 1, 2, 1, 2]
    assert pool_lane(blue_pool, primer).colors[0] == "U"
    late = rank_pack([3, 2], blue_pool, primer, pack_number=2, pick_number=3)
    assert late[0].grp_id in (2, 3) and all(pick.grp_id in (2, 3) for pick in late)
    assert any("trap" in reason for pick in rank_pack([6], [], primer) for reason in pick.reasons)


@pytest.mark.parametrize("basic", ["Plains", "Island", "Swamp", "Mountain", "Forest"])
@pytest.mark.parametrize("has_primer", [False, True])
def test_unrated_basics_lose_to_even_weak_off_color_spells(primer, basic, has_primer):
    # Live P1p10/P1p13: a Forest absent from 17lands got the unknown-card
    # prior and beat weak spells. Basic lands are freely available later.
    picks = choose_picks(
        [999, 6],
        [1, 2] * 10,
        primer if has_primer else None,
        names={999: basic, 6: "Shiny Trap"},
    )
    assert picks[0].grp_id == 6


def test_basics_can_be_taken_when_only_basics_remain(primer):
    names = {998: "Island", 999: "Forest"}
    picks = choose_picks([998, 999], [], primer, picks_required=2, names=names)
    assert {p.grp_id for p in picks} == {998, 999}
    picks = choose_picks([998, 6, 999], [], primer, picks_required=2, names=names)
    assert picks[0].grp_id == 6 and picks[1].grp_id in names


def test_pick_two_takes_two_distinct_pack_cards(primer):
    picks = choose_picks([1, 3, 12, 13], [], primer, picks_required=2)
    assert len(picks) == 2 and len({p.grp_id for p in picks}) == 2
    assert {p.grp_id for p in picks} <= {1, 3, 12, 13}


# ---------------------------------------------------------------------------
# Course tracking and log routing
# ---------------------------------------------------------------------------


def test_course_tracker_follows_stages_and_wins():
    tracker = CourseTracker()
    tracker.observe(
        {
            "Courses": [
                {"InternalEventName": "PickTwoDraft_TST", "CourseId": "c1", "CurrentModule": "HumanDraft"},
                {"InternalEventName": "Brawl_Ladder", "CurrentModule": "CreateMatch"},
            ]
        }
    )
    assert tracker.active_limited().stage == "draft"
    tracker.observe(
        {
            "Course": {
                "InternalEventName": "PickTwoDraft_TST",
                "CurrentModule": "DeckSelect",
                "CardPool": [1, 2, 3],
            }
        }
    )
    course = tracker.course("PickTwoDraft_TST")
    assert course.stage == "build" and course.card_pool == [1, 2, 3]
    tracker.observe(
        {"InternalEventName": "PickTwoDraft_TST", "CurrentModule": "CreateMatch", "CurrentWins": 2}
    )
    assert tracker.course("PickTwoDraft_TST").wins == 2 and tracker.active_limited().stage == "play"
    tracker.observe({"Course": {"InternalEventName": "PickTwoDraft_TST", "CurrentModule": "Complete"}})
    assert tracker.active_limited() is None


def test_parser_routes_course_replies():
    parser, seen = LogParser(), []
    parser.register_handler("EventCourse", seen.append)
    parser.process_chunk(
        "[UnityCrossThreadLogger]<== EventGetCoursesV2(abc)\n"
        '{"Courses":[{"InternalEventName":"QuickDraft_TST","CurrentModule":"BotDraft"}]}\n'
        "[UnityCrossThreadLogger]<== EventClaimPrize(def)\n"
        '{"Course":{"InternalEventName":"QuickDraft_TST","CurrentModule":"Complete"}}\n'
    )
    assert [list(p) for p in seen] == [["Courses"], ["Course"]]


# ---------------------------------------------------------------------------
# Mac bridge hands, against a scripted object world
# ---------------------------------------------------------------------------


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


def draft_world(ok=True, reserved=0, human=True) -> World:
    world = World()
    cards = [world.add(200 + i, "CardData", GrpId=grp, TitleId=grp * 10) for i, grp in enumerate((1, 3, 12))]
    views = [world.add(300 + i, "DraftPackCardView", Card=Obj(card)) for i, card in enumerate(cards)]
    holder = world.add(400, "DraftPackHolder", IsAnimating=False, CardViews=[Obj(v) for v in views])
    manager = world.add(500, "DraftDeckManager")
    world.results[(manager, "ReservedCardCount")] = reserved
    info = world.add(610, "PickInfo", SelfPack=1, SelfPick=4)
    pod_class = "Wotc.Mtga.Wrapper.Draft.HumanDraftPod" if human else "Wotc.Mtga.Wrapper.Draft.BotDraftPod"
    pod = world.add(
        600,
        pod_class,
        PickNumCardsToTake=1,
        _currentPickInfo=Obj(info),
        PickSecondsRemaining=50,
        _currentPack=0,
        _currentPick=3,
    )
    world.finds["Wotc.Mtga.Wrapper.Draft.DraftContentController"] = world.add(
        100,
        "DraftContentController",
        _okToPickCard=ok,
        _draftPackHolder=Obj(holder),
        DraftPod=Obj(pod),
        _draftDeckManager=Obj(manager),
    )
    return world


def test_draft_state_reads_the_pack_and_pick_through_the_controller():
    state = MacBridgeAdapter(draft_world().send).handle({"action": "get_draft_state"})
    assert state["is_open"] and state["draft_mode"] == "Human" and state["pack_cards"] == [1, 3, 12]
    assert (state["pack_number"], state["pick_number"], state["pick_seconds_remaining"]) == (1, 4, 50)
    bot = MacBridgeAdapter(draft_world(human=False).send).handle({"action": "get_draft_state"})
    assert (bot["pack_number"], bot["pick_number"]) == (1, 4)


def test_pick_uses_the_double_click_path_and_refuses_unready_packs():
    world = draft_world()
    result = MacBridgeAdapter(world.send).handle({"action": "submit_draft_pick", "cards": [{"grp_id": 3}]})
    assert result["ok"] and result["grp_ids"] == [3]
    assert [(h, m, a) for h, m, a in world.calls if m == "ReserveCardAndLockIn"] == [
        (100, "ReserveCardAndLockIn", [{"h": 301}, {"null": True}])
    ]
    by_title = draft_world()
    assert MacBridgeAdapter(by_title.send).handle(
        {"action": "submit_draft_pick", "cards": [{"grp_id": 999, "title_id": 120}]}
    )["grp_ids"] == [12]
    for world, cards in (
        (draft_world(ok=False), [{"grp_id": 3}]),
        (draft_world(reserved=1), [{"grp_id": 3}]),
        (draft_world(), [{"grp_id": 3}, {"grp_id": 1}]),
        (draft_world(), [{"grp_id": 77}]),
    ):
        assert not MacBridgeAdapter(world.send).handle({"action": "submit_draft_pick", "cards": cards})["ok"]
        assert not any(m == "ReserveCardAndLockIn" for _h, m, _a in world.calls)


def entry(grp_id, count):
    return {"Id": grp_id, "Quantity": count}


def deck_world(main, side) -> World:
    world = World()
    state = {"main": dict(main)}
    context = world.add(20, "DeckBuilderContext", IsLimited=True, IsSideboarding=False, IsReadOnly=False)
    model = world.add(40, "DeckBuilderModel")
    provider = world.add(30, "Provider", _model=Obj(model))
    world.finds["DeckBuilderWidget"] = world.add(
        10, "DeckBuilderWidget", _isActive=True, Context=Obj(context), ModelProvider=Obj(provider)
    )

    def server_model(_args):
        deck = world.add(
            50,
            "Deck",
            mainDeck=[entry(g, c) for g, c in state["main"].items() if c],
            sideboard=[entry(g, c) for g, c in side.items()],
        )
        return Obj(deck)

    def add(args):
        state["main"][args[0]["uint"]] = state["main"].get(args[0]["uint"], 0) + args[1]["uint"]

    def remove(args):
        state["main"][args[0]["uint"]] = state["main"].get(args[0]["uint"], 0) - args[1]["uint"]

    world.results[(model, "GetServerModel")] = server_model
    world.results[(model, "AddCardToMainDeck")] = add
    world.results[(model, "RemoveCardFromMainDeck")] = remove
    world.results[(model, "GetQuantityInCardPool")] = lambda args: 99 if args[0]["uint"] == 7001 else 0
    world.state = state
    return world


def test_limited_deck_is_replaced_exactly_and_submitted_with_done():
    world = deck_world({1: 1, 7001: 17}, {2: 1, 3: 1})
    adapter = MacBridgeAdapter(world.send)
    pool = adapter.handle({"action": "get_limited_pool", "basic_candidates": [7001, 7002]})
    assert pool["basics_in_pool"] == {"7001": 99} and pool["sideboard"] == [
        {"grp_id": 2, "count": 1},
        {"grp_id": 3, "count": 1},
    ]
    target = [{"grp_id": 2, "count": 12}, {"grp_id": 3, "count": 11}, {"grp_id": 7001, "count": 17}]
    written = adapter.handle({"action": "set_limited_deck", "main_deck": target})
    assert written["ok"] and {e["grp_id"]: e["count"] for e in written["main_deck"]} == {
        2: 12,
        3: 11,
        7001: 17,
    }
    assert adapter.handle({"action": "submit_limited_deck"})["ok"]
    assert (10, "DoneButton_OnClick", []) in world.calls
    assert not adapter.handle({"action": "set_limited_deck", "main_deck": [{"grp_id": 2, "count": 5}]})["ok"]


def event_world(module: str) -> World:
    world = World()
    info = world.add(70, "EventInfo", InternalEventName="PickTwoDraft_TST")
    course = world.add(71, "CourseData", CurrentModule={"e": module, "v": 1})
    player_event = world.add(72, "LimitedPlayerEvent", CourseData=Obj(course), EventInfo=Obj(info))
    context = world.add(73, "EventContext", PlayerEvent=Obj(player_event))
    world.finds["EventPage.EventPageContentController"] = world.add(
        74, "EventPageContentController", _currentEventContext=Obj(context)
    )
    delegate = world.add(75, "System.Action")
    world.finds["EventPage.Components.MainButtonComponent"] = world.add(
        76,
        "MainButtonComponent",
        PlayButton_OnClick=Obj(delegate),
        PayJoinButton_OnClick=Obj(world.add(77, "System.Action")),
    )
    return world


@pytest.mark.parametrize(
    "module,allowed",
    [
        ("WinLossGate", True),
        ("ClaimPrize", True),
        ("DeckSelect", True),
        ("HumanDraft", True),
        ("Join", False),
        ("Pay", False),
        ("PayEntry", False),
        ("Complete", False),
        ("Choice", False),
    ],
)
def test_event_play_never_runs_in_a_paying_stage(module, allowed):
    world = event_world(module)
    result = MacBridgeAdapter(world.send).handle({"action": "event_play", "event_name": "PickTwoDraft_TST"})
    assert result["ok"] is allowed
    invoked = [h for h, m, _a in world.calls if m == "Invoke"]
    assert invoked == ([75] if allowed else [])
    assert 77 not in invoked


def test_event_play_requires_the_expected_event():
    world = event_world("WinLossGate")
    assert not MacBridgeAdapter(world.send).handle({"action": "event_play", "event_name": "Other"})["ok"]
    assert not world.calls


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class FakeBridge:
    connected = True

    def __init__(self, replies: dict[str, Any]) -> None:
        self.replies = replies
        self.sent: list[tuple[str, dict]] = []

    def draft_command(self, action, **fields):
        self.sent.append((action, fields))
        reply = self.replies.get(action, {"ok": False, "error": "unscripted"})
        return reply(fields) if callable(reply) else reply


class FakeDB:
    names = {1: "Blue Ace", 3: "Red Ace", 12: "Filler 2", 2: "Blue Common"}

    def get_card(self, grp_id):
        return SimpleNamespace(name=self.names.get(grp_id, f"Card {grp_id}"), expansion_code="TST")


def driver_for(bridge, primer, **kwargs) -> DraftEventDriver:
    service = SimpleNamespace(get=lambda code: primer, ensure=lambda code: None, quick=lambda code: primer)
    driver = DraftEventDriver(
        bridge_fn=lambda: bridge,
        tracker_fn=lambda: CourseTracker(),
        primer_service=service,
        card_db=FakeDB(),
        **kwargs,
    )
    driver.set_enabled(True)
    return driver


PICK_STATE = {
    "ok": True,
    "is_open": True,
    "ok_to_pick": True,
    "animating": False,
    "reserved_count": 0,
    "pick_num_cards_to_take": 1,
    "pack_number": 1,
    "pick_number": 1,
    "pick_seconds_remaining": 10,
    "pack_cards": [12, 1, 3],
    "pack_views": [{"grp_id": g, "title_id": g * 10} for g in (12, 1, 3)],
}


def test_driver_picks_the_ranked_card_once_and_waits_for_it_to_land(primer):
    bridge = FakeBridge(
        {
            "get_screen": {"ok": True, "draft": True},
            "get_draft_state": PICK_STATE,
            "submit_draft_pick": {"ok": True},
        }
    )
    driver = driver_for(bridge, primer)
    driver._step()
    submits = [f for a, f in bridge.sent if a == "submit_draft_pick"]
    assert submits == [{"cards": [{"grp_id": 1, "title_id": 10}], "timeout": 8.0}]
    assert driver.run.pool == [1] and driver.owns_ui
    driver._step()  # same pack still showing: our pick is landing, no second submit
    assert len([a for a, _f in bridge.sent if a == "submit_draft_pick"]) == 1


def test_driver_ranks_against_picks_made_before_it_took_over(primer):
    # 2026-10-05: autoplay joined at P2p13 of a mostly blue pool and, seeing
    # only its own picks, drafted black/red. Player.log has every pick.
    bridge = FakeBridge(
        {
            "get_screen": {"ok": True, "draft": True},
            "get_draft_state": {**PICK_STATE, "pack_number": 3, "pick_number": 1},
            "submit_draft_pick": {"ok": True},
        }
    )
    red_pool = [4] * 8 + [13, 18, 3]
    driver = driver_for(bridge, primer, picked_fn=lambda: red_pool)
    driver._step()
    submits = [f for a, f in bridge.sent if a == "submit_draft_pick"]
    assert submits[0]["cards"][0]["grp_id"] == 3


@pytest.mark.parametrize("pack_number", [1, 3])
def test_model_takeover_sees_all_picks_and_current_pack_signals(primer, pack_number):
    logged_pool = [4] * 8 + [13, 18, 3]
    seen = {}

    def pool_cards(ids, set_code):
        seen["pool_ids"] = ids
        return [{"grp_id": g, "name": f"Card {g}"} for g in ids]

    def recommend(details, fallback):
        seen["details"] = details
        seen["rankings"] = fallback["evaluations"]
        return {
            "reasoning_source": "card_rules",
            "recommendations": [{"grp_id": 3, "reason": "Support our red creatures."}],
            "plan": "Red creatures with interaction.",
            "needs": ["Removal"],
        }

    bridge = FakeBridge(
        {
            "get_screen": {"ok": True, "draft": True},
            "get_draft_state": {
                **PICK_STATE,
                "pack_number": pack_number,
                "pick_number": 6,
                "pick_seconds_remaining": 60,
            },
            "submit_draft_pick": {"ok": True},
        }
    )
    driver = driver_for(
        bridge,
        primer,
        picked_fn=lambda: logged_pool,
        pool_cards_fn=pool_cards,
        pick_advisor_fn=lambda: SimpleNamespace(recommend=recommend),
        pack_fn=lambda: {
            "pack_number": pack_number,
            "pick_number": 6,
            "cards": [{"grp_id": g} for g in PICK_STATE["pack_cards"]],
        },
    )
    driver.run.pool = [3]
    driver._step()

    assert seen["pool_ids"] == logged_pool
    assert len(seen["details"]["picked_cards"]) == len(logged_pool)
    assert seen["details"]["set_strategy"]
    has_signal = any("looks open" in row["reason"] for row in seen["rankings"])
    assert has_signal == (pack_number == 1)
    assert driver.run.picks[-1]["source"] == "model"
    assert driver.run.picks[-1]["grp_ids"] == [3]


def test_model_cannot_override_ranking_with_an_ordinary_basic(primer):
    bridge = FakeBridge({})
    driver = driver_for(
        bridge,
        primer,
        pack_fn=lambda: {"cards": [{"grp_id": 999}, {"grp_id": 6}]},
        pick_advisor_fn=lambda: SimpleNamespace(
            recommend=lambda *args: {
                "reasoning_source": "card_rules",
                "recommendations": [{"grp_id": 999, "reason": "We need lands."}],
            }
        ),
    )
    driver._db = SimpleNamespace(get_card=lambda g: SimpleNamespace(name="Forest" if g == 999 else "Spell"))
    assert driver._refine_pick([999, 6], primer, [], [], 1) is None
    assert driver._refine_pick([999], primer, [], [], 1) is None  # stale pack refused
    driver._pack_fn = lambda: {"cards": [{"grp_id": 999}]}
    assert driver._refine_pick([999], primer, [], [], 1)[0] == [999]


def test_driver_pauses_after_a_pick_does_not_register_twice(primer):
    bridge = FakeBridge(
        {
            "get_screen": {"ok": True, "draft": True},
            "get_draft_state": PICK_STATE,
            "submit_draft_pick": {"ok": False, "error": "not ready"},
        }
    )
    driver = driver_for(bridge, primer)
    for _ in range(3):
        driver._step()
    assert "did not register" in driver.paused_reason and not driver.tick()


def test_driver_stops_when_the_bridge_cannot_draft(primer):
    bridge = FakeBridge({"get_screen": {"ok": False, "unsupported": True}})
    driver = driver_for(bridge, primer)
    driver._step()
    assert "cannot drive drafts" in driver.paused_reason


def test_driver_never_presses_play_for_an_unentered_event(primer):
    bridge = FakeBridge(
        {
            "get_screen": {"ok": True, "event_page": True},
            "get_event_page": {"ok": True, "is_open": True, "module": "Join", "event_name": "X"},
        }
    )
    driver = driver_for(bridge, primer)
    driver._step()
    assert not any(a == "event_play" for a, _f in bridge.sent) and not driver.run.event_name


def test_driver_queues_once_and_does_not_cancel_its_own_queue(primer):
    page = {"ok": True, "is_open": True, "module": "WinLossGate", "event_name": "PickTwoDraft_TST"}
    bridge = FakeBridge(
        {"get_screen": {"ok": True, "event_page": True}, "get_event_page": page, "event_play": {"ok": True}}
    )
    driver = driver_for(bridge, primer)
    driver._step()
    driver._step()
    assert [a for a, _f in bridge.sent].count("event_play") == 1
    assert driver.run.event_name == "PickTwoDraft_TST"


def test_driver_builds_and_submits_a_forty_card_deck(primer, monkeypatch):
    pool = [{"grp_id": g, "count": 1} for g in range(100, 130)]
    bridge = FakeBridge(
        {
            "get_screen": {"ok": True, "deck_builder": True},
            "get_limited_pool": {
                "ok": True,
                "main_deck": [{"grp_id": 7001, "count": 17}],
                "sideboard": pool,
                "basics_in_pool": {"7001": 99, "7002": 99},
            },
            "set_limited_deck": lambda fields: {"ok": True, "main_deck": fields["main_deck"]},
            "submit_limited_deck": {"ok": True},
        }
    )
    build = {
        "main_deck": [{"grp_id": g, "count": 1} for g in range(100, 123)],
        "basic_lands": {"U": 9, "R": 8},
        "plan": "Izzet tempo",
    }
    monkeypatch.setattr("arenamcp.limited_deck.fallback_deck", lambda cards, *_, **__: dict(build))
    driver = driver_for(bridge, primer, pool_cards_fn=lambda ids, code: [{"grp_id": g} for g in ids])
    driver._basics = {7001: "U", 7002: "R"}
    driver._step()
    written = [f["main_deck"] for a, f in bridge.sent if a == "set_limited_deck"]
    assert written and sum(e["count"] for e in written[0]) == 40
    assert {e["grp_id"]: e["count"] for e in written[0]}[7002] == 8
    assert ("submit_limited_deck", {"timeout": 10.0}) in bridge.sent


def test_deck_entries_refuse_missing_basic_colors():
    build = {"main_deck": [{"grp_id": 1, "count": 23}], "basic_lands": {"G": 17}}
    assert deck_entries(build, {7001: "U"}, {"basics_in_pool": {"7001": 9}, "main_deck": []}) is None


def test_tick_runs_steps_in_the_background_and_reports_ownership(primer):
    bridge = FakeBridge(
        {
            "get_screen": {"ok": True, "draft": True},
            "get_draft_state": PICK_STATE,
            "submit_draft_pick": {"ok": True},
        }
    )
    driver = driver_for(bridge, primer)
    driver.tick()
    driver._worker.join(timeout=5)
    assert driver.run.pool == [1]
    assert driver.tick() is True
    driver.set_enabled(False)
    assert driver.tick() is False


def test_owned_screens_wait_while_a_match_is_being_played(primer):
    bridge = FakeBridge({"get_screen": {"ok": True, "draft": True}})
    driver = driver_for(bridge, primer, in_match_fn=lambda: True)
    driver._step()
    assert bridge.sent == [] and not driver.owns_ui
    assert driver._next_poll > time.monotonic()


def test_driver_only_adopts_limited_events(primer):
    page = {"ok": True, "is_open": True, "module": "WinLossGate", "event_name": "Standard_Event"}
    bridge = FakeBridge(
        {"get_screen": {"ok": True, "event_page": True}, "get_event_page": page, "event_play": {"ok": True}}
    )
    driver = driver_for(bridge, primer)
    driver._step()
    assert not any(a == "event_play" for a, _f in bridge.sent) and not driver.run.event_name


def test_driver_waits_for_the_builder_pool_to_load(primer):
    bridge = FakeBridge(
        {
            "get_screen": {"ok": True, "deck_builder": True},
            "get_limited_pool": {"ok": True, "main_deck": [], "sideboard": [], "basics_in_pool": {}},
        }
    )
    driver = driver_for(bridge, primer)
    driver._basics = {}
    for _ in range(9):
        driver._step()
    assert not driver.paused_reason
    driver._step()
    assert "only 0 spells" in driver.paused_reason


# ---------------------------------------------------------------------------
# Limited deck choice (2026-10-05 Arena Direct sealed)
# ---------------------------------------------------------------------------


def spell(grp_id, name, cost, *, gih=None, rarity="common", type_line="Creature — Bear", text="Vigilance"):
    card = {
        "grp_id": grp_id,
        "name": name,
        "mana_cost": cost,
        "type_line": type_line,
        "oracle_text": text,
        "rarity": rarity,
    }
    if gih is not None:
        card["gih_wr"] = gih
    return card


def sealed_pool():
    # Ordinary commons: an unrated mythic should beat these, a vanilla 2-drop should not.
    red = [spell(i, f"Red {i}", "{1}{R}", gih=0.52) for i in range(1, 13)]
    green = [spell(100 + i, f"Green {i}", "{1}{G}", gih=0.51) for i in range(1, 13)]
    white = [spell(200 + i, f"White {i}", "{1}{W}", gih=0.48) for i in range(1, 13)]
    extras = [
        spell(300, "Unrated Mythic", "{4}{R}", rarity="mythic", type_line="Creature — Dragon", text="Flying"),
        spell(301, "Legend", "{1}{G}", gih=0.60, type_line="Legendary Creature — Elf"),
        spell(301, "Legend", "{1}{G}", gih=0.60, type_line="Legendary Creature — Elf"),
    ]
    return red + green + white + extras


def test_builder_scores_whole_decks_and_considers_unrated_bombs():
    from arenamcp.limited_deck import candidate_decks, fallback_deck

    pool = sealed_pool()
    ranked = candidate_decks(pool)
    assert ranked[0]["quality"]["colors"] == "RG"
    assert ranked[0]["quality"]["score"] > ranked[-1]["quality"]["score"]
    build = fallback_deck(pool)
    names = Counter(entry["name"] for entry in build["main_deck"] for _ in range(entry["count"]))
    assert names["Unrated Mythic"] == 1  # rarity prior, not zero
    assert names["Legend"] == 1  # a second copy of a legendary card is cut first
    assert build["quality"]["colors"] == "RG" and build["candidates"][0]["colors"] == "RG"
    assert "avg GIH" in build["plan"]


def test_archetype_win_rates_break_close_calls():
    from arenamcp.limited_deck import candidate_decks

    pool = sealed_pool()
    plain = {c["quality"]["colors"]: c["quality"]["score"] for c in candidate_decks(pool, top=10)}
    shifted = {
        c["quality"]["colors"]: c["quality"]["score"]
        for c in candidate_decks(pool, {"RG": 0.50, "WR": 0.60, "WG": 0.55}, top=10)
    }
    assert shifted["WR"] - plain["WR"] > shifted["RG"] - plain["RG"]


class ScriptedDeckBackend:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, message, max_tokens, **kwargs):
        self.calls.append((max_tokens, kwargs.get("response_format")))
        return self.replies.pop(0)


def test_deck_review_has_room_to_answer_and_retries_bad_json():
    from arenamcp.draft_advisor import DraftAdvisor
    from arenamcp.limited_deck import fallback_deck

    pool = sealed_pool()
    build = {**fallback_deck(pool), "pool_cards": pool}
    good = json.dumps(
        {
            "main_deck": [{"grp_id": e["grp_id"], "count": e["count"]} for e in build["main_deck"]],
            "basic_lands": build["basic_lands"],
            "plan": "Red-green bodies.",
            "cuts": [{"grp_id": c["grp_id"], "reason": "weaker card"} for c in build["cuts"]],
        }
    )
    backend = ScriptedDeckBackend(['{"main_deck": [', good])
    result = DraftAdvisor(backend, timeout=5).recommend_deck(build)
    assert result is not build and result["plan"] == "Red-green bodies."
    assert backend.calls == [(8000, {"type": "json_object"})] * 2


def test_driver_keeps_the_counted_build_when_the_model_deck_scores_much_worse(primer, monkeypatch):
    from arenamcp.limited_deck import fallback_deck

    pool = sealed_pool()
    sideboard = [{"grp_id": g, "count": n} for g, n in Counter(c["grp_id"] for c in pool).items()]
    bridge = FakeBridge(
        {
            "get_screen": {"ok": True, "deck_builder": True},
            "get_limited_pool": {
                "ok": True,
                "main_deck": [],
                "sideboard": sideboard,
                "basics_in_pool": {"7001": 99, "7002": 99, "7003": 99},
            },
            "set_limited_deck": lambda fields: {"ok": True, "main_deck": fields["main_deck"]},
            "submit_limited_deck": {"ok": True},
        }
    )
    by_id = {c["grp_id"]: c for c in pool}
    white = [g for g in by_id if 200 < g < 300]
    weak = {
        "main_deck": [{"grp_id": g, "count": 1} for g in white]
        + [{"grp_id": g, "count": 1} for g in range(1, 12)],
        "basic_lands": {"W": 9, "R": 8},
        "plan": "White weenies",
    }
    advisor = SimpleNamespace(recommend_deck=lambda build: {**build, **weak})
    driver = driver_for(
        bridge,
        primer,
        pool_cards_fn=lambda ids, code: [by_id[g] for g in ids],
        deck_advisor_fn=lambda: advisor,
    )
    driver._basics = {7001: "R", 7002: "G", 7003: "W"}
    driver._step()
    written = [f["main_deck"] for a, f in bridge.sent if a == "set_limited_deck"][0]
    assert {e["grp_id"] for e in written} & set(white) == set()  # counted RG build kept
    counted = {e["grp_id"] for e in fallback_deck(pool)["main_deck"]}
    assert counted <= {e["grp_id"] for e in written}


# ---------------------------------------------------------------------------
# Damage removal must kill something (2026-10-05 Arena Direct, Wrath at a 5/5)
# ---------------------------------------------------------------------------

WRATH = {
    "name": "Wrath of the Bloodmane",
    "type_line": "Instant",
    "oracle_text": "This spell costs {1} less to cast if you control a legendary creature.\n"
    "Wrath of the Bloodmane deals 4 damage to target creature or planeswalker.",
}


def board(*creatures, walker=False):
    battlefield = [
        {
            "instance_id": iid,
            "name": name,
            "type_line": "Creature",
            "power": p,
            "toughness": t,
            "controller_seat_id": 1,
            **extra,
        }
        for iid, name, p, t, extra in creatures
    ]
    if walker:
        battlefield.append(
            {
                "instance_id": 900,
                "name": "Walker",
                "type_line": "Legendary Planeswalker",
                "controller_seat_id": 1,
            }
        )
    return {
        "players": [{"seat_id": 2, "is_local": True}, {"seat_id": 1, "is_local": False}],
        "battlefield": battlefield,
    }


def test_fixed_damage_removal_is_withheld_when_nothing_dies():
    from arenamcp.play_safety import damage_removal_kills_nothing, fixed_damage_removal

    assert fixed_damage_removal(WRATH) == (4, True)
    big = board((275, "Uldaros", 5, 5, {}), (276, "Ruric", 4, 6, {}))
    assert "kills no opposing creature" in damage_removal_kills_nothing(WRATH, big)
    assert (
        damage_removal_kills_nothing(WRATH, board((275, "Uldaros", 5, 5, {}), (300, "Geist", 1, 2, {}))) == ""
    )
    assert damage_removal_kills_nothing(WRATH, board((275, "Uldaros", 5, 5, {"is_attacking": True}))) == ""
    assert damage_removal_kills_nothing(WRATH, board((275, "Uldaros", 5, 5, {}), walker=True)) == ""
    burn = {**WRATH, "oracle_text": "Deals 4 damage to any target."}
    assert damage_removal_kills_nothing(burn, big) == ""


def test_damage_removal_is_pointed_at_a_creature_it_kills():
    from arenamcp.action_planner import DECLINE_DECISION, ActionPlanner

    planner = ActionPlanner.__new__(ActionPlanner)
    planner._decision_source_oracle = lambda decision, state: WRATH["oracle_text"].lower()
    state = board((275, "Uldaros", 5, 5, {}), (276, "Ruric", 4, 6, {}), (300, "Geist", 1, 2, {}))
    decision = SimpleNamespace(options=[SimpleNamespace(option_id=f"tgt:{i}") for i in (275, 276, 300)])
    assert planner._prefer_lethal_damage_target(decision, state, ["tgt:275"]) == ["tgt:300"]
    assert planner._prefer_lethal_damage_target(decision, state, ["tgt:300"]) == ["tgt:300"]
    no_kill = board((275, "Uldaros", 5, 5, {}), (276, "Ruric", 4, 6, {}))
    two = SimpleNamespace(options=[SimpleNamespace(option_id=f"tgt:{i}") for i in (275, 276)])
    assert planner._prefer_lethal_damage_target(two, no_kill, ["tgt:275"]) == [DECLINE_DECISION]


# -- the coach's in-match gate ------------------------------------------------


class _InMatchHarness:
    def __init__(self, state):
        from arenamcp.standalone_draft_event import _DraftEventMixin

        self._mixin = _DraftEventMixin
        self._mcp = SimpleNamespace(get_game_state=lambda: state)

    def in_match(self, pack=None):
        self._draft_event_pack = pack
        return self._mixin._draft_event_in_match(self)


def test_in_match_gate_ignores_a_finished_match_and_an_open_draft(monkeypatch):
    from arenamcp import server

    # 2026-10-05: the sealed match ended, its result was consumed, and the
    # board (match_id, turn 16) stayed until the next match: the Premier
    # Draft joined right after was never picked.
    state = {"match_id": "m-1", "turn": {"turn_number": 16}, "last_game_result": None}
    monkeypatch.setattr(server, "get_completed_match_for_navigation", lambda: {})
    harness = _InMatchHarness(state)
    assert harness.in_match() is True
    assert harness.in_match({"is_active": True}) is False
    assert harness.in_match({"is_building": True}) is False

    monkeypatch.setattr(
        server, "get_completed_match_for_navigation", lambda: {"match_id": "m-1", "match_complete": True}
    )
    assert harness.in_match() is False
    monkeypatch.setattr(
        server, "get_completed_match_for_navigation", lambda: {"match_id": "m-0", "match_complete": True}
    )
    assert harness.in_match() is True


# ---------------------------------------------------------------------------
# Deck review narration gates the submission (2026-10-06 FRA draft)
# ---------------------------------------------------------------------------


def review_world(primer, review, *, screens=None, writes=None, advisor=None):
    pool = sealed_pool()
    sideboard = [{"grp_id": g, "count": n} for g, n in Counter(c["grp_id"] for c in pool).items()]
    by_id = {c["grp_id"]: c for c in pool}
    screens = list(screens or [])
    writes = list(writes or [])
    bridge = FakeBridge(
        {
            "get_screen": lambda fields: screens.pop(0) if screens else {"ok": True, "deck_builder": True},
            "get_limited_pool": {
                "ok": True,
                "main_deck": [],
                "sideboard": sideboard,
                "basics_in_pool": {"7001": 99, "7002": 99, "7003": 99},
            },
            "set_limited_deck": lambda fields: (
                writes.pop(0) if writes else {"ok": True, "main_deck": fields["main_deck"]}
            ),
            "submit_limited_deck": {"ok": True},
        }
    )
    spoken: list[str] = []
    kwargs = {"deck_advisor_fn": (lambda: advisor)} if advisor else {}
    driver = driver_for(
        bridge,
        primer,
        pool_cards_fn=lambda ids, code: [by_id[g] for g in ids],
        review_fn=review,
        speak_fn=spoken.append,
        **kwargs,
    )
    driver._basics = {7001: "R", 7002: "G", 7003: "W"}
    return driver, bridge, spoken


def actions(bridge):
    return [action for action, _ in bridge.sent]


def test_deck_is_submitted_only_after_the_review_has_been_heard(primer):
    heard: list[tuple[str, list[str]]] = []
    holder: dict = {}

    def review(text, cancelled):
        assert not cancelled()
        heard.append((text, actions(holder["bridge"])))
        return True

    driver, bridge, spoken = review_world(primer, review)
    holder["bridge"] = bridge
    driver._step()
    text, before = heard[0]
    assert "set_limited_deck" not in before and "submit_limited_deck" not in before
    assert "Option 1 is" in text and "Option 2 is" in text and "I'm submitting option 1" in text
    after = actions(bridge)[len(before) :]
    # Arena is re-read after the narration, then the deck is written and submitted.
    assert after == ["get_screen", "get_limited_pool", "set_limited_deck", "submit_limited_deck"]
    assert spoken == ["Deck submitted."]


def test_stopped_review_pauses_without_submitting(primer):
    driver, bridge, _ = review_world(primer, lambda text, cancelled: False)
    driver._step()
    assert "set_limited_deck" not in actions(bridge)
    assert "turn autoplay off and on" in driver.paused_reason


def test_turning_autoplay_off_during_review_cancels_quietly(primer):
    seen = {}

    def review(text, cancelled):
        driver.set_enabled(False)
        seen["cancelled"] = cancelled()
        return False

    driver, bridge, _ = review_world(primer, review)
    driver._step()
    assert seen["cancelled"]
    assert "set_limited_deck" not in actions(bridge)
    assert not driver.paused_reason


def test_review_is_repeated_if_arena_left_the_deck_builder_meanwhile(primer):
    reviews = []
    driver, bridge, _ = review_world(
        primer,
        lambda text, cancelled: reviews.append(text) or True,
        screens=[{"ok": True, "deck_builder": True}, {"ok": True, "home": True}],
    )
    driver._step()  # first screen read routes to the deck step; the post-review read sees home
    assert "set_limited_deck" not in actions(bridge)
    driver._next_poll = 0.0
    driver._step()
    assert len(reviews) == 2 and "submit_limited_deck" in actions(bridge)


def test_retried_submission_reuses_the_narrated_build_without_rereviewing(primer):
    reviews = []
    calls = []

    class Advisor:
        def recommend_deck(self, build):
            calls.append(build)
            return build

    driver, bridge, _ = review_world(
        primer,
        lambda text, cancelled: reviews.append(text) or True,
        writes=[{"ok": False, "error": "builder busy"}],
        advisor=Advisor(),
    )
    driver._step()
    driver._step()
    written = [fields["main_deck"] for action, fields in bridge.sent if action == "set_limited_deck"]
    assert len(written) == 2 and written[0] == written[1]
    assert len(reviews) == 1 and len(calls) == 1
    assert "submit_limited_deck" in actions(bridge)


def test_claimed_event_waits_for_reentry_instead_of_pausing(primer):
    """2026-10-06 15:23: after claiming the prize the event showed Join; the
    pause outlived the player's re-entry and P1p1 went unpicked."""
    page = {"ok": True, "is_open": True, "module": "ClaimPrize", "event_name": "PremierDraft_TST"}
    bridge = FakeBridge(
        {
            "get_screen": {"ok": True, "event_page": True},
            "get_event_page": lambda fields: dict(page),
            "event_play": {"ok": True},
        }
    )
    driver = driver_for(bridge, primer)
    driver._step()  # adopts the finished event and claims its prize
    assert ("event_play", {"event_name": "PremierDraft_TST", "timeout": 8.0}) in bridge.sent
    page["module"] = "Join"
    driver._next_poll = 0.0
    driver._step()
    assert not driver.paused_reason and not driver.run.event_name
    assert driver.tick() is False or not driver.paused_reason  # still running, not paused
    bridge.replies["get_screen"] = {"ok": True, "draft": True}
    bridge.replies["get_draft_state"] = dict(PICK_STATE)
    bridge.replies["submit_draft_pick"] = {"ok": True}
    driver._next_poll = 0.0
    driver._step()
    assert any(a == "submit_draft_pick" for a, _f in bridge.sent)


@pytest.mark.parametrize("auto_queue", [False, True])
def test_matches_are_queued_only_with_auto_queue_on(primer, auto_queue):
    """2026-10-06: Auto-queue was off but every PremierDraft match was queued."""
    bridge = FakeBridge(
        {
            "get_screen": {"ok": True, "event_page": True},
            "get_event_page": {
                "ok": True,
                "is_open": True,
                "module": "WinLossGate",
                "event_name": "PremierDraft_TST",
            },
            "event_play": {"ok": True},
        }
    )
    driver = driver_for(bridge, primer, queue_fn=lambda: auto_queue)
    driver.run.event_name = "PremierDraft_TST"
    driver._step()
    assert any(a == "event_play" for a, _f in bridge.sent) is auto_queue
    assert not driver.paused_reason


def test_prize_is_claimed_even_with_auto_queue_off(primer):
    bridge = FakeBridge(
        {
            "get_screen": {"ok": True, "event_page": True},
            "get_event_page": {
                "ok": True,
                "is_open": True,
                "module": "ClaimPrize",
                "event_name": "PremierDraft_TST",
            },
            "event_play": {"ok": True},
        }
    )
    driver = driver_for(bridge, primer, queue_fn=lambda: False)
    driver.run.event_name = "PremierDraft_TST"
    driver._step()
    assert any(a == "event_play" for a, _f in bridge.sent)
