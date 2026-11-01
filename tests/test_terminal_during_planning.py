"""#711: a plan that returns after the game authoritatively ended is discarded quietly.

Incident (3.7.0, 2026-10-31 17:31:28): the auto-concede was acknowledged at
.272 and the old SelectN plan was reported expired at .371. Current master
already labels an unexplained stale outcome as attribution unknown (#341), so
the gap here is narrower: an obsolete plan could still submit a gameplay
command, enter an actionable preview or announce a manual failure for a game
that had already ended normally.
"""

from unittest.mock import MagicMock

import pytest
from tests.bug_180436_states import bug_state
from tests.commander_return_states import ASSIGN_DAMAGE_POLL, assign_damage_state
from tests.concede_fakes import Harness
from tests.fakes import FakePlanner, make_engine

from arenamcp.action_planner import ActionPlan, ActionType, GameAction
from arenamcp.autopilot import AutopilotState
from arenamcp.standalone import StandaloneCoach

MATCH_ID = "d58f6da0-41c8-4313-bbf6-75b046ab5b25"

INTERMISSION_POLL = {
    "ok": True,
    "has_pending": True,
    "request_type": "Intermission",
    "request_class": "IntermissionRequest",
    "actions": [],
}


def _ended_state() -> dict:
    """The snapshot once Arena has ended the game (bridge intermission)."""
    state = assign_damage_state()
    state.update(
        pending_decision=None,
        decision_context=None,
        legal_actions=[],
        _bridge_request_type=None,
        _bridge_request_class=None,
        _bridge_in_intermission=True,
        match_ended=True,
    )
    return state


@pytest.mark.parametrize(
    "action_type",
    [None, ActionType.ASSIGN_DAMAGE, ActionType.PASS_PRIORITY, ActionType.RESOLVE, ActionType.CLICK_BUTTON],
    ids=["empty", "assign-damage", "pass", "resolve", "click"],
)
def test_plan_returning_after_the_game_ended_is_discarded_without_a_takeover_report(caplog, action_type):
    bridge = MagicMock()
    bridge.connected = True
    bridge.get_pending_actions.return_value = ASSIGN_DAMAGE_POLL
    live = [assign_damage_state()]

    class Planner(FakePlanner):
        def plan_actions(self, *args, **kwargs):
            self.plan_calls += 1
            # The game ends while this plan is in flight.
            bridge.get_pending_actions.return_value = INTERMISSION_POLL
            live[0] = _ended_state()
            return ActionPlan(
                actions=[] if action_type is None else [GameAction(action_type=action_type)],
                overall_strategy="All-in race",
            )

    engine = make_engine(bridge, Planner(), game_state=lambda: live[0])
    engine._game_plan_mgr = None
    engine._pause_for_manual = MagicMock()
    engine._notify = MagicMock()
    engine._bug_report_fn = MagicMock()
    engine._execute_action = MagicMock()
    engine._try_interactive_safe_default = MagicMock()
    engine._emit_turn_plan_payload = MagicMock()

    with caplog.at_level("INFO", logger="arenamcp.autopilot"):
        assert engine.process_trigger(assign_damage_state(), "stack_spell_yours") is True

    # No gameplay command for a game that is already over.
    bridge.submit_assign_damage.assert_not_called()
    bridge.submit_pass.assert_not_called()
    engine._execute_action.assert_not_called()
    engine._try_interactive_safe_default.assert_not_called()
    engine._emit_turn_plan_payload.assert_not_called()
    # No actionable preview and no manual-failure announcement.
    assert engine._state == AutopilotState.IDLE
    assert getattr(engine, "_current_plan", None) is None
    engine._pause_for_manual.assert_not_called()
    messages = [str(call) for call in engine._notify.call_args_list]
    assert not any("PLAN:" in message for message in messages)
    assert not any("Plan discarded" in message for message in messages)
    # A normal game end is neither a user takeover nor a bridge failure.
    assert engine._pending_fallback_bugs == []
    assert engine._recent_takeovers == []
    # The discard is still explained locally.
    assert any("game ended during planning" in record.getMessage().lower() for record in caplog.records)


