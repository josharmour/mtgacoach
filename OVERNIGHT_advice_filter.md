# OVERNIGHT TASK — "Only speak on meaningful decisions" advice filter

**Run this autonomously, unattended, with max thinking. Do NOT ask the human
anything — make every decision yourself, implement, test, and commit. The human
is asleep.** This brief is fully self-contained; everything you need (exact
files/lines, design, tests, guardrails) is below — you should not need to
re-explore much.

---

## The problem (what the human observed)

`mtgacoach` desktop runs a headless coach (`src/arenamcp/standalone.py`) in
**advisory mode** (autopilot OFF) that generates strategic advice via the LLM
and speaks it via TTS. There's a setting `advice_frequency` with two modes:

- `start_of_turn` — advice once per turn. Too sparse: the human only heard the
  opening "Plan: …" line the entire match.
- `every_priority` — advice on every priority window. Too noisy: it pipes up on
  every trivial "Pass" / "Wait" window (e.g. opponent's turn with no instants),
  which is filler.

**Goal:** make the coach talk *frequently but only on MEANINGFUL decisions* —
i.e. when the human actually has a real choice — and stay silent on trivial
pass-only / nothing-to-do windows. Frequent, useful advice without the filler.
Trivial windows should be skipped *entirely* (no LLM call, no Coach-Log line, no
TTS) — not just muted at the TTS layer.

The infrastructure is ~80% there: there are already pure helper functions that
classify a window as meaningful; they just aren't gating the advice path.

---

## Architecture you're working in (grounded map — trust these line refs, verify before editing)

The advice pipeline lives in `src/arenamcp/standalone.py` (the headless coach)
and `src/arenamcp/coach.py` (advice generation). The desktop UI just renders
what standalone emits over a pipe.

### The trigger → advice loop (`standalone.py`, ~lines 2300–3016)
`coach.GameStateTrigger.check_triggers()` (coach.py:4459-4837) diffs game states
and returns trigger names (`decision_required`, `priority_gained`, `new_turn`,
`opponent_turn`, `combat_attackers`, `combat_blockers`, `stack_spell_yours`,
`stack_spell_opponent`, `land_played`, `spell_resolved`, `low_life`,
`threat_detected`, …). The main loop iterates them and runs each through a gauntlet
of suppression checks before calling `get_advice()`.

Key existing points (DO NOT regress these — they handle dedup/stale/boundary):
- `CRITICAL_PRIORITY` set @ standalone.py:1795 — always-fire triggers
  (`decision_required`, `low_life`, `opponent_low_life`, `threat_detected`,
  `losing_badly`, `stack_spell*`). `is_critical` overrides the frequency gate.
- Suppression 1 (stale after match boundary) @ 2420-2433.
- Suppression 2 (duplicate 'Action Required' same turn+phase) @ 2498-2507.
- Suppression 3 (turn-ownership: rename new_turn→opponent_turn off-turn; drop
  combat_attackers off-turn / combat_blockers on-turn) @ 2609-2622.
- Suppression 4 (step-by-step triggers when a pending decision exists) @ 2630-2649.
- **Suppression 5 — the `advice_frequency` gate** @ 2651-2656:
  `is_frequent = (advice_frequency == 'every_priority' AND trigger in
  [priority_gained, combat_attackers, combat_blockers] AND (turn_num >
  last_advice_turn OR phase != last_advice_phase))`.
- Suppression 6 (priority_gained when new_turn in same batch) @ 2658-2661.
- Suppression 7 (non-critical pre-empted by decision_required in batch) @ 2663-2666.
- Suppression 8 (duplicate unresolved decision by signature) @ 2673-2689,
  signature builder `_build_pending_decision_signature` @ 604-627.
- Suppression 9 (stack active, no castable instants → "Quiet") @ 2697-2744,
  using `_has_castable_instants()` (coach.py:4405-4457).
- `should_advise = is_critical OR is_new_turn OR is_opponent_turn OR
  is_step_by_step OR is_frequent` @ ~2668.
- `get_advice()` invoked @ 2865-2869 (advisory path).
- Stale-advice re-check after the LLM returns @ 2872-2924 (turn drifted while
  the LLM was thinking → discard).
- Empty/error-advice suppression @ 2940-2972.
- `speak_advice()` (TTS) @ standalone.py:928-969 — already has a heuristic
  `silence_triggers = ['wait','pass','pass priority','no actions',
  'wait for opponent','opponent has priority']` + action-verb check that mutes
  short passive lines. Tested by `tests/test_standalone_speak_advice.py`.
- State updated (`last_advice_turn/phase`, decision sig, record) only AFTER
  delivery @ 2984-2999.

### The helpers that already classify meaningfulness (REUSE THESE) — `standalone.py:851-913`
- `_is_meaningful_legal_action(action)` (@staticmethod) — True if the action
  string starts with `Cast `, `Play `, `Activate Ability`, `Action: Activate`,
  `Action: Attack`, `Action: Block` (verify exact prefixes in code).
- `_has_meaningful_local_action_window(game_state)` (@classmethod) — True when
  it's the local player's window AND there's a pending decision OR a meaningful
  legal action.
- `_has_actionable_priority_window(game_state)` — related actionable check.
- `_has_castable_instants(game_state)` (coach.py:4405-4457) — untapped mana vs
  hand instants/flash.

### Game-state signals available at each decision
`game_state` dict keys: `turn` (`active_player`, `priority_player`,
`turn_number`, `phase`, `step`), `players` (`seat_id`, `is_local`,
`life_total`), `legal_actions` (list of readable strings like
`['Cast Bolt [ok]', 'Wait']`), `pending_decision` (str or None;
`'Action Required'` is the generic priority one), `decision_context` (dict with
`type`: mulligan/scry/discard/target_selection/modal_choice/declare_attackers/
declare_blockers/pay_costs/numeric_input/…), and bridge overlays
`_bridge_can_pass`, `_bridge_request_type`. coach.py:1513 emits
`'NONE — say "pass priority"'` when legal moves are empty;
`_post_filter_uncastable_legal_moves` (coach.py:2493-2515) can reduce a window
to no real casts.

---

## The fix (design — refine as you see fit, but stay within these intentions)

Add a **single "meaningful window" gate** in the trigger loop so that, for
*non-critical* triggers, advice is only generated/spoken when the window has a
real choice. Concretely:

1. **Primary gate at generation time** (preferred over muting at TTS): in the
   trigger loop (around standalone.py:2651-2744, after turn-ownership filtering
   and before `should_advise`/`get_advice`), if the trigger is non-critical and
   the window is **trivial**, `continue` (skip) with a debug log. Trivial =
   NOT `_has_meaningful_local_action_window(curr_state)` AND no pending decision
   AND (pass-only priority OR opponent priority with `not _has_castable_instants`).
   This means trivial windows make **no LLM call, no Coach-Log line, no TTS**.
2. **Redefine `every_priority` to mean "every *meaningful* priority."** That is
   exactly the behavior the human wants: frequent advice, minus the Pass/Wait
   filler. (Keep `start_of_turn` working as-is for users who want it quiet.)
   Consider whether a third explicit value is cleaner (e.g.
   `meaningful` / `every_meaningful_decision`) — if you add one, default the
   human's current setting (`every_priority`, already set in
   `~/.arenamcp/settings.json`) to the new meaningful behavior so it "just
   works" on next launch without them changing anything. Keep the F3 toggle
   (@5153) cycling through the available modes.
