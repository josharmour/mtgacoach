"""The sidebar plan card's one emitter (arenamcp.plan_card) and who pushes through it.

2026-10-18 match 02fc3a1d: the card showed "AGGRESSOR, lethal on T14" for 22 s
after the opponent's turn made the board "CONTROL/STABILIZE, dead in 2 turns",
and a finished plan reform painted the snapshot it had started from. The
coaching loop's sync is covered in test_standalone_coaching_loop.py.
"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from tests.strategic_states import G1_T8

from arenamcp import standalone
from arenamcp.autopilot import AutopilotEngine
from arenamcp.game_plan import GamePlanManager
from arenamcp.plan_card import PlanCard


class NoModelBackend:
    def complete(self, *args, **kwargs):
        raise AssertionError("the plan card makes no model call")


class Clock:
    def __init__(self) -> None:
        self.now = 50.0

    def __call__(self) -> float:
        return self.now


def opponent_turn() -> dict:
    """G1_T8 on the opponent's turn 9: they have a clock of 4 on our empty board."""
    state = deepcopy(G1_T8)
    state["turn"].update(turn_number=9, active_player=2, priority_player=2)
    return state


def pumped() -> dict:
    """The same turn after their Cadet became 16/16: dead in 2 unless we stabilize."""
    state = opponent_turn()
    for card in state["battlefield"]:
        if card["instance_id"] == 244:
            card.update(power=16, toughness=16)
    return state


def roles(shown: list[dict]) -> list[str]:
    return [payload["facts"]["role"] for payload in shown]


def capture_reform(manager: GamePlanManager) -> list:
    """Replace the reform's model call: keep its on_updated callback to finish it by hand."""
    callbacks: list = []

    def request_reform(game_state, *, on_updated=None):
        callbacks.append(on_updated)
        return True

    manager.request_reform = request_reform  # type: ignore[method-assign]
    return callbacks


# ---------------------------------------------------------------------------
# PlanCard
# ---------------------------------------------------------------------------


def test_publish_sends_only_what_the_card_does_not_show_yet():
    shown: list[dict] = []
    card = PlanCard(shown.append)
    manager = GamePlanManager(NoModelBackend())

    assert card.publish(manager, opponent_turn(), source="coach")
    assert not card.publish(manager, opponent_turn(), source="coach")
    assert card.publish(manager, pumped(), source="coach")
    assert roles(shown) == ["defender", "control/stabilize"]
    assert shown[0]["source"] == "coach" and shown[0]["outlook"]["pct"] > shown[1]["outlook"]["pct"]


def test_a_running_reforms_seconds_alone_do_not_repaint_the_card():
    shown: list[dict] = []
    card = PlanCard(shown.append)
    status = {"state": "forming", "inflight": True, "inflight_s": 3}
    manager = SimpleNamespace(ui_payload=lambda state: {"plan_status": dict(status)})

    assert card.publish(manager, opponent_turn(), source="autopilot")
    status["inflight_s"] = 4
    assert not card.publish(manager, opponent_turn(), source="autopilot")
    status.update(state="ready", inflight=False, inflight_s=0)
    assert card.publish(manager, opponent_turn(), source="autopilot")
    assert [p["plan_status"]["state"] for p in shown] == ["forming", "ready"]


def test_sync_computes_at_most_once_per_interval():
    shown: list[dict] = []
    clock = Clock()
    card = PlanCard(shown.append, clock=clock, sync_interval_s=0.5)
    manager = GamePlanManager(NoModelBackend())

    assert card.sync(manager, opponent_turn(), source="coach")
    clock.now += 0.3
    assert not card.sync(manager, pumped(), source="coach")
    clock.now += 0.3
    assert card.sync(manager, pumped(), source="coach")
    # A direct publish (decision, advice, reform) is never throttled.
    assert card.publish(manager, opponent_turn(), source="coach")
    assert roles(shown) == ["defender", "control/stabilize", "defender"]


def test_a_failing_payload_or_ui_never_raises():
    def boom(*_args):
        raise RuntimeError("pipe closed")

    card = PlanCard(boom)
    assert not card.publish(GamePlanManager(NoModelBackend()), opponent_turn(), source="coach")
    assert not PlanCard([].append).publish(SimpleNamespace(ui_payload=boom), {}, source="coach")


# ---------------------------------------------------------------------------
# A finished reform paints the board as it is now
# ---------------------------------------------------------------------------


def test_autopilot_reform_completion_shows_the_latest_board():
    manager = GamePlanManager(NoModelBackend())
    callbacks = capture_reform(manager)
    shown: list[dict] = []
    latest = {"state": opponent_turn()}
    engine = AutopilotEngine.__new__(AutopilotEngine)
    engine._game_plan_mgr = manager
    engine._ui_game_plan_fn = shown.append
    engine._planner = SimpleNamespace(set_game_plan=lambda text: None)
    engine._config = SimpleNamespace(afk_mode=False, land_drop_mode=False)
    engine._get_game_state = lambda: deepcopy(latest["state"])

    engine._refresh_game_plan(opponent_turn())
    assert roles(shown) == ["defender"] and len(callbacks) == 1

    # The reform answers after their turn pumped the Cadet.
    latest["state"] = pumped()
    callbacks[0]()
    assert roles(shown) == ["defender", "control/stabilize"]
    assert shown[-1]["source"] == "autopilot"


