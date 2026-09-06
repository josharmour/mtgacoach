# C8 — X Chooser Verification (#390)

## Issue #390: "Bridge gap: casting-time X chooser invisible to FindPendingInteraction"
- Filed: 2026-07-02
- Mitigation shipped: `efaf527` — X-cost casts dropped from autopilot plans (advice-only)
- Proper fix PR: #427 (merged 2026-07-30)

## PR #427: "WP-3: bridge-x-chooser — submit_x + CastingTimeOption numeric fields"
- Merged: 2026-07-30
- 12 tests in `tests/test_bridge_x_chooser.py` (all passing)

## Capability mapping: #390 requirements vs #427 delivery

| #390 Requirement | Status | Evidence |
|---|---|---|
| Make CastingTimeOption window visible to FindPendingInteraction | ✅ Delivered | `Plugin.cs` line 833+: `CastingTimeOptionRequest` surfacing `numeric_min`, `numeric_max`, `numeric_step`, `numeric_disallowed`, `numeric_disallow_even`, `numeric_disallow_odd`, `grp_id` in `get_pending_actions` response |
| Expose numeric fields through get_pending_actions | ✅ Delivered | `Plugin.cs` lines 852-875: all numeric fields serialized in response |
| Add submit_x pipe command (C#) | ✅ Delivered | `Plugin.cs` line 433: `case "submit_x": HandleSubmitX(cmd);`; lines 1637-1690: `HandleSubmitX` walks child requests, calls `SubmitX(value)`, clamps to min/max |
| Python bridge submit_x method | ✅ Delivered | `gre_bridge.py` lines 923-946: `submit_x(value)` sends `submit_x` pipe command |
| Python bridge submit_numeric method | ✅ Delivered | `gre_bridge.py` lines 911-921: `submit_numeric(value)` for older paths |
| Tests (12 pure-Python) | ✅ Delivered | `tests/test_bridge_x_chooser.py` — 12 tests passed in 0.37s |
| `numeric_input_type` + `numeric_suggested` | ✅ Delivered | PR #431 (follow-up) |
| X-cost casts re-enabled in planner | ✅ Delivered | `action_planner.py:1299-1307` re-enables X-cost casts when bridge connected |
| `_safe_default_numeric` fallback | ✅ Delivered | `autopilot_bridge.py:1677-1704` provides min/suggested default |

## Remaining Python-side gaps (2 small, clearly-scoped)

### Gap 1: `autopilot_bridge.py:145` calls `submit_numeric()` instead of `submit_x()`

**Problem**: When `ActionType.NUMERIC_INPUT` fires, `autopilot_bridge.py:145-152` calls `self._gre_bridge.submit_numeric(value)`. The C# handler `HandleSubmitNumeric` (`Plugin.cs:1620`) only matches a **standalone** `NumericInputRequest`. When the X chooser appears as a `CastingTimeOption_NumericInputRequest` **child** of `CastingTimeOptionRequest`, `FindPendingInteraction()` returns `CastingTimeOptionRequest` (the parent), so `HandleSubmitNumeric` fails — the cast type is wrong.

The correct handler is `HandleSubmitX` (which walks child requests), reachable via `submit_x()`, but `submit_x()` is never called from the autopilot execution path.

**Evidence**: `grep -rn "submit_x" src/arenamcp/ --include="*.py"` returns only the definition in `gre_bridge.py:923-946`. No caller exists.

**Fix**: In `autopilot_bridge.py:145-152`, check if `_bridge_request_class` is `CastingTimeOptionRequest` — if so, call `submit_x()` instead of `submit_numeric()`.

### Gap 2: Numeric fields not propagated to the planner snapshot

**Problem**: `numeric_min`, `numeric_max`, `numeric_suggested`, `numeric_disallowed` exist in the raw poll response from C# but `_normalize_poll()` / `_stamp_bridge_fields()` don't extract them into the game state snapshot. The LLM prompt has no visibility into X ranges or suggested values.

**Evidence**: The C# response includes `resp["numeric_min"] = (int)numericChild.Min` etc. (`Plugin.cs:852-878`), confirmed in the raw poll dict. But `autopilot_bridge.py` `_normalize_poll` and `gre_bridge.py` `_stamp_bridge_fields` don't pass them through.

**Fix**: Add numeric field extraction in `_stamp_bridge_fields` or `_normalize_poll` so the LLM sees `numeric_min`, `numeric_max`, `numeric_suggested`, `numeric_disallowed` as snapshot fields.

### What is NOT a gap
- C# plugin: complete — `HandleSubmitX` works correctly for CastingTimeOption children
- Python bridge: complete — both `submit_x()` and `submit_numeric()` exist
- Planner: `action_planner.py:1299-1307` already re-enables X-cost casts when bridge is connected
- Fallback: `_safe_default_numeric` at `autopilot_bridge.py:1677-1704` provides min/suggested default

Both fixes are small, self-contained Python changes — no C# modifications needed.

VERDICT: RESIDUAL-WORK: bridge plumbing delivered via PR #427; two small Python gaps remain — (1) `autopilot_bridge.py:145` calls `submit_numeric()` instead of `submit_x()` for CastingTimeOption children, (2) numeric fields not propagated to planner snapshot. Both clearly scoped for follow-up.