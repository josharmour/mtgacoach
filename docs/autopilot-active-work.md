# Active Coach work queue

**Only these 40 items are active:39numbered bugs plus CI-PERF (two failing tests).**
The machine-readable authority is [autopilot-active-work.json](autopilot-active-work.json).
This is a task queue, not a statement that39distinct defects have been reproduced.
Many latency rows are recurrences of shared mechanisms; reuse fixes and evidence.

## Scope and dispositions

Membership is exactly the `partial_or_open_mixed_scope` group from the 2026-11-01
cumulative 77-record reconciliation. The user retired 20 prerequisite/ambiguous
records as OBSOLETE and accepted 18 scoped repairs as FIX_ASSUMED on 2026-11-02.
Neither group is active. Preserve their reports; reopen an assumed-fixed ID only
on recurrence. Reopening an obsolete scope requires an explicit user decision.

Active bug IDs: #141, #210, #223, #231, #232, #235, #239, #241, #247, #248, #249, #253, #254, #256, #260, #264, #265, #266, #269, #270, #273, #275, #276, #277, #278, #283, #284, #285, #286, #289, #300, #306, #307, #308, #309, #317, #318, #329, #350.

The maintainer keeps the original reports in the historical `autopilot-improvements.md` ledger outside this repository. The source/test locator
index is [backlog-evidence-2026-11-01.md](backlog-evidence-2026-11-01.md).
Old ledger statuses are history; they cannot expand this queue. Do not read the
entire archive into every task: find the selected ID and its later recurrences.
Private captures remain local; never upload them or credential-bearing logs.

## Sequential implementation contract

1. Read this file andJSON. Check for an already-running owner before starting.
   CI-PERF is first; the 39 gameplay items are not authorized to start in the current
   run. A later explicit instruction to work through this queue activates them.
2. Work on one item at a time in an isolated branch/worktree. Record the base,
   owner/session, exact reproduced scope and `in_progress`. Inspect current code
   first; old incidents may already be repaired. Do not recreate merged patches.
3. Implement a bounded correction. Before merge, run the relevant regression or
   reproduction plus quick checks on changed files. Reuse valid existing receipts;
   **do not run the full suite per item**. Do not repeatedly rerun unrelated failures.
   Classify each failure as caused by this change, known baseline, or unknown; record
   the evidence, and investigate a relevant unknown before claiming the fix is sound.
   Preserve meaningful
   assertions, deadlines and 480 ms CI limits. Record commands, actual results,
   before/after evidence and limitations under `docs/backlog-receipts/<item>/`.
   Use sanitized small receipts; do not commit captured private game reports.
4. Commit the fix/tests and mark `implementation_reported_complete`. Record exact
   source commits and test receipts. This is the implementer's claim, not verification.
5. Merge each completed item into master **before starting the next item**. Check
   current branch/index and preserve unrelated edits. User authorizes per-item
   merges, not force-push, deployment, security changes or app interruption.
   If master is the editable launch checkout, verify Coach is stopped on BOTH
   Mac and Windows/shared consumers before changing its files. Never assume this
   from old messages. If running, hold that item at the merge gate and report the
   exact safe-stop requirement; do not silently restart or start the next item.
   Never interrupt Arena or inject a bridge during a match. Native DLL activation
   requires its own safe unload window. Headless tools without write/approval
   capability must report that constraint, not bypass it.
6. Record the actual master commit, ancestry/patch-equivalence proof and
   `merged_awaiting_review`, then proceed sequentially without waiting for review.
   Remote publication is a separate receipt; local merge is not a GitHub push.
   If no code change is needed, retain a bounded current-source regression receipt
   and a master documentation/test commit before progressing; do not manufacture a fix.
7. Any independent reviewer chosen by the user can later mark `verified` or
   `needs_follow_up`. Implementation must never set its own verification status.
   Review is a bounded on-demand pass, with no monitor, schedule or provider tie.
   Keep code verification distinct from live gameplay qualification. A restart
   loads Python changes only when the launcher points to that checkout; packaged
   Windows installs may need an explicit update. Never claim a merge alone changed
   the running process.

## States and required receipts

Implementation: `queued` → `in_progress` → `implementation_reported_complete` →
`merged_awaiting_review`; use `needs_follow_up` with an exact blocker when necessary.
Independent verification: `not_reviewed`, `verified`, or `needs_follow_up`.
Record reviewer identity/date, reviewed master hash, inspected tests, findings,
coverage gaps and receipt. Verification is always scoped; live state is separate
(`not_exercised`, `observed`, `failed`, or `not_applicable`). Never erase prior receipts.

## Separate future proposal — not part of this queue

Consider a later test-suite maintenance project: test selection, isolation,
performance measurement stability and runtime. No work on that proposal is started
or included in the 40 items. Test count alone does not establish why prior work stalled.

## Portable implementation prompt

> Read docs/autopilot-active-work.md and docs/autopilot-active-work.json. Work only
> on the first eligible item, sequentially. Reuse current fixes, reproduce the
> remaining scope, implement and run only the relevant regression/repro and quick changed-file checks
> without weakening thresholds. Do not run the full suite per item; reuse valid receipts
> and distinguish regressions from baseline or unknown failures.
> Record commit/test evidence, safely merge the completed item into master, then
> continue to the next authorized item. Do not self-mark verified. Preserve active
> apps, user edits and private data; if a safe merge is blocked, record the exact
> blocker and stop rather than skipping ahead. No push or deployment is implied.

For the current CI-only run, add: **Only CI-PERF is authorized; stop after its handoff/merge.**

## Portable independent verifier prompt

> Read docs/autopilot-active-work.json. Review only completed, unreviewed items
> against actual master history, diffs and their recorded test receipts. Confirm
> the implementation is present, its claimed scope and relevant negative controls;
> run only small additional checks needed to resolve a specific gap. Do not expand
> scope, fix code, start jobs or poll. Record reviewed commit, reviewer, evidence,
> findings and gaps, then set independent verification to verified or needs_follow_up.
> Keep live qualification separate and stop after this bounded review pass.

## Current handoff

No implementation job or review monitor is running. The CI-only headless attempt
was stopped at a sandbox/worktree setup blocker; no candidate was produced. Read
[CI-PERF handoff](backlog-ci-handoff.md) before starting. The user can start one
DeepSeek task with the implementation prompt and explicitly authorize all 40 items.
There is no dependency on this chat or any particular reviewer/provider.

### Progress (2026-11-02, session-965ac31a)

Sequential implementation is under way on local master; nothing is pushed or
deployed.

