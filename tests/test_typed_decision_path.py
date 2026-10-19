"""Phase B wiring: autopilot's typed-decision path + planner option picking.

SelectTargets/SelectN/Mulligan flow as structured options end-to-end; the
LLM answers with option ids; submission is by id; legacy string planning
never runs for these families when the bridge serves them.
"""

import pytest
from tests.fakes import TARGET_POLL, TypedBridge, make_engine, planner_with, target_state

from arenamcp.decisions import build_pending_decision


def test_typed_path_submits_llm_choice_by_id():
    bridge = TypedBridge(TARGET_POLL)
    planner = planner_with('{"option_ids": ["tgt:2"], "reasoning": "opponent"}')
    eng = make_engine(bridge, planner)
    handled = eng._try_typed_decision_path(target_state(known_source=True), "decision_required")
    assert handled is True

    assert bridge.submitted == [("targets", [2])]


def test_typed_path_surfaces_reason_for_the_submitted_choice():
    bridge = TypedBridge(TARGET_POLL)
    planner = planner_with('{"option_ids": ["tgt:2"], "reasoning": "Remove the opposing threat."}')
    engine = make_engine(bridge, planner)
    notifications = []
    engine._ui_advice_fn = lambda text, label: notifications.append(text)
    assert engine._try_typed_decision_path(target_state(known_source=True), "decision_required") is True
    assert "Remove the opposing threat." in notifications[-1]
    assert planner.get_decision_reasoning(["tgt:2"]) == "Remove the opposing threat."
    assert planner.get_decision_reasoning(["tgt:161"]) == ""
    assert bridge.submitted == [("targets", [2])]


def test_typed_path_pauses_for_unknown_effect_on_garbage_llm(monkeypatch):
    bridge = TypedBridge(TARGET_POLL)
    planner = planner_with("completely invalid")
    eng = make_engine(bridge, planner)
    pauses = []
    monkeypatch.setattr(eng, "_pause_for_manual", lambda reason, state: pauses.append(reason))
    handled = eng._try_typed_decision_path(target_state(), "decision_required")
    assert handled is True
    assert bridge.submitted == []
    assert pauses and "no safe automatic choice" in pauses[0]


def test_typed_path_rejects_hallucinated_ids_without_blind_target_fallback():
    bridge = TypedBridge(TARGET_POLL)
    planner = planner_with('{"option_ids": ["tgt:9999"], "reasoning": "x"}')
    eng = make_engine(bridge, planner)
    handled = eng._try_typed_decision_path(target_state(), "decision_required")
    assert handled is True
    assert bridge.submitted == []


def test_failed_target_submission_pauses_instead_of_retrying_legacy(monkeypatch):
    bridge = TypedBridge(TARGET_POLL)
    bridge.submit_targets = lambda ids: False
    engine = make_engine(bridge, planner_with('{"option_ids": ["tgt:2"]}'))
    pauses = []
    monkeypatch.setattr(engine, "_pause_for_manual", lambda reason, state: pauses.append(reason))
    assert engine._try_typed_decision_path(target_state(known_source=True), "decision_required") is True
    assert pauses and "not verified" in pauses[0]


@pytest.mark.parametrize(
    "after",
    [
        {"ok": True, "has_pending": False},  # the player clicked the target first
        {**TARGET_POLL, "game_state_id": 241, "msg_id": 314},  # the GRE moved to the next slot
    ],
    ids=["answered-by-the-player", "advanced"],
)
def test_failed_target_submission_on_a_moved_on_request_is_not_manual(monkeypatch, after):
    # Seam Rip, 2026-10-18 18:26:12: Player.log shows the player's own
    # SelectTargetsResp for Llanowar Elves at 18:26:12.136; our submit 40 ms
    # later failed ("ref does not name an object result") and MANUAL REQUIRED
    # was announced for a spell that had already resolved.
    bridge = TypedBridge({**TARGET_POLL, "game_state_id": 240, "msg_id": 312})

    def player_answers_first(ids):
        bridge.poll_resp = after
        return False

    bridge.submit_targets = player_answers_first
    engine = make_engine(bridge, planner_with('{"option_ids": ["tgt:2"]}'))
    pauses = []
    monkeypatch.setattr(engine, "_pause_for_manual", lambda reason, state: pauses.append(reason))
    assert engine._try_typed_decision_path(target_state(known_source=True), "decision_required") is True
    assert pauses == []


@pytest.mark.parametrize(
    ("answer", "submitted"),
    [
        ("pass", ("pass",)),
        # The chosen action's identity travels with its index, for bridges that check it.
        ("idx:0", ("action", 0, {"grpId": 123})),
    ],
)
def test_typed_path_handles_actions_available(answer, submitted):
    poll = {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "actions": [
            {"actionType": "ActionType_Cast", "grpId": 123, "autoTapSolution": {}},
            {"actionType": "ActionType_Pass"},
        ],
        "can_pass": True,
    }
    bridge = TypedBridge(poll)
    bridge.submit_action_by_index = lambda index, expected=None, **_: (
        bridge.submitted.append(("action", index, expected)) or True
    )
    planner = planner_with(f'{{"option_ids": ["{answer}"]}}')
    eng = make_engine(bridge, planner)
    # ActionsAvailable migrated to typed decision path in Phase E.
    assert eng._try_typed_decision_path(target_state(), "decision_required") is True
    assert bridge.submitted == [submitted]


