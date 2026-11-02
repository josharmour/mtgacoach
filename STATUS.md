# MTGA Coach — integration status

Last updated: 2026-11-01T22:52:00-08:00 (America/Los_Angeles)

This is the durable operational checkpoint for the current maintenance window.
It records verified facts only; do not treat planned work as complete.

## Source and CI

- Repository: `/Volumes/repos/mtgacoach`, branch `master`.
- Local and `origin/master` HEAD: `429418c` (`Website: point the landing page
  at 3.8.1`).
- Recent commits:
  - `429418c` Website: point the landing page at 3.8.1
  - `e655ef9` v3.8.1: speak advice in every mode and show trial state
  - `7704c54` Record the final reconciliation audit and green master
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
- CI run for `7704c54` (STATUS.md only): https://github.com/josharmour/mtgacoach/actions/runs/41398227544
  - `ruff`: success; `Hosted trials and Patreon keys`: success
  - `pytest`: **failure** — both cases of
    `test_the_slowest_real_boards_stay_within_the_latency_target` measured
    483.16 ms and 486.92 ms against the CI bound of 480 ms (6 × 80). The
    deterministic invariants in the same test passed (`bounded`, not
    `truncated`, identical node counts), so this is runner speed, not a search
    regression: the same boards measure 130-137 ms on this Mac at load ~5.
    No test or bound was changed; the next run passed.
- CI run for `e655ef9` (v3.8.1 release commit):
  https://github.com/josharmour/mtgacoach/actions/runs/41399639749 — success.
- CI run for `429418c` (landing page): https://github.com/josharmour/mtgacoach/actions/runs/41401765642
  — in progress at the time of writing.
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

## Release v3.8.1

- GitHub release published (not draft/prerelease), marked latest:
  https://github.com/josharmour/mtgacoach/releases/tag/v3.8.1
  (`publishedAt` 2026-11-02T06:40:48Z).
- Tag `v3.8.1` points at `e655ef9`.
- Assets verified live:
  - `mtgacoach-Setup.exe`, 293,359,666 bytes,
    sha256 `da24635156d8f90cf347f23ec5652a4f07f1b0ca3d01c4b06df79bf55a29292d`
  - `arenamcp-3.8.1-py3-none-any.wheel`, 3,717,512 bytes; contains
    `arenamcp/resources/MtgaCoachBridge.dll` and `arenamcp/desktop/app.py`
- `releases/latest` returns HTTP 302 to the `v3.8.1` tag; the installer URL
  returns HTTP 302 then HTTP 200 with content length 293,359,666.
- Release notes cover speech, trial/license state, tests, the installer model
  and the known limits (Windows autopilot still needs the BepInEx bridge).

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

### Landing page 3.8.1 (template-only, deployed 2026-11-02T06:44Z)

- Commit `429418c` rewrites only the release section of
  `website/templates/landing.html`: label "Version 3.8.1 · November 2026",
  heading "Advice you can hear in every mode.", two feature cards (advice
  speaks in every mode; trial and license state is visible) and the release
  link now points at `releases/tag/v3.8.1`.
- Deployed by copying that one file into the build context
  `/home/joshu/docker-stack/appdata/mtgacoach/templates/landing.html`
  (production runs the pre-router app, so no Python file was touched).
- Pre-deploy image tagged `trash-guides-mtgacoach:pre-deploy-20261101`.
- Rebuilt and recreated only the `mtgacoach` service; container `Up`.
- Post-deploy verification: `https://mtgacoach.com/` serves "Version 3.8.1"
  and links `releases/tag/v3.8.1`; `/health` returns HTTP 200;
  `POST /v1/subscription/check` with an empty body returns HTTP 401 as before.

## Windows state (verified this session)

### v3.8.1 installer acceptance (2026-11-02T06:20Z)

- Installer copied to the VM and hash-matched the Mac download exactly:
  293,359,666 bytes,
  sha256 `DA24635156D8F90CF347F23EC5652A4F07F1B0CA3D01C4B06DF79BF55A29292D`.
