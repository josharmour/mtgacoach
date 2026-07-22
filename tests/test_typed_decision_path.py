"""Phase B wiring: autopilot's typed-decision path + planner option picking.

SelectTargets/SelectN/Mulligan flow as structured options end-to-end; the
LLM answers with option ids; submission is by id; legacy string planning
never runs for these families when the bridge serves them.
"""

import arenamcp.autopilot as autopilot_module
from arenamcp.action_planner import ActionPlanner
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine
from arenamcp.decisions import build_pending_decision


class _Backend:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def complete(self, system, user, *a, **k):
        self.calls += 1
        return self.response


def _planner_with(response) -> ActionPlanner:
    p = ActionPlanner.__new__(ActionPlanner)
    p._backend = _Backend(response)
    p._timeout = 5.0
    return p


class _TypedBridge:
    def __init__(self, poll):
        self.connected = True
        self.poll_resp = poll
        self.submitted = []

    def connect(self):
        return True

    def get_pending_actions(self):
        return self.poll_resp

    def submit_targets(self, iid):
        self.submitted.append(("targets", iid))
        return True

    def submit_mulligan(self, keep):
        self.submitted.append(("mulligan", keep))
        return True

    def submit_selection(self, ids):
        self.submitted.append(("selection", ids))
        return True

    def submit_pass(self):
        self.submitted.append(("pass",))
        return True


class _DummyMapper:
    window_rect = (0, 0, 100, 100)
    cache_size = 0

    def refresh_window(self):
        return self.window_rect

    def get_button_coord(self, name):
        return None


class _DummyController:
    def focus_mtga_window(self):
        return None


_TARGET_POLL = {
    "has_pending": True,
    "request_type": "SelectTargets",
    "target_candidates": [
        {"targetInstanceId": 2, "grpId": 0},
        {"targetInstanceId": 161, "grpId": 91644},
    ],
    "target_selections": [{"minTargets": 1, "maxTargets": 1}],
}


def _engine(monkeypatch, bridge, planner) -> AutopilotEngine:
    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: bridge)
    return AutopilotEngine(
        planner=planner,
        mapper=_DummyMapper(),
        controller=_DummyController(),
        get_game_state=lambda: {},
        config=AutopilotConfig(dry_run=False),
    )


def _state():
    return {
        "turn": {"turn_number": 5, "phase": "Phase_Main1"},
        "players": [{"seat_id": 1, "is_local": True}],
        "_bridge_connected": True,
        "_bridge_request_type": "SelectTargets",
        "pending_decision": "Select Targets",
        "battlefield": [],
        "hand": [],
    }


def test_typed_path_submits_llm_choice_by_id(monkeypatch):
    bridge = _TypedBridge(_TARGET_POLL)
    planner = _planner_with('{"option_ids": ["tgt:2"], "reasoning": "opponent"}')
    eng = _engine(monkeypatch, bridge, planner)
    handled = eng._try_typed_decision_path(_state(), "decision_required")
    assert handled is True
    assert bridge.submitted == [("targets", [2])]


def test_typed_path_falls_back_deterministically_on_garbage_llm(monkeypatch):
    bridge = _TypedBridge(_TARGET_POLL)
    planner = _planner_with("completely invalid")
    eng = _engine(monkeypatch, bridge, planner)
    handled = eng._try_typed_decision_path(_state(), "decision_required")
    assert handled is True
    # Deterministic pick = first option, still submitted BY ID.
    assert bridge.submitted == [("targets", [2])]


def test_typed_path_rejects_hallucinated_ids_then_falls_back(monkeypatch):
    bridge = _TypedBridge(_TARGET_POLL)
    planner = _planner_with('{"option_ids": ["tgt:9999"], "reasoning": "x"}')
    eng = _engine(monkeypatch, bridge, planner)
    handled = eng._try_typed_decision_path(_state(), "decision_required")
    assert handled is True
    assert bridge.submitted == [("targets", [2])]  # mechanical fallback


def test_typed_path_handles_actions_available(monkeypatch):
    poll = {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "actions": [{"actionType": "ActionType_Pass"}],
        "can_pass": True,
    }
    bridge = _TypedBridge(poll)
    planner = _planner_with('{"option_ids": ["pass"]}')
    eng = _engine(monkeypatch, bridge, planner)
    # ActionsAvailable migrated to typed decision path in Phase E.
    assert eng._try_typed_decision_path(_state(), "decision_required") is True
    assert bridge.submitted == [("pass",)]


