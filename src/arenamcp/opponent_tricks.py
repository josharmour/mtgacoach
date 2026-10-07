"""Opponent instant-speed interaction risk for Limited (phase 1: advisory only).

Answers "how likely is it that the opponent holds a castable trick right now?"
from the planner snapshot (``server.get_game_state`` shape) and a per-set
table built from data already cached for the draft helper. The result is a
fact for prompts, the UI and telemetry; nothing in the combat solvers or the
line search reads it, so a wrong number cannot change a decision.

Per-set table (built off the decision path, cached on disk)
-----------------------------------------------------------
* Cards: every Instant, every card with a ``Flash`` line, and every card with
  an instant-speed "{cost}, Discard this card: ..." ability (Proft, Sureshot
  Sower) from the set primer, with oracle text cleaned (``<nobr>``/``<indent>``
  stripped, Arena's triplicated variants collapsed, ``{oX}`` expanded) and each
  mode classified as pump / keyword / protect / removal / shrink / bounce / tap /
  flash_body / fog / counter / other. "other" is never scored; a card with only
  "other" modes is left out.
* Deck hypotheses h: the 10 colour pairs ("UR"), each pair plus a splash colour
  ("UR+W", the splash colour at density SIGMA_SPLASH) and the 10 three-colour
  decks ("WUR"). Class priors two-colour / pair+splash / three-colour come from
  17Lands colour ratings (FRA, 2026-10-06: 48 / 34 / 14 % of all decks), the
  pair split from the primer's pair shares.
* castable(c, h): every coloured pip of c's mana cost has at least one colour in
  h's colours. A hybrid pip is one pip with several options, so Ferocity of the
  Hunt {1}{B/G} fits every pair with B or G (and Twinned Vision {1}{U/R} every
  pair with U or R), not only the gold pair the primer's colours name.
* Expected copies per 40-card deck (23 nonland cards):
      d_h(c) = 1 if castable from h's main colours, SIGMA_SPLASH if it needs the
               splash colour, else 0
      a(c,h) = game_count_c * prior_h * d_h(c) / sum_h' prior_h' * d_h'(c)
      K_h(c) = 23 * a(c,h) / sum over nonland c' of a(c',h)

Runtime estimate (``trick_risk``: pure, no I/O, well under a millisecond)
------------------------------------------------------------------------
* Scope: Limited only, with a table for the set and a known opponent hand count.
* Open mana: the opponent's untapped mana sources (lands; rocks; mana creatures
  not summoning sick, where one that entered on their latest turn is still sick
  during our turn) plus their mana pool. Colours come from ``color_production``
  (names or ManaColor digits) else ``mulligan_policy._land_colors``. Tapped state
  is the battlefield's ``is_tapped``, never the GSM action list.
* Posterior over h: prior_h times, for every public opponent land and spell, its
  colour weight under h (1 main colour, SIGMA_SPLASH the splash colour, 0.15 an
  off-colour land, 0.05 an off-colour spell; a spell takes its least-supported
  pip, hybrid-aware), divided by Z_h = (#main colours + splash weight) / 2. For
  pure pairs Z_h = 1, i.e. exactly "1 vs 0.15 per land, 1 vs 0.05 per spell";
  Z_h stops the wider splash/three-colour hypotheses from explaining everything
  for free. Behaviour signals have likelihood ratio 1.0 (no fit).
* Copies in hand (the critique's A1 fix): under the Poisson copy model the
  copies left in library + hand follow Poisson(K * U / D) whatever has been
  seen, so each hidden hand card is a copy of c with probability K_h(c) / 40:
      lambda_{c,h} = hand_unknown * RHO * K_h(c) / 40,   RHO = 1.0 (no fit)
  NOT (hand / unseen pool) * (K minus copies seen): that overstates by D / U
  (x1.4 at the G3 T10 block, x1.6-2 by G1 T14) and the copies-seen step is not
  informative for Poisson-like copy counts.
* Deck size: D = opponent library + hand + public non-token cards once
  ``zones.opponent_library_count`` exists (WP6), else D = 40 and
  ``deck_size_assumed``. Per-card density K/40 does not depend on D when a big
  deck scales its spells, but 17Lands decks are 93.5 % exactly 40 cards, so a
  bigger deck is a different population: each card's K is shrunk toward
  GENERIC_COPIES_PER_40 for its kind with w = clamp((D - 42) / 18, 0, 1).
* Castable now: mana value <= open mana and the coloured pips matched one source
  each (``mulligan_policy._pip_matching``). Cost reductions, alternative costs and
  convoke are ignored, which underestimates.
* Combined exactly per card per hypothesis (Poisson thinning):
      p_hand = sum_h post_h * (1 - prod_c exp(-lambda_{c,h}))
  over the castable cards with a kind that matters at this timing. q_by_class
  (display only) is the same sum per kind.
* Certain public options (q = 1): affordable instant-speed activated abilities
  on their battlefield (not "Activate only as a sorcery", not loyalty, not mana
  abilities), affordable flashback / jump-start / retrace instants and
  instant-speed "from your graveyard" abilities in their graveyard, and revealed
  cards still in their hand (``revealed_cards``, WP6) that are castable now.
  p_any = 1 when one of those matters now, else p_hand.

A mode's target restriction ("with flying", "attacking or blocking", a colour)
counts it only when one of our creatures qualifies (``mode_target``); a source
whose mana can't cast spells ("Spend this mana only ...") pays only activated
abilities. Not modelled (left to ``unknowns``): their draws, cards castable
from exile (impulse draw, prepare spells), cost reductions, other target
restrictions (power, mana value), and which trick they would pick.
The planner text is a verdict without card names (critique C2); card names are
for the human-facing UI only.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from itertools import combinations
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from arenamcp.draftstate import extract_set_code
from arenamcp.format_profile import detect_format_profile
from arenamcp.mulligan_policy import _land_colors, _mana_value, _pip_matching, _pips
from arenamcp.set_primer import PRIMER_DIR, PRIMER_MAX_AGE_S, SetPrimer

logger = logging.getLogger(__name__)

TRICK_DIR = Path.home() / ".arenamcp" / "cache" / "trick_tables"
TRICK_TABLE_VERSION = 2  # 2: per-mode target restrictions; base P/T, sweeps, tucks, "that permanent" bites

COLORS = "WUBRG"
PAIRS = ("WU", "WB", "WR", "WG", "UB", "UR", "UG", "BR", "BG", "RG")
TRIOS = tuple("".join(trio) for trio in combinations(COLORS, 3))

DECK_REFERENCE = 40  # K is per 40-card deck
NONLAND_PER_DECK = 23
# Hand-set, not fitted (the user's 2026-10-06 decision: no learned model).
RHO = 1.0  # how much more often an instant stays in hand than other cards
SIGMA_SPLASH = 0.25  # a splash colour's card density relative to a main colour
OFF_COLOR_LAND = 0.15
OFF_COLOR_SPELL = 0.05
# Two-colour / pair+splash / three-colour share of all FRA decks (17Lands colour
# ratings, 2026-10-06); used when no colour ratings reach the build.
CLASS_PRIOR_DEFAULT = {"pure": 0.48, "splash": 0.34, "three": 0.14}
# Behaviour signals keep likelihood ratio 1.0 until a fit exists (critique A6/C1).
BEHAVIOUR_LR = {"missed_land_drop": 1.0}
# Generic expected copies per 40-card deck of each kind (by a card's first
# kind), toward which a big deck's table is shrunk. Hand-set, not fitted: FRA's
# prior-weighted two-colour averages on 2026-10-06 were removal 2.57, bounce
# 0.39, shrink 0.35, pump 0.26, counter 0.18, flash bodies 0.17, protect 0.10,
# tap 0.09, keyword 0.04; the rarely played tricks (pump, protection, flash
# bodies) are raised for a deck that runs its whole pool.
GENERIC_COPIES_PER_40 = {
    "removal": 2.5,
    "bounce": 0.4,
    "pump": 0.6,
    "protect": 0.2,
    "keyword": 0.2,
    "shrink": 0.3,
    "fog": 0.1,
    "tap": 0.2,
    "flash_body": 0.3,
    "counter": 0.3,
}

# Display / dominance order of the kinds.
KINDS = (
    "removal",
    "bounce",
    "pump",
    "protect",
    "keyword",
    "shrink",
    "fog",
    "tap",
    "flash_body",
    "counter",
    "other",
)
SCORED_KINDS = frozenset(KINDS) - {"other"}
_COMBAT_KINDS = frozenset({"removal", "bounce", "pump", "protect", "keyword", "shrink"})
_AFTER_BLOCKS = frozenset(
    {"Step_DeclareBlock", "Step_FirstStrikeDamage", "Step_CombatDamage", "Step_EndCombat"}
)

_KIND_WORDS = {
    "removal": "removal",
    "bounce": "bounce",
    "pump": "pump",
    "protect": "protection",
    "keyword": "keyword grant",
    "shrink": "shrink",
    "fog": "fog",
    "tap": "tap effect",
    "flash_body": "flash creature",
    "counter": "counterspell",
}
_COLOR_NAMES = {
    "white": "W",
    "blue": "U",
    "black": "B",
    "red": "R",
    "green": "G",
    "colorless": "C",
    "w": "W",
    "u": "U",
    "b": "B",
    "r": "R",
    "g": "G",
    "c": "C",
    # GRE ManaColor enum: the log path keeps ColorProduction's int32 values.
    "1": "W",
    "2": "U",
    "3": "B",
    "4": "R",
    "5": "G",
    "6": "C",
}

_ERRORS_LOGGED: set[str] = set()
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}  # fmt: skip


def _log_once(key: str, message: str, *args: Any) -> None:
    if key not in _ERRORS_LOGGED:
        _ERRORS_LOGGED.add(key)
        logger.warning(message, *args, exc_info=True)


# --- oracle text --------------------------------------------------------------

_MARKUP = re.compile(r"<[^>]*>")
_ARENA_GROUP = re.compile(r"\{(o[^}]*)\}")


def expand_costs(text: str) -> str:
    """Arena's packed costs as one brace per symbol: '{o1o(U/R)}' -> '{1}{U/R}'."""

    def unpack(match: re.Match) -> str:
        return "".join("{%s}" % part.strip("()") for part in match.group(1).split("o") if part)

    return _ARENA_GROUP.sub(unpack, text or "")