3. **Always keep CRITICAL/meaningful triggers firing**: `decision_required`
   with a real `pending_decision` (targets/scry/discard/mulligan/modal/
   pay_costs/declare_*), `low_life`, `threat_detected`, etc. These are
   meaningful by definition — never suppress them with the new gate.
4. **Bias toward speaking when uncertain.** Missing a real decision is worse
   than an occasional redundant line. If you can't confidently classify a window
   as trivial, treat it as meaningful.
5. Optionally fold the existing `speak_advice` `silence_triggers` heuristic into
   the same meaningful-window decision for consistency (belt-and-suspenders),
   but the *primary* gate is at generation time.

Edge cases that MUST stay meaningful (speak): your main phase with any castable
spell / playable land / legal attack / activatable ability; any real pending
decision; lethal/at-risk life totals. Edge cases that should go quiet (skip):
pass-only priority ("Priority (Pass Only)" / only `Pass`/`Wait` legal); opponent
turn with no castable instants; empty legal moves with no pending decision.

---

## Autonomous testing (this is how you PROVE it works without the GUI)

You CANNOT drive the live MTGA GUI overnight. Verify via **pure-function unit
tests** over crafted `game_state` dicts. This is sufficient and expected.

- Create `tests/test_meaningful_advice_filter.py` (remember: `tests/` is
  gitignored — you MUST `git add -f tests/test_meaningful_advice_filter.py`).