def test_typed_path_handles_mulligan(monkeypatch):
    bridge = _TypedBridge({"has_pending": True, "request_type": "Mulligan"})
    planner = _planner_with('{"option_ids": ["mull:keep"], "reasoning": "fine hand"}')
    eng = _engine(monkeypatch, bridge, planner)
    handled = eng._try_typed_decision_path(_state(), "decision_required")
    assert handled is True
    assert bridge.submitted == [("mulligan", True)]


def test_typed_path_fsm_blocks_double_submit_and_exhausts(monkeypatch):
    """One in-flight submission per request; after the cap the path
    declares MANUAL REQUIRED once and owns the trigger (no legacy replan,
    no coaching fall-through)."""
    bridge = _TypedBridge(_TARGET_POLL)
    planner = _planner_with('{"option_ids": ["tgt:2"], "reasoning": "x"}')
    eng = _engine(monkeypatch, bridge, planner)
    monkeypatch.setattr(eng._request_tracker, "REJECT_GRACE_S", 0.0)

    # 1st call: submits.
    assert eng._try_typed_decision_path(_state(), "decision_required") is True
    assert bridge.submitted == [("targets", [2])]

    # 2nd/3rd calls: window re-presented (same poll) → rejection counted,
    # resubmit allowed up to the cap.
    assert eng._try_typed_decision_path(_state(), "decision_required") is True
    assert eng._try_typed_decision_path(_state(), "decision_required") is True
    assert len(bridge.submitted) == 3

    # 4th call: cap reached → owns the trigger, no 4th submission.
    pauses = []
    monkeypatch.setattr(
        eng, "_pause_for_manual", lambda reason, gs=None: pauses.append(reason)
    )
    assert eng._try_typed_decision_path(_state(), "decision_required") is True
    assert len(bridge.submitted) == 3
    assert pauses and "not accepted after" in pauses[0]


def test_planner_respects_max_select():
    planner = _planner_with(
        '{"option_ids": ["sel:10", "sel:11", "sel:12"], "reasoning": "all"}'
    )
    d = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "SelectN",
            "select_n_ids": [10, 11, 12],
            "select_n_min": 1,
            "select_n_max": 2,
        }
    )
    chosen = planner.plan_decision_options(d, {"turn": {}})
    assert chosen == ["sel:10", "sel:11"]


_GROUP_POLL = {
    "has_pending": True,
    "request_type": "Group",
    "group_instance_ids": [101, 102, 103, 104, 105, 106, 107, 108],
    "group_specs": [
        {"zoneType": "Hand", "subZoneType": "Top"},
        {"zoneType": "Library", "subZoneType": "Bottom", "lowerBound": 1},
    ],
    "group_context": "LondonMulligan",
}


def test_group_family_builds_bottoming_decision():
    d = build_pending_decision(
        _GROUP_POLL, resolve_instance=lambda iid: f"Card{iid}"
    )
    assert d is not None and d.request_type == "Group"
    assert d.min_select == 1 and d.max_select == 1
    assert d.find("grp:101").label == "Bottom Card101"
    assert len(d.options) == 8


def test_group_ordering_window_returns_none_for_legacy():
    poll = dict(_GROUP_POLL)
    poll["group_specs"] = [{"zoneType": "Hand", "subZoneType": "Top"}]
    poll["group_context"] = "OrderTriggers"
    assert build_pending_decision(poll) is None


def test_group_submit_builds_keep_and_bottom_groups():
    from arenamcp.decisions import submit_option

    class _GroupBridge:
        def __init__(self):
            self.groups = None

        def submit_group(self, groups):
            self.groups = groups
            return True

    d = build_pending_decision(_GROUP_POLL)
    bridge = _GroupBridge()
    assert submit_option(bridge, d, ["grp:103"]) is True
    keep, bottom = bridge.groups
    assert bottom == {"ids": [103], "zone": "Library", "sub_zone": "Bottom"}
    assert 103 not in keep["ids"] and len(keep["ids"]) == 7


def test_typed_path_group_uses_llm_or_defers_to_legacy(monkeypatch):
    bridge = _TypedBridge(_GROUP_POLL)
    bridge.groups = None
    bridge.submit_group = lambda groups: (setattr(bridge, "groups", groups), True)[1]

    # Valid LLM pick → typed path owns it.
    planner = _planner_with('{"option_ids": ["grp:105"], "reasoning": "worst card"}')
    eng = _engine(monkeypatch, bridge, planner)
    assert eng._try_typed_decision_path(_state(), "decision_required") is True
    assert bridge.groups[1]["ids"] == [105]

    # Garbage LLM → defer to legacy smart default (None, no submission).
    bridge2 = _TypedBridge(_GROUP_POLL)
    planner2 = _planner_with("nonsense")
    eng2 = _engine(monkeypatch, bridge2, planner2)
    assert eng2._try_typed_decision_path(_state(), "decision_required") is None
