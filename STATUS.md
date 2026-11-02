# MTGA Coach — integration status

Last updated: 2026-11-01T22:12:00-08:00 (America/Los_Angeles)

This is the durable operational checkpoint for the current maintenance window.
It records verified facts only; do not treat planned work as complete.

## Source and CI

- Repository: `/Volumes/repos/mtgacoach`, branch `master`.
- Local and `origin/master` HEAD: `0a5c1f6` (`Lift the hard cap in the
  burn-heavy latency test`).
- Recent commits:
  - `0a5c1f6` Lift the hard cap in the burn-heavy latency test
  - `49765ac` Keep a leaked plan refresh from failing the next test
  - `613e146` Speak coaching advice in every speech mode
  - `36a21d2` Format trial expiration changes (ruff format only)
  - `7ab802a` Email verified trial users once when their trial expires
  - `1db7896` Show expired trial and license-key state in the desktop app
  - `50cd983` Document the 3.8.0 installer runtime and bridge packaging
  - `7bb9418` Build relocatable Windows runtime for installer
- CI run for `36a21d2`: https://github.com/josharmour/mtgacoach/actions/runs/41368676929
  - `ruff`: success
  - `Hosted trials and Patreon keys`: success
  - `pytest`: success (after rerunning only the load-sensitive failed jobs)
- CI run for `613e146`: https://github.com/josharmour/mtgacoach/actions/runs/41391376924
  - `ruff`: success; `Hosted trials and Patreon keys`: success
  - `pytest`: **failure** — one test,
    `test_action_finishes_while_high_effort_strategy_is_inflight`, saw a
    background plan refresh from an earlier test still holding the process-wide
    strategic lane. Fixed in `49765ac` (conftest waits for the lane around every
    test; the test joins its own plan thread).
- CI run for `49765ac`: https://github.com/josharmour/mtgacoach/actions/runs/41396351219
  - `ruff`: success; `Hosted trials and Patreon keys`: success
  - `pytest`: **failure** — one test,
    `test_a_burn_heavy_hand_stays_within_the_latency_target`, asserted no
    truncated result. The search's only clock is the 350 ms hard cap, and a
    loaded runner crossed it. Fixed in `0a5c1f6` by passing `hard_ms=1e9`, as
    every other search in that file already does; the latency assertion is
    unchanged.
- CI run for `0a5c1f6`: https://github.com/josharmour/mtgacoach/actions/runs/41397271654
  - `ruff`: success; `Hosted trials and Patreon keys`: success; `pytest`: success
    (4m41s). Master is green at `0a5c1f6`.
- Installer validation run on `36a21d2` (no release attachment):
  https://github.com/josharmour/mtgacoach/actions/runs/41370990361
  - Dispatched with no `release_tag`, so it builds and verifies the installer
    from current master without mutating the published v3.8.0 assets.
  - Result: **success**.
- Local focused verification after formatting:
  - `ruff check src tests`: pass
  - `ruff format --check src tests`: pass (991 files)
  - `pytest tests/test_trial_endpoint.py tests/test_compact_coach.py -q`: 87 passed
- Local full suite on `49765ac` (with the new lane guard): 8957 passed,
  1 skipped, 2 failed — both in `tests/test_update_lock_coordination.py`, both
  a 15 s subprocess timeout importing PySide6 while the suite loaded the SMB
  share. They pass alone (41 s) and pass in CI; no timeout was raised.

## Release v3.8.0

- GitHub release published (not draft/prerelease):
  https://github.com/josharmour/mtgacoach/releases/tag/v3.8.0
- Tag `v3.8.0` points at `50cd983`.
- Assets verified live:
  - `arenamcp-3.8.0-py3-none-any.whl`, 3,716,522 bytes,
    sha256 `ae67819f9bd964751eda0628a3c62c96720fa99c0a0e1d013a3a52f0f8c9a0c4`
  - `mtgacoach-Setup.exe`, 293,370,836 bytes,
    sha256 `c3aef664e8be641e3b8fafe1fff5730fdf253ec99556b6dd4b1edafe9a5ec9b3`
