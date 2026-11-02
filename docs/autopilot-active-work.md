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

The original reports remain in the clearly labelled archive in
[autopilot-improvements.md](../autopilot-improvements.md). The source/test locator
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
