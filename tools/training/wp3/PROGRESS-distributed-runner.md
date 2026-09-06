# PROGRESS: distributed-runner (MageZero speed program, lever 1)

Date: 2026-07-31. Swarm task key: `distributed-runner`.
MageZero branch: `distributed-runner` (LOCAL only, never pushed — origin is
upstream WillWroble/MageZero). Worktree used for development:
`~/repos/magezero-wt-distributed` (live tree untouched; the running
`mz train` + boundary watcher were never disturbed).

## What was built

Multi-host parallel arm dispatch for `runner.py` (arms were strictly
sequential: per-opponent `build_game_yml` -> `launch_jvm`).

New/changed files in the magezero branch:

- `src/magezero/distributed.py` (new, ~600 lines) — hosts.yml schema
  (`HostSpec`), round-robin arm->host planning, per-host/per-arm isolation,
  attached-subprocess/ssh dispatch with per-host concurrency caps, win-rate
  parsing + merge into the existing `run.json` gen contract.
- `src/magezero/runner.py` — `run_pipeline(..., hosts=None)` routes a
  non-trivial fleet to `distributed.run_generation()`; sequential path is the
  verbatim old loop. `record_gen(..., arms=None)` adds an `arms` key ONLY for
  distributed runs. All `run.json` writes now atomic (tmp + `os.replace`;
  torn run.json was a documented hazard).
- `src/magezero/cli.py` — `mz train --hosts configs/hosts.yml` (opt-in).
- `src/magezero/server.py` — `MZ_SERVER_BIND` env (default still 127.0.0.1);
  remote JVMs need a server bound to 0.0.0.0.
- `configs/hosts.yml.example` — the-training-host entry + fully documented
  commented-out Mac entry.
- `scripts/check_remote_host.sh` — remote-host preflight (ssh BatchMode,
  java >= 17, tree visibility, NFS/CIFS write round-trip, server healthz from
  the remote side, db lock status).
- `scripts/integration_distributed.py` — gated real-JVM concurrency smoke.
- `tests/test_distributed.py` — 12 unit tests, all passing (`PYTHONPATH=src
  python -m pytest tests/test_distributed.py -q` -> 12 passed).

## Isolation design (what the shared tree actually allows — measured)

- The repo tree is a CIFS/SMB share (`//<LAN-IP>/repos`, mounted
  ~/repos and /mnt/repos; the Mac sees the same tree). **Symlink
  creation fails with "Operation not supported"** — measured 2026-07-31 when
  the first integration attempt died on `ln -sfn`. The design uses real
  copies only.
- Per HOST (`<workdir>/<tmp_subdir>/xmage/`, prepared once by the dispatcher,
  cached): copies of `config/`, `log4j.properties`, `db/cards.h2.mv.db`
  (269 MB), seeds of `seenFeatures.ser`/`FeatureTable.txt`. This dir is the
  cwd of every arm JVM on that host.
- H2 card DB: JVM URL is `jdbc:h2:file:./db/cards.h2;AUTO_SERVER=TRUE`
  (recovered from mage jar strings). Same-path same-host concurrency is the
  supported AUTO_SERVER case — measured working: two arms shared one host db
  copy, first JVM served, second attached. The shared `xmage/db` is never
  opened by arms: the live curriculum JVM holds it under a different
  canonical path (/mnt/repos vs ~/repos) and H2's lock check
  rejects that; cross-host locking over the share is worse.
- Per ARM: own `game.yml`, launch script, captured stdout, and slug-suffixed
  JVM logs. Stock `log4j.properties` HARDCODES `magezero.log` /
  `magezeroErrors.log` (`-Dlog.file` is inert — measured: first passing run
  wrote interleaved shared logs), so each arm gets a sed-rewritten log4j
  config.
- Win-rate attribution: primarily the arm's own stdout
  (`Player A win rate: X% (w/n)`), fallback per-arm magezero log, fallback
  host `WinRates.txt` lines filtered by byte-offset-at-prepare + opponent
  deck (unique within a generation).
- `run.json`: arm threads never touch it; merge happens on the caller thread
  after all arms join, written atomically. Sequential gen entries keep the
  exact legacy key set (unit-tested); distributed entries add one `arms` list.

## Backward compatibility

- No `--hosts`, or a hosts.yml that degenerates to one local default host
  (`is_trivial()`): `run_pipeline` takes the pre-existing sequential loop
  verbatim — same `.mz_tmp/game.yml`, same ports, same `xmage/mz-xmage.sh`
  launch, same run.json shape.
