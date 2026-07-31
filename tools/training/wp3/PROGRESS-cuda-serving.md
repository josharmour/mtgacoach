# PROGRESS: cuda-serving (MageZero speed program, lever 2)

Date: 2026-07-31. Machine: blackwell. Agent key: cuda-serving.

## What was done

MageZero inference server (src/magezero/server.py — HTTP/msgpack via
Flask+waitress, POST /evaluate, NOT gRPC despite the task brief) brought up on
CUDA card 1 alongside the Qwen coach, and benchmarked against the live R9700
server on 50052.

- Venv: /home/joshu/venv-mz-cuda — fresh venv from venv-train's python 3.12.13,
  with a .pth link to venv-train's site-packages (reuses torch 2.11.0+cu130
  without modifying venv-train, which has no pip). Added: waitress, flask,
  msgpack, pyroaring.
- Launcher: /home/joshu/launch_mz_server_cuda.sh (also committed to the
  magezero repo as tools/launch_mz_server_cuda.sh on local branch
  speed/cuda-serving-50060). Env-tunable: MZ_CUDA_GPU (default 1), MZ_PORT
  (default 50060), MZ_VRAM_GB (default 6, maps to MZ_SERVER_VRAM_GB), MZ_DECK
  (default UWTempo), MZ_VERSION (default: latest verN under models/<deck>),
  MZ_PYTHON, MZ_REPO. Sets HIP_VISIBLE_DEVICES="" so device.py can never pick
  the R9700.
- Running server: UWTempo ver1 (only version present), port 50060, PID 432987,
  4482 MiB on card 1 (cap 6 GB, honored). Left RUNNING for the smoke stage.
- Bench client: magezero repo tools/bench_eval_http.py (local branch
  speed/cuda-serving-50060, NOT pushed — origin is upstream WillWroble).
  Mirrors the real protocol; request shape calibrated from the live server log
  (pre-filter indices p50~1368, keep-ratio ~0.49 -> 900 post-filter indices
  per request, sampled from the non-ignored universe in ignore.roar).

## Measured numbers

CUDA card 1 (RTX PRO 6000 Blackwell), idle server, 200 sequential + 600
concurrent requests per point, 900 kept indices/request, single bag:

| metric | value |
|---|---|
| sequential p50 | 6.91 ms |
| sequential p95 | 8.22 ms |
| concurrency 1 throughput | 135.9 evals/s |
| concurrency 8 throughput | 661.0 evals/s (p50 9.9 ms) |
| concurrency 32 throughput | 623.2 evals/s (p50 49.8 ms) |

Live R9700 server on 50052 (LOW volume: 150 sequential requests throttled at
25 ms spacing + one 64-request concurrency-8 burst — CAVEAT: it is serving the
live curriculum, so these numbers include queueing behind live traffic and are
not a clean device comparison):

| metric | value |
|---|---|
| sequential p50 | 41.13 ms |
| sequential p95 | 72.89 ms |
| concurrency 8 throughput (64 reqs) | 99.4 evals/s (p50 79.7 ms) |

Under-live-load speedup: ~6x on p50 latency (41.1 -> 6.9 ms), ~8.9x on p95
(72.9 -> 8.2 ms). Prior clean R9700 baseline from launch_mz_train.sh comments:
823 states/s at B=16 server-side batch; the CUDA card's server-side ceiling was
measured at 4920-5324 states/s in the server.py VRAM-cap comment, so the clean
device gap is roughly 6x too — but the number that matters for the curriculum
is the ~6x under-load latency cut, since 60% of decisions are timeout-bound.

Throughput plateaus at concurrency ~8 (661 evals/s) and degrades at 32:
waitress serves with threads=6, so at most ~6-7 requests reach the batching
queue simultaneously and MAX_BATCH=16 is unreachable from a single client
regardless of offered load. Raising waitress threads is a candidate follow-up
if arm-level concurrency rises with distributed dispatch.

Qwen coach on card 1 verified unaffected: chat completion on localhost:8002
answered before launch, after launch, and after the bench (card 1: 56.0 GB ->
60.8 GB used of 97.9 GB; card 0 untouched at 33.9 GB).

## Cutover note (how to point an arm at the CUDA server)

Flow of server config: runner.py hardcodes PRIMARY_PORT=50052 /
OPPONENT_PORT=50053, writes them plus configs/game.yml's server.host into
.mz_tmp/game.yml, and the JVM reads that file. The runner also SPAWNS the
server processes itself per arm (start_server) and stops them at arm end —
so cutover is NOT "edit game.yml to point at :50060". The sanctioned path is
the runner's own env hooks:

    export MZ_SERVER_PYTHON=/home/joshu/venv-mz-cuda/bin/python3
    export MZ_SERVER_ENV="CUDA_VISIBLE_DEVICES=1 HIP_VISIBLE_DEVICES= MZ_SERVER_VRAM_GB=6"

added to the `mz train` launch environment (i.e. launch_mz_train.sh, which
currently exports HIP_VISIBLE_DEVICES=0 and CUDA_VISIBLE_DEVICES="" for the
whole process tree — MZ_SERVER_ENV overrides both for the server children
only). The runner then starts its usual servers on 50052/50053, just on CUDA
card 1. Both primary and opponent servers (when the opponent is mcts with a
checkpoint) inherit this: worst case 2 x ~4.5 GB on card 1, inside the spare.
The standalone :50060 server exists for smoke/bench; a manual arm can also be
pointed at it by editing the server block of a hand-built game.yml passed to
xmage/mz-xmage.sh.

RULE: cutover for the LIVE run happens ONLY at a generation boundary.
Eval-throughput changes teacher strength (timeout-bound decisions search
deeper), which is a documented training discontinuity — same rule as the
budget sweep. Do not restart or retarget servers mid-generation; apply the two
exports before the next `mz train` invocation at a boundary the watcher
respects.

## Artifacts

- /home/joshu/launch_mz_server_cuda.sh (launcher, env-tunable)
- /home/joshu/venv-mz-cuda (serving venv; venv-train untouched)
- magezero local branch speed/cuda-serving-50060: tools/bench_eval_http.py,
  tools/launch_mz_server_cuda.sh (never pushed; origin is upstream)
- Running: PID 432987, port 50060, card 1, UWTempo ver1
- Server logs: /home/joshu/mz_server_cuda_50060.log, .run2.log

## Concerns

- Port 50060 was transiently bound by another process during first launch
  (bind failed AFTER model load; port was free on retry) — the swarm shares
  the "50060+" range, so smoke-stage consumers should healthz-check first.
- waitress threads=6 caps effective batching at ~6 in-flight; MAX_BATCH=16 is
  unreachable. Follow-up knob if distributed arms raise offered concurrency.
- R9700 numbers are under live curriculum load (stated caveat); clean-device
  comparison should be redone at a generation boundary if a precise ratio is
  needed.
- venv-mz-cuda depends on venv-train's site-packages via .pth; if venv-train's
  torch is upgraded/removed the serving venv changes silently.
