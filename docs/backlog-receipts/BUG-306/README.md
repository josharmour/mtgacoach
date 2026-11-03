# BUG-306 — optional payment vs. a same-turn mana reservation

Verdict: verification-only for production code; the merged guard already holds
on both submission routes. This item adds the missing submission-leg regression.
Merged to local master as MERGE COMMIT (receipt commit SOURCE COMMIT).

## Symptom
Queue row: "An optional trigger spends mana that the current plan explicitly
reserves." Triage row `docs/backlog-triage-2026-11-01.md:465`:
"#306 | PARTIAL | Gameplay / optional payment ignores mana reservation".
Stage doc `docs/BACKLOG_STAGE_2026-11-01.md:106` groups #303/#254/#306 as
land-entry/payment claims, counted mana and Dalkovan source-tap.

## Existing work reused (no new production change)
- `c5d62fa` / `ecc168d` "Refresh current mana holds after confirmed resource
  spending": `src/arenamcp/resource_hold.py` (`current_mana_hold`) plus the
  `_with_progress` hook in `src/arenamcp/game_plan.py`. Pinned by
  `tests/test_post_payment_commitment_306.py`.
- `src/arenamcp/optional_reservation.py` (`reserved_optional_mana`) feeding
  `AutopilotEngine._should_decline_optional_cost`. Pinned by
  `tests/test_optional_reservation_306.py`.
- Both PayCosts submission routes call that same guard:
  `autopilot_bridge.py:2774-2792` (`_try_interactive_safe_default`) and
  `autopilot.py:3546-3575` (`process_trigger`).

## Remaining gap reproduced on master db82bf2
The producer/planner half and the post-spend refresh were pinned, but nothing
asserted the last hop: that a reserved PayCosts is answered with MTGA's cancel
and never with an AutoTap solution, on either route. A regression there would
have spent the reserved mana silently while every existing test still passed.

## Change (tests only)
New `tests/test_optional_reservation_submission_306.py` drives the real
submission handlers with a recording bridge:

| Case | Route | Expected |
| --- | --- | --- |
| sole reserved mana ({1} optional vs {W} Rescue) | safe-default net | cancel once, no auto-tap, no pay/decline model call |
| spare mana (second untapped Plains) | safe-default net | auto-tap once, no cancel |
| unrelated payment (memory says nothing worth holding) | safe-default net | auto-tap once, no cancel |
| decline required but `can_cancel` false | safe-default net | returns False, never pays |
| sole reserved mana | main dispatch (`process_trigger`) | cancel once, no auto-tap |
| spare mana | main dispatch (`process_trigger`) | auto-tap once, no cancel |

## Verification
See `focused-tests.txt`: 6 passed for the new file; 62 passed across the seven
reservation/payment files; ruff check and format clean.

## Remaining limits
- The guard only fires for a recorded pass decision that states a same-turn
  hold; a fresh tactical board or another explicit priority decision supersedes
  it by design.
- Numeric shortfall proof only: colored-mana feasibility beyond the exact total
  is not solved here.
- Historical raw packet is not bundled; these are equivalent board/memory
  regressions, not a replay reconstruction.
- No live qualification and no Linux CI receipt for this item.

BLOCKER: independent review and a Linux CI run are still required; nothing here
is verified by the author.
