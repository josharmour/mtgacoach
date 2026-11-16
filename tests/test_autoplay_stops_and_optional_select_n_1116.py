"""Regression and control tests for the 2026-11-16 autoplay stops batch.

Covers all four player-facing fixes from bug_20261116_114909:
1. Auto-queue & 3-consecutive-loss cutoff visibility in Compact UI.
2. Bridge staleness/incompleteness overlay guards during rapid transitions.
3. Graveyard/exile/command owner resolution without leaking onto battlefield/stack.
4. Optional SelectN handling: AllowCancel_Continue decline (via reflect-1 Cancel)
   and min_select == 0 empty selection (via SubmitSelection([])), with controls.
"""

from __future__ import annotations

import json
import os
from unittest.mock import Mock

import pytest
from tests.fakes import TypedBridge, planner_with
from tests.mac_bridge_fakes import MSG, FakeGame, submitted_uint_values

from arenamcp.action_planner import DECLINE_DECISION, ActionPlanner
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine, AutopilotState
from arenamcp.decisions import (
    SELECT_N_DECLINE,
    build_pending_decision,
    decision_from_dict,
    decision_to_dict,
    hand_loss_choice,
    select_n_needs_arena_default,
    select_n_note,
    select_n_without_blind_options,
)
from arenamcp.gre_bridge import GREBridge
from arenamcp.mac_bridge_adapter import MacBridgeAdapter
from arenamcp.standalone_auto_queue import LOSS_STOP_DETAIL, _AutoQueueMixin

# ---------------------------------------------------------------------------
# Fix 1: Compact UI Auto-Queue Visibility & 3-Loss Cutoff Feedback
# ---------------------------------------------------------------------------


class _RecordingUI:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, str]] = []
        self.logs: list[str] = []

    def status(self, key: str, value: str) -> None:
        self.statuses.append((key, value))

    def log(self, message: str) -> None:
        self.logs.append(message)


class _QueueHarness(_AutoQueueMixin):
    def __init__(self) -> None:
        self.ui = _RecordingUI()
        self.queued_enabled: list[bool] = []
        self._auto_queue_enabled = True

    def set_auto_queue(self, enabled: bool) -> None:
        self._auto_queue_enabled = enabled
        self.queued_enabled.append(enabled)


def test_stop_queue_after_losses_emits_detail_and_log():
    harness = _QueueHarness()
    result = harness._stop_queue_after_losses()
    assert result is False
    assert harness.queued_enabled == [False]
    assert harness.ui.statuses[-1] == ("AUTO_QUEUE_DETAIL", LOSS_STOP_DETAIL)
    assert LOSS_STOP_DETAIL in harness.ui.logs


def test_compact_ui_auto_queue_grind_and_three_loss_stop_visibility():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from arenamcp.desktop.compact_coach import CompactCoachPanel

    app = QApplication.instance() or QApplication([])
    panel = CompactCoachPanel()
    try:
        panel.resize(320, 650)
        panel._on_status_changed("AUTO_QUEUE", "ON")
        assert panel.after_match_btn.text() == "Queue: On"
        assert panel.after_match_btn.property("state") == "on"

        panel._on_status_changed("GRIND", "ON")
        assert panel.after_match_btn.text() == "Grind: On"
        assert panel.after_match_btn.property("state") == "on"

        # 3-loss cutoff stops auto-queue while idle: paused state pill + feed log + idle _now
        panel._on_status_changed("GRIND", "OFF")
        panel._on_status_changed("AUTO_QUEUE", "OFF")
        panel._on_status_changed("AUTO_QUEUE_DETAIL", LOSS_STOP_DETAIL)
        assert panel.after_match_btn.text() == "After match…"
        assert panel.after_match_btn.property("state") == "paused"
        assert LOSS_STOP_DETAIL in panel.after_match_btn.toolTip()
        assert any(LOSS_STOP_DETAIL in entry[1] for entry in panel._activity_history)
        assert panel._now is not None and LOSS_STOP_DETAIL in panel._now[1]

        # Repeated identical snapshot status must not duplicate the feed entry
        before_count = sum(1 for entry in panel._activity_history if LOSS_STOP_DETAIL in entry[1])
        panel._on_status_changed("AUTO_QUEUE_DETAIL", LOSS_STOP_DETAIL)
        after_count = sum(1 for entry in panel._activity_history if LOSS_STOP_DETAIL in entry[1])
        assert after_count == before_count

        # Active in-match stop logs to feed without clobbering active turn decision
        panel._activity_history.clear()
        panel._last_state = {"match_id": "m-live", "turn": {"turn_number": 5}}
        panel._now = ("12:00", "Attack with two 3/3 creatures")
        panel._on_status_changed("AUTO_QUEUE_DETAIL", "")
        panel._on_status_changed("AUTO_QUEUE_DETAIL", LOSS_STOP_DETAIL)
        assert any(LOSS_STOP_DETAIL in entry[1] for entry in panel._activity_history)
        assert panel._now[1] == "Attack with two 3/3 creatures"

        # Contrary control: manual Queue Off with empty detail leaves state == "off"
        panel._on_status_changed("AUTO_QUEUE_DETAIL", "")
        panel._on_status_changed("AUTO_QUEUE", "OFF")
        assert panel.after_match_btn.text() == "After match…"
        assert panel.after_match_btn.property("state") == "off"

        # Active queue paused for bridge disconnect shows amber paused state too
        panel._on_status_changed("AUTO_QUEUE", "ON")
        panel._on_status_changed(
            "AUTO_QUEUE_DETAIL",
            "Bridge disconnected — automation paused; reconnect Arena after the match.",
        )
        assert panel.after_match_btn.text() == "Queue: On"
        assert panel.after_match_btn.property("state") == "paused"

        # Layout invariant: stays within 320px width and 650px default compact budget
        app.processEvents()
        assert panel.minimumWidth() <= 320
        assert panel.minimumSizeHint().height() <= 650
    finally:
        panel.close()


