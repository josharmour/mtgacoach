"""T5 Turn-Advice dispatch-parity harness (Wave 5 release threshold).

Compares the CURRENT ``StandaloneCoach`` dispatch behavior in ``turn_advice``
mode against the PRE-FEATURE BASELINE at commit ``2adb7c9`` (the last commit
before any conversation-mode work) on the same scripted recorded game.

How baseline expectations are derived
=====================================

The baseline module is loaded directly from a checkout of commit 2adb7c9
(``git show 2adb7c9:src/arenamcp/standalone.py`` — read-only, never touches
the working tree; override with ``$MTGACOACH_BASELINE_FILE``) and the REAL
baseline ``StandaloneCoach._coaching_loop`` runs as a live oracle over the
identical scripted snapshots + trigger batches as the current tree. The
recorded dispatch sequences must be byte-equal.

Static-analysis cross-check (baseline 2adb7c9 ``src/arenamcp/standalone.py``
line numbers; ``git diff 2adb7c9 -- src/arenamcp/standalone.py`` shows ZERO
removed lines — every legacy line survives verbatim in the current tree):

- L352 ``speak_advice(text, blocking=True)``: TTS filter ladder =
  L358 ``strip_health_tags`` -> L363 ``_is_passive_advice`` mute ->
  L369 ``_is_pass_narration`` 45s cooldown -> L377-379 raw
  ``self._voice_output.speak(text, blocking=blocking)``. The current tree adds
  an arbiter route ONLY when ``conversation.mode == "conversation"``; in
  turn_advice ``_conversation_identity_for_speech`` returns None, so the raw
  sink call with the SAME text and blocking argument is preserved.
- L1054 ``CRITICAL_PRIORITY`` / L1846 ``trigger_priorities`` sort: unchanged.
- L2130 ``should_advise`` ladder: unchanged.
- L2108 meaningful-window gate / L2177 ``QUIET_TRIGGERS``: unchanged.
- L2215 losing_badly -> ``generate_win_probability`` -> L2225
  ``speak_advice(prob)`` (default blocking=True); L2228 threat_detected ->
  ``get_advice(threat=...)`` -> ``speak_advice(advice)``: unchanged.
- L2530 delivered advice -> ``speak_advice(advice, blocking=False)``: unchanged.
- L1893-1901 'Action Required' turn+phase dedup and L2142-2153
  unresolved-decision signature dedup: unchanged.
- Conversation additions are all gated on ``mode == "conversation"``
  (on_state before the trigger loop, the non-critical continue gate,
  speak_topic_if_any after the batch) or are inert recorders
  (``_conversation_reset_for_match`` at the three boundary sites).

Scripted game coverage: new_turn, priority_gained (is_frequent=False
suppression), land_played, combat_attackers (fresh-state staleness pass),
combat_blockers (my-turn suppression), stack_spell_opponent (QUIET
suppression), threat_detected, losing_badly, decision_required, spell_resolved
(QUIET suppression), opponent-turn new_turn rename (QUIET suppression), a
match boundary (m1->m2, with the post-boundary trigger-suppression window),
and byte-equality of the strip_health_tags/passive/pass-narration filters.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Baseline module loader (live oracle)
# ---------------------------------------------------------------------------


def _export_baseline_file() -> str:
    """Materialize baseline standalone.py from commit 2adb7c9 (read-only)."""
    override = os.environ.get("MTGACOACH_BASELINE_FILE")
    if override and os.path.exists(override):
        return override
    repo = os.environ.get("MTGACOACH_REPO", os.getcwd())
    blob = subprocess.run(
        ["git", "show", "2adb7c9:src/arenamcp/standalone.py"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    fd, path = tempfile.mkstemp(prefix="w5_t5_baseline_", suffix=".py")
    with os.fdopen(fd, "w") as fh:
        fh.write(blob)
    return path


def _load_baseline_module():
    baseline_path = _export_baseline_file()
    import importlib.util

    spec = importlib.util.spec_from_file_location("w5_t5_baseline_standalone", baseline_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASELINE = _load_baseline_module()

import arenamcp.match_packets as _match_packets  # noqa: E402  (after baseline export)
import arenamcp.standalone as current_mod  # noqa: E402  (after baseline export)
from arenamcp.conversation import TURN_ADVICE, ConversationController  # noqa: E402
from arenamcp.standalone_tempo import _TempoTracker  # noqa: E402
from arenamcp.voice_session import VoiceSession  # noqa: E402

# ---------------------------------------------------------------------------
# Scripted recorded game + shared fakes
# ---------------------------------------------------------------------------


class RecordingSink:
    """Records every speak() with text + blocking; playback is instant."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def speak(self, text: str, blocking: bool = True) -> None:
        self.calls.append({"text": text, "blocking": blocking})

    def stop(self) -> None:
        pass


