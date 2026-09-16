"""Wave-2b Worker E tests: standalone.py Conversation Mode wiring.

Covers (mock-based, no Qt, no subprocess, no network):
- VoiceSession + ConversationController instantiated on StandaloneCoach init,
  saved conversation_mode restored without re-persisting.
- Coaching-loop mode routing: conversation mode keeps CRITICAL triggers on the
  legacy dispatch and makes all other triggers memory-only; turn_advice mode
  never consults the controller.
- set_mode via pipe dispatch flips routing live (no restart).
- reset_for_match at all three match-boundary sites (game-end event,
  match_id change, turn-number drop), clearing memory and bumping session_id.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

from arenamcp import standalone
from arenamcp.conversation import TURN_ADVICE, ConversationController
from arenamcp.pipe_adapter import PipeAdapter
from arenamcp.standalone_tempo import _TempoTracker
from arenamcp.voice_session import VoiceSession

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class StubUI:
    """UI adapter stub WITHOUT auto-created attributes (getattr must miss so
    the loop's emit_* pipe hooks stay inert)."""

    def __init__(self) -> None:
        self.logs: list[str] = []
        self.statuses: list[tuple[str, str]] = []
        self.advice_calls: list[tuple[str, str]] = []

    def log(self, message: str) -> None:
        self.logs.append(message)

    def status(self, key: str, value: str) -> None:
        self.statuses.append((key, value))

    def advice(self, text: str, seat_info: str) -> None:
        self.advice_calls.append((text, seat_info))

    def error(self, message: str) -> None:
        self.logs.append(f"ERROR: {message}")


class FakeMcp:
    def __init__(self, state: dict) -> None:
        self.state = state
        self.poll_hooks: list = []

    def poll_log(self) -> None:
        for hook in list(self.poll_hooks):
            hook()

    def get_game_state(self) -> dict:
        return dict(self.state)

    def get_draft_pack(self) -> dict:
        return {"is_active": False}

    def clear_pending_combat_steps(self) -> None:
        pass


class FakeTrigger:
    def __init__(self, triggers: list[str], repeat: bool = True) -> None:
        self._triggers = list(triggers)
        self._repeat = repeat

    def check_triggers(self, prev_state: dict, curr_state: dict) -> list[str]:
        fired = list(self._triggers)
        if not self._repeat:
            self._triggers = []
        return fired

    def _has_castable_instants(self, state: dict) -> bool:
        return False


class FakeBridgePoller:
    connected = False

    def poll(self):
        return None

    def reset(self) -> None:
        pass

    def enrich_snapshot(self, snapshot: dict) -> None:
        pass


class FakeCoachEngine:
    def __init__(self) -> None:
        self.calls: list = []
        self._deck_strategy = None

    def get_advice(self, game_state, trigger=None, style=None, **kwargs) -> str:
        self.calls.append(trigger)
        return "Cast your Lightning Bolt."

    def clear_deck_strategy(self) -> None:
        self._deck_strategy = None


class FakeSettings:
    def __init__(self, data: dict | None = None) -> None:
        self.data = dict(data or {})

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value, save: bool = True) -> None:
        self.data[key] = value


class FakeGameStateModule:
    """Stand-in for arenamcp.server.game_state (game-end boundary tests)."""

    def __init__(self) -> None:
        self.game_ended_event = threading.Event()
        self.consumed = 0

    def consume_game_end(self):
        self.consumed += 1
        self.game_ended_event.clear()
        return "loss", None


# ---------------------------------------------------------------------------
# Loop coach builder
# ---------------------------------------------------------------------------


def make_state(match_id="m1", turn=5, pending=None, decision_type=None) -> dict:
    state = {
        "match_id": match_id,
        "turn": {
            "turn_number": turn,
            "phase": "Phase_Main1",
            "step": "",
            "active_player": 1,
            "priority_player": 1,
        },
        "players": [],
        "battlefield": [],
        "hand": [],
        "stack": [],
        "legal_actions": [],
    }
    if pending is not None:
        state["pending_decision"] = pending
        state["decision_context"] = {"type": decision_type or "target_selection"}
    return state