# --- the concede latch: producer (ConcedeController) -> coach -> autopilot ---------


def _harness(match_id: str = MATCH_ID) -> Harness:
    """A real ConcedeController on a snapshot with no ``game_number``.

    The #711 incident snapshot had none, so the latch must key on the coach's
    own per-game counter.
    """
    state = bug_state()
    state["match_id"] = match_id
    return Harness(state)


def _accepted_concede(match_id: str = MATCH_ID, match_number: int = 7) -> Harness:
    """A real ConcedeController whose bridge accepted a concede for one game."""
    harness = _harness(match_id)
    harness.observe((match_id, match_number))
    harness.run_countdown()
    assert "sent" in harness.states, harness.states
    return harness


def _coach_with(harness: Harness, match_number: int):
    coach = StandaloneCoach.__new__(StandaloneCoach)
    coach._concede = harness.controller
    coach._match_number = match_number
    return coach


def test_the_concede_latch_is_scoped_to_the_coachs_per_game_counter():
    harness = _accepted_concede()
    controller = harness.controller

    assert controller.conceded_game(MATCH_ID, 7) is True
    # The next game of the same match is a different game.
    assert controller.conceded_game(MATCH_ID, 8) is False
    # Another match never inherits it.
    assert controller.conceded_game("another-match", 7) is False
    # No identity at all is never evidence.
    assert controller.conceded_game(MATCH_ID, None) is False
    assert controller.conceded_game(None, None) is False


def test_a_refused_or_cancelled_concede_is_not_terminal_evidence():
    state = bug_state()
    state["match_id"] = MATCH_ID

    refused = Harness(state, result={"ok": False, "error": "refused"}, ends_on_concede=False)
    refused.observe((MATCH_ID, 7))
    refused.run_countdown()
    assert refused.controller.conceded_game(MATCH_ID, 7) is False

    cancelled = Harness(dict(state))
    cancelled.observe((MATCH_ID, 7))
    cancelled.controller.cancel("test")
    cancelled.run_countdown()
    assert cancelled.controller.conceded_game(MATCH_ID, 7) is False


def _plan_after_concede(
    harness: Harness,
    coach_match_number: int,
    caplog,
    *,
    advance_to: int | None = None,
    trigger_match_id: str | None = MATCH_ID,
    acknowledge_in_flight: bool = True,
    returned_actions: list[GameAction] | None = None,
):
    """Run one planning call whose plan returns after the game ended.

    The snapshot keeps no terminal flags and no ``game_number`` — exactly the
    17:31:28 incident shape — so only the real latch can discard the plan.
    With ``acknowledge_in_flight`` the real controller acknowledges this
    game's concede *inside* the planner call, so the acknowledgment lands
    while the plan is in flight rather than before planning starts.
    ``advance_to`` moves the coach's counter while the plan is in flight, as
    the coaching loop does when it detects the next game of the match.
    ``trigger_match_id=None`` starts planning before the snapshot carried a
    match id, which the poll then enriches.
    """
    live_state = assign_damage_state()
    live_state["match_id"] = MATCH_ID
    live = [live_state]

    bridge = MagicMock()
    bridge.connected = True
    bridge.get_pending_actions.return_value = ASSIGN_DAMAGE_POLL

    coach = _coach_with(harness, coach_match_number)

    class Planner(FakePlanner):
        def plan_actions(self, *args, **kwargs):
            self.plan_calls += 1
            if acknowledge_in_flight:
                # Nothing was acknowledged before planning started; the real
                # countdown runs and its bridge accepts it mid-call.
                assert "sent" not in harness.states, harness.states
                harness.observe((MATCH_ID, coach_match_number))
                harness.run_countdown()
                assert "sent" in harness.states, harness.states
            # The snapshot has not caught up with the acknowledgment.
            bridge.get_pending_actions.return_value = {"ok": True, "has_pending": False}
            if advance_to is not None:
                coach._match_number = advance_to
            return ActionPlan(
                actions=(
                    [GameAction(action_type=ActionType.SELECT_N, select_card_names=["Forest"])]
                    if returned_actions is None
                    else returned_actions
                ),
                overall_strategy="Take the Forest",
            )

    engine = make_engine(bridge, Planner(), game_state=lambda: live[0])
    engine._game_plan_mgr = None
    engine._pause_for_manual = MagicMock()
    engine._notify = MagicMock()
    engine._bug_report_fn = MagicMock()
    engine._execute_action = MagicMock()
    engine._try_interactive_safe_default = MagicMock()
    engine._emit_turn_plan_payload = MagicMock()
    # The real producer-to-autopilot wiring: coach -> ConcedeController.
    engine._game_key_fn = coach._autopilot_game_key
    engine._conceded_game_fn = coach._conceded_game_for_autopilot

    trigger_state = dict(live_state)
    if trigger_match_id is None:
        trigger_state.pop("match_id", None)
    else:
        trigger_state["match_id"] = trigger_match_id

    with caplog.at_level("INFO", logger="arenamcp.autopilot"):
        assert engine.process_trigger(trigger_state, "stack_spell_yours") is True
    return engine


