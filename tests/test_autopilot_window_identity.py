"""R1: decision-window identity + one legal-action list for prompt/validator.

2026-07-05 evidence: 21 stale discards keyed on the log-lagged turn counter
(Arcane Signet discarded as "turn advanced 5→6" while its 13-action window
stayed open), and the prompt's Legal: line advertising Cast Silkguard [OK]
while the validator had X-cost-stripped it (6 propose→drop cycles).
"""

from unittest.mock import MagicMock

import pytest
from tests.commander_return_states import (
    ASSIGN_DAMAGE_POLL,
    COMMANDER_RETURN_POLL,
    assign_damage_state,
    commander_return_state,
)
from tests.fakes import FakePlanner, make_engine

import arenamcp.autopilot as autopilot_module
from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine, AutopilotState


class _DummyBridge:
    connected = False

    def connect(self):
        return False


def _engine(monkeypatch) -> AutopilotEngine:
    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: _DummyBridge())
    return AutopilotEngine(
        planner=None,
        get_game_state=lambda: {},
        config=AutopilotConfig(dry_run=True),
    )


def test_normalize_request_type():
    n = AutopilotEngine._normalize_request_type
    assert n("ActionsAvailableRequest") == "ActionsAvailable"
    assert n("PayCostsReq") == "PayCosts"
    assert n("SelectTargets") == "SelectTargets"
    assert n(None) == ""


def test_snapshot_identity_from_bridge_fields(monkeypatch):
    eng = _engine(monkeypatch)
    state = {
        "_bridge_game_state_id": 4711,
        "_bridge_request_type": "ActionsAvailable",
        "_bridge_actions": [{}, {}, {}],
    }
    assert eng._snapshot_window_identity(state) == (4711, "ActionsAvailable", 3)
    # No bridge data → no identity (fall back to turn/phase checks)
    assert eng._snapshot_window_identity({}) is None


def test_identity_match_rules(monkeypatch):
    m = AutopilotEngine._window_identities_match
    a = (4711, "ActionsAvailable", 13)
    assert m(a, (4711, "ActionsAvailable", 13)) is True
    # unknown action count is a wildcard
    assert m(a, (4711, "ActionsAvailable", -1)) is True
    assert m((4711, "ActionsAvailable", -1), a) is True
    # different window
    assert m(a, (4712, "ActionsAvailable", 13)) is False
    assert m(a, (4711, "SelectTargets", 13)) is False
    assert m(a, (4711, "ActionsAvailable", 12)) is False
    # unknown on either side is never a match
    assert m(None, a) is False
    assert m(a, None) is False


class _NoBackend:
    def complete(self, *a, **k):
        raise AssertionError("prompt build must not call the LLM")


def _planner() -> ActionPlanner:
    return ActionPlanner(_NoBackend())


def _tiny_state():
    return {
        "turn": {"turn_number": 8, "phase": "Main1"},
        # Raw summarized list still contains the X-cost spell...
        "legal_actions": [
            "Cast Silkguard [OK]",
            "Cast The Spirit Oasis [OK]",
            "Pass",
        ],
        "battlefield": [],
        "hand": [],
    }


def test_prompt_legal_menu_uses_effective_list():
    p = _planner()
    # ...but the planner's effective list has stripped it.
    effective = ["Cast The Spirit Oasis [OK]", "Action: Activate_Mana", "Pass"]
    prompt = p._build_action_prompt(_tiny_state(), "decision_required", effective)
    assert "Legal: (pick by number)" in prompt
    menu_part = prompt.split("Legal:", 1)[1].split("EXCLUDED", 1)[0]
    assert "1. Cast The Spirit Oasis [OK]" in menu_part
    # Silkguard was filtered out of the effective list → not in the menu
    assert "Silkguard" not in menu_part
    # Mana activations never appear in the menu (auto-paid by the engine)
    assert "Activate_Mana" not in menu_part
    # The menu is what picks resolve against
    assert p._last_menu == ["Cast The Spirit Oasis [OK]", "Pass"]
    # The stripped entry is named as excluded so the model stops proposing it
    assert "EXCLUDED" in prompt
    assert "Silkguard" in prompt.split("EXCLUDED", 1)[1]


