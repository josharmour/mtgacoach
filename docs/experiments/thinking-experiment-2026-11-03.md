# DeepSeek thinking experiment — M5 Mac handoff

Investigation: November 3, 2026. Target checkout: `<checkout>` on
`the Mac`. Windows guest: `vm` / `<Windows VM address>`, reachable from the Mac.

This is a findings report and implementation/runbook handoff, **not an executable
shell script**. No thinking experiment has been implemented or run yet. The
investigation did not change Coach code, application settings, backend routing,
model configuration, or running applications.

## Recommendation

**Test selective low-effort thinking before abandoning DeepSeek, while preserving
the existing fast non-thinking path and Arena deadlines. Do not turn thinking on
for every request.** First replay captured decisions without submitting anything
to Arena; then, if results justify it, run an explicitly enabled live trial on
one client with a fixed deck, queue, build and backend.

The evidence supports **both model-quality and harness/integration problems**:

- The model made a demonstrably incorrect self-targeting decision despite correct
  ownership, toughness and damage information. The harness validated and executed it.
- Slow reasoning-enabled strategic plans frequently failed to finish before being
  superseded. Some requests exhausted their budgets. Connection failures also occurred.
- Actual non-LLM fallback decisions are present in both clients' logs.
- Changing the model also changed thinking policy, prompts and budgets. The
  historical win records are not a controlled comparison of model intelligence.

There is no measurement yet showing that enabling thinking fixes the observed
mistake, improves overall play, or fits the tactical deadline reliably.

## Where the evidence lives

The full collected diagnostic outputs and extraction scripts are preserved
privately on the Mac at:

```text
~/.arenamcp/evidence/thinking-experiment-20261103/
```

That directory contains `manifest.json` with filenames, sizes and SHA-256 hashes,
the two clients' audit JSON files, reconstructed historical match logs, the
specific VM mistake trace, filtered backend configuration, and the scripts used
to extract them. These are private diagnostic captures; keep them out of public
commits, PR attachments and issue reports. This document contains the relevant
results without credentials or opponent names.

Original sources remain on their respective machines:

| Source | Mac | Windows VM |
|---|---|---|
| Runtime log | `~/.arenamcp/standalone.log` | `%USERPROFILE%\.arenamcp\standalone.log` |
| Match history | `~/.arenamcp/match_history/history.json` | Same suffix under `%USERPROFILE%\.arenamcp` |
| Decision packets | `~/.arenamcp/match_packets/packet_*.json` | Same suffix under the Windows data directory |
| Match bundles | `~/.arenamcp/match_bundles/*.json.gz` | Same suffix under the Windows data directory |

The source log was approximately 273 MiB on the Mac and 6.3 MB on the VM. The
Mac inspection covered 500 retained history rows, 250 packets and 100 bundles;
the VM covered 38 history rows, 38 packets and 38 bundles. The older Mac results
were reconstructed from logged packet completions because history is capped at
500 matches. The audit JSON includes historical request aggregates beyond the
tables selected below.

Runtime log timestamps and packet times below are local runtime time. History
timestamps use offsets/UTC; convert them before grouping by local day. Do not
treat a November 4 UTC history row as a future local match.

## 1. Actual win records and their limits

Mac results reconstructed from `Match packet recorded` log entries, deduplicated
by match ID, October 20–November 3:

| Logged backend period | Wins | Losses | Games | Win rate |
|---|---:|---:|---:|---:|
| `glm-5.3-flash`, October 20–29 | 321 | 295 | 616 | 52.1% |
| `current`, October 29–November 3 | 204 | 249 | 453 | 45.0% |
| `current`, before November 3 only | 170 | 178 | 348 | 48.9% |
| `current`, November 3 only | 34 | 71 | 105 | 32.4% |
| VM retained history, all in `current` era | 14 | 24 | 38 | 36.8% |

There were **zero result disagreements** between reconstructed Mac outcomes and
overlapping retained history rows. Mac and VM retained match IDs did not overlap.

Full daily reconstructed Mac counts:

