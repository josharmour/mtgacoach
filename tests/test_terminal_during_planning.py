"""#711: a plan that returns after the game authoritatively ended is discarded quietly.

Incident (3.7.0, 2026-10-31 17:31:28): the auto-concede was acknowledged at
.272 and the old SelectN plan was reported expired at .371. Current master
already labels an unexplained stale outcome as attribution unknown (#341), so
the gap here is narrower: an obsolete plan could still submit a gameplay
command, enter an actionable preview or announce a manual failure for a game
that had already ended normally.
"""

from unittest.mock import MagicMock

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


def test_plan_returning_after_the_game_ended_is_discarded_without_a_takeover_report(caplog):
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
                actions=[GameAction(action_type=ActionType.ASSIGN_DAMAGE, card_name="Mossborn Hydra")],
                overall_strategy="All-in race",
            )

    engine = make_engine(bridge, Planner(), game_state=lambda: live[0])
    engine._game_plan_mgr = None
    engine._pause_for_manual = MagicMock()
    engine._notify = MagicMock()
    engine._bug_report_fn = MagicMock()
    engine._execute_action = MagicMock()

    with caplog.at_level("INFO", logger="arenamcp.autopilot"):
        assert engine.process_trigger(assign_damage_state(), "stack_spell_yours") is True

    # No gameplay command for a game that is already over.
    bridge.submit_assign_damage.assert_not_called()
    bridge.submit_pass.assert_not_called()
    engine._execute_action.assert_not_called()
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
                actions=[GameAction(action_type=ActionType.SELECT_N, select_card_names=["Forest"])],
                overall_strategy="Take the Forest",
            )

    engine = make_engine(bridge, Planner(), game_state=lambda: live[0])
    engine._game_plan_mgr = None
    engine._pause_for_manual = MagicMock()
    engine._notify = MagicMock()
    engine._bug_report_fn = MagicMock()
    engine._execute_action = MagicMock()
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


def test_the_real_concede_latch_discards_a_plan_that_returns_after_the_game_ended(caplog):
    harness = _harness()
    engine = _plan_after_concede(harness, coach_match_number=7, caplog=caplog)

    engine._execute_action.assert_not_called()
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
