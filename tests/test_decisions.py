"""Phase B foundation: typed PendingDecision builder + submit-by-id.

The planner picks among option ids; submission is mechanical. No display
string is ever parsed for semantics (fable-improvements.md item 1).
"""

from arenamcp.decisions import (
    PendingDecision,
    build_pending_decision,
    submit_option,
)

_NAMES = {91644: "Ruthless Negotiation", 94899: "Momentum Breaker", 75557: "Swamp"}


def _resolver(grp_id):
    return _NAMES.get(grp_id, "")


class _FakeBridge:
    def __init__(self):
        self.calls = []

    def submit_pass(self):
        self.calls.append(("pass",))
        return True

    def submit_mulligan(self, keep):
        self.calls.append(("mulligan", keep))
        return True

    def submit_action_by_index(self, idx):
        self.calls.append(("action", idx))
        return True

    def submit_targets(self, iid):
        self.calls.append(("targets", iid))
        return True

    def submit_selection(self, ids):
        self.calls.append(("selection", ids))
        return True


def test_actions_available_builds_options_with_payability():
    poll = {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "can_pass": True,
        "actions": [
            {"actionType": "ActionType_Cast", "grpId": 91644, "instanceId": 320,
             "autoTapSolution": {"mana": []}},
            {"actionType": "ActionType_Cast", "grpId": 94899, "instanceId": 164,
             "autoTapSolution": None},
            {"actionType": "ActionType_Play", "grpId": 75557, "instanceId": 167},
            {"actionType": "ActionType_Pass"},
        ],
    }
    d = build_pending_decision(poll, resolve_name=_resolver)
    assert d is not None and d.request_type == "ActionsAvailable"
    by_id = {o.option_id: o for o in d.options}
    assert by_id["idx:0"].payable is True
    assert by_id["idx:0"].label == "Cast Ruthless Negotiation"
    assert by_id["idx:1"].payable is False
    assert "cannot auto-pay" in by_id["idx:1"].label
    assert by_id["idx:2"].label == "Play land: Swamp"
    assert "pass" in by_id
    assert d.can_pass


def test_select_targets_builds_target_options():
    poll = {
        "has_pending": True,
        "request_type": "SelectTargets",
        "target_candidates": [
            {"targetInstanceId": 2, "targetIdx": 1, "grpId": 0},
            {"targetInstanceId": 161, "targetIdx": 1, "grpId": 91644},
        ],
        "target_selections": [{"targetIdx": 1, "minTargets": 1, "maxTargets": 1}],
    }
    d = build_pending_decision(poll, resolve_name=_resolver)
    assert d is not None and d.request_type == "SelectTargets"
    assert d.option_ids() == {"tgt:2", "tgt:161"}
    assert d.min_select == 1 and d.max_select == 1
    assert d.find("tgt:161").label == "Ruthless Negotiation"
    assert d.find("tgt:2").label == "Target #2"  # player target: no grpId


def test_select_n_and_mulligan():
    d = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "SelectN",
            "select_n_ids": [10, 11, 12],
            "select_n_min": 1,
            "select_n_max": 2,
        },
        resolve_name=_resolver,
    )
    assert d is not None
    assert d.option_ids() == {"sel:10", "sel:11", "sel:12"}
    assert d.max_select == 2

    m = build_pending_decision({"has_pending": True, "request_type": "Mulligan"})
    assert m is not None
    assert m.option_ids() == {"mull:keep", "mull:mull"}


def test_unknown_family_returns_none_for_legacy_fallback():
    assert build_pending_decision(
        {"has_pending": True, "request_type": "GroupRequest"}
    ) is None
    assert build_pending_decision({"has_pending": False}) is None
    assert build_pending_decision(None) is None


def test_submit_option_dispatches_by_id_scheme():
    bridge = _FakeBridge()
    poll = {
        "has_pending": True,
        "request_type": "SelectTargets",
        "target_candidates": [{"targetInstanceId": 2, "grpId": 0}],
        "target_selections": [{"minTargets": 1, "maxTargets": 1}],
    }
    d = build_pending_decision(poll, resolve_name=_resolver)
    assert submit_option(bridge, d, ["tgt:2"]) is True
    # submit_targets now receives a per-slot list (one id per TargetSelection).
    assert bridge.calls == [("targets", [2])]


def test_submit_option_rejects_ids_outside_decision():
    bridge = _FakeBridge()
    d = PendingDecision(
        request_id=(1, 2),
        request_type="ActionsAvailable",
        options=(),
    )
    assert submit_option(bridge, d, ["idx:0"]) is False
    assert bridge.calls == []


def test_submit_option_multi_select():
    bridge = _FakeBridge()
    d = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "SelectN",
            "select_n_ids": [10, 11, 12],
            "select_n_max": 2,
        },
        resolve_name=_resolver,
    )
    assert submit_option(bridge, d, ["sel:10", "sel:12"]) is True
    assert bridge.calls == [("selection", [10, 12])]