def test_typed_path_handles_mulligan():
    bridge = TypedBridge({"has_pending": True, "request_type": "Mulligan"})
    planner = planner_with('{"option_ids": ["mull:keep"], "reasoning": "fine hand"}')
    eng = make_engine(bridge, planner)
    handled = eng._try_typed_decision_path(target_state(), "decision_required")
    assert handled is True
    assert bridge.submitted == [("mulligan", True)]


def test_optional_accept_keeps_request_identity_and_reasoning():
    poll = {"has_pending": True, "request_type": "OptionalAction", "game_state_id": 42, "msg_id": 5}
    bridge = TypedBridge(poll)
    bridge.submit_optional = lambda accept, **kwargs: bridge.submitted.append((accept, kwargs)) or True
    engine = make_engine(
        bridge, planner_with('{"option_ids":["optional:accept"],"reasoning":"Take the free card."}')
    )
    assert engine._try_typed_decision_path(target_state(), "decision_required")
    assert bridge.submitted == [(True, {"expected_request_id": (42, 5)})]


def test_changed_optional_window_does_not_accept_the_next_prompt():
    poll = {"has_pending": True, "request_type": "OptionalAction", "game_state_id": 42, "msg_id": 5}
    bridge = TypedBridge(poll)
    bridge.submit_optional = lambda accept, **kwargs: bridge.submitted.append((accept, kwargs)) or True
    planner = planner_with('{"option_ids":["optional:accept"]}')

    def changed_during_reasoning(*args, **kwargs):
        bridge.poll_resp = {**poll, "msg_id": 6}
        return '{"option_ids":["optional:accept"]}'

    planner._backend.complete = changed_during_reasoning
    engine = make_engine(bridge, planner)
    assert engine._try_typed_decision_path(target_state(), "decision_required")
    assert bridge.submitted == []


def test_typed_path_fsm_blocks_double_submit_and_exhausts(monkeypatch):
    """One in-flight submission per request; after the cap the path
    declares MANUAL REQUIRED once and owns the trigger (no legacy replan,
    no coaching fall-through)."""
    bridge = TypedBridge(TARGET_POLL)
    planner = planner_with('{"option_ids": ["tgt:2"], "reasoning": "x"}')
    eng = make_engine(bridge, planner)
    monkeypatch.setattr(eng._request_tracker, "REJECT_GRACE_S", 0.0)

    # 1st call: submits.
    assert eng._try_typed_decision_path(target_state(known_source=True), "decision_required") is True
    assert bridge.submitted == [("targets", [2])]

    # 2nd/3rd calls: window re-presented (same poll) → rejection counted,
    # resubmit allowed up to the cap.
    assert eng._try_typed_decision_path(target_state(known_source=True), "decision_required") is True
    assert eng._try_typed_decision_path(target_state(known_source=True), "decision_required") is True
    assert len(bridge.submitted) == 3

    # 4th call: cap reached → owns the trigger, no 4th submission.
    pauses = []
    monkeypatch.setattr(eng, "_pause_for_manual", lambda reason, gs=None: pauses.append(reason))
    assert eng._try_typed_decision_path(target_state(known_source=True), "decision_required") is True
    assert len(bridge.submitted) == 3
    # Submissions remain capped while the semantic 8s settlement gate runs.
    assert not pauses
    first = eng._progress_guard._first_attempt
    monkeypatch.setattr("arenamcp.autopilot.time.monotonic", lambda: first + 9)
    captures = []
    eng._stuck_report_fn = lambda reason, context: captures.append(context)
    assert eng._try_typed_decision_path(target_state(known_source=True), "decision_required") is True
    assert len(bridge.submitted) == 3
    assert captures[0]["semantic_progress"]["attempts"] == 3


def test_planner_respects_max_select():
    planner = planner_with('{"option_ids": ["sel:10", "sel:11", "sel:12"], "reasoning": "all"}')
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
    d = build_pending_decision(_GROUP_POLL, resolve_instance=lambda iid: f"Card{iid}")
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


def test_typed_path_group_uses_llm_or_defers_to_legacy():
    bridge = TypedBridge(_GROUP_POLL)
    bridge.groups = None
    bridge.submit_group = lambda groups: (setattr(bridge, "groups", groups), True)[1]

    # Valid LLM pick → typed path owns it.
    planner = planner_with('{"option_ids": ["grp:105"], "reasoning": "worst card"}')
    eng = make_engine(bridge, planner)
    assert eng._try_typed_decision_path(target_state(), "decision_required") is True
    assert bridge.groups[1]["ids"] == [105]

    # Garbage LLM → defer to legacy smart default (None, no submission).
    bridge2 = TypedBridge(_GROUP_POLL)
    planner2 = planner_with("nonsense")
    eng2 = make_engine(bridge2, planner2)
    assert eng2._try_typed_decision_path(target_state(), "decision_required") is None