# ---------------------------------------------------------------------------
# Fix 2A: Bridge Staleness & Incompleteness Guards
# ---------------------------------------------------------------------------


def _origin_spider_man_log_zones():
    return {
        "battlefield": [
            {
                "instance_id": 374,
                "grp_id": 91001,
                "name": "Spider-Creature",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
                "zone": "battlefield",
                "object_kind": "CARD",
            }
        ],
        "hand": [],
        "graveyard": [],
        "local_graveyard": [],
        "opponent_graveyard": [],
        "stack": [],
        "exile": [],
        "command": [],
        "opponent_hand_count": 3,
        "library_count": 40,
        "library_count_source": "log",
    }


def _players_two_seats():
    return [
        {"seat_id": 1, "is_local": True, "life_total": 20},
        {"seat_id": 2, "is_local": False, "life_total": 20},
    ]


@pytest.mark.parametrize(
    "bridge_state,expected_flag",
    [
        # Explicit game_state_id == 0 must be caught as stale when log is ahead
        (
            {
                "game_state_id": 0,
                "turn": {"turn_number": 12, "game_state_id": 0},
                "players": [],
                "zones": {"battlefield": {"cards": [], "total_count": 0}},
            },
            "_bridge_state_stale",
        ),
        # Explicit turn_number == 0 behind log turn 12 must be caught as stale
        (
            {
                "game_state_id": 245,
                "turn": {"turn_number": 0, "game_state_id": 245},
                "players": [],
                "zones": {"battlefield": {"cards": [], "total_count": 0}},
            },
            "_bridge_state_stale",
        ),
        # Partial/empty battlefield dict with total_count=None while log has battlefield
        (
            {
                "game_state_id": 245,
                "turn": {"turn_number": 12},
                "players": [],
                "zones": {"battlefield": {"cards": [], "total_count": None}},
            },
            "_bridge_state_incomplete",
        ),
        # Missing battlefield key in zones during transition
        (
            {
                "game_state_id": 245,
                "turn": {"turn_number": 12},
                "players": [],
                "zones": {},
            },
            "_bridge_state_incomplete",
        ),
    ],
)
def test_get_bridge_overlay_keeps_log_board_on_zero_stale_or_incomplete(
    monkeypatch, bridge_state, expected_flag
):
    from arenamcp import server

    fake_bridge = Mock()
    fake_bridge.connected = True
    fake_bridge.get_game_state.return_value = bridge_state
    monkeypatch.setattr("arenamcp.gre_bridge.get_bridge", lambda: fake_bridge)

    overlay = server._get_bridge_overlay(
        fallback_turn={"turn_number": 12, "phase": "Phase_Main1"},
        fallback_players=_players_two_seats(),
        fallback_zones=_origin_spider_man_log_zones(),
        fallback_local_seat_id=1,
        fallback_opponent_seat_id=2,
        log_game_state_id=245,
    )
    assert overlay.get(expected_flag) is True
    assert "battlefield" not in overlay


def test_get_bridge_overlay_allows_genuine_empty_battlefield(monkeypatch):
    from arenamcp import server

    fake_bridge = Mock()
    fake_bridge.connected = True
    fake_bridge.get_game_state.return_value = {
        "game_state_id": 245,
        "turn": {"turn_number": 12},
        "players": _players_two_seats(),
        "zones": {"battlefield": {"cards": [], "total_count": 0}},
    }
    monkeypatch.setattr("arenamcp.gre_bridge.get_bridge", lambda: fake_bridge)

    overlay = server._get_bridge_overlay(
        fallback_turn={"turn_number": 12},
        fallback_players=_players_two_seats(),
        fallback_zones=_origin_spider_man_log_zones(),
        fallback_local_seat_id=1,
        fallback_opponent_seat_id=2,
        log_game_state_id=245,
    )
    assert overlay.get("_bridge_state_stale") is not True
    assert overlay.get("_bridge_state_incomplete") is not True
    assert overlay.get("battlefield") == []


