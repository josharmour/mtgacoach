"""Planning, spoken actions and execution must share authoritative choices."""

import json
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlanner, ActionType, GameAction
from arenamcp.autopilot import AutopilotEngine
from arenamcp.gre_bridge import enrich_snapshot_from_pending_response
from arenamcp.rules_engine import RulesEngine


@pytest.fixture
def target_state():
    return {
        "local_seat_id": 1,
        "players": [{"seat_id": 1, "is_local": True}, {"seat_id": 2}],
        "battlefield": [
            {
                "name": "Haywire Mite",
                "instance_id": 545,
                "type_line": "Artifact Creature",
                "owner_seat_id": 1,
            },
            {
                "name": "Lumbering Worldwagon",
                "instance_id": 652,
                "type_line": "Artifact — Vehicle",
                "owner_seat_id": 1,
            },
        ],
        "decision_context": {
            "type": "target_selection",
            "source_id": 755,
            "source_card": "Haywire Mite",
            "source_oracle_text": "Exile target noncreature artifact or noncreature enchantment.",
            "raw": {
                "sourceId": 755,
                "targets": [
                    {"targetIdx": 1, "minTargets": 1, "maxTargets": 1, "targets": [{"targetInstanceId": 652}]}
                ],
            },
        },
    }


def test_raw_gre_target_slots_are_authoritative(target_state):
    assert RulesEngine.get_legal_actions(target_state) == ["Select target: Lumbering Worldwagon (YOURS)"]


def test_inferred_noncreature_target_also_excludes_mite(target_state):
    del target_state["decision_context"]["raw"]
    assert RulesEngine.get_legal_actions(target_state) == ["Select target: Lumbering Worldwagon (YOURS)"]


def test_empty_and_unresolved_candidates_never_expand_to_the_board(target_state):
    target_state["decision_context"]["raw"]["targets"][0]["targets"] = []
    assert RulesEngine.get_legal_actions(target_state) == ["No legal targets"]
    target_state["_bridge_target_candidates"] = [{"targetInstanceId": 999}]
    assert RulesEngine.get_legal_actions(target_state) == ["Select target: Object #999"]


def test_authoritative_players_do_not_include_inferred_extra_targets(target_state):
    target_state["decision_context"]["source_oracle_text"] = "Target player draws a card."
    target_state["_bridge_target_candidates"] = [{"targetInstanceId": 2}]
    assert RulesEngine.get_legal_actions(target_state) == ["Select target: Opponent"]


def test_target_labels_use_controller_and_keep_all_candidates(target_state):
    target_state["battlefield"][1]["controller_seat_id"] = 2
    assert RulesEngine.get_legal_actions(target_state) == ["Select target: Lumbering Worldwagon (OPP)"]
    target_state["_bridge_target_candidates"] = [{"targetInstanceId": number} for number in range(900, 905)]
    assert len(RulesEngine.get_legal_actions(target_state)) == 5


def test_bridge_target_candidates_survive_enrichment_and_clear_when_idle(target_state):
    poll = {
        "has_pending": True,
        "request_type": "SelectTargets",
        "target_candidates": [{"targetInstanceId": 652}],
        "request_payload": {"sourceId": 755, "targets": [{}]},
    }
    enrich_snapshot_from_pending_response(target_state, poll, bridge_connected=True)
    assert target_state["_bridge_target_candidates"] == [{"targetInstanceId": 652}]
    assert RulesEngine.get_legal_actions(target_state) == ["Select target: Lumbering Worldwagon (YOURS)"]
    enrich_snapshot_from_pending_response(target_state, {"has_pending": False}, bridge_connected=True)
    assert target_state["_bridge_target_candidates"] is None


def test_target_passthrough_cannot_bypass_explicit_menu(target_state):
    planner = ActionPlanner(backend=Mock())
    legal = RulesEngine.get_legal_actions(target_state)
    bad = GameAction(ActionType.SELECT_TARGET, target_names=["Haywire Mite"])
    good = GameAction(ActionType.SELECT_TARGET, target_names=["Lumbering Worldwagon"])
    assert not planner._is_action_legal(bad, legal, target_state["decision_context"], "SelectTargets")
    assert planner._is_action_legal(good, legal, target_state["decision_context"], "SelectTargets")


def test_spoken_recommendation_is_derived_from_accepted_pick():
    planner = ActionPlanner(backend=Mock())
    planner._last_menu = ["Pass"]
    plan = planner._parse_response(
        json.dumps(
            {
                "actions": [{"pick": 1}],
                "voice_advice": "Sacrifice Haywire Mite to exile itself.",
            }
        ),
        ["Pass"],
    )
    assert plan.voice_advice == "Pass."
    assert plan.spoken_actions() == "Pass."
    plan.overall_strategy = "Sacrifice Haywire Mite to exile itself."
    plan.actions[0].reasoning = plan.overall_strategy
    engine = AutopilotEngine(planner=planner)
    assert "Haywire" not in engine._format_plan_preview(plan)