| Local date | Logged model | Wins | Losses |
|---|---|---:|---:|
| October 20 | GLM | 34 | 28 |
| October 21 | GLM | 16 | 19 |
| October 23 | GLM | 5 | 4 |
| October 24 | GLM | 19 | 23 |
| October 25 | GLM | 88 | 73 |
| October 26 | GLM | 40 | 26 |
| October 27 | GLM | 70 | 61 |
| October 28 | GLM | 46 | 55 |
| October 29 | GLM | 3 | 6 |
| October 29 | current | 5 | 7 |
| October 30 | current | 17 | 23 |
| October 31 | current | 84 | 89 |
| November 1 | current | 40 | 39 |
| November 2 | current | 24 | 20 |
| November 3 | current | 34 | 71 |

No row means no reconstructed records in this extraction, not proof that no
games were played that day.

The recent Mac packet set contains 191 packets with the mono-green Halfling /
Eldrazi-Titan playbook, 33 Boros Mobilize packets, 20 mono-white enchantment
packets, and 6 without a playbook. The VM has 37 mono-red haste/prowess/burn
packets and 1 without a playbook. Deck descriptions are playbook labels, not
verified identical 60/100-card lists.

For retained Mac history grouped by local **completion** date, Brawl was 37–29
on November 1, 14–9 on November 2 and 25–59 on November 3. Packet-start-date
counts differ slightly around midnight. The sharp recent drop occurred within
the `current` era as well as across the older GLM/current comparison.

**Limits:** decks, queues, rank/opponents, code versions, server load and
manual/autoplay behavior differ. This establishes a recorded decline, not its
causal attribution. Do not claim that DeepSeek costs exactly 7.1 percentage
points or that every recorded game was fully autonomous.

## 2. Model identity, clients and server configuration

The Mac log's last observed GLM request was **October 29 at 07:47:53.619**.
The first observed `current` request was **October 29 at 17:55:54.976**.
Both transition-period client diagnostics used `http://<gateway address>:8444/v1`.
Recent VM diagnostics use `https://api.mtgacoach.com/v1`.

Live, read-only inspection of the gateway on `blackwell` found:

| Alias | Underlying model | Upstream |
|---|---|---|
| current | `openai/deepseek-v4.1-flash-local` | `http://coding-admission:8080/v1` |
| current | `openai/deepseek-v4.1-flash-local` | `http://dsv41-flash-trial:8001/v1` |

The inference container `dsv41-flash-trial` had been running since
`2026-10-30T22:06:46Z`. Selected serving arguments:

```text
--served-model-name deepseek-v4.1-flash-local
--quantization exl3
--tensor-parallel-size 2
--max-model-len 819200
--gpu-memory-utilization 0.92
--max-num-seqs 8
--tool-call-parser deepseek_v41
--reasoning-parser deepseek_v41
--scheduling-policy priority
```

The gateway container was created November 3 at 05:48 UTC and its recorded
latest start was 05:54 UTC. These timestamps and route entries are observations,
not evidence that a particular match's request used a particular upstream or
was queued behind coding traffic. No historical per-request GPU queue attribution
was established. The `current` alias alone cannot identify immutable historical
weights, quantization, server revision or route.

All 100 recent Mac bundles reported app **3.8.3**: 64 used Python 3.12.13 and
36 used Python 3.11.18. The Mac was running the packaged
`/Applications/MTGA Coach (master).app`, started November 3 around 15:33.
The checkout's observed HEAD was `502489d`; that does **not** prove the packaged
process loaded that exact commit.

VM bundles: 28 at 3.8.3, 5 at 3.8.0, 1 at 3.8.1 and 4 at 3.7.0. The VM was
running Arena but no Coach process at the later inventory snapshot. No process
was stopped by this investigation. App version strings are not enough to prove
identical code, prompts or bridge versions.

The Mac's inspected settings had `model=current`, an empty saved gateway
override, `separate_strategy_planning=true`, and `autopilot_enabled=false` at
the snapshot. Do not project these current settings backward onto every match.

## 3. Confirmed fallback to non-LLM play

**Yes: latency and unavailability caused deterministic fallback decisions.**
These are explicit November 3 `typed-decision fallback` log entries:

| Logged reason | Mac | VM |
|---|---:|---:|
| model unavailable | 16 | 16 |
| model too slow | 1 | 0 |
| no usable model answer | 11 | 2 |
| the model's pick was set aside | 5 | 2 |

The **33 unavailable/too-slow entries** map to eight matches:

