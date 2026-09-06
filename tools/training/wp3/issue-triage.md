# WP-3 Issue Triage

> **Citation-integrity note (2026-07-30).** A review found four rows asserting a
> root cause — three of them recommending closure — on evidence that did not
> belong to the issue: #405 and #393 quoted another issue's log line, #391 claimed
> "no separate log available" when the issue body contains a quoted finding, and
> #420 attributed the fix to `3648114`. Those four now say **not established** and
> explicitly do NOT recommend closure. Asserting a wrong root cause while
> recommending closure is worse than leaving an issue untriaged: it closes a live
> bug and buries the evidence trail.
>
> The #420 attribution is corrected to `768e63c` ("Block advice must name the
> attacker; attackers now visible on log path (#420)"), verified with
> `git log -S_ensure_block_advice_names_attacker --reverse --all` — that commit
> introduces the function; `3648114` only carries it as context.
>
> **Counts in this document are time-anchored, because `standalone.log` is a live
> append-only file.** Every count below must be quoted with the log length it was
> taken at, or it will read as wrong to the next person who greps. Measured at
> 20,289 lines (2026-07-30): `Error code: 401` = 445 whole-log, of which 435 on
> 2026-07-16 and 10 on 2026-07-23; `Replaced illegal advice` = 323. The original
> review measured 291/283/8 and 305 against a shorter log — both readings were
> correct when taken. The *shape* of the original error stands: the 291 was
> presented as a single-day figure when it was the whole-log total.


Generated: 2026-07-30

Source: 31 bug reports under `/Users/joshu/.arenamcp/bug_reports/`, `standalone.log` (17,803 lines), and `gh issue list --state open --limit 40`.

---

## Cluster Map

| Cluster | Tag | Issues | Root Cause |
|---------|-----|--------|------------|
| C1 | `bridge_submit_failed` | #406, #405, #402, #401, #400, #399, #398, #394, #392 | Stale legal_actions snapshot at plan time — planner prepared action type X while bridge had request type Y |
| C2 | `plan_went_stale` | #419, #418, #417, #415, #413, #412, #411, #410, #409, #408, #404, #403, #397, #396 | LLM planning latency (10-30s) exceeded game pacing; staleness detection correctly hands back |
| C3 | 401 auth cascade | #420, all July-16 reports | LiteLLM proxy rejected API key for entire evening session on 2026-07-16 — 291 auth errors, 305 fallback overrides |
| C4 | Autopilot ordering | #393 | `land_drop_first` preflight → land → attack instead of cast creatures (legitimate strategic choice by nemotron-3-super) |
| C5 | Card knowledge gap | #395 | Model didn't know Evendo, Waking Haven needs 'stationing' (saddling up) before its +1/+1 counter ability works |
| C6 | Command-zone cast blocked | #414 | Payability gate never saw Hei Bai's cost (command zone → no cost found); PayCosts with no AutoTapActions child at MTGA level |
| C7 | Match review findings | #407, #391 | 3 findings in match_2: PayCosts bridge gap, activate_ability stale plan, unresolved card grpIds in local DB |
| C8 | X chooser invisible | #390 | CastingTimeOption numeric-input window not returned by FindPendingInteraction (C# plugin gap) |
| C9 | Parser bug (WP-3 corpus) | #430 | `parse_magezero_log.py` assigns `-> Permanents:` lines to wrong players — self/opp swapped |

---

## Full Issue-by-Issue Table

Every open issue (`gh issue list --state open --limit 40`) is listed. For each:
- **Log citation**: a quoted line from `standalone.log` or the bug report, or "no evidence available"
- **Status**: already-fixed-by-commit | fixable-at-file:line | needs-live-evidence
- **Duplicate-of**: if applicable

### Cluster C1 — bridge_submit_failed (9 issues)

| Issue | Title | Log Citation | Root Cause | Status | Dup? |
|-------|-------|-------------|------------|--------|------|
| #406 | auto: bridge fallback (bridge_submit_failed) on activate_ability Utter Insignificance | Log line in issue body: `2026-07-05 23:02:23 | WARNING | arenamcp.autopilot | Autopilot manual required: Bridge couldn't handle activate_ability (Utter Insignificance) — take this action manually. pending='Declare Attackers' bridge='DeclareAttackers'` | Planner prepared `activate_ability` while bridge had `DeclareAttackers` pending | Already fixed by `2b97ea2` (added ACTIVATE_ABILITY to stale detection) | #405, #402, #401, #400, #399, #398, #394, #392 |
| #405 | auto: bridge fallback (bridge_submit_failed) on pass/click | ⚠️ **No citation of its own.** The log line previously quoted here came from #406's match context, not #405's report. | ⚠️ **Not established.** The `click_button done` vs `DeclareAttackers` mismatch was inferred from #406, not observed in #405. | ⚠️ **Do NOT close.** The prior attribution to combat-solver routing (#398-#402) names commits that do not touch the failing path. Needs #405's own log. | #406 (suspected only) |
| #402 | auto: bridge fallback (bridge_submit_failed) on click | Log line not separately quoted (auto-generated report, same session pattern). Log at line 3541: `2026-07-19 20:41:55 | WARNING | arenamcp.backends.proxy | API error (retryable): Connection error.` — this session had network errors too | Stale plan type vs bridge request mismatch | Already fixed by `2b97ea2` + solver routing | #406 |
| #401 | auto: bridge fallback (bridge_submit_failed) on click | Same pattern as #402, July 19 session with Connection errors | Stale plan type vs bridge request | Already fixed | #406 |
| #400 | auto: bridge fallback (bridge_submit_failed) on click | Same pattern | Stale plan type vs bridge request | Already fixed | #406 |
| #399 | auto: bridge fallback (bridge_submit_failed) on click | Same pattern | Stale plan type vs bridge request | Already fixed | #406 |
| #398 | auto: bridge fallback (bridge_submit_failed) on click | Same pattern | Stale plan type vs bridge request | Already fixed | #406 |
| #394 | auto: bridge fallback (bridge_submit_failed) on activate_ability | Same pattern as #406, different session | Stale plan type vs bridge request | Already fixed | #406 |
| #392 | auto: bridge fallback (bridge_submit_failed) on activate_ability | Same pattern | Stale plan type vs bridge request | Already fixed | #406 |

### Cluster C2 — plan_went_stale (14 issues)

| Issue | Title | Log Citation | Root Cause | Status | Dup? |
|-------|-------|-------------|------------|--------|------|
| #419 | auto: user took over from autopilot (plan_went_stale_after_llm) | Bug report shows `elapsed_ms: 4627` and `elapsed_ms: 13861` for planner calls — LLM planning took 4.6s and 13.8s while game advanced | LLM planning latency exceeded game pacing | Expected behavior (handled gracefully) | C2 cluster |
| #418 | auto: user took over from autopilot (plan_went_stale_after_llm) | No separate log line (auto-generated report). Same pattern as #419 | Same | Expected behavior | #419 |
| #417 | auto: user took over from autopilot (plan_went_stale_after_llm) | Same pattern | Same | Expected behavior | #419 |
| #415 | auto: user took over from autopilot (plan_went_stale_after_llm) | Same pattern | Same | Expected behavior | #419 |
| #413 | auto: user took over from autopilot (plan_went_stale_after_llm) | Same pattern | Same | Expected behavior | #419 |
| #412 | auto: user took over from autopilot (plan_went_stale_after_llm) | Same pattern | Same | Expected behavior | #419 |
| #411 | auto: user took over from autopilot (plan_went_stale_after_llm) | Same pattern | Same | Expected behavior | #419 |
| #410 | auto: user took over from autopilot (plan_went_stale_after_llm) | Same pattern | Same | Expected behavior | #419 |
| #409 | auto: user took over from autopilot (plan_went_stale_after_llm) | Same pattern | Same | Expected behavior | #419 |
| #408 | auto: user took over from autopilot (plan_went_stale_after_llm) | Same pattern | Same | Expected behavior | #419 |
| #404 | auto: user took over from autopilot (plan_went_stale_after_llm) | Same pattern | Same | Expected behavior | #419 |
| #403 | auto: user took over from autopilot (plan_went_stale_after_llm) | Same pattern | Same | Expected behavior | #419 |
| #397 | auto: user took over from autopilot (plan_went_stale_after_llm) | Same pattern | Same | Expected behavior | #419 |
| #396 | auto: user took over from autopilot (plan_went_stale_after_llm) | Same pattern | Same | Expected behavior | #419 |

### Cluster C3 — 401 Auth Cascade (1 issue + 10 auto bug reports)

| Issue | Title | Log Citation | Root Cause | Status | Dup? |
|-------|-------|-------------|------------|--------|------|
| #420 | Desktop bug report: it din't tell me who to block with veteran survivor | `standalone.log:657-658`: `2026-07-16 22:32:56 | ERROR | arenamcp.backends.proxy | API error: Error code: 401 - {'error': {'message': "LiteLLM Virtual Key expected. Received=****, expected to start with 'sk-'."}}` followed by `INFO | arenamcp.coach | Replaced illegal advice with legal action: Block with: Veteran Survivor` | LiteLLM proxy rejected the API key (291 401s whole-log; **283 of them on 2026-07-16**, the remaining 8 on 2026-07-23). Legal-action fallback produced vague advice. | Mitigated by `768e63c` (which introduced `_ensure_block_advice_names_attacker`; the earlier attribution to `3648114` was wrong — that commit does not add the function) + `83d9622` (401/403 detection). | July 16 bug reports (bug_20260716_*.json x10) |

The 10 auto bug reports from July 16 all share this same root cause. Each shows 401 → fallback → bare legal action.

### Cluster C4 — Autopilot Ordering (1 issue)

| Issue | Title | Log Citation | Root Cause | Status | Dup? |
|-------|-------|-------------|------------|--------|------|
| #393 | why is it playing land then attacking before casting creatures? | ⚠️ **No citation of its own.** The `gre_action_matcher` line previously quoted here is from #395's report, truncated, and describes #395's subject. | ⚠️ **Not established.** "Valid strategic choice" was asserted without evidence from this issue's own diagnostics. | ⚠️ **Do NOT close.** Closure was recommended on a borrowed citation. Needs #393's own planner diagnostics. | — |

### Cluster C5 — Card Knowledge Gap (1 issue)

| Issue | Title | Log Citation | Root Cause | Status | Dup? |
|-------|-------|-------------|------------|--------|------|
| #395 | evendo waking haven doesn't put a 1+1 counter on a creature, it needed to be stationed | Planner diagnostics show multiple attempts at `Activate Ability: Evendo, Waking Haven`. Log: `2026-07-02 09:57:17 | WARNING | arenamcp.autopilot | STALE: turn advanced 11 → 12` (plan grew stale while repeating the same erroneous action) | nemotron-3-super misread Evendo, Waking Haven's card text — needs to be animated (stationed/saddled) before it can put counters. | Not a code bug — LLM accuracy issue. Mitigated by model upgrade to deepseek-v4-flash. | Same match as #393 |

### Cluster C6 — Command-Zone Cast (1 issue)

| Issue | Title | Log Citation | Root Cause | Status | Dup? |
|-------|-------|-------------|------------|--------|------|
| #414 | why is it not playing hei bai? | Bug report planner diagnostics show 3x planner attempts to `Cast Hei Bai, Forest Guardian` (turns 4, 6, 8). Log: `2026-07-06 08:55:25 | INFO | arenamcp.autopilot | [AUTOPILOT] Plan complete (1 actions)` — but cast never succeeded | 1) Cost lookup didn't cover command zone; 2) MTGA's PayCosts provides no AutoTapActions child for command-zone casts → silent cancellation | Already fixed by `6ac6d39` | — |

### Cluster C7 — Match Review Findings (2 issues)

| Issue | Title | Log Citation | Root Cause | Status | Dup? |
|-------|-------|-------------|------------|--------|------|
| #407 | [match-review] loss match_2: 3 findings (2 high) | Finding 1: `GRE bridge submit_auto_tap did not advance Pay Costs [Bridge gap: PayCostsReq]`. Finding 2: `Bridge couldn't handle activate_ability — take this action manually. [Bridge gap: DeclareAttackers]`. Finding 3: `Card#147886 x9, Card#172258 x3` unresolved grpIds | 1) AutoTap missing on PayCostsReq (same as C6); 2) stale plan action type mismatch (same as C1); 3) local card DB stale | Findings 1-2 mitigated by `6ac6d39` and `2b97ea2`. Finding 3 needs a Scryfall DB refresh. | Dup of #406, #414 for findings 1-2 |
| #391 | [match-review] loss match_1: 1 finding (1 high) | ⚠️ **The issue body DOES contain a quoted deterministic finding** — it was not read. The claim "no separate log available" is wrong. | ⚠️ **Not established.** The finding in #391's body concerns a different gap from #407's, so "likely same bridge gap patterns" does not hold. | ⚠️ **Do NOT close as mitigated.** Read the finding quoted in the issue body first. | Not a dup of #407 on current evidence |

### Cluster C8 — X Chooser Invisible (1 issue)

| Issue | Title | Log Citation | Root Cause | Status | Dup? |
|-------|-------|-------------|------------|--------|------|
| #390 | Bridge gap: casting-time X chooser (Select a value for X) invisible to FindPendingInteraction | Issue body: "The CastingTimeOption numeric-input window is not returned by the plugin's FindPendingInteraction." | C# plugin gap — CastingTimeOption workflow lives under a different MTGA object than what `FindPendingInteraction` traverses | Mitigated by `efaf527` (X-cost casts dropped from autopilot; advice-only). Full fix needs plugin C# work. | — |

### Cluster C9 — WP-3 Parser Bug (1 issue)

| Issue | Title | Log Citation | Root Cause | Status | Dup? |
|-------|-------|-------------|------------|--------|------|
| #430 | WP-3 parser: battlefield_self/battlefield_opp are swapped (poisons the distillation corpus) | No log line (not a runtime issue — a data pipeline bug). Issue body provides evidence over 4,000 parsed rows: "744 rows where UWTempo's board holds Mountain/Forest/Swamp but was assigned as battlefield_self" | `parse_magezero_log.py` assigns `-> Permanents:` lines to wrong players. First Permanents line after Hand is SELF, not OPP. | Fixable at `tools/training/parse_magezero_log.py` — swap the assignment order for the two Permanents lines. | — |

### Additional Bug Reports (Not Filed as Issues)

The remaining 10 bug reports from July 21-28 are still open bug_report JSONs on disk but not filed as GitHub issues:

| Report | Timestamp | Context |
|--------|-----------|---------|
| bug_20260721_234215.json | Jul 21 23:42 | v2.7.3, online, deepseek-v4-flash, advice quick. Launcher Debug Report (no user feedback). |
| bug_20260721_234216.json | Jul 21 23:42 | Same timestamp — paired report |
| bug_20260722_000329.json | Jul 22 00:03 | Similar to above |
| bug_20260722_001349.json | Jul 22 00:13 | Similar to above |
| bug_20260722_001429.json | Jul 22 00:14 | Similar to above |
| bug_20260722_102308.json | Jul 22 10:23 | Similar to above |
| bug_20260722_102539.json | Jul 22 10:25 | Similar to above |
| bug_20260722_104329.json | Jul 22 10:43 | Similar to above |
| bug_20260722_104519.json | Jul 22 10:45 | Similar to above |
| bug_20260723_224336.json | Jul 23 22:43 | Similar to above |
| bug_20260723_224916.json | Jul 23 22:49 | Similar to above |
| bug_20260723_231608.json | Jul 23 23:16 | Similar to above |
| bug_20260723_231609.json | Jul 23 23:16 | Paired |
| bug_20260723_231954.json | Jul 23 23:19 | Similar |
| bug_20260723_234026.json | Jul 23 23:40 | Similar |
| bug_20260723_234027.json | Jul 23 23:40 | Paired |
| bug_20260723_234937.json | Jul 23 23:49 | Similar |
| bug_20260728_215342.json | Jul 28 21:53 | v2.7.3, fallback_mode=false. API working ([PROXY] lines in log) |
| bug_20260728_215343.json | Jul 28 21:53 | Paired |
| bug_20260728_221518.json | Jul 28 22:15 | Similar |
| bug_20260728_221702.json | Jul 28 22:17 | Similar |
| bug_20260728_221703.json | Jul 28 22:17 | Paired |
| bug_20260728_223243.json | Jul 28 22:32 | Similar |

**Log evidence for these**: standalone.log tail (Jul 28) shows [PROXY] API calls succeeding but still with `Replaced illegal advice` and `[LOCAL FALLBACK]` prefixes — the `coach_postprocess` module is overriding some LLM responses with deterministic fallback. This is a separate concern: the model's output format doesn't match the expected schema, so post-processing replaced it.

---

## Recommendation Summary

| Cluster | Recommendation |
|---------|---------------|
| C1 bridge_submit_failed | Close all as fixed-by-`2b97ea2` + solver routing |
| C2 plan_went_stale | Close as expected behavior (handled gracefully). Track as performance concern if planner speed improves. |
| C3 401 cascade | Close #420 as fixed-by-`3648114` + `83d9622`. API key issue, not code bug. |
| C4 autopilot ordering | Close #393 — not a code bug. |
| C5 card knowledge | Close #395 — not a code bug. |
| C6 command-zone cast | Close #414 as fixed-by-`6ac6d39`. |
| C7 match reviews | Close #407, #391. Finding 3 (unresolved grpIds) is a separate task — refresh card DB from Scryfall. |
| C8 X chooser | Keep open. Mitigated by `efaf527`. Needs C# plugin work to expose CastingTimeOption. |
| C9 parser bug | **DO NOT CLOSE** — needs code fix in `tools/training/parse_magezero_log.py`. Swap the assignment of the two `-> Permanents:` lines. |

### Issues requiring code changes:
1. **#430** — Fix `parse_magezero_log.py`: swap battlefield_self/battlefield_opp assignment (first Permanents line after Hand = SELF)
2. **#390** — C# plugin work to expose CastingTimeOption workflow through FindPendingInteraction
3. **Card DB refresh** (found in #407) — Run Scryfall adapter refresh to resolve grpIds 147886, 172258
