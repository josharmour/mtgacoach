"""Idle Grind integration, with all bridge and desktop side effects mocked."""

import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp import server
from arenamcp.auto_queue import AutoQueueNavigator
from arenamcp.standalone_auto_queue import _AutoQueueMixin

TARGET = {"quest_mode": True, "event_id": "Play", "deck_id": "owned-deck", "deck_name": "Owned Deck"}


class Runtime(_AutoQueueMixin):
    def __init__(self):
        self.settings = Mock()
        self.ui = Mock()
        self._coach = SimpleNamespace(_backend=Mock())
        self._mcp = Mock()
        self._auto_queue_enabled = True
        self._auto_queue_since = time.time()
        self.ap_status = "AP:ON"
        self._auto_quest = {
            "idle_ready": True,
            "target": dict(TARGET),
            "deadline": time.time() + 600,
            "valid_until": time.time() + 600,
        }
        self._check_grind_availability = Mock(return_value=True)
        self._prepare_auto_quest = Mock(return_value=True)

    def _autopilot_control_status(self):
        return self.ap_status


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setattr(server, "get_completed_match_for_navigation", lambda: {})
    monkeypatch.setattr("arenamcp.gre_bridge.get_bridge", Mock(return_value=Mock()))
    monkeypatch.setattr("arenamcp.idle_sleep.SleepInhibitor", Mock())
    # Exercise actual navigation state without starting any desktop workers.
    monkeypatch.setattr(AutoQueueNavigator, "process_tick", lambda self, state: self.active)
    return Runtime()


def test_explicit_grind_and_autoplay_arm_once_from_idle(runtime):
    assert runtime._poll_auto_queue({})
    nav = runtime._auto_queue_navigator
    assert nav.active and nav._stage == "home"
    assert nav._expected_queue == {"event_id": "Play", "deck_name": "Owned Deck"}
    assert nav._quest_deck_id == "owned-deck"
    assert not nav._seen_ends
    assert nav._ended_match_id is None
    generation = nav._generation
    assert runtime._poll_auto_queue({})
    assert nav._generation == generation
    assert runtime._auto_quest["idle_started"] is True
    assert runtime.ap_status == "AP:ON"


@pytest.mark.parametrize("status", ["AP:OFF", "AP:PAUSED"])
def test_idle_never_enables_autoplay(runtime, status):
    runtime.ap_status = status
    assert not runtime._poll_auto_queue({})
    runtime._prepare_auto_quest.assert_not_called()
    assert not hasattr(runtime, "_auto_queue_navigator")
    assert runtime.ap_status == status


@pytest.mark.parametrize("flag", ["_autopilot_dry_run", "_autopilot_afk"])
def test_idle_requires_live_autoplay(runtime, flag):
    setattr(runtime, flag, True)
    assert not runtime._poll_auto_queue({})
    runtime._prepare_auto_quest.assert_not_called()


def test_active_draft_owns_navigation(runtime):
    runtime._draft_event_driver = SimpleNamespace(
        enabled=True, run=SimpleNamespace(event_name="Draft", finished=False)
    )
    assert not runtime._poll_auto_queue({})
    runtime._prepare_auto_quest.assert_not_called()


@pytest.mark.parametrize("state", [{"match_id": "live"}, {"pending_decision": {"type": "mulligan"}}])
def test_unresolved_match_or_decision_prevents_idle_arm(runtime, state):
    assert not runtime._poll_auto_queue(state)
    assert not hasattr(runtime, "_auto_queue_navigator")


def test_stale_completed_match_can_be_idle_baseline(runtime, monkeypatch):
    monkeypatch.setattr(server, "get_completed_match_for_navigation", lambda: {"match_id": "old"})
    assert runtime._poll_auto_queue({"match_id": "old"})
    nav = runtime._auto_queue_navigator
    assert not nav._new_match({"match_id": "old"})
    assert nav._new_match({"match_id": "new"})
    assert not nav._seen_ends
    assert nav.export_for_reload() is None


@pytest.mark.parametrize("field", ["deadline", "valid_until"])
def test_expired_grind_target_is_not_armed(runtime, field):
    runtime._auto_quest[field] = time.time() - 1
    assert not runtime._poll_auto_queue({})
    assert not hasattr(runtime, "_auto_queue_navigator")


def test_preparation_failure_does_not_navigate(runtime):
    runtime._prepare_auto_quest.return_value = False
    assert not runtime._poll_auto_queue({})
    assert not hasattr(runtime, "_auto_queue_navigator")


@pytest.mark.parametrize("cancel", ["autoplay", "queue", "grind"])
def test_controls_are_rechecked_after_blocking_preparation(runtime, cancel):
    def prepare(state):
        if cancel == "autoplay":
            runtime.ap_status = "AP:OFF"
        elif cancel == "queue":
            runtime._auto_queue_enabled = False
        else:
            runtime._auto_quest = None
        return True

    runtime._prepare_auto_quest.side_effect = prepare
    assert not runtime._poll_auto_queue({})
    assert not hasattr(runtime, "_auto_queue_navigator")


def test_ordinary_auto_queue_does_not_start_initial_match(runtime):
    runtime._auto_quest = None
    assert not runtime._poll_auto_queue({})
    assert not runtime._auto_queue_navigator.active


def test_new_grind_replaces_previous_queue_target(runtime):
    old = AutoQueueNavigator(backend=Mock(), get_game_state=lambda: {})
    old.set_enabled(True)
    old.note_match_end({"match_id": "previous", "deck_name": "Previous Deck"}, confirmed_match_end=True)
    old.set_enabled(False)
    runtime._auto_queue_navigator = old
    assert runtime._poll_auto_queue({})
    assert runtime._auto_queue_navigator is not old
    assert not old.active
    assert runtime._auto_queue_navigator._quest_deck_id == TARGET["deck_id"]


