"""Regression tests for the non-passable interactive-request safe-default net.

The reported dead-loop: right after a London-mulligan KEEP, the bridge presents
a GroupRequest ("put N cards on the bottom"). The planner can only produce
``pass_priority`` for it; a GroupRequest does not accept a pass, so the submit
is rejected, the "blocked action repeated" guard fires, and the autopilot halts
the turn forever.

These tests pin that:
  - ``_try_gre_bridge_group_default`` bottoms the required N cards (worst keeps)
    and keeps the rest, using MTGA's LondonWorkflow response shape.
  - ``_try_interactive_safe_default`` routes a Group request to that handler and
    reports success instead of passing illegally.
  - ``_plan_cannot_legally_submit`` treats a pass-only plan as "no real submit".
"""

from __future__ import annotations

import importlib
import sys
from unittest.mock import MagicMock


def _load_autopilot():
    try:
        return importlib.import_module("arenamcp.autopilot")
    except ImportError as e:
        if "PIL" in str(e):
            sys.modules["PIL"] = MagicMock()
            sys.modules["PIL.ImageGrab"] = MagicMock()
            return importlib.import_module("arenamcp.autopilot")
        raise


class _FakeBridge:
    def __init__(self, pending):
        self.connected = True
        self._pending = pending
        self.group_calls = []
        self.selection_calls = []
        self.auto_respond_calls = 0

    def connect(self):
        return True

    def get_pending_actions(self):
        return self._pending

    def submit_group(self, groups):
        self.group_calls.append(groups)
        return True

    def submit_selection(self, ids):
        self.selection_calls.append(ids)
        return True

    def auto_respond(self):
        self.auto_respond_calls += 1
        return True


def _engine(bridge):
    ap = _load_autopilot()
    engine = ap.AutopilotEngine.__new__(ap.AutopilotEngine)
    engine._gre_bridge = bridge
    engine._config = MagicMock(dry_run=False)
    engine._path_stats = {}
    engine._gre_bridge_failed_methods = set()
    engine._advice_recorder = None
    return engine, ap


# London-mulligan bottoming: drew 7, mulliganed once → bottom 1 worst card.
_LONDON_PENDING = {
    "ok": True,
    "has_pending": True,
    "request_type": "Group",
    "request_class": "GroupRequest",
    "group_context": "LondonMulligan",
    "group_instance_ids": [101, 102, 103, 104, 105, 106, 107],
    "group_specs": [
        {"zoneType": "Hand", "subZoneType": "Top", "lowerBound": 6, "upperBound": 6},
        {"zoneType": "Library", "subZoneType": "Bottom", "lowerBound": 1, "upperBound": 1},
    ],
}


def _london_game_state():
    # 5 lands + 2 spells; one land is "excess" (>4 kept) and the 7-cmc bomb is
    # the most expensive spell. The excess land should be bottomed first.
    return {
        "_bridge_request_type": "Group",
        "_bridge_request_class": "GroupRequest",
        "_bridge_can_pass": False,
        "decision_context": {"type": "group_selection"},
        "hand": [
            {"instance_id": 101, "name": "Island", "type_line": "Basic Land", "mana_cost": ""},
            {"instance_id": 102, "name": "Island", "type_line": "Basic Land", "mana_cost": ""},
            {"instance_id": 103, "name": "Island", "type_line": "Basic Land", "mana_cost": ""},
            {"instance_id": 104, "name": "Island", "type_line": "Basic Land", "mana_cost": ""},
            {"instance_id": 105, "name": "Island", "type_line": "Basic Land", "mana_cost": ""},
            {"instance_id": 106, "name": "Opt", "type_line": "Instant", "mana_cost": "{U}"},
            {"instance_id": 107, "name": "Big Bomb", "type_line": "Creature", "mana_cost": "{5}{U}{U}"},
        ],
    }


def test_group_default_bottoms_required_count():
    bridge = _FakeBridge(_LONDON_PENDING)
    engine, _ = _engine(bridge)
    result = engine._try_gre_bridge_group_default(_london_game_state(), _LONDON_PENDING)
    assert result is not None and result.success
    assert len(bridge.group_calls) == 1
    groups = bridge.group_calls[0]
    # LondonWorkflow shape: [Hand/Top keep, Library/Bottom putback].
    keep, putback = groups[0], groups[1]
    assert keep["zone"] == "Hand" and keep["sub_zone"] == "Top"
    assert putback["zone"] == "Library" and putback["sub_zone"] == "Bottom"
    # Exactly one card bottomed (GroupSpecs[1].LowerBound == 1).
    assert len(putback["ids"]) == 1
    assert len(keep["ids"]) == 6
    # All 7 cards accounted for, no overlap.
    assert set(keep["ids"]) | set(putback["ids"]) == set(range(101, 108))
    assert not (set(keep["ids"]) & set(putback["ids"]))
    # Worst keep = the 5th (excess) land, not a spell.
    assert putback["ids"] == [105]


def test_safe_default_net_routes_group_to_bottoming():
    bridge = _FakeBridge(_LONDON_PENDING)
    engine, _ = _engine(bridge)
    handled = engine._try_interactive_safe_default(_london_game_state(), "decision_required")
    assert handled is True
    assert len(bridge.group_calls) == 1
    assert bridge.auto_respond_calls == 0  # routed to the typed handler, not AutoRespond


def test_is_non_passable_interactive_excludes_actions_available():
    bridge = _FakeBridge(None)
    engine, _ = _engine(bridge)
    assert engine._is_non_passable_interactive(
        {"_bridge_request_type": "Group", "_bridge_request_class": "GroupRequest", "_bridge_can_pass": False}
    )
    # The normal priority window must NOT engage the net.
    assert not engine._is_non_passable_interactive(
        {
            "_bridge_request_type": "ActionsAvailable",
            "_bridge_request_class": "ActionsAvailableRequest",
            "_bridge_can_pass": True,
        }
    )
    # A passable request must NOT engage the net.
    assert not engine._is_non_passable_interactive(
        {"_bridge_request_type": "Group", "_bridge_can_pass": True}
    )
    # The dedicated Mulligan keep/mull decision is its own path.
    assert not engine._is_non_passable_interactive(
        {"_bridge_request_type": "Mulligan", "_bridge_request_class": "MulliganRequest"}
    )


def test_plan_cannot_legally_submit():
    ap = _load_autopilot()
    Engine = ap.AutopilotEngine
    pass_plan = ap.ActionPlan(actions=[ap.GameAction(action_type=ap.ActionType.PASS_PRIORITY)])
    real_plan = ap.ActionPlan(
        actions=[ap.GameAction(action_type=ap.ActionType.PLAY_LAND, card_name="Island")]
    )
    assert Engine._plan_cannot_legally_submit(pass_plan) is True
    assert Engine._plan_cannot_legally_submit(ap.ActionPlan(actions=[])) is True
    assert Engine._plan_cannot_legally_submit(real_plan) is False
