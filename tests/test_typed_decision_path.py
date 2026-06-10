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
    assert bridge.submitted == [("targets", 2)]


def test_typed_path_falls_back_deterministically_on_garbage_llm(monkeypatch):
    bridge = _TypedBridge(_TARGET_POLL)
    planner = _planner_with("completely invalid")
    eng = _engine(monkeypatch, bridge, planner)
    handled = eng._try_typed_decision_path(_state(), "decision_required")
    assert handled is True
    # Deterministic pick = first option, still submitted BY ID.
    assert bridge.submitted == [("targets", 2)]


def test_typed_path_rejects_hallucinated_ids_then_falls_back(monkeypatch):
    bridge = _TypedBridge(_TARGET_POLL)
    planner = _planner_with('{"option_ids": ["tgt:9999"], "reasoning": "x"}')
    eng = _engine(monkeypatch, bridge, planner)
    handled = eng._try_typed_decision_path(_state(), "decision_required")
    assert handled is True
    assert bridge.submitted == [("targets", 2)]  # mechanical fallback


def test_typed_path_declines_actions_available(monkeypatch):
    poll = {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "actions": [{"actionType": "ActionType_Pass"}],
        "can_pass": True,
    }
    bridge = _TypedBridge(poll)
    planner = _planner_with('{"option_ids": ["pass"]}')
    eng = _engine(monkeypatch, bridge, planner)
    # Legacy strategic path keeps ActionsAvailable until Phase C.
    assert eng._try_typed_decision_path(_state(), "decision_required") is None
    assert bridge.submitted == []


def test_typed_path_handles_mulligan(monkeypatch):
    bridge = _TypedBridge({"has_pending": True, "request_type": "Mulligan"})
    planner = _planner_with('{"option_ids": ["mull:keep"], "reasoning": "fine hand"}')
    eng = _engine(monkeypatch, bridge, planner)
    handled = eng._try_typed_decision_path(_state(), "decision_required")
    assert handled is True
    assert bridge.submitted == [("mulligan", True)]


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