def _brace_cost(cost: Any) -> str:
    text = str(cost or "").strip()
    if text and "{" not in text and "o" in text:
        text = "{" + text + "}"  # bare Arena form, "o2oUoU"
    return expand_costs(text)


def clean_oracle(text: str) -> str:
    """Oracle text without markup, one copy of each line, costs expanded."""
    lines: list[str] = []
    seen: set[str] = set()
    for raw in _MARKUP.sub("", text or "").splitlines():
        line = " ".join(raw.split())
        if line and line.lower() not in seen:
            seen.add(line.lower())
            lines.append(line)
    return expand_costs("\n".join(lines))


def _rules(text: str) -> str:
    """Lower-case cleaned text without reminder text."""
    lowered = clean_oracle(text).lower()
    while re.search(r"\([^()]*\)", lowered):
        lowered = re.sub(r"\([^()]*\)", "", lowered)
    return lowered


def split_modes(text: str) -> list[str]:
    """'Choose one' bullets as separate modes, else the whole text as one."""
    cleaned = clean_oracle(text)
    bullets = [line.lstrip("•").strip() for line in cleaned.splitlines() if line.startswith("•")]
    return bullets or ([cleaned] if cleaned else [])


# Patterns that extend board_assessment.removal_reach/_PUMP and
# combat_strategy._BLOCK_TRICK where they miss tricks (edicts, "bite", +X pumps,
# -X/-0, taps, protection).
_EDICT = re.compile(r"\b(?:opponent|player)s? sacrifices?\b[^.]*?\bcreature")
_BITE = re.compile(
    r"\bdeals? damage equal to (?:its|that creature's|their) power to (?:(?:another )?(?:target|up to)"
    r"|that (?:creature|permanent))"
)
# "Target creature has base power and toughness 0/0" (removal), "1/1" (shrink), "4/5" (pump).
_BASE_PT = re.compile(r"\bha(?:s|ve) base power and toughness (\d+)/(\d+)")
# "All creatures get -3/-3": a sweep that removes.
_SWEEP_SHRINK = re.compile(
    r"\b(?:all|each) (?:other )?creatures?\b[^.]*?\bgets? [-−–](?:\d+|x)/[-−–](?:\d+|x)"
)
# "Choose target nonland permanent ... on top / the bottom of their library": a tuck.
_TUCK = re.compile(r"\btarget nonland permanent\b[^\n]*?\b(?:top|bottom) of (?:their|its owner's) library")
_SELF_SUBJECT = re.compile(r"(?:~|this creature|this permanent)\s*$")
_QUOTED = re.compile(r"\"[^\"]*\"|“[^”]*”")
_COLOR_WORD = {"white": "W", "blue": "U", "black": "B", "red": "R", "green": "G"}
_TARGET_COLORS = re.compile(
    r"\btarget ((?:white|blue|black|red|green)(?: or (?:white|blue|black|red|green))*) creature"
    r"|\btarget creature(?: or planeswalker)? that's ((?:white|blue|black|red|green)(?: or (?:white|blue|black|red|green))*)"
)
_PUMP_EXTRA = re.compile(
    r"\b(?:target|enchanted|equipped|that|another target) creatures?(?: you control)?(?: [a-z]+)? gets? \+(?:\d+|x)/"
    r"|\bcreatures you control get \+(?:\d+|x)/|\bit gets \+(?:\d+|x)/|\+1/\+1 counters? on (?:target|up to|each)"
)
_SELF_PUMP = re.compile(r"\b(?:this creature|this permanent|~) gets \+(?:\d+|x)/")
_KEYWORD = re.compile(
    r"\b(?:gains?|has|have)\b[^.]*?\b(?:first strike|double strike|deathtouch|trample|flying|menace|lifelink"
    r"|reach|vigilance)\b"
)
_PROTECT = re.compile(
    r"\b(?:gains?|has|have)\b[^.]*?\b(?:hexproof|indestructible|protection from|ward)\b|\bregenerate\b|\bphases? out\b"
)
_SHRINK_POWER = re.compile(r"\bgets? [-−–](?:\d+|x)/[-−–+]?0\b")
_TAP = re.compile(
    r"\btap (?:target|up to (?:one|two|three|four|x|\d+) target|all|each) (?:[a-z]+ )*?creatures?\b"
    r"|\bstun counters?\b"
)
_FOG = re.compile(r"\bprevent all combat damage\b")
_FLASH_LINE = re.compile(r"(?m)^flash\s*$")
_TRIGGER_START = re.compile(r"(?:when|whenever|at the beginning|if|as long as)\b")
_LOYALTY_COST = re.compile(r"^\s*\[?\s*[+\-−–]?\s*(?:\d+|x)\s*\]?\s*$")
_MANA_EFFECT = re.compile(r"\badd\b[^.]*?(?:\{|\bmana\b)")
_GRAVEYARD_CAST = re.compile(r"\b(?:flashback|escape)\s*[—–-]?\s*((?:\{[^}]+\})+)")


def classify_effect(text: str, name: str = "", *, ability: bool = False) -> tuple[str, ...]:
    """The kinds of one mode's rules text, in KINDS order; ('other',) if none.

    ``ability``: the text is an activated ability's effect, so "this creature
    gets +1/+0" is a pump (on a card it is usually a static bonus).
    """
    from arenamcp.board_assessment import _COUNTER, _PUMP, removal_reach
    from arenamcp.combat_strategy import _BLOCK_TRICK

    rules = _rules(text)
    if name:
        rules = rules.replace(name.lower(), "~")
        short = name.split(",")[0].strip().lower()
        if short and short != name.lower():
            rules = rules.replace(short, "~")  # "Loot gets -2/-0": the card itself
    kinds: set[str] = set()
    reach = removal_reach({"oracle_text": text, "name": name})
    if reach is not None:
        kinds.add("bounce" if reach[0] == "bounce" else "removal")
    if _EDICT.search(rules) or _BITE.search(rules) or _SWEEP_SHRINK.search(rules) or _TUCK.search(rules):
        kinds.add("removal")
    for match in _BASE_PT.finditer(rules):
        power, toughness = int(match.group(1)), int(match.group(2))
        kinds.add("removal" if toughness == 0 else "shrink" if power <= 1 and toughness <= 1 else "pump")
    if any(m.group(0) != "enchant creature" for m in _PUMP.finditer(rules)) or _PUMP_EXTRA.search(rules):
        kinds.add("pump")
    if ability and _SELF_PUMP.search(rules):
        kinds.add("pump")
    # _BLOCK_TRICK's keyword, indestructible and fog branches; its bare "gets +X/+Y"
    # also matches a creature's static bonus, so pumps come from the targeted patterns.
    for match in _BLOCK_TRICK.finditer(rules):
        found = match.group(0)
        if found.startswith("prevent"):
            kinds.add("fog")
        elif "indestructible" in found:
            kinds.add("protect")
        elif found.startswith("gain"):
            kinds.add("keyword")
    if _KEYWORD.search(rules):
        kinds.add("keyword")
    if _PROTECT.search(rules):
        kinds.add("protect")
    if any(not _SELF_SUBJECT.search(rules[: m.start()]) for m in _SHRINK_POWER.finditer(rules)):
        kinds.add("shrink")  # not the card shrinking itself
    if _TAP.search(rules):
        kinds.add("tap")
    if _FOG.search(rules):
        kinds.add("fog")
    if _COUNTER.search(rules):
        kinds.add("counter")
    ordered = tuple(kind for kind in KINDS if kind in kinds)
    return ordered or ("other",)


def _ordered(kinds: Iterable[str]) -> tuple[str, ...]:
    wanted = set(kinds)
    return tuple(kind for kind in KINDS if kind in wanted)


def _cost_parts(cost_text: str) -> tuple[str, bool]:
    """(mana cost in braces, needs {T} or {Q}) of an activated ability's cost."""
    symbols = re.findall(r"\{([^}]+)\}", cost_text)
    mana = "".join("{%s}" % s for s in symbols if s.lower() not in ("t", "q"))
    return mana.upper(), any(s.lower() in ("t", "q") for s in symbols)