- First silent install passed `/DIR=<path with spaces>` as a separate
  PowerShell array element, so it split at the spaces and installed into
  `C:\Users\joshu\MTGA`. That tree was removed; the retry passed one quoted
  argument string (`/DIR="..." LOG="..."`) and exited 0.
- Installed location under test:
  `C:\Users\joshu\MTGA Coach 3.8.1 Verify`
- Installed runtime receipt:
  - `arenamcp 3.8.1`, imported from
    `...\MTGA Coach 3.8.1 Verify\runtime\Lib\site-packages\arenamcp\__init__.py`
    with `sys.executable` = `...\runtime\python.exe` — no build-machine source
    path leaked into the installed runtime.
  - packaged bridge DLL
    `...\runtime\Lib\site-packages\arenamcp\resources\MtgaCoachBridge.dll`,
    156,160 bytes,
    sha256 `49D8354208F9E8B884DE17711F17EE1ECDDEDC64CC02BDBC2CFA358A0CBF8B4E`
    — identical to the DLL deployed in
    `C:\Program Files\Wizards of the Coast\MTGA\BepInEx\plugins`.
  - Qt platform plugins present under
    `runtime\Lib\site-packages\PySide6\plugins\platforms`: `qwindows.dll`,
    `qoffscreen.dll`, `qminimal.dll`, `qdirect2d.dll`.
  - gateway probe from the installed runtime:
    `api.mtgacoach.com reachable (HTTP 401)`.
- Launch from the installed location produced three processes, all rooted at
  the install directory:
  - `runtime\pythonw.exe -I -X utf8 -m arenamcp.desktop`
  - `runtime\python.exe -I -X utf8 -u -m arenamcp.desktop.tts_worker`
  - `runtime\python.exe -I -X utf8 -u -m arenamcp.__main__ --pipe`
    (the standalone coach child process)
- `desktop.log` recorded
  `app_root=C:\Users\joshu\MTGA Coach 3.8.1 Verify`; `standalone.log` showed a
  healthy startup (`backend_name=online`, `model=current`,
  `base_url=https://api.mtgacoach.com/v1`, card database ready).
- The app was stopped cleanly afterwards; no python process remained.

### Earlier v3.8.0 checks

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

- MTGA and Coach are **running** (the user reopened them to play after the
  maintenance window): MTGA pid 57360, desktop pid 57343 (started 19:28:33),
  TTS worker pid 57348, coach subprocess pid 22125 (restarted 22:24:00).
- Editable Python points at `/Volumes/repos/mtgacoach/src`; the venv reports
  `arenamcp 3.8.1`.
- The coach subprocess restarted at 22:24, after `613e146` (20:55) and
  `e655ef9` (22:23), so it already runs the speech fix. The **desktop**
  process started at 19:28 and therefore still runs the pre-fix
  `commentary_controller.py`; restart Coach between matches to load it.
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

1. Restart Mac Coach between matches so the desktop process loads `613e146`
   (the mode gate and feed dedupe live in the desktop process); confirm the
   window reports 3.8.1.
2. Windows: reconnect the NAS share so `Z:\mtgacoach` resolves, confirm it sees
   master `429418c`, then launch Coach from the installed 3.8.1 location for
   gameplay.
3. Resume dual monitoring with fresh baselines: Mac account `armour`, Windows
   account `armour.tv`; record stalls and actionable mistakes in
   `autopilot-improvements.md` with evidence, distinguishing manual or
   restarted games.
4. Continue the evidence-backed backlog from the reconciliation's
   ready-unmerged list (currently empty) and the incomplete task
   (`qualification-trial`).
5. Known CI flake to watch, not to paper over:
   `test_the_slowest_real_boards_stay_within_the_latency_target` measures a
   clock bound that a slow shared runner can cross (483/487 ms against 480 ms
   on run 41398227544) while its deterministic invariants pass. A principled
   fix would normalise the bound by a calibration workload measured on the same
   runner; do not simply raise the multiplier.

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
