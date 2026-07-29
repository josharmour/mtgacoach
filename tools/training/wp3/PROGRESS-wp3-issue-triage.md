# WP-3: Issue Triage — PROGRESS

Branch: wp3-issue-triage
Date: 2026-07-29

## What was done
1. Analyzed all 30 open GitHub issues across 6 clusters
2. Produced docs/wp3-issue-triage.md with per-issue root cause table
3. Found that #420 (vague block advice) and #414 (not playing Hei Bai) are **already fixed** in master
4. Found that the plan_went_stale cluster (14 issues) is expected behavior from staleness detection (not bugs)
5. Implemented fix for bridge_submit_failed cluster: added ACTIVATE_ABILITY to stale bridge detection

## Fix: activate_ability staleness bridge detection
- `src/arenamcp/autopilot.py` `_is_planner_action_stale_vs_bridge` — add ActionType.ACTIVATE_ABILITY to Shape 2 (non-ActionsAvailable bridge request = stale)
- Before: activate_ability always returned False (not stale) → fell through to bridge execution → bridge_submit_failed noise bugs
- After: when bridge has non-ActionsAvailable pending (SelectTargets, DeclareAttackers, PayCosts, etc.), activate_ability is treated as stale → system re-plans cleanly
- Tests added: 5 new tests in test_autopilot_stale_vs_bridge.py

## Test output
All 39 tests pass:
- tests/test_autopilot_stale_vs_bridge.py: 25 tests (20 existing + 5 new) — PASS
- tests/test_block_advice_specificity.py: 14 tests — PASS

## Key decisions
- Chose activate_ability staleness detection over other candidates because:
  1. Clear evidence from #392 (Mutagen) and #407 (Utter Insignificance)
  2. Self-contained code change (8 lines + comment)
  3. Converts hard bridge_submit_failed failures into soft plan_went_stale re-plans
  4. Has clean regression tests
- Did NOT try to fix the bridge's activate_ability implementation (much larger change)
- Did NOT add Shape 1 checks for activate_ability (bridge doesn't enumerate activations the same way)

## Known gaps
- The match review issues (#391, #407) document bridge gaps that need individual bridge work
- X chooser (#390) still needs bridge work — mitigation is in place
