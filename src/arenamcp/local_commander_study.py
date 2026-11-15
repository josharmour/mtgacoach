"""Source-bound prerequisites for a local command-zone deployment hypothesis."""

import re
from collections import Counter

from arenamcp.deck_strategy import oracle_rules

_ACTIONS = ("play", "hold", "avoid")
_SYMBOLS = re.compile(r"(?:\{(?:[WUBRGC]|[0-9]+)\})+")
# Only unambiguous instructions to the controller are classified. Other
# costs/choices remain for semantic review, including additional mana costs.
_SACRIFICE = re.compile(
    r"(?:^|:\s*|[.]\s*|as an additional cost to cast this spell,\s*)"
    r"(?:you may\s+)?sacrifice (another|a|an) (creature|artifact|land|enchantment|permanent)\b",
    re.I,
)
_SAC_WORD = re.compile(r"\b(?:sacrifice|sacrificing|sacrificed|fodder|fling)\b", re.I)


def _separate_resources(card, gid):
    resources = []
    for n, line in enumerate(oracle_rules(card), 1):
        for match in _SACRIFICE.finditer(line):
            determiner, kind = (part.lower() for part in match.groups())
            types = card["type_line"].partition("—")[0].lower().split()
            if determiner != "another" and (kind == "permanent" or kind in types):
                continue
            resources.append(
                {
                    "card": gid,
                    "rule": n,
                    "kind": kind,
                    "zone": "battlefield",
                    "controller": "self",
                    "minimum": 1,
                    "exclude_source_instance": True,
                }
            )
    return resources


def deployment_context(card, gid):
    cost = card.get("mana_cost") or ""
    printed = None
    if _SYMBOLS.fullmatch(cost):
        symbols = re.findall(r"\{([^}]+)\}", cost)
        printed = dict(Counter(sym for sym in symbols if not sym.isdigit()))
        printed["generic"] = sum(int(sym) for sym in symbols if sym.isdigit())
    return {
        "printed_cost": cost,
        "base_payment": printed,
        "command_zone": "play must deploy the designated commander from command; hold may wait there or hold an already-deployed permanent on the battlefield",
        "timing": "Give an ordinary deployment line. Assume no acceleration or discount. State the printed base payment and allow the current commander tax in addition; the current tax is unknown outside a game.",
        "required_references": "Supply action_rules.play, hold and avoid with the numeric rules used by EACH action. Also include them in evidence. Cite any printed sacrifice you discuss. A piece in the deck is not a creature available to sacrifice.",
        "independent_battlefield_resources": _separate_resources(card, gid),
    }


def commander_conditions(row, context):
    policy = context["commander_deployment"]
    gid = context["card"]
    actions = row.get("action_rules")
    if not isinstance(actions, dict) or set(actions) != set(_ACTIONS):
        raise ValueError("Commander study requires separate printed references for play, hold and avoid")
    cited = {(r["card"], r["rule"]) for r in row["evidence"]}
    base = policy["base_payment"]
    if base is None:
        raise ValueError("The commander study cannot certify an ordinary payment for this printed cost")
    resources = policy["independent_battlefield_resources"]
    certified = {}
    for action in _ACTIONS:
        conditions = row[action]["conditions"]
        ids = {p["card"]: p["zone"] for p in conditions["pieces"]}
        refs = actions[action]
        if not isinstance(refs, list) or not refs:
            raise ValueError("Each commander action must cite its own printed evidence")
        keys = set()
        for ref in refs:
            if (
                type(ref) is not dict
                or set(ref) != {"card", "rule"}
                or any(type(v) is not int for v in ref.values())
            ):
                raise ValueError("Commander action needs numeric printed references")
            k = ref["card"], ref["rule"]
            if k not in cited or k[0] not in ids or k in keys:
                raise ValueError("Commander action refers to unsupported or duplicate evidence")
            keys.add(k)
        if not any(card == gid for card, _ in keys):
            raise ValueError("Commander action must cite its focal commander")
        zone = ids.get(gid)
        if (action == "play" and zone != "command") or (
            action == "hold" and zone not in ("command", "battlefield")
        ):
            raise ValueError("Normal commander deployment and holding must account for the command zone")
        item = {}
        if zone == "command" and action in ("play", "hold"):
            mana = conditions["mana"]
            value = sum(base.values())
            if (
                any(mana.get(k, 0) < n for k, n in base.items() if k != "generic")
                or sum(mana.values()) < value
            ):
                raise ValueError("Commander deployment lacks its printed base payment")
            if conditions["first_turn"] < max(1, value):
                raise ValueError("The ordinary command-zone line assumes unsupported acceleration")
            item["command_zone_payment"] = {
                "card": gid,
                "printed_cost": policy["printed_cost"],
                "generic_per_prior_command_cast": 2,
                "prior_casts": "read_from_current_game",
            }
        required = [r for r in resources if (r["card"], r["rule"]) in keys]
        if resources and _SAC_WORD.search(row[action]["advice"]) and not required:
            raise ValueError("Commander sacrifice advice lacks its printed resource requirement")
        if required:
            item["independent_battlefield_resources"] = required
        certified[action] = item
    return certified


def explain_conditions(item):
    lines = []
    if payment := item.get("command_zone_payment"):
        lines.append(
            f"Command-zone deployment • printed {payment['printed_cost']}, plus {{2}} for each prior cast "
            "of this commander from the command zone. Check the current tax and available mana."
        )
    for resource in item.get("independent_battlefield_resources", []):
        lines.append(
            f"To use printed rule {resource['rule']} • another {resource['kind']} you control must be present on the battlefield."
        )
    return lines
