# Live performance handoff — September 7, 2026

## User's immediate problem

Windows-wide input and rendering lag: typing in the agent window, clicking Arena, and dragging windows are slow while mtgacoach is open. User wants the cause fixed, not more speculative explanations. They requested this handoff due to quota. They normally launch from a desktop shortcut; inspect and use that shortcut on future launches. Do not assume it matches launch.bat.

**The remaining system-wide lag has NOT been diagnosed or fixed.** CPU/RAM saturation is not supported by the most recent samples. Previous explanations blaming the speech watchdog alone were too confident.

## Current launch and measurements

- Agent launched `Y:\mtgacoach\launch.bat` around 13:45:45. This starts `scripts/launch_installed.py` with `%LOCALAPPDATA%\mtgacoach\venv\Scripts\pythonw.exe`, unlike the user's earlier shortcut (`C:\Program Files\mtgacoach\runtime\Scripts\pythonw.exe scripts/launch_desktop.py`). User accepted a dialog, then UI appeared around 13:46:25–13:46:32. Startup took tens of seconds. Both launch paths resolve source to `\\10.0.0.2\Repos\mtgacoach` (network share).
- Latest known processes: desktop actual PID 1856, venv wrapper 12504; TTS actual 22004, wrapper 11456; coach actual 11796, wrapper 18056. Arena PID 20484. Recheck identities before acting; these are historical IDs.
- During reported lag: overall CPU ~25%; ~49 GB memory available; zero sampled page-ins and disk queue. Arena uses 2–3 logical CPU cores, DWM sometimes ~1 core. GPU sample: Arena 52%, DWM 22% on same 3D engine. Earlier Arena GPU 78%. TTS idle in multiple samples. No sustained saturation established; intermittent spikes still possible.
- Five actual Windows `SendMessageTimeoutW(WM_NULL)` probes: coach 0.6, 0.2, 102.0, 2.3, 0.4 ms; Arena 2–12 ms. Windows `Responding=True`. This excludes a persistent UI-thread hang during those samples, not frame/input latency.
- Coach logs show repeated deck reconstruction multiple times/sec and substantial pipe traffic (~0.6 MB/sec sampled). Inspect unnecessary GUI redraw / text layout / update rates and overlay rendering.
- Recent LLM calls typically 1.6–3 seconds; these do not explain dragging other windows slowly.
- `UiAnrWatchdog` emits spurious-looking huge stall durations (51+ seconds, earlier 386 seconds), but captured UI stack is just `app.exec()`. MainWindow supplies `QTimer.singleShot(0, cb)` from the watchdog Python thread, which has no Qt event loop. Investigate queued signal to a GUI-thread QObject instead. Do NOT present current ANR dumps as proof of a blocked GUI. Dumps repeat every 10 seconds under `%USERPROFILE%\.mtgacoach\anr_dumps`.

## Best next diagnostic sequence

1. Reproduce while typing/dragging. Perform a controlled coach-closed vs coach-open comparison, verifying all coach/TTS processes exit. Preserve Arena and unrelated services. User has authorized fixing lag, stopping leftover coach speech, and stopping MageZero on NUC.
2. Inspect desktop shortcut target/arguments and compare interpreter, environment, source location, and settings to agent's launch. Network-share import/startup delay is a candidate, not established root cause.
3. Sample frame/GPU/DWM load and per-core CPU during visible lag, not just idle snapshots. Consider Windows performance recording if needed; actual input-to-display lag is the complaint.
4. Inspect low-level keyboard/global hotkeys and overlay/update work. Synergy is running (historical PID 5188); DO NOT stop it casually because it may carry user's keyboard/mouse. No evidence currently implicates it.
5. Isolate TTS, overlay, GUI updates, and coach subprocess one at a time. Do not blindly change GPU drivers, kill unrelated processes, or repeatedly reload Kokoro.
6. Add trustworthy render/queue timing telemetry: current speech protocol includes audio duration, but not actual synthesis wall time. Latest TTS timeout fix allows a truly hung render to wait indefinitely; implement bounded asynchronous recovery if needed without replaying stale advice or blocking GUI.

## Confirmed MageZero contention and incomplete retirement