| Order | Item | State | Master commit | Receipts |
|---|---|---|---|---|
| 1 | CI-PERF | merged_awaiting_review | c66155c (queue 636459f) | docs/backlog-receipts/CI-PERF/ |
| 2 | BUG-141 | merged_awaiting_review | 264334b (merge f72c411) | docs/backlog-receipts/BUG-141/ |
| 3 | BUG-210 | merged_awaiting_review | 5194398 (merge 39fc230, no source change) | docs/backlog-receipts/BUG-210/ |
| 4 | BUG-223 | merged_awaiting_review | 409749f (merge a46ed56, tests only) | docs/backlog-receipts/BUG-223/ |
| 5 | BUG-231 | merged_awaiting_review | 429fac1 (merge cd578a0) | docs/backlog-receipts/BUG-231/ |
| 6 | BUG-232 | merged_awaiting_review | ec4836b (merge b63ff82, tests only) | docs/backlog-receipts/BUG-232/ |
| 7 | BUG-235 | merged_awaiting_review | a69830e (merge ac32e8a, tests only) | docs/backlog-receipts/BUG-235/ |
| 8 | BUG-239 | merged_awaiting_review | 66e2d16 (merge 9018179) | docs/backlog-receipts/BUG-239/ |
| 9 | BUG-241 | merged_awaiting_review | e972314 (merge 895d9fa, tests only) | docs/backlog-receipts/BUG-241/ |
| 10 | BUG-247 | merged_awaiting_review | 3e0f415 (merge 2c0691f) | docs/backlog-receipts/BUG-247/ |
| 11 | BUG-248 | merged_awaiting_review | 87faace (merge acee2d6, tests only) | docs/backlog-receipts/BUG-248/ |
| 12 | BUG-249 | merged_awaiting_review | 93bd0bf (merge 6a40f07, tests only) | docs/backlog-receipts/BUG-249/ |
| 13 | BUG-253 | merged_awaiting_review | 813f252 (merge 43679e3, tests only) | docs/backlog-receipts/BUG-253/ |
| 14 | BUG-254 | merged_awaiting_review | 6cddacc (merge 712db15, tests only) | docs/backlog-receipts/BUG-254/ |
| 15 | BUG-256 | merged_awaiting_review | dadd9bc (merge 5a38627, no source change) | docs/backlog-receipts/BUG-256/ |
| 16 | BUG-260 | merged_awaiting_review | 507de46 (merge dae4f06, no source change) | docs/backlog-receipts/BUG-260/ |
| 17 | BUG-264 | merged_awaiting_review | 3ef8919 (merge 3303c4d, no source change) | docs/backlog-receipts/BUG-264/ |
| 18 | BUG-265 | merged_awaiting_review | c6eb3d7 (merge d94cf56, no source change) | docs/backlog-receipts/BUG-265/ |
| 19 | BUG-266 | merged_awaiting_review | 03c6439 (merge 2c49ed9, no source change) | docs/backlog-receipts/BUG-266/ |
| 20 | BUG-269 | merged_awaiting_review | 33e93e4 (merge 34f12f8, no source change) | docs/backlog-receipts/BUG-269/ |
| 21 | BUG-270 | merged_awaiting_review | a87c21a (merge 8c3d7bf, no source change) | docs/backlog-receipts/BUG-270/ |
| 22 | BUG-273 | merged_awaiting_review | 2ad966e (merge ca5ef9f, no source change) | docs/backlog-receipts/BUG-273/ |
| 23 | BUG-275 | merged_awaiting_review | 618ec02 (merge 7693b3c, no source change) | docs/backlog-receipts/BUG-275/ |
| 24 | BUG-276 | merged_awaiting_review | 8ff78b7 (merge 855129d, no source change) | docs/backlog-receipts/BUG-276/ |
| 25 | BUG-277 | merged_awaiting_review | cde678c (merge 4da04b3, no source change) | docs/backlog-receipts/BUG-277/ |
| 26 | BUG-278 | merged_awaiting_review | c00bfad (merge cb60432, no source change) | docs/backlog-receipts/BUG-278/ |
| 27 | BUG-283 | merged_awaiting_review | dde78f7 (merge fa0e292, no source change) | docs/backlog-receipts/BUG-283/ |
| 28 | BUG-284 | merged_awaiting_review | e78789a (merge 4be2eaf, no source change) | docs/backlog-receipts/BUG-284/ |
| 29 | BUG-285 | merged_awaiting_review | 8f595f3 (merge 6ad4b01, trace scoping) | docs/backlog-receipts/BUG-285/ |
| 30 | BUG-286 | merged_awaiting_review | 0cdf55e (merge 78acf87, regression test only) | docs/backlog-receipts/BUG-286/ |
| 31 | BUG-289 | merged_awaiting_review | 6c43157 (merge 46ffe24, regression test only) | docs/backlog-receipts/BUG-289/ |
| 32 | BUG-300 | merged_awaiting_review | 1b1443c (merge ed82854, draft tutor-outcome fact check) | docs/backlog-receipts/BUG-300/ |
| 33 | BUG-306 | merged_awaiting_review | 44a81f7 (merge 8ddeda1, submission-leg regression only) | docs/backlog-receipts/BUG-306/ |
| 34 | BUG-307 | merged_awaiting_review | 2d058c6 (merge 407441d, removal valuation keeps the returning engine) | docs/backlog-receipts/BUG-307/ |
| 35 | BUG-308 | merged_awaiting_review | 5758e9b (merge f0f50c8, verification only: both orders, enemy response, once-per-turn and protection already pinned) | docs/backlog-receipts/BUG-308/ |
| 36 | BUG-309 | merged_awaiting_review | 5ff82b9 (merge 7fcca7f, verification only: intent carry, phase note and Main2 claim check already merged; planner hop pinned) | docs/backlog-receipts/BUG-309/ |
| 37 | BUG-317 | merged_awaiting_review | fa1dccd | docs/backlog-receipts/BUG-317/ |
| 38 | BUG-318 | merged_awaiting_review | 518d7af | docs/backlog-receipts/BUG-318/ |
| 39 | BUG-329 | merged_awaiting_review | d8fc11f | docs/backlog-receipts/BUG-329/ |
| 40 | BUG-350 | merged_awaiting_review | 83f1fbf | docs/backlog-receipts/BUG-350/ |