def test_autopilot_reform_completion_falls_back_to_its_snapshot_without_a_live_one():
    manager = GamePlanManager(NoModelBackend())
    callbacks = capture_reform(manager)
    painted: list = []
    manager.ui_payload = lambda state: painted.append(state) or {"facts": {}}  # type: ignore[method-assign]
    engine = AutopilotEngine.__new__(AutopilotEngine)
    engine._game_plan_mgr = manager
    engine._ui_game_plan_fn = lambda payload: None
    engine._planner = SimpleNamespace(set_game_plan=lambda text: None)
    engine._config = SimpleNamespace(afk_mode=False, land_drop_mode=False)

    def unreadable():
        raise RuntimeError("log not ready")

    engine._get_game_state = unreadable
    snapshot = pumped()
    engine._refresh_game_plan(snapshot)
    callbacks[0]()
    assert len(painted) == 2 and painted[-1] is snapshot


class UI:
    def __init__(self) -> None:
        self.game_plans: list[dict] = []

    def game_plan(self, payload: dict) -> None:
        self.game_plans.append(payload)


def loop_coach(manager: GamePlanManager, latest: dict, *, autopilot: bool) -> standalone.StandaloneCoach:
    coach = standalone.StandaloneCoach.__new__(standalone.StandaloneCoach)
    coach.ui = UI()
    coach.draft_mode = False
    coach._coach = SimpleNamespace(_ensure_game_plan_mgr=lambda: manager, _deck_strategy=None)
    coach._mcp = SimpleNamespace(get_game_state=lambda: deepcopy(latest["state"]))
    coach._normalize_turn_snapshot = lambda state: state  # type: ignore[method-assign]
    coach._autopilot_enabled = autopilot
    coach._autopilot = (
        SimpleNamespace(state=SimpleNamespace(value="idle"), is_window_given_up=lambda state: False)
        if autopilot
        else None
    )
    return coach


@pytest.mark.parametrize("autopilot,source", [(True, "autopilot"), (False, "coach")])
def test_coach_loop_reform_completion_shows_the_latest_board(autopilot, source):
    """The loop's reforms painted nothing while the autopilot drove (all of the
    incident's opponent-turn reforms) and their starting snapshot otherwise."""
    manager = GamePlanManager(NoModelBackend())
    callbacks = capture_reform(manager)
    latest = {"state": opponent_turn()}
    coach = loop_coach(manager, latest, autopilot=autopilot)

    coach._maybe_refresh_game_plan(opponent_turn())
    assert len(callbacks) == 1 and callbacks[0] is not None
    latest["state"] = pumped()
    callbacks[0]()

    assert roles(coach.ui.game_plans) == ["control/stabilize"]
    assert coach.ui.game_plans[0]["source"] == source


def test_the_coach_keeps_one_card_and_none_without_a_ui_hook():
    coach = loop_coach(GamePlanManager(NoModelBackend()), {"state": opponent_turn()}, autopilot=True)
    card = coach._game_plan_card()
    assert card is not None and coach._game_plan_card() is card
    coach.ui = None
    coach._plan_card = None
    assert coach._game_plan_card() is None


# ---------------------------------------------------------------------------
# The desktop repaints every payload, same plan or not
# ---------------------------------------------------------------------------


def test_desktop_details_repaint_intra_turn_facts_and_reject_late_old_assessment(qapp):
    from arenamcp.desktop.compact_coach import CompactCoachPanel
    from arenamcp.plan_card import card_payload

    manager = GamePlanManager(NoModelBackend())
    panel = CompactCoachPanel()
    panel.show()
    try:
        state = opponent_turn()
        old = card_payload(manager, state, source="autopilot")
        panel._on_game_state_changed(state)
        panel._on_game_plan_changed(old)
        panel._show_detail("advice")
        before = panel.detail_pager.browser.toPlainText()
        changed = pumped()
        panel._on_game_state_changed(changed)
        assert panel.strategy_view.scale.value is None
        assert "DEFENDER" not in panel.detail_pager.browser.toPlainText()
        panel._on_game_plan_changed(card_payload(manager, changed, source="autopilot"))
        after = panel.detail_pager.browser.toPlainText()
        assert "DEFENDER" in before and "CONTROL/STABILIZE" in after
        assert "their clock 4" in before and "dead in 2 turns" in after
        assert "DEAD IN 2 TURNS UNLESS WE STABILIZE" in after
        panel._on_game_plan_changed(old)
        assert panel.detail_pager.browser.toPlainText() == after
        assert panel.strategy_view.current
    finally:
        panel.close()
        panel.deleteLater()
