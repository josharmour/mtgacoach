#!/usr/bin/env python3
"""
litellm-exporter — queries litellm's PostgreSQL for per-user token metrics
and exposes them as Prometheus metrics on :8012 for scraping.

Run: python3 litellm-exporter.py  (or docker container)
Depends on: psycopg2-binary, prometheus_client
"""
import os
import time
from collections import defaultdict
from prometheus_client import start_http_server, Gauge, Counter
import psycopg2

# ── Config from env ─────────────────────────────────────────────────────
DB_HOST = os.getenv("DB_HOST", "10.0.0.100")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_USER = os.getenv("DB_USER", "litellm")
DB_PASS = os.getenv("DB_PASS", "")
DB_NAME = os.getenv("DB_NAME", "litellm")
EXPORTER_PORT = int(os.getenv("EXPORTER_PORT", "8012"))
SCRAPE_INTERVAL = int(os.getenv("SCRAPE_INTERVAL", "30"))  # seconds

# ── Prometheus metrics ──────────────────────────────────────────────────

# Per-user gauge — total prompt tokens today (resets daily)
litellm_prompt_tokens = Gauge(
    "litellm_prompt_tokens",
    "Total prompt tokens processed per user (today)",
    ["key_alias", "model", "api_key_hash"]
)

# Per-user gauge — total completion tokens today
litellm_completion_tokens = Gauge(
    "litellm_completion_tokens",
    "Total completion tokens generated per user (today)",
    ["key_alias", "model", "api_key_hash"]
)

# Per-user gauge — total (prompt+completion) tokens today
litellm_total_tokens = Gauge(
    "litellm_total_tokens",
    "Total tokens processed per user (today)",
    ["key_alias", "model", "api_key_hash"]
)

# Per-user gauge — request count today
litellm_requests_total = Gauge(
    "litellm_requests_total",
    "Total API requests per user (today)",
    ["key_alias", "api_key_hash"]
)

# Per-user gauge — successful requests today
litellm_requests_successful = Gauge(
    "litellm_requests_successful",
    "Successful API requests per user (today)",
    ["key_alias", "api_key_hash"]
)

# Per-user gauge — failed requests today
litellm_requests_failed = Gauge(
    "litellm_requests_failed",
    "Failed API requests per user (today)",
    ["key_alias", "api_key_hash"]
)

# Per-key alias — model access info
litellm_model_access = Gauge(
    "litellm_model_access",
    "Model access per key alias (1 = has access, 0 = no access)",
    ["key_alias", "model"]
)


def get_today_str():
    """Return today's date in litellm's format (YYYY-MM-DD)."""
    return time.strftime("%Y-%m-%d")


def fetch_key_aliases(conn):
    """Fetch key alias → api_key_hash mapping from VerificationToken table."""
    cursor = conn.cursor()
    cursor.execute('SELECT "token", "key_alias" FROM "LiteLLM_VerificationToken"')
    rows = cursor.fetchall()
    cursor.close()
    # Return dict of token_hash -> key_alias
    return {row[0]: row[1] if row[1] else row[0][:12] + "..." for row in rows}


