"""K3: the 85-option selection Arena offered right after Etali's Favor resolved.

Captured in match_packets/packet_20261109_232745_eedf1ae3-711d-4a8c-847a-
6c6e3b934d9b.json (decision index 14, 2026-11-09 23:26:45; the decision's
trace records local seat 2): a SelectN over 85 hidden objects — "Option 927" …
"Option 1179", grpId 0, pick at least 1 of 85, no cancel, no pass. The model
read it as "pick one offered card … continue" and answered blind
(bug_20261110_070459); the same kind of request recurred 2026-11-10 07:03/07:04
(match 31618195, where the chosen sel:970 was never submitted and the window
cleared itself).

What the evidence holds: the packet's pending_decision (option ids, min/max,
can_cancel), the coach log around it, and the bug report. The bridge payload
itself (the GreToClientEvent SelectNreq) was summarized away by Player.log
(standalone.log 2026-11-09 23:26:45.235 and 2026-11-10 07:03:56.311: "its
details are missing from the log"), so what Arena was asking and which zone
the options live in are not recorded anywhere. The zone label asserted below
is therefore the derivation from the live game state at decision time, with
that state reconstructed for the test; only the ids, bounds and seat come from
the capture.

Pinned behaviour: an unidentified option never reaches the model; a menu that
cannot be answered from identified options alone is declined without a model
call and completed with Arena's own default (never a manual stall); a partly
identified menu is decided among the identified cards only.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import DECLINE_DECISION, ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision, build_pending_decision

# Instance IDs exactly as captured in packet decision index 14: 85 objects,
# three apart.
ETALI_FAVOR_OPTION_IDS = [927 + 3 * index for index in range(85)]
# packet decisions[14].trace.local_seat_id
CAPTURED_LOCAL_SEAT = 2


def _captured_poll(**overrides):
    poll = {
        "has_pending": True,
        "request_type": "SelectN",
        "game_state_id": 336,
        "msg_id": 402,
        "select_n_ids": list(ETALI_FAVOR_OPTION_IDS),
        "select_n_min": 1,
        "select_n_max": 85,
        "can_cancel": False,
    }
    poll.update(overrides)
    return poll


def _hidden_library_objects(seat=CAPTURED_LOCAL_SEAT):
    """A reconstructed live state for the captured ids.

    The capture does not say where these objects live (see module docstring);
    this reconstruction places them in a private zone owned by the local seat
    so the zone labelling and identity resolution have something to read.
    """
    objects = {
        iid: SimpleNamespace(
            grp_id=0,
            visibility="Visibility_Private",
            owner_seat_id=seat,
            zone_id=7,
        )
        for iid in ETALI_FAVOR_OPTION_IDS
    }
    state = SimpleNamespace(
        local_seat_id=seat,
        zones={7: SimpleNamespace(zone_type=SimpleNamespace(value="ZoneType_Library"))},
    )
    return objects, state


def test_captured_selection_builds_with_the_captured_shape():
    decision = build_pending_decision(_captured_poll(), resolve_instance=lambda _: "")

    assert decision is not None
    assert decision.request_type == "SelectN"
    assert [option.option_id for option in decision.options] == [
        f"sel:{iid}" for iid in ETALI_FAVOR_OPTION_IDS
    ]
    assert (decision.min_select, decision.max_select) == (1, 85)
    assert decision.can_cancel is False
    assert all(option.meta["identity_known"] is False for option in decision.options)


def test_unnamed_options_record_the_zone_the_live_state_gives_them(monkeypatch):
    from arenamcp import decisions

    objects, state = _hidden_library_objects()
    monkeypatch.setattr(decisions, "_live_object", objects.get)
    monkeypatch.setattr(decisions, "_live_game_state", lambda: state)

    decision = build_pending_decision(_captured_poll(), resolve_instance=lambda _: "")

    assert all(option.meta.get("zone") == "Library" for option in decision.options)


@pytest.mark.parametrize("count,min_select", [(3, 1), (9, 1), (85, 0), (85, 1)])
def test_fully_unnamed_hidden_library_selections_never_reach_the_model(count, min_select):
    # Same shape as the capture (ids three apart, "Option NNN" labels), scaled
    # down to 3 and 9 to pin that no size threshold gates the decline.
    decision = PendingDecision(
        (336, 402),
        "SelectN",
        tuple(
            DecisionOption(
                f"sel:{927 + 3 * index}",
                f"Option {927 + 3 * index}",
                meta={"grpId": 0, "identity_known": False, "zone": "Library"},
            )
            for index in range(count)
        ),
        min_select=min_select,
        max_select=count,
    )

    planner = ActionPlanner(Mock())
    llm = Mock(side_effect=AssertionError("unnamed options must not reach the model"))
    planner._llm_decision_options = llm

    assert planner._plan_decision_options(decision, {"players": [], "battlefield": []}) == [DECLINE_DECISION]
    llm.assert_not_called()


def test_unnamed_options_outside_a_hidden_zone_still_reach_the_model():
    # Crew-style payment: the log may not carry the creatures' names, but they
    # stand on a public battlefield — the model decides by weight, exactly as
    # before (tests/test_weighted_decisions.py pins this end to end).
    decision = PendingDecision(
        (336, 402),
        "SelectN",
        (
            DecisionOption("sel:763", "Option 763", meta={"grpId": 0, "identity_known": False, "weight": 3}),
            DecisionOption("sel:771", "Option 771", meta={"grpId": 0, "identity_known": False, "weight": 1}),
            DecisionOption("sel:875", "Option 875", meta={"grpId": 0, "identity_known": False, "weight": 4}),
        ),
        min_select=0,
        max_select=3,
    )
    planner = ActionPlanner(Mock())
    llm = Mock(return_value=["sel:875"])
    planner._llm_decision_options = llm

    assert planner._plan_decision_options(decision, {"players": [], "battlefield": []}) == ["sel:875"]
    llm.assert_called_once()


def test_captured_selection_is_completed_by_arena_default_end_to_end(monkeypatch):
    """The full typed path: no model call, no manual stall — Arena's default."""
    from tests.fakes import TypedBridge, make_engine, target_state

    from arenamcp import decisions

    objects, state = _hidden_library_objects()
    monkeypatch.setattr(decisions, "_live_object", objects.get)
    monkeypatch.setattr(decisions, "_live_game_state", lambda: state)
    planner = ActionPlanner(Mock())
    llm = Mock(side_effect=AssertionError("no model call for unnamed options"))
    planner._llm_decision_options = llm
    bridge = TypedBridge(_captured_poll())
    engine = make_engine(bridge, planner, game_state=target_state())
    pauses = []
    engine._pause_for_manual = lambda reason, game_state=None: pauses.append(reason)

    handled = engine._try_typed_decision_path(target_state(), "decision_required")

    assert handled is True
    assert ("auto_respond",) in bridge.submitted
    assert pauses == []
    llm.assert_not_called()


