# Conversation Mode — Progress

Plan of record: `conversation-mode.md` (do not treat this file as evidence of shipped functionality).
Coordinator: Hermes (glm-5.3-flash via LiteLLM gateway 10.0.0.10:8444 — verified 200 OK, sole model `glm-5.3-flash`; delegation max 5 children, no model pin).

## Environment (verified this session)

- Repo checkout: `/mnt/repos/mtgacoach` (CIFS mount `//10.0.0.2/repos`). Branch `master`. All PRE-EXISTING (MageZero/perf/docs) working-tree work was committed at Josh's request as `7b6d8ab` (30 code files), `49f007f` (AGENTS.md + perf handoff doc), `2adb7c9` (flow_layout) — nothing stashed or discarded. Only Conversation Mode changes remain uncommitted until Wave-2 verification.
- Test runner: `.venv/bin/python` (Python 3.12.13) with `QT_QPA_PLATFORM=offscreen` and `ARENAMCP_LOG_FILE=/tmp/arenamcp-pytest-baseline.log`.
- Venv repairs applied (metadata-only, no behavior change): installed `msgpack`; force-reinstalled `mcp>=1.0.0,<2` (1.30.0 — dist-info lost in a Mac→Linux venv copy); installed `openai`; purged stale macOS `__pycache__` trees under src/tests/tools (tracebacks referenced `/Volumes/repos/...`).
- Command template: `cd /mnt/repos/mtgacoach && ARENAMCP_LOG_FILE=/tmp/arenamcp-pytest-convo.log QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest <files> -q -p no:cacheprovider`
- SMB/CRLF discipline: repo files use `\r\n` line endings — patches must match exactly; run `python3 -c "import ast; ast.parse(open('<file>').read())"` after EVERY `.py` write/patch (mount mangles indentation); never reformat whole files.

## Baseline (2026-09-15, BEFORE any Conversation Mode work)

**1988 passed, 24 skipped, 12 failed + 4 errors — all PRE-EXISTING, unrelated to Conversation Mode.** Do not attribute these to this feature's changes; workers own only failures in files they touched.

| Group | Tests | Root cause |
|---|---|---|
| Signature drift | `test_neural_response_validation.py::test_evaluator_valid_batch_applies_rl`, `test_full_lifecycle_coaching_request.py::test_full_lifecycle_healthy_request` | Uncommitted `mcts_evaluator.py` changes pass `checkpoint_hash=` to `MageZeroClient.evaluate_batch`; local monkeypatch stubs use the old signature. |
| Fail-closed gate guard | 6 × `test_unseen_deck_gate.py` | `tools/training/wp3/build_unseen_gate_corpus.py:433` dies with `MCTS leak marker in prompt` inside the tests. |
| Render/eligibility | `test_bridge_policy_eligibility.py::test_unknown_record_mcts_counts_never_rendered`, `test_oracle_coverage.py::TestBridgeOracleAttachment::test_opp_never_marked_attacking` | MCTS-count/oracle rendering assertions vs current render path. |
| Combat-micro fixture | 4 × ERROR `test_magezero_combat_micro.py::test_render_*` + `::test_pipeline_flag_on_renders_combat_rows` | `build_combat_record` assertion failure at fixture setup (`tests/test_magezero_combat_micro.py:345`). |

## Integrated design (Wave 1 output — BINDING CONTRACT for Wave 2)

Wave-1 inspection reports (conversation / voice / desktop agents) are consolidated here. Read `conversation-mode.md` for goals; this section is the implementation contract.

### Canonical strings

- Session modes: `"turn_advice"` | `"conversation"`.
- Verbosity (keys reserved now, behavior Wave 3): `"quiet"` | `"balanced"` | `"detailed"`.
- Speech priorities (ordered high→low): `"question"` > `"urgent"` > `"advice"` > `"proactive"`.
- Conversation status: `"idle"` | `"listening"` | `"thinking"` | `"speaking"`.
- Settings keys (DEFAULTS in `src/arenamcp/settings.py`): `"conversation_mode": "turn_advice"`, `"conversation_verbosity": "balanced"`.

### ResponseIdentity (canonical, lives in `src/arenamcp/conversation.py`)

```python
@dataclass(frozen=True)
class ResponseIdentity:
    session_id: int      # bumps on mode change AND match boundary
    match_id: str | None
    match_number: int
    turn_number: int
    active_player: int | None
    decision_sig: str | None
    mode: str
    request_id: int      # monotonic per engine process
```

