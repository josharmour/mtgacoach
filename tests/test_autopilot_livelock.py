"""Regression tests for the 2026-06-09 live livelock.

Chain observed live: planner fallback picked an unpayable cast (no [OK]
tag) → cast entered PayCosts/targeting → rolled back → re-planned → re-cast
at machine speed, locking the user out of the MTGA UI. Plus: legal
"Activate Ability: X" lines were mis-parsed so legitimate planner actions
got dropped as illegal, forcing the bad fallback in the first place.
"""

import time

import arenamcp.autopilot as autopilot_module
from arenamcp.action_planner import ActionPlanner, ActionType
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine


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

    action = GameAction(
        action_type=ActionType.ACTIVATE_ABILITY, card_name="Hopeless Nightmare"
    )
    legal = [
        "Cast Momentum Breaker",
        "Activate Ability: Hopeless Nightmare",
        "Pass",
    ]
    assert p._is_legal_default(action, legal)


def test_fallback_never_picks_unpayable_cast_when_ok_tagging_active():
    legal = [
        "Cast Momentum Breaker",            # listed but NOT auto-payable
        "Cast Ruthless Negotiation [OK]",   # payable
        "Pass",
    ]
    picked = ActionPlanner._pick_preferred_legal_action(legal)
    assert picked == "Cast Ruthless Negotiation [OK]"


def test_fallback_prefers_pass_over_unpayable_cast():
    legal = ["Cast Momentum Breaker", "Cast Liliana of the Veil [OK]", "Pass"]
    # remove the only [OK] cast → with tagging active, bare cast loses to Pass
    legal_no_ok_cast = ["Cast Momentum Breaker", "Done (confirm) [OK]", "Pass"]
    picked = ActionPlanner._pick_preferred_legal_action(legal_no_ok_cast)
    assert picked != "Cast Momentum Breaker"


def test_fallback_unchanged_without_ok_tagging():
    # Log-fallback mode has no [OK] tags at all — keep legacy behavior.
    legal = ["Cast Shock", "Pass"]
    assert ActionPlanner._pick_preferred_legal_action(legal) == "Cast Shock"


# ---------------------------------------------------------------------------
# Autopilot-side loop breakers
# ---------------------------------------------------------------------------


class _DummyPlanner:
    _timeout = 0.1
    _backend = object()

    def get_recent_diagnostics(self):
        return []


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


def _engine(monkeypatch, bridge=None) -> AutopilotEngine:
    monkeypatch.setattr(
        autopilot_module, "get_bridge", lambda: bridge or _DummyBridge()
    )
    return AutopilotEngine(
        planner=_DummyPlanner(),
        mapper=_DummyMapper(),
        controller=_DummyController(),
        get_game_state=lambda: {},
        config=AutopilotConfig(dry_run=False),
    )


def _state(turn, **extra):
    s = {"turn": {"turn_number": turn}}
    s.update(extra)
    return s


def test_rolled_back_cast_hidden_from_planner(monkeypatch):
    eng = _engine(monkeypatch)
    eng._last_cast_submitted = (3, "momentum breaker")
    eng._note_cast_rollback("PayCosts cancelled (test)")
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


def test_single_rollback_does_not_suppress(monkeypatch):
    eng = _engine(monkeypatch)
    eng._last_cast_submitted = (3, "momentum breaker")
    eng._note_cast_rollback("once is allowed")
    legal = ["Cast Momentum Breaker", "Pass"]
    assert eng._filter_rolled_back_casts(legal, _state(3)) == legal


def test_auto_respond_escape_budget_per_turn(monkeypatch):
    bridge = _DummyBridge()
    eng = _engine(monkeypatch, bridge)
    gs = _state(5, _bridge_request_type="SelectTargets")

    assert eng._try_auto_respond_escape(gs, "test 1") is True
    assert eng._try_auto_respond_escape(gs, "test 2") is True
    # Third escape in the same turn must be refused.
    assert eng._try_auto_respond_escape(gs, "test 3") is False
    assert bridge.auto_respond_calls == 2

    # New turn resets the budget.
    gs6 = _state(6, _bridge_request_type="SelectTargets")
    assert eng._try_auto_respond_escape(gs6, "test 4") is True


def test_escape_on_casting_window_counts_as_cast_rollback(monkeypatch):
    eng = _engine(monkeypatch)
    eng._last_cast_submitted = (5, "ruthless negotiation")
    gs = _state(5, _bridge_request_type="SelectTargets")
    eng._try_auto_respond_escape(gs, "test")
    assert eng._cast_rollback_counts.get((5, "ruthless negotiation")) == 1


def test_escape_blocked_on_young_window(monkeypatch):
    """The escape must NOT fire just because triggers ping fast — a window
    that appeared <12s ago is not 'stuck', it just hasn't been handled yet.
    Live 2026-06-09: the escape fired 0.5s after a cast and consumed the
    SelectTargetsRequest, freezing the game on the targeting arrow."""
    bridge = _DummyBridge()
    eng = _engine(monkeypatch, bridge)
    eng._window_repeat_sig = ("sig",)
    eng._window_repeat_count = 50          # trigger spam
    eng._window_first_seen_at = time.monotonic()  # window just appeared
    gs = _state(5, _bridge_request_type="SelectTargets")
    assert eng._maybe_escape_stuck_window(gs) is False
    assert bridge.auto_respond_calls == 0

    # Same window, genuinely old → escape allowed.
    eng._window_first_seen_at = time.monotonic() - 60.0
    assert eng._maybe_escape_stuck_window(gs) is True
    assert bridge.auto_respond_calls == 1


def test_runaway_protection_stands_down_for_turn(monkeypatch):
    eng = _engine(monkeypatch)
    eng._runaway_tripped_turn = 7
    assert eng.process_trigger(_state(7), "decision_required") is False
    # Next turn it self-clears; with no legal work it still returns False,
    # but the trip flag must be gone.
    eng.process_trigger(_state(8), "decision_required")
    assert eng._runaway_tripped_turn is None


def test_given_up_window_silences_autopilot(monkeypatch):
    """After MANUAL REQUIRED, the same window must not be replanned —
    the backstop re-forcing decision_required every 2s replayed the same
    LLM call + TTS line forever (live 2026-06-09)."""
    eng = _engine(monkeypatch)
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


def test_new_match_clears_rollback_memory(monkeypatch):
    eng = _engine(monkeypatch)
    eng._max_seen_turn = 9
    eng._cast_rollback_counts[(9, "shock")] = 2
    eng._last_cast_submitted = (9, "shock")
    # Turn counter goes backwards → new match.
    eng.process_trigger(_state(1), "decision_required")
    assert eng._cast_rollback_counts == {}
    assert eng._last_cast_submitted is None
