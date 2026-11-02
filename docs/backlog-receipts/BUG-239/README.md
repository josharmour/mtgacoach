# BUG-239 — one OptionalAction answered three times in six seconds

Verdict: **fixed on local master** (`codex/watchdog-239-duplicate-answers`).
The decision watchdog now recognises Arena re-offering the question it just
answered and escalates to its alert instead of submitting the same safe default
again. Production change is confined to `src/arenamcp/decision_watchdog.py`.

## Retained trace

Match `18975f2b-2685-43e3-9026-40200c6be761`, 2026-10-24 10:42:20–10:43:20,
`~/.arenamcp/standalone.log` lines 716400–716560 (the same window is in
`~/.arenamcp/bug_reports/bug_20261024_111414.json` and the match bundle):

```
10:42:20.356 Bridge detected decision: Optional Action (type=OptionalAction, actions=0)
10:42:31.712 Autopilot manual required: OptionalAction no safe automatic choice — pick manually [Bridge gap OptionalAction]
10:43:12.341 [WATCHDOG] Optional Action pending 52.0s with no answer, 20s left on Arena's clock — submitting a safe default
10:43:12.383 GRE bridge auto_respond: OptionalActionMessageRequest
10:43:12.383 [gre-aware] OptionalAction: safe-default submission (auto_respond)
10:43:12.472 Bridge detected decision cleared
10:43:12.533 Bridge detected decision: Optional Action (type=OptionalAction, actions=0)
10:43:17.043 [WATCHDOG] Optional Action pending 4.5s with no answer, 15s left on Arena's clock — submitting a safe default
10:43:18.368 [WATCHDOG] Optional Action pending 1.1s with no answer, 14s left on Arena's clock — submitting a safe default
10:43:20.253 [gre-aware] typed-decision OptionalAction Accept the optional effect Plan moving The Notary Hobbits from exile to the command zone
10:43:20.371 Bridge detected decision cleared
```

Three AutoResponds landed inside six seconds; none of them resolved the prompt.
The model's own typed answer at 10:43:20 is what cleared it.

## Mechanism on current master

Two axes, both reproduced deterministically:

1. **Identity churn.** `request_key` includes `msg_id` and `game_state_id`, and
   Arena re-offers an unanswered prompt as a new GRE message with fresh ids.
   Every re-issue therefore looked like a brand-new request, `step()` reset its
   escalation state, and the urgent-clock branch (`clock_remaining_s <=
   CLOCK_MARGIN_S`) fired immediately — three times in six seconds. The same
   reset is why the log reports "pending 4.5s" for a prompt that had been up for
   57 s (`last_answer_at` restarts on our own submission).
2. **No typed OptionalAction handler.** `_try_interactive_safe_default` has no
   OptionalAction branch; it falls through to `bridge.auto_respond()`, which
   Arena accepts but does not resolve when the answer is wrong or unavailable,
   so it re-raises the prompt.

This item fixes axis 1 — the duplicate submissions and the false "new request"
escalation. Axis 2 is unchanged by design: the watchdog still cannot decide an
optional effect for the player, so it now says so instead of answering again,
and the model's typed answer remains the resolution path.

## Acceptance map

| Acceptance clause | Where it lives | Pinned by |
|---|---|---|
| Locate this ID's retained trace, reproduce its current-path mechanism | trace above; reproduction drives `DecisionWatchdog.step()` with a fake monotonic clock through the exact id churn | `test_the_reissued_optional_action_is_not_answered_three_times`, `test_the_coaching_loop_submits_one_answer_and_then_alerts` |
| Record queue / first-token / generation / cancellation timing and final submission | BUG-231's decision trace (`litrace.py`) records every request of a decision; this item adds the watchdog's own submission count to that picture | `test_the_coaching_loop_submits_one_answer_and_then_alerts` asserts exactly one "submitting a safe default" line and one AutoRespond |
| Preserve deadline freshness and the legal fallback | `SAME_PROMPT_GUARD_S = 10.0` only suppresses a *repeat* of a question we already answered inside that window; after it expires a stuck prompt gets a bounded retry, and any different question still gets its safe default immediately | `test_the_guard_expires_so_a_stuck_prompt_still_gets_a_bounded_retry`, `test_a_different_question_still_gets_its_safe_default`, `test_a_second_optional_action_from_another_card_is_still_answered`, `test_a_new_request_is_still_replanned_and_defaulted` |
| If an earlier queue item already fixes it, verify that regression and reference that master commit | checked before patching: no commit references #239, and `decision_watchdog.py` history is only `342cf1c` (introduced the watchdog), `70d9e2f`, `4c36bd3` — none closes this recurrence | this receipt |

