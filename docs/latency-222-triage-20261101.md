# Latency family #222: isolated Coach scheduling follow-up

Base: `8c021d29`; branch: `fix/latency-222`. No live input, installation,
restart, settings edit, model-server operation or push was performed. The
original ledger and runtime traces were read only. This report does not close
historical incidents or claim a measured win-rate improvement.

## Reconciled causes

The 23 assigned IDs have these primary investigation buckets. Incidents can
contain several signatures; these buckets prevent duplicate implementation,
not duplicate-evidence deletion.

| Primary bucket | IDs | Current assessment |
|---|---|---|
| Budget exhaustion / no first token / decision-chain latency | #222, #231, #239, #248, #249, #253, #256, #260, #264, #266, #269, #275, #276, #283, #289 | Existing proxy deadlines, circuit classification and typed-hedge cancellation address some paths. No blanket closure: not every historical stall is a model request, and sequential decisions can still accumulate time. |
| Declaration hedge waits for high after low answered | #232, #265, #277, #285, #286 | Reproduced on current code. Previously `_hedged_declaration` waited only on high, then used low after timeout. Fixed here; validation and deployment qualification remain separate. |
| Background contention / initial plan starvation | #141, #223 | Shared reform/repair deadline and failure cooldown fixed here. Fresh 650ae555 evidence also motivates a fast initial hosted request before ordinary deep refreshes. First-plan availability remains live-unqualified. |
| Reversal recheck after an outage pass | #241 | Existing outage-provenance and board-signature fixes retained. This patch does not change reversal semantics or claim exact historical replay. Shared declaration/plan budget changes do not prove this incident closed. |

