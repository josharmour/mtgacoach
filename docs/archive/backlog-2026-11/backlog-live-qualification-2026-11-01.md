# Live qualification plan — 2026-11-01

Working scope and per-item implementation/review state now live in
[the portable 40-item queue](autopilot-active-work.md). This document retains
dispositions/evidence; it is not an additional implementation queue.

## User-accepted closure — 2026-11-02

The user accepts the following **18 scoped repairs as FIXED BY ASSUMPTION**
(`FIX_ASSUMED`) and closes their reports: #84, #121, #162, #205, #222, #237, #242, #246, #267, #274, #279, #311, #322, #325, #328, #342, #345, #349.
These are assumed fixed, **not newly reproduced, independently qualified or
live-verified**. Existing evidence and limits remain retained. Reopen the same
report if its edge case recurs; do not keep it active awaiting that opportunity.

**39 partial/open records remain active.** The 20 user-obsolete records remain
obsolete. This is a disposition change, not new implementation or test evidence.
Statements below about pending verification are historical qualification limits,
not a requirement to keep these 18 reports open.


The 2026-11-02 user disposition retires 20 prerequisite/ambiguous records from
active follow-up; see [exact IDs and active totals](backlog-triage-2026-11-01.md#user-disposition--2026-11-02).
Do not schedule qualification for those obsolete scopes. Historical evidence stays
retained; the separate 18 scoped repairs are now FIX_ASSUMED as stated above.

This is an observation plan, not a deployment or permission to interrupt a game.
The audit qualified source `0b33a56` offline. The running editable checkout was
last verified at `5612969`; checkout identity alone does not prove what a process
loaded. CI still has two performance failures at the audited revision.

Before a separately coordinated release test, record the actual engine-loaded
revision, process/start time, bridge handshake and Arena process identity. Use
that release's CI receipt. Do not induce an outage, freeze, ranked loss, restart
or spending just to exercise a test.

For each naturally occurring opportunity, retain a minimal linked record of the
request identity, relevant public state and offered choices, chosen IDs/reason,
submitted response, Arena progression and final visible explanation. Redact
unrelated account/configuration data. If the required state never occurs, mark
it **not exercised**; an ordinary match cannot qualify every special case.

| Scope | Required observation | Qualification limit |
|---|---|---|
| Results #5/#267/#337 | Correct same-match history/packet/bundle result; early room/turn-zero end and delayed final result exercised separately; omitted terminal life reconciles once | A routine end does not qualify delayed identity or early-end races; do not rewrite old records |
| Restart ownership #347 | Authorized restart with an unfinished same-ID room returns to mulligan/gameplay without idle Recent Match navigation | Empty-home startup does not exercise this race; no surprise closure |
| Choices #298/#313/#314/#315/#332 | Correct namespace, source/effect and revealed identities; submitted IDs legal; newer damage assigner recognized | Generic selection does not qualify every request family or strategic choice |
| Recovery diagnostics #299 | Naturally rejected request and linked recovery remain in a delayed, bounded report | Submission alone does not establish acceptance or human input origin |
| Explanations #84/#108/#117/#138/#258/#259/#321/#323/#324/#344 | Final accepted action, trace, speech/UI and reusable advice agree after a guard or factual correction | Correct wording does not prove optimal play; #84's broader family stays open |
| Combat/resources #120/#210/#247/#329/#340/#348 | Current costs, protection, terminal grants, modal/commander constraints, legendary bodies and pending animations match the supported model | No universal combat or trigger simulation; broader #247/#329 remain partial |
| Sequence/search #308/#322/#328/#342/#345/#349 | Complete same-request public inputs reach planner; supported search/rechecks honor current identity, costs and alternatives | Original missing packets, full sequence ranking and numerical Ugin valuation remain separate; no forced optimality claim |
| Timing #66/#206/#226 and latency family | Current rope bounds planning/repair; fresh priority answered without reset; old clocks not borrowed; valid cached answer reused | Simple fast decisions do not qualify backend/load behavior or the separate CI performance gate |
| Draft #293/#295/#296/#297/#304/#312 | Correct event context and mechanics, local fallback metadata, accepted-pick-only history, consistent explanation | Structural priors are heuristics; #294 pick-quality evaluation is obsolete by user decision; no improved-picks claim |
| UI #215/#319/#320 | Natural health changes recover; qualitative Outlook and unknown sparse-deck role are displayed correctly | No calibrated probability or retrospective attribution of screenshot/state divergence |
| Upload #292 | After durable local save, a naturally slow or rate-limited upload does not hold reload as if writing continues | Instant uploads do not exercise the race; no live service fault injection |
| Frozen client #326 | A natural repeated failed-tick sequence pauses and alerts once; socket ping does not clear the latch | Does not repair Arena's freeze or authorize a restart |

The 18 FIX_ASSUMED reports require no dedicated qualification campaign. If they
recur, reopen the same ID with the new evidence. The table retains optional
observation criteria; it does not make those reports active again.

Promote only the exact observed scenario on an identified running revision to
live-verified. Keep offline negative controls and artifact references. Otherwise
retain **offline verified**, **not exercised**, or **evidence missing**. A fresh
regression on an active scope reopens its canonical ID without erasing history or
creating a duplicate. Reopening a user-obsolete scope requires an explicit decision.