- Locked by tests: `test_build_game_yml_unchanged_defaults` (sequential
  builder output: `.mz_tmp/game.yml`, ports 50052/50053, threads untouched),
  `test_arm_cfg_matches_sequential_builder` (for a default local host the
  distributed builder produces the byte-equal config dict),
  `test_record_gen_contract` (legacy gen key set preserved),
  `test_is_trivial`.

## Integration test (REAL concurrent JVMs, run on the-training-host)

Gates at launch: load 12.9 (< 17 required), no `mz_budget_sweep`/`mz batch`
running. Live workloads (curriculum JVM PID <redacted>, R9700 server :50052, LoRA
on CUDA0, Qwen on CUDA1) untouched — verified live JVM still running after.

Run 1 (pre-fix): FAILED in <1 s — `ln: Operation not supported` (CIFS
symlink discovery). Fixed by the copy-based design above.

Run 2: 2 arms x 4 games (UWTempo vs Standard-MonoR / Standard-MonoG,
minimax), threads 2, Xmx 6g, search_budget 40 / timeout 1000 ms, server
localhost:50052 (live server, brief extra load — sanctioned), both JVMs
concurrent (pids 452022/452023 observed running simultaneously alongside live
276069). Result:
- both arms rc=0; wall 230 s vs 398 s sum of arm durations (1.73x on one
  host) — intervals overlapped.
- per-arm win rates parsed and attributed: MonoR 0.00 (0/4), MonoG 0.25
  (1/4); WinRates.txt offset+deck attribution returned exactly one correct
  line per arm.
- both primary hdf5 outputs written at distinct paths (5,107,696 bytes each);
  H2 same-path sharing between the two JVMs worked.
- verdict FAILED only on the per-arm JVM log check: log4j hardcodes
  magezero.log, -Dlog.file inert. Fixed (sed-generated per-arm log4j config).

Run 3 (with per-arm log4j fix): PASSED.
- both arms rc=0, intervals overlapped; wall 196 s vs 316 s sum of arm
  durations (1.61x on one host).
- per-arm JVM logs present and distinct
  (magezero-gen0-arm0-Standard-MonoR.log / magezero-gen0-arm1-Standard-MonoG.log).
- per-arm win rates: MonoR 0.25 (4 games), MonoG 0.00 (3 counted games — one
  of 4 appears excluded by the max_turns=30 smoke override).
- both primary hdf5 outputs written at distinct paths (5,107,696 bytes each).
- live curriculum JVM (PID <redacted>) confirmed alive and untouched afterward;
  test tmp dirs under .mz_tmp/hosts/it-* removed after each run.

## Mac enrollment status

- SSH to the Mac is DOWN (<LAN-IP> timeout, <LAN-IP> refused — Remote
  Login likely off). Everything Mac-side is designed-for but unverified.
- `configs/hosts.yml.example` documents the preconditions: Remote Login on,
  passwordless key auth, java 17+ on the non-interactive PATH, the repos
  share mounted (expected `/Volumes/repos/magezero` — VERIFY, unverifiable
  today), server reachable from the Mac (requires `MZ_SERVER_BIND=0.0.0.0`
  on the serving box; the live :50052 server is loopback-only and must NOT be
  pointed at from remote hosts).
- `scripts/check_remote_host.sh <user>@<MAC_IP> /Volumes/repos/magezero
  <SERVER_IP> 50060` runs the whole preflight in one shot.

## Concerns / follow-ups

1. Remote (Mac) path is code-complete but UNTESTED end to end — ssh
   unreachable. First real enrollment must run check_remote_host.sh.
2. Distributed mode refuses checkpointed-mcts opponents (they need per-arm
   opponent servers; port collision). Current curriculum is all-minimax, so
   no impact today. Lift by allocating per-arm opponent ports if needed.
3. The per-host db copy is made while the live JVM may hold the source db
   open (read-mostly MVStore; mtime showed no writes since open). The prep
   script warns when the source lock exists. If a copy were torn, the arm
   JVM would rebuild its own db copy (minutes of CPU), not corrupt the
   source.
4. `data/playerA`/`data/playerB` under the host xmage dir are shared by that
   host's arms; nothing was observed writing colliding filenames during the
   smoke, but it is unaudited Java-side behavior.
5. Adopting this for the live curriculum should happen at a generation
   boundary only (throughput changes teacher strength — documented
   discontinuity), and the fleet server story pairs with lever 2 (CUDA
   serving on card 1 with MZ_SERVER_BIND=0.0.0.0 + VRAM cap).