def test_gre_bridge_does_not_cache_partial_state_and_invalidates_on_select():
    bridge = GREBridge(port=0)
    bridge._connected = True
    replies = [
        {"ok": True, "game_state_id": 10, "zones": {}},
        {
            "ok": True,
            "game_state_id": 10,
            "zones": {"battlefield": {"cards": [{"instance_id": 374}], "total_count": 2}},
        },
        {
            "ok": True,
            "game_state_id": 11,
            "zones": {"battlefield": {"cards": [{"instance_id": 374}], "total_count": 1}},
        },
        {"ok": True, "submitted_type": "SelectN"},
        {
            "ok": True,
            "game_state_id": 12,
            "zones": {"battlefield": {"cards": [], "total_count": 0}},
        },
    ]

    def send_safe(cmd, timeout=None):
        return replies.pop(0)

    bridge._send_safe = send_safe

    # Partial and truncated snapshots must NOT be cached
    s1 = bridge.get_game_state()
    assert s1["game_state_id"] == 10
    assert bridge._cached_game_state is None
    s1b = bridge.get_game_state()
    assert len(s1b["zones"]["battlefield"]["cards"]) == 1
    assert bridge._cached_game_state is None

    # Complete snapshot IS cached
    s2 = bridge.get_game_state()
    assert s2["game_state_id"] == 11
    assert bridge._cached_game_state is s2
    assert bridge.get_game_state()["game_state_id"] == 11

    # submit_selection invalidates the cache
    assert bridge.submit_selection([]) is True
    assert bridge._cached_game_state is None
    assert bridge.get_game_state()["game_state_id"] == 12


# ---------------------------------------------------------------------------
# Fix 2B: Graveyard/Exile/Command Owner Resolution vs Battlefield/Stack Control
# ---------------------------------------------------------------------------


def test_battlefield_controllers_resolves_graveyard_and_preserves_battlefield_unknown(caplog):
    planner = ActionPlanner(backend=Mock())
    state = {
        "local_seat_id": 1,
        "players": _players_two_seats(),
        "battlefield": [
            # Unknown battlefield control must NEVER fall back to owner_seat_id
            {"instance_id": 500, "name": "Unknown Permanent", "owner_seat_id": 1},
            # Stolen permanent: controller is opponent (2), owner is us (1)
            {
                "instance_id": 501,
                "name": "Stolen Grizzly",
                "owner_seat_id": 1,
                "controller_seat_id": 2,
            },
        ],
        "stack": [
            {"instance_id": 600, "name": "Stack Trigger", "owner_seat_id": 1},
        ],
        "graveyard": [
            # Wary Zone Guard returning land from our graveyard (tgt:847)
            {"instance_id": 847, "name": "Forest", "owner_seat_id": 1},
            {"instance_id": 848, "name": "Swamp", "owner_seat_id": 2},
        ],
        "exile": [
            {"instance_id": 700, "name": "Exiled Spell", "owner_seat_id": 2},
        ],
        "command": [
            {"instance_id": 800, "name": "Commander", "owner_seat_id": 1},
        ],
    }

    local_seat, controllers = planner._battlefield_controllers(state)
    assert local_seat == 1
    # Graveyard / exile / command resolve owner seat when controller_seat_id is absent
    assert controllers[847] == 1
    assert controllers[848] == 2
    assert controllers[700] == 2
    assert controllers[800] == 1
    # Battlefield & stack remain strict
    assert controllers[500] is None
    assert controllers[501] == 2
    assert controllers[600] is None

    # End-to-end target planning: Wary Zone Guard returns tgt:847 without false pause,
    # whereas unknown battlefield permanent tgt:500 is still vetoed for unknown controller.
    gy_state = {
        **state,
        "decision_context": {
            "type": "target_selection",
            "source_id": 910,
            "source_card": "Wary Zone Guard",
            "source_oracle_text": (
                "Whenever Wary Zone Guard attacks, return target land card from your graveyard "
                "to the battlefield tapped."
            ),
        },
    }
    gy_decision = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "SelectTargets",
            "game_state_id": 200,
            "msg_id": 10,
            "can_cancel": True,
            "source_card": "Wary Zone Guard",
            "source_instance_id": 910,
            "target_candidates": [{"targetInstanceId": 847, "grpId": 0}],
            "target_selections": [{"minTargets": 1, "maxTargets": 1}],
        },
        resolve_instance=lambda iid: "Forest" if iid == 847 else "",
    )
    assert gy_decision is not None
    gy_planner = planner_with(
        json.dumps(
            {
                "option_ids": ["tgt:847"],
                "target_controllers": {"tgt:847": "self"},
                "reasoning": "Return our land from graveyard.",
            }
        )
    )
    assert gy_planner.plan_decision_options(gy_decision, gy_state) == ["tgt:847"]

    # Contrary control: targeting battlefield card 500 with unknown control STILL declines
    # specifically because its current controller is unknown!
    bf_state = {
        **state,
        "decision_context": {
            "type": "target_selection",
            "source_id": 911,
            "source_card": "Murder",
            "source_oracle_text": "Destroy target creature.",
        },
    }
    bf_decision = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "SelectTargets",
            "game_state_id": 201,
            "msg_id": 11,
            "can_cancel": True,
            "source_card": "Murder",
            "source_instance_id": 911,
            "target_candidates": [{"targetInstanceId": 500, "grpId": 0}],
            "target_selections": [{"minTargets": 1, "maxTargets": 1}],
        },
        resolve_instance=lambda iid: "Unknown Permanent" if iid == 500 else "",
    )
    assert bf_decision is not None
    bf_planner = planner_with(
        json.dumps(
            {
                "option_ids": ["tgt:500"],
                "target_controllers": {"tgt:500": "opponent"},
                "reasoning": "Destroy it.",
            }
        )
    )
    with caplog.at_level("WARNING"):
        assert bf_planner.plan_decision_options(bf_decision, bf_state) == [DECLINE_DECISION]
    assert "current controller is unknown" in caplog.text


