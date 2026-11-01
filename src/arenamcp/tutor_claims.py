"""Bound tutor explanations to current numeric picks or an identified search menu.

No choice changes, source-name guesses, database reads or remembered-X reuse.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from arenamcp.mulligan_policy import _mana_value

_TUTOR_LIMIT = re.compile(
    r"^search your (?:library(?: and/or graveyard)?|graveyard) for (?:a |an )?"
    r"(?:(?:green|white|blue|black|red) )?creature card with "
    r"(?:mana value|converted mana cost) x or less and put (?:it|that card) "
    r"(?:onto the battlefield|into your hand)\.",
    re.I,
)
_RETRIEVAL = re.compile(
    r"\b(?:fetch(?:es)?|find(?:s)?|tutor(?:s)?|search(?:es)? for|get(?:s)?|legal (?:tutor )?target)\b", re.I
)
_UNCERTAIN = re.compile(
    r"\b(?:if|unless|would|could|might|assuming|suppose|not|no|never|instead of|rather than|over|cannot|can't|doesn't|don't|won't|later|future|next turn)\b",
    re.I,
)


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _mv(card: dict) -> float | None:
    for key in ("cmc", "mana_value"):
        value = card.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value) if math.isfinite(value) and value >= 0 else None
    cost = str(card.get("mana_cost") or "")
    if not re.fullmatch(r"(?:\{(?:\d+|[WUBRGCXYZ]|[WUBRG2]/[WUBRGP])\})+", cost, re.I):
        return None
    return float(_mana_value(cost))


def _known_cards(state: dict, decision: Any) -> list[dict]:
    catalog = state.get("deck_catalog") or {}
    cards = list(catalog.values()) if isinstance(catalog, dict) else []
    cards += [
        c
        for zone in ("hand", "stack", "battlefield", "graveyard", "command", "exile")
        for c in state.get(zone) or []
    ]
    cards += [(o.meta or {}).get("card") for o in decision.options]
    return [c for c in cards if isinstance(c, dict) and c.get("name")]


def _numeric_limit(state: dict, decision: Any, chosen: list[str]) -> tuple[int, str] | None:
    if decision.request_type != "CastingTimeOptions" or len(chosen) != 1:
        return None
    options = list(decision.options)
    if not options or any((o.meta or {}).get("choiceKind") != "numeric_input" for o in options):
        return None
    groups = {(_integer(o.meta.get("sourceId")), _integer(o.meta.get("childIndex"))) for o in options}
    if len(groups) != 1 or decision.min_select != 1 or decision.max_select != 1:
        return None
    source_id, child = next(iter(groups))
    if not source_id or child is None:
        return None
    explicit_source = getattr(decision, "source_instance_id", 0)
    if explicit_source and explicit_source != source_id:
        return None
    selected = [o for o in options if o.option_id == chosen[0]]
    if len(selected) != 1:
        return None
    value = _integer(selected[0].meta.get("numericValue"))
    if value is None or not re.fullmatch(rf"X\s*=\s*{value}", selected[0].label, re.I):
        return None
    sources = [
        c for c in state.get("stack") or [] if isinstance(c, dict) and c.get("instance_id") == source_id
    ]
    if len(sources) != 1:
        return None
    source = sources[0]
    catalog = state.get("deck_catalog") or {}
    reference = (
        (catalog.get(source.get("grp_id")) or catalog.get(str(source.get("grp_id"))) or {})
        if isinstance(catalog, dict)
        else {}
    )
    # A current instance identifies the spell; catalog data supplies missing
    # printed rules only, never a guessed association from reasoning or names.
    rules = source.get("oracle_text") or reference.get("oracle_text") or ""
    rules = " ".join(re.sub(r"<[^>]*>", "", str(rules)).split())
    if not _TUTOR_LIMIT.search(rules):
        return None
    return value, str(source.get("name") or reference.get("name") or "This tutor")


def correct_tutor_claims(text: str, state: dict, decision: Any, chosen: list[str]) -> tuple[str, list[str]]:
    """Remove proved impossible named retrieval claims, preserving legal picks.

    Numeric choice: only known MV > the exact chosen X is rejected; unknown
    card facts are not treated as failures. Search: only a fully identified
    offered menu establishes which names are available, with no inferred X.
    """
    limit = _numeric_limit(state, decision, chosen)
    offered = None
    if decision.request_type == "Search" and len(chosen) == 1:
        options = list(decision.options)
        if (
            options
            and any(o.option_id == chosen[0] for o in options)
            and all(
                (o.meta or {}).get("identity_known") is True
                and isinstance((o.meta or {}).get("card"), dict)
                and o.meta["card"].get("name")
                for o in options
            )
        ):
            offered = {o.meta["card"]["name"].casefold() for o in options}
    if limit is None and offered is None:
        return text, []
    groups: dict[str, list[dict]] = {}
    for card in _known_cards(state, decision):
        groups.setdefault(str(card["name"]).casefold(), []).append(card)
    first_words = Counter(name.split()[0].rstrip(",") for name in groups)
    patterns = {}
    impossible = {}
    for name, cards in groups.items():
        aliases = {name, name.split(",")[0]}
        first = name.split()[0].rstrip(",")
        if len(first) >= 4 and first_words[first] == 1 and first not in {"the", "this", "that"}:
            aliases.add(first)
        patterns[name] = re.compile(
            r"(?<!\w)(?:"
            + "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True))
            + r")(?!\w)",
            re.I,
        )
        values = {_mv(c) for c in cards}
        if limit is not None:
            if None not in values and len(values) == 1 and next(iter(values)) > limit[0]:
                impossible[name] = f"{cards[0]['name']} (mana value {next(iter(values)):g})"
        elif name not in offered:
            impossible[name] = str(cards[0]["name"])
    kept, rejected = [], set()
    for clause in re.split(r"(?<=[;!?])\s*|(?<=\.)\s+", str(text or "")):
        x_claims = re.findall(r"\bX\s*=\s*(\d+)", clause, re.I)
        if (
            not _RETRIEVAL.search(clause)
            or _UNCERTAIN.search(clause.replace("’", "'"))
            or (limit is not None and any(int(x) != limit[0] for x in x_claims))
        ):
            kept.append(clause)
            continue
        retrieval = _RETRIEVAL.search(clause)
        targets = re.split(
            r"\b(?:but|while|whereas|and (?:hold|cast|draw|save))\b",
            clause[retrieval.end() :],
            maxsplit=1,
            flags=re.I,
        )[0]
        bad = {name for name in impossible if patterns[name].search(targets)}
        # An abbreviated name shared with a legal candidate is not evidence
        # that the explanation refers to the more expensive card.
        for name in list(bad):
            for match in patterns[name].finditer(targets):
                if any(other != name and patterns[other].fullmatch(match.group()) for other in groups):
                    bad.discard(name)
        if bad:
            rejected.update(bad)
        else:
            kept.append(clause)
    if not rejected:
        return text, []
    names = ", ".join(impossible[name] for name in sorted(rejected))
    fact = (
        f"At X={limit[0]}, {limit[1]} can search only for a creature with mana value {limit[0]} or less; {names} exceed that limit."
        if limit is not None
        else f"{names} are not among this search's offered choices. Choose only from the current menu."
    )
    return " ".join([*kept, "Rules correction: " + fact]), [fact]


def correct_cast_tutor_blockers(
    text: str, state: dict, decision: Any, chosen: list[str]
) -> tuple[str, list[str]]:
    """A proposed tutor creature's fight ability does not make it a flying blocker.

    Cast-stage X/affordability remains unknown. Known printed costs establish
    only the minimum X and printed payment before modifiers, never a new pick.
    Reuse the existing keyword/grant and name-binding checks (#138).
    """
    from arenamcp.game_plan import _claim_keys, _keyword_truth, _name_spans

    if decision.request_type != "ActionsAvailable" or len(chosen) != 1:
        return text, []
    selected = [o for o in decision.options if o.option_id == chosen[0]]
    if len(selected) != 1 or (selected[0].meta or {}).get("actionType") != "ActionType_Cast":
        return text, []
    meta = selected[0].meta
    source_id = _integer(meta.get("instanceId"))
    sources = [
        c
        for c in state.get("hand") or []
        if isinstance(c, dict) and source_id and c.get("instance_id") == source_id
    ]
    if len(sources) != 1:
        return text, []
    source = sources[0]
    if meta.get("grpId") is not None and meta.get("grpId") != source.get("grp_id"):
        return text, []
    if not _TUTOR_LIMIT.search(" ".join(str(source.get("oracle_text") or "").split())):
        return text, []
    source_name = str(source.get("name") or "")
    if not source_name or not re.match(rf"\s*{re.escape(source_name)}\b", str(text or ""), re.I):
        return text, []
    board = state.get("battlefield")
    if not isinstance(board, list) or any(
        not isinstance(c, dict)
        or not c.get("oracle_text")
        and "basic land" not in str(c.get("type_line") or "").lower()
        for c in board
    ):
        return text, []  # missing public rules might hide a grant or blocking exception
    if any(
        re.search(r"\bblock\b[^.]*\bas though\b", str(c.get("oracle_text") or ""), re.I)
        for c in [*board, source]
    ):
        return text, []
    cards = _known_cards(state, decision)
    keys = _claim_keys([c for c in cards if "creature" in str(c.get("type_line") or "").lower()])
    kept, facts = [], []
    for clause in re.split(r"(?<=[;!?])\s*|(?<!e\.g\.)(?<=\.)\s+", str(text or "")):
        lowered = clause.lower().replace("’", "'")
        match = re.search(r"\bfetch(?:es)?\b(?P<targets>.*?)\bto block (?:the |their )?flying\b", lowered)
        bound = re.search(r"\bit can fetch\b", lowered) or source_name.casefold() in lowered
        if (
            not match
            or not bound
            or _UNCERTAIN.search(lowered)
            or re.search(
                r"\b(?:equip|equipping|grant|gains?|give|giving|once|after|when|but|while|whereas)\b", lowered
            )
        ):
            kept.append(clause)
            continue
        named = {name for _, _, name in _name_spans(match["targets"], keys)}
        bad = []
        for name in sorted(named):
            variants = [c for c in cards if c.get("name") == name]
            if variants and all(
                c.get("oracle_text")
                and "creature" in str(c.get("type_line") or "").lower()
                and not re.search(
                    r"\bblock\b[^.]*\b(?:as though|flying)\b", str(c.get("oracle_text") or ""), re.I
                )
                and _keyword_truth([c], "flying", [*board, source, c]) is False
                and _keyword_truth([c], "reach", [*board, source, c]) is False
                for c in variants
            ):
                fact = f"{name} has neither flying nor reach in the supplied rules; blocking a flying creature requires an additional enabling effect"
                values = {_mv(c) for c in variants}
                cost = str(source.get("mana_cost") or "")
                if (
                    len(values) == 1
                    and None not in values
                    and re.fullmatch(r"\{X\}(?:\{(?:\d+|[WUBRGC])\})+", cost)
                ):
                    value = next(iter(values))
                    printed = value + _mana_value(cost)
                    fact += f". Its mana value {value:g} requires X at least {value:g}, with a printed payment of {printed:g} mana before cost modifiers"
                bad.append(fact)
        if bad:
            facts.extend(bad)
        else:
            kept.append(clause)
    if not facts:
        return text, []
    facts = list(dict.fromkeys(facts))
    kept.append(
        "Rules correction: "
        + ". ".join(facts)
        + ". Fight damage is distinct from blocking. Confirm a payable X after cost modifiers before naming the tutor target."
    )
    return " ".join(kept), facts