def fetch_today_user_data(conn, key_aliases):
    """
    Query LiteLLM_DailyUserSpend for today's data,
    grouped by api_key + model.
    """
    today = get_today_str()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT api_key, model, model_group,
                  prompt_tokens, completion_tokens,
                  api_requests, successful_requests, failed_requests
           FROM "LiteLLM_DailyUserSpend"
           WHERE date = %s
             AND (prompt_tokens > 0 OR completion_tokens > 0 OR api_requests > 0)
        """,
        (today,)
    )
    rows = cursor.fetchall()
    cursor.close()

    # Aggregate per api_key + model
    per_key_model = defaultdict(lambda: {
        "prompt_tokens": 0, "completion_tokens": 0,
        "total_tokens": 0, "requests": 0, "successful": 0, "failed": 0,
        "models": set()
    })

    for api_key, model, model_group, pt, ct, reqs, succ, fail in rows:
        key = api_key
        alias = key_aliases.get(api_key, api_key[:12] + "...")
        d = per_key_model[(key, alias, model)]
        d["prompt_tokens"] += (pt or 0)
        d["completion_tokens"] += (ct or 0)
        d["total_tokens"] += (pt or 0) + (ct or 0)
        d["requests"] += (reqs or 0)
        d["successful"] += (succ or 0)
        d["failed"] += (fail or 0)
        d["models"].add(model)

    return per_key_model


def update_metrics(conn, key_aliases):
    """Query the DB and update all Prometheus metrics."""

    # 1) Key alias → model access info
    cursor = conn.cursor()
    cursor.execute(
        'SELECT "token", "models" FROM "LiteLLM_VerificationToken"'
    )
    access_rows = cursor.fetchall()
    cursor.close()

    all_models = set()
    for token, models in access_rows:
        alias = key_aliases.get(token, token[:12] + "...")
        if models:
            for m in models:
                all_models.add(m)
                litellm_model_access.labels(key_alias=alias, model=m).set(1)

    # 2) Today's per-user token data
    per_key_model = fetch_today_user_data(conn, key_aliases)

    # Reset all gauges to 0 first (so unused aliases don't show stale data)
    for label_set in litellm_prompt_tokens._metrics.keys():
        litellm_prompt_tokens.remove(*label_set)
    for label_set in litellm_completion_tokens._metrics.keys():
        litellm_completion_tokens.remove(*label_set)
    for label_set in litellm_total_tokens._metrics.keys():
        litellm_total_tokens.remove(*label_set)
    for label_set in litellm_requests_total._metrics.keys():
        litellm_requests_total.remove(*label_set)
    for label_set in litellm_requests_successful._metrics.keys():
        litellm_requests_successful.remove(*label_set)
    for label_set in litellm_requests_failed._metrics.keys():
        litellm_requests_failed.remove(*label_set)

    # Set current values
    for (api_key, alias, model), data in per_key_model.items():
        # Skip noise entries
        if not alias or alias in ["None...", "no-key-requi...", "ignored...", "litellm_prox..."]:
            continue
        if not model or model == "":
            continue
        key_hash = api_key[:16] + "..."
        litellm_prompt_tokens.labels(key_alias=alias, model=model, api_key_hash=key_hash).set(data["prompt_tokens"])
        litellm_completion_tokens.labels(key_alias=alias, model=model, api_key_hash=key_hash).set(data["completion_tokens"])
        litellm_total_tokens.labels(key_alias=alias, model=model, api_key_hash=key_hash).set(data["total_tokens"])

    # Aggregate per-key for request counts
    per_key_agg = defaultdict(lambda: {"requests": 0, "successful": 0, "failed": 0, "alias": "", "hash": ""})
    for (api_key, alias, model), data in per_key_model.items():
        if not alias or alias in ["None...", "no-key-requi...", "ignored...", "litellm_prox..."]:
            continue
        pk = api_key
        per_key_agg[pk]["requests"] += data["requests"]
        per_key_agg[pk]["successful"] += data["successful"]
        per_key_agg[pk]["failed"] += data["failed"]
        per_key_agg[pk]["alias"] = alias
        per_key_agg[pk]["hash"] = api_key[:16] + "..."

    for api_key, data in per_key_agg.items():
        litellm_requests_total.labels(key_alias=data["alias"], api_key_hash=data["hash"]).set(data["requests"])
        litellm_requests_successful.labels(key_alias=data["alias"], api_key_hash=data["hash"]).set(data["successful"])
        litellm_requests_failed.labels(key_alias=data["alias"], api_key_hash=data["hash"]).set(data["failed"])


def main():
    print(f"litellm-exporter starting on :{EXPORTER_PORT}")
    print(f"  DB: {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}")
    print(f"  Scrape interval: {SCRAPE_INTERVAL}s")

    # Start Prometheus HTTP server
    start_http_server(EXPORTER_PORT)
    print(f"  HTTP server started on :{EXPORTER_PORT}")

    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT,
        user=DB_USER, password=DB_PASS,
        dbname=DB_NAME
    )
    conn.autocommit = True

    while True:
        try:
            # Reconnect if needed
            if conn.closed:
                conn = psycopg2.connect(
                    host=DB_HOST, port=DB_PORT,
                    user=DB_USER, password=DB_PASS,
                    dbname=DB_NAME
                )
                conn.autocommit = True

            key_aliases = fetch_key_aliases(conn)
            print(f"  Fetched {len(key_aliases)} key aliases for {get_today_str()}")

            update_metrics(conn, key_aliases)
            print(f"  Metrics updated successfully")

        except Exception as e:
            print(f"  Error: {e}")

        time.sleep(SCRAPE_INTERVAL)


if __name__ == "__main__":
    main()