def test_battlefield_controllers_reads_nested_zones_dict():
    planner = ActionPlanner(backend=Mock())
    state = {
        "local_seat_id": 1,
        "players": _players_two_seats(),
        "zones": {
            "graveyard": [{"instance_id": 461, "name": "Mountain", "owner_seat_id": 1}],
            "battlefield": [{"instance_id": 502, "name": "Mystery", "owner_seat_id": 2}],
        },
    }
    _, controllers = planner._battlefield_controllers(state)
    assert controllers[461] == 1
    assert controllers[502] is None


# ---------------------------------------------------------------------------
# Fix 3: Optional SelectN (AllowCancel_Continue Decline + min_select == 0 Empty)
# ---------------------------------------------------------------------------


def _chrome_mox_poll(**overrides):
    poll = {
        "has_pending": True,
        "request_type": "SelectN",
        "request_class": "SelectNRequest",
        "game_state_id": 88,
        "msg_id": 42,
        "can_cancel": True,
        "allow_cancel": "AllowCancel_Continue",
        "select_n_ids": [101, 102],
        "select_n_min": 1,
        "select_n_max": 1,
        "source_card": "Chrome Mox",
        "decision_context": {
            "source_id": 999,
            "source_card": "Chrome Mox",
            "source_oracle_text": (
                "Imprint — When Chrome Mox enters, you may exile a nonartifact, nonland card from your hand."
            ),
            "prompt_text": "Exile a card.",
        },
    }
    poll.update(overrides)
    return poll


def _hand_state():
    return {
        "local_seat_id": 1,
        "players": _players_two_seats(),
        "hand": [
            {
                "instance_id": 101,
                "grp_id": 1001,
                "name": "Lightning Bolt",
                "owner_seat_id": 1,
                "mana_cost": "{R}",
            },
            {
                "instance_id": 102,
                "grp_id": 1002,
                "name": "Counterspell",
                "owner_seat_id": 1,
                "mana_cost": "{U}{U}",
            },
        ],
    }


def test_select_n_allow_cancel_continue_offers_decline_and_roundtrips():
    decision = build_pending_decision(
        _chrome_mox_poll(),
        resolve_instance=lambda iid: {101: "Lightning Bolt", 102: "Counterspell"}.get(iid, ""),
        resolve_card=lambda iid: {
            101: {"name": "Lightning Bolt", "mana_cost": "{R}"},
            102: {"name": "Counterspell", "mana_cost": "{U}{U}"},
        }.get(iid, {}),
    )
    assert decision is not None
    assert decision.allow_cancel == "AllowCancel_Continue"
    assert SELECT_N_DECLINE in decision.option_ids()
    assert decision.selection_is_valid([SELECT_N_DECLINE]) is True
    assert decision.selection_is_valid(["sel:101"]) is True
    # Mixed card + decline is invalid
    assert decision.selection_is_valid(["sel:101", SELECT_N_DECLINE]) is False
    # Round-trips through decision_to_dict / decision_from_dict
    assert decision_from_dict(decision_to_dict(decision)) == decision
    # Optional Chrome Mox imprint must NOT auto-discard without consulting the model
    assert hand_loss_choice(decision, _hand_state()) == ([], "")
    assert "select_n:decline" in select_n_note(decision, _hand_state())


def test_select_n_contrary_controls_do_not_expose_decline():
    # AllowCancel_Abort does NOT get select_n:decline
    abort_decision = build_pending_decision(
        _chrome_mox_poll(allow_cancel="AllowCancel_Abort"),
        resolve_instance=lambda iid: {101: "Lightning Bolt", 102: "Counterspell"}.get(iid, ""),
    )
    assert SELECT_N_DECLINE not in abort_decision.option_ids()

    # AllowCancel_No does NOT get select_n:decline
    no_decision = build_pending_decision(
        _chrome_mox_poll(allow_cancel="AllowCancel_No", can_cancel=False),
        resolve_instance=lambda iid: {101: "Lightning Bolt", 102: "Counterspell"}.get(iid, ""),
    )
    assert SELECT_N_DECLINE not in no_decision.option_ids()

    # Triggered ability stacking even with AllowCancel_Continue keeps stacking policy
    stack_decision = build_pending_decision(
        _chrome_mox_poll(
            select_n_context="SelectionContext_TriggeredAbility",
            select_n_option_context="OptionContext_Stacking",
        ),
        resolve_instance=lambda iid: {101: "Trigger A", 102: "Trigger B"}.get(iid, ""),
    )
    assert SELECT_N_DECLINE not in stack_decision.option_ids()


def test_planner_handles_select_n_decline_and_strips_mixed_pick():
    decision = build_pending_decision(
        _chrome_mox_poll(),
        resolve_instance=lambda iid: {101: "Lightning Bolt", 102: "Counterspell"}.get(iid, ""),
    )
    state = _hand_state()

    planner_decline = planner_with(
        json.dumps({"option_ids": [SELECT_N_DECLINE], "reasoning": "Keep spells."})
    )
    assert planner_decline.plan_decision_options(decision, state) == [SELECT_N_DECLINE]

    # Contrary control: mixed [sel:101, select_n:decline] drops select_n:decline rather than failing
    planner_mixed = planner_with(
        json.dumps({"option_ids": ["sel:101", SELECT_N_DECLINE], "reasoning": "Actually exile Bolt."})
    )
    assert planner_mixed.plan_decision_options(decision, state) == ["sel:101"]


