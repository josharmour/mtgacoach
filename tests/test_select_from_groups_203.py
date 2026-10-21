"""Regression tests for bug #203: SelectFromGroups edict prompts.

Match 5834a68d T13 (16:44:06-16:45:17): Liliana, Dreadhorde General -9 sent a
SelectFromGroupsReq with two groups (17 creatures, 6 lands), each minSelect 1 /
maxSelect 1, minTotalSel 1 / maxTotalSel 2. The only bridge answer ever sent
was an EMPTY selectFromGroupsResp {} — illegal when a group's minSelect > 0 —
so Arena answered IllegalRequest x4 and the watchdog's wrong-class attempts
(submit_group / submit_order) were logged as "safe default sent".

These tests pin:
  - the typed builder exposes one labelled option list PER GROUP;
  - the safe default keeps one valuable permanent per group and NEVER
    returns [] when any group's minSelect > 0;
  - the watchdog net reports failure honestly and submits per-group keeps
    through submit_select_from_groups;
  - _try_gre_bridge_order checks minSelect before submitting [].
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import arenamcp.autopilot as ap
from arenamcp.decisions import (
    build_pending_decision,
    select_from_groups_safe_default,
    submit_option,
)

# Match 5834a68d T13 shape: 17 creature instances, 6 land instances.
_CREATURE_IDS = list(range(9000, 9017))
_LAND_IDS = list(range(9100, 9106))

_NAMES = {
    9000: "Hare Apparent",
    9001: "Leonardo, the Winged Savant",
    9002: "Copperline Sentinel",
    9100: "Plains",
    9101: "Island",
    9102: "Swamp",
}


def _edict_pending():
    """The bridge poll as the Mac adapter's shaper now produces it."""
    return {
        "ok": True,
        "has_pending": True,
        "request_type": "SelectFromGroups",
        "request_class": "SelectFromGroupsRequest",
        "can_cancel": False,
        "source_instance_id": 679,
        "select_from_groups": [
            {"group_id": 5003, "ids": list(_CREATURE_IDS), "min": 1, "max": 1},
            {"group_id": 5004, "ids": list(_LAND_IDS), "min": 1, "max": 1},
        ],
        "select_from_groups_min_total": 1,
        "select_from_groups_max_total": 2,
    }


def _raw_payload_pending():
    """The same request in raw-payload form (tolerant parsing path)."""
    return {
        "ok": True,
        "has_pending": True,
        "request_type": "SelectFromGroupsReq",
        "request_class": "SelectFromGroupsRequest",
        "request_payload": {
            "sourceId": 679,
            "minTotalSelections": 1,
            "maxTotalSelections": 2,
            "groups": [
                {"groupIdx": 5003, "ids": list(_CREATURE_IDS), "minSelections": 1, "maxSelections": 1},
                {"groupIdx": 5004, "ids": list(_LAND_IDS), "minSelections": 1, "maxSelections": 1},
            ],
        },
    }


def _resolve_instance(instance_id):
    return _NAMES.get(instance_id, "")


def _edict_game_state():
    return {
        "_bridge_request_type": "SelectFromGroups",
        "_bridge_request_class": "SelectFromGroupsRequest",
        "_bridge_can_pass": False,
        "decision_context": {"type": "select_from_groups"},
        "battlefield": [
            {
                "instance_id": 9000,
                "name": "Hare Apparent",
                "type_line": "Creature — Rabbit",
                "power": "1",
                "toughness": "1",
            },
            {
                "instance_id": 9001,
                "name": "Leonardo, the Winged Savant",
                "type_line": "Legendary Creature — Bird Artificer",
                "power": "4",
                "toughness": "5",
            },
            {
                "instance_id": 9100,
                "name": "Plains",
                "type_line": "Basic Land — Plains",
                "tapped": True,
            },
            {
                "instance_id": 9101,
                "name": "Island",
                "type_line": "Basic Land — Island",
                "tapped": False,
            },
            {
                "instance_id": 9102,
                "name": "Swamp",
                "type_line": "Basic Land — Swamp",
                "tapped": False,
            },
        ],
    }


# --- builder -----------------------------------------------------------------


