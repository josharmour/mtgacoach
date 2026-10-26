"""Persistent, atomic trial issuance limits. Stores digests, never credentials."""
import hashlib
import ipaddress
import time

from fastapi import HTTPException

# Cloudflare tunnel connectors: the NAS, and since 2026-10-22 the blackwell
# cloudflared container, whose host-port hairpin arrives from the
# trash-guides_default bridge gateway. Never trust forwarding headers from
# arbitrary callers, other LAN hosts, or X-Forwarded-For.
TRUSTED_TUNNEL_PEERS = {"10.0.0.2", "172.23.0.1"}
TRIAL_LIMITS = {"rpm_limit": 30, "tpm_limit": 500_000, "max_parallel_requests": 2}
PATRON_LIMITS = {"rpm_limit": 60, "tpm_limit": 1_000_000, "max_parallel_requests": 4}


def _network_hash(address):
    address = ipaddress.ip_address(address)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    network = ipaddress.ip_network(f"{address}/{'64' if address.version == 6 else '32'}", strict=False)
    return hashlib.sha256(str(network).encode()).hexdigest()


def network_identity(request):
    peer = request.client.host if request.client else ""
    try:
        if peer in TRUSTED_TUNNEL_PEERS:
            # Missing/invalid headers fail closed; do not collapse all public
            # signups onto the tunnel's address or accept a client-chosen ID.
            return _network_hash(request.headers.get("cf-connecting-ip", ""))
        return _network_hash(peer)
    except ValueError:
        raise HTTPException(400, "Unable to verify signup network") from None


def request_origin(request):
    """Telemetry view of a caller; never raises, keeps digests only.

    network_hash is what the limits enforce. client_hash digests the
    Cloudflare header even from an untrusted peer, so a tunnel path that
    collapses callers onto one network still shows how many there were.
    """
    peer = request.client.host if request.client else ""
    try:
        network = network_identity(request)
    except HTTPException:
        network = ""
    try:
        client = _network_hash(request.headers.get("cf-connecting-ip", ""))
    except ValueError:
        client = ""
    country = request.headers.get("cf-ipcountry", "").strip().upper()[:2]
    return {"network_hash": network, "client_hash": client, "peer": peer[:64], "country": country}


def _reject(detail, retry):
    raise HTTPException(429, detail, headers={"Retry-After": str(max(1, int(retry)))})


def reserve_signup(get_db, machine_id, network, now=None):
    now = time.time() if now is None else now
    with get_db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS trial_signup_reservations (id INTEGER PRIMARY KEY, machine_id TEXT NOT NULL, network_hash TEXT NOT NULL, created REAL NOT NULL, status TEXT NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS trial_signup_network_time ON trial_signup_reservations(network_hash, created)")
        conn.execute("CREATE INDEX IF NOT EXISTS trial_signup_machine_time ON trial_signup_reservations(machine_id, created)")
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        pending = conn.execute("SELECT created, status FROM trial_signup_reservations WHERE machine_id=? ORDER BY created DESC LIMIT 1", (machine_id,)).fetchone()
        if pending and (pending[1] == "issued" or pending[0] > now - 300):
            _reject("Trial signup is already recorded or in progress. Retry shortly.", 60)
        # Count failed and pending attempts too. Rotating IDs or upstream failure
        # must not grant an unbounded mint/retry loop. No audit rows are deleted.
        # Global signup volume is alert-only in the protection monitor. This
        # loop must never impose a shared growth ceiling across networks.
        for window, limit, per_network in [(3600, 3, True), (86400, 6, True)]:
            where = "created>?" + (" AND network_hash=?" if per_network else "")
            args = (now - window, network) if per_network else (now - window,)
            count, oldest = conn.execute("SELECT count(*), min(created) FROM trial_signup_reservations WHERE " + where, args).fetchone()
            if count >= limit:
                _reject("Trial signup limit reached. Please try later.", oldest + window - now + 1)
        conn.execute("INSERT INTO trial_signup_reservations(machine_id,network_hash,created,status) VALUES(?,?,?,'pending')", (machine_id, network, now))


def finish_signup(get_db, machine_id, success):
    with get_db() as conn:
        conn.execute("UPDATE trial_signup_reservations SET status=? WHERE machine_id=? AND status='pending'", ("issued" if success else "failed", machine_id))


def reserve_code_request(get_db, machine_id, network, now=None):
    """Per-machine and per-network verification-code request limits.

    Mirrors reserve_signup's style: its own reservation table, counted inside
    an immediate transaction, rows never deleted so rotating identities or
    hammering the mailer can't outrun the budget. Limits: 3/hour per
    machine_id, 20/hour per network.
    """
    now = time.time() if now is None else now
    with get_db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS trial_code_reservations (id INTEGER PRIMARY KEY, machine_id TEXT NOT NULL, network_hash TEXT NOT NULL, created REAL NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS trial_code_network_time ON trial_code_reservations(network_hash, created)")
        conn.execute("CREATE INDEX IF NOT EXISTS trial_code_machine_time ON trial_code_reservations(machine_id, created)")
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        for window, limit, column in [(3600, 3, "machine_id"), (3600, 20, "network_hash")]:
            value = machine_id if column == "machine_id" else network
            count, oldest = conn.execute(
                f"SELECT count(*), min(created) FROM trial_code_reservations WHERE {column}=? AND created>?",
                (value, now - window),
            ).fetchone()
            if count >= limit:
                _reject("Verification code limit reached. Please try later.", oldest + window - now + 1)
        conn.execute("INSERT INTO trial_code_reservations(machine_id,network_hash,created) VALUES(?,?,?)", (machine_id, network, now))