- Java PID 17208 was definitively MageZero XMage Gen14 `recoveryB20260906_gen14-arm7-EVG_Elves`, with eight connections to Blackwell `10.0.0.10:50052`, 11–12 GiB private memory and up to ~7 cores.
- Local Stop-Process failed Access Denied. Successfully stopped it via `ssh joshu@10.0.0.10`, then remote SSH from Blackwell to `joshu@10.0.0.25`, running encoded PowerShell `Stop-Process -Id 17208 -Force`. Verified gone locally afterward. This is DONE.
- Direct local SSH to NUC failed host-key verification; Blackwell's existing SSH trust worked. Do not disable host-key verification.
- User explicitly requested **all RL execution only on Blackwell, no NUC**. Blackwell `~/repos/magezero/configs/hosts.yml` ALREADY lists only Blackwell, 5 concurrent arms, localhost:50052. However existing dispatcher PID 347058 was started before this change and still had remote NUC and Mac SSH children. NUC SSH child disappeared after worker stop. It may retain old hosts in memory; inspect and safely restart/reload dispatcher to enforce actual Blackwell-only operation. THIS IS NOT FINISHED.
- Supervisor `/home/joshu/mz_watch2.sh` PID 339673 uses singleton flock, checks maintenance files and run completion, restarts via `/home/joshu/launch_mz_train_fleet.sh`. Launcher uses venv-mz-rocm312, `mz train --hosts configs/hosts.yml`, recovery attempt `recoveryB20260906`, and writes `remediation-recovery-20260906/RECOVERY_REQUIRED` on nonzero exit. Avoid accidental maintenance hold or duplicate dispatchers. Preserve artifacts and candidate gates.
- `src/magezero/cli.py:29` calls `distributed.load_hosts(args.hosts)`; `runner.py:869` also loads hosts. Need verify running job lifecycle before restart.
- Read Blackwell repo AGENTS.md and docs/PLAN_OF_RECORD.md before changes. R9700 only; NVIDIA GPUs/dsv4 are off limits. Inference :50052 self-play, :50054 live coach. Do not stop live inference.

## Changes already made in mtgacoach (uncommitted)

User's existing `AGENTS.md` modification must be preserved. No commits or branches created.

- `autopilot.py`: removed dangling `_should_prefetch_vision` / `_scan_layout_if_needed` calls that crashed EVERY execution before typed decisions. Replaced missing `_exec_pass_priority` with existing `_run_bridge_action` and returns actual success.
- `autopilot_bridge.py`: removed missing `_build_attacker_id_map` calls; uses existing `_find_instance_id` battlefield resolution. Tests cover both attacker entry paths.
- `desktop/tts_manager.py`: stop system fallback before Kokoro request/playback; six-second timeout now only emits a delayed-status message, keeping render busy and preserving latest pending request. Previously timeout called synchronous shutdown/start (waits up to seconds on GUI thread), reloaded model, replayed last advice via SAPI. Also ignore new speech during shutdown, invalidate generations, stop timeout timer, reject late rendered playback.
- `desktop/coach_session.py`: stop TTS before coach on shutdown. Closing previously left GUI process alive and speech continuing; actual GUI process was explicitly stopped in earlier turn.
- `desktop/main_window.py`: restore frame position via resize+move rather than setGeometry; save client size rather than frame size; preserve negative monitor coordinates; clamp full frame to available screen after show.
- Tests: autopilot suite 92 passed at first broad run, then added attacker and empty-plan checks passed separately; TTS handoff tests 4 passed; window geometry tests 3 passed. No full suite run.
- `desktop/app.py` still has an **unfixed NameError: RESTART_EXIT_CODE undefined** after app.exec() returns (seen twice in desktop-launch.log, including 13:43:33). Fix with regression coverage; may explain lingering error dialog / desktop process after close. Do not confuse that dialog with confirmed TTS worker duplication.

## Latest mana bug fix (source changed; running app has old code)

User twice reported autopilot casting The Notary Hobbits before affordable. Confirmed schema mismatch:

- Plugin `Plugin.GameState.cs:733` serializes action type as `Cast`, `Play`, `Pass` and `hasAutoTap` boolean.
- Typed builder `decisions.py` only recognized `ActionType_Cast` and `autoTapSolution`. Live casts became nameless `Cast` with `payable=None`, bypassing warnings and poisoning own-action tracking (Green Sun's Zenith bot cast then falsely labeled manual play, leading to 20-second stand-down).
- Builder now normalizes unprefixed types and recognizes `hasAutoTap` or non-null `autoTapSolution`.
- `ActionPlanner.plan_decision_options` rejects payable=False LLM picks; deterministic picker excludes payable=False; submit_option defensively refuses such submissions.
- Added `test_live_bridge_cast_payability_is_enforced` in tests/test_decisions.py, covers live fields, name, both planners and submission rejection. All 8 tests in test_decisions.py passed; git diff --check passed (only CRLF warnings).
- This policy conservatively refuses casts without auto-pay evidence, even if manual payment might work. Existing legacy path has richer affordability logic; evaluate consistency and edge cases before broad claims.
- App must restart to load latest mana fix; do not claim it is active in current PID 1856.

## Test environment / tools

Windows PowerShell, cwd Y:\mtgacoach. No pytest in either installed interpreter originally. Installed pytest only under `%TEMP%\mtgacoach-test-deps`, leaving runtime dependencies untouched.

```powershell
$env:PYTHONPATH = "$env:TEMP\mtgacoach-test-deps;Y:\mtgacoach\src"
& 'C:\Program Files\mtgacoach\runtime\Scripts\python.exe' -m pytest tests/test_decisions.py -q --tb=short
```

Tests redirect logs via conftest. Avoid full MainWindow construction tests during live play unless coach/session launch and global hotkeys are mocked. Geometry tests deliberately use plain QMainWindow.

Logs: `%USERPROFILE%\.arenamcp\standalone.log`, `%LOCALAPPDATA%\mtgacoach\desktop.log`, `desktop-launch.log`. Never dump settings/secrets. PowerShell formatting requires explicit Format-Table/List for different object schemas in one command.
