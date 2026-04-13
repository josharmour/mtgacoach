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
                notes TEXT
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

            CREATE INDEX IF NOT EXISTS idx_subscribers_key ON subscribers(license_key);
            CREATE INDEX IF NOT EXISTS idx_usage_key ON usage_log(license_key);
            CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_log(timestamp);
            CREATE INDEX IF NOT EXISTS idx_client_installs_key ON client_installs(license_key);
            CREATE INDEX IF NOT EXISTS idx_client_installs_last_seen ON client_installs(last_seen);
        """)


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
    allowed = {"email", "name", "status", "expires_at", "notes"}
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
