"""#281 player poison pipeline; #247 Odin/decking remain separate controls."""

from copy import deepcopy

from arenamcp.autopilot_progress import decision_semantics
from arenamcp.board_assessment import assess
from arenamcp.gamestate import GameState
from arenamcp.gamestate_transforms import _counter_type_name
from arenamcp.poison_clock import poison_threat


def annotation(i, amount, *, seat=1, added=True):
    return {
        "id": i,
        "affectedIds": [seat],
        "type": ["AnnotationType_CounterAdded" if added else "AnnotationType_CounterRemoved"],
        "details": [
            {"key": "counter_type", "valueInt32": [3]},
            {"key": "transaction_amount", "valueInt32": [amount]},
        ],
    }


def state(poison=9):
    return {
        "match_id": "poison-281",
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "turn": {"turn_number": 14, "active_player": 1, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 23, "counters": {"Poison": poison}},
            {"seat_id": 2, "life_total": 19, "counters": {}},
        ],
        "battlefield": [
            {
                "instance_id": 300,
                "name": "Ichormoon Gauntlet",
                "type_line": "Artifact",
                "controller_seat_id": 2,
                "oracle_text": 'Planeswalkers you control have "[0]: Proliferate."',
            }
        ],
        "hand": [],
        "stack": [],
    }


def test_player_counter_annotations_publish_preserve_dedupe_and_reset():
    game = GameState()
    game.update_from_message({"players": [{"systemSeatNumber": 1, "lifeTotal": 23}]})
    assert _counter_type_name({"counter_type": 3}) == "Poison"
    game.update_from_message({"annotations": [annotation(1, 9)]})
    game.update_from_message(
        {"players": [{"systemSeatNumber": 1, "lifeTotal": 22}], "annotations": [annotation(1, 9)]}
    )
    assert game.players[1].to_dict()["counters"] == {"Poison": 9}
    assert len([e for e in game.recent_events if e["type"] == "player_counter_changed"]) == 1
    game.update_from_message({"annotations": [annotation(2, 1, added=False)]})
    assert game.players[1].counters == {"Poison": 8}
    restored = GameState()
    assert restored.restore_checkpoint(game.export_checkpoint())
    assert restored.players[1].counters == {"Poison": 8}
    restored.update_from_message({"annotations": [annotation(1, 9), annotation(2, 1, added=False)]})
    assert restored.players[1].counters == {"Poison": 8}
    game.reset()
    game.update_from_message({"players": [{"systemSeatNumber": 1}], "annotations": [annotation(1, 1)]})
    assert game.players[1].counters == {"Poison": 1}


def test_poison_alone_invalidates_assessment_and_progress():
    before = state(8)
    after = state(9)
    a, b = assess(before), assess(after)
    assert a and b and a is not b
    assert not a.poison_urgent and b.poison_urgent and b.survival_mode
    assert "9/10 poison" in b.facts_line()
    assert "1 proliferate resolution(s) can finish us" in b.prompt_block()
    assert "life-only search lines do not establish survival" in b.role_reason
    poll = {"request_type": "DeclareBlockersReq"}
    assert decision_semantics(poll, before) != decision_semantics(poll, after)


def test_zero_poison_proliferate_does_not_start_poison():
    assert poison_threat(state(0)) is None


def test_our_proliferator_is_not_an_opponent_poison_clock():
    snapshot = state()
    snapshot["battlefield"][0]["controller_seat_id"] = 1
    threat = poison_threat(snapshot)
    assert threat and threat.counters == 9 and not threat.urgent
    assert not threat.engines and not threat.proliferators


def test_one_shot_jump_is_not_a_recurring_rate_and_stale_gains_expire():
    snapshot = state()
    snapshot["recent_events"] = [
        {
            "type": "player_counter_changed",
            "seat_id": 1,
            "counter_type": "Poison",
            "amount": 9,
            "turn": 11,
        }
    ]
    assert poison_threat(snapshot).turns is None
    snapshot["recent_events"].append(
        {
            "type": "player_counter_changed",
            "seat_id": 1,
            "counter_type": "Poison",
            "amount": 1,
            "turn": 13,
        }
    )
    threat = poison_threat(snapshot)
    assert threat.rate == 1 and threat.turns == 1
    assert "if recent rate" in threat.fact()
    snapshot["turn"]["turn_number"] = 20
    assert poison_threat(snapshot).turns is None


def test_removed_engine_stops_rate_projection_but_keeps_poison_total():
    snapshot = state(8)
    snapshot["battlefield"] = []
    snapshot["recent_events"] = [
        {"type": "player_counter_changed", "seat_id": 1, "counter_type": "Poison", "amount": 1, "turn": t}
        for t in (11, 13)
    ]
    threat = poison_threat(snapshot)
    assert threat.counters == 8 and threat.turns is None


def test_enemy_gains_do_not_create_our_rate():
    snapshot = state(8)
    snapshot["recent_events"] = [
        {"type": "player_counter_changed", "seat_id": 2, "counter_type": "Poison", "amount": 1, "turn": t}
        for t in (11, 13)
    ]
    assert poison_threat(snapshot).turns is None


def test_rate_change_invalidates_assessment_without_counter_change():
    snapshot = state(8)
    a = assess(snapshot)
    changed = deepcopy(snapshot)
    changed["recent_events"] = [
        {"type": "player_counter_changed", "seat_id": 1, "counter_type": "Poison", "amount": 1, "turn": t}
        for t in (11, 13)
    ]
    b = assess(changed)
    assert a and b and a is not b
    assert a.poison_turns is None and b.poison_turns == 2


def test_native_player_counter_reads_and_bridge_merge_preserve_known_totals():
    from arenamcp.mac_game_state import fetch_game_state
    from arenamcp.server import _normalize_bridge_players
    from test_mac_game_state import enum, listing, scripted_probe

    probe, _ = scripted_probe()
    probe.players[0]["counterDatas_"] = listing({"type_": enum("Poison", 3), "count_": 9})
    rows = fetch_game_state(probe.send, None)["players"]
    assert rows[0]["counters"] == {"Poison": 9}
    assert "CounterDatas" not in next(op for op in probe.batches[0] if op.get("member") == "Players").get(
        "skip", []
    )
    fallback = [{"seat_id": 1, "counters": {"Poison": 8}}]
    assert _normalize_bridge_players(rows[:1], fallback)[0]["counters"] == {"Poison": 9}
    probe.players[0]["counterDatas_"] = {"$n": 2, "$items": [{"type_": enum("Poison", 3), "count_": 9}]}
    incomplete = fetch_game_state(probe.send, None)["players"]
    assert "counters" not in incomplete[0]
    assert _normalize_bridge_players(incomplete[:1], fallback)[0]["counters"] == {"Poison": 8}
    probe.players[0]["counterDatas_"] = listing()
    empty = fetch_game_state(probe.send, None)["players"]
    assert _normalize_bridge_players(empty[:1], fallback)[0]["counters"] == {}


def test_resolved_engine_text_invalidates_poison_assessment():
    snapshot = state()
    snapshot["battlefield"][0]["oracle_text"] = ""
    before = assess(snapshot)
    after = assess(state())
    assert before and after and before is not after
    assert not before.poison_urgent and after.poison_urgent
