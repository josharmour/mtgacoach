"""The colors a stated draft plan names, so the narrator follows the plan.

2026-10-08 QuickDraft_FRA: the model's plan read "Izzet spells/Jace-empowerment
tempo ... light black splash" from P1p2 on, but the spoken lane came from the
pool's color count, which tipped to blue-black at P1p9 and stayed there, so the
coach said "the deck is Dimir Threshold Mill" and called red cards "outside our
blue-black colors" while every pick followed the Izzet plan. A plan that names
a guild, a primer archetype, a color pair or a color code is the lane; the pool
estimate is only for a plan that names nothing, or no plan at all.

The reading is deterministic and conservative: a color named as a splash or a
secondary is the splash, a clause about staying open, a fallback or a pivot
names nothing, and alternatives joined by "or" count only for what they share
("Izzet or Azorius" is blue).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from arenamcp.set_primer import PAIR_NAMES, normalize_colors

COLOR_WORDS = {"white": "W", "blue": "U", "black": "B", "red": "R", "green": "G"}
GUILD_COLORS = {name.lower(): colors for colors, name in PAIR_NAMES.items()}

_COLOR = r"(white|blue|black|red|green)"
_GUILD = r"\b(" + "|".join(GUILD_COLORS) + r")\b"
# "blue-red", "blue/black", "blue and red", "red-blue"
_COLOR_PAIR = re.compile(rf"\b{_COLOR}(?:\s*[/-]\s*|\s+and\s+){_COLOR}\b")
# "UB", "U/R" (uppercase, so ordinary words never match)
_PAIR_CODE = re.compile(r"\b([WUBRG])/?([WUBRG])\b")
# "blue-based", "blue-centered", "mono-blue", "blue tempo", "blue deck"
_SINGLE_COLOR = re.compile(
    rf"\b(?:mono[- ]?{_COLOR}|{_COLOR}(?:-(?:based|centered|centred|led|heavy|leaning)"
    r"|\s+(?:deck|tempo|control|aggro|midrange|lane|core|plan|spells|threshold|ramp|mill)))\b"
)
# "splashing black", "splash for black", "light black splash", "red as a secondary splash",
# "black as a light secondary", "green as a possible secondary"
_SPLASH = re.compile(
    rf"\bsplash\w*\s+(?:\w+\s+){{0,2}}?{_COLOR}\b|\b{_COLOR}\s+(?:as\s+(?:a\s+)?)?(?:\w+\s+){{0,2}}?(?:splash|secondary)\b"
)
_CLAUSE_BREAK = re.compile(r"[,;:()]|\s[-—–]\s|\s(?:while|with|but|though|although|whereas)\s")
_OPEN = re.compile(r"\bopen\b|fallback|pivot|flexib|alternativ|\bif\b|\bunless\b|possible|optional")
_OR = re.compile(r"\bor\b")


@dataclass(frozen=True)
class PlanColors:
    main: str = ""  # WUBRG order, at most two colors
    splash: str = ""  # colors named as a splash or secondary, outside main
    archetype: str = ""  # the primer archetype the plan names, if any

    def __bool__(self) -> bool:
        return bool(self.main)


@dataclass(frozen=True)
class _Mention:
    start: int
    colors: str
    pair: bool  # a guild, archetype, color pair or code (vs a single color word)
    archetype: str = ""
    words: bool = False  # named by color words ("blue-red"), not a guild, archetype or code


def _archetype_pattern(name: str) -> re.Pattern | None:
    words = re.findall(r"[a-z0-9]+", name.lower())
    if not words:
        return None
    return re.compile(r"\b" + r"\W+".join(map(re.escape, words)) + r"\b")


def _mentions(plan: str, archetypes: list[dict] | None) -> list[_Mention]:
    lowered = plan.lower()
    found: list[_Mention] = []
    for archetype in archetypes or []:
        colors = normalize_colors(archetype.get("colors", ""))
        pattern = _archetype_pattern(str(archetype.get("name") or ""))
        if len(colors) == 2 and pattern:
            for match in pattern.finditer(lowered):
                found.append(_Mention(match.start(), colors, True, str(archetype.get("name"))))
    for match in re.finditer(_GUILD, lowered):
        found.append(_Mention(match.start(), GUILD_COLORS[match.group(1)], True))
    for match in _COLOR_PAIR.finditer(lowered):
        colors = normalize_colors(COLOR_WORDS[match.group(1)] + COLOR_WORDS[match.group(2)])
        if len(colors) == 2:
            found.append(_Mention(match.start(), colors, True, words=True))
    for match in _PAIR_CODE.finditer(plan):
        colors = normalize_colors(match.group(1) + match.group(2))
        if len(colors) == 2:
            found.append(_Mention(match.start(), colors, True))
    for match in _SINGLE_COLOR.finditer(lowered):
        word = match.group(1) or match.group(2)
        found.append(_Mention(match.start(), COLOR_WORDS[word], False))
    return sorted(found, key=lambda m: (m.start, not m.pair))


def _clauses(plan: str) -> list[tuple[int, int, str]]:
    lowered = plan.lower()
    spans, start = [], 0
    for match in _CLAUSE_BREAK.finditer(lowered):
        spans.append((start, match.start(), lowered[start : match.start()]))
        start = match.end()
    spans.append((start, len(lowered), lowered[start:]))
    return spans


def plan_colors(plan: str, archetypes: list[dict] | None = None) -> PlanColors:
    """The main colors (and splash) a plan sentence names, or nothing when it is still open."""
    plan = " ".join(str(plan or "").split())
    if not plan:
        return PlanColors()
    splash = ""
    for match in _SPLASH.finditer(plan.lower()):
        splash += COLOR_WORDS[match.group(1) or match.group(2)]
    splash_spans = [(m.start(), m.end()) for m in _SPLASH.finditer(plan.lower())]
    mentions = _mentions(plan, archetypes)
    # "blue, black and red goodstuff" names no pair: color words count only when
    # the plan (its splash aside) names at most two colors.
    outside_splash = _SPLASH.sub(" ", plan.lower())
    many_colors = len(set(re.findall(rf"\b{_COLOR}\b", outside_splash))) >= 3
    main, archetype = "", ""
    for start, end, text in _clauses(plan):
        if _OPEN.search(text):
            continue
        here = [m for m in mentions if start <= m.start < end]
        pairs = [m for m in here if m.pair and not (m.words and many_colors)]
        if pairs:
            if _OR.search(text) and len(pairs) > 1:
                shared = set(pairs[0].colors)
                for mention in pairs[1:]:
                    shared &= set(mention.colors)
                main = normalize_colors("".join(shared)) or pairs[0].colors
            else:
                main = pairs[0].colors
            archetype = next((m.archetype for m in pairs if m.archetype and m.colors == main), "")
            break
        if not main:
            for mention in here:
                if not mention.pair and not any(s <= mention.start < e for s, e in splash_spans):
                    main = mention.colors
                    break
    if main:
        splash = normalize_colors("".join(c for c in splash if c not in main))
    else:
        main, splash = "", ""
    return PlanColors(main=main, splash=splash, archetype=archetype)