def make_loop_coach(
    mode: str = TURN_ADVICE,
    state: dict | None = None,
    triggers: list[str] | None = None,
    repeat_triggers: bool = True,
) -> standalone.StandaloneCoach:
    """Bare StandaloneCoach via __new__ + targeted attribute injection
    (test_decision_arbiter.py / test_standalone_generic_selection.py pattern),
    with the conversation wiring __init__ now performs mirrored in miniature.
    """
    coach = standalone.StandaloneCoach.__new__(standalone.StandaloneCoach)
    coach.ui = StubUI()
    coach._mcp = FakeMcp(state or make_state())
    coach._normalize_turn_snapshot = lambda s: s
    coach._voice_output = None
    coach._running = True
    coach.draft_mode = False
    coach.set_code = None
    coach._trigger = FakeTrigger(triggers or [], repeat=repeat_triggers)
    coach._bridge_poller = FakeBridgePoller()
    coach._last_bridge_ui_status = False
    coach._match_number = 0
    coach._advice_history = []
    coach._game_end_handled = False
    coach._match_boundary_ts = 0.0
    coach._recent_gre_log = []
    coach._recent_gre_log_max = 30
    coach._vlm_card_cache = {}
    coach._vlm_card_failures = set()
    coach._missed_decisions = []
    coach._tempo_tracker = _TempoTracker()
    coach._autopilot_enabled = False
    coach._autopilot = None
    coach._auto_deck_strategy = False
    coach._deck_analyzed = False
    coach.advice_style = "quick"
    coach.settings = FakeSettings()
    coach.advice_frequency = "every_priority"
    coach._coach = FakeCoachEngine()
    coach._last_advised_decision_sig = None
    coach._last_forced_decision_sig = None
    coach._last_forced_decision_ts = 0.0
    coach._win_plan_turn = 0
    coach._pending_win_plan = None
    coach._pending_win_plan_turn = 0
    coach._pending_win_plan_turns = 0
    coach._last_backend_status = ""
    coach._backend_failed = False
    coach._backend_name = "online"
    coach._original_backend = None
    coach._original_model = None
    coach.speak_advice_calls: list[str] = []
    coach.speak_advice = lambda text, blocking=True: coach.speak_advice_calls.append(text)  # type: ignore[method-assign]
    coach._record_advice = lambda *args, **kwargs: None  # type: ignore[method-assign]
    coach._inject_library_summary_if_needed = lambda s: None  # type: ignore[method-assign]
    coach._has_actionable_priority_window = lambda s: False  # type: ignore[method-assign]
    coach._is_meaningful_advice_window = lambda *a, **k: True  # type: ignore[method-assign]

    # Conversation wiring (mirrors the new __init__ block).
    coach.last_match_id = None
    coach.voice_session = VoiceSession(coach._voice_output)
    coach.conversation = ConversationController(
        coach, emit_event=None, snapshot_fn=lambda: dict(coach._mcp.state)
    )
    if mode == "conversation":
        coach.conversation.set_mode("conversation", persist=False)
    return coach


def run_loop(monkeypatch, coach, iterations: int) -> None:
    """Run _coaching_loop for exactly `iterations` iterations (the trailing
    urgency-aware sleep marks iteration completion)."""
    sleeps = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        sleeps["n"] += 1
        if sleeps["n"] >= iterations:
            coach._running = False

    monkeypatch.setattr(standalone.time, "sleep", fake_sleep)
    coach._coaching_loop()


def seed_memory(coach, trigger: str = "land_played") -> None:
    coach.conversation.on_state(dict(coach._mcp.state), {}, [trigger])


# ---------------------------------------------------------------------------
# Init wiring
# ---------------------------------------------------------------------------


def test_init_builds_voice_session_and_controller(monkeypatch):
    monkeypatch.setattr(standalone, "get_settings", lambda: FakeSettings())
    coach = standalone.StandaloneCoach(register_hotkeys=False, backend="proxy")

    assert isinstance(coach.voice_session, VoiceSession)
    assert isinstance(coach.conversation, ConversationController)
    # Voice not initialized until start(): the arbiter wraps a no-op sink.
    assert coach.voice_session._output is None
    assert coach.last_match_id is None
    assert coach.conversation.mode == TURN_ADVICE


def test_init_restores_saved_mode_without_repersisting(monkeypatch):
    settings = FakeSettings({"conversation_mode": "conversation"})
    monkeypatch.setattr(standalone, "get_settings", lambda: settings)
    coach = standalone.StandaloneCoach(register_hotkeys=False, backend="proxy")

    assert coach.conversation.mode == "conversation"
    # Init restores the mode without REWRITING it: the fake's saved value must
    # survive byte-for-byte (persist=True is reserved for explicit changes).
    assert settings.data["conversation_mode"] == "conversation"