- `voice_session.py` treats identity STRUCTURALLY (any object with `session_id`/`match_id`/`turn_number`/`request_id` attrs; `identity=None` ⇒ always speak, legacy path). No import of `conversation.py` needed.
- Across the pipe, identity is a nested JSON dict: `{"session_id", "match_id", "turn_number", "request_id", "mode"}`.
- Stale rule: discard a response/speech when live identity differs in `session_id`, `match_id`, or `match_number`; position-bound advice additionally drops on `turn_number`/`active_player`/`decision_sig` change; a newer `request_id` supersedes older pending ones. Payloads WITHOUT identity are never stale-dropped (preserves all legacy/Turn-Advice behavior).

### Pipe protocol additions (backward compatible; absent fields = legacy behavior)

- UI→engine commands: `{"cmd":"set_mode","mode":str}`, `{"cmd":"set_verbosity","verbosity":str}`, `{"cmd":"stop_speech"}`.
- `speak_request` event payload gains optional `"priority": str` and `"identity": dict`.
- Engine→UI event: `{"type":"conversation_reply","text":str,"identity":dict}`.
- Status keys: `MODE`, `VERBOSITY`, `CONVO_STATE`.

### ConversationController API (`conversation.py`; engine wiring instantiates as `coach.conversation`)

```python
ConversationController(coach, emit_event=None, snapshot_fn=None)
# emit_event(type, **fields) -> None   pipe event emitter; None in CLI mode
# snapshot_fn() -> dict                live game-state snapshot; None => controller falls back
#                                      to a guarded coach._mcp.get_game_state() read
```

Methods/properties: `mode` (str), `verbosity` (str), `set_mode(mode, persist=True)`, `set_verbosity(verbosity)`, `on_state(curr_state, prev_state, triggers)` (records memory; NO proactive speech in Wave-2 slice — topic selector is Wave 3), `on_user_question(text, source="typed") -> request_id` (records turn, preempts current speech, spawns daemon answer thread), `cancel_pending()`, `reset_for_match(match_id, match_number)`, `current_identity() -> ResponseIdentity`.

Answer path: `coach._coach.get_advice(game_state, question=text)` with memory replayed as a compact prior-conversation block in the user message; gate on delivery (fresh identity re-check + request supersession); speech via `coach.voice_session.speak(...)` (fallback `coach._voice_output.speak`); emit `conversation_reply` via `emit_event`. Failed LLM calls return `[BACKEND ERROR]`-tagged text displayed in the transcript but NEVER spoken (use `backend_health.strip_health_tags` at the TTS boundary; `is_backend_error_text` for detection). Mic/transcription failures surface as `[LOCAL FALLBACK]`-tagged UI notices.

Memory (`MatchMemory`): ring of ~12 `ConversationTurn(role, text, ts, identity, trigger, topic)`, `discussed_topics: dict[str, float]` (topic→last-spoken ts, Wave-3 cooldown), `revealed_opponent_cards`, `plan_summary` (seeded from `GamePlanManager.coach_intro()`), `last_evidence` (Wave-4 `EvidenceBlock` shape reserved). Reset at ALL THREE match-boundary sites in `standalone.py` (game-end event, match_id change, turn-number drop). Thread-safe (lock — coaching loop thread + answer threads). Memory never stores a bare win probability; observed facts vs hypotheses distinguished via `MCTSBranch.score_provenance` + gating labels.

### VoiceSession API (`voice_session.py`, generic arbiter)

```python
VoiceSession(output, now=time.monotonic)
speak(text, *, priority, identity) -> SpeechOutcome
stop_speaking(reason="user"); cancel_obsolete(identity)
state -> "idle" | "rendering" | "speaking";  add_listener(cb)
```

`SpeechState`/`SpeechPriority` enums; `SpeechRequest`/`SpeechOutcome` dataclasses. Preemption: higher priority replaces current; same/lower priority replaces only with newer `request_id`; stale identity ⇒ `CANCELLED` without touching audio. Stop hook delegates to the sink: CLI `VoiceOutput.stop()` (`tts.py:908`), desktop `_PipeVoiceOutput` → `emit_speech_stop` → `TtsManager.stop_speech()`. Thread-safe (speak is called from coaching-loop AND answer threads). EventPriority→speech mapping (in `conversation.py`): USER_QUESTION→"question"; URGENT_DECISION, THREAT→"urgent"; TURN_CONTEXT→"advice"; STATE_SHIFT, FILLER→"proactive".

### Mode-switch flow (end-to-end)

