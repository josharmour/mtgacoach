"""Regression tests for the 2026-07-05 Go-Shintai self-destruction.

Live chain: an end-step PayCostsReq from Go-Shintai of Hidden Cruelty's
optional trigger ("you may pay {1} ... destroy target creature with
toughness X or less") was blind auto-paid, all opponent creatures exceeded
the toughness cap, and the forced SelectTargets destroyed the user's own
1/1 Spirit. The gate: only blind auto-pay costs of actions the autopilot
itself just submitted; out-of-band cancellable harmful triggers get a
pay/decline decision (decline when the LLM is unavailable).
"""

import time

from tests.fakes import make_engine

from arenamcp.autopilot import AutopilotEngine


class _NoLLMPlanner:
    """Planner whose pay/decline LLM path is unavailable."""

    def plan_pay_or_decline(self, name, oracle, game_state):
        return None


class _PayPlanner:
    def plan_pay_or_decline(self, name, oracle, game_state):
        return True


class _DeclinePlanner:
    def plan_pay_or_decline(self, name, oracle, game_state):
        return False


def _engine(planner=None) -> AutopilotEngine:
    return make_engine(planner=planner or _NoLLMPlanner())


def _go_shintai_state(can_cancel=True):
    return {
        "turn": {"turn_number": 14},
        "_bridge_can_cancel": can_cancel,
        "decision_context": {"type": "pay_costs", "sourceId": 900},
        "stack": [
            {
                "instance_id": 900,
                "name": "Go-Shintai of Hidden Cruelty",
                "oracle_text": (
                    "you may pay {1}. when you do, destroy target creature with toughness x or less"
                ),
            }
        ],
        "battlefield": [],
        "local_seat_id": 1,
    }


def test_out_of_band_harmful_cost_declined_when_llm_unavailable():
    eng = _engine()
    reason = eng._should_decline_optional_cost(_go_shintai_state())
    assert reason is not None
    assert "declining conservatively" in reason


def test_out_of_band_harmful_cost_respects_llm_decline():
    eng = _engine(planner=_DeclinePlanner())
    reason = eng._should_decline_optional_cost(_go_shintai_state())
    assert reason is not None
    assert "LLM chose decline" in reason


def test_out_of_band_harmful_cost_pays_when_llm_says_pay():
    eng = _engine(planner=_PayPlanner())
    assert eng._should_decline_optional_cost(_go_shintai_state()) is None


def test_own_fresh_cast_with_matching_source_auto_pays():
    # The normal casting flow: autopilot cast something seconds ago; the
    # PayCosts belongs to it. No LLM call, no decline.
    eng = _engine()
    eng._last_cast_submitted = (14, "rampant growth")
    eng._last_cast_submitted_ts = time.monotonic()
    state = _go_shintai_state()
    state["stack"][0]["name"] = "Rampant Growth"
    assert eng._should_decline_optional_cost(state) is None


def test_stale_own_cast_does_not_bless_the_cost():
    # A cast from 2 minutes ago is not this window's source (live incident:
    # Rampant Growth at 23:00:26 vs the trigger at 23:01:01).
    eng = _engine()
    eng._last_cast_submitted = (12, "rampant growth")
    eng._last_cast_submitted_ts = time.monotonic() - 120.0
    assert eng._should_decline_optional_cost(_go_shintai_state()) is not None


def test_non_cancellable_cost_keeps_auto_pay():
    # No pay/decline choice exists — nothing to gate.
    eng = _engine()
    assert eng._should_decline_optional_cost(_go_shintai_state(can_cancel=False)) is None


def test_beneficial_mana_trigger_declines_when_planner_unavailable():
    eng = _engine()
    state = _go_shintai_state()
    state["stack"][0]["oracle_text"] = "you may pay {2}. if you do, draw a card."
    assert "optional mana cost" in eng._should_decline_optional_cost(state)


