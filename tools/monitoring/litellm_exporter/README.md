# litellm-exporter

Prometheus exporter for per-key / per-model **token accounting** from the
LiteLLM gateway (api.mtgacoach.com). Reads LiteLLM's PostgreSQL directly
(the proxy's native `/metrics` returns 404 on `main-stable`) and exposes
gauges on `:8012` for Prometheus to scrape.

## Provenance

This script was previously **not in any repo** — it lived only on the Plex
host. Imported verbatim on 2026-07-29 from:

- Host file: `/home/joshu/litellm-exporter.py` on `10.0.0.100` (Plex)
- md5 of the imported (buggy) version: `92e631261821f124cd934f6aaa471a56`
- Container: `litellm-exporter`
  - Image: `python:3.11-slim` (NB: an earlier task brief claimed
    `lucabecker42/ollama-exporter:latest` — that is stale info; the real
    image is `python:3.11-slim`)
  - Cmd: `sh -c 'pip install -q prometheus_client psycopg2-binary && python3 /app/exporter.py'`
  - Bind mount: `/home/joshu/litellm-exporter.py` → `/app/exporter.py` (ro)
  - Docker network: `litellm_default` (so it can resolve `litellm-db`)
  - Port: `0.0.0.0:8012 -> 8012/tcp`
  - Env: `DB_HOST=litellm-db`, `DB_USER=litellm`, `DB_PASS=<postgres pw>`,
    `DB_NAME=litellm`, `EXPORTER_PORT=8012`

Runtime deps (installed by the container's Cmd): `prometheus_client`,
`psycopg2-binary`. Python 3.10+.

## Data source

- `"LiteLLM_DailyUserSpend"` — pre-aggregated per `date + api_key + model`:
  prompt/completion tokens, api/successful/failed request counts.
- `"LiteLLM_VerificationToken"` — `token` (hash) → `key_alias`, `models`.

## Metrics

| Metric | Labels | Meaning |
|---|---|---|
| `litellm_prompt_tokens` | `key_alias, model, api_key_hash` | prompt tokens today |
| `litellm_completion_tokens` | `key_alias, model, api_key_hash` | completion tokens today |
| `litellm_total_tokens` | `key_alias, model, api_key_hash` | total tokens today |
| `litellm_requests_total` | `key_alias, api_key_hash` | API requests today |
| `litellm_requests_successful` | `key_alias, api_key_hash` | successful requests today |
| `litellm_requests_failed` | `key_alias, api_key_hash` | failed requests today |
| `litellm_model_access` | `key_alias, model` | 1 = key has model access |
| `litellm_exporter_errors_total` | `stage` | internal collector errors (added 2026-07-29) |

All daily gauges reset each scrape cycle (stale label sets are removed first).
Noise aliases (`None...`, `no-key-requi...`, `ignored...`, `litellm_prox...`)
are filtered out.

## Deploy (owner runs this on the Plex host)

```bash
# 1. copy the fixed script over the bind-mounted host file
scp tools/monitoring/litellm_exporter/exporter.py joshu@10.0.0.100:/home/joshu/litellm-exporter.py

# 2. restart so the bind mount picks up the new file
ssh joshu@10.0.0.100 "docker restart litellm-exporter"

# 3. verify
ssh joshu@10.0.0.100 "docker logs litellm-exporter --tail 5"
curl -s localhost:8012/metrics | grep -c '^litellm_total_tokens'
```

Prometheus already scrapes `10.0.0.100:8012` as job `litellm-exporter` — no
Prometheus config change is needed; the ten empty Grafana panels
(uid `vllm-gemma`) fill in on the next successful scrape.