| Client | Match ID prefix | Count | Local event window | Result |
|---|---|---:|---|---|
| Mac | ae7d8ea3 | 16 | 08:27:51–08:30:01 | Win |
| Mac | d8752a81 | 1 | 17:46:23 | Loss |
| VM | 5c38a4b6 | 3 | 16:17:57–16:18:05 | Loss |
| VM | 76931616 | 1 | 17:09:08 | Win |
| VM | 6885dcb4 | 3 | 17:19:58–17:20:05 | Loss |
| VM | 268f9976 | 1 | 17:38:08 | Loss |
| VM | e9b2d5fa | 4 | 17:39:58–17:40:03 | Win |
| VM | 1c2014cb | 4 | 17:51:13–17:51:20 | Loss |

Specific evidence:

- **Mac 17:46:23.758**, `d8752a81-1292-489d-bede-13d52bb7fa44`: first-token
  timeout after 11.1 seconds, followed by `fallback (model too slow)` and a pass.
- **Mac 08:27–08:30**, `ae7d8ea3-29c6-4e93-93da-9bc845a3e3f0`: unavailable-model
  fallbacks played Forest, cast Kami of Bamboo Groves / Badgermole Cub /
  Tamiyo's Safekeeping, and passed in other windows. Deterministic play does
  not necessarily mean inactivity; this match was won.
- **VM 16:17–16:18**, `5c38a4b6-1427-4922-a5ae-ed717a9c389b`: connection failures
  led to unavailable-model fallback passes.

Counts are logged fallback decisions, not all fallback mechanisms or independent
model calls. A decision can make zero, one or several requests. Watchdog default
submissions and deterministic combat are separate paths. Do not divide these
counts by request counts and label the result a gameplay fallback percentage.

**Important distinctions:**

1. Thinking disabled is still LLM-based play.
2. A canceled background plan can leave the prior plan in use while tactical
   LLM calls continue. Cancellation is not automatically a no-LLM turn.
3. No answer before the deadline, connection failure, malformed/unusable output,
   and a deliberately rejected model choice are different failure classes.
4. Not every unavailable event is evidence of slow GPU inference. A circuit-open
   client can skip requests after a preceding connection failure.
5. These eight matches finished 3–5. That association does not prove fallback
   caused all five losses, nor explain every other loss.

## 4. Thinking latency and plan completion

November 3 request metrics, combining entries with and without `served_model`:

| Client / call | Effort | Requests | OK | Canceled | Error |
|---|---|---:|---:|---:|---:|
| Mac background game plan | low | 595 | 195 | 358 | 42 |
| Mac background game plan | none | 470 | 451 | 18 | 1 |
| VM background game plan | low | 111 | 25 | 83 | 3 |
| VM background game plan | none | 114 | 107 | 5 | 2 |
| Mac October 28 GLM background game plan | low | 250 | 204 | 46 | 0 |

`OK` means transport completion, **not** that JSON was valid, the strategy was
correct, or the plan was accepted and applied. Superseding obsolete work is
sometimes correct. The compact and deep planning prompts/budgets differ, so this
table is not a controlled thinking-on/off benchmark.

For the November 3 Mac subgroup that reported `served_model=current`:

| Call / effort | Requests | Median total time | 95th-percentile total time |
|---|---:|---:|---:|
| Game plan / low | 593 | 23.70 s | 75.00 s |
| Game plan / none | 469 | 5.74 s | 11.34 s |
| Typed tactical decision / none | 4,059 | 1.92 s | 3.65 s |

The quantiles include canceled/failed calls in those subgroups; they are not
success-only latency estimates. Two low-planning requests and one compact
request without a served-model value account for the denominator differences.

All November 3 Mac typed requests: **4,091**, with 4,047 OK, 39 canceled,
4 errors and 1 first-token timeout. VM typed requests: **749**, with 639 OK,
86 canceled and 24 errors. The VM served-current typed subgroup had a 1.90 s
median. Routine tactical inference was therefore not uniformly slow.

### An exact recent reasoning delay

Mac November 3 **17:52:48.114**, in match
`7d69a4b9-22b3-45f5-8b6a-721a5cd10b4f`, logged this game-plan request:

```text
response_id: chatcmpl-acec84dc49043fe1
call_class: background.game_plan
reasoning_effort: low
budget_s: 75
lane_wait_ms: 0
first_reasoning_ms: 1843.8
stream_reasoning_chars: 7706
stream_content_chars: 0
total_ms: 61684.0
status: cancelled
```

The server began producing reasoning after about 1.8 seconds, but the request
had no answer content after 61.7 seconds. This was a cancellation, not a claim
that the full 75-second deadline elapsed. It shows why reducing client-side
queueing alone cannot solve all delays. `lane_wait_ms=0` does not measure the
server's internal queue or contention during generation.

The next compact request, at 17:52:52.587, completed in **4.42 seconds**, effort
`none`, 1,037 content characters, no reasoning characters, budget 20 seconds.
Again, it used a different planning prompt/output budget, not a matched A/B input.

### Earlier recorded evidence

[The October 31 latency investigation](docs/latency-222-triage-20261101.md)
and [its selected request metrics](docs/evidence/latency-222-20261101.json)
already documented match `650ae555-192a-4f2c-aedb-a5b30d065e5e`:

- Initial low-effort plan: 45.300 seconds, 12,439 reasoning characters,
  zero answer characters, then superseded.
- Replacement: first answer content after 23.513 seconds; canceled at game end
  after 24.238 seconds with only 59 content characters. No plan completed.
- A nearby tactical request completed in 3.670 seconds. The service was not
  uniformly unavailable.
- **That match was won.** This documents a failure mechanism, not a proven loss.

November 3 Mac warning extraction also found 38 `LLM streaming time budget
exhausted` API error entries and 32 retryable connection-error entries. Those
are log-entry counts, not 70 distinct affected games or necessarily independent
incidents. The VM had its own connection errors and fallback episodes.

## 5. Confirmed model mistake that passed validation

VM match **`233ea039-b6b7-455e-b945-20bd5db04a87`**, November 3,
16:43:12–16:52:16, result loss. Relevant sequence:

| Time | Observation |
|---|---|
| 16:45:39.287 | Model chooses to cast Burst Lightning, claiming it pumps Slickshot to 7/2 so it survives the opponent's Shock. |
| 16:45:41.883 | Model chooses unkicked mode and repeats the claimed survival benefit. |
| 16:45:45.358 | Model chooses `tgt:287`: **its own Slickshot Show-Off**. |
| 16:45:46.152 | Harness submits that target. |
| 16:45:46.445 | OOPS observer records that our Burst Lightning dealt 2 damage to our Slickshot and it died. |

The selected target's captured facts were:

```json
{
  "option_id": "tgt:287",
  "name": "Slickshot Show-Off",
  "controller_seat_id": 2,
  "owner_seat_id": 2,
  "pt": "5/2",
  "dies_to_damage": 2,
  "arena_marks": "bad target for you"
}
```

The player's local seat was 2. Other offered targets were the opponent
(`tgt:1`, 13 life) and self (`tgt:2`, 20 life). The source was identified as
**Burst Lightning** with rules saying 2 damage to any target, 4 if kicked.

The captured model explanation was:

> Targeting our own Slickshot with Burst Lightning pumps it to 7/2 so it
> survives the opponent's Shock and remains our lethal attacker.

The trace recorded:

```json
{
  "selected_ids": ["tgt:287"],
  "target_controllers": {"tgt:287": "self"},
  "validated_ids": ["tgt:287"],
  "target_validation": "validated"
}
```

This is an intentional model choice using false toughness arithmetic, not a
controller-ID mapping mistake. The harness had enough target information to
recognize the damage risk but accepted the decision.

Current `_gate_harmful_llm_target_picks` permits exceptions for acknowledged
unusual targets with a nonempty explanation. A supplied explanation does not
establish that the asserted survival benefit is true. The saved trace does not
include the raw `unusual_target_reasons` field, so do not invent its exact value
or claim to have fully replayed this guard from the packet alone.

**Causal limit:** an opposing Shock already threatened Slickshot. This does not
prove it would otherwise have survived or that another play wins the game. It
does prove faulty reasoning and self-harming execution. The desired replay
assertion is rejection of the false survival claim, not a guaranteed match win.

## 6. Findings that must not be overstated

### The 44.5-second blocker stall is not a proven latency-caused loss

At 17:59:46 in Mac match `7d69a4b9`, the watchdog submitted a default after
44.5 seconds without an answer. Closer inspection showed:

