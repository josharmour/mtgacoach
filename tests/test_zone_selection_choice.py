"""Zone-instance SelectN handling (bug_20261122_000617: Finale of Devastation).

When resolving Finale of Devastation ("Search your library and/or graveyard..."),
Arena sends a SelectNReq with ``idType: IdType_ZoneInstanceId`` and ``ids: [36, 37]``
(seat 2's Library and Graveyard zone IDs, ``minSel=1, maxSel=2``, prompt 111
"Select zones to search."). Previously those IDs were resolved as card IDs
("Card#36", "Card#37" / "Option 36", "Option 37"), marked blind, and looped on
AutoResp (which Arena rejects with FailureReason_UnexpectedMessage).
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import arenamcp.decisions as decisions
import arenamcp.server as server_module
from arenamcp.action_planner import DECLINE_DECISION, ActionPlanner
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine, AutopilotState
from arenamcp.coach import CoachEngine
from arenamcp.decisions import (
    build_pending_decision,
    format_zone_label,
    is_zone_id_type,
    is_zone_selection_request,
    select_n_needs_arena_default,
    select_n_note,
    submit_option,
)
from arenamcp.gamestate import GameState
from arenamcp.gamestate_decisions import _handle_decision_message
from arenamcp.gamestate_models import GameObject, Zone, ZoneType
from arenamcp.gre_bridge import enrich_snapshot_from_pending_response
from arenamcp.mac_bridge_adapter import AdapterError, MacBridgeAdapter
from arenamcp.rules_engine import RulesEngine

FINALE_ORACLE = (
    "Search your library and/or graveyard for a creature card with mana value X or less "
    "and put it onto the battlefield. If you search your library this way, shuffle. "
    "If X is 10 or more, creatures you control get +X/+X and gain haste until end of turn."
)

# Exact GREMessageType_SelectNReq captured in bug_20261122_000617 (seq 768).
LOG_FINALE_ZONE_REQ = {
    "type": "GREMessageType_SelectNReq",
    "systemSeatIds": [2],
    "msgId": 620,
    "gameStateId": 389,
    "prompt": {
        "promptId": 111,
        "parameters": [
            {"parameterName": "CardId", "type": "ParameterType_Number", "numberValue": 831},
        ],
    },
    "selectNReq": {
        "minSel": 1,
        "maxSel": 2,
        "context": "SelectionContext_Resolution",
        "optionContext": "OptionContext_Resolution",
        "listType": "SelectionListType_Dynamic",
        "ids": [36, 37],
        "prompt": {},
        "idType": "IdType_ZoneInstanceId",
        "sourceId": 831,
        "validationType": "SelectionValidationType_NonRepeatable",
        "minWeight": -2147483648,
        "maxWeight": 2147483647,
    },
    "allowCancel": "AllowCancel_No",
}


def _finale_bridge_poll() -> dict:
    return {
        "has_pending": True,
        "request_type": "SelectN",
        "request_class": "SelectNRequest",
        "game_state_id": 389,
        "msg_id": 620,
        "source_instance_id": 831,
        "select_n_ids": [36, 37],
        "select_n_id_type": "ZoneInstanceId",
        "select_n_is_zone": True,
        "select_n_list_type": "Dynamic",
        "select_n_context": "Resolution",
        "select_n_option_context": "Resolution",
        "select_n_min": 1,
        "select_n_max": 2,
        "can_cancel": False,
        "allow_cancel": "AllowCancel_No",
        "prompt": {
            "promptId": 111,
            "parameters": [
                {"parameterName": "CardId", "type": "Number", "value": 831, "valueCase": "NumberValue"},
            ],
        },
    }


def _make_live_state() -> GameState:
    gs = GameState()
    gs.local_seat_id = 2
    gs.zones = {
        27: Zone(zone_id=27, zone_type=ZoneType.STACK, owner_seat_id=None, object_instance_ids=[831]),
        28: Zone(zone_id=28, zone_type=ZoneType.EXILE, owner_seat_id=None, object_instance_ids=[]),
        33: Zone(zone_id=33, zone_type=ZoneType.GRAVEYARD, owner_seat_id=1, object_instance_ids=[]),
        35: Zone(zone_id=35, zone_type=ZoneType.HAND, owner_seat_id=2, object_instance_ids=[]),
        36: Zone(zone_id=36, zone_type=ZoneType.LIBRARY, owner_seat_id=2, object_instance_ids=[]),
        37: Zone(zone_id=37, zone_type=ZoneType.GRAVEYARD, owner_seat_id=2, object_instance_ids=[572]),
    }
    gs.game_objects[831] = GameObject(
        instance_id=831,
        grp_id=69611,
        zone_id=27,
        owner_seat_id=2,
        controller_seat_id=2,
    )
    return gs


def _finale_snapshot() -> dict:
    return {
        "match_id": "fe4b80ef-f52f-428c-881b-c3b9bff25683",
        "local_seat_id": 2,
        "turn": {
            "turn_number": 15,
            "active_player": 2,
            "priority_player": 2,
            "phase": "Main1",
            "step": "None",
        },
        "players": [
            {"seat_id": 1, "life_total": 23, "is_local": False},
            {"seat_id": 2, "life_total": 22, "is_local": True},
        ],
        "battlefield": [],
        "hand": [],
        "graveyard": [
            {
                "instance_id": 572,
                "name": "Cultivate",
                "owner_seat_id": 2,
                "card_types": ["Sorcery"],
            }
        ],
        "stack": [
            {
                "instance_id": 831,
                "grp_id": 69611,
                "name": "Finale of Devastation",
                "oracle_text": FINALE_ORACLE,
                "owner_seat_id": 2,
                "controller_seat_id": 2,
            }
        ],
        "exile": [],
        "command": [],
        "decision_context": {},
    }


def test_zone_label_and_type_helpers():
    assert is_zone_id_type("IdType_ZoneInstanceId") is True
    assert is_zone_id_type("ZoneInstanceId") is True
    assert is_zone_id_type("IdType_InstanceId") is False
    assert is_zone_id_type("IdType_PromptParameterIndex") is False

    assert format_zone_label(ZoneType.LIBRARY, 2, 2) == "Your Library"
    assert format_zone_label(ZoneType.GRAVEYARD, 2, 2) == "Your Graveyard"
    assert format_zone_label(ZoneType.GRAVEYARD, 1, 2) == "Opponent's Graveyard"
    assert format_zone_label(ZoneType.EXILE, None, 2) == "Exile"
    assert format_zone_label(ZoneType.EXILE, 0, 2) == "Exile"
    assert format_zone_label("zoneType_library", 2, 2) == "Your Library"
    assert format_zone_label(ZoneType.COMMAND, None, 2) == "Command Zone"
    assert format_zone_label(ZoneType.UNKNOWN, 2, 2) == ""


def test_gamestate_decisions_and_bridge_enrichment_resolve_finale_zones(monkeypatch):
    gs = _make_live_state()
    monkeypatch.setattr(
        server_module,
        "get_card_info",
        lambda grp_id: (
            {"name": "Finale of Devastation", "oracle_text": FINALE_ORACLE} if grp_id == 69611 else None
        ),
    )
    monkeypatch.setattr(
        "arenamcp.arena_prompts.prompt_text", lambda prompt, resolver=None: "Select zones to search."
    )

    _handle_decision_message(gs, LOG_FINALE_ZONE_REQ["type"], LOG_FINALE_ZONE_REQ)

    assert gs.pending_decision == "Select Zones"
    ctx = gs.decision_context
    assert ctx["type"] == "choose_zone"
    assert ctx["option_cards"] == ["Your Library", "Your Graveyard"]
    assert ctx["option_ids"] == [36, 37]
    assert [z["label"] for z in ctx["zone_choices"]] == ["Your Library", "Your Graveyard"]
    assert all("Card#" not in label for label in ctx["option_cards"])

    # Bridge enrichment must preserve choose_zone and zone_choices instead of overwriting with "Select Cards".
    snap = _finale_snapshot()
    snap["pending_decision"] = gs.pending_decision
    snap["decision_context"] = dict(ctx)
    enrich_snapshot_from_pending_response(snap, _finale_bridge_poll(), bridge_connected=True)

    assert snap["pending_decision"] == "Select Zones"
    assert snap["decision_context"]["type"] == "choose_zone"
    assert [z["label"] for z in snap["decision_context"]["zone_choices"]] == [
        "Your Library",
        "Your Graveyard",
    ]

    # RulesEngine and Coach manual display both surface the zone labels.
    assert RulesEngine.get_legal_actions(snap) == ["Choose zone: Your Library", "Choose zone: Your Graveyard"]
    coach = CoachEngine.__new__(CoachEngine)
    coach._clean_oracle_for_prompt = lambda text: text
    coach._remove_reminder_text = lambda text: text
    formatted = "\n".join(coach._format_decision_lines(snap))
    assert "SELECT ZONE(S) for Finale of Devastation" in formatted
    assert "Your Library, Your Graveyard" in formatted


def test_build_pending_decision_and_submit_for_finale_zones(monkeypatch):
    gs = _make_live_state()
    monkeypatch.setattr(decisions, "_live_game_state", lambda: gs)
    monkeypatch.setattr(
        "arenamcp.arena_prompts.prompt_text", lambda prompt, resolver=None: "Select zones to search."
    )

    decision = build_pending_decision(_finale_bridge_poll())
    assert decision is not None
    assert decision.request_type == "SelectN"
    assert decision.min_select == 1
    assert decision.max_select == 2
    assert decision.prompt_text == "Select zones to search."
    assert [(o.option_id, o.label) for o in decision.options] == [
        ("sel:36", "Your Library"),
        ("sel:37", "Your Graveyard"),
    ]
    assert all(o.meta["choice"] == "zone" and o.meta["identity_known"] is True for o in decision.options)
    assert select_n_needs_arena_default(decision) is False

    # Note clearly names the zone choice, Arena's prompt, Finale of Devastation, and multi-select bounds.
    note = select_n_note(decision, _finale_snapshot())
    assert "ZONE CHOICE" in note
    assert "Select zones to search." in note
    assert "Finale of Devastation" in note
    assert "1 to 2 zones" in note

    # Single-zone and multi-zone selections both validate and submit the zone IDs.
    submitted: list[list[int]] = []
    bridge = SimpleNamespace(submit_selection=lambda ids: submitted.append(list(ids)) or True)
    assert submit_option(bridge, decision, ["sel:36"]) is True
    assert submit_option(bridge, decision, ["sel:36", "sel:37"]) is True
    assert submit_option(bridge, decision, []) is False
    assert submitted == [[36], [36, 37]]


def test_control_instance_id_select_n_is_not_treated_as_zone(monkeypatch):
    gs = _make_live_state()
    gs.decision_context = {
        "type": "choose_zone",
        "id_type": "IdType_ZoneInstanceId",
        "option_ids": [36, 37],
        "zone_choices": [{"id": 36, "label": "Your Library"}],
    }
    monkeypatch.setattr(decisions, "_live_game_state", lambda: gs)

    # Control: an ordinary InstanceId SelectN must NOT be hijacked by stale zone context.
    poll = {
        "has_pending": True,
        "request_type": "SelectN",
        "request_class": "SelectNRequest",
        "game_state_id": 395,
        "msg_id": 640,
        "select_n_ids": [36],
        "select_n_id_type": "InstanceId",
        "select_n_is_zone": False,
        "select_n_min": 1,
        "select_n_max": 1,
    }
    assert is_zone_selection_request(poll) is False
    decision = build_pending_decision(
        poll, resolve_instance=lambda iid: "Llanowar Elves" if iid == 36 else ""
    )
    assert decision is not None
    assert decision.options[0].label == "Llanowar Elves"
    assert decision.options[0].meta.get("choice") is None


def test_planner_and_autopilot_choose_library_for_finale(monkeypatch):
    gs = _make_live_state()
    monkeypatch.setattr(decisions, "_live_game_state", lambda: gs)
    monkeypatch.setattr(
        "arenamcp.arena_prompts.prompt_text", lambda prompt, resolver=None: "Select zones to search."
    )

    poll = _finale_bridge_poll()
    snap = _finale_snapshot()

    llm_calls: list[tuple] = []

    class _FakePlanner:
        def plan_decision_options(self, decision, game_state):
            llm_calls.append((decision.request_type, [(o.option_id, o.label) for o in decision.options]))
            assert select_n_needs_arena_default(decision) is False
            return ["sel:36"]

    submitted: list[list[int]] = []
    auto_responded: list[bool] = []

    class _FakeBridge:
        connected = True

        def get_pending_actions(self):
            return dict(poll)

        def submit_selection(self, ids):
            submitted.append(list(ids))
            return True

        def auto_respond(self):
            auto_responded.append(True)
            return True

    engine = AutopilotEngine(
        planner=_FakePlanner(),
        get_game_state=lambda: dict(snap),
        config=AutopilotConfig(dry_run=False),
    )
    engine._gre_bridge = _FakeBridge()
    handled = engine._try_typed_decision_path(snap, "decision_required")
    assert handled is True
    assert llm_calls == [("SelectN", [("sel:36", "Your Library"), ("sel:37", "Your Graveyard")])]
    assert submitted == [[36]]
    assert auto_responded == []


def test_unresolved_zone_ids_stay_blind_and_never_call_auto_respond(monkeypatch):
    # Empty GameState.zones and no zone_choices: zone IDs cannot be identified.
    empty_gs = GameState()
    empty_gs.local_seat_id = 2
    monkeypatch.setattr(decisions, "_live_game_state", lambda: empty_gs)

    poll = _finale_bridge_poll()
    decision = build_pending_decision(poll)
    assert decision is not None
    assert [(o.option_id, o.label) for o in decision.options] == [
        ("sel:36", "Zone #36"),
        ("sel:37", "Zone #37"),
    ]
    assert all(o.meta.get("identity_known") is False and "choice" not in o.meta for o in decision.options)
    assert select_n_needs_arena_default(decision) is True

    # Planner declines without calling the LLM.
    planner = ActionPlanner.__new__(ActionPlanner)
    planner._last_decision_option_ids = []
    planner._last_decision_reasoning = ""
    planner._last_decision_trace = {}
    planner._land_strand_notes = []
    planner._land_strand_excluded = set()
    planner._llm_decision_options = lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("must not call LLM")
    )
    assert planner.plan_decision_options(decision, _finale_snapshot()) == [DECLINE_DECISION]

    # Autopilot refuses AutoResp on zone selections and pauses cleanly for manual.
    auto_responded: list[bool] = []

    class _FakeBridge:
        connected = True

        def get_pending_actions(self):
            return dict(poll)

        def auto_respond(self):
            auto_responded.append(True)
            return True

    engine = AutopilotEngine(
        planner=SimpleNamespace(plan_decision_options=lambda d, s: [DECLINE_DECISION]),
        get_game_state=lambda: _finale_snapshot(),
        config=AutopilotConfig(dry_run=False),
    )
    engine._gre_bridge = _FakeBridge()
    handled = engine._try_typed_decision_path(_finale_snapshot(), "decision_required")
    assert handled is True
    assert auto_responded == []
    assert engine.state == AutopilotState.PAUSED


def test_generic_blind_select_n_auto_respond_does_not_loop_when_rejected(monkeypatch):
    # Non-zone blind SelectN (e.g. unidentified objects): tries AutoResp once,
    # records submission in RequestTracker, and hands to manual if rejected instead of looping.
    empty_gs = GameState()
    empty_gs.local_seat_id = 2
    monkeypatch.setattr(decisions, "_live_game_state", lambda: empty_gs)

    poll = {
        "has_pending": True,
        "request_type": "SelectN",
        "request_class": "SelectNRequest",
        "game_state_id": 400,
        "msg_id": 700,
        "select_n_ids": [901, 902],
        "select_n_id_type": "InstanceId",
        "select_n_min": 1,
        "select_n_max": 1,
        "can_cancel": False,
    }
    snap = _finale_snapshot()
    auto_responded: list[bool] = []

    current_poll = dict(poll)

    class _FakeBridge:
        connected = True

        def get_pending_actions(self):
            return dict(current_poll)

        def auto_respond(self):
            auto_responded.append(True)
            return True

    engine = AutopilotEngine(
        planner=SimpleNamespace(plan_decision_options=lambda d, s: [DECLINE_DECISION]),
        get_game_state=lambda: dict(snap),
        config=AutopilotConfig(dry_run=False),
    )
    engine._gre_bridge = _FakeBridge()
    # First attempt calls auto_respond once.
    assert engine._try_typed_decision_path(snap, "decision_required") is True
    assert len(auto_responded) == 1

    # Immediate re-poll (while in-flight) waits rather than machine-gunning auto_respond.
    current_poll = {**poll, "game_state_id": 401, "msg_id": 703}
    assert engine._try_typed_decision_path(snap, "decision_required") is True
    assert len(auto_responded) == 1

    # After REJECT_GRACE_S, the same fingerprint settles as REJECTED and hands off to manual.
    rec = next(iter(engine._request_tracker._records.values()))
    rec.submitted_at = time.monotonic() - (engine._request_tracker.REJECT_GRACE_S + 1.0)
    current_poll = {**poll, "game_state_id": 402, "msg_id": 706}
    assert engine._try_typed_decision_path(snap, "decision_required") is True
    assert len(auto_responded) == 1
    assert engine.state == AutopilotState.PAUSED


def test_mac_bridge_adapter_refuses_auto_respond_on_zone_selection():
    snapshot = SimpleNamespace(
        request={
            "$t": "SelectNRequest",
            "ListType": {"e": "Dynamic", "v": 1},
            "StaticList": {"e": "None", "v": 0},
            "IdType": {"e": "ZoneInstanceId", "v": 5},
        },
        getters={"IsZoneSelection": True},
        request_class="SelectNRequest",
        game_state_id=389,
        msg_id=620,
    )
    with pytest.raises(AdapterError, match="Zone selection requires explicit zone IDs"):
        MacBridgeAdapter._require_automatic_response_safe(snapshot)


def test_stale_decision_context_does_not_leak_source_or_decline_into_zone_choice(monkeypatch):
    gs = _make_live_state()
    gs.decision_context = {
        "request_id": [300, 500],  # Stale prior decision
        "prompt_source_id": 999,
        "allow_cancel": "AllowCancel_Continue",
        "type": "choose_zone",
        "zone_choices": [{"id": 36, "label": "Your Library"}],
    }
    monkeypatch.setattr(decisions, "_live_game_state", lambda: gs)

    # BepInEx-style poll: no source_instance_id or allow_cancel, can_cancel=True (e.g. Abort).
    poll = {
        "has_pending": True,
        "request_type": "SelectN",
        "request_class": "SelectNRequest",
        "game_state_id": 389,
        "msg_id": 620,
        "select_n_ids": [36, 37],
        "select_n_id_type": "ZoneInstanceId",
        "select_n_is_zone": True,
        "select_n_min": 1,
        "select_n_max": 2,
        "can_cancel": True,
        "prompt": {
            "promptId": 111,
            "parameters": [
                {"parameterName": "CardId", "type": "Number", "value": 831, "valueCase": "NumberValue"},
            ],
        },
    }
    decision = build_pending_decision(poll)
    assert decision is not None
    assert decision.source_instance_id == 831
    assert decision.allow_cancel == ""
    assert decisions.SELECT_N_DECLINE not in decision.option_ids()

    # Coach manual prompt ignores stale zone_choices when dec_type moved on.
    snap = _finale_snapshot()
    snap["pending_decision"] = "Discard"
    snap["decision_context"] = {
        "type": "discard",
        "count": 1,
        "zone_choices": [{"id": 36, "label": "Your Library"}],
    }
    coach = CoachEngine.__new__(CoachEngine)
    coach._clean_oracle_for_prompt = lambda text: text
    coach._remove_reminder_text = lambda text: text
    formatted = "\n".join(coach._format_decision_lines(snap))
    assert "SELECT ZONE(S)" not in formatted
    assert "DISCARD" in formatted


def test_transient_empty_poll_after_auto_respond_does_not_rearm_loop(monkeypatch):
    empty_gs = GameState()
    empty_gs.local_seat_id = 2
    monkeypatch.setattr(decisions, "_live_game_state", lambda: empty_gs)

    poll = {
        "has_pending": True,
        "request_type": "SelectN",
        "request_class": "SelectNRequest",
        "game_state_id": 400,
        "msg_id": 700,
        "select_n_ids": [901, 902],
        "select_n_id_type": "InstanceId",
        "select_n_min": 1,
        "select_n_max": 1,
        "can_cancel": False,
    }
    snap = _finale_snapshot()
    auto_responded: list[bool] = []
    current_poll = dict(poll)

    class _FakeBridge:
        connected = True

        def get_pending_actions(self):
            return dict(current_poll)

        def auto_respond(self):
            auto_responded.append(True)
            return True

    engine = AutopilotEngine(
        planner=SimpleNamespace(plan_decision_options=lambda d, s: [DECLINE_DECISION]),
        get_game_state=lambda: dict(snap),
        config=AutopilotConfig(dry_run=False),
    )
    engine._gre_bridge = _FakeBridge()

    assert engine._try_typed_decision_path(snap, "decision_required") is True
    assert len(auto_responded) == 1

    # Transient empty poll while Arena consumes AutoResp and before IllegalRequest returns.
    current_poll = {"has_pending": False}
    assert engine._try_typed_decision_path(snap, "decision_required") is True

    # Re-issued identical SelectN in the same game window must NOT re-fire auto_respond.
    current_poll = {**poll, "game_state_id": 401, "msg_id": 703}
    assert engine._try_typed_decision_path(snap, "decision_required") is True
    assert len(auto_responded) == 1
    assert engine.state == AutopilotState.PAUSED


def test_safe_default_never_submits_arbitrary_or_auto_respond_on_zone_selection(monkeypatch):
    empty_gs = GameState()
    empty_gs.local_seat_id = 2
    monkeypatch.setattr(decisions, "_live_game_state", lambda: empty_gs)

    poll = {
        **_finale_bridge_poll(),
        "select_n_ids": [],  # Unanswerable zone selection
    }
    calls: list[str] = []

    class _FakeBridge:
        connected = True

        def get_pending_actions(self):
            return dict(poll)

        def submit_selection(self, ids):
            calls.append(f"submit_selection:{ids}")
            return True

        def auto_respond(self):
            calls.append("auto_respond")
            return True

    engine = AutopilotEngine(
        planner=SimpleNamespace(),
        get_game_state=lambda: _finale_snapshot(),
        config=AutopilotConfig(dry_run=False),
    )
    engine._gre_bridge = _FakeBridge()
    snap = _finale_snapshot()
    snap["_bridge_request_type"] = "SelectN"
    snap["_bridge_request_class"] = "SelectNRequest"
    assert engine._try_interactive_safe_default(snap, "watchdog") is False
    assert calls == []
