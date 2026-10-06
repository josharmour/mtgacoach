"""Stuck-Mulligan escape and the logged mulligan count (2026-10-06, match 3da54de9 G2).

13:53:37 we mulliganed; the GRE then waited ~15s for the opponent's mulligan.
The macOS bridge reports game_state_id 0, so the window signature never
changed, and the repeat counter (bumped on every state fetch) hit 12. When our
next MulliganReq arrived at 13:53:52 the first trigger "escaped" it with
AutoResp, and the GRE answered GREMessageType_IllegalRequest
(FailureReason_UnexpectedMessage).
"""

from __future__ import annotations

import time

import arenamcp.autopilot as autopilot_module
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine
from arenamcp.coach_postprocess import _mulligan_hand_call
from arenamcp.decisions import build_pending_decision
from arenamcp.gamestate import GameState
from arenamcp.gamestate_decisions import _handle_decision_message
from arenamcp.request_tracker import decision_fingerprint


class _Planner:
    _timeout = 0.1
    _backend = object()

    def get_recent_diagnostics(self):
        return []


class MulliganBridge:
    def __init__(self, game_state_id=4, msg_id=12):
        self.connected = True
        self.pending = {
            "ok": True,
            "has_pending": True,
            "request_type": "Mulligan",
            "request_class": "MulliganRequest",
            "game_state_id": game_state_id,
            "msg_id": msg_id,
        }
        self.auto_respond_calls = 0
        self.mulligan_calls = []

    def connect(self):
        return True

    def get_pending_actions(self):
        return self.pending

    def auto_respond(self):
        self.auto_respond_calls += 1
        return True

    def submit_mulligan(self, keep):
        self.mulligan_calls.append(keep)
        return True


def engine_with(monkeypatch, bridge):
    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: bridge)
    return AutopilotEngine(planner=_Planner(), config=AutopilotConfig(dry_run=False))


MULLIGAN_STATE = {
    "turn": {"turn_number": 0},
    "pending_decision": "Mulligan",
    "decision_context": {"type": "mulligan"},
    "_bridge_request_type": "Mulligan",
    "_bridge_request_class": "MulliganRequest",
    "_bridge_game_state_id": 0,
}


def live_fingerprint(bridge):
    return decision_fingerprint(build_pending_decision(bridge.pending))


def stuck_window(engine):
    engine._window_repeat_sig = engine._priority_window_signature(MULLIGAN_STATE)
    engine._window_repeat_count = 12
    engine._window_first_seen_at = time.monotonic() - 16.0


def test_new_mulligan_after_opponent_wait_is_not_escaped(monkeypatch):
    bridge = MulliganBridge()
    engine = engine_with(monkeypatch, bridge)
    # Our previous round's answer (gameStateId 2) is still awaiting settlement.
    previous = build_pending_decision({**bridge.pending, "game_state_id": 2, "msg_id": 5})
    engine._request_tracker.note_submitted(decision_fingerprint(previous))
    stuck_window(engine)

    assert engine._maybe_escape_stuck_window(dict(MULLIGAN_STATE)) is False
    assert bridge.auto_respond_calls == 0
    assert bridge.mulligan_calls == []


def test_unanswered_mulligan_is_left_to_the_planner(monkeypatch):
    bridge = MulliganBridge()
    engine = engine_with(monkeypatch, bridge)
    stuck_window(engine)

    assert engine._maybe_escape_stuck_window(dict(MULLIGAN_STATE)) is False
    assert engine._try_auto_respond_escape(dict(MULLIGAN_STATE), "manual-required fallback") is False
    assert bridge.auto_respond_calls == 0
    assert bridge.mulligan_calls == []


def test_truly_stuck_mulligan_is_kept_never_auto_responded(monkeypatch):
    bridge = MulliganBridge()
    engine = engine_with(monkeypatch, bridge)
    record = engine._request_tracker._record(live_fingerprint(bridge))
    record.submissions = record.rejected = 3  # Arena re-presented our answers
    stuck_window(engine)

    assert engine._maybe_escape_stuck_window(dict(MULLIGAN_STATE)) is True
    assert bridge.mulligan_calls == [True]
    assert bridge.auto_respond_calls == 0
    # The keep is now in flight: a second escape waits for it to settle.
    assert engine._try_auto_respond_escape(dict(MULLIGAN_STATE), "again") is False
    assert bridge.mulligan_calls == [True]


def test_other_interactive_requests_still_escape(monkeypatch):
    bridge = MulliganBridge()
    bridge.pending = {"ok": True, "has_pending": True, "request_type": "Group"}
    engine = engine_with(monkeypatch, bridge)
    state = {"turn": {"turn_number": 5}, "_bridge_request_type": "Group"}
    assert engine._try_auto_respond_escape(state, "stuck group") is True
    assert bridge.auto_respond_calls == 1


# --- logged mulligan count ------------------------------------------------------------


def mulligan_req(count=None):
    request = {"mulliganType": "MulliganType_London"}
    if count is not None:
        request["mulliganCount"] = count
    return {"type": "GREMessageType_MulliganReq", "mulliganReq": request}


def test_mulligan_req_records_the_count():
    state = GameState()
    _handle_decision_message(state, "GREMessageType_MulliganReq", mulligan_req())
    assert state.decision_context == {"type": "mulligan", "mulligan_count": 0}
    _handle_decision_message(state, "GREMessageType_MulliganReq", mulligan_req(2))
    assert state.decision_context["mulligan_count"] == 2
    assert state._build_raw_snapshot_locked()["decision_context"]["mulligan_count"] == 2


def test_bridge_overlay_keeps_the_logged_count():
    from arenamcp.gre_bridge import enrich_snapshot_from_pending_response

    snapshot = {"pending_decision": "Mulligan", "decision_context": {"type": "mulligan", "mulligan_count": 2}}
    enrich_snapshot_from_pending_response(snapshot, MulliganBridge().pending, bridge_connected=True)
    assert snapshot["decision_context"]["mulligan_count"] == 2


ISLAND = {"name": "Island", "type_line": "Basic Land — Island", "card_types": ["CardType_Land"]}
OPT = {"name": "Opt", "type_line": "Instant", "mana_cost": "{U}"}
SPRITE = {"name": "Spell Sprite", "type_line": "Creature", "mana_cost": "{U}"}
BIG = {"name": "Big Sea Monster", "type_line": "Creature", "mana_cost": "{5}{U}{U}"}


def seven(*cards):
    return [{**card, "instance_id": 100 + index} for index, card in enumerate(cards)]


def test_fallback_hand_call_uses_the_logged_count():
    hand = seven(ISLAND, OPT, SPRITE, OPT, BIG, BIG, BIG)
    state = GameState()
    _handle_decision_message(state, "GREMessageType_MulliganReq", mulligan_req(2))
    game_state = {"hand": hand, "decision_context": dict(state.decision_context)}
    # Keeping 5: one land with cheap plays is a keep, not a mulligan to 4.
    assert _mulligan_hand_call(game_state) == "KEEP"
    assert _mulligan_hand_call({"hand": hand}) == "MULLIGAN"


def test_fallback_hand_call_matches_the_policy_on_land_counts():
    five_lands = seven(ISLAND, ISLAND, ISLAND, ISLAND, ISLAND, SPRITE, OPT)
    six_lands = seven(ISLAND, ISLAND, ISLAND, ISLAND, ISLAND, ISLAND, SPRITE)
    assert _mulligan_hand_call({"hand": five_lands}) == "KEEP"
    assert _mulligan_hand_call({"hand": six_lands}) == "MULLIGAN"