def test_cancellable_unnamed_selection_is_cancelled_rather_than_defaulted(monkeypatch):
    from tests.fakes import TypedBridge, make_engine, target_state

    from arenamcp import decisions

    objects, state = _hidden_library_objects()
    monkeypatch.setattr(decisions, "_live_object", objects.get)
    monkeypatch.setattr(decisions, "_live_game_state", lambda: state)
    planner = ActionPlanner(Mock())
    planner._llm_decision_options = Mock(side_effect=AssertionError("no model call for unnamed options"))
    bridge = TypedBridge(_captured_poll(can_cancel=True))
    engine = make_engine(bridge, planner, game_state=target_state())
    pauses = []
    engine._pause_for_manual = lambda reason, game_state=None: pauses.append(reason)

    handled = engine._try_typed_decision_path(target_state(), "decision_required")

    assert handled is True
    assert ("cancel",) in bridge.submitted
    assert ("auto_respond",) not in bridge.submitted
    assert pauses == []


def test_partly_identified_menu_passes_through_intact_to_the_model():
    # A partly identified menu (a loot's discard where one name trailed) is
    # intentionally passed through whole: the model sees every option, named
    # or not, exactly as before this change.
    decision = PendingDecision(
        (336, 402),
        "SelectN",
        (
            DecisionOption("sel:5", "Bear Cub", meta={"identity_known": True}),
            DecisionOption("sel:8", "Option 8", meta={"identity_known": False}),
            DecisionOption("sel:11", "Option 11", meta={"identity_known": False}),
        ),
        min_select=1,
        max_select=1,
    )
    planner = ActionPlanner(Mock())
    llm = Mock(return_value=["sel:5"])
    planner._llm_decision_options = llm

    assert planner._plan_decision_options(decision, {"players": [], "battlefield": []}) == ["sel:5"]
    llm.assert_called_once()
    offered = llm.call_args[0][0]
    assert [option.option_id for option in offered.options] == ["sel:5", "sel:8", "sel:11"]


