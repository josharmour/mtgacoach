"""The stack is top-first in every snapshot (ledger #115, 2026-10-19).

Evidence, Player.log 2026-10-19: in every multi-object Stack zone that was not
a set of simultaneous triggers, Arena listed the newest id first. Every single
object put on the stack appeared at index 0 (141 of 141), and every single
object that left the stack left from index 0 (271 of 271). In match 2c67eed8 T6
(the fixture), Burst Lightning #304, our prowess trigger #306, their Shock #307,
our Shock #309 and prowess #311 resolved 311, 309, 307, 306, 304. The coach
read [-1] as the top, so at 12:49:07 their Shock answering our Burst was
logged as stack_spell_yours.

The bridges serialize ``MtgZone.VisibleCards`` in insertion order. Bug
reports with the bridge connected list [515, 516, 517] with Erode on top.
``server._get_bridge_overlay`` puts that list into the log's order, either by
the zone's own ``card_ids`` or by the log's stack (``arenamcp.stack_order``).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from tests.test_mac_card_seats import ProbeDump, player_node, publish

from arenamcp.action_planner import ActionPlanner, own_stack_only
from arenamcp.autopilot import AutopilotEngine
from arenamcp.coach_triggers import GameStateTrigger
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.gamestate import GameState, create_game_state_handler
from arenamcp.mac_game_state import build_state_ops, fetch_game_state
from arenamcp.play_safety import numeric_option_source
from arenamcp.stack_order import order_top_first, stack_top
from arenamcp.ward import pending_ward_triggers

FIXTURE = Path(__file__).parent / "fixtures" / "player_log_stack_order_20261019.log"
LOCAL, OPP = 2, 1
BURST, PROWESS, THEIR_SHOCK, OUR_SHOCK, PROWESS_2 = 304, 306, 307, 309, 311
# grpIds in the fixture: Burst Lightning, our Shock, their Shock (another printing), prowess.
NAMES = {93905: "Burst Lightning", 75525: "Shock", 98039: "Shock", 137: "Prowess"}


_REPLAY: list[tuple[int, dict[str, Any], list[int]]] = []


def replay_stack_fight() -> list[tuple[int, dict[str, Any], list[int]]]:
    """(gameStateId, published snapshot, ids that finished resolving) after each game state of the fixture.

    Each GRE message goes through the real log handler on its own, so every
    gameStateId gets its own snapshot. Replayed once; callers get a copy.
    """
    if not _REPLAY:
        _REPLAY.extend(_replay())
    return copy.deepcopy(_REPLAY)


def _replay() -> list[tuple[int, dict[str, Any], list[int]]]:
    gs = GameState()
    handler = create_game_state_handler(gs)
    frames = []
    for line in FIXTURE.read_text().splitlines():
        if not line.startswith("{"):
            continue
        for message in json.loads(line)["greToClientEvent"]["greToClientMessages"]:
            handler({"greToClientEvent": {"greToClientMessages": [message]}})
            state = message.get("gameStateMessage")
            if not state:
                continue
            resolved = [
                note["affectorId"]
                for note in state.get("annotations") or []
                if "AnnotationType_ResolutionComplete" in note.get("type", [])
            ]
            frames.append((state["gameStateId"], gs.get_published_snapshot(), resolved))
    return frames


def coach_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The fields the coach reads, named like ``server.get_game_state``'s, with card names."""
    zones = snapshot["zones"]
    stack = [dict(card, name=NAMES.get(card["grp_id"], "?")) for card in zones["stack"]]
    return {
        "turn": snapshot["turn_info"],
        "players": snapshot["players"],
        "local_seat_id": snapshot["local_seat_id"],
        "battlefield": zones["battlefield"],
        "hand": zones["my_hand"],
        "stack": stack,
    }


def stack_ids(state: dict[str, Any]) -> list[int]:
    return [card["instance_id"] for card in state["stack"]]


def frame(frames: list, game_state_id: int) -> dict[str, Any]:
    return next(snapshot for gsid, snapshot, _ in frames if gsid == game_state_id)


# --- the convention, from the log ----------------------------------------------------


def test_arena_lists_the_stack_top_first_and_resolves_index_0():
    frames = replay_stack_fight()
    assert stack_ids(coach_state(frame(frames, 131))) == [PROWESS_2, OUR_SHOCK, THEIR_SHOCK, PROWESS, BURST]
    resolved_in_order = []
    previous: list[int] = []
    for _gsid, snapshot, resolved in frames:
        ids = stack_ids(snapshot["zones"])
        added = [iid for iid in ids if iid not in previous]
        if len(added) == 1 and len(previous) > 0:
            assert ids[0] == added[0]  # a new object goes on top: index 0
        for iid in resolved:
            assert previous and previous[0] == iid  # what resolves is the top of the stack before it
            resolved_in_order.append(iid)
        previous = ids
    assert resolved_in_order == [PROWESS_2, OUR_SHOCK, THEIR_SHOCK, PROWESS, BURST]