- Download URLs return HTTP 302 then HTTP 200 with matching content length.
- **The published v3.8.0 assets do not include the post-release trial commits**
  (`1db7896`, `7ab802a`, `36a21d2`). Do not mutate the published tag/assets;
  if those changes must ship to users, cut a new version/release after Windows
  verification.

## Website deployment (trial-expiration email)

Deployed to production blackwell (`10.0.0.100`) on 2026-11-02T01:04:08Z.

- Pre-deploy safety:
  - sqlite online backup:
    `/app/data/mtgacoach.db.bak-pre-trial-email-20261101T1705Z`
    (2,449,408 bytes)
  - previous image tagged
    `trash-guides-mtgacoach:pre-trial-email-20261101`
    (`sha256:5a42bc63aeaa4cbccdc243983b3b02cde5b568182f6fb6673b37f9567c6588ab`)
- Files installed into build context
  `/home/joshu/docker-stack/appdata/mtgacoach/` (hashes match staging):
  - `app.py` sha256 `9b6337a210fd...`
  - `db.py` sha256 `b73c124a178e...`
  - `patreon.py` sha256 `87624707ebca...`
  - `routers/billing.py` sha256 `9e3f65c0076c...`
  - `routers/proxy.py` sha256 `de9b7a25915f...`
- Rebuilt image manifest list:
  `sha256:461ce74bfab8e9552021b4b4cdb50ca007911b553928e5acce568d65df32d1fd`
- Recreated only the `mtgacoach` service; container started
  2026-11-02T01:04:08Z, port 8443.
- Post-deploy verification:
  - `/health` returns HTTP 200 with provider `home-glm53-flash` available.
  - Public site `/`, `/subscribe`, `/health` all return HTTP 200.
  - `trials` table now has `expired_email_sent_at`.
  - Eligible expired verified-email trials: **0**; claimed/sent: **0**.
    Manual sweep returned `sweep_sent 0`.
  - Production-only model alias preserved:
    `PATRON_KEY_MODELS = ["current-standard"]`.
  - SMTP configured; no sweep errors in container logs.
- Landing page: production already serves the v3.8 release section
  (`id="release"`, "Version 3.8.0 · November 2026"). The worktree commit
  `fc165c3` is an exact duplicate of master commit `cb91703`
  (identical patch-id), so no second deploy was needed.

## Windows state (verified this session)

- Apps are **stopped** as of the latest process scan (no MTGA/Arena/python/
  pythonw/mtgacoach processes).
- Installed location under test:
  `C:\Users\joshu\MTGA Coach 3.8.0 Release Test`
- Installed runtime receipt:
  - `arenamcp 3.8.0`
  - PySide6 `6.12.0`
  - module path:
    `...\runtime\Lib\site-packages\arenamcp\__init__.py`
  - packaged bridge DLL SHA256:
    `49D8354208F9E8B884DE17711F17EE1ECDDEDC64CC02BDBC2CFA358A0CBF8B4E`
  - Qt platform plugins present: `qwindows.dll`, `qoffscreen.dll`,
    `qminimal.dll`, `qdirect2d.dll`
  - gateway reachable: HTTP 401 in 142 ms (reachability probe; no key printed)
  - installed build does **not** contain the post-release trial UI strings
    (`installed_trial_ui False`) because it was built from tag v3.8.0.
- Shared checkout access from Windows is currently unavailable:
  - `Z:\mtgacoach` missing.
  - UNC path `\\10.0.0.2\repos\mtgacoach` also missing in the latest probe.
  - The authoritative NAS checkout is updated via the Mac mount to
    `36a21d2`; when Windows reconnects the share it will see that source.
- Bridge source compatibility: no bridge source changes between tag
  `v3.8.0` (`50cd983`) and master (`36a21d2`); installed DLL provenance is
  unchanged from the verified v3.8.0 build.

## Mac state

- No MTGA/Arena/Coach/ArenaMCP processes running.
- Editable Python points at `/Volumes/repos/mtgacoach/src`.
- Do not overwrite the live editable checkout while either Coach runs.

