"""Reviewed, source-bound Limited roles. These records have no power or win-rate fields."""

import copy
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

_CORPUS_SHA256 = {
    "blb": "c783f588ef30b9017a828298aec0c72d066ee376b437e7049fb4ec74dd3a2e2f",
    "trk": "7f6e9f6ba35029eba28db8bb08e9a6cb0c1148ac5daa7b8e0da0233bcee01b98",
}
SUPPORTED_SETS = frozenset(_CORPUS_SHA256)


def reviewed_event_set(event_name, set_code):
    """A pack's inferred printing set can supply ratings; roles need the named event too."""
    if not isinstance(event_name, str):
        return ""
    # Only recognized native retail event names bind this set's archetypes.
    # Mixed pools, Alchemy and unrelated formats may contain the same print.
    match = re.fullmatch(
        r"(?:PremierDraft|QuickDraft|Trad(?:itional)?_?(?:Draft|Sealed)|Contender(?:Draft|Sealed)|Draft|Sealed)"
        r"_([A-Z0-9]{3})(?:_[0-9]{8})?",
        event_name,
    )
    if not match:
        return ""
    declared = match.group(1)
    return declared if declared.lower() in SUPPORTED_SETS and declared == str(set_code).upper() else ""


@lru_cache(maxsize=2)
def _records(set_code) -> dict:
    if set_code not in SUPPORTED_SETS:
        return {}
    try:
        raw = Path(__file__).with_name(f"limited_{set_code}.json").read_bytes()
    except OSError:
        return {}
    if hashlib.sha256(raw).hexdigest() != _CORPUS_SHA256[set_code]:
        return {}
    return json.loads(raw)["prints"]


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
    return {
        "set_code": set_code,
        **{
            key: copy.deepcopy(row[key])
            for key in ("roles", "removal", "archetypes", "note", "interaction")
            if key in row
        },
    }


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
    if role:
        return {**card, "limited_role": role}
    if "limited_role" in card:
        return {key: value for key, value in card.items() if key != "limited_role"}
    return card


def rebind_attached_role(card):
    """Rebuild saved/advisor data from the current print; never trust a serialized claim."""
    if "limited_role" not in card:
        return card
    prior = card["limited_role"]
    return with_role(card, prior.get("set_code") if isinstance(prior, dict) else None)


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
