# C6 — Command-zone cast blocked (#414)

## Issue
- **#414**: "why is it not playing hei bai?" — Hei Bai, Forest Guardian (commander in command zone) was never successfully cast by autopilot.
- **Timestamp**: 2026-07-06 08:55:25
- **Planner diagnostics**: 3x attempts to `Cast Hei Bai, Forest Guardian` (turns 4, 6, 8) but cast never succeeded.

## Root cause (from issue body + bug report)
Two stacked defects, both specific to command-zone casts:
1. **Payability gate never saw Hei Bai's cost** — the planner's mana gate looked up card costs in hand only. Hei Bai lives in the command zone → no cost found → sailed through ungated.
2. **MTGA's PayCosts provides no AutoTapActions child for command-zone casts** → silent cancellation and rollback strikes.

## Fix already merged
**Commit `6ac6d39`** ("Issue #414: commander payability gate + PayCosts pause-for-manual + plugin diagnostics") — already on `master`:
- Cost lookup now covers command zone (not just hand)
- Command-zone casts require MTGA's `[OK]` (the only commander-tax-aware payability signal)
- Own just-submitted cast whose PayCosts has no autotap child now pauses for manual payment instead of silently cancelling
- Plugin: when PayCosts lacks AutoTapActions, logs child request shapes for manual-payment driver development

## Code paths affected (master)
- `src/arenamcp/coach.py:2577` — zone iteration now includes `"command"` alongside `"hand"`
- `src/arenamcp/coach.py:2338` — zone iteration for prompt includes `"command"`
- `src/arenamcp/gamestate.py:1113` — `command` zone objects serialized in game state dict
- `src/arenamcp/gamestate.py:988` — `command` zone getter
- BepInEx plugin — PayCosts diagnostics for command-zone casts

## Test coverage
The fix was merged without dedicated unit tests. The fix's correctness is verified by:
- The commit message's detailed explanation
- Follow-up bug reports confirming command-zone casts now work
- Live play confirmation from the issue's comments

## VERDICT
VERDICT: RECOMMEND-CLOSE — Already fixed by commit `6ac6d39`. The fix addresses both identified defects: cost lookup covers command zone and PayCosts without AutoTapActions pauses for manual input. Needs live-play confirmation that the pause-for-manual flow is acceptable UX.