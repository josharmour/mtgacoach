"""Bounded legend-rule projections (CR 704.5j), never a casting-legality ban.

Entry effects still happen. The same controller keeps one same-named legendary
permanent before future combat. Which copy is best is a tactical choice: these
projections use a stable material/readiness estimate, not an Arena selection.
"""

from __future__ import annotations

import re


def card_of(obj: dict) -> dict:
    return obj.get("_card") or obj


def name_of(obj: dict) -> str:
    card = card_of(obj)
    return str(card.get("modified_name") or card.get("name") or "").casefold()


def controller(obj: dict):
    card = card_of(obj)
    return card.get("controller_seat_id") or card.get("owner_seat_id")


def text(obj: dict) -> str:
    return (
        re.sub(r"<[^>]*>", "", str(card_of(obj).get("oracle_text") or ""))
        .lower()
        .replace("’", "'")
        .replace("“", '"')
        .replace("”", '"')
    )


def legendary(obj: dict) -> bool:
    card = card_of(obj)
    # Honor explicit live/copy characteristics when available.
    if isinstance(card.get("is_legendary"), bool):
        return card["is_legendary"]
    for field in ("super_types", "supertypes"):
        if field in card and isinstance(card[field], list):
            return any(str(t).lower().removeprefix("supertype_") == "legendary" for t in card[field])
    return bool(re.search(r"\blegendary\b", str(card.get("type_line") or "").lower()))


def token(obj: dict) -> bool:
    card = card_of(obj)
    return bool(
        obj.get("is_token") or card.get("is_token") or "token" in str(card.get("object_kind") or "").lower()
    )


def exempt(obj: dict, battlefield) -> bool:
    """Unconditional, Oracle-stated exceptions, scoped to their controller/token.

    Conditional exceptions are not guessed. Nonlegendary copies are handled by
    ``legendary`` using their current characteristics rather than their name.
    """
    for source in battlefield:
        if card_of(source).get("is_phased_out"):
            continue
        for line in re.split(r"(?<=\.)\s+|\n", text(source)):
            line = line.strip()
            match = re.fullmatch(
                r'(?:the )?["\']?legend rule["\']? (?:doesn\'t|does not) apply(?: to (permanents|creatures|tokens) you control)?\.',
                line,
            )
            if match:
                kind = match[1]
                if kind is None:
                    return True
                if controller(source) != controller(obj) or controller(obj) is None:
                    continue
                if kind == "tokens" and not token(obj):
                    continue
                if kind == "creatures" and "creature" not in str(card_of(obj).get("type_line") or "").lower():
                    continue
                return True
    return False


def conflicts(obj: dict, battlefield) -> list[dict]:
    board = list(battlefield)
    name = name_of(obj)
    seat = controller(obj)
    if (
        not name
        or seat is None
        or not legendary(obj)
        or card_of(obj).get("is_phased_out")
        or exempt(obj, board)
    ):
        return []
    return [
        other
        for other in board
        if other is not obj
        and other.get("instance_id") != obj.get("instance_id")
        and controller(other) == seat
        and legendary(other)
        and name_of(other) == name
        and not card_of(other).get("is_phased_out")
        and not exempt(other, board)
    ]


def _keep_value(obj: dict) -> tuple:
    card = card_of(obj)
    p = obj.get("power") if isinstance(obj.get("power"), int) else 0
    t = obj.get("toughness") if isinstance(obj.get("toughness"), int) else 0
    damage = card.get("damage") if isinstance(card.get("damage"), int) else 0
    disabled = obj.get("_can_attack") is False and obj.get("_can_block") is False
    ready = not obj.get("_sick") and not obj.get("_tapped", card.get("is_tapped", False))
    return (not disabled, p + max(0, t - damage), ready, -damage)


def resolve_legends(objects, *, battlefield=(), prefer_ids=()) -> list[dict]:
    """Return surviving objects; input dictionaries remain untouched.

    ``battlefield`` supplies noncreature static exception sources only; projected
    creatures replace their original snapshots so a dead exception source cannot
    keep protecting later duplicates. ``prefer_ids`` preserves an existing body
    on otherwise equal choices (including its counters/sickness/attachments).
    """
    objects = list(objects)
    groups: dict[tuple, list] = {}
    for obj in objects:
        card = card_of(obj)
        name, seat = name_of(obj), controller(obj)
        if name and seat is not None and legendary(obj) and not card.get("is_phased_out"):
            groups.setdefault((seat, name), []).append(obj)
    duplicates = [group for group in groups.values() if len(group) > 1]
    if not duplicates:
        return objects
    static = [
        obj for obj in battlefield if "creature" not in str(card_of(obj).get("type_line") or "").lower()
    ]
    board = static + objects
    preferred = set(prefer_ids)
    removed = set()
    for group in duplicates:
        group = [obj for obj in group if not exempt(obj, board)]
        if len(group) < 2:
            continue
        keep = max(group, key=lambda b: (_keep_value(b), b.get("instance_id") in preferred))
        removed.update(id(other) for other in group if other is not keep)
    return [obj for obj in objects if id(obj) not in removed]


def cast_note(state: dict, card: dict) -> str:
    local = state.get("local_seat_id") or next(
        (p.get("seat_id") for p in state.get("players") or [] if p.get("is_local")), None
    )
    incoming = dict(card, controller_seat_id=local)
    same = conflicts(incoming, state.get("battlefield") or [])
    if not same:
        return ""
    ids = ", ".join(str(c.get("instance_id")) for c in same)
    return (
        f" [LEGEND RULE: {card.get('name')} already controlled ({ids}); this cast does NOT add a lasting second body. "
        "Keep one copy; compare current stats, damage, counters, attachments and readiness. "
        "Cast only for a concrete entry/cast/death payoff or useful replacement; price the lost copy and mana.]"
    )


def keep_note(state: dict, decision) -> str:
    """Ground Arena's legend choice in raw instance facts, not the projected survivor."""
    import json

    prompt = str(getattr(decision, "prompt_text", "") or "").lower()
    if (
        getattr(decision, "request_type", "") != "SelectN"
        or "legendary" not in prompt
        or "keep" not in prompt
    ):
        return ""
    board = {str(c.get("instance_id")): c for c in state.get("battlefield") or []}
    choices = []
    for option in decision.options:
        iid = str(option.option_id).removeprefix("sel:")
        card = board.get(iid)
        if card is None:
            continue
        facts = {
            key: card[key]
            for key in (
                "name",
                "instance_id",
                "power",
                "toughness",
                "modified_power",
                "modified_toughness",
                "damage",
                "counters",
                "is_tapped",
                "summoning_sickness",
                "turn_entered_battlefield",
                "oracle_text",
            )
            if key in card
        }
        facts["option_id"] = option.option_id
        facts["attachments"] = [
            c.get("name") or c.get("instance_id")
            for c in board.values()
            if c.get("attached_to_id") == card.get("instance_id")
        ]
        choices.append(facts)
    return (
        "LEGEND RULE CHOICE: keep exactly one offered instance. Compare its damage, counters, "
        "attachments, tapped state and summoning sickness; identical names do not mean identical usefulness. "
        "The other copy goes to the graveyard (not a sacrifice or destruction). CURRENT INSTANCES: "
        + json.dumps(choices, ensure_ascii=False)
    )