def test_rejected_target_has_no_surviving_spoken_instruction(target_state):
    planner = ActionPlanner(backend=Mock())
    planner._last_menu = []
    plan = planner._parse_response(
        json.dumps(
            {
                "actions": [{"action_type": "select_target", "target_names": ["Haywire Mite"]}],
                "voice_advice": "Sacrifice Haywire Mite to exile itself.",
            }
        ),
        RulesEngine.get_legal_actions(target_state),
        target_state["decision_context"],
        "SelectTargets",
    )
    assert plan.actions == []
    assert plan.voice_advice == ""


def test_busy_engine_consumes_trigger_without_competing_coaching():
    engine = AutopilotEngine(planner=Mock())
    engine._detect_manual_play = Mock(return_value=False)
    engine._maybe_escape_stuck_window = Mock(return_value=False)
    assert engine.process_trigger({"game_engine_busy": True}, "decision_required") is True
    engine._planner.plan_actions.assert_not_called()
    engine._planner.plan_decision_options.assert_not_called()


def test_busy_planner_lock_is_never_stolen_or_sent_to_coaching():
    engine = AutopilotEngine(planner=Mock())
    assert engine._acquire_lock(blocking=False)
    owner = engine._lock_owner_thread_id
    try:
        assert engine.process_trigger({}, "decision_required") is True
        assert engine._lock.locked()
        assert engine._lock_owner_thread_id == owner
        engine._planner.plan_actions.assert_not_called()
    finally:
        engine._release_lock()


def test_ability_id_collision_does_not_render_unrelated_card(monkeypatch):
    from arenamcp import server
    from arenamcp.gamestate import GameObject, GameObjectKind, GameState, Zone, ZoneType
    from arenamcp.gamestate_decisions import _resolve_request_source_context

    infos = {
        123: {"name": "Lumbering Worldwagon", "type_line": "Artifact — Vehicle", "oracle_text": "Crew 2."},
        76611: {
            "name": "Magma Opus",
            "type_line": "Instant",
            "oracle_text": "Deal 4 damage.",
            "mana_cost": "{6}{U}{R}",
        },
    }
    monkeypatch.setattr(server, "enrich_with_oracle_text", infos.get)
    state = GameState()
    parent = GameObject(instance_id=652, grp_id=123, zone_id=1, owner_seat_id=1)
    ability = GameObject(
        instance_id=700,
        grp_id=76611,
        zone_id=2,
        owner_seat_id=1,
        object_kind=GameObjectKind.ABILITY,
        parent_instance_id=652,
    )
    state.game_objects = {652: parent, 700: ability}
    state.zones[2] = Zone(zone_id=2, zone_type=ZoneType.STACK, object_instance_ids=[700])
    state._card_name_cache.update({123: "Lumbering Worldwagon", 76611: "Magma Opus"})
    state.publish_snapshot()
    enriched = state.get_snapshot()["zones"]["stack"][0]
    assert enriched["name"] == "Lumbering Worldwagon ability"
    assert enriched["type_line"] == "Ability"
    assert enriched["oracle_text"] == "Crew 2."
    assert enriched["mana_cost"] == ""
    assert state._resolve_object_display_name(ability) == "Lumbering Worldwagon's ability"
    assert _resolve_request_source_context(state, 700)["source_oracle_text"] == "Crew 2."


def test_legacy_coaching_does_not_recommend_only_friendly_removal_target(target_state):
    planner = ActionPlanner(backend=Mock())
    plan = planner.plan_actions(
        target_state, "decision_required", RulesEngine.get_legal_actions(target_state)
    )
    assert plan.actions == []
    assert plan.spoken_actions() == ""
    planner._backend.complete.assert_not_called()


def test_emergency_fallback_cannot_target_own_worldwagon(target_state):
    from arenamcp.autopilot import AutopilotConfig

    engine = AutopilotEngine(planner=Mock(), config=AutopilotConfig(dry_run=False))
    engine._gre_bridge = Mock(connected=True)
    engine._gre_bridge.get_pending_actions.return_value = {
        "has_pending": True,
        "request_type": "SelectTargets",
        "can_cancel": True,
        "target_candidates": [{"targetInstanceId": 652}],
        "target_selections": [{"minTargets": 1, "maxTargets": 1}],
    }
    assert engine._try_interactive_safe_default(target_state, "decision_required") is True
    engine._gre_bridge.cancel_action.assert_called_once()
    engine._gre_bridge.submit_targets.assert_not_called()
    assert engine._try_auto_respond_escape(target_state, "stuck target selection") is False
    engine._gre_bridge.auto_respond.assert_not_called()