def test_prompt_legal_line_absent_when_no_effective_list():
    p = _planner()
    prompt = p._build_action_prompt(_tiny_state(), "decision_required", None)
    assert "EXCLUDED" not in prompt


class _PollBridge:
    connected = True

    def __init__(self, poll):
        self.poll = poll

    def connect(self):
        return True

    def get_pending_actions(self):
        return self.poll


def test_live_pending_request_verification(monkeypatch):
    # P1-5: a consumed PayCosts window must be detected before blind cancel.
    eng = _engine(monkeypatch)

    eng._gre_bridge = _PollBridge({"has_pending": False})
    assert eng._live_pending_request_is("PayCosts") is False

    eng._gre_bridge = _PollBridge({"has_pending": True, "request_class": "PayCostsRequest"})
    assert eng._live_pending_request_is("PayCosts") is True

    eng._gre_bridge = _PollBridge({"has_pending": True, "request_type": "SelectTargets"})
    assert eng._live_pending_request_is("PayCosts") is False

    class _DeadBridge:
        connected = False

        def connect(self):
            return False

    eng._gre_bridge = _DeadBridge()
    assert eng._live_pending_request_is("PayCosts") is None


def test_reusable_advice_same_window_only(monkeypatch):
    # P2-3: coach fall-through reuses the plan advice for the SAME window.
    import time as _time

    eng = _engine(monkeypatch)
    state = {
        "_bridge_game_state_id": 42,
        "_bridge_request_type": "ActionsAvailable",
        "pending_decision": "Action Required",
        "turn": {"turn_number": 7, "phase": "Main1", "step": ""},
    }
    eng._last_plan_advice = (eng._priority_window_signature(state), "Cast the thing.", _time.time())
    assert eng.get_reusable_advice(state) == "Cast the thing."

    moved = dict(state, _bridge_game_state_id=43)
    assert eng.get_reusable_advice(moved) is None

    eng._last_plan_advice = (eng._priority_window_signature(state), "Cast the thing.", _time.time() - 30)
    assert eng.get_reusable_advice(state) is None


# --- the Mac bridge publishes no board id: the live request stands in -------


def test_plan_identity_prefers_the_snapshot_board_id(monkeypatch):
    eng = _engine(monkeypatch)
    eng._gre_bridge = _PollBridge(COMMANDER_RETURN_POLL)
    state = {"_bridge_game_state_id": 338, "_bridge_request_type": "AssignDamage"}
    assert eng._plan_window_identity(state) == (338, "AssignDamage", -1)


def test_plan_identity_without_a_board_id_is_the_live_request_of_the_same_family(monkeypatch):
    eng = _engine(monkeypatch)
    eng._gre_bridge = _PollBridge(ASSIGN_DAMAGE_POLL)
    assert eng._plan_window_identity(assign_damage_state()) == (338, "AssignDamage", -1)
    # The live request is already another family: no identity for this snapshot.
    eng._gre_bridge = _PollBridge(COMMANDER_RETURN_POLL)
    assert eng._plan_window_identity(assign_damage_state()) is None
    # No bridge request in the snapshot: nothing to identify.
    assert eng._plan_window_identity({}) is None


def test_a_mac_plan_for_a_replaced_request_is_discarded_and_yields():
    """18:18:21-26 without the mid-call cancel: the post-planning check catches it.

    The Mac snapshot has no _bridge_game_state_id, so this check used to fall
    back to the turn/phase counters (all unchanged) and the plan went on to
    MANUAL REQUIRED against the OptionalAction.
    """
    bridge = MagicMock()
    bridge.connected = True
    bridge.get_pending_actions.return_value = ASSIGN_DAMAGE_POLL

    class _Planner(FakePlanner):
        def plan_actions(self, *args, **kwargs):
            self.plan_calls += 1
            bridge.get_pending_actions.return_value = COMMANDER_RETURN_POLL
            return ActionPlan(
                actions=[GameAction(action_type=ActionType.ASSIGN_DAMAGE, card_name="Mossborn Hydra")],
                overall_strategy="All-in race",
            )

    engine = make_engine(bridge, _Planner(), game_state=commander_return_state)
    engine._game_plan_mgr = None
    engine._pause_for_manual = MagicMock()

    assert engine.process_trigger(assign_damage_state(), "stack_spell_yours") is True

    assert engine._planner.plan_calls == 1
    bridge.submit_assign_damage.assert_not_called()
    engine._pause_for_manual.assert_not_called()
    assert engine._state == AutopilotState.IDLE