def _activated_abilities(text: str) -> list[tuple[str, str]]:
    """(cost, effect) for every activated ability line of rules text (lower case).

    Abilities granted in quotes ('Planeswalkers you control have "[-4]: ..."')
    belong to other permanents and are dropped; loyalty abilities too.
    """
    abilities = []
    for line in _rules(text).splitlines():
        line = _QUOTED.sub("", line).strip()
        if ":" not in line or _TRIGGER_START.match(line):
            continue
        cost, effect = line.split(":", 1)
        if _LOYALTY_COST.match(cost) or _MANA_EFFECT.search(effect):
            continue
        abilities.append((cost.strip(), effect.strip()))
    return abilities


# --- the per-set table ----------------------------------------------------------


@dataclass(frozen=True)
class TrickMode:
    kinds: tuple[str, ...]
    text: str = ""
    # What its target must be: "flying", "combat" (attacking or blocking) or
    # "colors:BG"; "" for any creature. Checked against our creatures at runtime.
    target: str = ""


def mode_target(text: str) -> str:
    """The target restriction of one mode's text ("" when any creature will do)."""
    rules = _rules(text)
    if re.search(r"\btarget (?:[a-z]+ )*creature(?: or planeswalker)? with flying\b", rules):
        return "flying"
    if re.search(r"\btarget (?:attacking or blocking|attacking|blocking) creature\b", rules):
        return "combat"
    match = _TARGET_COLORS.search(rules)
    if match:
        words = re.findall(r"white|blue|black|red|green", match.group(1) or match.group(2) or "")
        return "colors:" + "".join(sorted({_COLOR_WORD[w] for w in words}))
    return ""


@dataclass(frozen=True)
class TrickCard:
    grp_id: int
    name: str
    mana_cost: str  # brace form, "{1}{U/R}"; for source "hand" the ability's cost
    mv: int
    pips: tuple[str, ...]  # one entry per coloured pip: its colour options ("UR" for {U/R})
    kinds: tuple[str, ...]  # scored kinds, KINDS order
    modes: tuple[TrickMode, ...] = ()
    source: str = "instant"  # "instant", "flash" or "hand" ("{cost}, Discard this card:")
    graveyard_cost: str = ""  # flashback/escape cost, or the mana cost for jump-start/retrace
    graveyard_discard: bool = False  # jump-start/retrace also discard a card

    @property
    def pip_sets(self) -> tuple[frozenset[str], ...]:
        return tuple(frozenset(pip) for pip in self.pips)

    @property
    def kind(self) -> str:
        return self.kinds[0] if self.kinds else "other"


