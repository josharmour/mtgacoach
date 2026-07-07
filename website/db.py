"""SQLite database for subscriber management."""

import sqlite3
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

DB_PATH = Path("./data/mtgacoach.db")


def _ensure_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_db():
    _ensure_db()
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Create tables if they don't exist."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS subscribers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                license_key TEXT UNIQUE NOT NULL,
                email TEXT,
                name TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                created_at REAL NOT NULL,
                expires_at REAL,
                notes TEXT,
                assigned_model TEXT
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                priority TEXT DEFAULT 'normal',
                target TEXT DEFAULT 'all',
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                license_key TEXT NOT NULL,
                model TEXT,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                provider TEXT,
                timestamp REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS client_installs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                license_key TEXT NOT NULL,
                install_id TEXT NOT NULL,
                client_version TEXT,
                frontend TEXT,
                user_agent TEXT,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                last_ip TEXT,
                UNIQUE(license_key, install_id)
            );

            CREATE TABLE IF NOT EXISTS proxy_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_subscribers_key ON subscribers(license_key);
        """)

        # Idempotent migration for the assigned_model column on existing DBs
        # that were created before it was part of the schema.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(subscribers)").fetchall()]
        if "assigned_model" not in cols:
            conn.execute("ALTER TABLE subscribers ADD COLUMN assigned_model TEXT")

        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_usage_key ON usage_log(license_key);
            CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_log(timestamp);

            CREATE TABLE IF NOT EXISTS eval_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target TEXT NOT NULL,
                ts REAL NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_eval_target_ts ON eval_results(target, ts DESC);
            CREATE INDEX IF NOT EXISTS idx_client_installs_key ON client_installs(license_key);
            CREATE INDEX IF NOT EXISTS idx_client_installs_last_seen ON client_installs(last_seen);
        """)


# --- Proxy config (provider overrides + default_model) ---

def get_config_value(key: str) -> Optional[str]:
    """Read a proxy_config entry. Returns None if absent."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM proxy_config WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else None


def set_config_value(key: str, value: str) -> None:
    """Upsert a proxy_config entry."""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO proxy_config (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, time.time()),
        )


def generate_license_key() -> str:
    """Generate a new license key."""
    return f"mc_{secrets.token_urlsafe(32)}"


def create_subscriber(
    email: str = "",
    name: str = "",
    days: int = 30,
    notes: str = "",
) -> dict:
    """Create a new subscriber with a license key."""
    key = generate_license_key()
    now = time.time()
    expires = now + (days * 86400) if days > 0 else None

    with get_db() as conn:
        conn.execute(
            "INSERT INTO subscribers (license_key, email, name, status, created_at, expires_at, notes) "
            "VALUES (?, ?, ?, 'active', ?, ?, ?)",
            (key, email, name, now, expires, notes),
        )
    return {"license_key": key, "email": email, "expires_at": expires}


def check_license(key: str) -> Optional[dict]:
    """Check a license key and return subscriber info."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM subscribers WHERE license_key = ?", (key,)
        ).fetchone()

    if not row:
        return None

    result = dict(row)

    # Check expiration
    if result["status"] == "active" and result.get("expires_at"):
        if time.time() > result["expires_at"]:
            result["status"] = "expired"
            with get_db() as conn:
                conn.execute(
                    "UPDATE subscribers SET status = 'expired' WHERE license_key = ?",
                    (key,),
                )
    return result


def list_subscribers() -> list[dict]:
    """List all subscribers."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM subscribers ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def update_subscriber(key: str, **kwargs) -> bool:
    """Update subscriber fields."""
    allowed = {"email", "name", "status", "expires_at", "notes", "assigned_model"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return False

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [key]

    with get_db() as conn:
        cursor = conn.execute(
            f"UPDATE subscribers SET {set_clause} WHERE license_key = ?",
            values,
        )
    return cursor.rowcount > 0


def revoke_subscriber(key: str) -> bool:
    """Revoke a subscriber's license."""
    return update_subscriber(key, status="revoked")


def extend_subscriber(key: str, days: int) -> bool:
    """Extend a subscriber's expiration by N days."""
    sub = check_license(key)
    if not sub:
        return False
    current_exp = sub.get("expires_at") or time.time()
    new_exp = max(current_exp, time.time()) + (days * 86400)
    return update_subscriber(key, expires_at=new_exp, status="active")


def delete_subscriber(key: str) -> bool:
    """Delete a subscriber entirely."""
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM subscribers WHERE license_key = ?", (key,))
    return cursor.rowcount > 0


# --- Messages ---

def create_message(title: str, body: str, priority: str = "normal", target: str = "all") -> int:
    """Create a service message for subscribers."""
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO messages (title, body, priority, target, created_at) VALUES (?, ?, ?, ?, ?)",
            (title, body, priority, target, time.time()),
        )
    return cursor.lastrowid