def test_autopilot_submits_select_n_decline_as_guarded_cancel_without_self_cancel_rollback():
    poll = _chrome_mox_poll()
    bridge = TypedBridge(poll)
    state = {
        **_hand_state(),
        "_log_game_state_id": 88,
        "turn": {"turn_number": 1, "active_player": 1, "priority_player": 1},
    }
    engine = AutopilotEngine(
        planner=planner_with(json.dumps({"option_ids": [SELECT_N_DECLINE], "reasoning": "Keep cards."})),
        get_game_state=lambda: state,
        config=AutopilotConfig(auto_execute_delay=0.0),
    )
    engine._gre_bridge = bridge
    engine._undo_activation_after_cancel = Mock()
    engine._withhold_after_self_cancel = Mock()

    handled = engine._try_typed_decision_path(state, "decision")
    assert handled is True
    assert bridge.submitted == [("cancel", (88, 42), True)]
    assert engine._state == AutopilotState.IDLE
    engine._undo_activation_after_cancel.assert_not_called()
    engine._withhold_after_self_cancel.assert_not_called()


def test_min_select_zero_select_n_allows_empty_selection_and_submits_empty_ids():
    # Non-Search SelectN with min_select == 0 and even 0 offered candidates
    poll = {
        "has_pending": True,
        "request_type": "SelectN",
        "request_class": "SelectNRequest",
        "game_state_id": 90,
        "msg_id": 44,
        "can_cancel": False,
        "select_n_ids": [101],
        "select_n_min": 0,
        "select_n_max": 1,
    }
    decision = build_pending_decision(poll, resolve_instance=lambda iid: "Lightning Bolt")
    assert decision is not None
    assert decision.selection_is_valid([]) is True
    assert "option_ids: []" in select_n_note(decision, _hand_state())

    planner = planner_with(json.dumps({"option_ids": [], "reasoning": "Choose zero cards."}))
    assert planner.plan_decision_options(decision, _hand_state()) == []

    # Empty SelectN candidate list when min_select == 0 still builds a legal decision
    empty_poll = {**poll, "select_n_ids": []}
    empty_dec = build_pending_decision(empty_poll, resolve_instance=lambda _: "")
    assert empty_dec is not None
    assert planner.plan_decision_options(empty_dec, _hand_state()) == []

    bridge = TypedBridge(poll)
    state = {
        **_hand_state(),
        "_log_game_state_id": 90,
        "turn": {"turn_number": 1, "active_player": 1, "priority_player": 1},
    }
    engine = AutopilotEngine(
        planner=planner_with(json.dumps({"option_ids": [], "reasoning": "Choose zero cards."})),
        get_game_state=lambda: state,
        config=AutopilotConfig(auto_execute_delay=0.0),
    )
    engine._gre_bridge = bridge
    assert engine._try_typed_decision_path(state, "decision") is True
    assert bridge.submitted == [("selection", [])]


def test_mac_adapter_reads_cancellation_guards_stale_and_submits_empty_for_min_zero():
    select_n_req = {
        "$c": "GreClient.Rules.SelectNRequest",
        "$h": 55,
        "Ids": {"$items": [{"v": 101}]},
        "MinSel": 0,
        "MaxSel": 1,
        "IdType": {"e": "InstanceId", "v": 1},
        "ListType": {"e": "Dynamic", "v": 1},
    }
    game = FakeGame(
        select_n_req,
        getters={
            "Type": {"e": "SelectN", "v": 12},
            "CanCancel": True,
            "CancellationType": {"e": "AllowCancel_Continue", "v": 2},
        },
        gsid=500,
        msg=600,
    )
    adapter = MacBridgeAdapter(game.send)

    pending = adapter.handle({"action": "get_pending_actions"})
    assert pending["allow_cancel"] == "AllowCancel_Continue"

    # Guarded cancel succeeds cleanly and records Cancel (not AutoRespond) with CanCancel expect
    res = adapter.handle(
        {
            "action": "cancel_action",
            "expected_game_state_id": 500,
            "expected_msg_id": 600,
            "require_cancel": True,
        }
    )
    assert res == {"ok": True, "cancelled": True, "request_class": "SelectNRequest"}
    cancel_batch = game.submits()[-1]
    assert any(o.get("method") == "Cancel" for o in cancel_batch)
    assert not any(o.get("method") == "AutoRespond" for o in cancel_batch)
    assert any(o.get("op") == "expect" and o.get("member") == "CanCancel" for o in cancel_batch)

    # Empty min_sel == 0 submission uses SubmitSelection([]), NOT SubmitArbitrary
    adapter.handle({"action": "submit_selection", "ids": []})
    assert submitted_uint_values(game.submits()[-1]) == []
    assert not any(o.get("method") == "SubmitArbitrary" for o in game.submits()[-1])

    # Contrary controls: stale gsid fails cleanly; CanCancel=False with require_cancel refuses AutoRespond
    stale = adapter.handle(
        {
            "action": "cancel_action",
            "expected_game_state_id": 499,
            "expected_msg_id": 600,
            "require_cancel": True,
        }
    )
    assert stale["ok"] is False and "stale command" in stale["error"]

    non_cancel_game = FakeGame(
        select_n_req,
        getters={"Type": {"e": "SelectN", "v": 12}, "CanCancel": False},
        gsid=500,
        msg=601,
    )
    non_cancel_adapter = MacBridgeAdapter(non_cancel_game.send)
    refused = non_cancel_adapter.handle(
        {
            "action": "cancel_action",
            "expected_game_state_id": 500,
            "expected_msg_id": 601,
            "require_cancel": True,
        }
    )
    assert refused["ok"] is False and "CanCancel is false" in refused["error"]
    assert non_cancel_game.submits() == []