@pytest.mark.parametrize("during_countdown", [False, True])
def test_expired_plan_with_idle_bridge_yields_without_user_handoff(during_countdown):
    from copy import deepcopy

    bridge = MagicMock()
    bridge.connected = True
    bridge.get_pending_actions.return_value = ASSIGN_DAMAGE_POLL
    initial = assign_damage_state()
    fresh = deepcopy(initial)
    fresh["turn"]["turn_number"] += 1
    live = [initial if during_countdown else fresh]

    class Planner(FakePlanner):
        def plan_actions(self, *args, **kwargs):
            self.plan_calls += 1
            if not during_countdown:
                bridge.get_pending_actions.return_value = {"ok": True, "has_pending": False}
            return ActionPlan(actions=[GameAction(ActionType.ASSIGN_DAMAGE, card_name="Mossborn Hydra")])

    engine = make_engine(bridge, Planner(), game_state=lambda: live[0])
    if during_countdown:
        engine._config.auto_execute_delay = 1.0

        def countdown(_delay):
            bridge.get_pending_actions.return_value = {"ok": True, "has_pending": False}
            live[0] = fresh
            return "execute"

        engine._wait_for_cancel = countdown
    engine._game_plan_mgr = None
    engine._bug_report_fn = MagicMock()
    engine._notify = MagicMock()
    assert engine.process_trigger(initial, "stack_spell_yours") is True
    bridge.submit_assign_damage.assert_not_called()
    assert engine._state == AutopilotState.IDLE
    assert not engine.is_window_given_up(fresh)
    messages = " ".join(str(call) for call in engine._notify.call_args_list)
    assert "your move" not in messages
    assert "Plan discarded" in messages
    assert engine._pending_fallback_bugs[0][1]["auto_user_takeover"]["attribution"] == "unknown"


@pytest.mark.parametrize(
    "replacement", [COMMANDER_RETURN_POLL, {"ok": True, "has_pending": False}, ASSIGN_DAMAGE_POLL]
)
def test_empty_plan_for_expired_request_yields_before_safe_default_or_manual(replacement):
    bridge = MagicMock()
    bridge.connected = True
    bridge.get_pending_actions.return_value = ASSIGN_DAMAGE_POLL

    class Planner(FakePlanner):
        def plan_actions(self, *args, **kwargs):
            self.plan_calls += 1
            bridge.get_pending_actions.return_value = replacement
            return ActionPlan(actions=[])

    engine = make_engine(bridge, Planner(), game_state=assign_damage_state)
    engine._game_plan_mgr = None
    engine._try_interactive_safe_default = MagicMock(return_value=False)
    engine._pause_for_manual = MagicMock()
    handled = engine.process_trigger(assign_damage_state(), "stack_spell_yours")
    if replacement == ASSIGN_DAMAGE_POLL:
        assert handled is False
        engine._try_interactive_safe_default.assert_called_once()
        engine._pause_for_manual.assert_called_once()
        return
    assert handled is True
    engine._try_interactive_safe_default.assert_not_called()
    engine._pause_for_manual.assert_not_called()
    assert engine._state == AutopilotState.IDLE


@pytest.mark.parametrize(
    "after_id,after_gsid,expected",
    [(843, 419, True), (745, 419, False), (843, 418, False), (None, 419, False)],
)
def test_damage_verification_recognizes_next_assigner(monkeypatch, after_id, after_gsid, expected):
    eng = _engine(monkeypatch)
    eng._config.post_action_delay = 0
    eng._config.verification_timeout = 0.001

    def snapshot(iid, gsid):
        return {
            "pending_decision": "Assign Damage",
            "_log_game_state_id": gsid,
            "decision_context": {"type": "assign_damage", "raw": {"damageAssigners": [{"instanceId": iid}]}},
            "turn": {},
            "battlefield": [],
        }

    eng._get_game_state = lambda: snapshot(after_id, after_gsid)
    monkeypatch.setattr(autopilot_module.time, "sleep", lambda _: None)
    assert (
        eng._verify_action(GameAction(action_type=ActionType.ASSIGN_DAMAGE), snapshot(745, 418)) is expected
    )