Implementation queue complete. All 40 items await independent review. Live qualification is recorded separately.
BUG-210 needed no source change: the merged repairs already cover its acceptance
points, and the receipt records the current-source evidence plus the remaining
conservative limits. BUG-223 also needed no production source change: the lane,
supersede/drop and priority mechanisms already existed, so the remaining scope
was the acceptance-required measurement fixture (queue vs setup vs generation in
one metrics record, urgent budget, cancellation, replacement completion).
BUG-231 added instrumentation only: one decision-scoped trace now ties its
requests (queue/first-token/generation/cancellation) to the action that was
submitted, so a masked land/pass fallback names its delay. No deadline,
freshness or fallback logic changed; live qualification of the historical stall
is still outstanding. A follow-up repair (cf7ad72) makes `_decision_suffix` tolerate engines built
without `__init__`; eight block-declaration tests had failed on master because
the suffix read attributes only `__init__` sets. BUG-232 also needed no production source change: the
retained c63678d8 mechanism is already repaired by b352b8a (a parser-usable
declaration wins and the other leg is cancelled) and 70d9e2f (per-leg cancel
events reach the backend call site), so the remaining scope was the combat
regression plus the planner's effort/elapsed timing record. The harmful
fallback strategy stays a separate cause. BUG-235 also needed no production
source change: 6194d8a already added the one bounded repair inside the first
call's deadline and the deterministic dependent-step cleanup, and 11a5f19 put
the per-turn mana budget into generation; coloured pips, commander tax and
sequential spending were already validated. The uncovered half was the cleanup
itself, now pinned by tests/test_plan_mana_dependencies_235.py together with the
evidence loop that returns drop issues as PREVIOUS VALIDATION. Restricted mana
("spend only to cast artifacts") is still unmodelled and counts as ordinary
coloured mana — over-permissive, not the #235 symptom. BUG-239 did need a
production change: Arena re-offers an unanswered prompt as a new GRE message, so
`request_key` saw three new requests in the 10:43:12 trace and the urgent-clock
branch answered the same OptionalAction three times in six seconds. The watchdog
now keeps the identity of the question its last safe default answered and
escalates a repeat inside `SAME_PROMPT_GUARD_S` to its alert instead of
submitting again; a stuck prompt still gets a bounded retry after the window and
any different question still gets its safe default. No typed OptionalAction
handler was added — deciding an optional effect stays with the model. BUG-241 also
needed no production source change: a pass recorded while the model was unavailable
is never a strategic hold (70d9e2f) and a pass only counts on the board it was made
on (eb72e38), so the timed-out re-check that kept a fallback pass over a fresh legal
choice can no longer happen. The remaining scope was the end-to-end planner
regression, which also pins that the re-check's budget is clamped to the first
call's deadline and so cannot renew it. A malformed answer (`bad_answer`) still
counts as a hold by design; only a model-server failure makes a pass non-strategic.
BUG-247 did need a production change, but not in the solver: `combat_solver.evaluate_attack` already scored a tenth poison counter and an emptied library as a win, while `combat_strategy._search` scored life damage only, so a toxic attacker at nine poison or a miller with three cards left was valued at one damage and could be held (reproduced: score 0.00, assignments `{}`). The declaration search now reads both `LossState`s, counts poison and mill only for player recipients, scores a new loss as 10000 and a newly losing crackback as -1000 + material, passes our loss state into the crackback `guarded_block_plan`, and includes the `zones` library counts in its memo key. Abstentions are deliberate: an unknown library count cannot score a mill win, damage to a planeswalker is not a player hit, and only printed unconditional toxic/poisonous/infect and mill text is recognized — activated grants, conditional triggers and replacement effects stay out, so obsolete #281 is not revived.
BUG-248 also needed no production source change: the retained "LLM streaming
time budget exhausted" is the proxy's own cut, which is already one attempt at
the budget, never retried, classified as slow-but-alive (circuit closed, health
degraded), turned into `llm_unavailable` by the planner inside its deadline and
replaced by the board-math line without escalating. The remaining scope was the
regression that pins that composition on this exact error, including the #231
trace decomposition (queue wait, first token, generation cut at the budget,
attempts). The post-cast six-second waits in the same trace are #206/#243 and
are not re-opened.
BUG-249 also needed no production source change: the retained "no first token /
budget timeout" pair is the proxy's own cut on a stream that was alive but never
produced an answer, and the first plan that outlived its turn is already bounded.
On current master that cut is one attempt at the budget, never retried,
classified as slow-but-alive (circuit closed, health degraded), and every hosted
plan attempt made while no plan exists runs at effort `none` inside the 20 s
initial budget, so repeated cuts cannot escalate into another full-effort call
that will be cancelled again. The remaining scope was the regression that pins
the distinction the trace turns on — reasoning satisfies the first-token deadline
while producing no content (`ttft_ms` set, `first_content_ms` null, zero content
characters) — plus the no-escalation invariant across three consecutive cuts.
The malformed blocker JSON in the same trace belongs to the parser stream and is
not re-opened.
BUG-253 also needed no production source change: a schema-valid answer that carries
a role and no plan content is already rejected by `_usable_plan` exactly like an
unparseable one, so the prior plan survives, the rejected text is logged, and the
reason reaches both `plan_status()` and the prompt as `PLAN NOT REFRESHED: … (no
usable plan from the model)`. A call the background lane never made (server down,
circuit open, dropped/superseded) is classified separately as `call not made
(model server unavailable)` and is never retried, and a reform whose turn passed
discards its answer instead of overwriting the plan. The "initial plan is
missing" half is bounded by the fast first-plan path: while no plan exists every
hosted attempt runs at effort `none` inside the 20 s initial budget, so a missing
first plan cannot escalate into a 75 s deep call. The remaining scope was the
regression that pins those four answers and the reported reason. The archived
transcript could not be replayed — both cited commits are absent from this
checkout and the queue's evidence-document pointer has no #253 row — so the
receipt records the mechanism reproduction instead.
BUG-254 also needed no production source change: both reported boards are already
closed on master by the counted subtype mana (`{T}: Add {C} for each Halfling you
control`, one per ready source) and the Channel self-discount strip in
`exact_mana_total`, so `affordable_x` prices Goldvein Hydra `{X}{G}` at X=7 on the
recorded 8 mana and Nature's Rhythm `{X}{G}{G}` at X=0 on the recorded 2 mana, and
`filter_play_options` withholds the reported X=10 and X=1 before the model sees them.
The remaining scope was the regression that replays both boards through that live
withholding path; a mutation check confirms either counting control is load-bearing.
One bounded gap is recorded rather than fixed: an activated ability whose cost
contains {X} is still uncapped because `affordable_x` reads the permanent's printed
`mana_cost`, and no reported case has that shape.
BUG-256 also needed no production source change: the five controls its triage row
names are merged and pinned on master — the proxy clamps the call budget to the
client hard timeout and closes a cut stream, a first-token timeout is never retried
and is recorded as `ttft_timeout`, the typed hedge returns the first usable answer
with a bounded doubt allowance, the declaration hedge returns the first answer the
parser accepts as legal actions and cancels the other leg, every hosted attempt made
while no plan exists runs at effort `none` inside the 20 s initial budget, and a
planner call whose waiter gives up has its own cancel token set so it stops competing
with the next live request. The remaining scope was verification: the 17 audit node
IDs pass (27 passed), and the decision-trace and model-down fallback files pass (38
passed), covering the queue/first-token/generation/cancellation timing record and the
legal fallback after a stalled decision. Two bounded gaps are recorded rather than
fixed: the unavailable-card strategy half belongs to #240 and the closed attack window
to #230/#243 (neither is in this queue), and decision-chain accumulation belongs to
execution follow-up f9b88c8. The retained trace `9dc1c55c` is absent from this
checkout, so this is mechanism verification on current master, not an exact replay.
BUG-260 also needed no production source change: its reported mechanism is
"15–21 s stalls without a first-token timeout", and on master every decision path
passes one — typed decisions use `first_token_budget(8.0, prompt chars)` with the
budget sized to keep answer time, plan and combat declarations use
`_plan_first_token_timeout`, and advice calls keep 2 s inside their API timeout.
That wiring is what closes the stall, because the proxy's post-first-token stall
guard deliberately does not apply before the first token (`read_limit` only adds
`stall_s` once `token_seen`), so without a first-token deadline a stream that never
starts waits out the whole budget. The typed hedge returns the first usable answer
with a bounded doubt allowance, and a planner call whose waiter gives up has its own
cancel token set. The remaining scope was verification: the 6 audit node IDs pass
(8 passed), and the decision-trace file plus the test pinning the first-token timeout
on decision calls pass (8 passed). Three bounded gaps are recorded rather than fixed:
the PayCosts/AutoTap request mismatch belongs to execution, the contradictory fallback
rationale to the claims/fallback owner, and decision-chain accumulation to execution
follow-up f9b88c8. The retained traces `6bef1efb`, `06bbdd81` and `b5f55777` are
absent from this checkout, so this is mechanism verification, not an exact replay.
BUG-264 also needed no production source change, and its mechanism is the opposite
of #256/#260: the retained stalls have no first-token or budget failure, so the model
call finished inside its limits and the wall clock went somewhere else. A watchdog
warning alone cannot prove a service delay, which is why this ID asks for client
versus backend timing separation — and that is what `llm_trace` records on master:
`client_setup_ms` and `lane_wait_ms` are kept apart from `ttft_ms` and `total_ms` per
request, scoped to one decision, next to what was finally submitted. The one
first-plan-start lag in the trace is covered by the fast initial plan (effort `none`
inside the 20 s initial budget), and the hedge and planner cancel token cover T/F5.
The remaining scope was verification: the 9 audit node IDs pass (16 passed) and the
decision-trace file passes (7 passed). Two bounded gaps are recorded rather than
fixed: end-to-end spans that explain a client-side stall without a failed model call
are not in this queue, and first-plan live completion is still pending. The retained
traces `90b6a56c`, `94f1043a` and `edfd8db0` are absent from this checkout, so this is
mechanism verification, not an exact replay.
BUG-265 also needed no production source change. Its mechanism is the declaration
hedge: the high-effort leg ran out its budget while the low-effort leg had an answer
that could stand, and the old hedge waited for high anyway, so the combat window
closed with a usable answer already in hand. On master both legs are submitted at
once and the first answer the ordinary parser accepts as legal actions wins — the
call site passes a parser-based predicate over the effective legal actions, so
"usable" means legal, not merely non-empty — and the losing leg's cancel token is set
in the `finally` block so its network work stops instead of running on. A fast but
invalid answer cannot displace the valid leg, and if neither is usable a response is
retained for the caller's bounded repair path rather than raising. The remaining scope
was verification: the 10 audit node IDs pass (14 passed) and both declaration files
pass (27 passed). Two bounded gaps are recorded rather than fixed: live qualification
of a faster accepted declaration is still pending, and malformed typed output plus
illegal declarations stay with the parser/legality owner. The retained traces
`e756afa4` and `15eaac1a` are absent from this checkout, so this is mechanism
verification, not an exact replay.
BUG-266 also needed no production source change. Its retained window had two budget
exhaustions, and the second is the one that matters: a malformed strategy answer used
to start a fresh model request with a fresh budget, so one reform could spend twice its
allowance. On master the JSON retry receives only what is left of the original reform
budget — 3.0 s of 10 s in the pinned reproduction — and an expired reform starts no
second request at all. The audit's prerequisite ("identify whether exhausted calls were
hedge or serial work") is answered by the decision trace, which records `call_class`,
`status`, `attempts` and `budget_s` per request, so a hedged pair is distinguishable
from serial retries inside one decision. Two mechanisms are recorded rather than fixed
because they belong to other owners: the hop limit (`_MAX_POST_CAST_PRIORITY_HOPS = 1`)
is deliberate bounded work per cast and logs explicitly when it trips instead of
stalling silently, and library-only strategy rejection belongs to the strategy owner
(#240, out of queue scope). The remaining scope was verification: the 15 audit node IDs
pass (24 passed) and the reform-budget plus post-cast priority files pass (37 passed).
The retained traces `04314b21`, `8d26e22e` and `c06d3a82` are absent from this
checkout, so this is mechanism verification, not an exact replay.
BUG-269 also needed no production source change, and its retained mechanism is the
intended behaviour rather than a stall: the low answer was doubted for a false lethal
claim, the high leg never arrived, and the hedge fell over to board math instead of
firing the unsafe low answer. On master a doubted low answer waits only for the bounded
doubt allowance (`_DOUBT_AFTER_LOW_S = 6.0`, clamped by the high leg's own deadline) and
then raises `SecondLookError`, which the caller records as `set_aside` and hands to the
board-math fallback. The triage forbids removing the false-lethal guard to reduce
latency, so that constraint is recorded rather than traded away — the pinned test passes
a face-burn doubt reason, which means it cannot pass with the guard deleted. The
remaining scope was verification: the 11 audit node IDs pass (13 passed) and both hedge
files pass (12 passed). Two bounded gaps are recorded rather than fixed: there is no
exact board replay because traces `211d4ccc` and `87cac475` are absent from this
checkout, and fallback time versus a real Arena rope is not qualified live.
BUG-270 is the first non-latency item in this stretch and also needed no production
source change. The block search already values a dying return creature as two separate
things: the body it loses and the residual engine it keeps as a noncreature enchantment.
`_death_material` is `max(0, _material - return_credit)`, so an equal body trade that keeps
a draw engine is affordable while the same trade with the return text removed is not — the
pinned test drives both sides of that comparison at a tiny and a huge option budget. The
returned permanent is built fresh (no power/toughness, counters, attachments, damage or
combat state, owner control), visible suppression or "exile … instead" withholds the credit,
and tokens, copies, face-down cards and lost ability text earn nothing. The retained packet
(`gameplay-ultra-audit.json` G03, `d1bad78e`) is absent from this checkout, so the tests
reconstruct its recorded combat facts rather than replaying hidden responses. Verification:
the two #270/#307 files pass (37 passed) and the three other consumers of the same helper
pass (34 passed). #307 covers the same subsystem later in the queue; this receipt is the
block/hold comparison it asks for.
BUG-273 also needed no production source change. Its protection half is already merged: an
attack that only feeds an untapped blocker is held unless an affordable instant-speed play
could change that combat, and "affordable" means the mana is untapped and the card is
castable in the window (`_possible_tricks` filters hand instants/flash by
`missing_trick_color`; `losing_attackers` keeps the whole attack only for tricks whose text
matches `_SAVES_ATTACKER`, while plain pumps are judged per attacker by `_doomed`). The new
file drives the supported precombat sequence through `_prepare_attack_submission`, the pass
every attack takes before it is sent, and contrasts it with the hold control, two
unavailable-protection controls (land tapped, wrong colour) and a sorcery-speed control;
the partial-attack cases show it thins a doomed attacker instead of forcing an all-in or a
full hold. Verification: the new file passes (7 passed), the guard/protection regression set
passes (98 passed) and the other consumers of the same guard pass (191 passed). Two bounded
gaps are recorded rather than fixed: nothing deterministically casts the trick in the
precombat main (`cast_notes.trick_timing_note` only fires for `gets +N/+N until end of turn`
pumps, so a protection spell gets no timing note), and a "protection from <quality>" trick is
credited without checking that the quality covers the answering group, so a colourless or
two-colour group can still answer it. The historical #273 packet is absent from this
checkout, so the tests reconstruct a precombat main rather than replaying hidden responses.
BUG-275 also needed no production source change. The audit's three signatures are each covered by
merged work: a per-call budget and deadline with a between-chunk cancellation check and a socket-level
cancel watcher (`proxy.py:1340`, `_TrackingNetworkBackend` at `proxy.py:880`) so a stalled read cannot
outlive the decision, plus `_wait_for_model`/`_bounded_call` (`action_planner.py:1133`/`1150`) and
`_llm_unavailable_plan` (`1233`) for the fallback; a fast 20 s hosted first plan
(`game_plan.py:3733`) with a prompt-sized first-token limit inside the planner budget
(`action_planner.py:1038`/`1045`); and a one-hop post-cast priority cap that hands off to the watchdog
(`autopilot.py:5172`, warning at `6107`). The acceptance's timing record already exists (#231):
`llm_trace` is started around every plan (`autopilot.py:2974`), fed by the proxy (`proxy.py:1887`) and
carried on the submitted action line by `_decision_suffix` (`2953`). Probes against real code paths show
a healthy slow first token recording per-request lane/first-token/total timings with
`(decision=1390.2ms, requests=2)`, and a budget cut falling back to `llm_unavailable` with
`(decision=255.8ms, requests=0)`; a timestamped wrapper probe measures ~1.04 s of prompt construction
before the model call starts, so the production 15 s cap leaves ~14 s of model time. Verification: the
14-file latency/trace regression set passes (101 passed), the post-cast hop-limit file passes (18 passed)
and the audit's exact 14 node IDs pass (21 passed). Bounded gaps are recorded rather than fixed: both
retained hashes exist only in `docs/latency-222-id-audit-20261101.json:530` and
`docs/latency-222-triage-20261101.md:150` with no trace or replay fixture in this checkout, so the exact
19 s stall cannot be replayed; setup time sits outside the model budget by design; a timed-out decision
records `requests=0` because the cancelled call's metrics land after `llm_trace.finish()`; and live timing
distribution plus hop progression remain unqualified, which is the audit's own unresolved prerequisite.
BUG-276 also needed no production source change. Its signature — a high-effort `decision.plan` that used
its full 15 s budget after its first token had already arrived at 5.4 s — is covered by the bounded
declaration hedge (`action_planner.py:1162`, `b352b8a`, per-leg cancel events from `70d9e2f`) and the proxy
budget cut (`proxy.py:1340`, `d27b878`/`9f0a328`). The new regression drives the real `ProxyBackend` with a
high leg that streams a reasoning chunk at once and then one content chunk every 50 ms: with a ready low
answer the low leg wins in 27 ms and the streaming high leg is cancelled at 58.7 ms total, and with an
unparseable low answer the streaming leg is cut at 1014.9 ms against a 1.0 s budget and the deterministic
`planner_free_attack` line is returned instead of an empty plan. Verification: the new file passes (2 passed),
the nine-file latency/trace regression set passes (48 passed) and ruff check/format are clean. Bounded gaps
are recorded rather than fixed: both retained hashes exist only in the audit and triage rows with no trace to
replay; the historical ~22 s loop resume is not fully explained by the planner (15 s budget plus ~1 s of
prompt construction leaves ~6 s, matching the post-cast priority waits already attributed to #206/#243 in the
BUG-248 receipt); prompt construction sits outside the model deadline; and live timing distribution remains
unqualified, which is the audit's own unresolved prerequisite.

BUG-277 also needed no production source change. Its delayed-winner signature — the high-effort declaration
leg exhausting its budget before the low-effort answer stood and the attack executed — is the same bounded
declaration hedge (`action_planner.py:1162`, `b352b8a`, per-leg cancel events from `70d9e2f`) plus the proxy
budget cut (`proxy.py:1340`, `d27b878`/`9f0a328`) that closed BUG-276, so this item references those commits
instead of duplicating a patch. The new regression drives the real `ProxyBackend` through three shapes of the
same decision (Odin's five power against their five life, so `declaration_effort` says high): a slow but
*valid* high answer that would have been usable at ~1.2 s loses to a ready low answer in 0.012 s of model time
with the high request cancelled at ttft 0.8 ms / total 1203.8 ms; a fast unparseable high answer does not
displace the valid low answer (both requests `ok`, 0.018 s); and a streaming high leg is cut at 54.0 ms while
the low answer stands (0.001 s). All three submit `declare_attackers` with attacker Odin and an empty
`fallback_reason`, so a silent fallback cannot pass as success. Running the new file before BUG-276's file also
exposed a scheduling-dependent leg-order assertion there (both legs go to a two-worker pool), now compared
sorted without weakening the behaviour under test. Verification: the new file passes (3 passed), the nine-file
latency/trace regression set passes (63 passed) and ruff check/format are clean. Bounded gaps are recorded
rather than fixed: both retained hashes exist only in the audit and triage rows with no trace to replay; live
first-valid-winner/cancel confirmation and the audit's residual 33 s chain timing remain unqualified (the
planner's 15 s budget plus ~1 s of prompt construction leaves ~6 s matching the post-cast priority waits
already attributed to #206/#243 in the BUG-248 receipt); prompt construction sits outside the model deadline;
and one probe artifact is noted — reusing a mutated state dict across several decisions suppressed the high leg
on the third decision, while fresh state per decision hedged every time.

BUG-278 also needed no production source change. The defender label never reaches a consumer that can veto a declaration: `_role` returns `ROLE_AGGRESSOR` for an all-in line before the behind branch, `role_guard` only replaces non-board `ActionsAvailable` picks, `line_search._posture` ranks hold against attack from simulated lines (net lifelink and crackback are inside those numbers) with no role input, `attack_push`/`attack_line_note` follow that posture, and `_check_attack_crackback` trims by life math only; the `survival_mode` consumers trim card draw or annotate a plan but cannot touch a declaration. The new regression pins the contrast on reconstructed boards: 2x 4/4 against 2x 3/3 at 11-12 life versus 10 is labelled `defender` yet the search says `attack`, the prompt note ends `Verdict: ATTACK.`, the push applies with both attackers and the crackback guard keeps them; 2x 1/1 against 4x 2/2 at 6 versus 20 is `hold` (`dead on T9` versus `dead on T11`), nothing is pushed and the guard empties the declaration with `planner_crackback_guard`. A grid over those boards found 1199 defender-labelled boards, 24 whose posture says attack, 2 that push an empty declaration and none where the guard empties an all-in attack. The historical packet is missing (`docs/BACKLOG_STAGE_2026-11-01.md:136`) and the push guard's conservative band (`ATTACK_PUSH_MARGIN = 6.0` against `POSTURE_MARGIN = 3.0`) is unchanged, so a hold standing where the search prefers attacking by less than that gap remains a separate tuning question.

BUG-283 also needed no production source change. The retained 15–18 s stall is the 15 s decision budget plus prompt construction, not an unbounded stream: `model_deadline` is anchored after prompt build (`action_planner.py:1486`), `_bounded_call`/`_wait_for_model` raise at that deadline and shut the pool down with `wait=False`, the proxy cuts a streaming request per chunk at its budget (`proxy.py:1995–1996`) and marks that cut non-retryable (`proxy.py:1618–1625`, `_budget_cut_stream`), and the except chain returns `llm_unavailable` with the deterministic board-math plan (an empty declaration holds every attacker). A probe against the real planner and proxy with a stream that yields a first token then sleeps 0.4 s per chunk returned at 3.87 s for a 3 s budget — 0.87 s of prompt build plus the full budget — with fallback `llm_unavailable` and no actions. The new regression pins both legs streaming past a 1 s budget returning at the deadline with a legal hold, and re-pins #277's hedge fix (low wins in 27 ms, high cancelled) as this item's reference. Prompt construction still sits outside the model deadline (~16 s worst case), a bounded-wait decision can still show `requests=0` in the trace (#275's recorded gap), and live timing distribution remains unqualified.

BUG-284 also needed no production source change. The declaration forecast already counts their blockers, damage order and net lifelink: `assess` derives `lethal_now` from `simulate_attacks` against their best blocks (`board_assessment.py`), and `optimal_blocks` / `_resolve_attacker` carry `damage_through`, `blocker_life_gained` and damage order (`combat_solver.py`). A 576-combination keyword grid found zero mismatches between forecast totals and the resolver and zero between `lethal_now` and the defender's best net life loss; four two-blocker damage-order boards matched; a 6/6 trample swing at 4 life is lethal against a plain 2/2 blocker and not lethal against a 2/2 lifelink blocker. The new regression pins that contrast pair, single-blocker totals over 16 keyword/P-T combinations, multi-blocker damage order, and `lethal_now == (plan.net_life_lost >= opponent_life)` on six boards. One bounded gap is recorded rather than fixed: `combat_strategy._best_through` (line 1033) returns raw `damage_through` without subtracting the blockers' lifelink and the losing-attack guard's lethal test (line 1571) uses it — conservative in the direction of keeping attackers, and no probe board showed it changing the guard's outcome. The historical #284 packet is absent (reconstructed boards), `docs/autopilot-improvements.md` is absent from this checkout, and triage row 443 is OPEN only.

BUG-285 also needed no production change to the latency mechanism itself, but it did expose one attribution bug in the #231 trace. The declaration hedge already returns the first usable answer and cancels the other leg (`action_planner.py:1162`), our own budget cut is classified as slow-but-alive and never retried (`proxy.py:444`, `1533`, `1618`), and the post-cast hop limit stays at one (`autopilot.py:5172`). A deterministic probe on current master shows the historical ordering no longer degrades the plan: a high leg cut at its budget leaves the standing low declaration to submit in 0.60 s, and both legs failing (cut or model-unavailable sentinel) yields `llm_unavailable` with no invented action. Running the audit's node IDs together with the newer latency regressions showed a cancelled leg from the previous decision finishing after the next trace opened and being recorded into it — a 1.0 s decision's trace held a cancelled entry with `budget_s = 2.0` and `total_ms = 55.2`. `RequestTrace` now records `started_unix_s` and ignores metrics whose `request_started_unix_s` predates it (metrics without that field are still recorded), so a decision's timing record names only its own requests; the combined set then ran twice at 64 passed with no bleed. Verification: the 14 audit node IDs plus this item's file and the #276/#277/#283 regressions pass (31 passed), the #231 trace file passes (7 passed), and ruff is clean. Live qualification is still outstanding — the audit disposition stays PARTIAL MECHANISM COVERED / LIVE UNQUALIFIED because its prerequisite (qualify declaration speed and service timeouts separately) needs live timing distribution, and the retained traces are absent from this checkout.

## Queue entries

The JSON contains per-item evidence, acceptance criteria, implementation and review
receipts. The compact index below preserves exact membership and initial scopes.

| Order | Item | Concrete symptom | Acceptance focus |
|---|---|---|---|
| 1 | CI-PERF | SLOW_212608 and SLOW_202111 measured491.433/493.853ms against the unchanged480ms bound on master c310005. | Preserve480ms bounds, search budgets and all meaningful assertions; no fixture-specific shortcuts. |
| 2 | BUG-141 | First useful game plan arrives too late or reforms repeatedly cancel each other. | Replay startup plus repeated turn/threat changes: a usable bounded plan arrives while urgent decisions retain their deadlines; stale plans never execute. |
| 3 | BUG-210 | Attack-tax choices can select an unaffordable army or repeat cancelled declarations. | Verify current payable subsets, tax conditions, legal empty withdrawal, no retry loop and no false attribution to unrelated casts; preserve mandatory attacks and legal alternatives. |
| 4 | BUG-223 | Background planning competes with urgent decisions and wastes work on cancelled plans. | Measure separate queue/network/generation delays with synthetic fixtures; urgent work meets its budget, abandoned work cancels and a useful plan can still complete. |
| 5 | BUG-231 | Decision stalls persist after streaming budget cuts; land/pass fallback masks the delay. | Locate this ID’s retained trace, reproduce its current-path mechanism, record queue/first-token/generation/cancellation timing and final submission; preserve the original deadline, freshness and legal fallback. If an earlier queue item already fixes it, verify this specific regression and reference that same master commit instead of duplicating a patch. |
| 6 | BUG-232 | A combat request times out and fallback chooses blocks instead of a usable model declaration. | Locate this ID’s retained trace, reproduce its current-path mechanism, record queue/first-token/generation/cancellation timing and final submission; preserve the original deadline, freshness and legal fallback. If an earlier queue item already fixes it, verify this specific regression and reference that same master commit instead of duplicating a patch. |
| 7 | BUG-235 | Mana-illegal plan steps are removed while later steps still depend on them. | Verify colored/restricted mana, commander tax and sequential spending; remove dependent steps or produce a legal repair within the original deadline. |
| 8 | BUG-239 | Repeated stalls and duplicate watchdog answers occur in the same decision window. | Locate this ID’s retained trace, reproduce its current-path mechanism, record queue/first-token/generation/cancellation timing and final submission; preserve the original deadline, freshness and legal fallback. If an earlier queue item already fixes it, verify this specific regression and reference that same master commit instead of duplicating a patch. |
| 9 | BUG-241 | A timed-out reversal recheck preserves a fallback pass over an otherwise usable action. | Replay completed legal low-effort action versus timed-out recheck, genuine model hold and changed state; keep a fresh valid choice without renewing the deadline. |
| 10 | BUG-247 | Block/race evaluation misses alternate-loss damage effects rather than ordinary life damage. | Preserve the Odin must-block replay and verify the next supported poison/mill gap with lethal/nonlethal controls; explicitly abstain on unsupported effects, without reviving obsolete #281. |
| 11 | BUG-248 | Streaming budget exhaustion stalls the coaching loop despite a benign fallback. | Locate this ID’s retained trace, reproduce its current-path mechanism, record queue/first-token/generation/cancellation timing and final submission; preserve the original deadline, freshness and legal fallback. If an earlier queue item already fixes it, verify this specific regression and reference that same master commit instead of duplicating a patch. |
| 12 | BUG-249 | Repeated first-token delays and cancelled background plans persist across matches. | Locate this ID’s retained trace, reproduce its current-path mechanism, record queue/first-token/generation/cancellation timing and final submission; preserve the original deadline, freshness and legal fallback. If an earlier queue item already fixes it, verify this specific regression and reference that same master commit instead of duplicating a patch. |
| 13 | BUG-253 | Planning can return an empty role while the coaching loop stalls or initial plan is missing. | Replay schema-valid empty output and busy/degraded planning; retain a valid prior plan or bounded fallback, cancel stale work and report the true reason. |
| 14 | BUG-254 | X values or activated/cast costs exceed the usable payment capacity. | Use X plus fixed and colored/restricted costs, tapped/sick sources and actual offered payment capacity; accept payable alternatives and never invent mana. |
| 15 | BUG-256 | A recurring latency incident still consumes most of a decision window. | Locate this ID’s retained trace, reproduce its current-path mechanism, record queue/first-token/generation/cancellation timing and final submission; preserve the original deadline, freshness and legal fallback. If an earlier queue item already fixes it, verify this specific regression and reference that same master commit instead of duplicating a patch. |
| 16 | BUG-260 | A recurring timeout/circuit-degradation incident still leaves delayed decisions. | Locate this ID’s retained trace, reproduce its current-path mechanism, record queue/first-token/generation/cancellation timing and final submission; preserve the original deadline, freshness and legal fallback. If an earlier queue item already fixes it, verify this specific regression and reference that same master commit instead of duplicating a patch. |
| 17 | BUG-264 | A recurring stall incident needs client versus backend timing separation. | Locate this ID’s retained trace, reproduce its current-path mechanism, record queue/first-token/generation/cancellation timing and final submission; preserve the original deadline, freshness and legal fallback. If an earlier queue item already fixes it, verify this specific regression and reference that same master commit instead of duplicating a patch. |
| 18 | BUG-265 | Planner failure and fallback behavior recur during latency pressure. | Locate this ID’s retained trace, reproduce its current-path mechanism, record queue/first-token/generation/cancellation timing and final submission; preserve the original deadline, freshness and legal fallback. If an earlier queue item already fixes it, verify this specific regression and reference that same master commit instead of duplicating a patch. |
| 19 | BUG-266 | Post-cast priority hop limits and stalls hand work to the watchdog. | Locate this ID’s retained trace, reproduce its current-path mechanism, record queue/first-token/generation/cancellation timing and final submission; preserve the original deadline, freshness and legal fallback. If an earlier queue item already fixes it, verify this specific regression and reference that same master commit instead of duplicating a patch. |
| 20 | BUG-269 | A recurring stall incident remains open despite successful fallback decisions. | Locate this ID’s retained trace, reproduce its current-path mechanism, record queue/first-token/generation/cancellation timing and final submission; preserve the original deadline, freshness and legal fallback. If an earlier queue item already fixes it, verify this specific regression and reference that same master commit instead of duplicating a patch. |
| 21 | BUG-270 | Blocking undervalues creatures that return as noncreature enchantment engines. | Compare block/hold on a retained return case: lose the creature body but preserve only the actual returned engine; handle exile, counters and conditional return accurately. |
| 22 | BUG-273 | Attack sequencing neglects available protection or pumps that change blocking/lethal. | Reproduce one supported precombat/protection sequence with actual mana and legal windows; contrast hold, nonlethal and unavailable-protection controls without forcing all-in attacks. |
| 23 | BUG-275 | An isolated long stall occurs amid otherwise responsive matches. | Locate this ID’s retained trace, reproduce its current-path mechanism, record queue/first-token/generation/cancellation timing and final submission; preserve the original deadline, freshness and legal fallback. If an earlier queue item already fixes it, verify this specific regression and reference that same master commit instead of duplicating a patch. |
| 24 | BUG-276 | High-effort planning exceeds its budget and resumes only after the decision delay. | Locate this ID’s retained trace, reproduce its current-path mechanism, record queue/first-token/generation/cancellation timing and final submission; preserve the original deadline, freshness and legal fallback. If an earlier queue item already fixes it, verify this specific regression and reference that same master commit instead of duplicating a patch. |
| 25 | BUG-277 | A slow declaration leg exhausts budget although the low-effort answer is usable. | Locate this ID’s retained trace, reproduce its current-path mechanism, record queue/first-token/generation/cancellation timing and final submission; preserve the original deadline, freshness and legal fallback. If an earlier queue item already fixes it, verify this specific regression and reference that same master commit instead of duplicating a patch. |
| 26 | BUG-278 | Defender-role plans repeatedly hold despite a supported surviving attack in a losing race. | Compare hold and attack using net lifelink/crackback and actual protection; a role label must not veto the better supported line, while genuinely losing attacks remain rejected. |
| 27 | BUG-283 | Streaming budget exhaustion still causes 15–18-second coaching stalls. | Locate this ID’s retained trace, reproduce its current-path mechanism, record queue/first-token/generation/cancellation timing and final submission; preserve the original deadline, freshness and legal fallback. If an earlier queue item already fixes it, verify this specific regression and reference that same master commit instead of duplicating a patch. |
| 28 | BUG-284 | An exactly-lethal attack forecast ignores blockers or lifelink and the opponent survives. | Run the complete declaration forecast with all visible blockers, damage order and net lifelink; match resolver totals and avoid unsupported exact-lethal claims. |
| 29 | BUG-285 | Budget exhaustion and circuit degradation overlap important planning windows. | Locate this ID’s retained trace, reproduce its current-path mechanism, record queue/first-token/generation/cancellation timing and final submission; preserve the original deadline, freshness and legal fallback. If an earlier queue item already fixes it, verify this specific regression and reference that same master commit instead of duplicating a patch. |
| 30 | BUG-286 | A failed high-effort declaration stalls before a deterministic guard recovers. | Locate this ID’s retained trace, reproduce its current-path mechanism, record queue/first-token/generation/cancellation timing and final submission; preserve the original deadline, freshness and legal fallback. If an earlier queue item already fixes it, verify this specific regression and reference that same master commit instead of duplicating a patch. |
| 31 | BUG-289 | Latency fallback and stale availability/board facts compound during a lethal window. | Locate this ID’s retained trace, reproduce its current-path mechanism, record queue/first-token/generation/cancellation timing and final submission; preserve the original deadline, freshness and legal fallback. If an earlier queue item already fixes it, verify this specific regression and reference that same master commit instead of duplicating a patch. |
| 32 | BUG-300 | Generated explanations invent card costs, mana, modes, targets or tutor outcomes. | Select one retained failing explanation, preserve the legal chosen action and original diagnostic, correct only supported claims, and test ambiguous/valid controls; do not claim better card selection from wording alone. |
| 33 | BUG-306 | An optional trigger spends mana that the current plan explicitly reserves. | Replay the sole-reserved-mana payment through producer, planner and submission; respect a current valid reserve, allow affordable/unrelated payments and refresh intent after spending. |
| 34 | BUG-307 | Death-return creatures or engines are treated as gone or harmless after their body dies. | Verify block/removal valuation retains the actual draw/drain engine but not nonexistent body power; preserve exile and no-return controls and compare supported outcomes. |
| 35 | BUG-308 | Removal, creature entry, draw and enemy-trigger sequences are evaluated in the wrong order. | Extend only a reproduced unsupported sequence, preserve once-per-turn usage and protection, and compare both orders plus enemy response; no fabricated future draws or universal target override. |
| 36 | BUG-309 | Nested equipment selection forgets combat has passed and promises an attack now. | Carry intent through real nested requests with current phase, source and expiry; disallow a Main2 attack promise while retaining valid shroud/mana-relay reasons. |
| 37 | BUG-317 | Draft advice lacks a useful strongest-alternative comparison or truncates its explanation. | Verify the accepted recommendation and spoken explanation contain a grounded trade-off or explicit uncertainty, including equal/unknown ratings; do not invent external scores or reopen obsolete #294. |
| 38 | BUG-318 | Sideboarding is confused with mulligan/gameplay and can submit or mutate the wrong deck. | Exercise same-request registered pool, deck constraints, dedup, short timer and resume ownership; preserve unsupported companion/commander/constructed paths visibly instead of blind submission. |
| 39 | BUG-329 | Chosen actions contradict hold/pass reasoning or stale beliefs contaminate later choices. | Reproduce one remaining accepted-action/trace/memory contradiction across the actual final submission path; preserve legal selections and distinguish corrected prose from improved strategy. |
| 40 | BUG-350 | Permanent hexproof is chosen as if it protects a spell from a counterspell. | Prevent a cast chosen solely for impossible spell protection while retaining deliberate life-gain or real permanent protection; test recheck failure, legal alternatives and original deadlines. |

BUG-286 needed no production source change: the deterministic Sneak guard runs before any
model call (action_planner.py:4970), the declaration hedge is bounded by the planner deadline
(action_planner.py:1486/1537), and the bridge's authoritative attack override recovers the
lethal Sneak line in milliseconds after a failed declaration (autopilot_bridge.py:807/830).
The new tests/test_latency_286_sneak_guard.py pins the regression on the ledger #156/#159
Leonardo board: a failed high declaration returns llm_unavailable at its 0.6 s budget and the
guard still declares all fifteen attackers with Sneak reasoning; a timely low declaration is
not overridden with fewer attackers; at 60/40 life the guard declines, so the recovery is not
an unconditional all-in. The audit's unresolved prerequisite stands: an illegal pass/fallback
retry is a distinct case needing its own exact-case reproduction, and live timing remains
unqualified.

BUG-289 needed no production source change either: the typed-decision hedge returns a usable
low answer at its own latency and cancels the high leg (action_planner.py:8845), and the
typed-decision validation filters any option id that is no longer legal before falling back to
a deterministic legal pick (action_planner.py:5049/5103). The new
tests/test_latency_289_stale_facts.py pins both halves: a critical hedge with a 0.2 s low leg
against a 1.0 s budget returns in under 0.9 s with winner=low and wait_after_low_ms=0, and a
stale tgt:999 answer is dropped in favour of a legal target. The audit's unresolved prerequisite
stands: the historical solver choice belongs to the combat owner (#288) and the strategic
compounding to #240/#287, and live timing remains unqualified.

BUG-300 did need a production change, in the draft fact checker rather than the advisor.
`draft_facts.reason_corrections` already corrected printed costs, simple mana production, one
printed generic discount and free-equip attribution, and `DraftAdvisor._validate` keeps the
chosen pick plus `original_reason` while replacing only the contradicted sentence. Tutor
outcomes were checked in gameplay (`tutor_claims.correct_tutor_claims`) but never in draft
prose: on master c743ae7 "Enlightened Tutor finds our best creature." passed through untouched
even though the supplied oracle text restricts the search to an artifact or enchantment card,
and "Finale of Devastation finds a land." survived against a creature-only search. The new
`_tutor_types`/`_tutor_corrections` pair reads the first restricted "search your library for …
card" clause and rewrites only a claim that names a type outside it, so an allowed type, a
qualified tail ("if we splash blue"), a negation, an ambiguous noun ("creature removal") and an
unrestricted tutor ("Demonic Tutor finds our best creature") all stay as written. Verification:
43 passed in tests/test_draft_facts.py and 206 passed across the seven draft files, ruff clean.
Bounded gaps recorded rather than fixed: only the first restricted clause is read, claims about
"permanent"/"spell" or mana-value-only restrictions abstain, and there is no live qualification
or Linux CI receipt.

BUG-306 needed no production change either: the reservation guard was already wired into both
PayCosts submission routes. `optional_reservation.reserved_optional_mana` feeds
`AutopilotEngine._should_decline_optional_cost`, which `autopilot_bridge.py:2774-2792` (the
safe-default net) and `autopilot.py:3546-3575` (`process_trigger`) both consult before any
auto-tap, and `resource_hold.current_mana_hold` already refreshes the rendered hold after
confirmed spending. What was missing was the last hop's regression: nothing asserted that a
reserved PayCosts is answered with MTGA's cancel rather than an AutoTap solution, so a future
regression could have spent the reserved mana while every existing test still passed. The new
`tests/test_optional_reservation_submission_306.py` drives both real handlers with a recording
bridge: the sole reserved mana ({1} optional against a {W} Rescue) cancels once with no auto-tap
and no pay/decline model call, spare mana and an unrelated payment still auto-tap, and a decline
without `can_cancel` returns False without paying. Verification: 6 passed for the new file and 62
passed across the seven reservation/payment files, ruff clean. Bounded gaps recorded rather than
fixed: the guard needs a recorded pass decision that states a same-turn hold (a fresh board or
another explicit priority decision supersedes it by design), the proof is numeric shortfall only,
the historical raw packet is not bundled, and there is no live qualification or Linux CI receipt.

BUG-307 split cleanly in two. The block half was already merged (#270/#284): `combat_solver._material`
counts a live body plus its engine and `_death_material` subtracts the retained engine credit, so a
block trade keeps the drain/draw engine and loses only the body — pinned by
`tests/test_retained_engine_value_284.py` and `tests/test_combat_persistence_284.py`. The open half was
removal valuation: `board_assessment._spell_value` credited a removal spell with killing the drain
engine of a creature that returns as an enchantment. A probe on master 728c05f returned 7.0 for a 2/2
drain body with Enduring-Tenacity-style return text, 7.0 for the same body without it, and 7.0 with
visible exile — i.e. the returning body was valued exactly like a plain one. The fix adds
`_keeps_engine(body)` (False when `_return_credit_blocked`, else
`persistent_returns.returned_enchantment`) and the threat line now reads
`power + (0.0 if _keeps_engine else engine_role)`: 5.0 for the returning body, 7.0 plain, 7.0
exile-blocked, and a draw-engine return body stays at 5.0 both ways so no discount is invented.
`tests/test_death_return_removal_value_307.py` pins those cases plus the block-half contrast
(10.0/6.0 returning, 7.0/7.0 plain) and the mixed-board "most dangerous target" pick. Verification:
5 passed for the new file, 442 passed across the valuation/search batch, 296 across the plan/line
batch, ruff clean. Bounded gaps recorded rather than fixed: only the printed self-return rule is
recognised (blink and reanimation abstain), the whole engine role is withheld for a returning body
including token activations a noncreature enchantment may not use, the historical raw packet is not
bundled, and there is no live qualification or Linux CI receipt.

BUG-308 needed no production source change. The bounded same-turn comparison merged earlier already
covers every clause of its acceptance on current master, so the receipt records the reproduction and
the gaps that stay outside that shape. A probe on master 347b0e9 drove the real modules with the
retained board (Enduring Innocence + Orcish Bowmasters + Answer + Test Recruit): `search_lines` ranked
removal-first at 34.80 with one draw above entrant-first at 33.30 with none and entry-first at 20.00,
where the engine survives as an enchantment and the opposing Army shows one counter; `sequence_values`
emitted both branches while the trigger was unknown and only the spent branch once the engine id was
marked used; `public_sequence_concerns` raised exactly one concern for a rationale claiming a draw
while that trigger was observed spent, and none for an acknowledged one; and with the entrant carrying
protection from black the ping target list was us plus the engine, with the protected entrant omitted
while the engine still died and returned. Verification: 157 passed across the nine #308 files, 554
passed with the line-search/board-assessment/sacrifice-continuation neighbours, ruff check and format
clean. Bounded gaps recorded rather than fixed: the search abstains outside the current turn, the
prompt vector projects only fixed-damage removal and only us-or-entrant ping targets,
`TriggerUseMemory` needs the full draw clause in the stack ability text, the historical packet is not
bundled, and there is no live qualification or Linux CI receipt.

BUG-309 needed no production source change either. The intent carry across nested requests
(`activation_intent.py`), the phase-aware equipment note ("Combat has passed: haste can enable a tap
ability, not an attack this turn") and the Main2 attack-promise claim check
(`game_plan.decision_claim_notes` plus `corrected_decision_reasoning`) were all merged already. The
unpinned hop was the planner call that stores the corrected reasoning: `ActionPlanner._note_false_claims`
(`action_planner.py:10163`, called from the typed path at `:9818`). A probe on master bd56f34 with a real
`ActionsAvailable` equip menu (Lightning Greaves, summoning-sick Notary Hobbits, Main2) showed the claim
note firing, the sentence being replaced by "Rules correction: The normal attack declaration window has
passed…", and the trace recording `uncorrected_reasoning` plus `false_claims`. The new
`tests/test_equipment_phase_planner_309.py` pins that hop: the Main2 attack promise is replaced, while a
mana-tap reason, a shroud reason, a next-turn attack and an explicit "combat has passed" reason stay as
written, and a Main1 attack promise is left alone. Verification: 6 passed in the new file, 107 passed
across the six equipment/intent files, ruff check and format clean. Bounded gaps recorded rather than
fixed: the retained packet is not bundled (equivalent board regressions), the claim check is text-level
and abstains on negations/future turns/explicit additional combat, and there is no live qualification or
Linux CI receipt.
