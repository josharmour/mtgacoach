# WP-3: C1 Verify — bridge_submit_failed cluster (#392-#406)

Branch: qwen/c1-verify
Date: 2026-07-30
Fix commit: a682894 (PR #428, originally 2b97ea2 on wp3-issue-triage)

## Fix Summary

Commit a682894 (merged via PR #428) fixes **two** failure patterns in the bridge_submit_failed cluster:

1. **ACTIVATE_ABILITY stale detection** (autopilot.py): When the bridge has a non-ActionsAvailable request pending (SelectTargets, DeclareAttackers, PayCosts, etc.), `activate_ability` is now treated as stale instead of falling through to bridge execution → bridge_submit_failed noise bug.

2. **click_button(done) on DeclareAttackers** (autopilot_bridge.py): When the planner picks `click_button(done)` during a DeclareAttacker bridge request, the fix routes through the combat solver for attacker names instead of unconditionally submitting `submit_attackers([])`.

## Per-Issue Verdict Table

| Issue | Action Type | Card | Bridge Request | Fix Pattern | Covered? | Verdict |
|-------|------------|------|----------------|-------------|----------|---------|
| #406 | activate_ability | Utter Insignificance | DeclareAttackers / DeclareAttackerRequest | Stale detection | YES | RECOMMEND-CLOSE |
| #405 | pay_costs | auto_pay | PayCostsReq / PayCostsRequest | Stale detection | NO (pay_costs not in stale list) | RESIDUAL-WORK |
| #402 | click_button | done | DeclareAttackers / DeclareAttackerRequest | click_button→solver | YES | RECOMMEND-CLOSE |
| #401 | click_button | done | DeclareAttackers / DeclareAttackerRequest | click_button→solver | YES | RECOMMEND-CLOSE |
| #400 | click_button | done | DeclareAttackers / DeclareAttackerRequest | click_button→solver | YES | RECOMMEND-CLOSE |
| #399 | click_button | done | DeclareAttackers / DeclareAttackerRequest | click_button→solver | YES | RECOMMEND-CLOSE |
| #398 | click_button | done | DeclareAttackers / DeclareAttackerRequest | click_button→solver | YES | RECOMMEND-CLOSE |
| #394 | click_button | done | DeclareAttackers / DeclareAttackerRequest | click_button→solver | YES | RECOMMEND-CLOSE |
| #392 | activate_ability | Evolution Witness | DeclareAttackers / DeclareAttackerRequest | Stale detection | YES | RECOMMEND-CLOSE |

## Evidence from Issue Bodies

### #406 — activate_ability Utter Insignificance vs DeclareAttackers
- Action type: `activate_ability`, card: `Utter Insignificance`
- Bridge request: `DeclareAttackers / DeclareAttackerRequest`
- Log: "Phase changed Main1 → Combat, proceeding with caution" then "Pending request has no actions" → "Bridge couldn't handle activate_ability (Utter Insignificance)"
- Planner picked activate_ability during ActionsAvailable, but phase shifted to Combat (Declare Attackers) before execution. **Stale detection fix catches this** — ACTIVATE_ABILITY + non-ActionsAvailable request = stale.

### #405 — pay_costs auto_pay vs PayCostsReq
- Action type: `pay_costs`, card: `auto_pay`
- Bridge request: `PayCostsReq / PayCostsRequest`
- Log: "GRE bridge submit_auto_tap failed: Pending is null, no AutoTapActionsRequest available" → "GRE bridge cancel_action failed: No pending interaction"
- This is a **timing/race condition** in the pay_costs flow, not an action-type-vs-request-type mismatch. The fix only adds ACTIVATE_ABILITY to stale detection — **pay_costs is NOT covered**.

### #402 — click_button done vs DeclareAttackers
- Action type: `click_button`, card: `done`
- Bridge request: `DeclareAttackers / DeclareAttackerRequest`
- Legal attackers: `["Michelangelo, Weirdness to 11"]`
- Log: "click_button | done" → "GRE bridge pass failed: Cannot pass on current interaction" → "Blocking action for current window (failure 4/5)"
- **click_button→solver fix routes this through declare_attackers with solver-picked attackers** instead of submit_attackers([]).

### #401 — click_button done vs DeclareAttackers
- Action type: `click_button`, card: `done`
- Bridge request: `DeclareAttackers / DeclareAttackerRequest`
- Legal attackers: `["Michelangelo, Weirdness to 11"]`
- Log: "click_button | done" → "GRE bridge pass failed" → "Blocking action for current window (failure 5/5)"
- Same pattern as #402. **Covered by click_button→solver fix**.

### #400 — click_button done vs DeclareAttackers
- Action type: `click_button`, card: `done`
- Bridge request: `DeclareAttackers / DeclareAttackerRequest`
- Legal attackers: `["Michelangelo, Weirdness to 11"]`
- Log: "click_button | done" → "GRE bridge pass failed" → "Blocking action for current window (failure 3/5)"
- Same pattern. **Covered by click_button→solver fix**.

### #399 — click_button done vs DeclareAttackers
- Action type: `click_button`, card: `done`
- Bridge request: `DeclareAttackers / DeclareAttackerRequest`
- Legal attackers: `["Michelangelo, Weirdness to 11"]`
- Log: "click_button | done" → "GRE bridge pass failed" → "Blocking action for current window (failure 2/5)"
- Same pattern. **Covered by click_button→solver fix**.

### #398 — click_button done vs DeclareAttackers
- Action type: `click_button`, card: `done`
- Bridge request: `DeclareAttackers / DeclareAttackerRequest`
- Legal attackers: `["Michelangelo, Weirdness to 11"]`
- Log: "click_button | done" → "GRE bridge pass failed" → "Blocking action for current window (failure 1/5)"
- Same pattern. **Covered by click_button→solver fix**.

### #394 — click_button done vs DeclareAttackers
- Action type: `click_button`, card: `done`
- Bridge request: `DeclareAttackers / DeclareAttackerRequest`
- Legal attackers: `["Michelangelo, Weirdness to 11"]`
- Log: "click_button | done" → "GRE bridge pass failed" → "Autopilot manual required: Bridge couldn't handle click_button (done)"
- Same pattern. **Covered by click_button→solver fix**.

### #392 — activate_ability Evolution Witness vs DeclareAttackers
- Action type: `activate_ability`, card: `Evolution Witness`
- Bridge request: `DeclareAttackers / DeclareAttackerRequest`
- **Stale detection fix catches this** — ACTIVATE_ABILITY + non-ActionsAvailable request = stale, so system re-plans cleanly instead of filing bridge_submit_failed.

## Tests

- `tests/test_autopilot_stale_vs_bridge.py`: 5 new tests for ACTIVATE_ABILITY stale detection (SelectTargets, ActionsAvailable, DeclareAttackers, PayCosts, no-bridge-request)
- `tests/test_autopilot_bridge_attackers.py`: 5 new tests for click_button→solver routing (solver preferred, empty fallback, blockers unchanged, DECLARE_ATTACKERS dispatch, solver returns list)
- All 31 tests pass (20 existing stale + 5 new stale + 5 new bridge attackers + 1 solver).

## Residual

- **#405 (pay_costs auto_pay)**: Not covered by the fix. The pay_costs action type was not added to stale detection. The root cause is a race condition where the first trigger (decision_required) successfully submits auto_tap, then the second trigger (stack_spell_yours) tries to submit again but the pending interaction is already cleared. This needs separate pay_costs bridging work.