@dataclass
class TrickTable:
    set_code: str
    version: int = TRICK_TABLE_VERSION
    built_at: float = 0.0
    primer_built_at: float = 0.0
    source: str = "17lands_alloc"
    prior: dict[str, float] = field(default_factory=dict)  # hypothesis -> prior
    cards: dict[int, TrickCard] = field(default_factory=dict)
    copies: dict[str, dict[int, float]] = field(default_factory=dict)  # hypothesis -> grp -> K per 40
    set_grp_ids: frozenset[int] = frozenset()  # every rated card of the set (set-code fallback)
    costs: dict[int, str] = field(default_factory=dict)  # grp -> mana cost, for the posterior

    def to_json(self) -> str:
        data = asdict(self)
        data["cards"] = {str(grp): asdict(card) for grp, card in sorted(self.cards.items())}
        data["copies"] = {
            hyp: {str(grp): value for grp, value in sorted(row.items())}
            for hyp, row in sorted(self.copies.items())
        }
        data["set_grp_ids"] = sorted(self.set_grp_ids)
        data["costs"] = {str(grp): cost for grp, cost in sorted(self.costs.items())}
        return json.dumps(data, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> TrickTable:
        data = json.loads(text)
        cards = {}
        for grp, raw in (data.pop("cards", {}) or {}).items():
            modes = tuple(
                TrickMode(kinds=tuple(m["kinds"]), text=m.get("text", ""), target=m.get("target", ""))
                for m in raw.pop("modes", [])
            )
            raw["pips"] = tuple(raw.get("pips") or ())
            raw["kinds"] = tuple(raw.get("kinds") or ())
            cards[int(grp)] = TrickCard(**raw, modes=modes)
        copies = {
            hyp: {int(grp): float(value) for grp, value in row.items()}
            for hyp, row in (data.pop("copies", {}) or {}).items()
        }
        set_grp_ids = frozenset(int(grp) for grp in data.pop("set_grp_ids", []) or [])
        costs = {int(grp): str(cost) for grp, cost in (data.pop("costs", {}) or {}).items()}
        return cls(**data, cards=cards, copies=copies, set_grp_ids=set_grp_ids, costs=costs)


@dataclass(frozen=True)
class _Hypothesis:
    key: str
    main: frozenset[str]
    splash: str = ""

    @property
    def splash_weight(self) -> float:
        return SIGMA_SPLASH if self.splash else 0.0

    @property
    def colors(self) -> frozenset[str]:
        return self.main | {self.splash} if self.splash else self.main

    @property
    def norm(self) -> float:
        """Z_h: 1 for a pair, larger for the wider splash and three-colour decks."""
        return (len(self.main) + self.splash_weight) / 2.0

    def density(self, pips: tuple[frozenset[str], ...]) -> float:
        """d_h(c): 1 castable from the main colours, SIGMA_SPLASH with the splash, else 0."""
        if all(pip & self.main for pip in pips):
            return 1.0
        if self.splash and all(pip & self.colors for pip in pips):
            return SIGMA_SPLASH
        return 0.0

    def weight(self, colors: frozenset[str], off: float) -> float:
        if colors & self.main:
            return 1.0
        if self.splash and self.splash in colors:
            return SIGMA_SPLASH
        return off


def _hypothesis(key: str) -> _Hypothesis:
    main, _, splash = key.partition("+")
    return _Hypothesis(key=key, main=frozenset(main), splash=splash)


def hypothesis_keys() -> tuple[str, ...]:
    keys = list(PAIRS)
    keys += [f"{pair}+{color}" for pair in PAIRS for color in COLORS if color not in pair]
    keys += list(TRIOS)
    return tuple(keys)


def _norm_colors(raw: Any) -> str:
    letters = {c for c in str(raw or "").upper() if c in COLORS}
    return "".join(c for c in COLORS if c in letters)


def hypothesis_priors(
    pair_share: dict[str, float], color_ratings: list[dict] | None = None
) -> dict[str, float]:
    """prior_h from 17Lands colour ratings (summary rows give the class split)."""
    split = dict(CLASS_PRIOR_DEFAULT)
    pure = {pair: float(pair_share.get(pair) or 0.0) for pair in PAIRS}
    splash = dict(pure)
    three = dict.fromkeys(TRIOS, 1.0)
    if color_ratings:
        summary: dict[str, float] = {}
        plain: dict[str, float] = {}
        plus: dict[str, float] = {}
        for row in color_ratings:
            if not isinstance(row, dict):
                continue
            name, games = str(row.get("short_name") or ""), float(row.get("games") or 0)
            if row.get("is_summary"):
                summary[name] = games
            else:
                (plus if name.endswith("+") else plain)[_norm_colors(name)] = games
        if all(summary.get(key) for key in ("2", "2+", "3")):
            split = {"pure": summary["2"], "splash": summary["2+"], "three": summary["3"]}
        if any(plain.get(pair) for pair in PAIRS):
            pure = {pair: plain.get(pair, 0.0) for pair in PAIRS}
        if any(plus.get(pair) for pair in PAIRS):
            splash = {pair: plus.get(pair, 0.0) for pair in PAIRS}
        if any(plain.get(trio) for trio in TRIOS):
            three = {trio: plain.get(trio, 0.0) for trio in TRIOS}
    total_split = sum(split.values()) or 1.0
    split = {key: value / total_split for key, value in split.items()}
    priors: dict[str, float] = {}
    for weights, kind in ((pure, "pure"), (splash, "splash"), (three, "three")):
        total = sum(weights.values())
        if total <= 0:
            continue
        for key, value in weights.items():
            share = split[kind] * value / total
            if kind == "splash":
                for color in COLORS:
                    if color not in key:
                        priors[f"{key}+{color}"] = share / 3.0
            else:
                priors[key] = share
    norm = sum(priors.values()) or 1.0
    return {key: priors[key] / norm for key in hypothesis_keys() if priors.get(key)}


def _lookup(card_lookup: Callable[[int], Any] | None, grp_id: int) -> dict[str, Any]:
    if card_lookup is None:
        return {}
    try:
        found = card_lookup(grp_id)
    except Exception:
        return {}
    if found is None:
        return {}
    if isinstance(found, dict):
        return found
    return {
        key: getattr(found, key, None)
        for key in ("mana_cost", "type_line", "oracle_text", "name", "expansion_code")
    }


def _fallback_cost(colors: str, cmc: Any) -> str:
    """A cost from colours and mana value when the card database is missing (loses hybrid)."""
    letters = _norm_colors(colors)
    generic = max(0, int(cmc or 0) - len(letters)) if isinstance(cmc, (int, float)) else 0
    return ("{%d}" % generic if generic else "") + "".join("{%s}" % c for c in letters)


def _pips_text(pips: tuple[frozenset[str], ...]) -> tuple[str, ...]:
    return tuple("".join(c for c in COLORS if c in pip) for pip in pips)


def _enters_text(cleaned: str) -> str:
    """A flash creature's enters-the-battlefield effect (with its bullets): what flashing it in does.

    Its activated abilities and other triggers are not a trick from the hand.
    """
    kept: list[str] = []
    keep = False
    for line in cleaned.splitlines():
        if line.startswith("•"):
            if keep:
                kept.append(line)
            continue
        keep = bool(re.match(r"when\b[^.]*\benters\b", line, re.IGNORECASE))
        if keep:
            kept.append(line)
    return "\n".join(kept)


def _trick_entry(grp_id: int, name: str, types: str, oracle: str, mana_cost: str) -> TrickCard | None:
    """The table entry of an instant-speed card, or None (not instant speed, or only 'other')."""
    types_lower = types.lower()
    if "land" in types_lower and "creature" not in types_lower:
        return None
    cleaned = clean_oracle(oracle)
    rules = _rules(oracle)
    instant = "instant" in types_lower
    flash = bool(_FLASH_LINE.search(cleaned.lower()))
    cost = _brace_cost(mana_cost)
    source = "instant" if instant else "flash" if flash else ""
    modes: list[TrickMode] = []
    if source:
        body = flash and not instant and "creature" in types_lower
        text = _enters_text(cleaned) if body else cleaned
        for mode in split_modes(text):
            modes.append(
                TrickMode(kinds=classify_effect(mode, name), text=mode[:100], target=mode_target(mode))
            )
        kinds = {k for mode in modes for k in mode.kinds} & SCORED_KINDS
        if body:
            kinds.add("flash_body")
    else:
        # A "{cost}, Discard this card: effect" ability works from the hand at instant speed.
        for ability_cost, effect in _activated_abilities(oracle):
            if "discard this card" not in ability_cost or "activate only as a sorcery" in effect:
                continue
            mana, _ = _cost_parts(ability_cost)
            modes.append(
                TrickMode(
                    kinds=classify_effect(effect, name, ability=True),
                    text=effect[:100],
                    target=mode_target(effect),
                )
            )
            cost = mana
            source = "hand"
            break
        kinds = {k for mode in modes for k in mode.kinds} & SCORED_KINDS
    if not source or not kinds:
        return None
    graveyard_cost, discard = "", False
    if source != "hand":
        match = _GRAVEYARD_CAST.search(rules)
        if match:
            graveyard_cost = match.group(1).upper()
            discard = bool(re.search(r"flashback[^\n]*discard", rules))
        elif re.search(r"\b(?:jump-start|retrace)\b", rules):
            graveyard_cost, discard = cost, True
    pips = _pips(cost)
    return TrickCard(
        grp_id=grp_id,
        name=name,
        mana_cost=cost,
        mv=_mana_value(cost),
        pips=_pips_text(pips),
        kinds=_ordered(kinds),
        modes=tuple(modes),
        source=source,
        graveyard_cost=graveyard_cost,
        graveyard_discard=discard,
    )


def build_trick_table(
    set_code: str,
    primer: SetPrimer,
    raw_ratings: list[dict],
    card_lookup: Callable[[int], Any] | None = None,
    *,
    color_ratings: list[dict] | None = None,
) -> TrickTable:
    """The set's trick table: instant-speed cards and their expected copies per hypothesis.

    ``card_lookup(grp_id)`` gives the exact mana cost (an MTGADatabase card or a
    dict); without it a card's colours stand in for its pips, which loses hybrid
    costs. ``color_ratings`` are 17Lands colour-rating rows (class split and
    splash/three-colour shares); the primer's pair shares are used otherwise.
    """
    games = {}
    for row in raw_ratings or []:
        grp = row.get("mtga_id")
        if isinstance(grp, int) and grp > 0:
            games[grp] = float(row.get("game_count") or 0)
    pair_share = {pair: float((primer.pair_stats.get(pair) or {}).get("share") or 0.0) for pair in PAIRS}
    priors = hypothesis_priors(pair_share, color_ratings)
    hypotheses = [_hypothesis(key) for key in priors]
    cards: dict[int, TrickCard] = {}
    costs: dict[int, str] = {}
    nonland: dict[int, tuple[frozenset[str], ...]] = {}
    for grp, card in sorted(primer.cards.items()):
        info = _lookup(card_lookup, grp)
        types = str(info.get("type_line") or card.types or "")
        cost = _brace_cost(info.get("mana_cost") or "") or _fallback_cost(card.colors, card.cmc)
        costs[grp] = cost
        lowered = types.lower()
        if "land" not in lowered or "creature" in lowered:
            nonland[grp] = _pips(cost)
        entry = _trick_entry(grp, card.name, types, card.oracle or str(info.get("oracle_text") or ""), cost)
        if entry is not None:
            cards[grp] = entry
    # a(c,h) = game_count_c * prior_h * d_h(c) / sum_h' prior_h' * d_h'(c)
    alloc: dict[str, dict[int, float]] = {h.key: {} for h in hypotheses}
    for grp, pips in nonland.items():
        weights = {h.key: priors[h.key] * h.density(pips) for h in hypotheses}
        total = sum(weights.values())
        if total <= 0 or games.get(grp, 0.0) <= 0:
            continue
        for key, weight in weights.items():
            if weight > 0:
                alloc[key][grp] = games[grp] * weight / total
    copies: dict[str, dict[int, float]] = {}
    for key, row in alloc.items():
        total = sum(row.values())
        if total <= 0:
            continue
        copies[key] = {
            grp: round(NONLAND_PER_DECK * value / total, 6)
            for grp, value in sorted(row.items())
            if grp in cards
        }
    return TrickTable(
        set_code=set_code.upper(),
        built_at=time.time(),
        primer_built_at=float(primer.built_at or 0.0),
        source="17lands_alloc",
        prior={key: round(value, 8) for key, value in priors.items()},
        cards=cards,
        copies=copies,
        set_grp_ids=frozenset(primer.cards),
        costs=costs,
    )


# --- runtime estimate ----------------------------------------------------------------


@dataclass
class TrickRisk:
    known: bool = False
    p_any: float = 0.0
    p_hand: float = 0.0  # hidden hand only
    q_by_class: dict[str, float] = field(default_factory=dict)  # display only
    top: list[tuple[str, str, float]] = field(default_factory=list)  # (name, kind, expected copies in hand)
    certain: list[dict] = field(default_factory=list)  # visible options, q = 1
    deck_size_assumed: bool = False
    deck_size: int = DECK_REFERENCE
    set_code: str = ""
    open_mana: int = 0
    open_colors: tuple[str, ...] = ()
    hand: int = 0
    hand_unknown: int = 0
    our_turn: bool = False
    relevant: tuple[str, ...] = ()  # kinds counted in p_any at this timing
    pairs: list[tuple[str, float]] = field(default_factory=list)  # top deck hypotheses
    signals: list[str] = field(default_factory=list)  # telemetry only (likelihood ratio 1.0)

    def _dominant(self) -> str:
        candidates = [(q, kind) for kind, q in self.q_by_class.items() if kind in self.relevant and q > 0]
        if not candidates:
            return ""
        best = max(q for q, _ in candidates)
        return next(kind for kind in KINDS if (best, kind) in candidates)

    def _advice(self, kind: str) -> str:
        if kind in ("pump", "keyword", "protect", "shrink"):
            return (
                "avoid attacks that lose a key creature to a pump"
                if self.our_turn
                else "avoid blocks that lose a key creature to a pump"
            )
        if kind in ("removal", "bounce"):
            return (
                "don't stack auras or pumps on one creature"
                if self.our_turn
                else "a blocker may still be removed after blocks"
            )
        return {
            "counter": "expect a possible counterspell",
            "flash_body": "expect a possible flash blocker",
            "fog": "don't rely on combat damage alone",
            "tap": "expect a possible tap effect",
        }.get(kind, "play normally")

    def verdict_line(self) -> str:
        """Planner text (<= 160 characters, no card names), '' when not known."""
        if not self.known:
            return ""
        visible = [c for c in self.certain if c.get("relevant")]
        pct = _percent(self.p_hand)
        if visible:
            first = visible[0]
            text = (
                f"Opponent has a visible instant-speed {_KIND_WORDS.get(first['kind'], first['kind'])} "
                f"({first['where']}, {self.open_mana} open): assume it is used; hidden-hand risk {pct}"
            )
        elif self.p_hand <= 0.0 and self.open_mana == 0:
            text = "Opponent tapped out: no instant-speed interaction"
        elif self.p_hand <= 0.0 and self.hand_unknown == 0:
            text = "Opponent has no unknown cards in hand: no hidden instant-speed interaction"
        elif self.p_hand <= 0.0:
            text = f"Opponent has no instant-speed interaction castable with {self.open_mana} open: play normally"
        else:
            base = f"{pct}; {self.open_mana} open, {self.hand} card{'s' if self.hand != 1 else ''}"
            kind = self._dominant()
            if self.p_hand < 0.10:
                text = f"Opponent interaction risk low ({base}): play normally"
            elif self.p_hand < 0.25:
                text = (
                    f"Opponent interaction risk moderate ({base}, mostly {_KIND_WORDS.get(kind, kind)}): "
                    f"play normally but {self._advice(kind)}"
                )
            else:
                text = (
                    f"Opponent interaction risk elevated ({base}, mostly {_KIND_WORDS.get(kind, kind)}): "
                    f"{self._advice(kind)}"
                )
        return text[:160]

    def ui_line(self) -> str:
        """Human-facing line with card names (<= 220 characters), '' when not known."""
        if not self.known:
            return ""
        colors = " ".join(self.open_colors) if self.open_colors else "-"
        head = f"Opp tricks {_percent(self.p_any)}: {self.open_mana} open ({colors}), {self.hand} in hand"
        if self.pairs:
            head += f", {self.pairs[0][0]} {round(self.pairs[0][1] * 100)}%"
        parts = [head]
        visible = [c for c in self.certain if c.get("relevant")]
        if visible:
            parts.append("visible: " + ", ".join(f"{c['name']} ({c['kind']})" for c in visible[:2]))
        if self.top:
            parts.append(", ".join(f"{name} ({kind}) {copies:.2f}" for name, kind, copies in self.top[:3]))
        text = " · ".join(parts)
        if len(text) > 220:
            text = text[:217].rsplit(" ", 1)[0] + "..."
        return text

    def summary(self) -> str:
        """One telemetry line ('Trick risk ...' in the coach log)."""
        if not self.known:
            return "unknown"
        deck = f"D={self.deck_size}{'(assumed)' if self.deck_size_assumed else ''}"
        pairs = ",".join(f"{key}:{value:.2f}" for key, value in self.pairs[:3])
        top = ",".join(f"{name}:{kind}:{copies:.3f}" for name, kind, copies in self.top[:4])
        certain = ",".join(f"{c['name']}:{c['kind']}" for c in self.certain[:3])
        return (
            f"p_any={self.p_any:.3f} p_hand={self.p_hand:.3f} open={self.open_mana}[{''.join(self.open_colors)}] "
            f"hand={self.hand} {deck} set={self.set_code} pairs={pairs} top={top}"
            + (f" certain={certain}" if certain else "")
            + (f" signals={';'.join(self.signals)}" if self.signals else "")
        )

    def as_payload(self) -> dict[str, Any]:
        """JSON-safe; card names allowed (UI)."""
        return {
            "known": self.known,
            "p_any": round(self.p_any, 4),
            "p_hand": round(self.p_hand, 4),
            "q_by_class": {kind: round(q, 4) for kind, q in self.q_by_class.items()},
            "top": [
                {"name": name, "kind": kind, "copies": round(copies, 4)} for name, kind, copies in self.top
            ],
            "certain": [dict(c) for c in self.certain],
            "deck_size": self.deck_size,
            "deck_size_assumed": self.deck_size_assumed,
            "set_code": self.set_code,
            "open_mana": self.open_mana,
            "open_colors": list(self.open_colors),
            "hand": self.hand,
            "relevant": list(self.relevant),
            "pairs": [[key, round(value, 4)] for key, value in self.pairs],
            "signals": list(self.signals),
            "verdict": self.verdict_line(),
            "ui_line": self.ui_line(),
        }


def _percent(p: float) -> str:
    if p <= 0.0:
        return "0%"
    if p < 0.01:
        return "<1%"
    return f"~{round(p * 100)}%"


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _seats(state: dict) -> tuple[int | None, int | None]:
    players = [p for p in state.get("players") or [] if isinstance(p, dict)]
    local = state.get("local_seat_id") or next((p.get("seat_id") for p in players if p.get("is_local")), None)
    opponent = state.get("opponent_seat_id") or next(
        (p.get("seat_id") for p in players if p.get("seat_id") not in (None, local)), None
    )
    return local, opponent


def _types(card: dict) -> str:
    return f"{card.get('type_line') or ''} {' '.join(str(t) for t in card.get('card_types') or [])}".lower()


def _is_token(card: dict) -> bool:
    return (
        str(card.get("object_kind") or "").upper() == "TOKEN"
        or "token" in str(card.get("type_line") or "").lower()
    )


def _is_land(card: dict) -> bool:
    types = _types(card)
    return "land" in types and "creature" not in types


def _owner(card: dict) -> Any:
    return card.get("owner_seat_id", card.get("controller_seat_id"))


def _controller(card: dict) -> Any:
    return card.get("controller_seat_id", card.get("owner_seat_id"))


def _is_ability(card: dict) -> bool:
    return (
        str(card.get("object_kind") or "").upper() == "ABILITY"
        or str(card.get("type_line") or "").startswith("Ability")
        or str(card.get("name") or "").startswith("Ability (")
    )


def source_colors(card: dict) -> frozenset[str]:
    """Colours a mana source makes: color_production (names or ManaColor digits), else its text."""
    produced = set()
    for raw in card.get("color_production") or []:
        color = _COLOR_NAMES.get(str(raw).strip().lower().removeprefix("manacolor_"))
        if color:
            produced.add(color)
    if produced:
        return frozenset(produced)
    return frozenset(_land_colors(card)) or frozenset({"C"})


def _is_limited(state: dict) -> bool:
    event = str(state.get("event_id") or state.get("format_name") or state.get("event_name") or "")
    profile = detect_format_profile(state, event_name=event)
    if profile.family == "limited":
        return True
    if profile.has_command_zone:
        return False
    # detect_format_profile calls any game with a life total >= 25 Brawl; a
    # 40-45 card deck list or a Draft/Sealed event still means Limited.
    deck = state.get("deck_cards")
    if isinstance(deck, list) and 40 <= len(deck) <= 45:
        return True
    return bool(re.search(r"Draft|Sealed|Jump_In|Cube", event, re.IGNORECASE))


def _phase_step(state: dict) -> tuple[str, str]:
    from arenamcp.board_model import canonical_phase_step

    return canonical_phase_step(state.get("turn") or {})


def relevant_kinds(state: dict) -> frozenset[str]:
    """The kinds that matter at this timing (critique A5).

    Our turn: everything, but a tap effect stops mattering once attackers are
    declared (tapping an attacker doesn't remove it) and a flash blocker once
    blocks are declared. Their turn: combat tricks and removal, plus taps until
    blocks (they can tap a would-be blocker); fog, flash bodies and counters
    don't touch their own attack.
    """
    local, _ = _seats(state)
    turn = state.get("turn") or {}
    _, step = _phase_step(state)
    after_blocks = step in _AFTER_BLOCKS
    attackers_declared = after_blocks or step == "Step_DeclareAttack"
    if turn.get("active_player") == local:
        kinds = set(SCORED_KINDS)
        if attackers_declared:
            kinds.discard("tap")
        if after_blocks:
            kinds.discard("flash_body")
        return frozenset(kinds)
    kinds = set(_COMBAT_KINDS)
    if not after_blocks:
        kinds.add("tap")
    return frozenset(kinds)


def _mana_sources(state: dict, opponent: Any) -> list[SimpleNamespace]:
    """The opponent's open mana: untapped sources that can tap now, then their pool."""
    turn = state.get("turn") or {}
    number = _int(turn.get("turn_number")) or 0
    their_turn = turn.get("active_player") == opponent
    # Their latest turn began at `number` (their turn) or `number - 1` (ours); a
    # creature that entered at or after it is still summoning sick.
    latest = number if their_turn else number - 1
    sources = []
    for card in state.get("battlefield") or []:
        if not isinstance(card, dict) or _controller(card) != opponent or card.get("is_tapped"):
            continue
        if card.get("is_phased_out"):
            continue
        text = _rules(str(card.get("oracle_text") or ""))
        is_land = _is_land(card)
        if not is_land and not re.search(r"\{t\}[^:\n]*:\s*add\b", text):
            continue
        if "creature" in _types(card):
            entered = _int(card.get("turn_entered_battlefield"))
            haste = "haste" in [str(k).lower() for k in card.get("keywords") or []] or re.search(
                r"(?m)^haste\b", text
            )
            if entered is not None and entered >= 0 and entered >= latest and not haste:
                continue
        sources.append(
            SimpleNamespace(
                produces=source_colors(card),
                name=str(card.get("name") or ""),
                instance_id=card.get("instance_id"),
                # Heartwood Crafter, Gideon's Memorial: their mana can't cast spells from hand.
                restricted=bool(_RESTRICTED_MANA.search(text)),
            )
        )
    player = next(
        (p for p in state.get("players") or [] if isinstance(p, dict) and p.get("seat_id") == opponent), {}
    )
    pool = player.get("mana_pool") or {}
    if isinstance(pool, dict):
        entries = list(pool.items())
    elif isinstance(pool, list):
        entries = [(item, 1) for item in pool]
    else:
        entries = []
    for raw, amount in entries:
        key = str(raw).strip().lower().removeprefix("manacolor_").removeprefix("mana_")
        color = _COLOR_NAMES.get(key, "C")  # unknown names still pay generic costs
        count = int(amount) if isinstance(amount, float) else _int(amount)
        for _ in range(max(0, count or 0)):
            sources.append(
                SimpleNamespace(
                    produces=frozenset({color}), name="mana pool", instance_id=None, restricted=False
                )
            )
    return sources


_RESTRICTED_MANA = re.compile(r"\bspend this mana only\b|\bcan't be spent to cast\b")


def _spell_sources(sources: list[SimpleNamespace]) -> list[SimpleNamespace]:
    """The sources whose mana can cast a spell (restricted mana pays only activated abilities)."""
    return [s for s in sources if not getattr(s, "restricted", False)]


def _castable(mv: int, pips: tuple[frozenset[str], ...], sources: list[SimpleNamespace]) -> bool:
    return mv <= len(sources) and _pip_matching(pips, sources)


def _public_cards(state: dict, opponent: Any) -> list[dict]:
    """The opponent's own non-token cards in public zones (each physical card once)."""
    cards = []
    for zone in ("battlefield", "graveyard", "exile", "stack"):
        for card in state.get(zone) or []:
            if not isinstance(card, dict) or _is_token(card) or _is_ability(card):
                continue
            if _owner(card) == opponent:
                cards.append(card)
    return cards


def _spell_pips(card: dict, table: TrickTable) -> tuple[frozenset[str], ...] | None:
    cost = _brace_cost(card.get("mana_cost") or "")
    if not cost:
        grp = _int(card.get("grp_id"))
        cost = table.costs.get(grp, "") if grp else ""
    if cost:
        return _pips(cost)
    colors = {_COLOR_NAMES.get(str(c).strip().lower()) for c in card.get("colors") or []} - {None, "C"}
    return tuple(frozenset({c}) for c in sorted(colors)) if colors else None


def _posterior(table: TrickTable, evidence: list[tuple[str, tuple[frozenset[str], ...]]]) -> dict[str, float]:
    """post_h from prior_h and each public land ('land', its colours) or spell ('spell', its pips)."""
    logs: dict[str, float] = {}
    for key, prior in table.prior.items():
        if prior <= 0:
            continue
        hyp = _hypothesis(key)
        total = math.log(prior)
        for kind, colors in evidence:
            if kind == "land":
                weight = max(hyp.weight(pip, OFF_COLOR_LAND) for pip in colors)
            else:
                weight = min(hyp.weight(pip, OFF_COLOR_SPELL) for pip in colors)
            total += math.log(weight / hyp.norm)
        logs[key] = total
    if not logs:
        return {}
    peak = max(logs.values())
    weights = {key: math.exp(value - peak) for key, value in logs.items()}
    norm = sum(weights.values())
    return {key: value / norm for key, value in weights.items()}


def _evidence(cards: list[dict], table: TrickTable) -> list[tuple[str, tuple[frozenset[str], ...]]]:
    evidence: list[tuple[str, tuple[frozenset[str], ...]]] = []
    for card in cards:
        if _is_land(card):
            colors = source_colors(card) - {"C"}
            if colors:
                evidence.append(("land", (frozenset(colors),)))
        else:
            pips = _spell_pips(card, table)
            if pips:
                evidence.append(("spell", pips))
    return evidence


def deck_posterior(state: dict, table: TrickTable) -> dict[str, float]:
    """post_h over the table's deck hypotheses from the opponent's public lands and
    spells (and cards revealed in their hand); behaviour signals have ratio 1.0."""
    _, opponent = _seats(state)
    cards = [*_public_cards(state, opponent), *_revealed_in_hand(state, opponent)]
    return _posterior(table, _evidence(cards, table))


def _shrink_weight(deck_size: int) -> float:
    return min(1.0, max(0.0, (deck_size - 42) / 18.0))


def _card_text(card: dict) -> str:
    return str(card.get("oracle_text") or "")


def _certain_options(
    state: dict,
    table: TrickTable,
    opponent: Any,
    sources: list[SimpleNamespace],
    hand: int,
    revealed: list[dict],
    relevant: frozenset[str],
) -> list[dict]:
    """Visible instant-speed options the opponent can afford now (q = 1)."""
    turn = state.get("turn") or {}
    number = _int(turn.get("turn_number")) or 0
    their_turn = turn.get("active_player") == opponent
    latest = number if their_turn else number - 1
    found: list[dict] = []

    def add(name: str, kinds: Iterable[str], where: str, cost: str) -> None:
        scored = _ordered(set(kinds) & SCORED_KINDS)
        if not scored:
            return
        hit = [k for k in scored if k in relevant]
        found.append(
            {
                "name": name,
                "kind": (hit or scored)[0],
                "kinds": list(scored),
                "where": where,
                "cost": cost,
                "relevant": bool(hit),
            }
        )

    def affordable(cost: str, without: Any = None, *, spell: bool = False) -> bool:
        pool = [
            s
            for s in (_spell_sources(sources) if spell else sources)
            if without is None or s.instance_id != without
        ]
        return _castable(_mana_value(cost), _pips(cost), pool)

    graveyard_size = sum(
        1 for c in state.get("graveyard") or [] if isinstance(c, dict) and _owner(c) == opponent
    )
    creatures = [
        c
        for c in state.get("battlefield") or []
        if isinstance(c, dict) and _controller(c) == opponent and "creature" in _types(c)
    ]

    def condition_met(effect: str) -> bool:
        """'Activate only if ...': a graveyard threshold is checked; any other condition fails."""
        match = re.search(r"activate only if ([^.]*)", effect)
        if not match:
            return True
        need = re.match(r"there are (\w+) or more cards in your graveyard", match.group(1).strip())
        if not need:
            return False
        count = _NUMBER_WORDS.get(need.group(1), _int(need.group(1)) or 99)
        return graveyard_size >= count

    for card in state.get("battlefield") or []:
        if not isinstance(card, dict) or _controller(card) != opponent:
            continue
        name = str(card.get("name") or "a permanent")
        entered = _int(card.get("turn_entered_battlefield"))
        sick = (
            "creature" in _types(card)
            and entered is not None
            and entered >= latest
            and "haste" not in [str(k).lower() for k in card.get("keywords") or []]
        )
        for cost, effect in _activated_abilities(_card_text(card)):
            if "discard this card" in cost or "from your graveyard" in cost:
                continue
            if "activate only as a sorcery" in effect or (
                "only during your turn" in effect and not their_turn
            ):
                continue
            if not condition_met(effect):
                continue
            if re.search(r"\bsacrifice another creature\b", cost) and not any(
                c.get("instance_id") != card.get("instance_id") for c in creatures
            ):
                continue
            mana, taps = _cost_parts(cost)
            if taps and (card.get("is_tapped") or sick):
                continue
            if "discard a card" in cost and hand <= 0:
                continue
            if affordable(mana, card.get("instance_id") if taps else None):
                add(name, classify_effect(effect, name, ability=True), "on board", mana)
    for card in state.get("graveyard") or []:
        if not isinstance(card, dict) or _owner(card) != opponent:
            continue
        name = str(card.get("name") or "a card")
        entry = table.cards.get(_int(card.get("grp_id")) or -1)
        if entry is None:
            text = _card_text(card)
            entry = (
                _trick_entry(0, name, _types(card), text, str(card.get("mana_cost") or "")) if text else None
            )
        if entry is not None and entry.graveyard_cost and entry.source != "hand":
            if (not entry.graveyard_discard or hand > 0) and affordable(entry.graveyard_cost, spell=True):
                add(name, entry.kinds, "graveyard", entry.graveyard_cost)
        for cost, effect in _activated_abilities(_card_text(card)):
            if "from your graveyard" not in cost or "activate only as a sorcery" in effect:
                continue
            mana, _ = _cost_parts(cost)
            if affordable(mana):
                add(name, classify_effect(effect, name, ability=True), "graveyard", mana)
    for card in revealed:
        entry = table.cards.get(_int(card.get("grp_id")) or -1)
        if entry is not None and _castable(entry.mv, entry.pip_sets, _spell_sources(sources)):
            add(entry.name, entry.kinds, "revealed in hand", entry.mana_cost)
    return found


def _revealed_in_hand(state: dict, opponent: Any) -> list[dict]:
    """WP6's revealed_cards ({instance_id, grp_id, name, zone}) still in the opponent's hand.

    Absent from today's snapshots (server.get_game_state drops the field).
    """
    result = []
    for card in state.get("revealed_cards") or []:
        if not isinstance(card, dict):
            continue
        zone = str(card.get("zone") or "hand").lower()
        if card.get("owner_seat_id", opponent) == opponent and "hand" in zone:
            result.append(card)
    return result


def _signals(state: dict, opponent: Any, hand: int) -> list[str]:
    """Telemetry-only behaviour facts (their likelihood ratios stay 1.0 until fitted)."""
    turn = state.get("turn") or {}
    number = _int(turn.get("turn_number")) or 0
    # Their last COMPLETED turn: during their combat no land yet is normal (A6).
    last = number - 2 if turn.get("active_player") == opponent else number - 1
    signals = []
    if last >= 1 and hand > 0:
        entered = {
            _int(c.get("turn_entered_battlefield"))
            for c in state.get("battlefield") or []
            if isinstance(c, dict) and _controller(c) == opponent and _is_land(c)
        }
        if entered and last not in entered:
            signals.append(f"missed_land_drop: no land on their turn {last}")
    return signals


def _our_targets(state: dict, local: Any) -> dict[str, Any]:
    """What our creatures offer a restricted mode: any flier, any combatant (or combat still to
    come this turn), and the colours they have."""
    ours = [
        c
        for c in state.get("battlefield") or []
        if isinstance(c, dict) and _controller(c) == local and "creature" in _types(c)
    ]
    phase, _step = _phase_step(state)
    colors: set[str] = set()
    for card in ours:
        named = {_COLOR_NAMES.get(str(c).strip().lower()) for c in card.get("colors") or []} - {None, "C"}
        colors |= named or {c for c in "WUBRG" if c in str(card.get("mana_cost") or "").upper()}
    return {
        "any": bool(ours),
        "flying": any(
            "flying" in [str(k).lower() for k in c.get("keywords") or []]
            or re.search(r"(?m)^flying\b", _rules(str(c.get("oracle_text") or "")))
            for c in ours
        ),
        "combat": any(c.get("is_attacking") or c.get("is_blocking") for c in ours)
        or (bool(ours) and phase not in ("Phase_Main2", "Phase_Ending")),
        "colors": colors,
    }


def _kinds_now(card: TrickCard, targets: dict[str, Any]) -> tuple[str, ...]:
    """The card's scored kinds from the modes that have a legal target among our creatures now."""
    if not card.modes or not any(mode.target for mode in card.modes):
        return card.kinds
    kinds: set[str] = set()
    for mode in card.modes:
        need = mode.target
        if need == "flying" and not targets["flying"]:
            continue
        if need == "combat" and not targets["combat"]:
            continue
        if need.startswith("colors:") and not set(need[7:]) & targets["colors"]:
            continue
        kinds |= set(mode.kinds)
    if card.source == "flash" and "flash_body" in card.kinds:
        kinds.add("flash_body")
    return _ordered(kinds & SCORED_KINDS)


def _apply_lr(p: float, ratio: float) -> float:
    """p after a likelihood ratio in odds space."""
    if ratio == 1.0 or p <= 0.0 or p >= 1.0:
        return p
    odds = p / (1.0 - p) * ratio
    return odds / (1.0 + odds)


def trick_risk(
    state: dict | None, table: TrickTable | None = None, *, deck_size: int | None = None
) -> TrickRisk:
    """The opponent's instant-speed interaction risk right now; known=False when out of scope.

    Pure: no I/O, no logging except one warning per distinct failure.
    """
    try:
        return _trick_risk(state, table, deck_size)
    except Exception as error:  # never break a decision on an advisory fact
        _log_once(type(error).__name__, "trick_risk failed: %s", error)
        return TrickRisk(known=False)


def _trick_risk(state: dict | None, table: TrickTable | None, deck_size: int | None) -> TrickRisk:
    if not isinstance(state, dict) or table is None or not table.prior:
        return TrickRisk(known=False)
    if not _is_limited(state):
        return TrickRisk(known=False)
    local, opponent = _seats(state)
    if opponent is None:
        return TrickRisk(known=False)
    zones = state.get("zones") if isinstance(state.get("zones"), dict) else {}
    hand = _int(zones.get("opponent_hand_count"))
    if hand is None or hand < 0:
        return TrickRisk(known=False)
    revealed = _revealed_in_hand(state, opponent)
    hand_unknown = max(0, hand - len(revealed))
    sources = _mana_sources(state, opponent)
    relevant = relevant_kinds(state)
    public = _public_cards(state, opponent)

    library = _int(zones.get("opponent_library_count"))
    assumed = False
    if deck_size is None:
        if library is not None and library >= 0:
            deck_size = library + hand + len(public)
        else:
            deck_size, assumed = DECK_REFERENCE, True
    shrink = _shrink_weight(deck_size)
    posterior = _posterior(table, _evidence([*public, *revealed], table))

    spell_sources = _spell_sources(sources)
    castable = {
        grp: card for grp, card in table.cards.items() if _castable(card.mv, card.pip_sets, spell_sources)
    }
    # A mode whose target must fly / attack or block / be a colour counts only when one of ours qualifies.
    targets = _our_targets(state, local)
    kinds_now = {grp: _kinds_now(card, targets) for grp, card in castable.items()}
    # lambda_{c,h} = hand_unknown * RHO * K_h(c) / 40, K shrunk toward the generic
    # density of its kind for a big deck.
    p_hand = 0.0
    q_by_class = dict.fromkeys(_ordered(SCORED_KINDS), 0.0)
    expected: Counter[int] = Counter()
    for key, post in posterior.items():
        row = table.copies.get(key) or {}
        totals = Counter()
        for grp, k in row.items():
            card = table.cards.get(grp)
            if card is not None:
                totals[card.kind] += k
        rates: Counter[str] = Counter()
        relevant_rate = 0.0
        for grp, card in castable.items():
            k = row.get(grp, 0.0)
            if k <= 0 or not kinds_now[grp]:
                continue
            if shrink > 0 and totals[card.kind] > 0:
                k *= (1.0 - shrink) + shrink * GENERIC_COPIES_PER_40.get(card.kind, 0.0) / totals[card.kind]
            lam = hand_unknown * RHO * k / DECK_REFERENCE
            expected[grp] += post * lam
            for kind in kinds_now[grp]:
                rates[kind] += lam
            if relevant.intersection(kinds_now[grp]):
                relevant_rate += lam
        p_hand += post * (1.0 - math.exp(-relevant_rate))
        for kind, rate in rates.items():
            q_by_class[kind] += post * (1.0 - math.exp(-rate))

    signals = _signals(state, opponent, hand)
    for signal in signals:
        p_hand = _apply_lr(p_hand, BEHAVIOUR_LR.get(signal.split(":", 1)[0], 1.0))
    certain = _certain_options(state, table, opponent, sources, hand, revealed, relevant)
    for option in certain:
        for kind in option["kinds"]:
            q_by_class[kind] = 1.0
    p_any = 1.0 if any(option["relevant"] for option in certain) else p_hand
    top = sorted(
        (
            (
                castable[grp].name,
                next((k for k in kinds_now[grp] if k in relevant), kinds_now[grp][0]),
                value,
            )
            for grp, value in expected.items()
            if value > 0 and relevant.intersection(kinds_now[grp])
        ),
        key=lambda row: (-row[2], row[0]),
    )[:6]
    if assumed:
        signals.append("deck size assumed 40")
    # One symbol per open mana: its colour, or "*" for a source of several colours.
    open_colors = tuple(
        sorted(
            (next(iter(s.produces)) if len(s.produces) == 1 else "*" for s in spell_sources),
            key="WUBRGC*".index,
        )
    )
    return TrickRisk(
        known=True,
        p_any=min(1.0, max(0.0, p_any)),
        p_hand=min(1.0, max(0.0, p_hand)),
        q_by_class={kind: q for kind, q in q_by_class.items() if q > 0},
        top=[(name, kind, round(value, 6)) for name, kind, value in top],
        certain=certain,
        deck_size_assumed=assumed,
        deck_size=int(deck_size),
        set_code=table.set_code,
        open_mana=len(spell_sources),
        open_colors=open_colors,
        hand=hand,
        hand_unknown=hand_unknown,
        our_turn=(state.get("turn") or {}).get("active_player") == local,
        relevant=_ordered(relevant),
        pairs=sorted(posterior.items(), key=lambda kv: (-kv[1], kv[0]))[:3],
        signals=signals,
    )


def observed_tricks(previous: dict | None, state: dict | None) -> list[str]:
    """Opponent instant-speed cards new on the stack or in the graveyard since ``previous``.

    For the 'Trick observed' telemetry line. A card that moves stack -> graveyard
    (new instance id) is reported once, from the stack.
    """
    if not isinstance(state, dict):
        return []
    _, opponent = _seats(state)
    before = previous if isinstance(previous, dict) else {}
    seen_ids = {
        c.get("instance_id")
        for z in ("stack", "graveyard")
        for c in before.get(z) or []
        if isinstance(c, dict)
    }
    seen_stack_names = {c.get("name") for c in before.get("stack") or [] if isinstance(c, dict)}
    names = []
    for zone in ("stack", "graveyard"):
        for card in state.get(zone) or []:
            if not isinstance(card, dict) or card.get("instance_id") in seen_ids or _is_ability(card):
                continue
            if _owner(card) != opponent and _controller(card) != opponent:
                continue
            if zone == "graveyard" and card.get("name") in seen_stack_names:
                continue
            text = clean_oracle(_card_text(card)).lower()
            if "instant" in _types(card) or _FLASH_LINE.search(text):
                name = str(card.get("name") or "an instant")
                if name not in names:
                    names.append(name)
    return names


# --- set code ----------------------------------------------------------------------


def _set_from_event(raw: Any, known: Iterable[str] = ()) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    joined = "_".join(text.split())
    candidates = [part for part in joined.split("_") if len(part) == 3 and part.isupper()]
    known_set = {code.upper() for code in known}
    preferred = next((code for code in candidates if code in known_set), "")
    return preferred or extract_set_code(joined)


def resolve_set_code(
    state: dict | None,
    *,
    tables: Iterable[TrickTable] = (),
    card_lookup: Callable[[int], Any] | None = None,
) -> str:
    """The match's set: event id, else the majority expansion of our non-basic deck
    cards, else of the opponent's non-basic, non-token public cards.

    Memory-only unless ``card_lookup`` is given: without it, grp ids are matched
    against the registered tables' card lists (basics are never in them; FRA's
    basics carry expansion FIN, so they are skipped either way).
    """
    if not isinstance(state, dict):
        return ""
    tables = list(tables)
    codes = [t.set_code for t in tables]
    for raw in (state.get("event_id"), state.get("format_name"), state.get("event_name")):
        code = _set_from_event(raw, codes)
        if code:
            return code

    def vote(grp_ids: Iterable[int]) -> str:
        counts: Counter[str] = Counter()
        for grp in grp_ids:
            if card_lookup is not None:
                info = _lookup(card_lookup, grp)
                code = str(info.get("expansion_code") or "").upper()
                if code and "basic" not in str(info.get("type_line") or "").lower():
                    counts[code] += 1
            else:
                for table in tables:
                    if grp in table.set_grp_ids:
                        counts[table.set_code] += 1
        return min(counts, key=lambda code: (-counts[code], code)) if counts else ""

    deck = [g for g in (_int(x) for x in state.get("deck_cards") or []) if g]
    code = vote(deck)
    if code:
        return code
    _, opponent = _seats(state)
    public = [
        _int(c.get("grp_id"))
        for c in _public_cards(state, opponent)
        if "basic" not in str(c.get("type_line") or "").lower()
    ]
    return vote(g for g in public if g)


# --- table service ---------------------------------------------------------------------


class TrickTableService:
    """Builds each set's TrickTable once on a background thread and serves it from memory.

    ``get`` never touches the disk or the network; ``ensure`` returns at once and
    loads ~/.arenamcp/cache/trick_tables/{SET}.json or builds it (which may
    download 17Lands data) on its own thread. A cached table is rebuilt when its
    version differs, it is older than PRIMER_MAX_AGE_S (7 days), or the set primer
    is newer than the one it was built from.
    """

    _shared: TrickTableService | None = None
    _shared_lock = threading.Lock()
    RETRY_AFTER_S = 900.0

    def __init__(
        self,
        *,
        primer_fn: Callable[[str], SetPrimer | None] | None = None,
        ratings_fn: Callable[[str], list[dict]] | None = None,
        color_ratings_fn: Callable[[str], list[dict] | None] | None = None,
        card_lookup: Callable[[int], Any] | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self._primer_fn = primer_fn or _default_primer
        self._ratings_fn = ratings_fn or _default_ratings
        self._color_ratings_fn = color_ratings_fn or _default_color_ratings
        self._card_lookup = card_lookup
        self._dir = cache_dir or TRICK_DIR
        self._lock = threading.Lock()
        self._tables: dict[str, TrickTable] = {}
        self._building: dict[str, threading.Thread] = {}
        self._failed: dict[str, float] = {}
        # Set-code resolution with the card database, per (match, our deck): its
        # result (code, or "" with the time it failed) and the one resolver thread.
        self._resolved: dict[tuple, tuple[str, float]] = {}
        self._resolver: threading.Thread | None = None

    @classmethod
    def shared(cls) -> TrickTableService:
        with cls._shared_lock:
            if cls._shared is None:
                cls._shared = cls()
            return cls._shared

    def _lookup(self) -> Callable[[int], Any] | None:
        if self._card_lookup is None:
            try:
                from arenamcp.mtgadb import MTGADatabase

                self._card_lookup = MTGADatabase().get_card
            except Exception as error:
                logger.info("Trick tables: no card database (%s); hybrid costs fall back to colours", error)
                self._card_lookup = lambda _grp: None
        return self._card_lookup

    def _path(self, set_code: str) -> Path:
        return self._dir / f"{set_code.upper()}.json"

    def register(self, table: TrickTable) -> None:
        """Put a table in memory (test hook; also what a finished build does)."""
        with self._lock:
            self._tables[table.set_code.upper()] = table

    def tables(self) -> list[TrickTable]:
        with self._lock:
            return list(self._tables.values())

    def get(self, set_code: str | None) -> TrickTable | None:
        """Memory only."""
        if not set_code:
            return None
        with self._lock:
            return self._tables.get(set_code.upper())

    def get_for_state(self, state: dict | None) -> TrickTable | None:
        """Memory only: the table for the state's set (event id or grp-id vote)."""
        return self.get(resolve_set_code(state, tables=self.tables()))

    def ensure(self, set_code: str | None) -> None:
        """Start loading or building the set's table; returns immediately."""
        if not set_code:
            return
        key = set_code.upper()
        with self._lock:
            table = self._tables.get(key)
            if table is not None and time.time() - table.built_at <= PRIMER_MAX_AGE_S:
                return
            worker = self._building.get(key)
            if worker is not None and worker.is_alive():
                return
            failed = self._failed.get(key)
            if failed is not None and time.monotonic() - failed < self.RETRY_AFTER_S:
                return
            worker = threading.Thread(
                target=self._load_or_build, args=(key,), daemon=True, name=f"tricks-{key}"
            )
            self._building[key] = worker
            worker.start()

    def ensure_for_state(self, state: dict | None) -> None:
        """``ensure`` for the state's set; resolves it with the card database off-thread if needed.

        Limited only (a Constructed game never builds a table). A Cube or
        Jump_In event without a set code in its id has no single-set table.
        The card-database vote runs at most once per match and deck, on one
        thread at a time; a failed vote is retried after RETRY_AFTER_S.
        """
        if not isinstance(state, dict) or not _is_limited(state):
            return
        code = resolve_set_code(state, tables=self.tables())
        if code:
            self.ensure(code)
            return
        event = str(state.get("event_id") or state.get("format_name") or state.get("event_name") or "")
        if re.search(r"Cube|Jump_?In", event, re.IGNORECASE):
            return
        key = (
            str(state.get("match_id") or ""),
            tuple(g for g in (_int(x) for x in state.get("deck_cards") or []) if g),
        )
        with self._lock:
            found = self._resolved.get(key)
            if found is not None and not found[0] and time.monotonic() - found[1] < self.RETRY_AFTER_S:
                return  # resolved to nothing recently
            if found is None or not found[0]:
                if self._resolver is not None and self._resolver.is_alive():
                    return  # one resolver at a time
        if found is not None and found[0]:
            self.ensure(found[0])
            return
        snapshot = {
            key: state.get(key)
            for key in (
                "deck_cards",
                "battlefield",
                "graveyard",
                "exile",
                "stack",
                "players",
                "opponent_seat_id",
            )
        }
        snapshot["local_seat_id"] = state.get("local_seat_id")

        def resolve() -> None:
            try:
                found = resolve_set_code(snapshot, card_lookup=self._lookup())
            except Exception as error:
                logger.info("Trick tables: set code not resolved: %s", error)
                found = ""
            with self._lock:
                self._resolved[key] = (found, time.monotonic())
            if found:
                self.ensure(found)

        with self._lock:
            if self._resolver is not None and self._resolver.is_alive():
                return
            self._resolver = threading.Thread(target=resolve, daemon=True, name="tricks-resolve")
            self._resolver.start()

    def wait(self, set_code: str, timeout: float | None = None) -> TrickTable | None:
        """Block until a running build finishes (tools and tests only)."""
        with self._lock:
            worker = self._building.get(set_code.upper())
        if worker is not None:
            worker.join(timeout)
        return self.get(set_code)

    def build_now(self, set_code: str) -> TrickTable | None:
        self._load_or_build(set_code.upper())
        return self.get(set_code)

    def _load(self, key: str, primer: SetPrimer | None) -> TrickTable | None:
        path = self._path(key)
        try:
            table = TrickTable.from_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, KeyError) as error:
            if path.exists():
                logger.info("Ignoring unreadable trick table %s: %s", path, error)
            return None
        if table.version != TRICK_TABLE_VERSION or time.time() - table.built_at > PRIMER_MAX_AGE_S:
            return None
        if primer is not None and float(primer.built_at or 0.0) > table.primer_built_at + 1.0:
            return None
        return table

    def _load_or_build(self, key: str) -> None:
        started = time.monotonic()
        try:
            primer = self._primer_fn(key)
            table = self._load(key, primer)
            if table is None:
                if primer is None or not primer.cards:
                    raise RuntimeError("no set primer")
                ratings = self._ratings_fn(key)
                if not ratings:
                    raise RuntimeError("no 17Lands card ratings")
                try:
                    colors = self._color_ratings_fn(key)
                except Exception:
                    colors = None
                table = build_trick_table(key, primer, ratings, self._lookup(), color_ratings=colors)
                try:
                    self._dir.mkdir(parents=True, exist_ok=True)
                    self._path(key).write_text(table.to_json(), encoding="utf-8")
                except OSError as error:
                    logger.info("Trick table %s not cached: %s", key, error)
                logger.info(
                    "Trick table %s built: %d instant-speed cards, %d hypotheses (%.1fs)",
                    key,
                    len(table.cards),
                    len(table.copies),
                    time.monotonic() - started,
                )
            self.register(table)
            with self._lock:
                self._failed.pop(key, None)
        except Exception as error:
            with self._lock:
                self._failed[key] = time.monotonic()
            logger.info("Trick table for %s unavailable: %s", key, error)


def _default_primer(set_code: str) -> SetPrimer | None:
    """The cached set primer (any age: its card list is what matters), else a data-only one."""
    path = PRIMER_DIR / f"{set_code.upper()}.json"
    try:
        return SetPrimer.from_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        pass
    from arenamcp.draftstats import DraftStatsCache
    from arenamcp.set_primer import data_primer

    stats = DraftStatsCache()
    return data_primer(set_code, stats.get_raw_ratings(set_code), stats.get_color_pair_stats(set_code))


def _default_ratings(set_code: str) -> list[dict]:
    from arenamcp.draftstats import DraftStatsCache

    return DraftStatsCache().get_raw_ratings(set_code)


def _default_color_ratings(set_code: str) -> list[dict] | None:
    from arenamcp.draftstats import CACHE_DIR, DraftStatsCache

    path = CACHE_DIR / f"{set_code.upper()}_ColorRatings.json"
    if not path.exists():
        DraftStatsCache().get_color_pair_stats(set_code)  # downloads and caches the raw rows
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rows if isinstance(rows, list) else None