UI button → `CoachSession.set_mode(m)`: `_tts.stop_speech()` + `send_command("set_mode", m)` → `pipe_adapter` dispatch → `coach.conversation.set_mode(m)` (bumps session_id, `cancel_pending()`, `coach.voice_session.stop_speaking("mode_change")`, persists engine-side, `status("MODE", m)`) → `statusChanged("MODE")` → panel button property update. Desktop writes the settings key too; on session `started`, panel syncs saved mode + verbosity (sync-at-start, like `sync_voice_preferences`).

### Wave-2 slice semantics (regression boundary)

- `"turn_advice"` mode: controller NOT consulted; legacy dispatch + `speak_advice` byte-for-byte.
- `"conversation"` mode: user questions (typed/PTT) via controller; CRITICAL triggers (`decision_required` / CRITICAL_PRIORITY set) still dispatch through the legacy path so decision machinery is preserved; all other triggers → `controller.on_state` memory-only (silent; proactive commentary is Wave 3).
- `stop_speech` command works in BOTH modes → `coach.voice_session.stop_speaking()` (guarded fallback `coach._voice_output.stop()`).
- PTT = press-and-hold UI button (no global hotkey in this slice — F4 is autopilot cancel; avoid conflicts). UI process captures mic + transcribes (faster-whisper if importable; else button disabled with clear "voice input unavailable" state); transcript rides the existing `chat` command.

### File ownership (Wave 2 — strictly disjoint)

- **Worker C**: NEW `src/arenamcp/conversation.py`, NEW `tests/test_conversation.py`.
- **Worker V**: NEW `src/arenamcp/voice_session.py`; EDIT `src/arenamcp/standalone_voice.py` (`_PipeVoiceOutput` only); EDIT `src/arenamcp/pipe_adapter.py` (dispatch additions, speak_request payload, `_handle_chat` routing — all engine-side uses guarded `getattr(coach, "conversation", None)` / `getattr(coach, "voice_session", None)` so they no-op until Worker E wires standalone); NEW `tests/test_voice_session.py`, NEW `tests/test_pipe_conversation.py`.
- **Worker D**: EDIT `src/arenamcp/settings.py` (the two DEFAULTS keys only); EDIT `src/arenamcp/desktop/coach_session.py`, `tts_manager.py`, `tts_worker.py`, `compact_coach.py`; NEW `src/arenamcp/desktop/conversation_transcript.py`; NEW PTT module under `desktop/` (optional slice item); NEW `tests/test_desktop_conversation.py`.
- **Worker E** (AFTER C+V+D land): EDIT `src/arenamcp/standalone.py` only — instantiate `voice_session` + `conversation`, coaching-loop routing per slice semantics, `reset_for_match` at the three boundary sites, `stop_speech` target. May make minimal contract-conformance fixes in C/V/D modules if imports/signatures mismatch this contract (must report every such change).

### Hard rules for all workers

1. DO NOT `git add`/`git commit` — the parent stages and commits by file group after verification.
2. Touch ONLY your owned files. Another agent is editing other files concurrently; failures in files you did not touch are transient foreign breakage — re-run once after ~60s; own only failures in YOUR files.
3. Do NOT modify the venv or install packages; report missing deps instead.
4. No subprocess-spawning tests (no `MainWindow()` in tests — use bare `CompactCoachPanel` + Mock session per `test_compact_coach.py` pattern; `QT_QPA_PLATFORM=offscreen`).
5. Backend-health tag conventions per `src/arenamcp/backend_health.py` (see also repo skill notes): failure text tagged, never silently advice-shaped, never spoken.
6. Preserve CRLF; `ast.parse` after every write; `ruff check --no-fix` your touched files and fix only NEW violations.
7. Baseline pre-existing failures (table above) are not yours.

## Wave status