def test_mixed_menu_that_still_requires_a_hidden_library_card_declines():
    # Two cards must be picked but only one is identified and the rest are
    # hidden library objects: every legal answer includes a card nobody can
    # see, so the model is not consulted.
    decision = PendingDecision(
        (336, 402),
        "SelectN",
        (
            DecisionOption("sel:5", "Bear Cub", meta={"identity_known": True}),
            DecisionOption("sel:8", "Option 8", meta={"identity_known": False, "zone": "Library"}),
            DecisionOption("sel:11", "Option 11", meta={"identity_known": False, "zone": "Library"}),
        ),
        min_select=2,
        max_select=2,
    )
    planner = ActionPlanner(Mock())
    llm = Mock(side_effect=AssertionError("a blind minimum must not reach the model"))
    planner._llm_decision_options = llm

    assert planner._plan_decision_options(decision, {"players": [], "battlefield": []}) == [DECLINE_DECISION]
    llm.assert_not_called()


def test_loot_discard_still_waits_for_the_name_then_decides_from_the_full_menu(monkeypatch):
    """The hand-card identity wait (match 07c043d8's loot) is untouched: wait
    for the log to name the card just drawn, then decide — the menu passes
    through to the model intact, exactly as before this change."""

    class _DisconnectedBridge:
        connected = False

        def connect(self):
            return False

    import time

    import arenamcp.autopilot as autopilot_module
    from arenamcp.autopilot import AutopilotConfig, AutopilotEngine

    hand = {119: "Garruk, Veiled Butcher", 232: "Swamp"}
    state = {
        "match_id": "07c043d8-e07b-4c15-9f05-173d2fe178ee",
        "turn": {"turn_number": 7},
        "players": [{"seat_id": 1, "is_local": True}],
        "hand": [{"instance_id": iid, "name": name} for iid, name in hand.items()],
        "battlefield": [],
        "stack": [],
    }
    decision = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "SelectN",
            "select_n_ids": [119, 232, 256],
            "select_n_min": 1,
            "select_n_max": 1,
        },
        resolve_instance=lambda iid: hand.get(iid, ""),
    )
    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: _DisconnectedBridge())
    engine = AutopilotEngine(planner=None, get_game_state=lambda: {}, config=AutopilotConfig(dry_run=True))
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    assert engine._wait_for_search_identities(decision, state) is True  # waiting for the name
    clock[0] += engine._SELECT_N_IDENTITY_WAIT_S
    assert engine._wait_for_search_identities(decision, state) is False  # decide anyway

    planner = ActionPlanner(Mock())
    llm = Mock(return_value=["sel:119"])
    planner._llm_decision_options = llm

    assert planner._plan_decision_options(decision, state) == ["sel:119"]
    llm.assert_called_once()
    offered = llm.call_args[0][0]
    assert [option.option_id for option in offered.options] == ["sel:119", "sel:232", "sel:256"]