def test_init_conversation_session_rebinds_real_sink():
    coach = standalone.StandaloneCoach.__new__(standalone.StandaloneCoach)
    sink = MagicMock()
    coach._voice_output = sink

    coach._init_conversation_session()

    assert isinstance(coach.voice_session, VoiceSession)
    assert coach.voice_session._output is sink


# ---------------------------------------------------------------------------
# Event emit adapter
# ---------------------------------------------------------------------------


def test_emit_conversation_event_pipe_forwards_reply_and_status():
    coach = standalone.StandaloneCoach.__new__(standalone.StandaloneCoach)

    class PipeUI:
        def __init__(self) -> None:
            self.events: list[dict] = []
            self.statuses: list[tuple[str, str]] = []

        def _emit(self, event: dict) -> None:
            self.events.append(event)

        def status(self, key: str, value: str) -> None:
            self.statuses.append((key, value))

    coach.ui = PipeUI()
    identity = {
        "session_id": 2,
        "match_id": "m1",
        "turn_number": 4,
        "request_id": 7,
        "mode": "conversation",
    }

    coach._emit_conversation_event("conversation_reply", text="hello", identity=identity)
    coach._emit_conversation_event("conversation_status", state="thinking", request_id=7)

    assert coach.ui.events == [
        {"type": "conversation_reply", "text": "hello", "identity": identity}
    ]
    assert coach.ui.statuses == [("CONVO_STATE", "thinking")]


def test_emit_conversation_event_cli_drops_silently():
    coach = standalone.StandaloneCoach.__new__(standalone.StandaloneCoach)
    coach.ui = StubUI()  # no _emit channel (CLI adapter)

    # Must not raise in CLI mode.
    coach._emit_conversation_event("conversation_reply", text="hello", identity=None)
    coach._emit_conversation_event("conversation_status", state="idle", request_id=1)


# ---------------------------------------------------------------------------
# speak_advice TTS funnel
# ---------------------------------------------------------------------------


def test_speak_advice_routes_through_voice_session_in_conversation_mode():
    coach = standalone.StandaloneCoach.__new__(standalone.StandaloneCoach)
    sink = MagicMock()
    coach._voice_output = sink
    coach.voice_session = VoiceSession(sink)
    coach.conversation = ConversationController(
        coach, emit_event=None, snapshot_fn=lambda: {"turn": {"turn_number": 5}}
    )
    coach.conversation.set_mode("conversation", persist=False)

    coach.speak_advice("Cast your Lightning Bolt.")

    assert sink.speak.call_count == 1
    # Routed via the arbiter: sink called bare (VoiceSession owns blocking).
    args, kwargs = sink.speak.call_args
    assert args[0] == "Cast your Lightning Bolt."
    assert "blocking" not in kwargs


def test_speak_advice_uses_raw_sink_in_turn_advice_mode():
    coach = standalone.StandaloneCoach.__new__(standalone.StandaloneCoach)
    sink = MagicMock()
    coach._voice_output = sink
    coach.voice_session = VoiceSession(sink)
    coach.conversation = ConversationController(coach, emit_event=None, snapshot_fn=None)

    coach.speak_advice("Cast your Lightning Bolt.")

    assert sink.speak.call_count == 1
    sink.speak.assert_called_once_with("Cast your Lightning Bolt.", blocking=True)


# ---------------------------------------------------------------------------
# Coaching-loop mode routing
# ---------------------------------------------------------------------------


def test_conversation_mode_noncritical_trigger_is_memory_only(monkeypatch):
    coach = make_loop_coach(mode="conversation", triggers=["land_played"])

    run_loop(monkeypatch, coach, iterations=1)

    # Memory recorded the batch; legacy advice ladder never ran.
    assert len(coach.conversation.memory.turns) == 1
    turn = coach.conversation.memory.turns[0]
    assert turn.role == "state"
    assert "land_played" in turn.trigger
    assert coach.speak_advice_calls == []


def test_conversation_mode_critical_trigger_still_dispatches_legacy(monkeypatch):
    state = make_state(pending="Select Targets", decision_type="target_selection")
    coach = make_loop_coach(
        mode="conversation", state=state, triggers=["decision_required"]
    )

    run_loop(monkeypatch, coach, iterations=1)

    # CRITICAL trigger keeps the legacy dispatch (decision machinery intact)…
    assert coach.speak_advice_calls == ["Cast your Lightning Bolt."]
    # …AND the batch is recorded in memory.
    assert any("decision_required" in (t.trigger or "") for t in coach.conversation.memory.turns)