def test_bepinex_reflect1_enriches_select_n_and_guards_cancel_without_cs_edits():
    # Minimal reflect_batch responder mimicking BepInEx Plugin.Reflect.cs
    select_n_req = {"$c": "GreClient.Rules.SelectNRequest", "$h": 77}
    orig_msg = {
        "$c": MSG + "GREToClientMessage",
        "$h": 1,
        "gameStateId_": 88,
        "msgId_": 42,
        "allowCancel_": {"e": "AllowCancel_Continue", "v": 2},
    }
    state_holder = {"can_cancel": True, "gsid": 88, "msg": 42}
    pipe_cmds = []

    def send_raw(cmd, timeout=None, expected_generation=None):
        pipe_cmds.append(cmd)
        if cmd["action"] == "get_pending_actions":
            return {
                "ok": True,
                "has_pending": True,
                "request_type": "SelectN",
                "request_class": "SelectNRequest",
                "can_cancel": True,
                "game_state_id": 88,
                "msg_id": 42,
                "select_n_ids": [101, 102],
                "select_n_min": 1,
                "select_n_max": 1,
            }
        if cmd["action"] == "reflect_batch":
            results = []
            for op in cmd["ops"]:
                if op["op"] == "pending":
                    results.append(select_n_req)
                elif op["op"] == "get" and op.get("member") == "OriginalMessage":
                    results.append(
                        {
                            **orig_msg,
                            "gameStateId_": state_holder["gsid"],
                            "msgId_": state_holder["msg"],
                        }
                    )
                elif op["op"] == "get" and op.get("member") == "CanCancel":
                    results.append(state_holder["can_cancel"])
                elif op["op"] == "get" and op.get("member") == "CancellationType":
                    results.append({"e": "AllowCancel_Continue", "v": 2})
                elif op["op"] == "get" and op.get("member") == "Type":
                    results.append({"e": "SelectN", "v": 12})
                elif op["op"] == "expect" and op.get("member") == "CanCancel":
                    if not state_holder["can_cancel"]:
                        return {"ok": False, "error": "expect failed", "failed_op": len(results)}
                    results.append(None)
                else:
                    results.append(None)
            return {"ok": True, "results": results}
        return {"ok": True, "cancelled": True, "request_class": "SelectNRequest"}

    bridge = GREBridge(port=0)
    bridge._connected = True
    bridge.client_runtime = "bepinex"
    bridge._send_command_raw = send_raw
    bridge._mac_adapter = MacBridgeAdapter(send_raw, runtime="bepinex")

    # 1. Poll enriches allow_cancel via reflect-1
    poll = bridge.get_pending_actions()
    assert poll["allow_cancel"] == "AllowCancel_Continue"

    # 2. Guarded cancel runs through reflect-1 adapter, NOT raw BepInEx pipe
    assert bridge.cancel_action(expected_request_id=(88, 42), require_cancel=True) is True
    assert any(c["action"] == "reflect_batch" for c in pipe_cmds)
    assert not any(c["action"] == "cancel_action" for c in pipe_cmds)

    # 3. Legacy unguarded cancel still takes raw BepInEx pipe
    assert bridge.cancel_action() is True
    assert any(c["action"] == "cancel_action" for c in pipe_cmds)

    # 4. Contrary control: legacy BepInEx bridge without reflect-1 adapter fails closed on guarded cancel
    legacy_bridge = GREBridge(port=0)
    legacy_bridge._connected = True
    legacy_bridge.client_runtime = "bepinex"
    legacy_bridge._mac_adapter = None
    legacy_pipe = []
    legacy_bridge._send_command_raw = lambda cmd, timeout=None, expected_generation=None: (
        legacy_pipe.append(cmd) or {"ok": True}
    )
    assert legacy_bridge.cancel_action(expected_request_id=(88, 42), require_cancel=True) is False
    assert legacy_pipe == []


def test_blind_select_n_control_still_uses_arena_default_without_decline():
    # eedf1ae3-style all-blind AllowCancel_No menu keeps Arena default
    poll = {
        "has_pending": True,
        "request_type": "SelectN",
        "game_state_id": 336,
        "msg_id": 402,
        "select_n_ids": [927, 930, 933],
        "select_n_min": 1,
        "select_n_max": 3,
        "can_cancel": False,
        "allow_cancel": "AllowCancel_No",
    }
    decision = build_pending_decision(poll, resolve_instance=lambda _: "")
    assert SELECT_N_DECLINE not in decision.option_ids()
    assert select_n_needs_arena_default(decision) is True

    # Mixed identified + unidentified with AllowCancel_Continue keeps identified + select_n:decline
    mixed_poll = {
        **poll,
        "can_cancel": True,
        "allow_cancel": "AllowCancel_Continue",
    }
    mixed_dec = build_pending_decision(
        mixed_poll,
        resolve_instance=lambda iid: "Opt" if iid == 927 else "",
        resolve_card=lambda iid: {"name": "Opt"} if iid == 927 else {},
    )
    assert select_n_needs_arena_default(mixed_dec) is False
    filtered = select_n_without_blind_options(mixed_dec)
    assert filtered is not None
    assert sorted(filtered.option_ids()) == ["sel:927", SELECT_N_DECLINE]


