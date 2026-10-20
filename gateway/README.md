# gateway/ — LiteLLM gateway for api.mtgacoach.com

This is the **live LLM gateway** (since 2026-07). All customer traffic goes:

```
client (sk- key) → api.mtgacoach.com → Cloudflare tunnel → 10.0.0.100:8444 (plex)
                 → LiteLLM (container `litellm`) → vLLM / Ollama backends
```

> **Identity note (2026-08-03):** "plex" (`10.0.0.100`) is the **same machine**
> as blackwell (`10.0.0.10`) — one box, hostname `blackwell`, both IPs on one
> NIC. Instructions saying "on plex" run locally on blackwell.

- `config.yaml` — tracked copy of the live config at
  `/home/joshu/docker-stack/litellm/config.yaml` on plex (`10.0.0.100`). Edit here, then
  copy to host and restart (below). No secrets — env refs only.
- `docker-compose.yml` — tracked copy of the live stack definition at
  `/home/joshu/docker-stack/litellm/docker-compose.yml`
  (LiteLLM `main-stable` + Postgres 16 for key storage).

Secrets live in `/volume1/docker/appdata/litellm/.env` on the NAS only:
`LITELLM_MASTER_KEY`, `POSTGRES_PASSWORD`, `AZURE_OPENAI_BASE_URL`,
`AZURE_OPENAI_API_KEY`.

## Ops

```bash
# Deploy a config change
scp gateway/config.yaml joshu@10.0.0.2:/volume1/docker/appdata/litellm/config.yaml
ssh joshu@10.0.0.2 "cd /volume1/docker/appdata/litellm && /usr/local/bin/docker-compose up -d --force-recreate litellm"
# NOTE: plain `docker restart` does NOT re-read .env — always use compose up --force-recreate
# after env edits. LiteLLM runs ~60s of prisma migrations at boot; transient DB
# ConnectErrors during that window are normal.

# Health check
curl -H "Authorization: Bearer <any-valid-key>" -H "User-Agent: mtgacoach/1.0" \
  https://api.mtgacoach.com/v1/models
# ALWAYS send a User-Agent — Cloudflare 403s the default Python-urllib UA.

# Master key (needed for /key/* admin API)
ssh joshu@10.0.0.2 "/usr/local/bin/docker exec litellm printenv LITELLM_MASTER_KEY"

# Admin UI
# https://api.mtgacoach.com/ui  (login: admin + master key)
```

## Key management

Customer keys are LiteLLM virtual keys (`sk-...`), minted via
`POST /key/generate` (Bearer master key), scoped to
`["glm-5.3-flash"]`. Patreon signup mints these
automatically — see `website/` (the mtgacoach.com FastAPI app).

The old hand-rolled proxy (`website/`, container `mtgacoach` on :8443) is
**website + Patreon auth only** — its `mc_` license keys are dead against
this gateway.

## Current customer model

Both MTGA Coach and ArenaOnAir use **GLM 5.3** (`glm-5.3-flash`). Patreon and
Coach trial keys are scoped only to that model. Existing customer key grants
were migrated without changing their budgets, expiry, or key values. Older
internal aliases may remain for other services; they are not offered in either
app or exposed by customer model discovery.

ArenaOnAir's separate five-match trial is served by `website/arenaonair.py` at
`https://mtgacoach.com/api/arenaonair`. It keeps the match counter in the website
SQLite database and forwards bounded requests to LiteLLM. Gateway admin credentials
stay server-side; the desktop only receives its own trial access. Coach's existing
seven-day trial is unchanged. See `website/ARENAONAIR.md` for limits and deployment.

## vLLM wedge watchdog (GLM-5.3-Flash container)

2026-10-19 09:33:58: the TP1 worker of `glm53-flash-2x-rtxpro6000` hit `Triton Error [CUDA]: out of memory`
loading a freshly compiled b12x attention kernel and TP0 waited forever (4 requests "running", 0 tok/s) —
every customer was down until a manual restart. Each GPU runs at ~95/95 GiB by design (88.07 GiB weights +
a 4.25 GiB KV cache = 1,061,441 tokens, exactly one 1M-token request for Hermes), so there is no headroom
to give back without shrinking the 1M context.