@pytest.mark.parametrize(
    "action_type",
    [None, ActionType.SELECT_N, ActionType.PASS_PRIORITY, ActionType.RESOLVE, ActionType.CLICK_BUTTON],
    ids=["empty", "select", "pass", "resolve", "click"],
)
def test_the_real_concede_latch_discards_a_plan_that_returns_after_the_game_ended(caplog, action_type):
    harness = _harness()
    engine = _plan_after_concede(
        harness,
        coach_match_number=7,
        caplog=caplog,
        returned_actions=[] if action_type is None else [GameAction(action_type=action_type)],
    )

    engine._execute_action.assert_not_called()
    engine._try_interactive_safe_default.assert_not_called()
    engine._emit_turn_plan_payload.assert_not_called()
    assert engine._state == AutopilotState.IDLE
    assert engine._pending_fallback_bugs == []
    assert engine._recent_takeovers == []
    messages = [str(call) for call in engine._notify.call_args_list]
    assert not any("PLAN:" in message for message in messages)
    assert not any("Plan discarded" in message for message in messages)
    assert any("concede acknowledged" in record.getMessage().lower() for record in caplog.records)


def test_a_game7_plan_is_discarded_when_the_counter_moves_to_game8_in_flight(caplog):
    """The game-to-game boundary: the plan answers game 7, which was conceded.

    Game 8 re-issues the same request (same gsid and action count), so the
    window identity matches and only the captured game key can tell them
    apart. Reading the counter after planning would compare against game 8,
    lose game 7's acknowledgment and execute an obsolete plan.
    """
    harness = _harness()
    engine = _plan_after_concede(harness, coach_match_number=7, caplog=caplog, advance_to=8)

    engine._execute_action.assert_not_called()
    assert engine._state == AutopilotState.IDLE
    assert engine._pending_fallback_bugs == []
    assert engine._recent_takeovers == []
    assert any("concede acknowledged" in record.getMessage().lower() for record in caplog.records)


def test_the_concede_latch_does_not_suppress_the_next_game_in_the_same_match(caplog):
    harness = _accepted_concede(match_number=7)
    # A fresh game-8 plan starts after the coaching loop counted game 8.
    engine = _plan_after_concede(harness, coach_match_number=8, caplog=caplog, acknowledge_in_flight=False)

    engine._execute_action.assert_called_once()