def test_stack_top_is_index_0():
    state = coach_state(replay_stack_fight_frame(125))
    assert stack_top(state)["instance_id"] == THEIR_SHOCK
    assert stack_top({"stack": []}) is None
    assert stack_top({}) is None
    assert stack_top(None) is None


def replay_stack_fight_frame(game_state_id: int) -> dict[str, Any]:
    return frame(replay_stack_fight(), game_state_id)


# --- the coach's trigger ---------------------------------------------------------------


def test_their_shock_answering_our_burst_is_an_opponent_stack_spell():
    frames = replay_stack_fight()
    trigger = GameStateTrigger()
    before, theirs = coach_state(frame(frames, 124)), coach_state(frame(frames, 125))
    assert stack_ids(theirs) == [THEIR_SHOCK, PROWESS, BURST]
    fired = trigger.check_triggers(before, theirs)
    assert "stack_spell_opponent" in fired
    assert "stack_spell_yours" not in fired
    ours = coach_state(frame(frames, 129))
    fired = trigger.check_triggers(theirs, ours)
    assert "stack_spell_yours" in fired
    assert "stack_spell_opponent" not in fired


# --- the bridge's insertion order ---------------------------------------------------


def _bridge_stack(ids: list[int]) -> list[dict[str, Any]]:
    return [{"instance_id": iid, "name": f"#{iid}"} for iid in ids]


def test_bridge_insertion_order_puts_the_newest_on_top():
    # bug_20261004_001313: the bridge listed [515, 516, 517]; Erode (517) was cast last.
    assert [c["instance_id"] for c in order_top_first(_bridge_stack([515, 516, 517]))] == [517, 516, 515]


def test_the_zone_order_decides_for_triggers_stacked_out_of_creation_order():
    # Player.log line 226293, gsid 287: four simultaneous triggers stacked [464, 463, 466, 465].
    cards = _bridge_stack([463, 464, 465, 466])
    ordered = order_top_first(cards, zone_ids=[464, 463, 466, 465])
    assert [c["instance_id"] for c in ordered] == [464, 463, 466, 465]
    # Without the zone's order the log's places what it knows; a newer bridge object goes on top.
    ordered = order_top_first(_bridge_stack([463, 464, 465, 466, 467]), reference_ids=[464, 463, 466, 465])
    assert [c["instance_id"] for c in ordered] == [467, 464, 463, 466, 465]


def test_entries_without_an_id_sink_to_the_bottom():
    # bug_20261011_205026: the bridge stack had [512, 0, 0] (unserialized items).
    cards = [{"instance_id": 0, "name": "Unknown"}, *_bridge_stack([510, 512]), {"name": "Unknown"}]
    ordered = order_top_first(cards)
    assert [c.get("instance_id") for c in ordered] == [512, 510, 0, None]


def _bridge_state(stack_cards: list[int], card_ids: list[int] | None = None) -> dict[str, Any]:
    zone: dict[str, Any] = {
        "zone_id": 27,
        "total_count": len(stack_cards),
        "cards": [
            {"instance_id": iid, "grp_id": grp, "owner_id": seat, "controller_id": seat}
            for iid, grp, seat in stack_cards
        ],
    }
    if card_ids is not None:
        zone["card_ids"] = card_ids
    return {
        "ok": True,
        "turn": {"turn_number": 6, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": LOCAL, "life_total": 19, "is_local": True},
            {"seat_id": OPP, "life_total": 15, "is_local": False},
        ],
        "zones": {"stack": zone},
    }


# gsid 125 as the bridge would send it: VisibleCards in the order the objects appeared.
INSERTION = [(BURST, 93905, LOCAL), (PROWESS, 137, LOCAL), (THEIR_SHOCK, 98039, OPP)]


def test_bridge_overlay_publishes_the_zone_order(monkeypatch):
    overlay = publish(monkeypatch, _bridge_state(INSERTION, card_ids=[THEIR_SHOCK, PROWESS, BURST]))
    assert stack_ids(overlay) == [THEIR_SHOCK, PROWESS, BURST]
    assert stack_ids(overlay["zones"]) == [THEIR_SHOCK, PROWESS, BURST]


def test_bridge_overlay_without_card_ids_follows_the_log(monkeypatch):
    log_zones = {"stack": [{"instance_id": iid} for iid in (THEIR_SHOCK, PROWESS, BURST)]}
    overlay = publish(monkeypatch, _bridge_state(INSERTION), log_zones=log_zones)
    assert stack_ids(overlay) == [THEIR_SHOCK, PROWESS, BURST]
    # An older plugin, the log not caught up: the newest instance id is on top.
    overlay = publish(monkeypatch, _bridge_state(INSERTION))
    assert stack_ids(overlay) == [THEIR_SHOCK, PROWESS, BURST]