## What changed

`src/arenamcp/decision_watchdog.py` only:

* `prompt_key(resp)` / `_question(resp)` — what Arena is asking, ignoring its
  per-message ids (`request_type`, `request_class`, offered-choice signature,
  prompt id + parameters, source instance). Combat windows keep their existing
  turn-window identity.
* `DecisionWatchdog.SAME_PROMPT_GUARD_S = 10.0` — Arena re-offering the same
  question inside this window means our answer did not take.
* The watchdog remembers which question its last safe default answered
  (`_answered_prompt`, `_answered_at`, `_answer_failed`) across both the request
  clearing for a poll and the id churn of a re-issue; a repeat resumes
  verification instead of submitting again, so it escalates to the alert after
  `VERIFY_AFTER_S`.
* `note_safe_default()` records success/failure for that question;
  `retry_safe_default()` clears it (nothing was submitted).

No change to alert text, voice line, escalation thresholds or any other module.

## Commands and results

```
$ .venv_mac_311/bin/python -m pytest tests/test_watchdog__239__duplicate__answers.py -q
10 passed in 6.55s

$ .venv_mac_311/bin/python -m pytest tests/test_decision_watchdog.py \
    tests/test_watchdog__239__duplicate__answers.py tests/test_deadline_recovery_327.py \
    tests/test_execution_recovery_backlog.py tests/test_priority_after_cast_206.py \
    tests/test_standalone_coaching_loop.py -q
82 passed in 10.42s

$ .venv_mac_311/bin/python -m pytest tests/test_standalone_auto_queue.py \
    tests/test_standalone_autopilot_capture.py tests/test_standalone_coaching_loop.py \
    tests/test_standalone_draft_hud.py tests/test_standalone_generic_selection.py \
    tests/test_standalone_hotkeys.py tests/test_standalone_match_completion.py \
    tests/test_standalone_speak_advice.py -q
36 passed in 10.02s

$ .venv_mac_311/bin/python -m pytest tests/test_autopilot_stale_vs_bridge.py \
    tests/test_autopilot_window_identity.py tests/test_bridge_decision_transitions.py \
    tests/test_autopilot_search_stall.py tests/test_casting_optional_decisions.py \
    tests/test_commander_return.py -q
121 passed in 11.16s

$ .venv_mac_311/bin/python -m ruff check src/arenamcp/decision_watchdog.py \
    tests/test_watchdog__239__duplicate__answers.py
All checks passed!
$ .venv_mac_311/bin/python -m ruff format --check src/arenamcp/decision_watchdog.py \
    tests/test_watchdog__239__duplicate__answers.py
2 files already formatted
```

## The new regression bites

With pre-fix behaviour restored (`SAME_PROMPT_GUARD_S = -1`, i.e. every re-issue
is a new request) four of the ten tests fail:

```
FAILED test_the_reissued_optional_action_is_not_answered_three_times
FAILED test_the_answer_record_survives_the_request_clearing
FAILED test_a_failed_answer_record_survives_the_request_clearing
FAILED test_the_coaching_loop_submits_one_answer_and_then_alerts
4 failed, 6 passed in 6.37s
```

The loop test's failure output is the live bug reproduced end to end: eight
"submitting a safe default" lines and eight AutoResponds inside twenty seconds,
one per coaching-loop iteration.

## Remaining limits

* The guard window is a heuristic. A genuinely different OptionalAction from the
  same card with an identical prompt id *and* identical parameters inside ten
  seconds would be treated as a repeat; different cards/questions are separated
  by source instance id and prompt parameters.
* No typed OptionalAction handler was added — deciding an optional effect stays
  with the model (or the player). The watchdog now reports that it cannot answer
  instead of repeating an AutoRespond.
* No live replay of match `18975f2b`; these are deterministic reproductions of
  the logged mechanism on current master, not live-service latency numbers.
* No Linux CI receipt for this commit yet.

## BLOCKER

Independent review and a real Linux CI run are still required before this item
can move past `mergedawaitingreview`. Nothing here was verified by its author.
