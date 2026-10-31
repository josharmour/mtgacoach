"""BasicLandTypes SelectN uses protocol ids, not card or SubType ids.

Verified against local client BasicLandType.cs and SelectBasicLandWorkflow's
ColorOptionsForRequest / ColorsSelected: Plains=1 through Forest=5, with
StaticSubset restricting that list. SelectBasicLandSubtypeWorkflow is a
different workflow and must not use this mapping.

Reports 20261031_151016 / 152006 rejected arbitrary [0] and card id 75556.
The user's [3] in match 19c9600a, request 97/state 67, made Passage a Swamp.
"""

from __future__ import annotations

from typing import Any

from arenamcp.color_choice import CARD_COLORS, COLOR_LETTERS, enum_short

BASIC_LAND_TYPES = {1: "Plains", 2: "Island", 3: "Swamp", 4: "Mountain", 5: "Forest"}


def basic_land_ids(poll: dict[str, Any]) -> list[int] | None:
    """None means another domain; [] means recognized but unavailable choices.

    Empty/invalid StaticSubset never expands to all types. Static ignores
    Ids, matching the client's ColorOptionsForRequest.
    """
    payload = poll.get("request_payload") or {}
    context = poll.get("decision_context") or {}
    payload = payload if isinstance(payload, dict) else {}
    context = context if isinstance(context, dict) else {}
    list_kind = enum_short(
        poll.get("select_n_list_type") or payload.get("listType") or context.get("list_type")
    )
    static_kind = enum_short(
        poll.get("select_n_static_list") or payload.get("staticList") or context.get("static_list")
    )
    if static_kind != "BasicLandTypes":
        return None
    if list_kind == "Static":
        return list(BASIC_LAND_TYPES)
    if list_kind != "StaticSubset":
        return []
    ids = poll.get("select_n_ids")
    if ids is None:
        ids = payload.get("ids", context.get("option_ids", []))
    if not isinstance(ids, (list, tuple)):
        return []
    offered = set()
    for value in ids or []:
        if type(value) is int or (isinstance(value, str) and value.isdecimal()):
            offered.add(int(value))
    return [value for value in BASIC_LAND_TYPES if value in offered]


def basic_land_options(ids: list[int]) -> list[dict[str, Any]]:
    return [
        {
            "id": value,
            "name": BASIC_LAND_TYPES[value],
            "color": CARD_COLORS[value],
            "symbol": COLOR_LETTERS[CARD_COLORS[value]],
        }
        for value in ids
    ]


def basic_land_ids_for_names(names: list[str], offered: list[int]) -> list[int]:
    """Land names or UI colour names/symbols, only within this domain."""
    aliases = {}
    for option in basic_land_options(offered):
        for key in (option["name"], option["color"], option["symbol"]):
            aliases[key.casefold()] = option["id"]
    selected = []
    for name in names:
        value = aliases.get(name.strip().casefold())
        if value is None or value in selected:
            return []
        selected.append(value)
    return selected


def is_basic_land_choice(decision: Any) -> bool:
    options = getattr(decision, "options", ())
    return bool(options) and all(o.meta.get("choice") == "basic_land_type" for o in options)
