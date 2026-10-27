"""Regression tests for the 2026-06-09 live livelock.

Chain observed live: planner fallback picked an unpayable cast (no [OK]
tag) → cast entered PayCosts/targeting → rolled back → re-planned → re-cast
at machine speed, locking the user out of the MTGA UI. Plus: legal
"Activate Ability: X" lines were mis-parsed so legitimate planner actions
got dropped as illegal, forcing the bad fallback in the first place.
"""

import time

import pytest
from tests.fakes import make_engine

from arenamcp.action_planner import ActionPlanner, ActionType
from arenamcp.autopilot import AutopilotEngine

# ---------------------------------------------------------------------------
# Planner-side fixes
# ---------------------------------------------------------------------------


def _planner() -> ActionPlanner:
    return ActionPlanner.__new__(ActionPlanner)  # no backend needed


def test_activate_ability_legal_line_parses_card_name():
    p = _planner()
    a = p._legal_action_to_action("Activate Ability: Hopeless Nightmare")
    assert a is not None
    assert a.action_type == ActionType.ACTIVATE_ABILITY
    assert a.card_name == "Hopeless Nightmare"


def test_activate_ability_planner_action_is_legal():
    p = _planner()
    from arenamcp.action_planner import GameAction

    action = GameAction(action_type=ActionType.ACTIVATE_ABILITY, card_name="Hopeless Nightmare")
    legal = [
        "Cast Momentum Breaker",
        "Activate Ability: Hopeless Nightmare",
        "Pass",
    ]
    assert p._is_legal_default(action, legal)


@pytest.mark.parametrize(
    ("legal", "expected"),
    [
        # 2026-07-01 policy: the blind fallback never casts while Pass is
        # available, payable or not. A blind cast picks blind targets and
        # re-arms wedge spirals (Patriar's Humiliation).
        (["Cast Momentum Breaker", "Cast Ruthless Negotiation [OK]", "Pass"], {"Pass"}),
        (["Cast Shock", "Pass"], {"Pass"}),
        # A payable confirm step is taken; the unpayable cast never is.
        (["Cast Momentum Breaker", "Done (confirm) [OK]", "Pass"], {"Done (confirm) [OK]"}),
        # Some windows are cast-or-nothing: then a payable cast is allowed.
        (["Cast Shock [OK]", "Cast Momentum Breaker"], {"Cast Shock [OK]"}),
        # 2026-07-02 (#387): the 403'd planner auto-picked "Activate Ability:
        # Mutagen" and the targeting window it opened was answered blindly
        # (buffed the opponent's creature). Blind activations and blind
        # attack-alls sit below Pass, same as casts.
        (["Activate Ability: Mutagen [OK]", "Pass"], {"Pass"}),
        (["Declare Attackers: all", "Pass"], {"Pass"}),
        # A discard prompt is answered with a discard, not Pass.
        (["Pass", "Discard Mutavault", "Discard Forest"], {"Discard Mutavault", "Discard Forest"}),
    ],
    ids=[
        "pass_over_payable_cast",
        "pass_over_unpayable_cast",
        "payable_confirm_over_unpayable_cast",
        "payable_cast_when_nothing_safer",
        "pass_over_blind_activation",
        "pass_over_blind_attack_all",
        "discard",
    ],
)
def test_blind_fallback_pick(legal, expected):
    assert ActionPlanner._pick_preferred_legal_action(legal) in expected


def test_select_target_legal_under_target_selection_context():
    """'target_selection' decision context must accept select_target plans —
    the substring heuristic missed this pairing and dropped valid target
    picks (live 2026-06-09: Nurturing Presence stall)."""
    p = _planner()
    from arenamcp.action_planner import GameAction

    action = GameAction(
        action_type=ActionType.SELECT_TARGET,
        card_name="Nurturing Presence",
        target_names=["Light-Paws, Emperor's Voice"],
    )
    assert p._is_action_legal(
        action,
        ["Select target for Nurturing Presence"],
        decision_context={"type": "target_selection"},
        bridge_request=None,
    )


# ---------------------------------------------------------------------------
# Autopilot-side loop breakers
# ---------------------------------------------------------------------------


class _DummyBridge:
    def __init__(self):
        self.connected = True
        self.auto_respond_calls = 0

    def connect(self):
        return True

    def auto_respond(self):
        self.auto_respond_calls += 1
        return True

    def get_pending_actions(self):
        return {"ok": True, "has_pending": False}

    def submit_pass(self):
        return False


