# Next coordinated Coach update

Installed baseline: `5d5914463a3018f13afb476689b7bcb7cdb1b71b`.
All work is isolated; no app, settings, history, native bridge, or GitHub changes.

## Included changes

- **#306**: render current mana reservations against confirmed spending, retaining the saved plan for later untaps. Conservative numeric shortfall proof only; not a general colored-mana solver. Source `c5d62fa` → `ecc168d`.
- **#308**: bounded public outcome vectors for burn versus discard-for-protection and creature-entry draw versus a known draw punisher; current stats/protection and observed trigger use respected. These are conditional components, not a universal strategic ranking. Sources `a4b9898`, `6f23578` → `1249076`, `ad5a1b2`.
- **#345**: decline an optional land-entry life payment only when complete known state proves its immediate one mana has no use before untap. Ambiguous resources/permissions remain advisory. Source `473786b` → `b7e12a1`.
- **#214/#245 diagnostics**: default-off, explicitly opted-in metadata or masked syntax, respecting effective DEBUG/disabled logging; 16 KiB per file, 20 files, private file permissions. No raw response/request capture or logging setting changes. Source `bd1b5f1` → `9fd6562`; original unsafe `091bd189` is excluded, not a dependency. Diagnostic coverage does not close every malformed-output cause.
- **False summoning-sickness warnings**: invalidate cached hand assessments when haste/rules/body facts change; allow a ready same-name copy to fulfill a name-level planned attack while still excluding sick instances. Source `da286386` → `9dc17e4`.

## Verification

One combined run across 32 affected files: **792 passed, 1 failed in 24.36s**. Both previously reproduced baseline sickness failures passed. The remaining failure was the old #228 test demanding a creature-only damage spell as face damage, without supplying its optional second-look response.

Focused follow-up: **399 passed, 1 failed in 12.77s**. A first test correction still assumed the advisory second look always runs; that assumption was removed. The final test checks the actual hold-release menu and absence of forced pass, then lets the model choose a real face-damage spell. Production guard code was not changed for this test correction.

Final affected burn/hold/decision rerun: **87 passed in 1.88s**. All encountered failures are resolved. Counts overlap; do not sum them or describe the initial combined run as all green. No second full suite, hosted CI, private-data external replay, or new live-match qualification was performed. Ruff, formatting, and whitespace checks passed.

## Activation

Keep Coach running until the parent coordinates a closure. Verify its process is stopped and acquire the startup lock, then fast-forward the clean installed master to this tested batch. Arena can remain open for manual play; there are no native changes. Do not quit or launch either app automatically, and do not push.

Prepared test manifest: `/tmp/coach-next-closure-tests.txt`.
Logs: `/tmp/coach-next-closure-results.log`, `/tmp/coach-next-closure-focused-results.log`, `/tmp/coach-next-closure-burn-results.log`.