def test_a_plan_from_the_previous_game_is_discarded_when_the_next_game_starts_in_flight(caplog):
    """A natural game end leaves no terminal flags in the new game's snapshot.

    The counter moves to game 8 while the game-7 plan is in flight and the
    fresh snapshot looks like an ordinary priority window, so only the
    captured per-game key can tell the plan it answers a game that is over.
    """
    harness = _harness()  # no concede at all
    engine = _plan_after_concede(
        harness,
        coach_match_number=7,
        caplog=caplog,
        advance_to=8,
        acknowledge_in_flight=False,
    )

    engine._execute_action.assert_not_called()
    assert engine._state == AutopilotState.IDLE
    assert engine._pending_fallback_bugs == []
    assert engine._recent_takeovers == []
    assert any(
        "next game started during planning" in record.getMessage().lower() for record in caplog.records
    )


def test_a_match_id_that_only_became_known_is_the_same_game(caplog):
    """Missing-to-known match id enrichment with an unchanged counter is compatible."""
    harness = _harness()
    engine = _plan_after_concede(
        harness,
        coach_match_number=7,
        caplog=caplog,
        trigger_match_id=None,
        acknowledge_in_flight=False,
    )

    engine._execute_action.assert_called_once()


def _typed_engine(monkeypatch):
    """A typed-path engine built without touching the real GRE singleton.

    ``AutopilotEngine.__init__`` calls ``get_bridge()``, which starts a
    keepalive thread and opens a socket. Patch the factory for construction
    so every typed test here stays offline.
    """
    from tests.test_autopilot_typed_freshness import _engine

    monkeypatch.setattr("arenamcp.autopilot.get_bridge", lambda: MagicMock())
    engine = _engine()
    engine._game_plan_mgr = None
    engine._pause_for_manual = MagicMock()
    return engine


def _typed_engine_with_coach(monkeypatch, harness, match_number):
    from tests.test_autopilot_typed_freshness import _poll

    coach = _coach_with(harness, match_number=match_number)
    engine = _typed_engine(monkeypatch)
    engine._gre_bridge.get_pending_actions.return_value = _poll()
    engine._game_key_fn = coach._autopilot_game_key
    engine._conceded_game_fn = coach._conceded_game_for_autopilot
    return engine, coach


@pytest.mark.parametrize("boundary", ["accepted-concede", "next-game"])
@pytest.mark.parametrize("choice", [["idx:0"], ["pass"], ["__DECLINE__"]])
def test_typed_plan_does_not_submit_after_game_boundary_with_unchanged_request(boundary, choice, monkeypatch):
    """The same accepted-concede lag also affects typed casts/pass/cancel."""
    from arenamcp.action_planner import DECLINE_DECISION

    harness = _harness()
    engine, coach = _typed_engine_with_coach(monkeypatch, harness, match_number=7)

    def choose_after_boundary(*args):
        if boundary == "accepted-concede":
            harness.observe((MATCH_ID, 7))
            harness.run_countdown()
            assert "sent" in harness.states
        else:
            coach._match_number = 8
        return [DECLINE_DECISION] if choice == ["__DECLINE__"] else choice

    engine._planner.plan_decision_options.side_effect = choose_after_boundary
    state = {"match_id": MATCH_ID, "turn": {"turn_number": 7}}
    assert engine._try_typed_decision_path(state, "decision_required") is True
    engine._gre_bridge.submit_action_by_index.assert_not_called()
    engine._gre_bridge.submit_pass.assert_not_called()
    engine._gre_bridge.cancel_action.assert_not_called()
    engine._pause_for_manual.assert_not_called()
    assert engine._actions_executed == 0


def test_a_fresh_next_game_typed_call_is_submitted(monkeypatch):
    """The latch for game 7 must not suppress a fresh game-8 decision."""
    harness = _accepted_concede(match_number=7)
    engine, _coach = _typed_engine_with_coach(monkeypatch, harness, match_number=8)

    assert engine._try_typed_decision_path({"match_id": MATCH_ID}, "decision_required") is True
    engine._gre_bridge.submit_action_by_index.assert_called_once()
    assert engine._actions_executed == 1