class StubUI:
    def __init__(self) -> None:
        self.logs: list[str] = []
        self.statuses: list[tuple[str, str]] = []
        self.advice_calls: list[tuple[str, str]] = []
        self.errors: list[str] = []

    def log(self, message: str) -> None:
        self.logs.append(message)

    def status(self, key: str, value: str) -> None:
        self.statuses.append((key, value))

    def advice(self, text: str, seat_info: str) -> None:
        self.advice_calls.append((text, seat_info))

    def error(self, message: str) -> None:
        self.errors.append(message)


class ScriptedMcp:
    """Snapshot cursor advances once per poll_log() call (the loop polls at
    the top of every iteration; the new_turn/spell_resolved delay buffers poll
    once more mid-iteration). The snapshot list is laid out to match that call
    flow exactly — see the snapshot builders below."""

    def __init__(self, snapshots: list[dict]) -> None:
        self.snapshots = list(snapshots)
        self.cursor = 0

    def poll_log(self) -> None:
        if self.cursor < len(self.snapshots) - 1:
            self.cursor += 1

    def get_game_state(self) -> dict:
        return dict(self.snapshots[self.cursor])

    def get_draft_pack(self) -> dict:
        return {"is_active": False}

    def clear_pending_combat_steps(self) -> None:
        pass


class ScriptedTriggers:
    """One single-trigger batch per loop iteration (repeat-last when
    exhausted). Batches are scripted independently of the snapshots; the
    snapshot layout guarantees each batch is evaluated against its intended
    state."""

    def __init__(self, batches: list[list[str]], threat: dict | None = None) -> None:
        self.batches = [list(b) for b in batches]
        self.i = 0
        if threat is not None:
            self._last_threat = threat

    def check_triggers(self, prev_state: dict, curr_state: dict) -> list[str]:
        b = self.batches[min(self.i, len(self.batches) - 1)]
        self.i += 1
        return list(b)

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


class RecordingCoachEngine:
    """Mock backend: deterministic advice per trigger + win-prob text."""

    def __init__(self, win_prob: str = "Win probability is low. Look for blockers.") -> None:
        self.calls: list[dict[str, Any]] = []
        self._deck_strategy = None
        self._win_prob_text = win_prob

    def get_advice(self, game_state, trigger=None, style=None, **kwargs) -> str:
        self.calls.append({"kind": "get_advice", "trigger": trigger, **kwargs})
        return f"ADVICE[{trigger}]"

    def generate_win_probability(self, game_state, opp_cards) -> str:
        self.calls.append({"kind": "win_prob"})
        return self._win_prob_text

    def clear_deck_strategy(self) -> None:
        self._deck_strategy = None


class FakeSettings:
    def __init__(self, data: dict | None = None) -> None:
        self.data = dict(data or {})

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value, save: bool = True) -> None:
        self.data[key] = value


def snap(
    match_id: str | None = "m1",
    turn: int = 5,
    pending: str | None = None,
    phase: str = "Phase_Main1",
    active: int = 1,
    priority: int = 1,
    life: tuple[int, int] = (20, 20),
    stack: list[dict] | None = None,
    legal_actions: list[str] | None = None,
) -> dict:
    """One scripted snapshot (local seat 1, opponents seat 2)."""
    state: dict[str, Any] = {
        "match_id": match_id,
        "local_seat_id": 1,
        "turn": {
            "turn_number": turn,
            "phase": phase,
            "step": "",
            "active_player": active,
            "priority_player": priority,
        },
        "players": [
            {"seat_id": 1, "life_total": life[0], "is_local": True},
            {"seat_id": 2, "life_total": life[1]},
        ],
        "battlefield": [],
        "hand": [],
        "stack": list(stack or []),
        "legal_actions": list(legal_actions or []),
    }
    if pending is not None:
        state["pending_decision"] = pending
        state["decision_context"] = {"type": "target_selection"}
    return state