def _engine(bridge=None) -> AutopilotEngine:
    return make_engine(bridge or _DummyBridge())


def _state(turn, **extra):
    s = {"turn": {"turn_number": turn}}
    s.update(extra)
    return s


def test_rolled_back_cast_hidden_from_planner():
    eng = _engine()
    # Each rollback consumes the submission record (one submission = at
    # most one rollback), so re-set it between notes like a real
    # submit → rollback → resubmit → rollback cycle.
    eng._last_cast_submitted = (3, "momentum breaker")
    eng._last_cast_submitted_ts = time.monotonic()
    eng._note_cast_rollback("PayCosts cancelled (test)")
    eng._last_cast_submitted = (3, "momentum breaker")
    eng._last_cast_submitted_ts = time.monotonic()
    eng._note_cast_rollback("PayCosts cancelled (test)")

    legal = [
        "Cast Momentum Breaker",
        "Cast Momentum Breaker [OK]",
        "Cast Ruthless Negotiation [OK]",
        "Pass",
    ]
    filtered = eng._filter_rolled_back_casts(legal, _state(3))
    assert "Cast Momentum Breaker" not in filtered
    assert "Cast Momentum Breaker [OK]" not in filtered
    assert "Cast Ruthless Negotiation [OK]" in filtered

    # Next turn the suppression lifts (fresh mana, fresh chances).
    assert "Cast Momentum Breaker" in eng._filter_rolled_back_casts(legal, _state(4))


def test_cast_rollback_game_limit_survives_turn_changes():
    # 2026-07-01: Patriar's Humiliation wedged at targeting, rolled back on
    # the timer, and was re-picked on LATER turns — the per-turn key reset
    # each time. After the game-wide limit, the cast stays suppressed.
    eng = _engine()
    for turn in (3, 4, 5):
        eng._last_cast_submitted = (turn, "patriar's humiliation")
        eng._last_cast_submitted_ts = time.monotonic()
        eng._note_cast_rollback("timer rollback (test)")

    legal = ["Cast Patriar's Humiliation [OK]", "Pass"]
    filtered = eng._filter_rolled_back_casts(legal, _state(6))
    assert "Cast Patriar's Humiliation [OK]" not in filtered
    assert "Pass" in filtered


def test_single_rollback_does_not_suppress():
    eng = _engine()
    eng._last_cast_submitted = (3, "momentum breaker")
    eng._last_cast_submitted_ts = time.monotonic()
    eng._note_cast_rollback("once is allowed")
    legal = ["Cast Momentum Breaker", "Pass"]
    assert eng._filter_rolled_back_casts(legal, _state(3)) == legal


def test_auto_respond_escape_budget_per_turn():
    bridge = _DummyBridge()
    eng = _engine(bridge)
    gs = _state(5, _bridge_request_type="Group")

    assert eng._try_auto_respond_escape(gs, "test 1") is True
    assert eng._try_auto_respond_escape(gs, "test 2") is True
    # Third escape in the same turn must be refused.
    assert eng._try_auto_respond_escape(gs, "test 3") is False
    assert bridge.auto_respond_calls == 2

    # New turn resets the budget.
    gs6 = _state(6, _bridge_request_type="Group")
    assert eng._try_auto_respond_escape(gs6, "test 4") is True


def test_refused_payment_escape_does_not_record_a_cast_rollback():
    # PayCosts requires validated payment/cancellation, never generic AutoResp.
    bridge = _DummyBridge()
    eng = _engine(bridge)
    eng._last_cast_submitted = (5, "ruthless negotiation")
    eng._last_cast_submitted_ts = time.monotonic()
    gs = _state(5, _bridge_request_type="PayCosts")
    assert eng._try_auto_respond_escape(gs, "test") is False
    assert bridge.auto_respond_calls == 0
    assert (5, "ruthless negotiation") not in eng._cast_rollback_counts


