#!/usr/bin/env python3
"""
Wedge watchdog for the GLM-5.3-Flash vLLM container behind api.mtgacoach.com.

2026-10-19 09:33:58 PDT: the TP1 worker of `glm53-flash-2x-rtxpro6000` hit
"Triton Error [CUDA]: out of memory" loading a freshly compiled b12x attention
kernel (each GPU runs at ~95/95 GiB: 88 GiB weights + a 4.25 GiB KV cache sized
for exactly one 1M-token request). TP0 then waited forever: 4 requests
"running", 0 tokens/s, both GPUs busy-spinning, "No available shared memory
broadcast block found in 60 seconds". The process never exits, so Docker's
restart policy cannot see it; every customer was down until a manual restart.

This is the docker-managed sibling of dsv4-build/scripts/wedge_watchdog.py:
a wedge is a request in flight (num_requests_running >= 1) with ZERO engine
progress — no new engine iteration (vllm:iteration_tokens_total_count, which
advances on every prefill chunk too, so a 1M-token prefill is progress), no
prompt and no generation tokens — for WD_STUCK_SEC. Then `docker restart`.

The container was started with --rm (AutoRemove), so a crash that EXITS the
process deletes it and Docker cannot restart it. When the container is gone
for two polls, WD_RECREATE (a `docker run` generated from its inspect output)
recreates it with --restart unless-stopped. A container that exists but is
stopped was stopped on purpose and is left alone.

Tuning via env:
  WD_CONTAINER     (default glm53-flash-2x-rtxpro6000)
  WD_RECREATE      (default /usr/local/lib/glm53-watchdog/recreate-glm53-flash.sh)
  WD_METRICS_PORT  (default 8001)  scraped inside the container (no published port)
  WD_POLL_SEC      (default 20)    seconds between polls
  WD_STUCK_SEC     (default 120)   zero-progress duration that confirms a wedge
  WD_COOLDOWN_SEC  (default 420)   pause after a restart (model load ~2m45s)
  WD_MAX_PER_HOUR  (default 3)    restart rate-limit (logs & skips beyond this)
  WD_DRY_RUN       (default 0)    if 1, log what it WOULD do but never restart
"""
import os
import subprocess
import time

CONTAINER    = os.environ.get("WD_CONTAINER", "glm53-flash-2x-rtxpro6000")
RECREATE     = os.environ.get("WD_RECREATE", "/usr/local/lib/glm53-watchdog/recreate-glm53-flash.sh")
PORT        = int(os.environ.get("WD_METRICS_PORT", "8001"))
POLL_SEC     = int(os.environ.get("WD_POLL_SEC", "20"))
STUCK_SEC    = int(os.environ.get("WD_STUCK_SEC", "120"))
COOLDOWN_SEC = int(os.environ.get("WD_COOLDOWN_SEC", "420"))
MAX_PER_HOUR = int(os.environ.get("WD_MAX_PER_HOUR", "3"))
DRY_RUN      = os.environ.get("WD_DRY_RUN", "0") == "1"

PROGRESS = (
    "vllm:iteration_tokens_total_count{",
    "vllm:prompt_tokens_total{",
    "vllm:generation_tokens_total{",
)


def log(msg):
    print(f"[glm53-watchdog] {msg}", flush=True)


def state():
    """'running', 'stopped' or 'missing'."""
    out = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER],
        capture_output=True, text=True, timeout=15,
    )
    if out.returncode != 0:
        # "error: no such object: <name>" (docker 28), "No such container" (older)
        return "missing" if "no such" in (out.stderr + out.stdout).lower() else "stopped"
    return "running" if out.stdout.strip() == "true" else "stopped"


def running():
    return state() == "running"


def recreate():
    log(f"RECREATING container {CONTAINER} with {RECREATE} (it is gone)")
    if DRY_RUN:
        log("DRY_RUN=1 -> not actually recreating")
        return
    result = subprocess.run([RECREATE], capture_output=True, text=True, timeout=300)
    log(f"recreate exit={result.returncode} {result.stderr.strip()[-300:]}")


def scrape():
    """(requests running, progress counters) or None when the endpoint is not up yet."""
    out = subprocess.run(
        ["docker", "exec", CONTAINER, "curl", "-s", "--max-time", "8", f"http://localhost:{PORT}/metrics"],
        capture_output=True, text=True, timeout=20,
    )
    if out.returncode != 0 or not out.stdout:
        return None
    in_flight = None
    progress = {}
    for line in out.stdout.splitlines():
        if line.startswith("vllm:num_requests_running{"):
            in_flight = float(line.rsplit(" ", 1)[-1])
        else:
            for name in PROGRESS:
                if line.startswith(name):
                    progress[name] = progress.get(name, 0.0) + float(line.rsplit(" ", 1)[-1])
    if in_flight is None or len(progress) != len(PROGRESS):
        return None
    return in_flight, tuple(progress[name] for name in PROGRESS)


def restart():
    log(f"RESTARTING container {CONTAINER} (confirmed wedge)")
    if DRY_RUN:
        log("DRY_RUN=1 -> not actually restarting")
        return
    subprocess.run(["docker", "restart", "-t", "20", CONTAINER], check=False, timeout=180)


def main():
    log(f"start container={CONTAINER} poll={POLL_SEC}s stuck={STUCK_SEC}s "
        f"cooldown={COOLDOWN_SEC}s max/hr={MAX_PER_HOUR} dry_run={DRY_RUN}")
    last = None
    stuck_since = None
    restart_times = []
    missing_polls = 0
    while True:
        time.sleep(POLL_SEC)
        now = time.monotonic()
        try:
            current = state()
            missing_polls = missing_polls + 1 if current == "missing" else 0
            if current != "running":
                stuck_since = last = None
                if missing_polls >= 2:
                    restart_times[:] = [t for t in restart_times if now - t < 3600]
                    if len(restart_times) >= MAX_PER_HOUR:
                        log(f"container gone but rate limit hit ({len(restart_times)}/hr) — NOT recreating")
                    else:
                        restart_times.append(now)
                        recreate()
                        missing_polls = 0
                        time.sleep(COOLDOWN_SEC)
                continue
            sample = scrape()
        except Exception as error:  # docker hiccup: not a wedge
            log(f"check failed ({type(error).__name__}: {error}); skipping")
            stuck_since = last = None
            continue
        if sample is None:  # still loading, or metrics briefly unavailable
            stuck_since = last = None
            continue
        in_flight, counters = sample
        progressed = last is not None and any(new > old for new, old in zip(counters, last))
        last = counters
        if in_flight >= 1 and not progressed:
            if stuck_since is None:
                stuck_since = now
                log(f"zero progress with running={in_flight:.0f} (counters={counters}) — arming")
            elapsed = now - stuck_since
            if elapsed < STUCK_SEC:
                continue
            restart_times[:] = [t for t in restart_times if now - t < 3600]
            if len(restart_times) >= MAX_PER_HOUR:
                log(f"WEDGE confirmed ({elapsed:.0f}s) but rate limit hit "
                    f"({len(restart_times)} restarts/hr) — NOT restarting")
                stuck_since = now
                continue
            log(f"WEDGE confirmed: running={in_flight:.0f}, zero engine progress for {elapsed:.0f}s")
            restart_times.append(now)
            restart()
            stuck_since = last = None
            time.sleep(COOLDOWN_SEC)
        elif stuck_since is not None:
            log(f"progress resumed after {now - stuck_since:.0f}s — disarmed")
            stuck_since = None


if __name__ == "__main__":
    main()
