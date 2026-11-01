"""Check a small, explicit subset of draft prose against supplied card facts.

This is not a general natural-language rules validator. Only direct statements
about printed costs and simple tap-for-mana abilities are checked. Ambiguous,
conditional and unsupported syntax is left alone, not declared correct.
"""

from __future__ import annotations

import html
import re
from collections import Counter

_COLORS = dict(white="W", blue="U", black="B", red="R", green="G", colorless="C")
_COST = r"(?:\{[0-9WUBRGCXYZ/Po]+\})+|[0-9WUBRGCXYZ]+"
_QUALIFIED = re.compile(
    r"\b(with|without|under|after|before|when|while|if|unless|instead|using|for|to|or|plus|maybe|perhaps)\b",
    re.I,
)


def _symbols(cost: str) -> tuple[str, ...]:
    # Arena writes {o1oB}, while Oracle normally uses {1}{B}.
    symbols = re.findall(r"\{([^{}]+)\}", cost)
    if not symbols or re.sub(r"\{[^{}]+\}", "", cost).strip():
        return ()
    tokens = tuple(part.upper() for symbol in symbols for part in symbol.split("o") if part)
    if not all(re.fullmatch(r"\d+|[WUBRGCXYZ]|[WUBRG2]/[WUBRGP]", p) for p in tokens):
        return ()
    return tokens


def _claim_symbols(cost: str) -> tuple[str, ...]:
    return _symbols(cost) if "{" in cost else tuple(re.findall(r"\d+|[WUBRGCXYZ]", cost))


def _simple_mana_colors(card: dict) -> set[str]:
    # Basic land types grant mana abilities that may be absent from Oracle.
    # This checker only treats explicitly supplied tap abilities as exhaustive.
    if re.search(r"\b(Plains|Island|Swamp|Mountain|Forest)\b", str(card.get("type_line") or "")):
        return set()
    text = html.unescape(re.sub(r"<[^>]*>", "", str(card.get("oracle_text") or "")))
    colors: set[str] = set()
    for line in text.splitlines():
        if not re.search(r"\badd\b", line, re.I):
            continue
        # A second, complex ability (any color, filter, granted ability,
        # conditional source, etc.) makes the whole inference uncertain.
        match = re.fullmatch(r"\s*\{o?T\}:\s*Add\s+([^.]*)\.\s*", line, re.I)
        symbols = _symbols(match[1]) if match else ()
        if not symbols or not set(symbols) <= set("WUBRGC"):
            return set()
        colors.update(symbols)
    return colors


def reason_corrections(reason: str, subject: dict, cards: list[dict]) -> list[str]:
    """Return facts contradicting direct claims; an empty list is not validation.

    Requiring a sentence-initial subject avoids interpreting comparisons,
    questions, negation and hypothetical quotations as the coach's claim.
    Alternate faces and effective casting costs are deliberately out of scope.
    """
    corrections: list[str] = []
    for card in {c["grp_id"]: c for c in cards}.values():
        name = str(card.get("name") or "")
        if not name:
            continue
        subjects = [re.escape(name)]
        # A short legendary name is safe only when unique among these cards.
        short = name.split(",", 1)[0]
        if short != name and sum(str(c.get("name") or "").split(",", 1)[0] == short for c in cards) == 1:
            subjects.append(re.escape(short))
        start = r"(?:^|[.!?]\s+)(?:" + "|".join(subjects) + r")"
        if card["grp_id"] == subject["grp_id"]:
            # After another sentence, "it" may refer to a different card.
            start = r"(?:" + start + r"|^(?:[Ii]t|[Tt]his card))"
        printed = _symbols(str(card.get("mana_cost") or ""))
        if printed and not card.get("related_faces"):
            pattern = (
                start
                + r"(?: costs |'s (?:printed )?mana cost is )("
                + _COST
                + r")(?=[.,;!?\s]|$)([^.!?]*)(?=[.!]|$)"
            )
            for match in re.finditer(pattern, reason):
                tail = match[2].lower()
                # Unbraced "costs 2" usually describes total mana, not a
                # printed {2} cost with no colored requirement.
                if "{" not in match[1] and not re.search(r"[WUBRGC]", match[1]):
                    continue
                # Do not compare discounted, alternative or conditional payments
                # to a printed mana cost, even when the prose is terse.
                if _QUALIFIED.search(tail):
                    continue
                if Counter(_claim_symbols(match[1])) != Counter(printed):
                    cost = "".join("{" + pip + "}" for pip in printed)
                    corrections.append(f"{name}'s printed mana cost is {cost}.")
        mana_colors = _simple_mana_colors(card)
        if mana_colors and not card.get("related_faces"):
            pattern = (
                start
                + r" (?:produces|adds|taps for) (?:one |two |three )?(white|blue|black|red|green|colorless)(?: mana|(?=[.,;!?]|$))(?=[.,;!?\s]|$)([^.!?]*)(?=[.!]|$)"
            )
            for match in re.finditer(pattern, reason):
                if not _QUALIFIED.search(match[2]) and _COLORS[match[1]] not in mana_colors:
                    produced = "/".join(label for label, symbol in _COLORS.items() if symbol in mana_colors)
                    corrections.append(f"{name}'s supplied tap ability produces {produced} mana.")
    return list(dict.fromkeys(corrections))
