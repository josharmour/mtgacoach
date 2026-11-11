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
details are missing from the log"), so what Arena was asking — and which zone
the options live in — is not recorded anywhere. The tests below therefore run
against the capture exactly as it is: ids, bounds and seat from the packet,
and NO live game state, so no option carries a zone label. The policy under
test keys on identifiability, never on a zone.

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


@pytest.fixture()
def no_live_state(monkeypatch):
    """The capture as it stands: no live game state to name or place anything.

    Mirrors a replay of the packet — every option stays unidentified and
    carries no zone label, exactly as the round-2 review reproduced it.
    """
    from arenamcp import decisions

    monkeypatch.setattr(decisions, "_live_object", lambda instance_id: None)
    monkeypatch.setattr(
        decisions,
        "_live_game_state",
        lambda: SimpleNamespace(zones={}, local_seat_id=CAPTURED_LOCAL_SEAT),
    )


def _captured_decision(**poll_overrides):
    return build_pending_decision(_captured_poll(**poll_overrides), resolve_instance=lambda _: "")


def test_captured_selection_builds_with_the_captured_shape(no_live_state):
    decision = _captured_decision()

    assert decision is not None
    assert decision.request_type == "SelectN"
    assert [option.option_id for option in decision.options] == [
        f"sel:{iid}" for iid in ETALI_FAVOR_OPTION_IDS
    ]
    assert (decision.min_select, decision.max_select) == (1, 85)
    assert decision.can_cancel is False
    assert all(option.meta["identity_known"] is False for option in decision.options)
    # The capture establishes no zone for these objects (see module docstring);
    # nothing here may invent one.
    assert all("zone" not in option.meta for option in decision.options)


def test_unnamed_options_record_the_zone_when_live_state_supplies_one(monkeypatch):
    # Observability only: when the live game state CAN place an object, its
    # zone is recorded for logs and manual hand-offs. The decline/default
    # policy below never requires this label to be present.
    from arenamcp import decisions

    objects = {
        iid: SimpleNamespace(
            grp_id=0,
            visibility="Visibility_Private",
            owner_seat_id=CAPTURED_LOCAL_SEAT,
            zone_id=7,
        )
        for iid in ETALI_FAVOR_OPTION_IDS
    }
    state = SimpleNamespace(
        local_seat_id=CAPTURED_LOCAL_SEAT,
        zones={7: SimpleNamespace(zone_type=SimpleNamespace(value="ZoneType_Library"))},
    )
    monkeypatch.setattr(decisions, "_live_object", objects.get)
    monkeypatch.setattr(decisions, "_live_game_state", lambda: state)

    decision = _captured_decision()

    assert all(option.meta.get("zone") == "Library" for option in decision.options)


@pytest.mark.parametrize("count,min_select", [(3, 1), (9, 1), (85, 0), (85, 1)])
def test_fully_unidentified_selections_of_any_size_never_reach_the_model(no_live_state, count, min_select):
    # Same shape as the capture (ids three apart, "Option NNN" labels), scaled
    # down to 3 and 9 to pin that no size threshold gates the decline — and
    # with no zone label anywhere, as in the actual packet.
    decision = PendingDecision(
        (336, 402),
        "SelectN",
        tuple(
            DecisionOption(
                f"sel:{927 + 3 * index}",
                f"Option {927 + 3 * index}",
                meta={"grpId": 0, "identity_known": False},
            )
            for index in range(count)
        ),
        min_select=min_select,
        max_select=count,
    )

    planner = ActionPlanner(Mock())
    llm = Mock(side_effect=AssertionError("unidentified options must not reach the model"))
    planner._llm_decision_options = llm

    assert planner._plan_decision_options(decision, {"players": [], "battlefield": []}) == [DECLINE_DECISION]
    llm.assert_not_called()


def test_captured_selection_is_completed_by_arena_default_end_to_end(no_live_state):
    """The full typed path on the captured poll: no model call, no manual
    stall — Arena's default completes the noncancellable request."""
    from tests.fakes import TypedBridge, make_engine, target_state

    planner = ActionPlanner(Mock())
    llm = Mock(side_effect=AssertionError("no model call for unidentified options"))
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


def test_cancellable_unidentified_selection_is_cancelled_rather_than_defaulted(
    no_live_state,
):
    from tests.fakes import TypedBridge, make_engine, target_state

    planner = ActionPlanner(Mock())
    planner._llm_decision_options = Mock(side_effect=AssertionError("no model call for unidentified options"))
    bridge = TypedBridge(_captured_poll(can_cancel=True))
    engine = make_engine(bridge, planner, game_state=target_state())
    pauses = []
    engine._pause_for_manual = lambda reason, game_state=None: pauses.append(reason)

    handled = engine._try_typed_decision_path(target_state(), "decision_required")

    assert handled is True
    assert ("cancel",) in bridge.submitted
    assert ("auto_respond",) not in bridge.submitted
    assert pauses == []