def test_turn_advice_mode_skips_controller_and_calls_legacy(monkeypatch):
    coach = make_loop_coach(mode=TURN_ADVICE, triggers=["land_played"])

    run_loop(monkeypatch, coach, iterations=1)

    assert coach.speak_advice_calls == ["Cast your Lightning Bolt."]
    # Controller never consulted in turn_advice mode: memory untouched.
    assert coach.conversation.memory.turns == []


# ---------------------------------------------------------------------------
# Wave 3 — proactive topic pipeline in the loop
# ---------------------------------------------------------------------------


def test_loop_calls_speak_topic_after_batch_in_conversation_mode(monkeypatch):
    coach = make_loop_coach(mode="conversation", triggers=["land_played"])

    topic_calls: list[tuple] = []
    original = coach.conversation.speak_topic_if_any

    def spy(match_id=None, match_number=0):
        topic_calls.append((match_id, match_number))
        return original(match_id=match_id, match_number=match_number)

    coach.conversation.speak_topic_if_any = spy  # type: ignore[method-assign]

    run_loop(monkeypatch, coach, iterations=1)

    # The loop asked for topics after dispatching the batch, passing the
    # loop's live match identity.
    assert topic_calls == [("m1", 0)]
    # No meaningful board change in the fixture state → nothing spoken.
    assert coach.speak_advice_calls == []


def test_loop_skips_speak_topic_in_turn_advice_mode(monkeypatch):
    coach = make_loop_coach(mode=TURN_ADVICE, triggers=["land_played"])

    topic_calls: list = []
    coach.conversation.speak_topic_if_any = (  # type: ignore[method-assign]
        lambda *a, **k: topic_calls.append((a, k))
    )

    run_loop(monkeypatch, coach, iterations=1)

    assert coach.speak_advice_calls == ["Cast your Lightning Bolt."]
    assert topic_calls == []


def test_loop_delivers_proactive_topic_speech(monkeypatch):
    coach = make_loop_coach(mode="conversation", triggers=["land_played"])
    # Meaningful board change: opponent revealed a new permanent this batch.
    prev_state = dict(coach._mcp.state)
    coach._mcp.state["battlefield"] = [
        {"name": "Grizzly Bears", "controller_seat_id": 2, "instance_id": 99, "type_line": "Creature"}
    ]
    coach._mcp.state["local_seat_id"] = 1
    coach._mcp.state["opponent_seat_id"] = 2
    coach.conversation.memory.last_proactive_ts = 0.0

    spoken: list[tuple] = []

    def fake_speak_topic(match_id=None, match_number=0):
        result = coach.conversation._speak_topic_if_any(match_id, match_number)
        if result is not None:
            spoken.append(result)
        return result

    coach.conversation.speak_topic_if_any = fake_speak_topic  # type: ignore[method-assign]

    run_loop(monkeypatch, coach, iterations=1)

    assert len(spoken) == 1
    text, priority, topic = spoken[0]
    assert topic.key == "opponent_development"
    assert priority == "proactive"
    # Spoken via the controller's arbiter route, NOT legacy speak_advice.
    assert coach.speak_advice_calls == []


def test_pipe_set_mode_flips_loop_routing_live(monkeypatch):
    import arenamcp.conversation as conversation_mod

    settings = FakeSettings()
    monkeypatch.setattr(conversation_mod, "get_settings", lambda: settings)

    coach = make_loop_coach(mode=TURN_ADVICE, triggers=["land_played"])
    adapter = PipeAdapter()
    adapter._emit = lambda event: None  # type: ignore[method-assign]
    adapter._coach = coach

    # Switch to conversation mode over the pipe — no restart.
    adapter._dispatch({"cmd": "set_mode", "mode": "conversation"})
    assert coach.conversation.mode == "conversation"

    run_loop(monkeypatch, coach, iterations=1)
    assert coach.speak_advice_calls == []  # non-critical trigger: memory-only

    # Switch back live — legacy dispatch resumes.
    coach._running = True
    adapter._dispatch({"cmd": "set_mode", "mode": "turn_advice"})
    assert coach.conversation.mode == TURN_ADVICE

    run_loop(monkeypatch, coach, iterations=1)
    assert coach.speak_advice_calls == ["Cast your Lightning Bolt."]


# ---------------------------------------------------------------------------
# Match-boundary resets (three sites)
# ---------------------------------------------------------------------------