# ---------------------------------------------------------------------------
# Follow-up: Historical-Loss Latching Fix, "Clear safeguard" & Deck Suggestions
# ---------------------------------------------------------------------------


def test_historical_loss_streak_clears_after_newer_win_across_restore_and_poll(monkeypatch):
    from arenamcp import server

    persisted: dict = {}

    class _MemSettings:
        def get(self, key, default=None):
            return persisted.get(key, default)

        def set(self, key, value, **kwargs):
            # Must persist durably (never save=False on streak resets)
            assert kwargs.get("save", True) is True
            persisted[key] = value

    class _FullQueueHarness(_AutoQueueMixin):
        def __init__(self) -> None:
            self.ui = _RecordingUI()
            self.ui.advice = Mock()
            self.settings = _MemSettings()
            self._auto_queue_enabled = True
            self._auto_queue_since = 100.0
            self._autopilot_status = "AP:ON"

        def _autopilot_control_status(self) -> str:
            return self._autopilot_status

    # 1. Saved streak has 3 losses up to t=200, then player won match w-1 at t=250.
    persisted["auto_queue_streak"] = {
        "since": 100.0,
        "rearmed_at": 100.0,
        "losses": 3,
        "last": "l-3",
        "through": 200.0,
    }
    monkeypatch.setattr(
        server,
        "logged_queue_results",
        lambda since, match_id=None, **kw: [
            {"match_id": "l-3", "outcome": "loss", "arena_completed_at": 200.0},
            {"match_id": "w-1", "outcome": "win", "arena_completed_at": 250.0},
        ],
    )
    h1 = _FullQueueHarness()
    assert h1._queue_loss_guard({}, {}) is True
    assert persisted["auto_queue_streak"]["losses"] == 0
    assert h1._auto_queue_enabled is True

    # 2. _reconcile_saved_loss_streak handles win + subsequent loss (losses -> 1)
    persisted["auto_queue_streak"] = {
        "since": 100.0,
        "rearmed_at": 100.0,
        "losses": 3,
        "last": "l-3",
        "through": 200.0,
    }
    monkeypatch.setattr(
        server,
        "logged_queue_results",
        lambda since, match_id=None, **kw: [
            {"match_id": "l-3", "outcome": "loss", "arena_completed_at": 200.0},
            {"match_id": "w-1", "outcome": "win", "arena_completed_at": 250.0},
            {"match_id": "l-4", "outcome": "loss", "arena_completed_at": 280.0},
        ],
    )
    h2 = _FullQueueHarness()
    assert h2._reconcile_saved_loss_streak()["losses"] == 1

    # 3. Contrary control: 3 losses with NO newer win still triggers stop
    persisted["auto_queue_streak"] = {
        "since": 100.0,
        "rearmed_at": 100.0,
        "losses": 3,
        "last": "l-3",
        "through": 200.0,
    }
    monkeypatch.setattr(
        server,
        "logged_queue_results",
        lambda since, match_id=None, **kw: [
            {"match_id": "l-3", "outcome": "loss", "arena_completed_at": 200.0},
        ],
    )
    h3 = _FullQueueHarness()
    assert h3._queue_loss_guard({}, {}) is False
    assert h3._auto_queue_enabled is False
    h3.ui.advice.assert_called_once()

    # 4. While Auto-queue is off with 3 losses, a live match-scope win clears the streak & UI pause
    h3.ui.statuses.clear()
    monkeypatch.setattr(
        server,
        "get_completed_match_for_navigation",
        lambda: {"match_id": "w-2", "outcome": "win", "completed_at": 300.0},
    )
    h3._poll_auto_queue({})
    assert persisted["auto_queue_streak"]["losses"] == 0
    assert ("AUTO_QUEUE_DETAIL", "") in h3.ui.statuses

    # 5. Contrary control: unresolved / incomplete result while Auto-queue is off does NOT clear losses
    persisted["auto_queue_streak"] = {
        "since": 100.0,
        "rearmed_at": 100.0,
        "losses": 3,
        "last": "l-3",
        "through": 200.0,
    }
    h3._auto_queue_streak = dict(persisted["auto_queue_streak"])
    monkeypatch.setattr(server, "get_completed_match_for_navigation", lambda: {})
    h3._poll_auto_queue({})
    assert persisted["auto_queue_streak"]["losses"] == 3