def test_mixed_menu_offers_the_model_only_the_identified_options():
    # A partly identified menu (a loot's discard where one name trailed): the
    # model chooses among the cards it can actually see. An unidentified
    # option never reaches it — previously the model picked "Option 8" over a
    # named alternative it could have judged.
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
    assert [option.option_id for option in offered.options] == ["sel:5"]
    assert all(option.meta.get("identity_known") is not False for option in offered.options)


def test_mixed_menu_that_still_requires_an_unidentified_card_declines():
    # Two cards must be picked but only one is identified: every legal answer
    # includes an object nobody can see, so the model is not consulted.
    decision = PendingDecision(
        (336, 402),
        "SelectN",
        (
            DecisionOption("sel:5", "Bear Cub", meta={"identity_known": True}),
            DecisionOption("sel:8", "Option 8", meta={"identity_known": False}),
            DecisionOption("sel:11", "Option 11", meta={"identity_known": False}),
        ),
        min_select=2,
        max_select=2,
    )
    planner = ActionPlanner(Mock())
    llm = Mock(side_effect=AssertionError("a blind minimum must not reach the model"))
    planner._llm_decision_options = llm

    assert planner._plan_decision_options(decision, {"players": [], "battlefield": []}) == [DECLINE_DECISION]
    llm.assert_not_called()


def test_weighted_unnamed_options_still_reach_the_model():
    # Crew-style payment: the log may not carry the creatures' names, but each
    # option carries a weight the planner ranks by — decidable without a name,
    # exactly as before (tests/test_weighted_decisions.py pins this end to end).
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
    offered = llm.call_args[0][0]
    assert [option.option_id for option in offered.options] == ["sel:763", "sel:771", "sel:875"]


def test_constrained_blind_menus_decline_like_the_captured_one(no_live_state):
    # Same captured menu (packet eedf1ae3 decision index 14), other shapes
    # Arena also offers: fewer choices than offered, or a take-most bound.
    # Menu size changes nothing — an option nobody can identify is equally
    # unjudgeable whether it is one of 3 or one of 85 — so every all-blind
    # shape declines without a model call and completes with Arena's default,
    # exactly like the captured 85/85 menu.
    planner = ActionPlanner(Mock())
    llm = Mock(side_effect=AssertionError("a blind menu must not reach the model"))
    planner._llm_decision_options = llm

    for count, maximum in ((3, 1), (9, 1), (85, 1), (85, 84)):
        decision = _captured_decision(
            select_n_ids=ETALI_FAVOR_OPTION_IDS[:count],
            select_n_min=1,
            select_n_max=maximum,
        )
        assert decision is not None, (count, maximum)
        assert planner._plan_decision_options(decision, {"players": [], "battlefield": []}) == [
            DECLINE_DECISION
        ], (count, maximum)

    llm.assert_not_called()


def test_trigger_stacking_selection_keeps_its_established_policy():
    # Real logged case #341 (tests/fixtures/scry_transition_341.json, poll
    # index 3): after a failed trigger selection expired, Arena re-offered the
    # same revealed card as a SelectN over already-triggered abilities
    # (SelectionContext_TriggeredAbility / OptionContext_Stacking). Its option
    # ids are unresolved, but this menu has its own established policy — the
    # dedicated stacking prompt — so it is not declined (the scry-transition
    # regression tests pin this end to end).
    import json
    from pathlib import Path

    raw = json.loads((Path(__file__).parent / "fixtures/scry_transition_341.json").read_text())
    select = raw[3]["message"]["selectNReq"]
    decision = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "SelectN",
            "select_n_ids": select["ids"],
            "select_n_min": select["minSel"],
            "select_n_max": select["maxSel"],
            "select_n_context": select["context"],
            "select_n_option_context": select["optionContext"],
        },
        resolve_instance=lambda _: "",
    )
    assert decision is not None
    assert decision.selection_context == "SelectionContext_TriggeredAbility"
    assert decision.option_context == "OptionContext_Stacking"
    assert all(option.meta.get("identity_known") is False for option in decision.options)

    planner = ActionPlanner(Mock())
    llm = Mock(return_value=["sel:337", "sel:338"])
    planner._llm_decision_options = llm

    assert planner._plan_decision_options(decision, {"players": [], "battlefield": []}) == [
        "sel:337",
        "sel:338",
    ]
    llm.assert_called_once()


def test_loot_discard_waits_for_the_name_then_decides_among_named_cards_only(monkeypatch):
    """The hand-card identity wait (match 07c043d8's loot) is untouched: wait
    for the log to name the card just drawn, then decide — but among the named
    cards only, so the model never sees an "Option N" (the 2026-10-08 game-1
    incident had it discard Sphinx's Approach unseen)."""

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
    # The still-unnamed third card is withheld from the model, not offered.
    assert [option.option_id for option in offered.options] == ["sel:119", "sel:232"]