- [COMPLETE] Wave 1 — inspection/design (3 agents).
- [COMPLETE] Wave 1 integration — this contract.
- [COMPLETE] Wave 2a Worker C — `src/arenamcp/conversation.py` + `tests/test_conversation.py`: 28 passed; ruff clean (system ruff 0.15.22); no material deviations. NOTE for Worker E: controller reads `coach.last_match_id` via getattr — standalone.py keeps match_id as a local loop var, so E must expose it (or pass `snapshot_fn`) or match_id degrades to snapshot-only.
- [COMPLETE] Wave 2a Worker V — `voice_session.py` (45 passed incl. existing pipe tests); `standalone_voice.py`/`pipe_adapter.py` edits backward compatible. 2 deliberate deviations: unknown priority strings coerce to PROACTIVE (no raise); `_handle_chat` conversation routing sits before the `coach._coach is None` guard (legacy block unchanged). Worker E note: standalone must set `coach.voice_session` + `coach.conversation`; routing/stop_speech activate automatically once present.
- [IN PROGRESS] Wave 2a Worker D — hit iteration cap; completion agent (sa-0-129eecfb) finishing per punch list: (1) `test_verbosity_button_cycles` root cause = panel mutates the REAL shared `~/.arenamcp/settings.json`; fix in TEST layer only (monkeypatch settings instance per test), production persistence unchanged. (2) Verify compact_coach.py fully on disk — Worker D's last patch to the 'Conversation status line' region (~L124-126) FAILED to apply; re-apply any missing pieces. (3) Intermittent SIGSEGV stability: qapp-only fixture, keep widget references, RuntimeError guards, xfail+report if a test still segfaults. (4) 3 consecutive green runs + zero NEW coredumps (today's baseline mtgacoach coredumps: 22:05/22:17/22:19/22:21/22:22 — anything newer is a regression). tts_worker render-cancellation: DEFERRED by D (manager-side generation gating already discards stale renders); PTT delivered but real mic/whisper device validation pending (faster-whisper not importable in this venv → graceful-degradation path).
- [COMPLETE] Wave 2a Worker D (via completion agent) — punch list fully closed: (1) `test_verbosity_button_cycles` fixed in TEST layer (isolated_settings fixture; real settings.json untouched, verified by stat). (2) compact_coach.py verified COMPLETE on disk — the 'patch failed' warning was spurious; all pieces grep-verified (mode button L212-216, verbosity cycler L221-225/L469-473, stop button L228-232, status line L125-130, transcript toggle L170-173/L523-528, reply routing L531-532, CONVO_STATE label L536-544, PTT L267-274, sync-at-start L309). (3) SIGSEGV ROOT-CAUSED AND FIXED: eager `TtsManager.start()` in `CoachSession.__init__` spawned real Kokoro worker QProcesses per test; destructor crash (QProcess::~QProcess→kill→waitForFinished on half-destroyed wrapper). Fix = lazy-start in coach_session.py (BEHAVIORAL NOTE: TTS worker no longer warms up until first request_speech — first-utterance latency increases by warmup time; revisit before release). A weakref.finalize guard in tts_manager was tried and fully reverted (made it worse). (4) tts_worker render-cancellation DEFERRED (needs render thread + interrupt plumbing; manager-side generation gating covers staleness). Verification: 49 passed × ~20 consecutive runs (22:36-22:48), coredumps flat at 26 after 22:33.
- [pending] Wave 2b — Worker E (standalone.py wiring): instantiate VoiceSession + ConversationController, mode routing per slice semantics, `reset_for_match` at the three boundary sites, expose match_id to the controller, stop_speech target.
- [pending] Wave 2c — parent full-suite verification + grouped staging (pre-existing work already committed as 7b6d8ab/49f007f/2adb7c9 at Josh's request).
- [pending] Wave 3 — proactive commentary. Wave 4 — MageZero evidence. Wave 5 — adversarial review + thresholds.

## Security/incident notes

- 2026-09-15: 5 SIGSEGV coredumps from `.venv/bin/python` during desktop test runs (PySide6 GC during module exec) — generated KDE 'service crash' notifications; contained (dead pytest + ~29MB coredumps), root cause = Worker D's new Qt tests, fix in progress per punch list above. User was told directly.
- Worker C reported injected non-user instruction lines (Chinese, then German) appearing INSIDE its heredoc writes mid-task, without the OUT-OF-BAND marker; treated as SMB-mount write corruption per the established pattern, stripped, and reconstructed. Code verified green afterward. If this recurs on other workers, consider single-call write_file instead of shell heredocs for affected regions.

## Decisions

- D1: Local `.venv` is the test runner on this Linux checkout (after metadata repairs).
- D2: Pre-existing failures are out of scope; Wave-2 gate = baseline set unchanged + all new tests green.
- D3: SMB discipline per `synology-smb-python-editing` (ast.parse after writes; heredoc for whole-file writes).
- D4: Disjoint ownership; agents never commit; parent verifies then stages by file group.
- D5: Single canonical `ResponseIdentity` in conversation.py; voice/pipe treat it structurally or as JSON — no cross-imports.
- D6: PTT is a UI hold-button, not a global hotkey (F4 conflict); always-listening deferred per plan.
- D7: `realtime.py` (Azure) is orphaned — excluded from this feature, not deleted (unrelated-work preservation).
- D8: Wave-2 slice keeps CRITICAL trigger dispatch legacy even in conversation mode (decision machinery preserved); proactive topics deferred to Wave 3.