def test_command_zone_cast_requires_ok_tag():
    # Issue #414: Hei Bai (command zone) bypassed the hand-only cost lookup
    # and was cast without payability every turn.
    from arenamcp.action_planner import ActionPlanner

    class _NoBackend:
        def complete(self, *a, **k):
            raise AssertionError("no LLM in filter tests")

    p = ActionPlanner(_NoBackend())
    state = {
        "turn": {"turn_number": 10},
        "hand": [],
        "command": [{"name": "Hei Bai, Forest Guardian", "mana_cost": "{3}{G}"}],
        "players": [],
    }
    legal = ["Cast Hei Bai, Forest Guardian", "Pass"]
    out = p._filter_legal_actions_for_planning(state, legal)
    assert "Cast Hei Bai, Forest Guardian" not in out

    # With MTGA's tax-aware [OK], the commander stays on the menu.
    legal_ok = ["Cast Hei Bai, Forest Guardian [OK]", "Pass"]
    out = p._filter_legal_actions_for_planning(state, legal_ok)
    assert "Cast Hei Bai, Forest Guardian [OK]" in out


def test_vendor_payment_respects_planner_reservation_decision():
    state = _go_shintai_state()
    state["stack"][0].update(
        name="Spellbook Vendor",
        oracle_text=(
            "At the beginning of combat on your turn, you may pay {o1}. "
            "When you do, create a Sorcerer Role token attached to target creature you control."
        ),
    )
    assert "LLM chose decline" in _engine(_DeclinePlanner())._should_decline_optional_cost(state)
    assert _engine(_PayPlanner())._should_decline_optional_cost(state) is None


def test_free_beneficial_optional_trigger_does_not_add_payment_call():
    state = _go_shintai_state()
    state["stack"][0]["oracle_text"] = "You may draw a card."
    assert _engine(_DeclinePlanner())._should_decline_optional_cost(state) is None


def test_optional_payment_prompt_includes_same_turn_reservation():
    from unittest.mock import Mock

    from arenamcp.action_planner import ActionPlanner

    planner = ActionPlanner(Mock())
    state = _go_shintai_state()
    planner._memory().note(state, "ActionsAvailable", "Pass", "Hold W for Rescue.")
    planner._call_llm = Mock(return_value='{"pay": false, "reasoning": "Keep Rescue available"}')
    assert planner.plan_pay_or_decline("Spellbook Vendor", "You may pay {1}.", state) is False
    system, prompt = planner._call_llm.call_args.args[:2]
    assert "Hold W for Rescue" in prompt
    assert "mana reserved for protection" in system


def test_fresh_unrelated_cast_does_not_bypass_optional_payment_review():
    eng = _engine(_DeclinePlanner())
    eng._last_cast_submitted = (14, "rampant growth")
    eng._last_cast_submitted_ts = time.monotonic()
    state = _go_shintai_state()
    assert "LLM chose decline" in eng._should_decline_optional_cost(state)
    state["stack"][0].update(
        name="Spellbook Vendor", oracle_text="You may pay {1}. If you do, create a Role token."
    )
    assert "LLM chose decline" in eng._should_decline_optional_cost(state)


def test_typed_recent_play_checks_id_resolved_payment_source():
    eng = _engine(_DeclinePlanner())
    eng._recent_own_play_name = lambda state: "Rampant Growth"
    assert eng._paying_for_own_play(_go_shintai_state()) is False


def test_unresolved_explicit_source_does_not_inherit_recent_cast():
    eng = _engine()
    eng._last_cast_submitted = (14, "rampant growth")
    eng._last_cast_submitted_ts = time.monotonic()
    state = _go_shintai_state()
    state["decision_context"]["sourceId"] = 12345
    assert eng._paying_for_own_play(state) is False


def test_payment_on_later_turn_does_not_inherit_legacy_cast():
    eng = _engine()
    eng._last_cast_submitted = (13, "go-shintai of hidden cruelty")
    eng._last_cast_submitted_ts = time.monotonic()
    assert eng._paying_for_own_play(_go_shintai_state()) is False