- Autoplay was off: `recommend only: autoplay is off`.
- The position was already at 1 life with approximately 66+ damage projected.
- The fallback submitted no blocks; its trace had zero model requests.

The investigation initially highlighted this as a harness failure before
checking the surrounding state. **Do not use it as evidence that a slow model
or a delayed blocker decision caused this loss.** It remains useful evidence
about watchdog behavior while autoplay is off.

### Contradiction warnings are not counts of wrong actions

There were 58 Mac and 22 VM `typed-decision reasoning contradicts the board`
warning entries on November 3. Some were real rule mistakes; others were noisy
checks of explanatory text. For example, Mac 00:02:24 warned that Green Sun's
Zenith was not an offered search target, while the selected option was actually
a Forest. The Forest remained selected and was submitted. Another warning
mentioned The Notary Hobbits while the explanation was about ramping toward it.
Do not count these warnings as 80 failed actions or model blunders.

The 245 Mac `post-cast priority: hop limit` warnings are also not automatically
245 missed decisions: subsequent outer-loop handling can complete the request.
Two VM and three Mac `Coaching loop stalled` warnings were observed on November
3; the complete causes of those stalls were not reconstructed here.

## 7. Why a thinking toggle is not enough

Relevant source:

- [Hosted request policy](src/arenamcp/backends/proxy.py), `ProxyBackend.complete`
  and `_build_params`.
- [Decision races and target validation](src/arenamcp/action_planner.py),
  `_hedged_decision`, `_hedged_declaration`, `_gate_harmful_llm_target_picks`.
- [Deck-analysis policy](src/arenamcp/coach.py), `_PLAYBOOK_PASS_SETTINGS` and
  `_HOSTED_PLAYBOOK_SETTINGS`.
- [Live planning policy](src/arenamcp/game_plan.py), `_reform`.

For `current`, DeepSeek or DSV4 names, `ProxyBackend.complete` **overrides all
`decision.*` calls to `reasoning_effort="none"`**, including callers that asked
for high effort. `_build_params` then sends:

```json
{"chat_template_kwargs": {"thinking": false, "reasoning_effort": "none"}}
```

Thus the current planner's nominal high/low race does not produce a genuine
thinking/non-thinking race on DeepSeek. Merely changing `enable_thinking=True`
or the server default will not override this client policy.

Background game-plan high/max requests are reduced to low effort. Compact
initial/recovery/routine plans can explicitly request none. Hosted deck discovery
uses none with a 1,536-token ceiling and a compact notes prompt, versus high
effort and a 12,000-token ceiling for legacy discovery. The final deck compilation
budget remains 12,288. These are meaningful policy differences, not a pure model
swap. A server-only rollback behind `current` will not recreate the old explicitly
named GLM client policy automatically.

The tactical policy change is commit **`ac60736`**, October 29, titled
“Restore grounded announcer speech and use current gateway within game deadlines.”
Its parent already contained parallel declaration and typed-decision calls.
The hosted deck-analysis budget change appears in **`9f9cd92`**, October 31.
Later changes included bounded replacement plans (**`264334b`**, November 2)
and compact live planning / selected-deck preparation (**`edd32e3`**, November 2).
Source fixes, installed versions and actual running processes must be tracked
separately; merging a fix does not qualify live performance.

## 8. Parallelism: what is already fixed and what remains

The user's hypothesis is reasonable: eliminating unnecessary sequential waits
can make selective reasoning more practical. However, current evidence does not
support “all old timeouts were due to serial calls.”

- `_hedged_decision` launches both legs in separate threads. Declaration planning
  also launches two calls. Both mechanisms existed before the thinking-disable
  commit; this was checked against its parent source.
- Later repairs stopped waiting for a slow high-effort declaration after a legal
  low-effort answer was ready. JSON repairs now share the original deadline.
- Tactical requests bypass the serialized background lane. A main background
  lane limits competing background work; optional commentary is preemptible.
- If a game-plan reform refuses to yield, the typed planner deliberately skips
  the second/high leg. Sending three heavy requests together had previously
  caused first-token timeouts. It also skips that leg when service health degrades.
- The backend currently admits up to eight sequences. Concurrent requests still
  share GPUs, prefill and decoding resources. More concurrency does not guarantee
  lower per-request latency.