def test_builder_exposes_per_group_labelled_options():
    decision = build_pending_decision(_edict_pending(), resolve_instance=_resolve_instance)
    assert decision is not None and decision.request_type == "SelectFromGroups"
    by_group = decision._select_from_groups_items()
    assert set(by_group) == {0, 1}
    assert {int(o.meta["instance_id"]) for o in by_group[0]} == set(_CREATURE_IDS)
    assert {int(o.meta["instance_id"]) for o in by_group[1]} == set(_LAND_IDS)
    # Real card names, not bare options.
    labels = {o.label for o in decision.options}
    assert "Leonardo, the Winged Savant" in labels and "Plains" in labels
    assert all(o.option_id.startswith(("sfg:0:", "sfg:1:")) for o in decision.options)
    assert decision.min_select == 1 and decision.max_select == 2


def test_builder_parses_raw_payload_shape():
    decision = build_pending_decision(_raw_payload_pending(), resolve_instance=_resolve_instance)
    assert decision is not None and decision.request_type == "SelectFromGroups"
    assert decision.min_select == 1 and decision.max_select == 2
    assert len(decision._select_from_groups_items()) == 2


def test_selection_validity_requires_one_per_group():
    decision = build_pending_decision(_edict_pending(), resolve_instance=_resolve_instance)
    keep_creature = f"sfg:0:{_CREATURE_IDS[0]}"
    keep_land = f"sfg:1:{_LAND_IDS[0]}"
    assert decision.selection_is_valid([keep_creature, keep_land])
    # Both picks from one group leave the other (minSelect 1) unanswered.
    assert not decision.selection_is_valid([keep_creature])
    assert not decision.selection_is_valid(
        [f"sfg:0:{_CREATURE_IDS[0]}", f"sfg:0:{_CREATURE_IDS[1]}"]
    )
    # Empty is illegal while any group's minSelect > 0.
    assert not decision.selection_is_valid([])


# --- safe default ------------------------------------------------------------


def test_safe_default_keeps_one_valuable_permament_per_group():
    decision = build_pending_decision(_edict_pending(), resolve_instance=_resolve_instance)
    choice = select_from_groups_safe_default(decision, _edict_game_state())
    assert choice is not None and choice != []  # never the illegal empty answer
    assert decision.selection_is_valid(choice)
    groups = decision._select_from_groups_items()
    kept_creatures = [o for o in groups[0] if o.option_id in choice]
    kept_lands = [o for o in groups[1] if o.option_id in choice]
    assert len(kept_creatures) == 1 and len(kept_lands) == 1
    # Creature: highest power (Leonardo 4/5 over Hare Apparent 1/1).
    assert kept_creatures[0].meta["instance_id"] == 9001
    # Land: untapped beats tapped.
    assert kept_lands[0].meta["instance_id"] in (9101, 9102)


def test_safe_default_prefers_commander_then_power():
    state = _edict_game_state()
    state["command"] = [
        {
            "instance_id": 9002,
            "name": "Copperline Sentinel",
            "type_line": "Legendary Creature — Construct",
            "power": "2",
            "is_commander": True,
        }
    ]
    decision = build_pending_decision(_edict_pending(), resolve_instance=_resolve_instance)
    choice = select_from_groups_safe_default(decision, state)
    assert f"sfg:0:9002" in choice


def test_safe_default_empty_only_when_every_group_min_is_zero():
    pending = _edict_pending()
    pending["select_from_groups"] = [
        {"group_id": 5003, "ids": list(_CREATURE_IDS), "min": 0, "max": 2},
        {"group_id": 5004, "ids": list(_LAND_IDS), "min": 0, "max": 2},
    ]
    pending["select_from_groups_min_total"] = 0
    pending["select_from_groups_max_total"] = 2
    decision = build_pending_decision(pending, resolve_instance=_resolve_instance)
    choice = select_from_groups_safe_default(decision, _edict_game_state())
    assert choice == []


# --- typed submission --------------------------------------------------------


class _RecordingBridge:
    def __init__(self, submit_ok=True, pending=None):
        self.connected = True
        self.submit_ok = submit_ok
        self._pending = pending
        self.select_from_groups_calls = []
        self.group_calls = []
        self.order_calls = []
        self.auto_respond_calls = 0

    def connect(self):
        return True

    def get_pending_actions(self):
        return self._pending

    def submit_select_from_groups(self, groups):
        self.select_from_groups_calls.append(groups)
        return self.submit_ok

    def submit_group(self, groups):
        self.group_calls.append(groups)
        return True

    def submit_order(self):
        self.order_calls.append(True)
        return True

    def auto_respond(self):
        self.auto_respond_calls += 1
        return True


