"""K3: the 85-option selection Arena offered right after Etali's Favor resolved.

Captured in match_packets/packet_20261109_232745_eedf1ae3-711d-4a8c-847a-
6c6e3b934d9b.json (decision index 14, 2026-11-09 23:26:45): a SelectN over 85
hidden library objects — "Option 927" … "Option 1179", grpId 0, pick at least
1 of 85, no cancel, no pass. The model read it as "pick one offered card …
continue" and answered blind (bug_20261110_070459). These tests pin the two
behaviours that replace that guesswork: every unnamed option records the zone
it lives in, and a selection where nothing is identified is declined without a
model call.
"""

from types import SimpleNamespace
from unittest.mock import Mock

from arenamcp.action_planner import DECLINE_DECISION, ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision, build_pending_decision

# Instance IDs exactly as captured: 85 hidden objects, three apart.
ETALI_FAVOR_OPTION_IDS = [927 + 3 * index for index in range(85)]


def _captured_poll():
    return {
        "has_pending": True,
        "request_type": "SelectN",
        "game_state_id": 336,
        "msg_id": 402,
        "select_n_ids": list(ETALI_FAVOR_OPTION_IDS),
        "select_n_min": 1,
        "select_n_max": 85,
        "can_cancel": False,
    }


def _hidden_library_objects():
    objects = {
        iid: SimpleNamespace(
            grp_id=0,
            visibility="Visibility_Private",
            owner_seat_id=1,
            zone_id=7,
        )
        for iid in ETALI_FAVOR_OPTION_IDS
    }
    state = SimpleNamespace(
        local_seat_id=1,
        zones={7: SimpleNamespace(zone_type=SimpleNamespace(value="ZoneType_Library"))},
    )
    return objects, state


def test_captured_etali_favor_selection_labels_every_option_with_its_zone(monkeypatch):
    from arenamcp import decisions

    objects, state = _hidden_library_objects()
    monkeypatch.setattr(decisions, "_live_object", objects.get)
    monkeypatch.setattr(decisions, "_live_game_state", lambda: state)

    decision = build_pending_decision(_captured_poll(), resolve_instance=lambda _: "")

    assert decision is not None
    assert decision.request_type == "SelectN"
    assert len(decision.options) == 85
    assert (decision.min_select, decision.max_select) == (1, 85)
    assert [option.option_id for option in decision.options[:3]] == ["sel:927", "sel:930", "sel:933"]
    for option in decision.options:
        assert option.meta["identity_known"] is False
        assert option.meta["zone"] == "Library"


def test_fully_unnamed_selection_is_declined_without_a_model_call(monkeypatch):
    from arenamcp import decisions

    objects, state = _hidden_library_objects()
    monkeypatch.setattr(decisions, "_live_object", objects.get)
    monkeypatch.setattr(decisions, "_live_game_state", lambda: state)
    decision = build_pending_decision(_captured_poll(), resolve_instance=lambda _: "")

    planner = ActionPlanner(Mock())
    llm = Mock(side_effect=AssertionError("model must not be consulted for unnamed options"))
    planner._llm_decision_options = llm

    assert planner._plan_decision_options(decision, {"players": [], "battlefield": []}) == [
        DECLINE_DECISION
    ]
    llm.assert_not_called()


def test_small_unnamed_selection_keeps_the_existing_wait_out_behaviour():
    """A scry/loot-sized unnamed selection is still answered (scry_transition_341)."""
    decision = PendingDecision(
        (336, 402),
        "SelectN",
        tuple(DecisionOption(f"sel:{iid}", f"Option {iid}") for iid in (10, 11, 12)),
        min_select=1,
        max_select=2,
    )
    planner = ActionPlanner(Mock())
    llm = Mock(return_value=["sel:10", "sel:11"])
    planner._llm_decision_options = llm

    assert planner._plan_decision_options(decision, {"turn": {}}) == ["sel:10", "sel:11"]
    llm.assert_called_once()


def test_partially_named_selection_still_reaches_the_model():
    decision = PendingDecision(
        (336, 402),
        "SelectN",
        (
            DecisionOption("sel:5", "Bear Cub", meta={"identity_known": True}),
            DecisionOption("sel:8", "Option 8", meta={"identity_known": False}),
        ),
        min_select=1,
        max_select=1,
    )
    planner = ActionPlanner(Mock())
    llm = Mock(return_value=["sel:5"])
    planner._llm_decision_options = llm

    assert planner._plan_decision_options(decision, {"players": [], "battlefield": []}) == ["sel:5"]
    llm.assert_called_once()
