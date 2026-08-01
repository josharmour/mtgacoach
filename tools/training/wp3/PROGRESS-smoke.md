# PROGRESS — smoke: integrated distributed-arms + CUDA-serving smoke test

Date: 2026-07-31. Machine: the-training-host. Agent key: `smoke`.
Program: MAGEZERO SPEED PROGRAM levers 1 (distributed arms) + 2 (CUDA serving)
(rl-pipeline-fix.md; docs/rl-training-status.md addenda 20-21).

## Verdict: GO (conditions below)

Adopt distributed arm dispatch (S2 branch `distributed-runner`) + CUDA serving
via `MZ_SERVER_PYTHON` (S1 venv `venv-mz-cuda`) at the next generation
boundary. Both levers were exercised together, live, on the-training-host, alongside
the untouched live curriculum, and every verification passed.

## Guard (step 1)

- 10:41: `pgrep -f "mz_budget_sweep|mz batch"` empty (only self-match);
  load avg 13.78 < 17. Watcher `mz_boundary_sweep_watcher2.sh` (PID <redacted>)
  was armed but its sweep sequence was NOT active. Started immediately.
- Mac probes at 10:41: `ssh <user>@<LAN-IP>` connect timeout,
  `ssh <user>@<LAN-IP>` connection refused. No Mac enrolled; both arms local.

## What ran (steps 2-3)

- Unit tests first: `tests/test_distributed.py` on branch `distributed-runner`
  (worktree `~/repos/magezero-wt-distributed`, commit c65dbdc),
  interpreter `~/venv-mz-cuda/bin/python3`: **12 passed in 0.47s**
  (covers arm planning, per-host isolation, backward-compat drift locks,
  run.json contract incl. atomic write, mocked dispatch, run_generation).
- Integrated smoke: `scripts/integration_distributed.py --workdir
  ~/repos/magezero --games 6` — 2 concurrent local arms
  (host dir `.mz_tmp/hosts/it-20260731_104257`), `training.threads 2`,
  Xmx 6g per arm, MCTS budget 40 / timeout 1000 ms / max_turns 30,
  distinct minimax opponents (Standard-MonoR, Standard-MonoG), both arms
  pointed at S1's CUDA server `localhost:50060` (PID <redacted>, venv-mz-cuda,
  CUDA_VISIBLE_DEVICES=1, MZ_SERVER_VRAM_GB=6).

## Measured results

| check | result |
|---|---|
| arms completed | 2/2, rc=0 both, 6 games each |
| win-rate lines parsed | arm0 wr=0.1667 (6 games), arm1 wr=0.1667 (6 games); per-arm `WR with UWTempo vs <deck>` lines attributed correctly |
| wall clock | **176 s** (arms overlapped: 10:42:57 -> 10:45:53 / 10:45:05) |
| sequential estimate (sum of arm durations) | **304 s** (176.1 + 128.2) — 1.73x speedup at 2 arms |
| output isolation | 4 distinct files, distinct sha256, zero cross-contamination: arm0 primary 9,107,696 B `b3018caf...`, arm1 primary 5,107,696 B `a96dd6a8...`, arm0 opp 21,640 B `f292f5f6...`, arm1 opp 21,640 B `6db9cb80...` |
| per-arm JVM logs | `magezero-gen0-arm0-Standard-MonoR.log` / `...arm1-Standard-MonoG.log`, both present, distinct paths |
| CUDA server held 2 concurrent JVM clients | yes — request counter 14,897 -> 31,697 across the smoke window (**16,800 evals / 176 s ~ 95 evals/s served**; counter static for 32 s pre-dispatch so the delta is smoke traffic). Coalesced size=2 batches observed in the log. `healthz` ok after; PID <redacted> uninterrupted. Note 95 evals/s is CLIENT-limited (budget 40, threads 2 per arm) — S1 bench capacity is 623 evals/s at batch 32, so headroom is ~6.5x. |
| live workloads untouched | curriculum java PID <redacted> and `mz train` PID <redacted> alive after; run bookkeeping uncorrupted: `runs/2026-07-29_03-19-18/run.json` sha256 `eb006d06...` identical pre/post and parses; `.mz_tmp/game.yml` sha256 `bed2a625...` identical pre/post |
| bookkeeping contract | integration path never touches run.json from arm threads (by design); atomic-write + arms-record contract locked by the 12 unit tests |
| cleanup | `.mz_tmp/hosts/it-20260731_104257` removed; `.mz_tmp/hosts/` empty |