def list_messages(limit: int = 50) -> list[dict]:
    """List recent messages."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM messages ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def delete_message(msg_id: int) -> bool:
    """Delete a message."""
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
    return cursor.rowcount > 0


def get_messages_after(after_id: int = 0) -> list[dict]:
    """Get messages created after a given ID."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE id > ? ORDER BY id ASC", (after_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# --- Usage tracking ---

def log_usage(license_key: str, model: str, prompt_tokens: int, completion_tokens: int, provider: str):
    """Log API usage for a subscriber."""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO usage_log (license_key, model, prompt_tokens, completion_tokens, provider, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (license_key, model, prompt_tokens, completion_tokens, provider, time.time()),
        )


def upsert_client_install(
    license_key: str,
    install_id: str,
    client_version: str = "",
    frontend: str = "",
    user_agent: str = "",
    last_ip: str = "",
):
    """Record or update client install telemetry for a subscriber."""
    install_id = (install_id or "").strip()
    if not install_id:
        return

    now = time.time()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO client_installs (
                license_key, install_id, client_version, frontend, user_agent,
                first_seen, last_seen, last_ip
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(license_key, install_id) DO UPDATE SET
                client_version = excluded.client_version,
                frontend = excluded.frontend,
                user_agent = excluded.user_agent,
                last_seen = excluded.last_seen,
                last_ip = excluded.last_ip
            """,
            (
                license_key,
                install_id,
                client_version,
                frontend,
                user_agent,
                now,
                now,
                last_ip,
            ),
        )


def get_client_summary(days: int = 30) -> dict[str, dict]:
    """Return recent install telemetry rolled up by subscriber."""
    since = time.time() - (days * 86400)
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT license_key, install_id, client_version, frontend, user_agent, first_seen, last_seen, last_ip
            FROM client_installs
            WHERE last_seen > ?
            ORDER BY last_seen DESC
            """,
            (since,),
        ).fetchall()

    summary: dict[str, dict] = {}
    for row in rows:
        item = dict(row)
        license_key = item["license_key"]
        entry = summary.get(license_key)
        if entry is None:
            entry = {
                "installs_30d": 0,
                "frontends_30d": set(),
                "latest_frontend": item.get("frontend") or "",
                "latest_version": item.get("client_version") or "",
                "latest_install_id": item.get("install_id") or "",
                "last_seen_at": item.get("last_seen") or 0,
                "last_seen_ip": item.get("last_ip") or "",
                "latest_user_agent": item.get("user_agent") or "",
            }
            summary[license_key] = entry

        entry["installs_30d"] += 1
        frontend = (item.get("frontend") or "").strip()
        if frontend:
            entry["frontends_30d"].add(frontend)

    for entry in summary.values():
        entry["frontends_30d"] = ", ".join(sorted(entry["frontends_30d"]))
    return summary


def get_usage_summary(license_key: str, days: int = 30) -> dict:
    """Get usage summary for a subscriber."""
    since = time.time() - (days * 86400)
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as requests, "
            "COALESCE(SUM(prompt_tokens), 0) as total_prompt, "
            "COALESCE(SUM(completion_tokens), 0) as total_completion "
            "FROM usage_log WHERE license_key = ? AND timestamp > ?",
            (license_key, since),
        ).fetchone()
    return dict(row) if row else {"requests": 0, "total_prompt": 0, "total_completion": 0}


def get_all_usage_summary(days: int = 30) -> list[dict]:
    """Get usage summary grouped by subscriber."""
    since = time.time() - (days * 86400)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT license_key, COUNT(*) as requests, "
            "COALESCE(SUM(prompt_tokens), 0) as total_prompt, "
            "COALESCE(SUM(completion_tokens), 0) as total_completion "
            "FROM usage_log WHERE timestamp > ? "
            "GROUP BY license_key ORDER BY requests DESC",
            (since,),
        ).fetchall()
    return [dict(r) for r in rows]


def insert_eval_result(target: str, payload: dict) -> int:
    """Persist an eval-results upload. ``ts`` taken from payload if present,
    otherwise wall-clock now. Returns the inserted row id.
    """
    import json as _json
    ts = float(payload.get("ts") or time.time())
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO eval_results (target, ts, payload_json) VALUES (?, ?, ?)",
            (target, ts, _json.dumps(payload)),
        )
        return int(cur.lastrowid)


def get_eval_results_history(target: str, limit: int = 30) -> list[dict]:
    """Return the last N full payloads for a target, oldest first.

    Used by the trend-over-time chart. Each row's stored payload is parsed
    and augmented with ``id`` and ``ts`` from the row metadata.
    """
    import json as _json
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, ts, payload_json FROM eval_results "
            "WHERE target = ? ORDER BY ts DESC LIMIT ?",
            (target, int(limit)),
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        payload = _json.loads(r["payload_json"])
        payload["id"] = int(r["id"])
        payload["ts"] = float(r["ts"])
        out.append(payload)
    out.reverse()  # oldest first for charts
    return out


