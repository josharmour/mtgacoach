"""Conservative card-text roles and supported enabler/payoff links for Limited."""

import html
import re
from functools import lru_cache


def rules_profile(card: dict) -> dict:
    return _rules_profile(str(card.get("type_line") or ""), str(card.get("oracle_text") or ""))


@lru_cache(maxsize=4096)
def _rules_profile(type_line: str, oracle_text: str) -> dict:
    text = html.unescape(re.sub(r"<[^>]*>", "", oracle_text)).lower()
    lines = list(dict.fromkeys(part.strip() for part in re.split(r"[.\n]", text) if part.strip()))
    enables, payoffs = set(), set()
    body = "creature" in type_line.lower()
    unconditional_body = body
    if body:
        enables.add("creature_entry")
    if "artifact" in type_line.lower():
        enables.add("artifact_entry")
    for line in lines:
        trigger, effect = (
            (line.split(",", 1) + [""])[:2] if line.startswith(("when ", "whenever ", "at ")) else ("", line)
        )
        for mechanic, pattern in (
            ("scry", r"^whenever you (?:scry|surveil or scry)\b"),
            ("surveil", r"^whenever you (?:surveil|scry or surveil)\b"),
            ("draw", r"^whenever you draw a card$"),
            ("lifegain", r"^whenever you gain life$"),
            (
                "plus_counters",
                r"^whenever you put (?:one or more |a )?\+1/\+1 counters? on a creature(?: you control)?$",
            ),
        ):
            if re.search(pattern, trigger):
                payoffs.add(mechanic)
        for kind in ("artifact", "creature"):
            if re.fullmatch(
                r"whenever (?:an? |one or more )(?:other )?"
                + kind
                + r"s? enters?(?: the battlefield)? under your control",
                trigger,
            ):
                payoffs.add(kind + "_entry")
        for mechanic, pattern in (
            ("scry", r"\bscry \d"),
            ("surveil", r"\bsurveil \d"),
            ("draw", r"\bdraw (?:a|one|two|three|\d+) cards?"),
            ("lifegain", r"\bgain (?:\d+|one|two|three) life\b"),
            ("plus_counters", r"\bput\b[^.]*?\+1/\+1 counters?"),
        ):
            if re.search(pattern, effect):
                enables.add(mechanic)
        if re.search(r"\bcreate\b[^.]*?\b(?:artifact|treasure|food|clue|thopter|servo)\b", effect):
            enables.add("artifact_entry")
        if re.search(r"\bcreate\b[^.]*?\bcreature tokens?\b", effect):
            enables.add("creature_entry")
            body = True
            if not trigger and not re.search(r"\bif\b|:", effect):
                unconditional_body = True
    return {
        "body": body,
        "unconditional_body": unconditional_body,
        "enables": sorted(enables),
        "payoffs": sorted(payoffs),
    }


def synergy_evidence(first: dict, second: dict) -> list[dict]:
    evidence = []
    for source, payoff in ((first, second), (second, first)):
        source_roles, payoff_roles = rules_profile(source), rules_profile(payoff)
        for mechanic in set(source_roles["enables"]) & set(payoff_roles["payoffs"]):
            evidence.append(
                {"source_id": source["grp_id"], "payoff_id": payoff["grp_id"], "mechanic": mechanic}
            )
    return sorted(evidence, key=lambda edge: (edge["source_id"], edge["payoff_id"], edge["mechanic"]))


def synergy_graph(cards: list[dict]) -> list[dict]:
    unique = list({card["grp_id"]: card for card in cards}.values())
    return [
        edge
        for index, first in enumerate(unique)
        for second in unique[index + 1 :]
        for edge in synergy_evidence(first, second)
    ]