No magezero code changes were needed for this smoke; no new magezero branch
was created (S2's `distributed-runner` was used as-is).

## Branch relationship (verified)

`git diff distributed-runner speed/cuda-serving-50060 -- src/` shows S2's
branch is a strict superset for `src/` (S1's src improvements were folded into
c65dbdc); S1's commit b6baefe adds only `tools/bench_eval_http.py` and
`tools/launch_mz_server_cuda.sh`. Both branches fork from 1b7af20 and merge
cleanly (no overlapping paths).

## Cutover steps (step 4) — generation boundary ONLY

Preconditions:
- Current curriculum is all-minimax opponents (required: distributed mode
  raises NotImplementedError for checkpointed-mcts opponents).
- Do NOT point the fleet at the standalone :50060 server for the live run —
  it is pinned at `--version 1` and `ensure_servers` treats any healthy server
  as externally managed (it would never pick up new checkpoints between
  generations). The runner must spawn its own per-generation servers on
  50052/50053 via `MZ_SERVER_PYTHON`.

At the boundary (pattern: arm a watcher modeled on
`~/mz_boundary_sweep_watcher2.sh` — poll `run.json`
`current_gen`/`stage`, then act; suggested name
`mz_distributed_cutover_watcher.sh`):

1. Wait for the boundary: poll
   `python3 -c "import json;d=json.load(open('~/repos/magezero/runs/<RUN>/run.json'));print(d['current_gen'],d['stage'])"`
   until the target generation's `generate` stage is reached (watcher2 lines
   24-31 are the exact pattern).
2. Stop the curriculum cleanly (watcher2 lines 33-38):
   `pkill -f '[m]z train'`; escalate to -9 only if TERM survives 20 s; then
   `pkill -f 'mage-magezero-1.4.58.jar'`. The old :50052 server dies with its
   parent; verify with `ss -tln | grep 50052`.
3. In `/mnt/repos/magezero`: `git merge distributed-runner && git merge
   speed/cuda-serving-50060` (local merges only — origin is upstream
   WillWroble/MageZero, NEVER push).
4. Write `configs/hosts.yml` (from `configs/hosts.yml.example`):
   ```yaml
   hosts:
     - name: the-training-host
       ssh: null
       workdir: ~/repos/magezero
       java_threads: 6
       java_xmx: 24g
       server_host: localhost
       server_port: 50052        # runner-spawned per-gen server
       max_concurrent_arms: 2
   ```
   Heap is per concurrent arm (2 x 24g); watch load — the smoke ran
   threads 2 at load ~14-16 without breaching 17, but 2 arms x threads 6
   is a bigger bite. If load contends with training, drop java_threads to 4.
5. Relaunch with CUDA serving:
   ```bash
   cd /mnt/repos/magezero && \
   MZ_SERVER_PYTHON=~/venv-mz-cuda/bin/python3 \
   MZ_SERVER_ENV="CUDA_VISIBLE_DEVICES=1 MZ_SERVER_VRAM_GB=6" \
   nohup mz train --hosts configs/hosts.yml &
   ```
   (`runner.start_server` honors both vars — runner.py lines 266-288;
   server.py enforces the 6 GB allocator cap on card 1, which held 2 clients
   in this smoke and 623 evals/s in S1's bench without touching Qwen.)
6. Optional: stop the standalone :50060 smoke server (PID <redacted>) to return
   its ~5 GB on card 1; it is not part of the live path.
7. First-generation sanity: confirm `[distributed]` prep lines in the run log,
   two JVMs under `.mz_tmp/hosts/the-training-host/arms/`, per-arm wr lines merged
   into run.json `arms` records, and `server_50052.log` showing the
   `[vram] allocator capped` banner and CUDA device.

Mac enrollment stays OUT until `scripts/check_remote_host.sh` passes
(Remote Login was still off on both IPs at 10:41 today); when it passes, add
the documented `mac` host entry with `server_host: <the-training-host LAN IP>` and a
server started with `MZ_SERVER_BIND=0.0.0.0`.

## Known discontinuity

Adopting both levers changes generation throughput and eval latency, which
changes effective teacher strength — that is the documented reason cutover is
generation-boundary only. Expect the post-cutover generation's stats to shift;
do not read that shift as a regression.

## Residual concerns

- Smoke arm speedup was 1.73x, not 2x: arm durations were unequal (176 s vs
  128 s), so the win from 2 arms is bounded by the slower arm. Real arms
  (250 games, budget 300) should equalize better, but tail-arm skew is the
  scaling limit.
- The :50060 request counter could in principle include traffic from other
  swarm agents during the 176 s window (it was static for the 32 s before
  dispatch; contamination unlikely but not impossible).
- `venv-mz-cuda` reuses venv-train's site-packages via a `.pth` link
  (S1 finding): torch upgrades in venv-train silently change the serving env.
- waitress `threads=6` caps in-flight server concurrency; fine for 2 local
  arms x threads 6 MCTS clients, revisit before enrolling the Mac
  (4 concurrent arms).
- Data volumes under a shared host dir (`data/playerA`/`playerB` Java write
  patterns) remain unaudited beyond "no collisions observed" (S2 finding).