def test_stopping_grind_discards_idle_navigation(runtime):
    assert runtime._poll_auto_queue({})
    nav = runtime._auto_queue_navigator
    runtime.set_auto_queue(False)
    assert not nav.active
    assert runtime._auto_queue_navigator is None
    runtime.set_auto_queue(True)
    assert not runtime._poll_auto_queue({})


def navigator(state=None):
    return AutoQueueNavigator(backend=Mock(), get_game_state=lambda: state or {})


@pytest.mark.parametrize(
    "change",
    [{"quest_mode": False}, {"event_id": "PremierDraft"}, {"deck_id": ""}, {"deck_name": ""}],
)
def test_navigator_rejects_unverified_or_paid_target(change):
    nav = navigator()
    nav.set_enabled(True)
    assert not nav.start_idle_grind({**TARGET, **change}, {})
    assert not nav.active


def test_idle_arm_requires_enabled_navigation():
    nav = navigator()
    assert not nav.start_idle_grind(TARGET, {})
    nav.set_enabled(True)
    assert nav.start_idle_grind(TARGET, {})
    assert not nav.start_idle_grind(TARGET, {})


def test_bridge_queues_exact_target_once_and_hands_off():
    nav = navigator()
    bridge = Mock()
    bridge.queue_match.return_value = True
    nav.set_enabled(True)
    assert nav.start_idle_grind(TARGET, {})
    nav._step_bridge(nav._generation, nav._abort, bridge)
    nav._step_bridge(nav._generation, nav._abort, bridge)
    bridge.queue_match.assert_called_once_with(event="Play", deck_id="owned-deck")
    bridge.return_to_home.assert_not_called()
    assert nav.queue_joined
    assert not nav.process_tick({"match_id": "first-match"})
    assert not nav.active
    assert nav.export_for_reload() is None


@pytest.mark.parametrize("joined", [False, True])
def test_autoplay_pause_and_resume_do_not_duplicate_queue(joined):
    nav = navigator()
    bridge = Mock()
    nav.set_enabled(True)
    assert nav.start_idle_grind(TARGET, {})
    if joined:
        nav._step_bridge(nav._generation, nav._abort, bridge)
    nav.set_enabled(False)
    assert not nav.active
    assert nav.export_for_reload() is None
    nav.set_enabled(True)
    assert nav.active
    assert nav._stage == ("queue" if joined else "home")
    nav._step_bridge(nav._generation, nav._abort, bridge)
    bridge.queue_match.assert_called_once_with(event="Play", deck_id="owned-deck")


def test_match_start_during_autoplay_pause_cancels_idle_resume():
    state = {}
    nav = AutoQueueNavigator(backend=Mock(), get_game_state=lambda: state)
    nav.set_enabled(True)
    assert nav.start_idle_grind(TARGET, {})
    nav.set_enabled(False)
    state["match_id"] = "first-match"
    nav.set_enabled(True)
    assert not nav.active


def test_completed_match_replaces_idle_arm_normally():
    nav = navigator()
    nav.set_enabled(True)
    assert nav.start_idle_grind(TARGET, {})
    assert not nav.process_tick({"match_id": "first-match"})
    assert nav.note_match_end({"match_id": "first-match", **TARGET}, confirmed_match_end=True)
    assert nav._stage == "results"
    assert not nav._idle_grind
    assert list(nav._seen_ends) == ["first-match"]


@pytest.mark.parametrize("home", [True, False, None])
def test_real_mixin_idle_contract_requires_verified_home(runtime, monkeypatch, home):
    from arenamcp.auto_quest import QuestSnapshot
    from arenamcp.standalone_auto_quest import AutoQuestMixin

    now = time.time()
    run = runtime._auto_quest
    run.update(goal="mastery", started=now - 1, observed=None, prepared=None, next_poll=0, home=None)
    run.pop("idle_ready")
    run.pop("target")
    bridge = Mock()
    bridge.draft_command.return_value = {"ok": home is not None, "home": home}
    runtime._deck_library_bridge = Mock(return_value=bridge)
    runtime._quest_actions_allowed = Mock(return_value=True)
    runtime._prepare_auto_quest = AutoQuestMixin._prepare_auto_quest.__get__(runtime)
    monkeypatch.setattr(server, "_log_path", lambda: "unused-mocked-log")
    monkeypatch.setattr(
        "arenamcp.standalone_auto_quest.read_snapshot",
        lambda path: QuestSnapshot(quests=[], observed_at=now, rewards={}),
    )
    monkeypatch.setattr("arenamcp.standalone_auto_quest.quest_descriptions", lambda: {})
    planner = Mock(
        return_value=SimpleNamespace(
            status="ready",
            detail="Verified target",
            coverage_gaps=[],
            deck_id=TARGET["deck_id"],
            deck_name=TARGET["deck_name"],
            event_id=TARGET["event_id"],
        )
    )
    monkeypatch.setattr("arenamcp.standalone_auto_quest.plan", planner)
    assert runtime._poll_auto_queue({}) is (home is True)
    if home is True:
        assert run["idle_started"] is True
        assert runtime._poll_auto_queue({})
        assert planner.call_count == 1
        assert runtime._auto_queue_navigator._quest_deck_id == TARGET["deck_id"]
    else:
        planner.assert_not_called()
        assert not hasattr(runtime, "_auto_queue_navigator")
    bridge.draft_command.assert_called_once_with("get_screen", timeout=5.0)
    bridge.return_to_home.assert_not_called()
