"""Traditional sideboarding has an explicit owner, never a generic game action."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests.draft_fakes import FakeBridge as DraftBridge
from tests.draft_fakes import driver_for, make_primer
from tests.fakes import FakeBridge, make_engine
from tests.mac_bridge_fakes import FakeGame, adapter_for, enum
from tests.test_draft_autoplay import deck_world

from arenamcp.autopilot_models import AutopilotState
from arenamcp.gre_bridge import enrich_snapshot_from_pending_response
from arenamcp.mac_bridge_adapter import MacBridgeAdapter
from arenamcp.sideboarding import SIDEBOARD_WAIT_MESSAGE, is_sideboarding
from arenamcp.standalone_draft_event import _DraftEventMixin


@pytest.mark.parametrize("request_type", ["SubmitDeck", "SubmitDeckReq", "", "SubmitDeckRequest"])
def test_native_submit_deck_is_sideboard_not_mulligan_or_unknown(request_type):
    game = FakeGame(
        {"$c": "GreClient.Rules.SubmitDeckRequest", "$h": 5},
        {"Type": enum(request_type, 0)} if request_type else {},
    )
    poll = adapter_for(game).handle({"action": "get_pending_actions"})
    assert poll["decision_context"]["type"] == "sideboard"
    state = {
        "turn": {"turn_number": 21},
        "pending_decision": "Mulligan",
        "decision_context": {"type": "mulligan", "mulligan_count": 1},
        "legal_actions": ["Keep", "Mulligan"],
        "legal_actions_raw": [{"actionType": "Mulligan"}],
    }
    enrich_snapshot_from_pending_response(state, poll, bridge_connected=True)
    assert state["pending_decision"] == "Sideboard"
    assert state["decision_context"]["type"] == "sideboard"
    assert "mulligan_count" not in state["decision_context"]
    assert state["turn"]["turn_number"] == 21
    assert state["legal_actions"] == state["legal_actions_raw"] == []


@pytest.mark.parametrize("action", ["auto_respond", "cancel_action"])
@pytest.mark.parametrize("can_cancel", [True, False])
def test_native_generic_escape_never_submits_or_cancels_a_deck(action, can_cancel):
    game = FakeGame(
        {"$c": "GreClient.Rules.SubmitDeckRequest", "$h": 5},
        {"Type": enum("SubmitDeck", 0), "CanCancel": can_cancel},
    )
    reply = adapter_for(game).handle({"action": action})
    assert not reply["ok"] and "explicit deck submission" in reply["error"]
    assert not game.submits()


def test_pool_is_readable_between_games_but_initial_deck_mutations_are_blocked():
    world = deck_world({1: 23, 7001: 17}, {2: 3})
    world.objects[20]["IsSideboarding"] = True
    adapter = MacBridgeAdapter(world.send)
    screen = adapter.handle({"action": "get_screen"})
    assert screen["sideboarding"] and screen["deck_builder"]
    pool = adapter.handle({"action": "get_limited_pool", "basic_candidates": [7001]})
    assert pool["ok"] and pool["is_sideboarding"]
    assert pool["main_deck"] == [{"grp_id": 1, "count": 23}, {"grp_id": 7001, "count": 17}]
    assert pool["sideboard"] == [{"grp_id": 2, "count": 3}]
    for command in (
        {"action": "set_limited_deck", "main_deck": [{"grp_id": 1, "count": 40}]},
        {"action": "submit_limited_deck"},
    ):
        assert not adapter.handle(command)["ok"]
    assert not any(
        method in {"AddCardToMainDeck", "RemoveCardFromMainDeck", "DoneButton_OnClick"}
        for _, method, _ in world.calls
    )


@pytest.mark.parametrize("action", ["set_limited_deck", "submit_limited_deck"])
def test_initial_deck_command_rechecks_sideboard_mode_in_mutation_frame(action):
    world = deck_world({1: 40}, {2: 3})

    def send(command, timeout):
        if any(op.get("method") in {"RemoveCardFromMainDeck", "DoneButton_OnClick"} for op in command["ops"]):
            world.objects[20]["IsSideboarding"] = True
        return world.send(command, timeout)

    result = MacBridgeAdapter(send).handle({"action": action, "main_deck": [{"grp_id": 2, "count": 40}]})
    assert not result["ok"] and "IsSideboarding" in result["error"]
    assert world.state["main"] == {1: 40}
    assert not any(method == "DoneButton_OnClick" for _, method, _ in world.calls)


def test_driver_holds_sideboard_once_then_releases_for_next_game():
    bridge = DraftBridge({"get_screen": {"ok": True, "sideboarding": True, "deck_builder": True}})
    in_match = [False]
    notices = []
    driver = driver_for(bridge, make_primer(), in_match_fn=lambda: in_match[0], status_fn=notices.append)
    driver.run.deck_plan = {"previous": "build"}
    driver.run.deck_attempts = 2  # This game's sideboard is not a failed initial build.
    driver._step()
    driver._step()
    assert driver.owns_ui and not driver.paused_reason
    assert sum("Sideboarding needs manual review" in notice for notice in notices) == 1
    assert [action for action, _ in bridge.sent] == ["get_screen", "get_sideboard_state"] * 2
    assert driver.run.deck_plan == {"previous": "build"}
    in_match[0] = True
    driver._step()
    assert not driver.owns_ui and driver.enabled and not driver.paused_reason


def test_driver_catches_sideboard_opening_after_screen_read():
    bridge = DraftBridge(
        {
            "get_screen": {"ok": True, "deck_builder": True},
            "get_limited_pool": {"ok": True, "is_sideboarding": True, "main_deck": [], "sideboard": []},
        }
    )
    driver = driver_for(bridge, make_primer())
    driver._basics = {}
    driver.run.deck_attempts = 2
    driver._step()
    assert driver.owns_ui and not driver.paused_reason
    assert driver.run.pool_waits == 0 and driver.run.deck_attempts == 2
    assert [action for action, _ in bridge.sent] == ["get_screen", "get_limited_pool", "get_sideboard_state"]


def test_autopilot_waits_without_planning_escaping_or_permanent_pause_then_handles_mulligan():
    poll = {"ok": True, "has_pending": True, "request_type": "SubmitDeck"}
    bridge = FakeBridge(poll)
    bridge.auto_respond = Mock(return_value=True)
    bridge.submit_mulligan = Mock(return_value=True)
    notices = []
    state = {"turn": {"turn_number": 21}, "hand_complete": False}
    enrich_snapshot_from_pending_response(state, poll, bridge_connected=True)
    engine = make_engine(
        bridge, game_state=lambda: dict(state), ui_advice_fn=lambda text, label: notices.append(text)
    )
    for _ in range(2):
        assert engine.process_trigger(state, "decision_required")
    assert notices == [SIDEBOARD_WAIT_MESSAGE]
    assert engine.state == AutopilotState.IDLE
    assert engine._planner.plan_calls == 0
    assert not engine._try_auto_respond_escape(state, "watchdog")
    assert not engine._try_interactive_safe_default({}, "watchdog")
    bridge.auto_respond.assert_not_called()
    bridge.pending_response = {"ok": True, "has_pending": True, "request_type": "Mulligan"}
    state["hand_complete"] = True
    state["turn"] = {"turn_number": 0}
    enrich_snapshot_from_pending_response(state, bridge.pending_response, bridge_connected=True)
    engine._planner.plan_decision_options = Mock(return_value=["mull:keep"])
    assert engine.process_trigger(state, "decision_required")
    bridge.submit_mulligan.assert_called_once_with(True)
    assert not engine._sideboard_waiting


def test_sideboard_log_state_cannot_override_live_mulligan_or_idle_bridge():
    stale = {"pending_decision": "Sideboard", "decision_context": {"type": "sideboard"}}
    assert is_sideboarding(stale)
    assert not is_sideboarding({**stale, "_bridge_request_type": "Mulligan"})
    assert not is_sideboarding({**stale, "_bridge_connected": True, "_bridge_has_pending": False})


def test_live_deck_request_replaces_queued_gameplay_before_planning():
    bridge = FakeBridge({"ok": True, "has_pending": True, "request_type": "SubmitDeck"})
    state = {"_bridge_request_type": "ActionsAvailable", "decision_context": {"type": "actions_available"}}
    engine = make_engine(bridge, game_state=state)
    assert engine._try_typed_decision_path(state, "priority_gained")
    assert engine._planner.plan_calls == 0
    assert engine._sideboard_waiting


def test_preserved_previous_turn_does_not_hide_sideboard_from_event_driver(monkeypatch):
    from arenamcp import server

    monkeypatch.setattr(server, "game_state", SimpleNamespace(pending_decision="Sideboard"))
    monkeypatch.setattr(server, "_match_in_progress", lambda: True)
    assert not _DraftEventMixin()._draft_event_in_match()
    server.game_state.pending_decision = "Mulligan"
    assert _DraftEventMixin()._draft_event_in_match()
