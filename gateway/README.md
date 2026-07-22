# gateway/ — LiteLLM gateway for api.mtgacoach.com

This is the **live LLM gateway** (since 2026-07). All customer traffic goes:

```
client (sk- key) → api.mtgacoach.com → Cloudflare tunnel → 10.0.0.100:8444 (plex)
                 → LiteLLM (container `litellm`) → vLLM / Ollama backends
```

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
`["deepseek-v4-flash", "gemma-4-12b-it"]`. Patreon signup mints these
automatically — see `website/` (the mtgacoach.com FastAPI app).

The old hand-rolled proxy (`website/`, container `mtgacoach` on :8443) is
**website + Patreon auth only** — its `mc_` license keys are dead against
this gateway.