def test_a_match_id_that_only_became_known_is_the_same_game_for_a_typed_call(monkeypatch):
    """Missing-to-known match id enrichment with an unchanged counter is compatible."""
    harness = _harness()
    engine, _coach = _typed_engine_with_coach(monkeypatch, harness, match_number=7)

    def enrich_then_choose(decision, state):
        # The snapshot had no match id when planning started; enrichment
        # lands while the model call is in flight.
        state["match_id"] = MATCH_ID
        return ["idx:0"]

    engine._planner.plan_decision_options.side_effect = enrich_then_choose
    assert engine._try_typed_decision_path({}, "decision_required") is True
    engine._gre_bridge.submit_action_by_index.assert_called_once()


def test_a_changed_match_id_with_the_same_counter_is_a_new_game_for_a_typed_call(monkeypatch, caplog):
    """A known match id that changes while planning is a new game, not enrichment."""
    harness = _harness()
    engine, _coach = _typed_engine_with_coach(monkeypatch, harness, match_number=7)

    def switch_match_then_choose(decision, state):
        state["match_id"] = "11111111-2222-3333-4444-555555555555"
        return ["idx:0"]

    engine._planner.plan_decision_options.side_effect = switch_match_then_choose
    with caplog.at_level("INFO", logger="arenamcp.autopilot"):
        assert engine._try_typed_decision_path({"match_id": MATCH_ID}, "decision_required") is True
    engine._gre_bridge.submit_action_by_index.assert_not_called()
    engine._gre_bridge.submit_pass.assert_not_called()
    engine._gre_bridge.cancel_action.assert_not_called()
    assert engine._actions_executed == 0
    assert any(
        "next game started during planning" in record.getMessage().lower() for record in caplog.records
    )


def test_a_concede_accepted_during_the_fresh_poll_is_not_submitted(monkeypatch, caplog):
    """The acknowledgment can land inside the post-planning re-poll (#711)."""
    from tests.test_autopilot_typed_freshness import _poll

    harness = _harness()
    engine, _coach = _typed_engine_with_coach(monkeypatch, harness, match_number=7)
    planned = {"done": False}

    def choose(*args):
        planned["done"] = True
        return ["idx:0"]

    def poll_then_concede(*args):
        if planned["done"] and not harness.states:
            harness.observe((MATCH_ID, 7))
            harness.run_countdown()
            assert "sent" in harness.states
        return _poll()

    engine._planner.plan_decision_options.side_effect = choose
    engine._gre_bridge.get_pending_actions.side_effect = poll_then_concede
    with caplog.at_level("INFO", logger="arenamcp.autopilot"):
        assert engine._try_typed_decision_path({"match_id": MATCH_ID}, "decision_required") is True
    engine._gre_bridge.submit_action_by_index.assert_not_called()
    engine._gre_bridge.submit_pass.assert_not_called()
    engine._gre_bridge.cancel_action.assert_not_called()
    engine._pause_for_manual.assert_not_called()
    assert engine._actions_executed == 0
    assert any("concede acknowledged" in record.getMessage().lower() for record in caplog.records)


@pytest.mark.parametrize("outcome", ["refused", "cancelled"])
def test_a_failed_concede_during_a_typed_call_still_submits(outcome, monkeypatch):
    """A refused or cancelled concession is not terminal evidence."""
    state = bug_state()
    state["match_id"] = MATCH_ID
    if outcome == "refused":
        harness = Harness(state, result={"ok": False, "error": "refused"}, ends_on_concede=False)
    else:
        harness = Harness(state)
    engine, _coach = _typed_engine_with_coach(monkeypatch, harness, match_number=7)

    def fail_concede_then_choose(*args):
        harness.observe((MATCH_ID, 7))
        if outcome == "cancelled":
            harness.controller.cancel("test")
        harness.run_countdown()
        assert harness.controller.conceded_game(MATCH_ID, 7) is False
        return ["idx:0"]

    engine._planner.plan_decision_options.side_effect = fail_concede_then_choose
    assert engine._try_typed_decision_path({"match_id": MATCH_ID}, "decision_required") is True
    engine._gre_bridge.submit_action_by_index.assert_called_once()
