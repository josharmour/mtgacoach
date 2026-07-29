# WP-3: bridge-x-chooser — PROGRESS

## Scope
Fix GitHub issue #390: autopilot blind to the X-cost chooser (CastingTimeOption_NumericInputRequest invisible to FindPendingInteraction).

## What was done

### C# Plugin (bepinex-plugin/MtgaCoachBridge/Plugin.cs) — uncommitted changes
1. Added `submit_x` case in ProcessCommand switch (line 433)
2. Added `HandleSubmitX` method (line 1597):
   - Finds CastingTimeOption_NumericInputRequest child in pending CastingTimeOptionRequest
   - Calls `SubmitX(value)` on the child
   - Falls back to NumericInputRequest for standalone numeric inputs
3. Surfaces CastingTimeOption_NumericInputRequest fields in HandleGetPendingActions (line 845):
   - `numeric_min`, `numeric_max`, `numeric_step`
   - `numeric_disallowed` (array)
   - `numeric_disallow_even`, `numeric_disallow_odd` (bool flags)
   - `grp_id` from the child request

### Python (src/arenamcp/gre_bridge.py)
1. Added `GREBridge.submit_x(value: int) -> bool` method:
   - Sends `{"action": "submit_x", "value": <int>}`
   - Fail-closed: returns False on plugin error or communication failure
   - Logs the submitted value and response type

### Tests (tests/test_bridge_x_chooser.py)
12 pure-Python tests covering:
- submit_x sends correct JSON command shape
- submit_x returns True on success
- submit_x returns False on plugin error (ok=false)
- submit_x raises GREBridgeError on disconnect
- submit_x handles X=0 (valid for e.g. Silkguard)
- submit_x handles large X values (e.g. X=20)
- get_pending_actions passes through numeric fields (min/max/step/disallowed/grp_id)
- get_pending_actions works with older plugin builds (fields absent)
- get_pending_actions includes disallow_even flag
- get_pending_actions includes disallow_odd flag
- submit_x via _send_safe round-trips correctly
- CastingTimeOptionRequest response always has actions list

### Build status
- `dotnet build -c Release` fails on this Linux machine (missing MTGA assemblies like Assembly-CSharp.dll, Core.dll — expected)
- Python tests: `uv run pytest tests/test_bridge_x_chooser.py -v` — 12/12 passed

## C# review-readiness
The C# code follows existing patterns:
- `HandleSubmitX` mirrors `HandleSubmitNumeric`'s structure
- Numeric-field surfacing mirrors the standalone `NumericInputRequest` handler (lines 1049-1065)
- Uses the same `FindPendingInteraction` → child-iteration pattern used elsewhere (e.g. CastingTimeOption_ManaTypeRequest child walk at line 2480)
- Backward compatible: older plugins without submit_x just get `ok=false` error; the CastingTimeOptionRequest handler still works with the entry-index protocol

## Known gaps
- C# could not be compiled on this Linux dev machine; structural review needed
- X casts are NOT re-enabled in the planner — that's a separate owner decision (per task spec)
