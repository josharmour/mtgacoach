# Conversation Mode — Morning Test Guide (Wave 5 complete)

**Status: Waves 2-5 COMPLETE and COMMITTED (5 wave-gates passed, final suite 2268 passed / failure set byte-identical to the pre-feature baseline). Implementation is done; packaging and live-Arena validation remain pending per the plan's release boundary.**

## What to test first (in order)

1. **Mode switch mid-match** — launch the desktop app, flip Conversation ↔ Turn Advice while a match is running. No restart needed; the setting persists across launches (defaults restored to `turn_advice` / `balanced` — test pollution was scrubbed at 05:15).
2. **Typed questions** — in Conversation mode, ask "What's my plan?" / "Explain that" / "Why not attack?" — answers use the current board + conversation memory. Ask directly "what's my win chance?" — the answer must NOT state a probability (uncalibrated-evidence discipline; if it ever does, that's a bug).
3. **Interruption** — while the coach is speaking, type a question or press Stop. Speech should halt promptly (desktop now consumes the engine's speak_stop event). Ask a follow-up; one response plays at a time.
4. **Proactive commentary** — play a few turns; expect short (≤2 sentence) comments only on meaningful changes: new threats, role shifts (attacking→defending), material swings, plan drift. Same topic should NOT repeat inside ~4.5 min (3×90s window). Nothing meaningful → silence.
5. **Verbosity cycler** — Quiet mutes everything below threat-class; Balanced adds state-shift commentary; Detailed currently ≡ Balanced (no filler-tier topics exist yet — documented deferral).
6. **Turn Advice regression** — switch back to Turn Advice mid-match: legacy behavior is byte-for-byte (proven by the T5 parity harness against the pre-feature commit, not just asserted).
7. **PTT (optional)** — press-and-hold the PTT button: if faster-whisper/sounddevice are unavailable the button shows "voice input unavailable" (expected on machines without those deps); failures during a press now surface as [LOCAL FALLBACK] notices instead of silence.

## Known limits (documented, not bugs)

- PTT real-mic validation never ran (no faster-whisper in this venv) — first live use is the real test.
- TTS worker render-cancellation deferred: a superseded utterance's render finishes before being discarded (urgent interrupt latency = stale render time).
- Match-start "deck plan + how much discussion do you want" intro (plan's Intended-experience row 1) was never built — deferral candidate for a Wave-6 polish pass.
- First TTS utterance of a session pays worker warmup latency (lazy-start change from the segfault fix).
- Pipe-mode arbiter releases the speech channel on a ~50ms hand-off rather than true audio end.

## If something misbehaves

- Log: `%USERPROFILE%\.arenamcp\standalone.log` (engine), desktop UI log per AGENTS.md; check `conversation-mode-progress.md` first — it has the full ledger.
- Known environment quirk: ~1-2 flaky test failures per full-suite run under heavy load (match-packet isolation leak in pre-existing loop tests — ledger item, not feature code). Re-run before believing a failure.

## Deferred items ledger (not bugs — tracked)

1. Match-start plan intro (product feature, never scoped to a wave)
2. Detailed verbosity tier inert until a FILLER-class topic exists
3. `listening`/`speaking` CONVO states never emitted by the engine (only thinking/idle)
4. Always-listening voice (plan-deferred by design decision D6)
5. Worker-side render cancellation (needs render-thread + interrupt plumbing)
6. Match-packet test leak (~/.arenamcp/match_packets junk from loop tests — hygiene)