The post-cast six-second wait and hop-limit signatures embedded in several rows
belong to the execution/request family (#206/#243/#327). They are not evidence
that the model service consumed the whole stall. Combat fallback quality and
missed requests belong to their respective owners; these changes do not alter
solver strategy or bridge submission.

## Proven current defects and fixes

1. **A ready low-effort combat declaration waited out high.** Return the first
   answer that the existing parser accepts as legal actions. Malformed and
   illegal fast answers cannot displace a valid second leg. Cancel outstanding
   calls on success, timeout or request change. If both responses are unusable,
   keep one for the existing bounded repair. Support an explicit earlier
   deadline, including an already exhausted deadline that must send no calls.
2. **Malformed game-plan repair received another full budget.** The initial
   model call and its one JSON retry now share a wall-clock deadline. Exhausted
   requests do not launch a retry; a ten-second reform that used seven seconds
   gives repair only three. Original unusable-answer diagnostics are retained.
3. **A long failed reform immediately reoccupied the lane.** The failed
   reform's retry cooldown now starts at completion. Reset and superseded
   workers return before changing the replacement's cooldown. Success cadence
   and intentional cancellation/resumption policies are retained.
4. **Hosted first plans could spend the entire initial window reasoning.**
   When no plan exists and the backend is `current`, DeepSeek or DSV4, request
   the same full, validated plan schema with reasoning effort `none`, within
   at most 20 seconds (or a smaller configured timeout). Its JSON retry shares
   both that effort and deadline. Once a plan exists, ordinary refreshes retain
   their previous reasoning policy and 75-second ceiling. Other backends retain
   their existing first-plan behavior. No persistent setting or shared backend
   default changes; the proxy test verifies the actual `thinking: false` request.

The fourth fix deliberately changes first-plan request policy, not the game's
rules or supplied facts. Its response quality and actual availability need live
qualification. It does not guarantee a plan when the service is unavailable,
malformed, or slower than the deadline.

## Fresh exact evidence: 650ae555

The watcher reported match `650ae555-192a-4f2c-aedb-a5b30d065e5e` on running
`8c021d29`: start 2026-10-31 20:53:56.176 PDT, win 20:55:22.169 PDT, duration
about 86 seconds. Deck strategy was present; this is not the earlier
deck-playbook handoff failure (#205).

- First game plan started at 20:54:08.736, about 12.6 seconds into the game.
  It was superseded at 20:54:54.031. Its actual request metrics report 45.300
  seconds, low effort, 12,439 reasoning characters and zero content characters.
- Replacement started at 20:54:57.926. Its first content appeared only after
  23.513 seconds; it was cancelled at game end after 24.238 seconds with only
  59 content characters. No plan completed.
- This was a short game, but real repeated reasoning/cancellation consumed
  most of it. It won, so no loss or rank cost is attributed to absent planning.
- A neighboring game-plan request at 20:51:47.175 completed in 22.223 seconds;
  a tactical request at 20:55:01.615 completed in 3.670 seconds. Model service
  was not uniformly down. First-token time is not first-answer time: reasoning
  arrived well before any usable content in the failed initial plan.

Read-only evidence: `task-2/monitoring/650ae555-review.json`, the ledger marker
`monitor-plan-availability-20261101T0357`, and the original standalone log.
[Selected request metrics](evidence/latency-222-20261101.json) preserve exact
timing records plus the source read range and SHA-256. These records predate
this fix and are not acceptance evidence.

The recent deck-pipeline fix is `9f9cd92` (and discovery/diagnostic follow-up
`701861b`); `08285a3` itself addresses selection-to-scry transitions. Both sets
of work were present in the audited baseline; this patch does not replace them.

## Validation and integration

Before implementation, four budget/declaration regression cases failed, then
the long-failure cooldown reproduction failed. Seven added first-hosted-plan
policy cases failed against the pre-policy code. The tests demonstrate the
local request/selection behavior; they do not replay the remote model.

Focused validation command (from this worktree; no full suite per issue):

Result: **163 passed** before the final transport regression was added; the
added `test_fast_initial_plan_reaches_proxy_without_hidden_reasoning` then
passed separately (**1 passed, 18 deselected**). Thus all 164 current cases in
the listed selection have passed, across those two runs. Ruff lint/format and
`git diff --check` also pass. No live server request was used by the added tests.

```sh
PYTHONPATH=src /Volumes/repos/mtgacoach/.venv_mac_311/bin/python -m pytest \
  tests/test_latency_budget_222_followup.py \
  tests/test_declaration_budget_232.py tests/test_big_board_planner_20261019.py \
  tests/test_game_plan.py tests/test_game_plan_unusable_and_kills_20261019.py \
  tests/test_separate_strategy_planning.py tests/test_game_plan_staleness_20261019.py \
  tests/test_game_plan_lethal_flip_midturn.py tests/test_deck_handoff_20261101.py \
  tests/test_llm_latency_222.py tests/test_decision_cancel_on_request_change.py \
  tests/test_proxy_backend_timeout.py tests/test_proxy_thinking_control.py -q
```

Shared-file conflicts: `action_planner.py` and `game_plan.py` are also owned by
timing/claims streams. This patch touches declaration waiting/call admission,
reform failure cooldown, and reform transport policy only. Preserve their
prompt facts, legality and claim checks. Execution commit `f9b88c8` changes
the same `_hedged_declaration` signature and call site to carry an Arena
deadline. Combine its `arena_deadline` calculation/clamp with this implementation's
`deadline` **and** `usable` arguments; retain the earlier deadline and the bounded
non-hedged call. Run its clock tests alongside this set after integration.

No serving-stack configuration change is established as necessary by these
traces. Remaining qualification: initial plan completion/quality in short games,
model TTFT/stalled-stream behavior under load, legacy backends ignoring timeouts,
total decision-chain elapsed time, and exact historical #241/combat fallback
outcomes. Keep the wider family PARTIAL/open until those are qualified.

## Per-ID disposition supplement

All 23 assigned IDs are mapped below. Control keys resolve to exact pytest node IDs in [the machine-readable audit](latency-222-id-audit-20261101.json). Each entry is **partial, live-unqualified**; a tested mechanism does not prove the historical match outcome or remote root cause.

| ID | Evidence / observed causes | Fix or existing control | Remaining prerequisite |
|---|---|---|---|
| #141 | d9a33232; fresh c667eb4d and 650ae555: Late-game supersede starvation is distinct from first-plan hidden-reasoning delay. Current 650ae555 first request had no content after 45.3s. | S, F2, F3, F4 | Verify initial plan completion/quality and retained late-game plan under live turn changes; no guarantee from request-policy tests. |
| #222 | 2c397f62, dffd55b0: Budget-cut streams and excessive second-answer waiting; ordinary and repair calls could also outlive their caller wait. | P, T, F1, F5 | Correlate end-to-end decision duration, remaining Arena clock, and cancellation acknowledgements after integration; remote saturation remains unqualified. |
| #223 | 2026-10-23 audit: 686 decisions; fresh 650ae555: Background overlap and cancelled/repeated reforms; long failures previously had no effective post-completion cooldown. Initial hosted reasoning remained expensive. | B, F2, F3, F4, F5 | Measure overlap and time to first usable plan on installed build. Existing immediate lethal-flip policy remains deliberate, not deferred wholesale. |
| #231 | d90d0eae, e276ca14: Repeated first-token and total-budget timeouts; 15–31s whole-loop stalls do not by themselves identify one request as the cause. | P, T, F5 | Match request start/end spans and chain waits to the historical stall. Live load qualification needed; no observed exact-case replay. |
| #232 | c63678d8: No first token plus declare-blocker timeout; late declaration hedge could wait despite ready low. Harmful fallback strategy is a separate cause. | P, F1, F5 | Replay exact blocker board with combat owner and qualify cancellation/deadline in live DeclareBlockers; this fix does not change fallback strategy. |
| #239 | 18975f2b: First-token/circuit saturation plus repeated watchdog replies and post-cast hop-limit signature. Multiple layers, not one remote-service defect. | P, T, F5 | Execution owner must qualify repeated OptionalAction responses/hop progression; correlate service and request lifecycle timings. |
| #241 | 4106994f; c7b1185c, 0a0b80ab: Outage fallback pass was treated as strategic hold during a timed-out reversal recheck; already addressed by provenance and board signature controls. | R, T, P | Replay exact Gaea’s Gift window with original memory provenance if available; controls are mechanism tests, not that exact card-state acceptance. |
| #248 | 50668fe5, 447ae530: Budget exhaustion and 16–31s loop delays accompanied by post-cast six-second waits. | P, T, F5 | Separate post-cast waits (#206/#243) from model duration; obtain post-install per-call and chain timings. |
| #249 | 0620a891, a627d4fe: No-first-token/budget timeouts, first-plan delay, benign recovered malformed blocker JSON. | P, T, F1, F4, F5 | Service-load and first-plan live qualification; malformed answer handling belongs to parser stream and is not evidence of universal parse failure. |
| #253 | cd0b1978, e53b79f6: Long decision chains plus schema-shaped empty role and initial-plan lag; follow-up opinions need one shared budget. | T, F2, F3, F4, J, F5 | Execution f9b88c8 owns Arena-clock/chain deadline changes. Verify empty-plan rejection and first availability without conflating it with service TTFT. |
| #256 | 9dc1c55c: First-token/budget delays; rejected unavailable-card win conditions and closed attack window compounded the episode. | P, T, F1, F4, F5 | Unavailable-card strategy (#240) and closed-window combat (#230/#243) need their owners; service timings alone cannot establish causal loss. |
| #260 | 6bef1efb, 06bbdd81, b5f55777: 15–21s stalls without first-token timeout; fallback contradiction, post-cast wait and PayCosts/AutoTap mismatch are separate signals. | T, F5 | Correlate blocking frame/decision-chain time. Execution owns PayCosts request mismatch; claims/fallback owner handles contradictory rationale. |
| #264 | 90b6a56c, 94f1043a, edfd8db0: Milder loop stalls with no first-token or budget failures; one first-plan-start lag. | T, F4, F5 | Need end-to-end spans to explain stalls; cannot infer service delay merely from watchdog warning. First-plan live completion pending. |
| #265 | e756afa4, 15eaac1a: High-effort declaration fails at budget while low could stand; separate malformed typed output and illegal declaration pass. | F1, F5, P | Qualify faster accepted declaration live; parser/legality owner retains malformed/illegal-output work. No exact historical full-game replay. |
| #266 | 04314b21, 8d26e22e, c06d3a82: Two budget exhaustions in one window, first-plan lag, hop-limit(1), and library-only strategy rejection. | P, T, F2, F4, F5 | Identify whether exhausted calls were hedge or serial work; execution owns hop limit and strategy owner owns unavailable-card rejection. |
| #269 | 211d4ccc, 87cac475: Low answer specifically doubted for false lethal; high did not arrive. Bounded wait correctly falls to board math rather than accepting unsafe low. | T, P, F5 | Replay doubt/fallback outcome on exact board; do not remove false-lethal guard to reduce latency. Qualify actual fallback time versus Arena clock. |
| #275 | 788e9968, a2b16e80: One budget-exhaustion/19s loop stall, first-plan lag and hop-limit signature. Companion match had no stalls. | P, T, F4, F5 | Live timing distribution and execution hop progression; one quiet game does not close recurrence. |
| #276 | 228fed6f, 750806ad: decision.plan high used full15s despite first token at5.4s; loop resumed around22s. | P, F1, F5 | Check whether historical low leg was ready; validate first-usable declaration and total loop timing. No causal loss established. |
| #277 | faa03ef3, bfd351f0: High-effort declaration exhausted budget then low stood and attack executed; a direct delayed-winner signature. | F1, F5, P | Live first-valid-winner/cancel confirmation and residual 33s chain timing. First-plan and hop-limit variants remain separate. |
| #283 | 341f42fc, 93237e88: 15–18s loop stalls and one budget exhaustion; correct board-math fallback in companion match. | P, T, F5 | Per-call/chain correlation under load; correct fallback is not evidence the full latency defect is closed. |
| #285 | 78904ea7, fd68fa31, 53bdd320, 10f24c09: High declaration budget cut while low stood; other budget failures/model-unavailable fallback and hop-limit(1). | F1, P, T, F5 | Qualify declaration speed and service timeouts separately; execution owner handles hop progression. No proven caused loss. |
| #286 | 0c12cb47: High declaration exhausted budget before deterministic Sneak guard converted lethal; illegal pass/fallback retry is distinct. | F1, P, F5 | Verify accepted timely declaration without weakening lethal guard; parser/legality retry requires exact-case owner qualification. |
| #289 | 220b8546: Typed low reported4.051s while high exhausted budget and loop stalled19s; big-board fallback and stale strategy later compounded outcome. | T, P, F5 | Current usable-low race test covers wait mechanism, not historical solver choice. Combat owner must replay #288 and strategic owner #240/#287. |

Controls: F1 = e4c06ca: first legal declaration wins; loser cancelled; F2 = e4c06ca: reform and JSON repair share deadline; F3 = e4c06ca: long failure cooldown starts at completion; F4 = e4c06ca: fast initial hosted plan followed by ordinary deeper refresh; F5 = follow-up: cancel non-hedged action/repair calls when caller stops waiting; P = existing proxy budget, bounded retry, health and stream closure controls; T = existing typed first-usable-answer, bounded doubt and shared second-look budget controls; R = existing outage-memory provenance and changed-board reversal controls; S = existing late-game supersede starvation controls; B = existing opponent-turn scheduling/lethal exception and deck handoff controls; J = existing unusable-plan rejection and one bounded JSON retry.

F5 is a new locally reproduced gap: non-hedged `decision.plan` and combat-repair work could continue after its caller deadline because only the future waiter stopped. Each call now owns a cancellation token that is set when the wait exits exceptionally (timeout, cancellation or error); successful calls are not marked cancelled; repair gets a fresh token and retains the original deadline. Three baseline regressions failed before the fix. No provider request is needed to reproduce or test it.

Follow-up validation: **84 passed in 13.01s**. This includes the new cancellation
regressions, unchanged-request damage submission, successful repair, current
typed budget/reversal controls, proxy fake-stream budget checks, and the two
original #141 starvation controls. Exact command:

```sh
PYTHONPATH=src /Volumes/repos/mtgacoach/.venv_mac_311/bin/python -m pytest \
  tests/test_action_plan_cancellation_222.py tests/test_declaration_budget_232.py \
  tests/test_latency_budget_222_followup.py tests/test_decision_cancel_on_request_change.py \
  tests/test_typed_budget_253.py tests/test_decision_memory.py \
  tests/test_reversal_recheck_failure_20261019.py tests/test_proxy_request_metrics.py \
  tests/test_llm_latency_222.py \
  tests/test_autopilot_fixes_20261019.py::test_late_game_sparse_hand_and_consecutive_supersedes_use_low_effort \
  tests/test_autopilot_fixes_20261019.py::test_one_supersede_with_a_sparse_late_hand_keeps_the_late_plan -q
```

The per-ID audit was also validated for exact set equality (23 IDs, no
duplicates), presence of unresolved prerequisites, and existence of every
referenced pytest node. Ruff lint/format and diff checks pass.

Follow-up integration touches only the local `_bounded_complete` helper in
`plan_actions` and its initial/repair call sites in `action_planner.py`, plus
tests/audit documents. Retain the integrated Arena-clamped `model_deadline`.
No new bridge/clock implementation or `game_plan.py` edit is included in this
follow-up. The execution owner's corrected #327 attribution is retained: its
cited timeout was the opponent's seat; #66/#253/#206 clock fixes do not prove
that our player timed out in that incident.

Q1 follow-up evidence was reviewed at
`/Users/joshu/Documents/Codex/2026-10-31/task-12/q1-latency-evidence.json`.
It duplicates the 650ae555 cancellation pair and adds 00573568's first plan,
accepted after **21.2655 seconds**, arriving on turn 4 from turn 1. This
corroborates variable initial latency and the existing keep-late-plan behavior;
it establishes no additional root cause. Its author explicitly did not pin the
runtime revision, so the audit's source/test revision `7c979d8` is not used as
runtime attribution. The recorded requests used low effort and 75-second
budgets, so they cannot qualify the new none-effort/20-second initial policy.
The per-ID audit records this source's SHA-256 and qualification limits.