def test_escape_blocked_on_young_window():
    """The escape must NOT fire just because triggers ping fast — a window
    that appeared <12s ago is not 'stuck', it just hasn't been handled yet.
    Live 2026-06-09: the escape fired 0.5s after a cast and consumed the
    SelectTargetsRequest, freezing the game on the targeting arrow."""
    bridge = _DummyBridge()
    eng = _engine(bridge)
    eng._window_repeat_sig = ("sig",)
    eng._window_repeat_count = 50  # trigger spam
    eng._window_first_seen_at = time.monotonic()  # window just appeared
    gs = _state(5, _bridge_request_type="Group")
    assert eng._maybe_escape_stuck_window(gs) is False
    assert bridge.auto_respond_calls == 0

    # Old polling without attempted submissions is still not evidence of a loop.
    eng._window_first_seen_at = time.monotonic() - 60.0
    assert eng._maybe_escape_stuck_window(gs) is False
    assert bridge.auto_respond_calls == 0


def test_runaway_protection_stands_down_for_turn():
    eng = _engine()
    eng._runaway_tripped_turn = 7
    assert eng.process_trigger(_state(7), "decision_required") is False
    # Next turn it self-clears; with no legal work it still returns False,
    # but the trip flag must be gone.
    eng.process_trigger(_state(8), "decision_required")
    assert eng._runaway_tripped_turn is None


def test_given_up_window_silences_autopilot():
    """After MANUAL REQUIRED, the same window must not be replanned —
    the backstop re-forcing decision_required every 2s replayed the same
    LLM call + TTS line forever (live 2026-06-09)."""
    eng = _engine()
    gs = _state(5, pending_decision="Select Targets")
    gs["_bridge_game_state_id"] = 123

    assert eng.is_window_given_up(gs) is False
    eng._given_up_window_sig = eng._priority_window_signature(gs)
    assert eng.is_window_given_up(gs) is True
    # process_trigger goes silent for this window.
    assert eng.process_trigger(gs, "decision_required") is False

    # Any window change (user clicked / game advanced) re-arms autopilot.
    gs2 = dict(gs)
    gs2["_bridge_game_state_id"] = 124
    assert eng.is_window_given_up(gs2) is False


def test_new_match_clears_rollback_memory():
    eng = _engine()
    eng._max_seen_turn = 9
    eng._cast_rollback_counts[(9, "shock")] = 2
    eng._last_cast_submitted = (9, "shock")
    eng._last_cast_submitted_ts = time.monotonic()
    # Turn counter goes backwards → new match.
    eng.process_trigger(_state(1), "decision_required")
    assert eng._cast_rollback_counts == {}
    assert eng._last_cast_submitted is None


def test_rolled_back_ability_hidden_from_planner():
    # P0-6 (2026-07-05): Utter Insignificance's exile ability was activated
    # 3x into an unpayable {C}, each cancelling at PayCosts, and the
    # cast-only rollback machinery never suppressed it.
    eng = _engine()
    for _ in range(2):
        eng._last_cast_submitted = (14, "utter insignificance")
        eng._last_cast_submitted_ts = time.monotonic()
        eng._note_cast_rollback("PayCosts cancelled (test)")

    legal = [
        "Activate Ability: Utter Insignificance",
        "Cast Witch Enchanter [OK]",
        "Pass",
    ]
    filtered = eng._filter_rolled_back_casts(legal, _state(14))
    assert "Activate Ability: Utter Insignificance" not in filtered
    assert "Cast Witch Enchanter [OK]" in filtered


def test_commander_exempt_from_game_wide_suppression():
    # #40 (live 2026-07-06): 3 PayCosts failures game-locked Hei Bai (the
    # commander). Command-zone cards keep the per-turn limit but never the
    # game-wide one.
    eng = _engine()
    eng._cast_rollback_totals["hei bai, forest guardian"] = 3

    state = _state(9)
    state["command"] = [{"name": "Hei Bai, Forest Guardian"}]
    legal = ["Cast Hei Bai, Forest Guardian [OK]", "Pass"]
    assert "Cast Hei Bai, Forest Guardian [OK]" in eng._filter_rolled_back_casts(legal, state)

    # A non-commander card with the same record stays suppressed.
    state["command"] = []
    assert "Cast Hei Bai, Forest Guardian [OK]" not in eng._filter_rolled_back_casts(legal, state)

    # The per-turn limit still applies to the commander.
    state["command"] = [{"name": "Hei Bai, Forest Guardian"}]
    eng._cast_rollback_counts[(9, "hei bai, forest guardian")] = 2
    assert "Cast Hei Bai, Forest Guardian [OK]" not in eng._filter_rolled_back_casts(legal, state)