- A first reasoning token is not an actionable JSON answer. The 61.7-second
  example above had no client-lane wait and was already generating reasoning.
- Recent request metrics had `priority=None`. The server supports priority
  scheduling, but the moving alias does not automatically assert that capability
  in the client. Whether to enable it is a separate measured configuration
  question, not an assumption to silently change during this experiment.

Therefore preserve the scheduling repairs, but measure thinking's incremental
generation cost and contention. Do not disable the background lane or extend
Arena deadlines as part of the initial test.

## 9. Experiment design to implement on the Mac

### A. Preflight and baseline capture

1. Read `AGENTS.md` and `DEV.md`. Inspect current source and existing changes;
   do not overwrite the unrelated UI/rank/autopilot edits present during this
   investigation. Use an isolated worktree for code changes.
2. Identify the actual executable, runtime, source commit, settings and bridge
   version used by each trial. The observed Mac process was packaged, so editing
   `<checkout>/src` alone may not affect it.
3. Preserve the source packets/bundles and diagnostic evidence directory before
   retention removes them. Never include authentication in replay output.
4. Record actual gateway model route and serving revision at trial time. Keep
   model, quantization and server configuration constant between conditions.
5. Run the existing relevant regressions once to establish the current baseline.
   Use the Mac's working `.venv_mac_311`, after verifying that interpreter works.

Read-only preflight examples:

```bash
cd <checkout>
git status --short
git rev-parse HEAD
.venv_mac_311/bin/python -c 'import sys; print(sys.executable); print(sys.version)'
```

Existing focused tests to start with (add the new experiment's tests after
implementation; these do not measure live model quality):

```bash
PYTHONPATH=src .venv_mac_311/bin/python -m pytest \
  tests/test_proxy_thinking_control.py \
  tests/test_latency_budget_222_followup.py \
  tests/test_declaration_budget_232.py \
  tests/test_proxy_backend_timeout.py \
  tests/test_decision_cancel_on_request_change.py -q
```

The Linux-side checkout's `.venv/bin/python` could not initialize its standard
library during the investigation; the system Python lacked pytest dependencies.
No fresh regression pass was claimed. Existing documented receipts are evidence
of previous runs, not tests rerun for this report.

### B. Offline paired replay first

Build a replay runner that submits captured/reconstructed positions to the model
but **never calls the Arena bridge, clicks the UI, or mutates live match state**.
There is not currently an implemented replay command in this document. Do not
pretend that setting a suggested environment variable already enables this test.

Use these conditions on the same inputs:

| Condition | Model | Thinking | Purpose |
|---|---|---|---|
| A | pinned DeepSeek V4.1 Flash | none | Current-policy baseline |
| B | same model/server | low | Isolate incremental thinking cost and benefit |
| C, later if available | pinned GLM | its explicit compatible policy | Compare model alternatives after A/B |

Keep system/user messages, supplied facts, option IDs, temperature and output
schema identical within each paired case. Log exact requested/effective options.
Start with the existing tactical token budget; record whether thinking consumes
that budget and truncates the final answer. If a larger token cap is tested,
label it as a separate condition and keep the wall-clock deadline unchanged.

Include:

- The Slickshot/Burst Lightning cast, mode and target sequence above.
- The VM Hired Claw explanations claiming haste despite the captured no-haste
  correction (November 3, 15:51–15:53), after reconstructing the actual state.
- Mac Finale of Devastation X=4 reasoning that named a mana-value-5 target
  (November 3, 01:34:40), with the exact options/rules reconstructed.
- Ordinary correct actions, legal intentional self-targeting, combat choices,
  passes and simple menus as negative controls. Do not evaluate only known failures.

The packets contain options and traces, but **not necessarily the complete
original prompt, full intermediate state, or raw model JSON**. Reconstruct
from packet/bundle/log evidence where possible; label reconstructed cases and
their missing information. Do not silently use the final-state board as though
it were the board at the earlier decision. Save exact prompts for every new trial.

Use multiple attempts per condition and alternate/randomize their order to reduce
cache/load/order effects. Do not run A and B concurrently in the initial quality
comparison: first measure each, then separately measure the proposed concurrent
execution to quantify contention. Reasoning can help without being fast enough
for live use, and a single correct retry does not establish reliable improvement.

