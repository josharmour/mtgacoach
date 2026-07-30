# WP-3: bridge-x-chooser — PROGRESS

## Scope
Fix GitHub issue #390: The CastingTimeOption numeric-input window
("Select a value for X") was invisible to FindPendingInteraction.
Autopilot could neither choose X nor cancel.

Mitigation already shipped (efaf527): X-cost casts are dropped from
autopilot plans. This PR adds the PROPER fix so the bridge can drive
the X chooser.

## What was done

### C# (Plugin.cs) — 3 changes
1. **`HandleGetPendingActions` CastingTimeOptionRequest branch**: surface
   `numeric_min`, `numeric_max`, `numeric_step`, `numeric_input_type`,
   `numeric_disallowed`, `numeric_disallow_even`, `numeric_disallow_odd`,
   and `grp_id` from the `CastingTimeOption_NumericInputRequest` child.
   Also sets `can_pass` from `castingReq.CanCancel`.

2. **`case "submit_x"`** added to the ProcessCommand switch, routing to
   new `HandleSubmitX` method.

3. **`HandleSubmitX`** pipe command: finds the
   `CastingTimeOption_NumericInputRequest` child of the pending
   `CastingTimeOptionRequest`, calls `SubmitX(value)` on it.
   Also accepts standalone `NumericInputRequest` as a fallback.

### C# Build
`dotnet build -c Release` fails on this dev machine because the .csproj
requires MTGA's game assemblies (BepInEx, Assembly-CSharp, UnityEngine)
which live in the MTGA install directory. The C# code follows existing
patterns in the file and is review-ready.

### Python (gre_bridge.py) — already in commit 31c0082
- `GREBridge.submit_x(value)` method was already committed.
- Uses `_send_safe({"action": "submit_x", "value": value})`.
- Returns True on ok, False on error or comm failure.
- Fail-closed: older plugins that don't have `submit_x` will return
  `{"ok": false, "error": "..."}` and the bridge returns False.

### Tests (test_bridge_x_chooser.py)
17 tests covering:
- `submit_x` command encoding and error handling (5 tests)
- `_safe_default_numeric` safe-value selection (7 tests)
- `GameAction.numeric_value` field (1 test)
- `get_pending_actions` pipe response shape (4 tests)
- `BridgeDecisionPoller.enrich_snapshot` casting_time_options (1 test)

## Test output (all 17 passed)

```
tests/test_bridge_x_chooser.py::test_submit_x_sends_correct_command PASSED
tests/test_bridge_x_chooser.py::test_submit_x_returns_true_on_ok PASSED
tests/test_bridge_x_chooser.py::test_submit_x_returns_false_on_error PASSED
tests/test_bridge_x_chooser.py::test_submit_x_returns_false_on_comm_error PASSED
tests/test_bridge_x_chooser.py::test_submit_x_zero_is_valid PASSED
tests/test_bridge_x_chooser.py::test_default_numeric_empty_pending PASSED
tests/test_bridge_x_chooser.py::test_default_numeric_uses_suggested PASSED
tests/test_bridge_x_chooser.py::test_default_numeric_skips_disallowed_suggested PASSED
tests/test_bridge_x_chooser.py::test_default_numeric_falls_back_to_min PASSED
tests/test_bridge_x_chooser.py::test_default_numeric_skips_disallowed_min PASSED
tests/test_bridge_x_chooser.py::test_default_numeric_non_int_fields PASSED
tests/test_bridge_x_chooser.py::test_action_carries_numeric_value PASSED
tests/test_bridge_x_chooser.py::test_get_pending_actions_passthrough_fields PASSED
tests/test_bridge_x_chooser.py::test_get_pending_actions_disallowed_field PASSED
tests/test_bridge_x_chooser.py::test_get_pending_actions_disallow_even PASSED
tests/test_bridge_x_chooser.py::test_get_pending_actions_grp_id PASSED
tests/test_bridge_x_chooser.py::test_bridge_enrich_snapshot_casting_options PASSED
============================== 17 passed in 0.59s ==============================
```

## Known gaps
- C# cannot be locally compiled (needs MTGA assemblies).
- X-cost casts remain dropped from autopilot plans (separate owner decision).
- `FindPendingInteraction` may still miss `CastingTimeOptionRequest` for
  some workflow paths; the fix here assumes FPI finds it (it does for the
  standard DuelScene workflow path). If FPI misses some edge-case paths,
  a Harmony prefix patch on the workflow creation would be needed.