def get_latest_eval_results(targets: list[str] | None = None) -> dict:
    """Return the latest payload per target, plus per-set latest and history.

    Shape:
        {
            target: {
                "latest": {payload dict, with 'id' and 'ts'},
                "by_set": {
                    "<SET_CODE>": {payload dict},   # latest run for that set
                    ...
                    "_unset": {payload dict},       # most recent untagged run
                },
                "history": [{"id", "ts"}, ...]   (recent N for trend lines)
            },
            ...
        }

    ``by_set`` lets the dashboard compare sets side-by-side without each
    set's data point clobbering the others on the trend chart.
    """
    import json as _json
    with get_db() as conn:
        if targets:
            placeholders = ",".join("?" * len(targets))
            rows = conn.execute(
                f"SELECT id, target, ts, payload_json FROM eval_results "
                f"WHERE target IN ({placeholders}) "
                f"ORDER BY target ASC, ts DESC",
                targets,
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, target, ts, payload_json FROM eval_results "
                "ORDER BY target ASC, ts DESC"
            ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        slot = out.setdefault(r["target"], {"latest": None, "by_set": {}, "history": []})
        payload_loaded = None
        if slot["latest"] is None:
            payload_loaded = _json.loads(r["payload_json"])
            payload_loaded["id"] = int(r["id"])
            payload_loaded["ts"] = float(r["ts"])
            slot["latest"] = payload_loaded
        if len(slot["history"]) < 30:
            slot["history"].append({"id": int(r["id"]), "ts": float(r["ts"])})
        # First (newest) row per (target, set_code) wins.
        if payload_loaded is None:
            payload_loaded = _json.loads(r["payload_json"])
            payload_loaded["id"] = int(r["id"])
            payload_loaded["ts"] = float(r["ts"])
        set_code = (payload_loaded.get("set_code") or "_unset").strip().upper()
        if set_code and set_code not in slot["by_set"]:
            slot["by_set"][set_code] = payload_loaded
    return out


def get_activity_series(days: int = 7, bucket_seconds: int = 3600) -> dict:
    """Return time-bucketed activity for the admin dashboard.

    Buckets are aligned to ``bucket_seconds`` boundaries (Unix epoch %
    bucket_seconds == 0). Empty buckets in the range are returned as zeros so
    the chart shows continuous time, not just buckets that had traffic.

    Returns:
        {
            "bucket_seconds": int,
            "buckets": [
                {
                    "ts": float,            # bucket start (epoch seconds)
                    "requests": int,
                    "prompt_tokens": int,
                    "completion_tokens": int,
                    "by_provider": {provider: requests, ...},
                },
                ...
            ],
        }
    """
    if bucket_seconds <= 0:
        bucket_seconds = 3600
    now = time.time()
    since = now - (days * 86400)
    # Snap "since" down to bucket boundary so the leftmost bucket aligns.
    start = int(since // bucket_seconds) * bucket_seconds
    end = int(now // bucket_seconds) * bucket_seconds + bucket_seconds

    with get_db() as conn:
        rows = conn.execute(
            "SELECT "
            "  CAST(timestamp / ? AS INTEGER) * ? AS bucket, "
            "  provider, "
            "  COUNT(*) AS requests, "
            "  COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
            "  COALESCE(SUM(completion_tokens), 0) AS completion_tokens "
            "FROM usage_log "
            "WHERE timestamp >= ? AND timestamp < ? "
            "GROUP BY bucket, provider "
            "ORDER BY bucket ASC",
            (bucket_seconds, bucket_seconds, start, end),
        ).fetchall()

    # Aggregate (bucket, provider) -> bucket-level dict.
    bucket_map: dict[int, dict] = {}
    for r in rows:
        bts = int(r["bucket"])
        slot = bucket_map.setdefault(
            bts,
            {
                "ts": float(bts),
                "requests": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "by_provider": {},
            },
        )
        slot["requests"] += int(r["requests"])
        slot["prompt_tokens"] += int(r["prompt_tokens"])
        slot["completion_tokens"] += int(r["completion_tokens"])
        provider = r["provider"] or "unknown"
        slot["by_provider"][provider] = (
            slot["by_provider"].get(provider, 0) + int(r["requests"])
        )

    # Fill empty buckets with zeros across [start, end).
    buckets: list[dict] = []
    bts = start
    while bts < end:
        if bts in bucket_map:
            buckets.append(bucket_map[bts])
        else:
            buckets.append(
                {
                    "ts": float(bts),
                    "requests": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "by_provider": {},
                }
            )
        bts += bucket_seconds

    return {"bucket_seconds": bucket_seconds, "buckets": buckets}
