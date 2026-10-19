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
  `prompt_tokens_total` or `generation_tokens_total` for 120 s (a 1M-token prefill advances the iteration
  count every 2048-token chunk) -> `docker restart`; 420 s cooldown (load takes ~2m45s); max 3/hour.
- The container runs with `--rm`, so a crash that EXITS deletes it and no restart policy can be set
  (`docker update` refuses). When the container is gone for two polls the watchdog runs
  `/usr/local/lib/glm53-watchdog/recreate-glm53-flash.sh` (root-only, 0700: it carries `VLLM_API_KEY`),
  generated from `docker inspect` of the running container (same image digest, env, mounts, GPUs,
  ulimits) but with `--restart unless-stopped`. A stopped-but-present container is left alone.
- Logs: `journalctl -u glm53-wedge-watchdog -f`.

Trial keys: `website/abuse_protection.py` TRIAL_LIMITS `max_parallel_requests` 3 -> 2 (2026-10-19; the coach's
own hedge runs 2 calls at once, so 1 would 429 legitimate trials). The 50 live trial keys were updated via
`/key/update`; new trial keys get 2 only after the website is redeployed.