- `vllm-watchdog/glm53_wedge_watchdog.py` + `glm53-wedge-watchdog.service` — installed on blackwell as
  `/usr/local/lib/glm53-watchdog/` + `/etc/systemd/system/glm53-wedge-watchdog.service` (enabled).
  A wedge = `num_requests_running >= 1` with no movement in `iteration_tokens_total_count`,
  `prompt_tokens_total`, `generation_tokens_total` or `kv_cache_usage_perc` for 120 s. The counters only
  move once a prefill completes, so until 2026-10-20 a long uncached Hermes prompt (>~440K new tokens)
  looked wedged: 10-19 19:51 the watchdog killed a healthy ~720K-token prefill at ~90%. KV usage climbs
  through a prefill and is flat in a real hang -> save `docker logs` to `/var/log/glm53-watchdog/wedge-*.log`, then
  `docker stop`; 420 s cooldown (load takes ~2m); max 3/hour.
- One supervisor (2026-10-20): the container is the foreground `docker run --rm` of the user unit
  `glm53-2x-rtxpro6000.service` (`Restart=always`, `~/glm53-spark/serve.sh`), so a stop removes it and
  systemd starts a fresh one with its readiness checks. The old `docker restart` detached the container's
  output from the unit journal, and `--rm` deleted its log at the next crash: the 2026-10-20 12:41 OOM
  left no stack trace. When the container is gone for two polls, `recreate-glm53-flash.sh` restarts the
  unit (`systemctl --user -M joshu@`). It used to `docker run` a frozen copy of the Oct 19 config outside
  systemd; that copy is kept root-only in `/root/glm53-watchdog-backup-20261020/` (it carries
  `VLLM_API_KEY`). A stopped-but-present container is left alone.
- Logs: `journalctl -u glm53-wedge-watchdog -f`. vLLM output also goes to the system journal through
  Docker's journald driver and survives any restart: `sudo journalctl -t glm53-vllm`.

### GPU memory headroom (2026-10-20)

The four unplanned failures between Oct 17 and Oct 20 (12:41 PDT: 2 min down) were CUDA OOMs or hangs
with the failing GPU at <= 20 MiB free under ordinary load. Fixed allocations used 95,929 of 97,250 MiB
per GPU, PyTorch's cache kept its peak, and the non-PyTorch allocations (Triton/CuTe kernel loads,
cuBLAS, NCCL) then had nothing left. `serve.sh` now passes:

- `VLLM_GLM53_MTP_DRAFT_HEAD=bf16` — vLLM's default. The glm53-flash profile's `nvfp4` kept a separate
  170 MiB-per-GPU draft vocabulary head; bf16 shares the target head (verifier unchanged, ~3-5% decode
  cost estimated).
- `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True,large_segment_size_mb:12,per_process_memory_fraction:0.985`
  — caps PyTorch's pool at 95,791 MiB per GPU, leaving ~150-300 MiB at the driver for non-PyTorch
  allocations. Explicit container env overrides the profile (`/opt/lil/runtime/launcher.py`).
- `--log-driver journald --log-opt tag=glm53-vllm`.

The 1M context is unchanged (KV still 1,061,441 tokens, 1.01x). After the restart each GPU had 921 MiB
free (316 MiB before). Healthy = `DCGM_FI_DEV_FB_FREE` levels off at >= ~150 MiB instead of decaying to
2 MiB. If PyTorch hits the cap (`exceed per_process_memory_fraction` / `OutOfMemoryError` in the log),
lower `MAX_NUM_BATCHED_TOKENS` 2048 -> 1536. Rollback: `cp ~/glm53-spark/serve.sh.bak-pre-memfix-20261020
~/glm53-spark/serve.sh && systemctl --user restart glm53-2x-rtxpro6000.service`.

Keep evals and `prompt_logprobs` runs off this vLLM: on 2026-10-17 a host-side script with the backend
key sent ~1,400 requests straight to the container, bypassing LiteLLM, and a `prompt_logprobs` request
crashed it.

Trial keys: `website/abuse_protection.py` TRIAL_LIMITS `max_parallel_requests` 3 -> 2 (2026-10-19; the coach's
own hedge runs 2 calls at once, so 1 would 429 legitimate trials). The 50 live trial keys were updated via
`/key/update`; new trial keys get 2 only after the website is redeployed.
