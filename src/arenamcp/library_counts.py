"""Public library counts with an explicit distinction between zero and unknown."""

from typing import Any

LIBRARY_COUNT_SOURCES = frozenset({"bridge_total_card_count", "log_zone_membership"})


def observed_library_count(state: dict[str, Any]) -> int | None:
    """Reject legacy default-zero snapshots without evidence of an empty zone.

    Positive legacy counts remain useful. Zero requires an authoritative source
    because older snapshots used zero for missing/hidden library observations.
    """
    zones = state.get("zones") if isinstance(state.get("zones"), dict) else {}
    count = zones.get("library_count", state.get("library_count"))
    source = zones.get("library_count_source", state.get("library_count_source"))
    if type(count) is not int or count < 0 or source == "unknown":
        return None
    if count == 0 and source not in LIBRARY_COUNT_SOURCES:
        return None
    return count