def test_mac_adapter_sends_the_stack_zones_card_ids():
    dump = ProbeDump([player_node(1, 501), player_node(2, 502, local=True)])
    dump.zone("stack", [(BURST, 2, 2), (PROWESS, 2, 2), (THEIR_SHOCK, 1, 1)])
    results, index = dump.results()
    results[index["stack"]]["CardIds"] = {"$c": "List", "$h": 90, "$items": [THEIR_SHOCK, PROWESS, BURST]}
    assert build_state_ops()[1] == index
    response = fetch_game_state(lambda command, timeout: {"ok": True, "results": results}, None)
    stack = response["zones"]["stack"]
    assert [card["instance_id"] for card in stack["cards"]] == [BURST, PROWESS, THEIR_SHOCK]
    assert stack["card_ids"] == [THEIR_SHOCK, PROWESS, BURST]


# --- consumers read the top at index 0 ------------------------------------------------


def _theirs_over_ours() -> dict[str, Any]:
    """gsid 125: their Shock on top of our prowess trigger and Burst Lightning."""
    state = coach_state(replay_stack_fight_frame(125))
    for card in state["stack"]:
        card["oracle_text"] = {
            BURST: "Kicker {4}\nBurst Lightning deals 2 damage to any target. If this spell was kicked, "
            "it deals 4 damage instead.",
            THEIR_SHOCK: "Shock deals 2 damage to any target.",
        }.get(card["instance_id"], "")
    return state


def test_autopilot_decision_source_without_an_id_is_the_top():
    engine = AutopilotEngine.__new__(AutopilotEngine)
    state = _theirs_over_ours()
    state["decision_context"] = {}
    name, oracle = engine._resolve_decision_source(state)
    assert (name, oracle) == ("Shock", "shock deals 2 damage to any target.")


def test_planner_decision_source_without_an_id_is_the_top():
    planner = ActionPlanner.__new__(ActionPlanner)
    state = _theirs_over_ours()
    decision = PendingDecision(
        (125, 1), "SelectTargets", (DecisionOption("tgt:294", "Emberheart Challenger"),)
    )
    assert planner._decision_source_oracle(decision, state) == "shock deals 2 damage to any target."


def test_own_stack_only_names_our_spell_on_top():
    state = coach_state(replay_stack_fight_frame(123))  # our prowess trigger over our Burst Lightning
    assert stack_ids(state) == [PROWESS, BURST]
    pass_only = PendingDecision((123, 1), "ActionsAvailable", (DecisionOption("pass", "Pass"),))
    assert own_stack_only(pass_only, state) == "Prowess"


def test_numeric_source_is_our_topmost_x_spell():
    state = {
        "players": [{"seat_id": LOCAL, "is_local": True}, {"seat_id": OPP}],
        "local_seat_id": LOCAL,
        "stack": [
            {"instance_id": 402, "name": "Top X", "mana_cost": "{X}{R}", "controller_seat_id": LOCAL},
            {"instance_id": 401, "name": "Bottom X", "mana_cost": "{X}{G}", "controller_seat_id": LOCAL},
        ],
    }
    decision = PendingDecision((1, 1), "CastingTimeOptions", (DecisionOption("idx:0", "X = 1", meta={}),))
    assert numeric_option_source(state, decision)["name"] == "Top X"


def test_ward_payment_without_a_source_reads_the_top():
    """No source id: the top object asks. Their own non-ward trigger on top is not a ward tax."""
    spider = {
        "instance_id": 287,
        "name": "Skyward Spider",
        "controller_seat_id": OPP,
        "oracle_text": "Ward {o2}",
        "type_line": "Creature - Spider",
    }
    ward_trigger = {
        "instance_id": 333,
        "object_kind": "ABILITY",
        "parent_instance_id": 287,
        "controller_seat_id": OPP,
        "oracle_text": "Ward {o2}",
    }
    their_trigger = {
        "instance_id": 334,
        "object_kind": "ABILITY",
        "parent_instance_id": 287,
        "controller_seat_id": OPP,
        "oracle_text": "When this creature attacks, draw a card.",
    }
    state = {
        "players": [{"seat_id": LOCAL, "is_local": True}, {"seat_id": OPP}],
        "local_seat_id": LOCAL,
        "battlefield": [spider],
        "stack": [ward_trigger, {"instance_id": 332, "name": "Burst Lightning", "controller_seat_id": LOCAL}],
    }
    assert [t.entry["instance_id"] for t in pending_ward_triggers(state)] == [333]
    state["stack"] = [their_trigger, ward_trigger]
    assert pending_ward_triggers(state) == []
