# litellm-exporter

Prometheus exporter for per-key / per-model **token accounting** from the
LiteLLM gateway (api.mtgacoach.com). Reads LiteLLM's PostgreSQL directly
and exposes
gauges on `:8012` for Prometheus to scrape.

## Runtime

The `litellm-exporter` container uses `python:3.11-slim` and bind-mounts the exporter as `/app/exporter.py`. The [exporter](exporter.py) uses `prometheus_client` and `psycopg2`. Configure database access on the host and provide the scrape port through the exporter environment.

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

## Verify an exporter change

Confirm the active container’s bind mount and scrape target before deploying. After deploying the reviewed script, check its startup logs and scrape `/metrics` on the configured exporter port. Compare a known key alias and model against that day’s spend rows before interpreting a missing series as zero usage.
