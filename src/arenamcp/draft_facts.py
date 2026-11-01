"""Check a small, explicit subset of draft prose against supplied card facts.

This is not a general natural-language rules validator. It checks direct printed
cost and mana-production claims, a narrow generic discount, and attribution of
free equip to hand-to-battlefield abilities. Unsupported syntax is left alone,
not declared correct.
"""

from __future__ import annotations

import html
import re
from collections import Counter

_COLORS = dict(white="W", blue="U", black="B", red="R", green="G", colorless="C")
_COST = r"(?:\{[0-9WUBRGCXYZ/Po]+\})+|[0-9WUBRGCXYZ]+"
_QUALIFIED = re.compile(
    r"\b(with|without|under|after|before|when|while|if|unless|instead|using|for|to|or|plus|targeting|maybe|perhaps)\b",
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


def _equipment_corrections(reason: str, subject: dict, cards: list[dict]) -> list[str]:
    corrections = []
    for card in cards:
        name = str(card.get("name") or "")
        text = html.unescape(re.sub(r"<[^>]*>", "", str(card.get("oracle_text") or "")))
        # This is a scope check on an explicit hand-to-battlefield ability,
        # not an assumption that all Equipment tutors lack equip abilities.
        if not name or not re.search(r"put an Equipment card from your hand onto the battlefield", text):
            continue
        if re.search(r"\b(?:equip|attach)\b", text, re.I):
            continue
        claim = re.search(
            r"\b(?:via|using|through|with) " + re.escape(name) + r"['’]s free[- ]equip ability\b",
            reason,
            re.I,
        )
        if claim:
            prefix = re.split(r"[.!?]", reason[: claim.start()])[-1]
            if re.search(r"\b(?:not|never|without|isn't|doesn't)\b", prefix, re.I):
                continue
            corrections.append(
                f"{name}'s hand-to-battlefield ability deploys Equipment; it does not equip it."
            )
            equipment_text = str(subject.get("oracle_text") or "")
            if re.search(r"(?:^|\n)Living weapon\b", equipment_text, re.I):
                corrections.append(
                    f"{subject['name']}'s Living weapon creates a token and attaches that Equipment to that token."
                )
    return corrections


def _discount_corrections(reason: str, card: dict) -> list[str]:
    """A single printed generic reduction cannot erase colored pips."""
    text = html.unescape(re.sub(r"<[^>]*>", "", str(card.get("oracle_text") or "")))
    if card.get("related_faces") or re.search(r"without paying|rather than|instead of", text, re.I):
        return []
    match = re.search(
        r"This spell costs (\{[^}]+\}) less to cast if it targets a ([A-Za-z]+(?: or [A-Za-z]+)?)( you control)?\.",
        text,
    )
    printed = _symbols(str(card.get("mana_cost") or ""))
    reduction = _symbols(match[1]) if match else ()
    if not match or len(reduction) != 1 or not reduction[0].isdigit() or not printed:
        return []
    colored = [pip for pip in printed if pip in "WUBRGC"]
    if not colored or not all(pip.isdigit() or pip in "WUBRGC" for pip in printed):
        return []
    claim = re.search(
        r"(?:" + re.escape(card["name"]) + r"|this (?:second |extra |additional )?copy) "
        r"(?:discounts to|costs) (?:\{0\}|0|zero) (?:when )?targeting "
        + r"(?:"
        + "|".join(re.escape(t) for t in match[2].split(" or "))
        + r")s?\b",
        reason,
        re.I,
    )
    if not claim:
        return []
    if re.search(
        r"\b(?:not|never|without|isn't|doesn't)\b", re.split(r"[.!?]", reason[: claim.start()])[-1], re.I
    ):
        return []
    if re.search(
        r"\b(with|without|under|when|while|if|unless|instead|using)\b",
        re.split(r"[.!?]", reason[claim.end() :])[0],
        re.I,
    ):
        return []
    generic = max(0, sum(int(p) for p in printed if p.isdigit()) - int(reduction[0]))
    remaining = ("{" + str(generic) + "}" if generic else "") + "".join("{" + p + "}" for p in colored)
    return [
        f"{card['name']}'s generic reduction leaves {remaining} when it targets a {match[2]}{match[3] or ''}; colored mana is still required."
    ]


def reason_corrections(reason: str, subject: dict, cards: list[dict]) -> list[str]:
    """Return facts contradicting direct claims; an empty list is not validation.

    Checks require an explicit subject or a narrow causal attribution. General
    comparisons, alternate faces and effective costs outside the recognized
    printed generic discount remain out of scope.
    """
    corrections = _equipment_corrections(reason, subject, cards) + _discount_corrections(reason, subject)
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
            # The captured Tutor claim phrases its colored requirement as a
            # "mana commitment beyond BB" rather than "costs BB". Compare
            # only colored pips here; "B commitment" needn't state generic mana.
            commitment = re.search(r"\bmana commitment (?:beyond|of) (" + _COST + r")(?=[.,;!?\s]|$)", reason)
            if (
                reason.startswith(name + " ")
                and commitment
                and not re.search(r"\b(?:not|never|isn't|doesn't)\b", reason[: commitment.start()], re.I)
                and all(p.isdigit() or p in "WUBRGC" for p in printed)
            ):
                claimed = _claim_symbols(commitment[1])
                if (
                    claimed
                    and all(p.isdigit() or p in "WUBRGC" for p in claimed)
                    and Counter(p for p in claimed if p in "WUBRGC")
                    != Counter(p for p in printed if p in "WUBRGC")
                ):
                    corrections.append(
                        f"{name}'s printed mana cost is {''.join('{' + p + '}' for p in printed)}."
                    )
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
            description = re.match(
                r"(white|blue|black|red|green|colorless)-producing (?:white |blue |black |red |green |colorless )?land(?= that|[.,;]|$)",
                reason,
                re.I,
            )
            if (
                card["grp_id"] == subject["grp_id"]
                and "land" in str(card.get("type_line") or "").lower()
                and description
                and _COLORS[description[1].lower()] not in mana_colors
            ):
                produced = "/".join(label for label, symbol in _COLORS.items() if symbol in mana_colors)
                corrections.append(f"{name}'s supplied tap ability produces {produced} mana.")
            pattern = (
                start
                + r" (?:produces|adds|taps for) (?:one |two |three )?(white|blue|black|red|green|colorless)(?: mana|(?=[.,;!?]|$))(?=[.,;!?\s]|$)([^.!?]*)(?=[.!]|$)"
            )
            for match in re.finditer(pattern, reason):
                if not _QUALIFIED.search(match[2]) and _COLORS[match[1]] not in mana_colors:
                    produced = "/".join(label for label, symbol in _COLORS.items() if symbol in mana_colors)
                    corrections.append(f"{name}'s supplied tap ability produces {produced} mana.")
    return list(dict.fromkeys(corrections))
