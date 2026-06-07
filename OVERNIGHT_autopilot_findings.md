# Overnight autopilot findings (2026-06-07, while you slept)

You asked me to fix autopilot autonomously overnight. Here's the honest scope I
worked within and why, what I **did** fix and verify, and a prioritized,
evidence-backed plan for the rest — which needs **you + live MTGA** to verify.

## The boundary I held (please read)

Autopilot is **execution** code (it submits real GRE actions). I cannot drive
live MTGA unattended, and every failure below was observed only when *you*
toggled AP on during play. Blindly rewriting submission/timing internals I can't
run would risk leaving autopilot **worse** than it is now, with no way for you to
catch it until morning. So I fixed only what is **pure logic + unit-testable**,
and for everything else I wrote this diagnosis instead of guessing. Nothing
unverified was shipped. Full suite: **257 passed**, py_compile clean.

## What I fixed and verified (committed)

**Bridge DeclareBlockers enrichment now includes blocker names**
(`src/arenamcp/gre_bridge.py`, commit `20b31b7`).
- Tonight I added `legal_blocker_ids` to the bridge block data (so block advice
  got specific — confirmed live: *"block the Raging Regisaur with both…"*). But
  I'd omitted the paired `legal_blockers` **names**, and autopilot's
  `_build_blocker_id_map` (autopilot.py:6259) requires `len(names)==len(ids)`.
  So for bridge-authoritative blocks it returned `{}` and autopilot **could not
  map a block-by-name**.
- Fix: populate `legal_blockers` paired 1:1 with `legal_blocker_ids` (resolved
  from the battlefield; stable per-id fallback when a name isn't present),
  matching the log-path shape in `gamestate.py:3242`.
- Verified by unit test: `_build_blocker_id_map` now returns
  `{'Llanowar Elves': 10}` from bridge data (was `{}`).
- **Still needs live confirmation** that autopilot then submits the block
  correctly end-to-end — I can only prove the map is built.

## Issues found in tonight's log — NOT fixed (need live repro to verify)

Evidence pulled from `~/.arenamcp/standalone.log`. Each AP-on burst
(23:47, 23:49, 23:56, 00:11) reproduced the *same* cascade.

### 1. `Action verification timed out after 0.01s` (HIGH — suspicious)
- Evidence: repeats every AP run (e.g. line 24006, 24257, 26001).
- `verification_timeout` defaults to **2.5s** (autopilot.py:187) and **nothing
  in the current source sets it to 0.01** — I grepped exhaustively. So either
  the running process predated a change, or there's an override path I couldn't
  find statically.
- Why I didn't "fix" it: I can't reproduce 0.01 from the code, so any change
  would be a blind guess at a value the source doesn't contain. **Action for
  you:** when you next run AP, check the live `_config.verification_timeout`
  (or paste me the AP init lines) so I can find the real source before touching
  it.

### 2. `MANUAL REQUIRED: Unmapped GRE interaction … bridge='MysteryReq'` (HIGH)
- Evidence: lines 24244, 24899, 25988. `pending='Manual Required'`,
  `type=unmapped_interaction` (autopilot.py:1694).
- `"MysteryReq"` is **not** a string in our source — it's a runtime request
  type/class the plugin reported and the bridge couldn't map. To add a mapping
  I need to know what GRE request it actually is. **Action for you:** when it
  recurs, grab the `BepInEx/LogOutput.log` line naming the real request class,
  or tell me what was on screen — then I can add an explicit bridge branch
  (like the DeclareBlockers one).

### 3. `cast_spell (Shock)` / `play_land (Forest)` bridge-match cascade (HIGH)
- Evidence: lines 24248–24256 etc. — `GRE bridge match failed for cast_spell`,
  then `Game advanced past play_land`, `Blocking action for current window
  (failure 1/5) … bridge game_state_id stayed at 123`.
- The bridge couldn't match the planner's chosen action to a pending GRE action
  and the game state never advanced (`game_state_id stayed at 123`), so AP got
  stuck retrying. This is the core "AP gets stuck" symptom. Fixing it means
  reproducing the exact pending-request vs chosen-action mismatch live —
  classic bridge-matching work that I can't verify offline.

### 4. `declare_attackers: confirming with no attackers (Done)` with attackers available (MEDIUM)
- Evidence: 23:37:56, while legal had `Llanowar Elves x3, Badgermole Cub`.
- Root cause is **upstream**: that path (autopilot.py:3741) only fires when
  `action.attacker_names` is empty — i.e. the **planner** returned no attackers
  (or auto-confirm fired without picking any). So this is a planning/auto-confirm
  decision, not a bug in the submit code. Candidate fix: when a DeclareAttackers
  decision has legal attackers and the planner returns none, don't silently
  auto-confirm "no attacks" — re-plan or default to the combat-solver's
  `optimal_attacks`. Needs live verification it doesn't over-attack.

### 5. `select_target: name lookup failed for ['Escape Tunnel']; using sole bridge candidate 20` (LOW)
- Evidence: 23:47:52, 23:49:59, 00:11:56. Self-recovers via the sole-candidate
  fallback (autopilot.py:4041), so not broken — just a name-resolution miss
  (likely a land/nonstandard card not in the name map). Cosmetic.

## Suggested order for the morning (with you driving a live match)
1. **#2 MysteryReq** — cheapest high-value: identify the request, add a bridge
   branch. You grab one log line; I implement + test.
2. **#1 verification 0.01s** — confirm the live value; likely a one-line config
   fix once the source is known.
3. **#3 bridge-match cascade** — needs a captured pending-request payload from a
   stuck window; then I can add the matching path.
4. **#4 no-attackers auto-confirm** — wire the combat solver's `optimal_attacks`
   as the default when the planner declines with attackers available.

I left autopilot execution otherwise untouched. The advisory-mode work from
earlier tonight (meaningful-window filter, block-data combat advice, named
combat phrasing, pass/wait filler suppression, plan-recitation gating) is all
committed, tested, and — except where noted — confirmed live.
