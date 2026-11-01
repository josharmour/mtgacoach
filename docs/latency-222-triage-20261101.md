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