def test_game_end_event_resets_conversation(monkeypatch):
    import arenamcp.server as server_mod

    fake_gs = FakeGameStateModule()
    monkeypatch.setattr(server_mod, "game_state", fake_gs)

    coach = make_loop_coach(mode="conversation", triggers=[], repeat_triggers=True)
    coach._advice_history = [{"advice": "earlier advice"}]
    coach._stage_post_match_analysis = lambda **kwargs: True  # type: ignore[method-assign]
    coach._get_latest_replay_path = lambda: None  # type: ignore[method-assign]

    reset_calls: list[tuple] = []
    original_reset = coach.conversation.reset_for_match

    def spy_reset(match_id, match_number):
        reset_calls.append((match_id, match_number))
        return original_reset(match_id, match_number)

    coach.conversation.reset_for_match = spy_reset  # type: ignore[method-assign]
    seed_memory(coach)
    session_before = coach.conversation.current_identity().session_id

    # The 2nd get_draft_pack call happens in iteration 2, AFTER iteration 1
    # mirrored last_match_id — so the event is a real game end, not startup
    # staleness.
    drafts = {"n": 0}
    original_draft = coach._mcp.get_draft_pack

    def draft_hook():
        drafts["n"] += 1
        if drafts["n"] >= 2:
            fake_gs.game_ended_event.set()
        return original_draft()

    coach._mcp.get_draft_pack = draft_hook  # type: ignore[method-assign]

    try:
        run_loop(monkeypatch, coach, iterations=2)
    finally:
        fake_gs.game_ended_event.clear()

    assert reset_calls == [("m1", 0)]
    assert fake_gs.consumed == 1
    # Memory cleared + session identity bumped by the boundary.
    assert coach.conversation.memory.turns == []
    assert coach.conversation.current_identity().session_id == session_before + 1


def test_match_id_change_resets_conversation_and_bumps_session(monkeypatch):
    coach = make_loop_coach(mode="conversation", triggers=[], repeat_triggers=True)

    reset_calls: list[tuple] = []
    original_reset = coach.conversation.reset_for_match

    def spy_reset(match_id, match_number):
        reset_calls.append((match_id, match_number))
        return original_reset(match_id, match_number)

    coach.conversation.reset_for_match = spy_reset  # type: ignore[method-assign]
    seed_memory(coach)
    session_before = coach.conversation.current_identity().session_id

    # On the 2nd poll_log (top of iteration 2) the match id flips m1 -> m2.
    polls = {"n": 0}

    def poll_hook():
        polls["n"] += 1
        if polls["n"] >= 2:
            coach._mcp.state["match_id"] = "m2"

    coach._mcp.poll_hooks.append(poll_hook)

    run_loop(monkeypatch, coach, iterations=2)

    # Boundary fired with the NEW match id and the bumped match number.
    assert reset_calls == [("m2", 1)]
    assert coach.last_match_id == "m2"
    assert coach.conversation.memory.turns == []
    assert coach.conversation.current_identity().session_id == session_before + 1


def test_turn_drop_resets_conversation(monkeypatch):
    coach = make_loop_coach(
        mode=TURN_ADVICE, triggers=["land_played"], repeat_triggers=False
    )

    reset_calls: list[tuple] = []
    original_reset = coach.conversation.reset_for_match

    def spy_reset(match_id, match_number):
        reset_calls.append((match_id, match_number))
        return original_reset(match_id, match_number)

    coach.conversation.reset_for_match = spy_reset  # type: ignore[method-assign]
    seed_memory(coach)
    session_before = coach.conversation.current_identity().session_id

    # On the 2nd poll_log (top of iteration 2) the turn drops 5 -> 3.
    polls = {"n": 0}

    def poll_hook():
        polls["n"] += 1
        if polls["n"] >= 2:
            coach._mcp.state["turn"]["turn_number"] = 3

    coach._mcp.poll_hooks.append(poll_hook)

    run_loop(monkeypatch, coach, iterations=2)

    # Iteration 1 delivered advice (turn_advice legacy path), so
    # last_advice_turn=5; iteration 2's turn 3 < 5 fires the turn-drop
    # boundary with the same match id and bumped match number.
    assert coach.speak_advice_calls == ["Cast your Lightning Bolt."]
    assert reset_calls == [("m1", 1)]
    assert coach.conversation.memory.turns == []
    assert coach.conversation.current_identity().session_id == session_before + 1