## Audit / sessions

- Reconciliation audit session:
  `session-be34d6d1-6e07-4651-aac8-f4621fcf22eb` — **completed** (turn ended
  22:08 PDT, exit 0). Read-only: no source, branch, remote, app, settings or
  credential changes.
- Final outputs (durable copies):
  `/Users/joshu/Documents/Codex/2026-10-31/task/backlog-audit/completed-work-reconciliation-20261101.json`
  and `.md` (also at `/tmp/coach-completed-work-reconciliation.{json,md}`).
- Result at master snapshot `613e146` (the two later commits are test-only):
  - 22 tasks: merged **17**, evidence-only **4**, incomplete **1**.
  - 260 candidate commits across 220 tips in four repos: exact ancestry **147**,
    stable patch-id equivalence **93**, adapted-hunk equivalence **15**,
    superseded **1**, rejected **3**, unverified **1**, ready-unmerged **0**.
  - All 11 screenshot-scope titles explicitly accounted for.
  - Rejected: `a33182e` (trial340 experimental queue branch), `20fcae8`
    (original #308 HOLD), `dde3c7e` (original DSH711 review hold).
    Superseded: `091bd18` by privacy-safe `bd1b5f1` (`9fd6562`).
    Unverified: `8f31662`, a test-only review branch, not a production fix.
  - These are candidate-commit counts, not confirmed current bugs.
- Installer session `fb7bd410-7d51-44fe-9a15-e3b73b327d44` completed; its
  candidate `1087b74` was integrated as `7bb9418`.
- Do not launch a duplicate audit; the reconciliation is final.

## Speech fix (commit `613e146`)

Field report `bug_20261101_194613.json`: the 19:45 Emrakul line appeared twice
in the feed but was never spoken. Visual-analysis advice was emitted as an
uncategorized speak request, which the desktop controller treated as workflow
and suppressed in commentary mode.

- New `SpeechCategory.ADVICE` between essential and workflow; `speak_advice`
  defaults to it, so advice speaks in every mode.
- Autopilot local narration stays `routine`; threat-combo lines stay
  `essential`.
- Suppressed speech is no longer logged as spoken, and markdown advice plus its
  stripped TTS copy no longer duplicate the feed.
- Tests: `tests/test_standalone_speak_advice.py`,
  `tests/aoa/test_controller.py` (two new cases),
  `tests/test_compact_coach.py` (dedupe case).
- Not yet installed on Windows or Mac; it ships with the next build.

## Next actions

1. Reconnect Windows to the NAS share, then verify it sees master `0a5c1f6`.
2. Decide whether the post-release fixes (`1db7896`, `7ab802a`, `36a21d2`,
   `613e146`, `49765ac`, `0a5c1f6`) ship as a new version/release; do not
   mutate v3.8.0 assets.
3. Re-run Windows installed-path smoke checks after any new install.
4. Resume dual monitoring only after rollout; record stalls in
   `autopilot-improvements.md`.
5. Continue the evidence-backed backlog from the reconciliation's
   ready-unmerged list (currently empty) and the incomplete task
   (`qualification-trial`).

## Resume commands

```bash
# Source / CI
cd /Volumes/repos/mtgacoach && git status --short && git log --oneline -5
gh run list --branch master --limit 5

# Resume reconciliation audit (same cwd + overlay) — already final; only if new evidence appears
cd /Users/joshu/Documents/Codex/2026-10-28/task
dsh headless --session-id session-be34d6d1-6e07-4651-aac8-f4621fcf22eb --json \
  "Continue the completed-work reconciliation audit from your checkpoint."

# Windows read-only checks (write .ps1 locally, scp, then run)
scp -F /Users/joshu/.ssh/mtgacoach_windows_vm_config /tmp/<script>.ps1 \
  mtgacoach-windows:'C:/Users/joshu/<script>.ps1'
ssh -F /Users/joshu/.ssh/mtgacoach_windows_vm_config mtgacoach-windows \
  'powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\joshu\<script>.ps1'
```
