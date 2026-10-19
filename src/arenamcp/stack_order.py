"""The stack's order in every game-state snapshot: index 0 is the top.

Arena's Player.log lists the Stack zone's ``objectInstanceIds`` top-first. In
the 2026-10-19 log every single object put on the stack appeared at index 0
(141 of 141) and every single object that left it left from index 0 (271 of
271); in match 2c67eed8 T6 the stack [311, 309, 307, 306, 304] resolved in
exactly that order (ledger #115). The log snapshot walks the zone's ids
(``GameState.get_objects_in_zone``), so it keeps that order.

The bridges (BepInEx plugin, Mac IL2CPP adapter) serialize
``MtgZone.VisibleCards``, which follows the order in which Arena's merged
GameStateMessage gained its game objects. A new object is appended, so a
bridge stack arrives roughly bottom-first: bug reports with the bridge
connected list [515, 516, 517] with Erode (517) on top, while the log-only
reports list the newest id first. ``MtgZone.CardIds`` copies the zone's
``objectInstanceIds``, and the bridges send it as ``card_ids``.
:func:`order_top_first` puts a bridge stack into the log's order once, where
the snapshot overlay is built (``server._get_bridge_overlay``).

Consumers read :func:`stack_top` for the object that resolves next. To walk
the stack from the top, they iterate ``state["stack"]`` front to back.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def stack_top(state: dict[str, Any] | None) -> dict[str, Any] | None:
    """The object on top of the stack (the one that resolves next), or None when it is empty."""
    stack = (state or {}).get("stack") or []
    return next((item for item in stack if isinstance(item, dict)), None)


def _instance_id(card: Any) -> int:
    try:
        return int(card.get("instance_id") or 0) if isinstance(card, dict) else 0
    except (TypeError, ValueError):
        return 0


def _ids(values: Iterable[Any] | None) -> list[int]:
    found = []
    for value in values or ():
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number:
            found.append(number)
    return found


def order_top_first(
    cards: list[dict[str, Any]],
    *,
    zone_ids: Iterable[Any] | None = None,
    reference_ids: Iterable[Any] | None = None,
) -> list[dict[str, Any]]:
    """``cards`` (a bridge stack in any order) top-first.

    ``zone_ids`` is the zone's own id order (``MtgZone.CardIds``, top-first);
    when it places every card, it decides. Otherwise ``reference_ids`` (the
    log snapshot's stack, top-first) places what it knows, and an object only
    the bridge has is newer than the log's view, so it goes above those,
    highest instance id first: the GRE numbers objects as it creates them.
    That id order is also the whole answer when neither list is given. It is
    wrong only for simultaneous triggers whose controller stacked them out of
    creation order, which the zone's own order gets right. Entries without an
    instance id go to the bottom in their given order.
    """
    known = [card for card in cards if _instance_id(card)]
    unknown = [card for card in cards if not _instance_id(card)]
    zone = {iid: n for n, iid in enumerate(_ids(zone_ids))}
    if zone and all(_instance_id(card) in zone for card in known):
        return sorted(known, key=lambda card: zone[_instance_id(card)]) + unknown
    reference = {iid: n for n, iid in enumerate(_ids(reference_ids))}

    def rank(card: dict[str, Any]) -> tuple[int, int]:
        iid = _instance_id(card)
        return (1, reference[iid]) if iid in reference else (0, -iid)

    return sorted(known, key=rank) + unknown
