"""Reviewed, source-bound Limited roles. These records have no power or win-rate fields."""

import hashlib
import json
from functools import lru_cache
from pathlib import Path

SUPPORTED_SETS = frozenset({"trk", "blb"})


@lru_cache(maxsize=2)
def _records(set_code) -> dict:
    try:
        return json.loads(Path(__file__).with_name(f"limited_{set_code}.json").read_text(encoding="utf-8"))[
            "prints"
        ]
    except (OSError, ValueError):
        return {}


def print_role(set_code, grp_id, name, oracle, *, type_line, mana_cost):
    set_code = str(set_code).lower()
    if set_code not in SUPPORTED_SETS or not isinstance(oracle, str) or not oracle:
        return None
    row = _records(set_code).get(str(grp_id))
    if not row or name != row["name"]:
        return None
    if type_line != row["type_line"]:
        return None
    if mana_cost != row["mana_cost"]:
        return None
    if hashlib.sha256(oracle.encode()).hexdigest() != row["rules_sha256"]:
        return None
    return {key: row[key] for key in ("roles", "removal", "archetypes", "note", "interaction") if key in row}


def bound_roles(set_code, ids, lookup):
    code = str(set_code).lower()
    if code not in SUPPORTED_SETS:
        return {}
    prints = _records(code)
    result = {}
    for gid in set(ids):
        if str(gid) not in prints:
            continue
        card = lookup(gid)
        if card is not None:
            role = print_role(
                code, gid, card.name, card.oracle_text, type_line=card.type_line, mana_cost=card.mana_cost
            )
            if role:
                result[gid] = {**role, "name": card.name}
    return result


def with_role(card, set_code):
    role = print_role(
        set_code,
        card.get("grp_id"),
        card.get("name"),
        card.get("oracle_text"),
        type_line=card.get("type_line", ""),
        mana_cost=card.get("mana_cost", ""),
    )
    return {**card, "limited_role": role} if role else card


def partner_reason(role, partners, lane):
    """Only credit a reviewed pair when both actual print records match."""
    if role and lane:
        for entry in role["archetypes"]:
            if set(entry["colors"]) != set(lane):
                continue
            for grp_id in entry["partners"]:
                if grp_id in partners:
                    return f"{entry['theme']} {entry['role']} with {partners[grp_id]}"
    return ""


def role_note(role):
    if role:
        if note := role.get("note"):
            return note
        return ", ".join(f"{ar['colors']} {ar['theme']} {ar['role']}" for ar in role["archetypes"])
    return ""