### C. Explicit, local, opt-in live policy

If offline results justify it, implement a clearly named, default-off experiment
policy that can genuinely reach the transport despite the current override.
Keep normal production behavior unchanged when the option is absent.

- Fast leg: existing non-thinking request.
- Thinking leg: low-effort reasoning for selected consequential decisions, not
  unbounded high/max reasoning for everything.
- Both share the original Arena-aware decision deadline. Never start a leg or
  retry after that deadline; cancel superseded/losing calls promptly.
- Preserve the current restrictions when the service is degraded or the
  background game-plan worker cannot yield. Keep concurrency bounded.
- Keep compact background planning, commentary and probes unchanged initially.
- A valid fast answer may execute immediately; a specifically unsafe/doubted
  answer may receive a bounded second opinion. Specify which condition lets the
  thinking answer affect the action. A thinking leg that always loses and never
  changes a decision is added load, not a quality improvement.
- Both answers go through the same legality, ownership, target and rules checks.
  A longer explanation must not automatically bypass those checks.
- Preserve the explicit GLM request behavior and its regression tests.

An independent correction to the self-harm validation gap is justified, but do
not silently combine that fix with the thinking experiment and attribute all
improvement to reasoning. Test and report it separately, or hold it identical
in both trial arms.

### D. Required instrumentation and meaningful regressions

For every decision, record:

- Match ID, decision/request identity, source commit, model/server identity,
  actual thinking/effort, prompt hash and original deadline.
- Client-lane wait, request start, first reasoning token, first answer content,
  complete-answer time, token use, finish reason and any server queue measure
  that is actually available. Never relabel client-lane wait as server queue time.
- Fast/thinking leg outcomes; selected leg; why it won; whether the thinking
  answer changed the action; whether it was canceled after another leg won.
- Parsing, legality and factual validation; fallback reason; final submitted
  action; bridge acknowledgment; state changes between request and submission.
- Plan returned, plan accepted and plan applied as separate events.

Regressions should verify actual outbound parameters, the default-off path,
no accidental conversion of both legs to `none`, shared deadlines, valid fast
answer versus hanging thinker, invalid fast answer versus valid thinker,
request-change cancellation, degraded-service/background-busy admission, and
no requests admitted after an expired deadline. Use real branching behavior,
not tests that merely mirror a configuration dictionary.

### E. Live qualification and rollback

Only install/switch the trial runtime between games. Do not replace a running
checkout, interrupt Arena, reinject the native bridge, or alter the shared server
to activate this experiment. Verify the next process actually loaded the intended
build and effective request policy before attributing results to it.

Start on one client, with one unchanged deck and queue. Alternate short A/B
blocks rather than doing one model on one day and another on a different day.
Record manual interventions, opponent/rank context, play/draw, server load and
concessions. Keep the other client's workload controlled or at least recorded.

Before running, choose a bounded replay/live sample size and stopping criteria.
Recommended gates:

- Thinking corrects captured rule/strategy mistakes reproducibly, without
  breaking negative controls.
- Complete valid answers fit existing deadlines; no increase in rope/default
  events attributable to the added leg is accepted during the initial trial.
- The fast leg's latency does not materially regress under concurrent load.
- The thinking leg actually contributes useful selected decisions.

These are proposed acceptance criteria, not already measured results. Use
per-decision quality and deadline/fallback behavior as the initial signal; a
small live sample cannot establish a win-rate improvement.

Rollback means disabling the local opt-in policy and restoring the known baseline
runtime at the next safe between-game boundary. Do not roll the shared inference
service back to GLM as an implicit side effect of this test.

## 10. Ready-to-use task prompt for an agent running on the M5 Mac

