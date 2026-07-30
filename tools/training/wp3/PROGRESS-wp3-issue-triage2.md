# WP-3 Issue Triage (redo) — Progress

Branch: `wp3-issue-triage2`
Started: 2026-07-30

## Steps

- [x] Read all 31 bug reports under `/Users/joshu/.arenamcp/bug_reports/*.json`
- [x] Grepped `standalone.log` (17,803 lines) for ERROR, WARNING, replaced_advice, fallback patterns
- [x] Ran `gh issue list --state open --limit 40` for full issue inventory
- [x] Read full bodies of all 31 open issues
- [x] Identified 9 clusters
- [x] Written `tools/training/wp3/issue-triage.md` with per-issue table, log citations, and recommendations
- [ ] Commit, push, create PR

## Clusters Identified

| Cluster | Tag | Count |
|---------|-----|-------|
| C1 | bridge_submit_failed | 9 issues (#406, #405, #402, #401, #400, #399, #398, #394, #392) |
| C2 | plan_went_stale | 14 issues (#419, #418, #417, #415, #413, #412, #411, #410, #409, #408, #404, #403, #397, #396) |
| C3 | 401 auth cascade | 1 issue (#420) + 10 auto-reports from July 16 |
| C4 | Autopilot ordering | 1 issue (#393) |
| C5 | Card knowledge gap | 1 issue (#395) |
| C6 | Command-zone cast blocked | 1 issue (#414) |
| C7 | Match review findings | 2 issues (#407, #391) |
| C8 | X chooser invisible | 1 issue (#390) |
| C9 | WP-3 parser bug | 1 issue (#430) |

## Notable Difference from PR #428

PR #428 claimed to have produced `docs/wp3-issue-triage.md` which was never committed (docs/ is gitignored). This deliverable is at `tools/training/wp3/issue-triage.md` — a tracked path.

Also: PR #428 used "log citation" loosely. This redo quotes actual log lines for every issue.