- Use the fixture pattern from `tests/test_coach_advice_matching.py:17-36`
  (`_make_state(legal_actions, hand=...)` builds the dict with `players`,
  `turn`, `hand`, `battlefield`, `legal_actions`, etc.). Match the exact dict
  shape the filter functions read.
- Cover this matrix (each → expected: SPEAK or SKIP):
  - your turn, `legal_actions=['Cast X [ok]','Wait']` → SPEAK
  - your turn, `legal_actions=['Play Land: Forest','Wait']` → SPEAK
  - your turn, `legal_actions=['Wait']` (pass-only) → SKIP
  - opponent turn, no castable instants → SKIP
  - opponent turn, you hold a castable instant → SPEAK
  - pending decision `target_selection` / `scry` / `discard` / `mulligan` → SPEAK (even if legal_actions sparse)
  - `pending_decision='Action Required'`, only Pass → SKIP
  - lethal/low_life situation → SPEAK
- Test the gate function directly (factor the trivial/meaningful decision into a
  single pure, testable function fed a `game_state` dict if it isn't already —
  the existing `_has_meaningful_local_action_window` is a good base/staticmethod
  to call directly, e.g. `StandaloneCoach._is_meaningful_legal_action(a)`).
- Keep `tests/test_standalone_speak_advice.py` green.
- For headless instantiation if needed: `StandaloneCoach.__new__(StandaloneCoach)`
  and set attrs manually (pattern in `tests/test_standalone_draft_hud.py:54-64`),
  but prefer pure @staticmethod/@classmethod calls that need no instance.

### Verify commands (must be green before you call it done)
```
.venv/bin/python -m py_compile src/arenamcp/standalone.py src/arenamcp/coach.py tests/test_meaningful_advice_filter.py
.venv/bin/python -m pytest tests -q
```
`tests/test_gre_bridge_read_timeout.py` is KNOWN-FLAKY (threading/timing) — if it
fails, re-run it in isolation; do not let it block you. Everything else must pass.

---

## Guardrails (hard requirements)

- Keep the full suite green and `py_compile` clean for every touched file.
- Do **not** regress the 9 existing suppression points or the critical-trigger
  behavior, the stale-advice check, or empty/error suppression.
- Do **not** touch the LiteLLM gateway, the online/ProxyBackend path, autopilot
  execution, or the GamePlan layer. This is purely advisory-mode advice gating.
- Commit each working, test-green change with a clear message. **No
  Co-Authored-By lines** (repo convention). `tests/` and some other paths are
  gitignored-but-force-tracked — use `git add -f` for new test files.
- Be conservative on the "speak when uncertain" bias — better slightly chatty
  than silent on a real decision.

## Definition of done
1. Trivial windows (pass-only, opponent-turn-no-instants, empty-no-pending)
   produce **no** advice: no LLM call, no Coach-Log line, no TTS.
2. Meaningful windows (real choices + critical triggers) still produce advice
   exactly as before, and `every_priority` now means "every meaningful window."
3. `tests/test_meaningful_advice_filter.py` covers the matrix above and passes;
   full suite green; `py_compile` clean. Existing speak_advice test still passes.
4. Changes committed with clear messages (no Co-Authored-By).
5. Append a short **"## RESULT"** section to the BOTTOM of this file summarizing:
   what you changed (files+functions), the new/changed `advice_frequency`
   semantics, test results, and any decisions/uncertainties — so the human can
   read it in the morning and then do the only remaining step (launch the desktop,
   play a Sparky match, confirm frequent-but-relevant advice). Live GUI
   verification is the human's job; do not attempt it.

## Suggested orchestration (you're in ultracode)
1. Understand: re-confirm the gate location + the existing helper signatures
   (this brief's line refs) — read the actual code before editing.
2. Design: a short judge/decision on exactly where the gate hooks and the
   trivial-vs-meaningful predicate (reuse `_has_meaningful_local_action_window`
   + `_has_castable_instants` + pending-decision check).
3. Implement on `standalone.py` (+ tiny `coach.py` helper if cleaner) + the new
   test file. Verify (py_compile + pytest) per change.
4. Adversarially review: does any path now silence a REAL decision? Add a test
   for it. Loop until the matrix is airtight.
