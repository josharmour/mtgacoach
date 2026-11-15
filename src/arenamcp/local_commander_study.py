"""Source-bound prerequisites for a local command-zone deployment hypothesis."""

import re
from collections import Counter

from arenamcp.deck_strategy import oracle_rules

_ACTIONS = ("play", "hold", "avoid")
_SYMBOLS = re.compile(r"(?:\{(?:[WUBRGC]|[0-9]+)\})+")
_SUBJECT = r"(another|a|an) (creature|artifact|land|enchantment|permanent)"
_CAST_SAC = re.compile(r"^as an additional cost to cast this spell, sacrifice " + _SUBJECT + r"[.]$", re.I)
_ACT_SAC = re.compile(r"^(?:(\{T\}),\s*)?sacrifice " + _SUBJECT + r"$", re.I)
_SACRIFICE = re.compile(r"(?:^|:\s*|[.]\s*)(?:you may\s+)?sacrifice " + _SUBJECT + r"\b", re.I)
_ACTIVATION_RIDER = re.compile(
    r"\bactivat(?:e|ed|ing)\b[^.\n]{0,80}\b(?:only|once|twice|unless|if|during|before|after|no more than)\b"
    r"|\b(?:can.t|cannot|may not) activate\b|\b(?:costs?|cost) [^.\n]+ to activate\b",
    re.I,
)
_ACTIVATION_ADVICE = re.compile(r"\b(?:taps?|tapping|activate|activation|loyalty)\b|[+−-]\d", re.I)
_SAC_WORD = re.compile(r"\b(?:sacrifice|sacrificing|sacrificed|fodder|fling)\b", re.I)
_CAST_CONSTRAINT = re.compile(
    r"additional cost to cast|(?:this spell|it) costs [^.]*more to cast|(?:can.t cast|cast only|can cast [^.]*only)",
    re.I,
)


def _costs(card, gid):
    resources, mandatory, unsupported_cast, unsupported_activation, tapped = [], [], [], [], []
    loyalty, activation_rows, riders = [], [], []
    types = card["type_line"].partition("—")[0].lower().split()
    for n, line in enumerate(oracle_rules(card), 1):
        prefix, colon, _ = line.partition(":")
        matches = []
        if _ACTIVATION_RIDER.search(line):
            riders.append(n)
        if colon and prefix != "Printed characteristics":
            activation_rows.append(n)
        if found := _CAST_SAC.fullmatch(line.strip()):
            mandatory.append(n)
            matches.append((*found.groups(), True))
        elif _CAST_CONSTRAINT.search(line):
            unsupported_cast.append(n)
        elif colon and re.search(r"\bsacrifice\b", prefix, re.I):
            found = _ACT_SAC.fullmatch(prefix.strip())
            if not found:
                unsupported_activation.append(n)
                continue
            tap, determiner, kind = found.groups()
            if tap:
                tapped.append(n)
            matches.append((determiner, kind, False))
        elif colon and prefix.strip() == "{T}":
            tapped.append(n)
            matches.extend((*m.groups(), False) for m in _SACRIFICE.finditer(line))
        elif colon and prefix != "Printed characteristics":
            # A colon separates an activation cost from its effect. Other-face
            # annotations and granted/conditional costs may be more complex;
            # abstain on those rather than treating the ability as free.
            label = prefix.strip().replace("−", "-")
            if "planeswalker" in types and re.fullmatch(r"(?:\[[+-]?\d+\]|[+-]\d+|0)", label):
                loyalty.append({"rule": n, "change": int(label.strip("[]"))})
                matches.extend((*m.groups(), False) for m in _SACRIFICE.finditer(line))
            else:
                unsupported_activation.append(n)
        else:
            matches.extend((*m.groups(), False) for m in _SACRIFICE.finditer(line))
        for determiner, kind, in_command in matches:
            kind = kind.lower()
            if not in_command and determiner.lower() != "another" and (kind == "permanent" or kind in types):
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
    return {
        "independent_battlefield_resources": resources,
        "mandatory_cast_rules": mandatory,
        "unsupported_cast_rules": unsupported_cast,
        # A standalone printed rider can govern a different paragraph. There
        # is no verified per-ability ownership for it in the numbered text.
        "activation_permission_rules": riders,
        "unsupported_activation_rules": sorted(
            set(unsupported_activation) | (set(activation_rows) if riders else set())
        ),
        "tap_source_rules": tapped,
        "loyalty_rules": loyalty,
    }


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
        **_costs(card, gid),
    }


def commander_conditions(row, context):
    policy = context["commander_deployment"]
    gid = context["card"]
    actions = row.get("action_rules")
    if not isinstance(actions, dict) or set(actions) != set(_ACTIONS):
        raise ValueError("Commander study requires separate printed references for play, hold and avoid")
    cited = {(r["card"], r["rule"]) for r in row["evidence"]}
    base = policy["base_payment"]
    if base is None or policy["unsupported_cast_rules"]:
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
        if policy["activation_permission_rules"] and _ACTIVATION_ADVICE.search(row[action]["advice"]):
            raise ValueError("The commander study cannot certify its printed activation permission")
        if any((gid, n) in keys for n in policy["unsupported_activation_rules"]):
            raise ValueError("The commander study cannot certify this printed activation cost")
        item = {}
        loyalty = [r for r in policy["loyalty_rules"] if (gid, r["rule"]) in keys]
        if loyalty:
            item["loyalty_source"] = {
                "card": gid,
                "zone": "battlefield",
                "controller": "self",
                "timing": "own_main_empty_stack",
                "activation_available": "verify_current_game",
                "alternatives": loyalty,
            }
        taps = [n for n in policy["tap_source_rules"] if (gid, n) in keys]
        if taps:
            item["activation_source"] = {
                "card": gid,
                "rules": taps,
                "zone": "battlefield",
                "controller": "self",
                "must_be_untapped": True,
                "tap_legality": "verify_current_game",
            }
        if zone == "command" and action in ("play", "hold"):
            if any((gid, n) not in keys for n in policy["mandatory_cast_rules"]):
                raise ValueError("Commander deployment must cite its mandatory additional casting cost")
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
    if source := item.get("loyalty_source"):
        for rule in source["alternatives"]:
            needed = max(0, -rule["change"])
            lines.append(
                f"Loyalty rule {rule['rule']} • on your main phase with an empty stack, verify the permanent has an activation available and at least {needed} loyalty."
            )
    if item.get("activation_source"):
        lines.append(
            "To activate the cited ability, the commander must be under your control on the battlefield, untapped and legally able to tap."
        )
    for resource in item.get("independent_battlefield_resources", []):
        lines.append(
            f"To use printed rule {resource['rule']} • another {resource['kind']} you control must be present on the battlefield."
        )
    return lines
