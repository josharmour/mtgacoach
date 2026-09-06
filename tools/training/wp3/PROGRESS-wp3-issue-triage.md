# WP-3: Issue Triage — PROGRESS

Branch: wp3-issue-triage
Date: 2026-07-29

## What was done
1. Clustered all 30 open GitHub issues into 8 groups (C1-C8)
2. Produced `docs/wp3-issue-triage.md` with per-issue root-cause table and log citations
3. Found that #420 (vague block advice) and #414 (not playing Hei Bai) are **already fixed** in master
4. Found that the plan_went_stale cluster (14 issues) is expected staleness-detection behavior, not a code defect
5. Implemented two code fixes:

### Fix 1: activate_ability staleness bridge detection (commit 2b97ea2)
- `src/arenamcp/autopilot.py` `_is_planner_action_stale_vs_bridge` — add ActionType.ACTIVATE_ABILITY to Shape 2 (non-ActionsAvailable bridge request = stale)
- 5 regression tests in `test_autopilot_stale_vs_bridge.py`

### Fix 2: click_button done→DeclareAttackers solver (commit 2b97ea2)
- `src/arenamcp/autopilot_bridge.py` `_try_gre_bridge` — when CLICK_BUTTON done hits DeclareAttacker, use combat solver for attacker names instead of unconditionally submitting empty attackers
- 6 regression tests in `test_autopilot_bridge_attackers.py`

## Fix rationale
Chose the bridge_submit_failed cluster because:
1. Issues #398-#402 had 5 duplicates from a single match — clear evidence of a pattern worth fixing
2. Issues #392, #394, #406 also showed stale activate_ability → bridge_submit_failed
3. Self-contained code changes with clean regression tests
4. Both fixes convert hard `bridge_submit_failed` pauses into clean stale-skip → re-plan cycles

## Test output (36 related tests + all pass)
```
tests/test_autopilot_bridge_attackers.py .............. PASS  (6/6)
tests/test_autopilot_stale_vs_bridge.py .............. PASS  (25/25)
tests/test_autopilot_solver_attacks.py .............. PASS  (5/5)
```

## Comments posted
- 1 comment per issue (30 total) with cluster, root cause, log citation, and close recommendation
- Issues NOT closed automatically — all recommendations are for the repo owner

## Known gaps
- #390 (X chooser invisible) needs plugin C# work — mitigation in place
- match review issues (#391, #407) document bridge gaps for individual attention
- Duplicate declarations: #396/#397, #398-#402 are all duplicates within matches

## Files changed
- `docs/wp3-issue-triage.md` — full triage document
- `tools/training/wp3/PROGRESS-wp3-issue-triage.md` — this file