THREAT = {"name": "Sheoldred, the Apocalypse", "warning": "deals 2 damage each upkeep"}

# Batches: one trigger per iteration. Snapshot S[i] is evaluated on iteration
# i-1 (cursor advances at the top-of-loop poll BEFORE the state read); the
# new_turn/spell_resolved delay buffers consume one extra snapshot
# mid-iteration.
MAIN_BATCHES: list[list[str]] = [
    ["new_turn"],  # it0  turn plan (non-blocking TTS)
    ["priority_gained"],  # it1  is_frequent=False -> suppressed
    ["land_played"],  # it2  step-by-step (non-blocking)
    ["combat_attackers"],  # it3  step-by-step, fresh state still in combat
    ["combat_blockers"],  # it4  my turn -> suppressed
    ["stack_spell_opponent"],  # it5  QUIET (no instants) -> suppressed
    ["threat_detected"],  # it6  critical threat advice (blocking default)
    ["losing_badly"],  # it7  critical win-prob path (blocking default)
    ["decision_required"],  # it8  critical decision advice (non-blocking)
    ["spell_resolved"],  # it9  QUIET (not my turn, no instants)
    ["new_turn"],  # it10 renamed opponent_turn -> QUIET
    ["land_played"],  # it11 m1->m2 boundary: suppressed (boundary age < 2s)
    ["land_played"],  # it12 m2 turn 2: fires (boundary age 5s > 2s)
]

MAIN_SNAPSHOTS: list[dict] = [
    snap(turn=5),  # S0  never evaluated (cursor starts past it after it0 poll)
    snap(turn=5),  # S1  it0 eval: new_turn, my turn
    snap(turn=5),  # S2  it0 re-fetch/fresh (delay poll)
    snap(turn=5),  # S3  it1 eval: priority_gained (my turn, no pending)
    snap(turn=5),  # S4  it2 eval/fresh: land_played
    snap(turn=5, phase="Phase_BeginCombat"),  # S5  it3 eval/fresh: combat_attackers
    snap(turn=5, phase="Phase_DeclareBlock"),  # S6  it4 eval: combat_blockers (my turn)
    snap(turn=5, active=2, priority=2, stack=[{"instance_id": 77, "name": "Spell"}]),  # S5->it5
    snap(turn=5),  # S8  it6 eval/fresh: threat_detected
    snap(turn=5, life=(3, 20)),  # S9  it7 eval: losing_badly
    snap(turn=5, pending="Select Targets"),  # S10 it8 eval/fresh: decision_required
    snap(turn=5, active=2, priority=2),  # S11 it9 eval: spell_resolved (opp turn)
    snap(turn=6, active=2, priority=2),  # S12 it9 fresh (delay poll)
    snap(turn=6, active=2, priority=2),  # S13 it10 eval: new_turn -> opponent_turn
    snap(turn=6, active=2, priority=2),  # S14 it10 fresh (delay poll)
    snap(match_id="m2", turn=1),  # S15 it11 eval: boundary suppression
    snap(match_id="m2", turn=2),  # S16 it12 eval/fresh: land_played fires
]