> Read `thinking-experiment.md`, `AGENTS.md` and `DEV.md` in
> `<checkout>`, plus the private evidence manifest at
> `~/.arenamcp/evidence/thinking-experiment-20261103/manifest.json`.
> Preserve existing edits and active games. Work in an isolated worktree.
> Implement the offline paired DeepSeek thinking-none versus thinking-low replay
> first, using the actual captured cases and clearly marking reconstructed inputs.
> Do not submit replay actions to Arena. Record effective transport options,
> complete-answer latency, factual correctness, parser/validation results,
> cancellation and fallback. Establish whether low thinking improves the observed
> Slickshot error within current tactical deadlines. Then prepare a default-off,
> local selective-thinking experiment that preserves the existing fast leg,
> bounded concurrency, Arena deadlines, cancellation and GLM behavior. Do not
> merely flip enable_thinking: the current proxy overrides decision calls to none.
> Add focused behavioral regressions and run the relevant checks in DEV.md.
> Report results separately from existing harness correctness fixes. Do not
> switch the live client or alter the shared backend as part of replay/implementation;
> provide a concrete trial command and rollback procedure for the resulting build.
> Keep private captures and credentials out of commits. No sub-agent work is required.

## Evidence inventory

| Preserved file | Contents |
|---|---|
| `mtgacoach-mac-audit.json` | Retained history/packet/bundle summaries, daily/model/call/effort request aggregates, selected events and historical model lines |
| `mtgacoach-mac-logmatches.json` | Deduplicated historical packet outcomes, backend transitions, November 3 warnings |
| `mtgacoach-vm-audit.json` | Corresponding VM records and request aggregates |
| `mtgacoach-vm-logmatches.json` | VM backend transitions, packet results and warnings |
| `mtgacoach-vm-case.txt` | Bounded runtime excerpts and captured decisions for the Slickshot incident |
| `mtgacoach-server-audit.txt` | Filtered container settings and verified gateway model routes; no API keys |
| `mtgacoach-diagnosis-2026-11-03.txt` | Earlier consolidated diagnostic report |
| `mtgacoach_audit.py` | Read-only history/packet/bundle and request-metrics extractor |
| `mtgacoach_logmatches.py` | Read-only historical result/transition/warning extractor |
| `mtgacoach_vm_case.py` | Read-only bounded incident extractor |
| `mtgacoach_server_audit.py` | Read-only filtered serving configuration/routing extractor |

The extraction scripts are diagnostic helpers, not the proposed model replay
runner. Review paths/encoding on the target host before reusing them. The saved
manifest hashes the extracted artifacts; it is not a claim that the original
live logs remained unchanged after capture.


## Execution update — November 3, 2026, 22:10 Pacific

The initial investigation-only status above is historical. Selective DeepSeek
thinking was implemented default-off on `codex/dsv41-thinking-replay`, including
an independent lethal self-targeting guard. The concurrent selective-thinking
qualification failed under shared load; it was not enabled in live clients.

The subsequently authorized comparison restored the previous GLM-5.3-Flash
QAD NVFP4-A16/MXFP8 backend behind the unchanged callable `current` alias.
Standard/coding access groups and token limits remain unchanged. A route-aware
gateway compatibility adjustment maps legacy DeepSeek thinking flags to GLM low
and preserves explicit GLM high/max. Both standard and coding routes were
verified. Clients and trial keys do not require an update for this model switch.
The independent targeting guard remains staged and tested, not installed in the
currently running packaged clients.

The user resumed both Mac and Windows on their existing decks. Another trial
customer is also active, so these observations represent shared production load,
not an isolated two-client experiment. Through an early recovered-state sample,
51 Mac and 17 Windows tactical requests completed within their own deadlines;
maximum successful latencies were 7.58 s and 6.64 s. No inference timeout was
found in that sample. This is preliminary operational evidence, not proof of a
win-rate or intelligence improvement.

The transition was not seamless: trial-key logs show 22 failed request entries
during the model reload, and Windows logged two HTTP 502 errors during the later
gateway compatibility restart. Draining active requests did not prevent new
requests arriving during downtime. Subsequent trial traffic was successfully
served by GLM. Synthetic replay was stopped while the live games ran.

Private evidence, exact configuration changes, latency samples, and the prepared
recovery procedure are recorded in
`~/.arenamcp/evidence/glm-low-trial-20261103/report.md`.

Client follow-up at 22:20 Pacific: the private guarded Mac launch failed to
provide a normal app window and blocked relaunch. It was stopped, and the
unchanged installed master 1.0.19/a2afb64b app was restored and visibly verified
ready with Bridge, Autoplay On, and Auto-queue On. The targeting guard remains
undeployed; a qualified managed update is still needed. GLM serving continued
unchanged. Two Mac wins and one Windows win were observed before that attempted
guard activation; this remains too small a sample for a causal claim.
