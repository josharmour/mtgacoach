# CI-PERF continuation receipt — 2026-11-02

Only one queue item covers both failures. This is a durable summary, not a new test run.

- Baseline/local and GitHub master last independently checked: `c31000589d5a0f38680855ac07c3f16580635694`.
- Failed run: https://github.com/josharmour/mtgacoach/actions/runs/41402478076
- `tests/test_line_search.py::test_the_slowest_real_boards_stay_within_the_latency_target[SLOW_212608]`:491.433ms against480ms.
- Same test `[SLOW_202111]`:493.853ms against480ms.
- Run totals:8,936passed,22skipped,2failed. Release commit `e655ef9` had a passing run; later documentation-only commits failed the same timing checks. Cause is not established by this correlation.
- Existing performance optimizations are already in master. Reuse them; preserve480ms bounds, search budgets, assertions, full outcomes and mutation/caching correctness.
- The original DeepSeek desktop session `session-bea7a22d-5761-41c1-a93b-a2c2988fd0a5` ended during profiling; no accepted final performance repair exists.
- Supported headless resume was attempted, but the runner rejected that desktop session's `standard` agent preset. Original session preserved.
- A new headless task `session-ad6cc501-1520-4a4d-b6c6-91c64c5af4b4` used existing `current` at `https://api.mtgacoach.com/v1`, Max reasoning and saved authentication. No secret is recorded here.
- It stopped before implementation after sandbox write denial creating an isolated worktree and an unavailable headless escalation channel. The coordinator explicitly terminated its process; it is NOT running and produced no accepted candidate or tests.
- Do not assume a new invocation inherits write access to a worktree outside its workspace. Use a supported desktop/CLI workspace with the needed ordinary repository permissions. Never bypass denied access or alter security settings.

## Resume without this chat

Read `autopilot-active-work.md` and `autopilot-active-work.json`. Start one task in
a permitted isolated workspace, using the existing approved model/provider setup.
The queue contains the reproduction, exact failures, acceptance criteria and
merge/review contract. Original session history is optional context; it is not
required to reproduce these two existing tests. Do not start duplicate sessions.
Run targeted profiling/checks, retain small sanitized receipts under
`docs/backlog-receipts/CI-PERF/`, and record actual source/master commits and results.
No live checkout changes while Coach is running; no new full-suite runs per item.
The independent reviewer may be any model or tool chosen later and is not a service
that must stay running. No credentials or raw private match captures belong in receipts.
