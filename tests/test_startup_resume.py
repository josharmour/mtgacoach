"""Preparation is visible; engine reload preserves strategy without re-planning."""

import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp.game_plan import GamePlanManager
from arenamcp.pipe_adapter import PipeAdapter
from arenamcp.standalone_startup import _StartupMixin
from test_deck_strategy import deck_case, playbook_for


def state(match="match-one", turn=4):
    result = deck_case()[0]
    result["match_id"] = match
    result["turn"]["turn_number"] = turn
    return result


class Runtime(_StartupMixin):
    def __init__(self):
        self.ui = Mock()
        self._autopilot = Mock()
        self._autopilot_enabled = True
        self._running = True
        self.manager = GamePlanManager(Mock())
        self._coach = SimpleNamespace(
            _deck_strategy=playbook_for().render(),
            _deck_playbook=playbook_for(),
            _game_plan_mgr=self.manager,
            _ensure_game_plan_mgr=lambda: self.manager,
        )
        self._deck_analyzed = False

    def _autopilot_control_status(self):
        return "AP:PAUSED"

    @staticmethod
    def _normalize_turn_snapshot(snapshot):
        return snapshot


@pytest.fixture
def resume_path(tmp_path, monkeypatch):
    path = tmp_path / "engine_resume.json"
    monkeypatch.setattr("arenamcp.standalone_startup.ENGINE_RESUME_PATH", path)
    monkeypatch.setenv("ARENAMCP_ENGINE_RELOAD", "1")
    return path


def test_reload_checkpoints_then_exits_and_restores_strategy_without_model_call(resume_path, monkeypatch):
    from arenamcp import server
    from arenamcp.game_plan import GamePlan

    runtime = Runtime()
    runtime.manager._plan = GamePlan(win_conditions=["Engine advantage"], turn_formed=4)
    runtime.manager._last_sig = runtime.manager._signature(state())
    runtime.manager.seed(runtime._coach._deck_strategy)
    runtime.manager._last_seed = runtime._coach._deck_strategy
    runtime.manager._last_reform_turn = 4
    game = SimpleNamespace(get_published_snapshot=lambda: state())
    monkeypatch.setattr(server, "game_state", game)
    monkeypatch.setattr(server, "watcher", SimpleNamespace(file_position=25, log_path="Player.log"))
    save = Mock(side_effect=lambda *a, **k: runtime._autopilot.on_abort.assert_called_once())
    monkeypatch.setattr("arenamcp.gamestate_persistence.save_match_state", save)
    runtime.prepare_engine_reload()
    assert not runtime._running and not runtime._autopilot_enabled
    assert resume_path.exists()
    runtime.ui._emit.assert_called_with({"type": "reload_ready"})
    save.assert_called_once_with(game, log_offset=25, log_path="Player.log")

    resumed = Runtime()
    resumed._coach._deck_strategy = None
    resumed._load_engine_resume()
    assert not resumed._autopilot_enabled  # paused before reload stays inactive
    assert not resume_path.exists()  # handoff consumed once
    resumed._try_restore_engine_resume(state())
    assert resumed._deck_analyzed
    assert resumed._coach._deck_strategy == runtime._coach._deck_strategy
    assert resumed.manager.current.win_conditions == ["Engine advantage"]
    assert resumed.manager._last_sig == runtime.manager._last_sig
    assert not resumed.manager.request_reform(state())
    resumed.manager._backend.complete.assert_not_called()


@pytest.mark.parametrize("change", ["expired", "different_match", "earlier_turn", "game_over"])
def test_resume_never_reuses_other_or_expired_game_strategy(resume_path, change):
    resume_path.write_text(
        json.dumps(
            {
                "saved_at": time.time() - (121 if change == "expired" else 0),
                "match_id": "match-one",
                "turn": 4,
                "deck_strategy": "OLD STRATEGY",
            }
        )
    )
    runtime = Runtime()
    runtime._coach._deck_strategy = None
    runtime._load_engine_resume()
    current = state(
        match="match-two" if change == "different_match" else "match-one",
        turn=1 if change == "earlier_turn" else 4,
    )
    if change == "game_over":
        current["game_over"] = True
    runtime._try_restore_engine_resume(current)
    assert runtime._coach._deck_strategy is None
    assert not runtime._deck_analyzed


def test_non_reload_start_does_not_consume_strategy_handoff(resume_path, monkeypatch):
    monkeypatch.delenv("ARENAMCP_ENGINE_RELOAD")
    resume_path.write_text("{}")
    runtime = Runtime()
    runtime._load_engine_resume()
    assert runtime._engine_resume is None
    assert resume_path.exists()


def test_startup_events_include_elapsed_and_failures_are_not_ready():
    runtime = Runtime()
    runtime._startup_started = time.monotonic() - 5
    runtime._startup_status("initializing_client", "Preparing connection…")
    payload = runtime.ui.emit_startup_status.call_args.args[0]
    assert payload["phase"] == "initializing_client" and not payload["ready"]
    assert payload["elapsed_s"] >= 5
    runtime._startup_connection_error = "unavailable"
    runtime._startup_complete()
    assert runtime.ui.emit_startup_status.call_args.args[0]["phase"] == "error"
    runtime._startup_connection_error = ""
    runtime._startup_complete()
    assert runtime.ui.emit_startup_status.call_args.args[0]["ready"]


def test_pipe_protocol_for_startup_and_reload():
    adapter = PipeAdapter.__new__(PipeAdapter)
    adapter._emit = Mock()
    adapter._coach = Mock()
    adapter.emit_startup_status({"phase": "initializing_client", "ready": False})
    assert adapter._emit.call_args.args[0]["type"] == "startup_status"
    adapter._dispatch({"cmd": "prepare_engine_reload"})
    adapter._coach.prepare_engine_reload.assert_called_once()


def test_inconclusive_model_list_check_keeps_coaching_enabled():
    runtime = Runtime()
    runtime._startup_connection_warning = "Model list rejected; inference is not yet tested"
    runtime._startup_complete()
    payload = runtime.ui.emit_startup_status.call_args.args[0]
    assert payload["phase"] == "connection_warning"
    assert payload["ready"] is True


def test_legacy_unstructured_summary_is_reanalyzed_after_reload(resume_path):
    resume_path.write_text(
        json.dumps(
            {
                "saved_at": time.time(),
                "match_id": "match-one",
                "turn": 4,
                "deck_strategy": "Generic old ramp summary without commander analysis.",
            }
        )
    )
    runtime = Runtime()
    runtime._coach._deck_strategy = None
    runtime._coach._deck_playbook = None
    runtime._load_engine_resume()
    runtime._try_restore_engine_resume(state())
    assert not runtime._deck_analyzed
    assert runtime._coach._deck_strategy is None
    assert runtime._coach._deck_playbook is None