class FakeClock:
    """Static-between-iterations wall clock: advances 5s per trailing poll
    sleep so the 2s post-boundary trigger-suppression window is crossed
    deterministically."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def time(self) -> float:
        return self.now


def make_engine(
    module,
    *,
    snapshots: list[dict],
    batches: list[list[str]],
    threat: dict | None = None,
    win_prob: str = "Win probability is low. Look for blockers.",
):
    """Build a bare StandaloneCoach from `module` (baseline or current) driven
    by the scripted game. Returns (coach, sink, llm)."""
    coach = module.StandaloneCoach.__new__(module.StandaloneCoach)
    sink = RecordingSink()
    llm = RecordingCoachEngine(win_prob)
    coach.ui = StubUI()
    coach._mcp = ScriptedMcp(snapshots)
    coach._normalize_turn_snapshot = lambda s: s
    coach._voice_output = sink
    coach._running = True
    coach.draft_mode = False
    coach.set_code = None
    coach._trigger = ScriptedTriggers(batches, threat=threat)
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
    coach._coach = llm
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
    coach._last_pipe_heartbeat_time = 0.0
    # Side-effect-free stubs for helpers with external I/O (identical on both
    # engines — they are not part of the dispatch logic under test):
    coach._record_advice = lambda *a, **k: None
    coach._inject_library_summary_if_needed = lambda s: None
    coach._has_actionable_priority_window = lambda s: False
    coach._summarize_actionable_window = lambda s: ""
    coach._enable_replay_recording = lambda: None
    coach._is_mulligan_pending = lambda s: False
    coach._detect_match_result = lambda: "unknown"
    coach._has_explicit_game_end_evidence = lambda: False
    coach._stage_post_match_analysis = lambda **kwargs: True
    coach._get_latest_replay_path = lambda: None
    coach._get_match_context = lambda: {}
    coach._win_plan_worker = lambda state: None

    # --- Current-tree conversation wiring (mirror of the new __init__ block).
    if module is current_mod:
        coach.last_match_id = None
        coach.voice_session = VoiceSession(coach._voice_output)
        coach.conversation = ConversationController(
            coach, emit_event=None, snapshot_fn=lambda: dict(coach._mcp.snapshots[0])
        )
        coach.conversation.set_mode(TURN_ADVICE, persist=False)
        coach._conversation_speech_seq = 0
    return coach, sink, llm


def run_scripted_game(monkeypatch, coach, iterations: int) -> None:
    """Run _coaching_loop for exactly `iterations` trailing-poll sleeps.
    Delay-buffer sleeps (<0.5s) don't count. The fake clock advances 5s per
    iteration so the post-boundary suppression window (2s) is crossed."""
    clock = FakeClock()
    polls = {"n": 0}

    def fake_sleep(seconds: float) -> None:
        if seconds >= 0.5:  # trailing urgency-aware poll sleep only
            polls["n"] += 1
            clock.now += 5.0
            if polls["n"] >= iterations:
                coach._running = False

    monkeypatch.setattr(current_mod.time, "sleep", fake_sleep)
    monkeypatch.setattr(current_mod.time, "time", clock.time)
    coach._coaching_loop()


def dispatch_record(sink: RecordingSink, llm: RecordingCoachEngine) -> list[dict]:
    """Normalized dispatch sequence (the loop always calls get_advice BEFORE
    speak_advice within a trigger, so per-engine lists concatenate cleanly)."""
    seq: list[dict] = []
    seq.extend(dict(c) for c in llm.calls)  # get_advice + win_prob entries
    seq.extend(
        {"kind": "speak", "text": c["text"], "blocking": c["blocking"]} for c in sink.calls
    )
    return seq


@pytest.fixture(autouse=True)
def _no_match_packet_files(monkeypatch):
    """Match-packet recording writes files under ~/.arenamcp — no-op it for
    both engines (the loop imports the functions at call time)."""
    monkeypatch.setattr(_match_packets, "start_match_packet", lambda match_id: None)
    monkeypatch.setattr(_match_packets, "stop_match_packet", lambda: None)


# ---------------------------------------------------------------------------
# Derived baseline expectations (documented per segment)
# ---------------------------------------------------------------------------


def expected_main_dispatch() -> list[dict]:
    """Trigger->advice dispatch the baseline produces on MAIN_BATCHES, derived
    from the quoted source lines (see module docstring for line numbers).

    The scripted snapshots have empty legal_actions / no pending decision / no
    castable instants, so the meaningful-window gate (baseline L2108-2125,
    ``_is_meaningful_advice_window`` in standalone_windows.py L192) marks the
    filler windows trivial — this is REAL baseline behavior, exercised here:

    it0  new_turn      is_new_turn (5 > 0) -> get_advice -> speak(False)
                          [L2130/L2530]
    it1  priority_gained  filler: meaningful-window gate -> trivial (no
                          pending, local priority, no legal actions) ->
                          "Quiet: priority_gained (no meaningful play)"
                          suppressed BEFORE should_advise/is_frequent.
    it2  land_played   filler: same trivial-window suppression.
    it3  combat_attackers  NOT a filler trigger (deliberately outside
                          _MEANINGFUL_GATE_TRIGGERS) -> is_step_by_step ->
                          get_advice; fresh state still BeginCombat -> not
                          stale -> speak(False)
    it4  combat_blockers  my turn -> suppressed ("my turn, not blocking")
    it5  stack_spell_opponent  critical but QUIET_TRIGGERS + no instants
                          -> suppressed BEFORE the LLM [L2177]
    it6  threat_detected  critical -> get_advice(threat=THREAT) ->
                          speak_advice(advice) blocking=True default [L2228]
    it7  losing_badly  critical -> generate_win_probability ->
                          speak_advice(prob) blocking=True default [L2215/2225]
    it8  decision_required  critical; arbitrate (bridge down, pending set)
                          -> log decision -> get_advice -> speak(False);
                          sig recorded for dedup [L2530]
    it9  spell_resolved  filler: trivial window -> suppressed
    it10 new_turn (opp turn) -> renamed opponent_turn [raw_new_turn branch]
                          -> filler trivial window -> suppressed
    it11 land_played (m2 t1)  match boundary m1->m2 reset _match_boundary_ts;
                          trigger suppressed within the 2s window
    it12 land_played (m2 t2)  boundary age 5s > 2s -> is_step_by_step BUT
                          filler trivial window (no pending, no legal
                          actions) -> suppressed
    """
    return [
        {"kind": "get_advice", "trigger": "new_turn"},
        {"kind": "get_advice", "trigger": "combat_attackers"},
        {"kind": "get_advice", "trigger": "threat_detected", "threat": THREAT},
        {"kind": "win_prob"},
        {"kind": "get_advice", "trigger": "decision_required"},
        {"kind": "speak", "text": "ADVICE[new_turn]", "blocking": False},
        {"kind": "speak", "text": "ADVICE[combat_attackers]", "blocking": False},
        {"kind": "speak", "text": "ADVICE[threat_detected]", "blocking": True},
        {"kind": "speak", "text": "Win probability is low. Look for blockers.", "blocking": True},
        {"kind": "speak", "text": "ADVICE[decision_required]", "blocking": False},
    ]


# ---------------------------------------------------------------------------
# Tests: dispatch parity (live baseline oracle vs current tree)
# ---------------------------------------------------------------------------


def test_t5_dispatch_sequence_matches_baseline(monkeypatch):
    """Core T5: identical trigger->advice dispatch sequence vs baseline
    2adb7c9 — same triggers admitted/suppressed, same LLM call count, same
    speak_advice invocation text and blocking argument."""
    b_coach, b_sink, b_llm = make_engine(
        BASELINE, snapshots=MAIN_SNAPSHOTS, batches=MAIN_BATCHES, threat=THREAT
    )
    run_scripted_game(monkeypatch, b_coach, iterations=len(MAIN_BATCHES))

    c_coach, c_sink, c_llm = make_engine(
        current_mod, snapshots=MAIN_SNAPSHOTS, batches=MAIN_BATCHES, threat=THREAT
    )
    run_scripted_game(monkeypatch, c_coach, iterations=len(MAIN_BATCHES))

    expected = expected_main_dispatch()
    actual_baseline = dispatch_record(b_sink, b_llm)
    actual_current = dispatch_record(c_sink, c_llm)

    assert actual_baseline == expected, (
        "baseline oracle diverged from the documented derivation — harness "
        f"must be fixed first:\nexpected={expected}\nbaseline={actual_baseline}"
    )
    assert actual_current == actual_baseline, (
        "T5 DISPATCH PARITY FAILURE — current turn_advice dispatch differs "
        f"from baseline 2adb7c9:\nbaseline={actual_baseline}\ncurrent={actual_current}"
    )


def test_t5_byte_equal_text_through_tts_filters(monkeypatch):
    """Byte-equality through the speak_advice filter ladder: the health-tag
    strip (baseline L358) at the TTS boundary — display text keeps the tag,
    speech drops it; both engines byte-identical."""
    batches = [["losing_badly"], ["losing_badly"]]
    snapshots = [
        snap(turn=5, life=(3, 20)),  # S0 (never evaluated)
        snap(turn=5, life=(3, 20)),  # S1 it0 eval
        snap(turn=6, life=(3, 20)),  # S2 it1 eval
    ]

    class TaggedProbEngine(RecordingCoachEngine):
        def generate_win_probability(self, game_state, opp_cards) -> str:
            self.calls.append({"kind": "win_prob"})
            # First call tagged (backend failure shape), second clean.
            if len(self.calls) == 1:
                return "[BACKEND ERROR] gateway down"
            return "Win probability is low. Look for blockers."

    b_coach, b_sink, b_llm = make_engine(BASELINE, snapshots=snapshots, batches=batches)
    b_coach._coach = TaggedProbEngine()
    run_scripted_game(monkeypatch, b_coach, iterations=2)

    c_coach, c_sink, c_llm = make_engine(current_mod, snapshots=snapshots, batches=batches)
    c_coach._coach = TaggedProbEngine()
    run_scripted_game(monkeypatch, c_coach, iterations=2)

    # speak_advice strips the [BACKEND ERROR] prefix before speaking (L358)
    # but the DISPLAY path (ui.advice, "WIN PROBABILITY") keeps the tag.
    assert [c["text"] for c in b_sink.calls] == [
        "gateway down",
        "Win probability is low. Look for blockers.",
    ]
    assert [c["text"] for c in c_sink.calls] == [c["text"] for c in b_sink.calls]
    assert [c["blocking"] for c in c_sink.calls] == [c["blocking"] for c in b_sink.calls]
    assert c_coach.ui.advice_calls == b_coach.ui.advice_calls
    assert b_coach.ui.advice_calls[0][0] == "[BACKEND ERROR] gateway down"


def test_t5_passive_and_pass_narration_filters_match(monkeypatch):
    """The passive TTS mute (baseline L363) silences the same texts on both
    engines — byte-equal silence, identical blocking flags on the survivors."""
    batches = [["new_turn"]] * 4
    snapshots = [
        snap(turn=5),  # S0
        snap(turn=5),  # S1 it0
        snap(turn=5),  # S2 it0 fresh
        snap(turn=6),  # S3 it1 eval
        snap(turn=6),  # S4 it1 fresh
        snap(turn=7),  # S5 it2 eval
        snap(turn=7),  # S6 it2 fresh
        snap(turn=8),  # S7 it3 eval
    ]

    class PassiveEngine(RecordingCoachEngine):
        answers = ["Pass.", "I'm passing priority now.", "Attack with everything.", "Pass."]

        def get_advice(self, game_state, trigger=None, style=None, **kwargs) -> str:
            self.calls.append({"kind": "get_advice", "trigger": trigger, **kwargs})
            return self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]

    b_coach, b_sink, b_llm = make_engine(BASELINE, snapshots=snapshots, batches=batches)
    b_coach._coach = PassiveEngine()
    run_scripted_game(monkeypatch, b_coach, iterations=4)

    c_coach, c_sink, c_llm = make_engine(current_mod, snapshots=snapshots, batches=batches)
    c_coach._coach = PassiveEngine()
    run_scripted_game(monkeypatch, c_coach, iterations=4)

    # Baseline speak_advice ladder: "Pass." is passive (silence phrase, no
    # action verb, <60 chars) -> muted. "I'm passing priority now." has NO
    # \bpass\b word-boundary match ("passing") so it is NOT passive, but it IS
    # a pass narration (L369: endswith "passing priority now") -> spoken once,
    # then the 45s cooldown would mute repeats. "Attack with everything."
    # spoken. Final "Pass." muted again.
    spoken_baseline = [c["text"] for c in b_sink.calls]
    assert spoken_baseline == ["I'm passing priority now.", "Attack with everything."]
    assert [c["text"] for c in c_sink.calls] == spoken_baseline
    assert [c["blocking"] for c in c_sink.calls] == [c["blocking"] for c in b_sink.calls]


def test_t5_dedup_action_required_suppression_matches(monkeypatch):
    """Dedup keys: 'Action Required' turn+phase suppression (baseline
    L1893-1901) — first decision_required advises, duplicates on the same
    turn+phase are suppressed identically on both engines."""
    batches = [["decision_required"]] * 3
    state = snap(turn=5, pending="Action Required")
    snapshots = [state, state, state, state]

    b_coach, b_sink, b_llm = make_engine(BASELINE, snapshots=snapshots, batches=batches)
    run_scripted_game(monkeypatch, b_coach, iterations=3)

    c_coach, c_sink, c_llm = make_engine(current_mod, snapshots=snapshots, batches=batches)
    run_scripted_game(monkeypatch, c_coach, iterations=3)

    assert [c["trigger"] for c in b_llm.calls] == ["decision_required"]
    assert [c["text"] for c in b_sink.calls] == ["ADVICE[decision_required]"]
    assert [c["blocking"] for c in b_sink.calls] == [False]
    assert [c["trigger"] for c in c_llm.calls] == [c["trigger"] for c in b_llm.calls]
    assert [c["text"] for c in c_sink.calls] == [c["text"] for c in b_sink.calls]
    assert [c["blocking"] for c in c_sink.calls] == [c["blocking"] for c in b_sink.calls]


def test_t5_match_boundary_parity(monkeypatch):
    """Match boundary (m1 -> m2): the boundary resets coaching state and
    suppresses triggers for the 2s window on BOTH engines; the first
    post-window dispatch is byte-identical (also covers the current tree's
    conversation reset at the boundary being inert)."""
    b_coach, b_sink, b_llm = make_engine(
        BASELINE, snapshots=MAIN_SNAPSHOTS, batches=MAIN_BATCHES, threat=THREAT
    )
    run_scripted_game(monkeypatch, b_coach, iterations=len(MAIN_BATCHES))

    c_coach, c_sink, c_llm = make_engine(
        current_mod, snapshots=MAIN_SNAPSHOTS, batches=MAIN_BATCHES, threat=THREAT
    )
    run_scripted_game(monkeypatch, c_coach, iterations=len(MAIN_BATCHES))

    # The boundary fired on both: match number bumped, match id mirrored.
    assert b_coach._match_number == c_coach._match_number == 1
    assert c_coach.last_match_id == "m2"
    # Post-boundary dispatch identical: it11 suppressed (2s window), it12
    # suppressed by the trivial-window gate (no legal actions) — byte-equal
    # on both engines, and the last spoken text is the it8 decision advice.
    assert dispatch_record(c_sink, c_llm) == dispatch_record(b_sink, b_llm)
    assert [c["text"] for c in c_sink.calls][-1] == "ADVICE[decision_required]"


def test_t5_mode_switch_back_restores_parity(monkeypatch):
    """Mode switch conversation -> turn_advice: after switching back, the
    dispatch sequence resumes byte-identically to the baseline (raw sink path,
    same blocking, same LLM calls)."""
    import arenamcp.conversation as conversation_mod

    monkeypatch.setattr(conversation_mod, "get_settings", lambda: FakeSettings())

    batches = [["land_played"]] * 4
    snapshots = [
        snap(turn=5),  # S0
        snap(turn=5, legal_actions=["Play Mountain"]),  # S1 it0 (meaningful)
        snap(turn=6, legal_actions=["Play Forest"]),  # S2 it1 (meaningful)
        snap(turn=7, legal_actions=["Play Plains"]),  # S3 it2 (meaningful)
        snap(turn=8, legal_actions=["Play Island"]),  # S4 it3 (meaningful)
    ]

    c_coach, c_sink, c_llm = make_engine(current_mod, snapshots=snapshots, batches=batches)
    # Two iterations in conversation mode: non-critical trigger is memory-only
    # (no speech at all — the arbiter never touches the sink).
    c_coach.conversation.set_mode("conversation", persist=False)
    run_scripted_game(monkeypatch, c_coach, iterations=2)
    c_coach._running = True
    c_coach.conversation.set_mode(TURN_ADVICE, persist=False)
    run_scripted_game(monkeypatch, c_coach, iterations=2)

    b_coach, b_sink, b_llm = make_engine(BASELINE, snapshots=snapshots, batches=batches)
    run_scripted_game(monkeypatch, b_coach, iterations=4)

    # Post-switch current dispatch == baseline's last two dispatch records.
    b_last2 = [(c["text"], c["blocking"]) for c in b_sink.calls][-2:]
    c_all = [(c["text"], c["blocking"]) for c in c_sink.calls]
    assert b_last2 == [("ADVICE[land_played]", False), ("ADVICE[land_played]", False)]
    assert c_all == b_last2
    assert [c["trigger"] for c in c_llm.calls] == ["land_played", "land_played"]


def test_t5_conversation_additions_inert_in_turn_advice(monkeypatch):
    """Regression guards for the conversation additions on the legacy path:
    controller never consulted (on_state/speak_topic_if_any), memory untouched,
    raw sink calls (text, blocking) with no arbiter indirection, and no memory
    residue after a transient conversation phase."""
    import arenamcp.conversation as conversation_mod

    monkeypatch.setattr(conversation_mod, "get_settings", lambda: FakeSettings())

    batches = [["land_played"]] * 3
    snapshots = [
        snap(turn=5),  # S0
        snap(turn=5, legal_actions=["Play Mountain"]),  # S1 it0 (meaningful)
        snap(turn=6, legal_actions=["Play Forest"]),  # S2 it1 (meaningful)
        snap(turn=7, legal_actions=["Play Plains"]),  # S3 it2 (meaningful)
    ]

    c_coach, c_sink, c_llm = make_engine(current_mod, snapshots=snapshots, batches=batches)

    on_state_calls: list = []
    topic_calls: list = []
    orig_on_state = c_coach.conversation.on_state
    orig_topic = c_coach.conversation.speak_topic_if_any

    def spy_on_state(curr, prev, triggers):
        on_state_calls.append(list(triggers))
        return orig_on_state(curr, prev, triggers)

    def spy_topic(*a, **k):
        topic_calls.append((a, k))
        return orig_topic(*a, **k)

    c_coach.conversation.on_state = spy_on_state  # type: ignore[method-assign]
    c_coach.conversation.speak_topic_if_any = spy_topic  # type: ignore[method-assign]

    run_scripted_game(monkeypatch, c_coach, iterations=2)

    # Turn-advice never consults the controller (loop gate reads mode).
    assert on_state_calls == []
    assert topic_calls == []
    assert c_coach.conversation.memory.turns == []
    # Raw sink calls — the arbiter never mediated (raw path = text + blocking).
    assert c_sink.calls == [
        {"text": "ADVICE[land_played]", "blocking": False},
        {"text": "ADVICE[land_played]", "blocking": False},
    ]

    # Transient conversation phase: on_state records harmlessly; back in
    # turn_advice the raw sink path resumes with no arbitration residue. The
    # third iteration ALSO fires the None->first-match boundary reset (the
    # scripted loop starts with last_match_id=None), clearing the seeded
    # memory — asserted below as the Wave-5 boundary behavior.
    c_coach.conversation.set_mode("conversation", persist=False)
    c_coach.conversation.on_state(dict(snapshots[0]), {}, ["land_played"])
    assert len(c_coach.conversation.memory.turns) == 1
    c_coach.conversation.set_mode(TURN_ADVICE, persist=False)
    c_coach._running = True
    run_scripted_game(monkeypatch, c_coach, iterations=1)
    # The None -> "m1" boundary cleared the seeded memory (Wave-5 reset site)
    # and the turn_advice iteration dispatched through the raw sink again.
    assert c_coach.conversation.memory.turns == []
    assert c_coach.last_match_id == "m1"
    assert c_sink.calls[-1] == {"text": "ADVICE[land_played]", "blocking": False}
