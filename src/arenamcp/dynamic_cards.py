"""Runtime name overlay for dynamically-created MTGA grpIds.

MTGA synthesizes grpIds at runtime for in-game objects (copies, modified
cards, conjured cards). These IDs live far above the catalog range
(max ~107k in Raw_CardDatabase; observed dynamic ids 167k-194k in match
9d7d486b) so NO static database — local or Scryfall — can ever name
them. Only the running game client can, via its card-title provider.

This module is the decoupling point: the GRE bridge populates names via
`put_names()` (using the plugin's `resolve_grp_ids` command), and card
lookups consult `get_name()` on a local-DB miss. Consumers register
unresolved ids with `note_unresolved()`; the bridge drains that queue on
its poll loop. No imports in either direction.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_names: dict[int, str] = {}
_pending: set[int] = set()
_asked: set[int] = set()  # ids we've already sent to the bridge (failed or not)

# Catalog grpIds end well below this; only ids above are worth asking the
# client about (catalog misses below the ceiling are DB staleness, not
# dynamic objects — asking the client for those is harmless but noisy).
DYNAMIC_ID_FLOOR = 110_000


def get_name(grp_id: int) -> str | None:
    """Resolved name for a dynamic grpId, if the bridge has supplied one."""
    with _lock:
        return _names.get(int(grp_id))


def note_unresolved(grp_id: int) -> None:
    """Record a grpId that no static database could name."""
    gid = int(grp_id)
    if gid <= 0:
        return
    with _lock:
        if gid in _names or gid in _asked:
            return
        _pending.add(gid)


def take_pending(limit: int = 32) -> list[int]:
    """Drain up to `limit` unresolved ids for a bridge resolve attempt.

    Ids are moved to the asked-set immediately so a failing resolve isn't
    retried every poll; `reset_asked()` clears that on match boundaries
    (a new game state can name instances the previous one couldn't).
    """
    with _lock:
        ids = sorted(_pending)[:limit]
        for gid in ids:
            _pending.discard(gid)
            _asked.add(gid)
        return ids


def put_names(names: dict[int, str]) -> int:
    """Store resolved names from the bridge. Returns how many were new."""
    added = 0
    with _lock:
        for gid, name in (names or {}).items():
            try:
                gid = int(gid)
            except (TypeError, ValueError):
                continue
            name = (name or "").strip()
            if not name:
                continue
            if gid not in _names:
                added += 1
            _names[gid] = name
    return added


def reset_asked() -> None:
    """Allow re-asking previously failed ids (call on match boundaries)."""
    with _lock:
        _asked.clear()


def stats() -> dict[str, int]:
    with _lock:
        return {"resolved": len(_names), "pending": len(_pending), "asked": len(_asked)}