def test_submit_option_sfg_builds_per_group_specs():
    decision = build_pending_decision(_edict_pending(), resolve_instance=_resolve_instance)
    bridge = _RecordingBridge()
    choice = select_from_groups_safe_default(decision, _edict_game_state())
    assert submit_option(bridge, decision, choice)
    assert len(bridge.select_from_groups_calls) == 1
    groups = bridge.select_from_groups_calls[0]
    assert [g["groupId"] for g in groups] == [5003, 5004]
    assert all(len(g["ids"]) == 1 for g in groups)


def test_submit_option_sfg_refuses_skipped_group():
    decision = build_pending_decision(_edict_pending(), resolve_instance=_resolve_instance)
    bridge = _RecordingBridge()
    # One pick only: the other group's minSelect makes this illegal.
    assert not submit_option(bridge, decision, [f"sfg:0:{_CREATURE_IDS[0]}"])
    assert bridge.select_from_groups_calls == []


# --- watchdog / safe-default net --------------------------------------------


def _engine(bridge):
    engine = ap.AutopilotEngine.__new__(ap.AutopilotEngine)
    engine._gre_bridge = bridge
    engine._abort_event = threading.Event()
    engine._progress_guard = ap.DecisionProgressGuard()
    engine._progress_game_state = {}
    engine._progress_last_poll = None
    engine._config = MagicMock(dry_run=False)
    engine._path_stats = {}
    engine._gre_bridge_failed_methods = set()
    engine._advice_recorder = None
    return engine


def test_watchdog_net_submits_keeps_not_wrong_class_or_empty():
    bridge = _RecordingBridge(pending=_edict_pending())
    engine = _engine(bridge)
    handled = engine._try_interactive_safe_default(_edict_game_state(), "watchdog")
    assert handled is True
    # The typed path answered; no wrong-class group/order submits, no AutoRespond.
    assert len(bridge.select_from_groups_calls) == 1
    groups = bridge.select_from_groups_calls[0]
    assert len(groups) == 2 and all(len(g["ids"]) == 1 for g in groups)
    assert bridge.group_calls == [] and bridge.order_calls == []
    assert bridge.auto_respond_calls == 0


def test_watchdog_net_reports_failure_when_submission_fails():
    bridge = _RecordingBridge(submit_ok=False, pending=_edict_pending())
    engine = _engine(bridge)
    handled = engine._try_interactive_safe_default(_edict_game_state(), "watchdog")
    # Honest failure — never the false "[WATCHDOG] safe default sent".
    assert handled is False
    assert len(bridge.select_from_groups_calls) == 1
    assert bridge.auto_respond_calls == 0


def test_watchdog_net_does_not_route_sfg_to_group_handler():
    """'SelectFromGroups' contains 'Group'; the London handler must not fire."""
    bridge = _RecordingBridge(pending=_edict_pending())
    engine = _engine(bridge)
    engine._try_interactive_safe_default(_edict_game_state(), "watchdog")
    assert bridge.group_calls == []


# --- order-path empty-submit guard ------------------------------------------


def test_order_path_checks_min_before_empty_submit():
    bridge = _RecordingBridge(pending=_edict_pending())
    engine = _engine(bridge)
    action = ap.GameAction(action_type=ap.ActionType.ORDER_TRIGGERS)
    result = engine._try_gre_bridge_order(action, _edict_game_state())
    assert result is not None and result.success
    # The minSelect-1 edict got real keeps, never the illegal empty submit.
    assert len(bridge.select_from_groups_calls) == 1
    groups = bridge.select_from_groups_calls[0]
    assert len(groups) == 2 and all(len(g["ids"]) == 1 for g in groups)


def test_order_path_empty_submit_only_without_typed_structure():
    """Without a typed decision the legacy empty submit stands (older bridge)."""
    bridge = _RecordingBridge(
        pending={
            "ok": True,
            "has_pending": True,
            "request_type": "SelectFromGroups",
            "request_class": "SelectFromGroupsRequest",
        }
    )
    engine = _engine(bridge)
    action = ap.GameAction(action_type=ap.ActionType.ORDER_TRIGGERS)
    result = engine._try_gre_bridge_order(action, {})
    assert result is not None and result.success
    assert bridge.select_from_groups_calls == [[]]