def test_clear_loss_safeguard_command_resets_streak_and_handles_clock_skew(monkeypatch):
    import time as _time

    from arenamcp import server
    from arenamcp.pipe_adapter import PipeAdapter

    persisted: dict = {
        "auto_queue_streak": {
            "since": 100.0,
            "rearmed_at": 100.0,
            "losses": 3,
            "last": "l-3",
            "through": 200.0,
        }
    }

    class _MemSettings:
        def get(self, key, default=None):
            return persisted.get(key, default)

        def set(self, key, value, **kwargs):
            assert kwargs.get("save", True) is True
            persisted[key] = value

    class _Harness(_AutoQueueMixin):
        def __init__(self) -> None:
            self.ui = _RecordingUI()
            self.settings = _MemSettings()
            self._auto_queue_enabled = False

    future_ts = _time.time() + 120.0
    monkeypatch.setattr(
        server,
        "logged_queue_results",
        lambda since, match_id=None, **kw: [
            {"match_id": "l-3", "outcome": "loss", "arena_completed_at": future_ts}
        ],
    )
    h = _Harness()
    assert h.clear_loss_safeguard(enable_auto_queue=True) is True
    assert h._auto_queue_enabled is True
    assert persisted["auto_queue_streak"]["losses"] == 0
    assert persisted["auto_queue_streak"]["since"] >= future_ts
    assert persisted["auto_queue_streak"]["through"] >= future_ts
    assert persisted["auto_queue_enabled"] is True

    # PipeAdapter dispatches clear_loss_safeguard cleanly
    adapter = PipeAdapter.__new__(PipeAdapter)
    adapter._coach = Mock()
    adapter._dispatch({"cmd": "clear_loss_safeguard", "enable_auto_queue": False})
    adapter._coach.clear_loss_safeguard.assert_called_once_with(enable_auto_queue=False)


def test_loss_safeguard_dialog_actions_and_popover_buttons(monkeypatch, tmp_path):
    from arenamcp.standalone_auto_queue import LOSS_SAFEGUARD_LABEL, LOSS_SAFEGUARD_MESSAGE

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox

    from arenamcp.desktop.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(MainWindow, "_start_session", lambda self: None)
    monkeypatch.setattr(
        "arenamcp.desktop.session_recap.SessionRecapController.offer_previous", lambda self: None
    )

    win = MainWindow()
    try:
        panel = win.coach_panel
        panel.resize(320, 650)
        assert not panel.clear_safeguard_btn.isVisible()
        assert not panel.safeguard_decks_btn.isVisible()

        # Tripping safeguard reveals popover action buttons without growing 320px compact frame
        panel._on_status_changed("AUTO_QUEUE", "OFF")
        panel._on_status_changed("AUTO_QUEUE_DETAIL", LOSS_STOP_DETAIL)
        assert panel.clear_safeguard_btn.isHidden() is False
        assert panel.safeguard_decks_btn.isHidden() is False
        app.processEvents()
        assert panel.minimumWidth() <= 320
        assert panel.minimumSizeHint().height() <= 650

        # Non-tactical LOSS SAFEGUARD advice must NOT overwrite active advice card
        panel._now = ("13:00", "Attack with two 2/2 flyers.")
        win._session.clear_loss_safeguard = Mock()
        win._session.set_auto_queue = Mock()
        win._session.request_collection = Mock()
        win._session.adviceReceived.emit(LOSS_SAFEGUARD_MESSAGE, LOSS_SAFEGUARD_LABEL)
        assert panel._now[1] == "Attack with two 2/2 flyers."

        # Dialog opened with explanation and all 3 actions
        popup = getattr(win, "_loss_safeguard_dialog", None)
        assert popup is not None and popup.isVisible()
        assert "3 consecutive match losses" in popup.text()
        assert "wildcards" in popup.informativeText().lower()
        labels = [b.text() for b in popup.buttons()]
        assert "Clear safeguard" in labels
        assert "Suggest decks (cards & wildcards)" in labels
        assert "Keep paused" in labels

        # 1. Clicking Clear safeguard resets streak & resumes Auto-queue
        next(b for b in popup.buttons() if b.text() == "Clear safeguard").click()
        win._session.clear_loss_safeguard.assert_called_once_with(enable_auto_queue=True)
        assert panel._auto_queue_enabled is True
        assert panel.after_match_btn.text() == "Queue: On"

        # 2. Re-open dialog and click Keep paused (contrary control)
        popup.close()
        panel._on_status_changed("AUTO_QUEUE", "OFF")
        panel._on_status_changed("AUTO_QUEUE_DETAIL", LOSS_STOP_DETAIL)
        win._session.clear_loss_safeguard.reset_mock()
        win._session.adviceReceived.emit(LOSS_SAFEGUARD_MESSAGE, LOSS_SAFEGUARD_LABEL)
        popup2 = win._loss_safeguard_dialog
        next(b for b in popup2.buttons() if popup2.buttonRole(b) == QMessageBox.RejectRole).click()
        win._session.clear_loss_safeguard.assert_not_called()
        assert panel._auto_queue_enabled is False

        # 3. Re-open dialog and click Suggest decks (cards & wildcards)
        popup2.close()
        win._session.clear_loss_safeguard.reset_mock()
        win._session.adviceReceived.emit(LOSS_SAFEGUARD_MESSAGE, LOSS_SAFEGUARD_LABEL)
        popup3 = win._loss_safeguard_dialog
        next(b for b in popup3.buttons() if b.text() == "Suggest decks (cards & wildcards)").click()
        win._session.clear_loss_safeguard.assert_called_once_with(enable_auto_queue=False)
        assert win._stack.currentIndex() == 3
        builder = getattr(win.decks_tab, "_builder", None)
        assert builder is not None and builder.isVisible()
        assert builder.mode.currentIndex() == 1
        win._session.request_collection.assert_called_once()

        # 4. Contrary control: unrelated advice label never opens the safeguard dialog
        popup3.close()
        win._loss_safeguard_dialog = None
        win._session.adviceReceived.emit("Hold removal for the combat step.", "COACH")
        assert win._loss_safeguard_dialog is None
    finally:
        win.close()
