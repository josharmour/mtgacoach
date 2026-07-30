#!/usr/bin/env python3
"""
litellm-exporter — queries litellm's PostgreSQL for per-user token metrics
and exposes them as Prometheus metrics on :8012 for scraping.

Run: python3 exporter.py  (or docker container)
Depends on: psycopg2-binary, prometheus_client
"""

import os
import time
from collections import defaultdict
from contextlib import suppress

from prometheus_client import Counter, Gauge, start_http_server

# ── Config from env ─────────────────────────────────────────────────────
DB_HOST = os.getenv("DB_HOST", "10.0.0.100")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_USER = os.getenv("DB_USER", "litellm")
DB_PASS = os.getenv("DB_PASS", "")
DB_NAME = os.getenv("DB_NAME", "litellm")
EXPORTER_PORT = int(os.getenv("EXPORTER_PORT", "8012"))
SCRAPE_INTERVAL = int(os.getenv("SCRAPE_INTERVAL", "30"))  # seconds

# Aliases used by internal/system traffic — not real users.
NOISE_ALIASES = frozenset({"None...", "no-key-requi...", "ignored...", "litellm_prox..."})

# ── Prometheus metrics ──────────────────────────────────────────────────

# Per-user gauge — total prompt tokens today (resets daily)
litellm_prompt_tokens = Gauge(
    "litellm_prompt_tokens",
    "Total prompt tokens processed per user (today)",
    ["key_alias", "model", "api_key_hash"],
)

# Per-user gauge — total completion tokens today
litellm_completion_tokens = Gauge(
    "litellm_completion_tokens",
    "Total completion tokens generated per user (today)",
    ["key_alias", "model", "api_key_hash"],
)

# Per-user gauge — total (prompt+completion) tokens today
litellm_total_tokens = Gauge(
    "litellm_total_tokens", "Total tokens processed per user (today)", ["key_alias", "model", "api_key_hash"]
)

# Per-user gauge — request count today
litellm_requests_total = Gauge(
    "litellm_requests_total", "Total API requests per user (today)", ["key_alias", "api_key_hash"]
)

# Per-user gauge — successful requests today
litellm_requests_successful = Gauge(
    "litellm_requests_successful", "Successful API requests per user (today)", ["key_alias", "api_key_hash"]
)

# Per-user gauge — failed requests today
litellm_requests_failed = Gauge(
    "litellm_requests_failed", "Failed API requests per user (today)", ["key_alias", "api_key_hash"]
)

# Per-key alias — model access info
litellm_model_access = Gauge(
    "litellm_model_access",
    "Model access per key alias (1 = has access, 0 = no access)",
    ["key_alias", "model"],
)

# Collector health — one bad key/row must never kill the whole scrape.
litellm_exporter_errors_total = Counter(
    "litellm_exporter_errors_total",
    "Internal errors while collecting litellm metrics, by stage "
    "(scrape, gauge_reset, row, model_access, key_tokens, key_requests)",
    ["stage"],
)

# Gauges whose label sets are rebuilt from scratch every scrape cycle.
RESETTABLE_GAUGES = (
    litellm_prompt_tokens,
    litellm_completion_tokens,
    litellm_total_tokens,
    litellm_requests_total,
    litellm_requests_successful,
    litellm_requests_failed,
)


def get_today_str():
    """Return today's date in litellm's format (YYYY-MM-DD)."""
    return time.strftime("%Y-%m-%d")


def connect_db():
    """Open a new autocommit connection to the litellm Postgres DB.

    psycopg2 is imported lazily so this module stays importable (for tests)
    on machines that only have prometheus_client installed.
    """
    import psycopg2

    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASS, dbname=DB_NAME)
    conn.autocommit = True
    return conn


def reset_gauge(gauge):
    """Remove every labelled child from a Gauge.

    Iterates a *snapshot* of the internal label dict. The previous code did
    ``for label_set in gauge._metrics.keys(): gauge.remove(*label_set)`` —
    remove() deletes from that same dict, so the loop died with
    ``RuntimeError: dictionary changed size during iteration`` on every
    scrape after the first, leaving all token gauges permanently stale.
    """
    for label_set in list(gauge._metrics.keys()):
        try:
            gauge.remove(*label_set)
        except Exception:
            litellm_exporter_errors_total.labels(stage="gauge_reset").inc()


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
        (today,),
    )
    rows = cursor.fetchall()
    cursor.close()

    # Aggregate per api_key + model
    per_key_model = defaultdict(
        lambda: {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "requests": 0,
            "successful": 0,
            "failed": 0,
            "models": set(),
        }
    )

    for row in rows:
        try:
            api_key, model, model_group, pt, ct, reqs, succ, fail = row
            alias = key_aliases.get(api_key, api_key[:12] + "...")
            d = per_key_model[(api_key, alias, model)]
            d["prompt_tokens"] += pt or 0
            d["completion_tokens"] += ct or 0
            d["total_tokens"] += (pt or 0) + (ct or 0)
            d["requests"] += reqs or 0
            d["successful"] += succ or 0
            d["failed"] += fail or 0
            d["models"].add(model)
        except Exception:
            # One malformed row must not kill the scrape.
            litellm_exporter_errors_total.labels(stage="row").inc()

    return per_key_model


