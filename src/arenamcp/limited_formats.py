"""Which 17lands expansion rates a limited event (stdlib only: on the decision path).

Alchemy drafts: Arena names the event "PremierDraft_YFRA_20261013"; 17lands files
it as "Y26FRA" (Alchemy year + base set). Its win rates stay hidden until a card
has ~500 games, so paper cards borrow the base set's numbers until then
(draftstats.merge_alchemy_rows).
"""

from __future__ import annotations

import logging
import re
import time

logger = logging.getLogger(__name__)

_ALCHEMY_TOKEN = re.compile(r"^Y([A-Z][A-Z0-9]{2})$")
_EVENT_DATE = re.compile(r"^(20\d\d)\d{4}$")
# Base set -> 17lands expansion of the limited event being played.
_event_expansions: dict[str, str] = {}


def alchemy_expansion(event_name: str) -> tuple[str, str] | None:
    """(base set, 17lands expansion) for an Alchemy event name, e.g. ("FRA", "Y26FRA")."""
    parts = str(event_name or "").split("_")
    base = next((m.group(1) for part in parts if (m := _ALCHEMY_TOKEN.match(part))), "")
    if not base:
        return None
    year = next((m.group(1) for part in parts if (m := _EVENT_DATE.match(part))), "")
    yy = (year or time.strftime("%Y"))[2:]
    return base, f"Y{yy}{base}"


def use_event_expansion(event_name: str) -> None:
    """Point a base set's ratings at the 17lands format of this event (Alchemy or not)."""
    alchemy = alchemy_expansion(event_name)
    if alchemy is not None:
        base, expansion = alchemy
        if _event_expansions.get(base) != expansion:
            logger.info(f"17lands ratings for {base} come from {expansion} ({event_name})")
        _event_expansions[base] = expansion
        return
    for part in str(event_name or "").split("_"):
        if _event_expansions.pop(part.upper(), None):
            logger.info(f"17lands ratings for {part.upper()} back to the base set ({event_name})")


def ratings_expansion(set_code: str) -> str:
    """The 17lands expansion that rates ``set_code`` for the current event."""
    code = str(set_code or "").upper()
    return _event_expansions.get(code, code)
