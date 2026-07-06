"""P1-8: spurious takeover records get relabeled when autopilot self-recovers.

2026-07-05 22:57: a plan_went_stale_after_llm takeover (Arcane Signet) was
filed 11s before autopilot itself cast the same card on the same window —
turn-counter lag misfiled as a user takeover.
"""

import arenamcp.autopilot as autopilot_module
from arenamcp.action_planner import ActionType, GameAction
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine


class _DummyBridge:
    connected = False

    def connect(self):
        return False


def _engine(monkeypatch) -> AutopilotEngine:
    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: _DummyBridge())
    eng = AutopilotEngine(
        planner=None,
        mapper=None,
        controller=None,
        get_game_state=lambda: {},
        config=AutopilotConfig(dry_run=True),
    )
    eng._bug_report_fn = lambda *a, **k: None
    return eng


class _Plan:
    def __init__(self, action):
        self.actions = [action]


def test_self_recovered_takeover_dropped_from_flush(monkeypatch):
    eng = _engine(monkeypatch)
    eng._last_exec_success_ts = 0.0  # no recent execution
    planned = GameAction(action_type=ActionType.CAST_SPELL, card_name="Arcane Signet")
    eng._record_user_takeover(_Plan(planned), {}, "plan_went_stale_after_llm")
    assert len(eng._pending_fallback_bugs) == 1

    # Autopilot executes the same card moments later → reclassified.
    eng._reclassify_matching_takeovers(planned)
    tag = eng._pending_fallback_bugs[0][1]["auto_user_takeover"]["reason_tag"]
    assert tag == "self_recovered_replan"

    # Flush drops the reclassified record entirely.
    assert eng.flush_fallback_bugs_for_match() == 0


def test_genuine_takeover_survives_flush(monkeypatch):
    eng = _engine(monkeypatch)
    eng._last_exec_success_ts = 0.0
    planned = GameAction(action_type=ActionType.CAST_SPELL, card_name="Arcane Signet")
    eng._record_user_takeover(_Plan(planned), {}, "plan_went_stale_after_llm")

    # A DIFFERENT action executing does not reclassify.
    other = GameAction(action_type=ActionType.CAST_SPELL, card_name="Talisman of Unity")
    eng._reclassify_matching_takeovers(other)
    assert eng.flush_fallback_bugs_for_match() == 1