def update_metrics(conn, key_aliases):
    """Query the DB and update all Prometheus metrics."""

    # 1) Key alias → model access info
    cursor = conn.cursor()
    cursor.execute('SELECT "token", "models" FROM "LiteLLM_VerificationToken"')
    access_rows = cursor.fetchall()
    cursor.close()

    for token, models in access_rows:
        try:
            alias = key_aliases.get(token, token[:12] + "...")
            if models:
                for m in models:
                    litellm_model_access.labels(key_alias=alias, model=m).set(1)
        except Exception:
            litellm_exporter_errors_total.labels(stage="model_access").inc()

    # 2) Today's per-user token data
    per_key_model = fetch_today_user_data(conn, key_aliases)

    # Reset all gauges to 0 first (so unused aliases don't show stale data)
    for gauge in RESETTABLE_GAUGES:
        reset_gauge(gauge)

    # Set current values
    for (api_key, alias, model), data in per_key_model.items():
        # Skip noise entries
        if not alias or alias in NOISE_ALIASES:
            continue
        if not model or model == "":
            continue
        try:
            key_hash = api_key[:16] + "..."
            litellm_prompt_tokens.labels(key_alias=alias, model=model, api_key_hash=key_hash).set(
                data["prompt_tokens"]
            )
            litellm_completion_tokens.labels(key_alias=alias, model=model, api_key_hash=key_hash).set(
                data["completion_tokens"]
            )
            litellm_total_tokens.labels(key_alias=alias, model=model, api_key_hash=key_hash).set(
                data["total_tokens"]
            )
        except Exception:
            litellm_exporter_errors_total.labels(stage="key_tokens").inc()

    # Aggregate per-key for request counts
    per_key_agg = defaultdict(lambda: {"requests": 0, "successful": 0, "failed": 0, "alias": "", "hash": ""})
    for (api_key, alias, model), data in per_key_model.items():
        if not alias or alias in NOISE_ALIASES:
            continue
        pk = api_key
        per_key_agg[pk]["requests"] += data["requests"]
        per_key_agg[pk]["successful"] += data["successful"]
        per_key_agg[pk]["failed"] += data["failed"]
        per_key_agg[pk]["alias"] = alias
        per_key_agg[pk]["hash"] = api_key[:16] + "..."

    for api_key, data in per_key_agg.items():
        try:
            litellm_requests_total.labels(key_alias=data["alias"], api_key_hash=data["hash"]).set(
                data["requests"]
            )
            litellm_requests_successful.labels(key_alias=data["alias"], api_key_hash=data["hash"]).set(
                data["successful"]
            )
            litellm_requests_failed.labels(key_alias=data["alias"], api_key_hash=data["hash"]).set(
                data["failed"]
            )
        except Exception:
            litellm_exporter_errors_total.labels(stage="key_requests").inc()


def main():
    print(f"litellm-exporter starting on :{EXPORTER_PORT}")
    print(f"  DB: {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}")
    print(f"  Scrape interval: {SCRAPE_INTERVAL}s")

    # Start Prometheus HTTP server
    start_http_server(EXPORTER_PORT)
    print(f"  HTTP server started on :{EXPORTER_PORT}")

    conn = None
    while True:
        try:
            # (Re)connect if needed
            if conn is None or conn.closed:
                conn = connect_db()

            key_aliases = fetch_key_aliases(conn)
            print(f"  Fetched {len(key_aliases)} key aliases for {get_today_str()}")

            update_metrics(conn, key_aliases)
            print("  Metrics updated successfully")

        except Exception as e:
            print(f"  Error: {e}")
            with suppress(Exception):
                litellm_exporter_errors_total.labels(stage="scrape").inc()
            # Drop the connection so the next cycle reconnects cleanly —
            # a failed query can leave the session unusable.
            if conn is not None:
                with suppress(Exception):
                    conn.close()
                conn = None

        time.sleep(SCRAPE_INTERVAL)


if __name__ == "__main__":
    main()
