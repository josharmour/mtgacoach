"""Deterministic draft pick ranking driven by the set primer.

Industry-standard bot drafting, made explicit and explainable:

- Card quality: the card's 17lands games-in-hand win rate as a z-score within
  the set (``SetCard.baseline``). A card 17lands leaves unrated is usually one
  nobody plays, so it starts well below average unless its play rate or game
  win rate says otherwise; scarce rares and mythics keep a modest prior.
- Lane: the pool's colors, weighted by card quality and by what each mana cost
  actually requires. Commitment grows with the pool, so early picks stay
  flexible and late off-color picks are discounted. A weak second color stays
  open through pack 2 and is re-chosen every pick (pool quality, signals and
  the set's pair win rates), so an open pair can still take over.
- Archetype: the live deck hypotheses (``draft_deck_plan``) score each card by
  its rules-text fit to the archetype's engine and roles (``draft_playbook``),
  what it adds to the engine given the pool's enablers and payoffs, and its
  rules-text links to cards already drafted.
- Synergy: the primer's synergy notes shared with picked cards.
- Signals: in pack 1, good cards arriving well after their average pick (ATA)
  suggest their colors are open upstream.
- Hate: a strong card we will not play, weighed by how much it beats our deck
  and how likely a neighbour plays it, wins the pick only when the best card
  for our deck adds little (a full lane, comparable cards still to come).

The model-backed DraftAdvisor sees this ranking as evidence; when the model is
unavailable or its answer fails validation, this ranking makes the pick.
"""

from __future__ import annotations

import html
import math
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from arenamcp.draft_guidance import normalize_card
from arenamcp.limited_rules import rules_profile
from arenamcp.set_primer import COLOR_ORDER, SetPrimer, normalize_colors

RARITY_PRIOR = {"M": 0.3, "R": 0.2, "U": -0.15, "C": -0.4}  # only when there is no primer at all

# How a pick's components combine (2026-10-13 user direction: the deck we are
# drafting -- lane, archetype, engine, rules-text synergy -- decides; 17Lands
# quality is about a quarter of what separates a pack's contenders, signals
# about a sixth). Units: one point of "quality" is one standard deviation of
# 17Lands GIH within the set. Calibrated on recorded FRA drafts
# (tests/test_draft_strategic_picks.py measures the shares).
SCORE_WEIGHTS = {
    "quality": 1.0,
    "bomb": 1.5,
    "lane": 4.0,
    "needs": 2.0,
    "archetype": 6.0,
    "engine": 4.0,
    "edges": 4.0,
    "notes": 4.0,
    "signal": 5.5,
    "scarcity": 3.0,
    "penalty": 1.0,
    "hate": 1.0,
}
DECK_CREDIT = ("archetype", "engine", "edges", "notes", "scarcity")
# With six removal spells the deck has enough: scarcity of more counts less.
REMOVAL_ENOUGH = 6
# Below PLAYABLE_Z (GIH z) deck credit fades, to UNPLAYABLE_CREDIT of itself at UNPLAYABLE_Z.
PLAYABLE_Z = -0.75
UNPLAYABLE_Z = -1.5
UNPLAYABLE_CREDIT = 0.25
PAYOFF_ENOUGH = 5
# Quality above this (a top-few card in the set) counts again: bombs still win packs.
BOMB_BASELINE = 1.5
# Most an unrated (not rarely played) card's weighted deck credit may add.
UNRATED_DECK_CAP = 0.25
FIT_NOTE_MIN = 0.3
SCARCITY_NOTE_MIN = 0.4
SCARCITY_GAIN = 0.5
ENGINE_NOTE_MIN = 0.05
SYNERGY_OVERLAP = 0.5
# Archetype, engine and synergy credit ramps in over the first picks of the draft.
DECK_RAMP_PICKS = 5
RARELY_PLAYED_LANE = 0.5

# 17lands hides GIH below ~500 games in hand. In a mature format a common or
# uncommon that short of games is one drafters leave in the sideboard
# (2026-10-06 FRA: Winter, Team Player 27% played / 49.9% game win rate,
# Arni 20% / 48.5%, Yargle 5%), so "unrated" means "assume below average".
UNRATED_PRIOR = -1.0
UNRATED_RARE_PRIOR = {"M": 0.2, "R": 0.0}
RARELY_PLAYED_PRIOR = -1.4
RARELY_PLAYED_RATE = 0.35  # 17lands play rate; the median rated card is ~0.7
RARELY_PLAYED_SHARE = 0.45  # games vs. the median rated card of the same rarity
ALSA_FIT_MIN_CARDS = 30  # rated cards with an ALSA needed to read quality from pick order
UNRATED_ALSA_MIN, UNRATED_ALSA_MAX = -1.0, 1.2  # an unrated rare's pick-order prior stays modest
# 17lands shows GIH from ~500 games in hand. A format whose typical rated card has
# fewer games of its own borrows its win rates (an Alchemy draft in its first weeks).
YOUNG_FORMAT_GAMES = 500
SECOND_COLOR_FLOOR = 0.25  # least hold on any second color (an undecided one too)
REMOVAL_SHARE = 0.12  # ~3 removal spells per 23-25 playables
# 17Lands GIH overrates nonbasic lands: a dual is drawn into a two-color deck
# whether or not it fixed anything (2026-10-07 P2p1: Haunted Ridge, a B/R dual
# at 58.2%, outranked every UB card for a UB drafter). An above-average land
# rating is pulled this far back toward average.
LAND_GIH_DISCOUNT = 0.3
# A dual that taps for only one lane color is a basic with a drawback.
PARTIAL_LAND_FIT = -0.6
# Redundant copies of a non-premium card lose value (2026-10-07: a third
# Tam's Resistance, a hybrid pump spell, at P2p1): per copy held for spells,
# per copy beyond the first for creatures (two of a playable body is normal).
DUPLICATE_STEP_SPELL = 0.2
DUPLICATE_STEP_BODY = 0.1
DUPLICATE_CREDIT = 0.6  # deck credit kept per redundant copy already held
PREMIUM_BASELINE = 0.75
BASIC_LAND_COLORS = {"plains": "W", "island": "U", "swamp": "B", "mountain": "R", "forest": "G"}

# Hate-drafting (2026-10-18 pro draft, P3p3: Jace, Reality Sculptor, a blue rare
# that blanks attackers, over an in-lane Arcane Amphisbaena for a full Selesnya
# creature deck). Denying a card is its own component, never deck fit: what the
# card would do against us, weighed against what the best pick for our deck adds
# once the rest of the draft is counted. Units are quality (GIH z) points.
DECK_SPELLS = 23
# Share of our remaining looks that end in a playable for our deck at a given quality.
FUTURE_TAKE_RATE = 0.5
# Only a card at least this good is worth denying.
HATE_MIN_Z = 0.35
# The least threat that earns a hate pick at all (a fringe card is not denied).
HATE_MIN_THREAT = 0.25
# How much of our own lane must be settled before denial counts in full.
HATE_COMMITMENT = 0.8
# How much of a card's strength lands on us. A merely good card counts half: the
# neighbour is one of seven drafters, and Arena pairs us from the whole queue.
# Rules text that beats our deck specifically adds to it.
HATE_HARM_BASE = 0.5
HATE_HARM_REMOVAL = 0.5
HATE_HARM_COMBAT = 0.75
HATE_HARM_LIFEGAIN = 0.75
# The chance a drafter next to us plays a color we know nothing about; colors
# flowing to us are flowing past us too, so the neighbours downstream get them.
NEIGHBOUR_BASE = 0.5
# Denying one copy of a common barely dents a neighbour who will see others.
HATE_RARITY = {"M": 1.0, "R": 1.0, "U": 0.5, "C": 0.25}
# A winning hate pick leads the best deck pick by this much per point of excess threat.
HATE_LEAD = 1.0
_COMBAT_DENIAL = re.compile(
    r"\bcreatures? (?:can't|cannot) attack\b"
    r"|\bwhenever a creature attacks you\b[^.]*\bgets -\d+/-0\b"
    r"|\battacking creatures? (?:get|gets) -\d+/-0\b"
    r"|\bprevent all combat damage\b"
    r"|\btap all creatures\b"
)
_LIFEGAIN_DENIAL = re.compile(
    r"\b(?:players|your opponents|opponents) can't gain life\b|\blife totals? can't change\b"
)

_REMOVAL_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bdestroy target\b",
        r"\bexile target (?:[a-z]+ )*?(?:creature|planeswalker|permanent)\b",
        r"\bdeals? (?:\d+|x) damage to (?:any target|(?:up to one )?target (?:[a-z]+ )*?(?:creature|planeswalker))",
        r"\btarget creature\b[^.]*?\bgets -(?:\d+|x)/-(?:[1-9]\d*|x)\b",
        r"\b(?:target|each) opponent sacrifices (?:a|an|one) (?:[a-z]+ )*?(?:creature|planeswalker)",
        r"\bbase power and toughness 0/0\b",
        r"\bfights? (?:up to one )?(?:other )?target (?:[a-z]+ )*?creature",
    )
)


# 17Lands quality as words (quality z-score lower bounds). The pick model sees
# only these: the user's 2026-10-13 direction is that win rate is a minor input.
QUALITY_BANDS = ((1.0, "strong"), (0.35, "good"), (-0.35, "average"), (-1.0, "below average"))
_QUALITY_NOTE = re.compile(r"^(?:GIH \d|unrated|not in the set data|basic/utility land|land; GIH)")


def quality_band(z: float | None, note: str = "") -> str:
    """strong / good / average / below average / weak, or rarely played / unknown."""
    if "rarely played" in note:
        return "rarely played"
    if z is None or note.startswith(("unrated; rarity prior", "not in the set data")):
        return "unknown"
    for floor, word in QUALITY_BANDS:
        if z >= floor:
            return word
    return "weak"


def is_ordinary_basic(name: str) -> bool:
    """These five basics can be added freely after drafting, even without ratings."""
    return name.strip().casefold() in {"plains", "island", "swamp", "mountain", "forest"}


@dataclass
class PickScore:
    grp_id: int
    name: str
    score: float
    reasons: list[str] = field(default_factory=list)
    # Weighted parts of ``score`` by name (SCORE_WEIGHTS keys).
    components: dict[str, float] = field(default_factory=dict)
    # 17Lands quality in words (QUALITY_BANDS), for prompts that get no win rates.
    band: str = ""
    # Drafted cards this one has a rules-text link with, strongest first.
    partners: list[str] = field(default_factory=list)

    def as_evaluation(self) -> dict[str, Any]:
        return {
            "grp_id": self.grp_id,
            "name": self.name,
            "score": round(self.score, 3),
            "reason": "; ".join(self.reasons),
        }

    def as_plan_evaluation(self, rank: int) -> dict[str, Any]:
        """The ranking for the pick model: deck value and a quality band, no win rates."""
        deck = sum(v for k, v in self.components.items() if k not in ("quality", "bomb", "signal", "hate"))
        return {
            "grp_id": self.grp_id,
            "name": self.name,
            "rank": rank,
            "deck_value": round(deck, 2),
            "quality_band": self.band or "unknown",
            "why": "; ".join(r for r in self.reasons if not _QUALITY_NOTE.match(r)),
        }


@dataclass
class Lane:
    colors: str
    weights: dict[str, float]
    commitment: float
    main: str = ""
    # How firmly the weaker lane color is held: 0 = still open, 1 = settled.
    second_hold: float = 1.0
    # Colors the stated plan splashes (never set by the pool estimate).
    splash: str = ""


@dataclass
class _SetStats:
    median_games: dict[str, float]
    game_wr_mean: float | None
    game_wr_sd: float | None
    # Rated cards' quality against how early drafters take them (ALSA): (intercept, slope).
    alsa_fit: tuple[float, float] | None = None
    # Too new for 17lands to show win rates of its own cards (an Alchemy format in its
    # first weeks): unrated then says nothing about whether drafters play a card.
    young: bool = False


def _alsa_fit(rated: list[Any]) -> tuple[float, float] | None:
    """Least-squares line baseline ~ ALSA over the rated cards (FRA 2026-10: r = -0.61)."""
    points = [(card.alsa, card.baseline) for card in rated if getattr(card, "alsa", None)]
    if len(points) < ALSA_FIT_MIN_CARDS:
        return None
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    spread = statistics.pvariance(xs)
    if spread <= 0:
        return None
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / len(points) / spread
    if slope >= 0:
        return None  # later picks rating higher: no usable signal
    return mean_y - slope * mean_x, slope


def _set_stats(primer: SetPrimer | None) -> _SetStats:
    """What a typical rated card of each rarity looks like, to judge unrated ones."""
    if primer is None:
        return _SetStats({}, None, None)
    rated = [card for card in primer.cards.values() if card.baseline is not None]
    median_games = {}
    for rarity in {card.rarity for card in rated}:
        games = [_format_games(card) for card in rated if card.rarity == rarity and _format_games(card) > 0]
        if len(games) >= 3:
            median_games[rarity] = float(statistics.median(games))
    fit = _alsa_fit(rated)
    own = [card.format_games for card in rated if getattr(card, "format_games", None) is not None]
    young = len(own) >= 10 and statistics.median(own) < YOUNG_FORMAT_GAMES
    game_wrs = [card.game_wr for card in rated if card.game_wr is not None]
    if len(game_wrs) >= 10 and statistics.pstdev(game_wrs) > 0:
        return _SetStats(median_games, statistics.fmean(game_wrs), statistics.pstdev(game_wrs), fit, young)
    return _SetStats(median_games, None, None, fit, young)


def _rarely_played(card: Any, stats: _SetStats) -> str | None:
    """Why an unrated card looks like one drafters leave out, or None."""
    if card.play_rate is not None:
        if card.play_rate < RARELY_PLAYED_RATE:
            return f"{card.play_rate:.0%} of drafted copies played"
        return None
    median = stats.median_games.get(card.rarity)
    games = _format_games(card)
    if median and games / median < RARELY_PLAYED_SHARE:
        return f"{games / median:.0%} of a typical card's games"
    return None


def _format_games(card: Any) -> int:
    """Games in the event's own format (an Alchemy draft's paper cards borrow ``games``)."""
    own = getattr(card, "format_games", None)
    return card.games if own is None else own


def card_value(card: Any, stats: _SetStats) -> tuple[float, str, bool]:
    """(base score in z units, reason, rated) for a primer card."""
    if card.baseline is not None:
        return card.baseline, f"GIH {card.gih_wr:.1%} (z {card.baseline:+.2f})", True
    if "Land" in card.types and not card.colors and card.rarity in {"C", ""}:
        return -2.0, "basic/utility land", False
    rarely = _rarely_played(card, stats)
    if rarely:
        return RARELY_PLAYED_PRIOR, f"unrated; rarely played ({rarely})", False
    prior, prior_reason = _unrated_prior(card, stats)
    if card.game_wr is not None and stats.game_wr_sd and stats.game_wr_mean is not None:
        # 2026-10-12 P2p1: Darklight Phoenix (mythic, taken ~2.5th) went unrated with a
        # 52.3% game win rate over 238 games, read at face value as z -1.07, and the
        # ranking vetoed the model's pick for a common. 238 games carry a +-3.2 point
        # sampling error, more than the 2.1-point spread between FRA cards, so the win
        # rate only moves the prior by its share of real signal.
        sd = stats.game_wr_sd
        noise = card.game_wr * (1.0 - card.game_wr) / max(1, card.games)
        weight = sd * sd / (sd * sd + noise)
        prior_wr = stats.game_wr_mean + prior * sd
        estimate = prior_wr + weight * (card.game_wr - prior_wr)
        z = max(-2.0, min(1.5, (estimate - stats.game_wr_mean) / sd))
        reason = f"unrated; game win rate {card.game_wr:.1%} over {card.games} games"
        if "drafters" in prior_reason:
            reason += ", " + prior_reason.removeprefix("unrated; ")
        return round(z, 3), reason, False
    return prior, prior_reason, False


def _unrated_prior(card: Any, stats: _SetStats) -> tuple[float, str]:
    """Where an unrated card starts: rares by when drafters take them, others below average.

    A rare or mythic goes unrated because few copies exist, not because nobody
    plays it, so how early drafters take it (ALSA, r = -0.61 with quality on
    FRA's rated cards) predicts its quality. An unrated common or uncommon in a
    mature format is one drafters leave out, so it starts below average. In a
    young format (``_SetStats.young``) every new card is unrated, so pick order
    judges commons and uncommons too (2026-10-18 Y26FRA: Dina, taken ~3.5th).
    """
    if card.rarity in UNRATED_RARE_PRIOR or stats.young:
        alsa = getattr(card, "alsa", None)
        if alsa and stats.alsa_fit:
            intercept, slope = stats.alsa_fit
            z = max(UNRATED_ALSA_MIN, min(UNRATED_ALSA_MAX, intercept + slope * alsa))
            return round(z, 3), f"unrated; drafters take it around pick {alsa:.1f}"
        if card.rarity in UNRATED_RARE_PRIOR:
            return UNRATED_RARE_PRIOR[card.rarity], "unrated rare; rarity prior"
    return UNRATED_PRIOR, "unrated; assumed below average"


def is_removal(card: Any) -> bool:
    """Kills, exiles, shrinks or forces a sacrifice of an opponent's creature, from rules text."""
    text = html.unescape(re.sub(r"<[^>]*>", "", str(getattr(card, "oracle", "") or ""))).lower()
    for sentence in re.split(r"[.\n]", text):
        if "you control" in sentence:
            continue
        if any(pattern.search(sentence) for pattern in _REMOVAL_PATTERNS):
            return True
    return False


def is_nonbasic_land(card: Any) -> bool:
    return "Land" in str(getattr(card, "types", "") or "") and not is_ordinary_basic(card.name)


def land_colors(card: Any) -> str:
    """Colors a land can tap for, from its rules text and basic land types ("" for none)."""
    if "Land" not in str(getattr(card, "types", "") or ""):
        return ""
    text = html.unescape(re.sub(r"<[^>]*>", "", str(getattr(card, "oracle", "") or ""))).lower()
    if re.search(r"\bmana of any (?:one )?color\b|\bmana of the chosen color\b", text):
        return COLOR_ORDER
    found = set()
    for clause in re.findall(r"\badd ([^.\n]*)", text):
        found |= {symbol.upper() for symbol in re.findall(r"\{o?([wubrg])\}", clause)}
    words = re.findall(r"[a-z]+", f"{card.types} {text}".lower())
    found |= {BASIC_LAND_COLORS[word] for word in words if word in BASIC_LAND_COLORS}
    return "".join(color for color in COLOR_ORDER if color in found)


def lane_fit(card: Any, colors: str, mana_costs: dict[int, str] | None = None) -> str:
    """How a card fits a lane's colors.

    "in": castable with the lane's colors; "colorless": needs no color;
    "fixing": a land that taps for every lane color; "partial": a land that taps
    for only some of them; "off": neither.
    """
    if card is None or not colors:
        return "unknown"
    if is_nonbasic_land(card):
        produced = set(land_colors(card))
        if not produced:
            return "colorless"
        if set(colors) <= produced:
            return "fixing"
        return "partial" if produced & set(colors) else "off"
    needs = _mana_needs(card, mana_costs or {})
    if not needs:
        return "colorless"
    return "in" if _payable(needs, colors) else "off"


def _mana_needs(card: Any, mana_costs: dict[int, str]) -> list[set[str]]:
    """Each colored mana symbol as the set of colors that can pay it.

    Generic, colorless, {2/X} and Phyrexian symbols need no color. Without an
    Arena cost the card's colors are all treated as required.
    """
    cost = mana_costs.get(card.grp_id)
    if not cost:
        return [{color} for color in card.colors]
    cost = normalize_card({"mana_cost": cost}).mana_cost
    needs = []
    for symbol in re.findall(r"\{([^}]+)\}", cost):
        choices = set(symbol.upper().split("/"))
        if choices & {"2", "P"}:
            continue
        colors = choices & set(COLOR_ORDER)
        if colors:
            needs.append(colors)
    return needs


def _payable(needs: list[set[str]], colors: set[str] | str) -> bool:
    return all(symbol & set(colors) for symbol in needs)


def _pair_factor(primer: SetPrimer | None, pair: str) -> float:
    """Tie-break between candidate second colors by the set's pair win rates (about +-25%)."""
    rates = [row.get("win_rate") for row in (primer.pair_stats if primer else {}).values()]
    rates = [rate for rate in rates if isinstance(rate, (int, float))]
    rate = ((primer.pair_stats if primer else {}).get(pair) or {}).get("win_rate")
    if len(rates) < 3 or not isinstance(rate, (int, float)):
        return 1.0
    return max(0.75, min(1.3, 1.0 + 8.0 * (rate - statistics.fmean(rates))))


def pool_lane(pool: list[int], primer: SetPrimer | None, mana_costs: dict[int, str] | None = None) -> Lane:
    """The pool's main color, its best second color, and how committed the drafter should be.

    A card adds weight (its quality) only to colors its mana cost requires. A
    hybrid symbol credits a color the pool already plays when it can, since
    the card does not need the other one; otherwise it splits.
    """
    mana_costs = mana_costs or {}
    stats = _set_stats(primer)
    weights = {color: 0.0 for color in COLOR_ORDER}
    deferred: list[tuple[float, set[str]]] = []
    for grp_id in pool:
        card = primer.card(grp_id) if primer else None
        if card is None or is_ordinary_basic(card.name):
            continue
        needs = _mana_needs(card, mana_costs)
        if not needs:
            continue
        quality = max(0.1 if card.baseline is None else 0.25, 1.0 + card_value(card, stats)[0])
        for symbol in needs:
            if len(symbol) == 1:
                weights[next(iter(symbol))] += quality / len(needs)
            else:
                deferred.append((quality / len(needs), symbol))
    firm = dict(weights)
    for share, symbol in deferred:
        held = [color for color in symbol if firm[color] > 0]
        if held:
            weights[max(held, key=lambda color: firm[color])] += share
        else:
            for color in symbol:
                weights[color] += share / len(symbol)
    ranked = sorted((color for color in COLOR_ORDER if weights[color] > 0), key=lambda c: -weights[c])
    main = ranked[0] if ranked else ""
    second = max(
        ranked[1:],
        key=lambda color: weights[color] * _pair_factor(primer, normalize_colors(main + color)),
        default="",
    )
    colors = normalize_colors(main + second)
    total = sum(weights.values())
    focus = (sum(weights[c] for c in colors) / total) if total else 0.0
    commitment = min(1.0, len(pool) / 14) * (0.6 + 0.4 * focus)
    ratio = weights[second] / weights[main] if second and weights[main] else 0.0
    second_hold = max(0.0, min(1.0, (ratio - 0.15) / 0.45))
    return Lane(
        colors=colors,
        weights=weights,
        commitment=round(commitment, 3),
        main=main,
        second_hold=round(second_hold, 3),
    )


def color_openness(pack: list[int], pick_number: int, primer: SetPrimer | None) -> dict[str, float]:
    """Per-color signal: good cards still in this pack well past their average pick."""
    openness = {color: 0.0 for color in COLOR_ORDER}
    if primer is None:
        return openness
    for grp_id in pack:
        card = primer.card(grp_id)
        if card is None or card.ata is None or card.baseline is None or not card.colors:
            continue
        lateness = max(0.0, pick_number - card.ata)
        value = lateness * max(0.0, card.baseline + 0.5)
        for color in card.colors:
            openness[color] += value / len(card.colors)
    peak = max(openness.values())
    return {color: (value / peak if peak else 0.0) for color, value in openness.items()}


# Pack direction: packs 1 and 3 pass the same way (what the right-hand neighbour
# leaves us), pack 2 the other way. A pack's own signal counts in full; the
# same-direction pack counts in full too; the other direction counts half.
def _signal_weight(source_pack: int, pack_number: int) -> float:
    if source_pack == pack_number or {source_pack, pack_number} == {1, 3}:
        return 1.0
    return 0.5


def note_openness(store: dict[int, dict[str, float]], pack_number: int, openness: dict[str, float]) -> None:
    """Accumulate one pick's per-color openness (0-1, peak color = 1) under its pack."""
    totals = store.setdefault(pack_number, {color: 0.0 for color in COLOR_ORDER})
    for color, value in openness.items():
        if color in totals:
            totals[color] += float(value or 0.0)


def color_signals(store: dict[int, dict[str, float]], pack_number: int) -> dict[str, float]:
    """Accumulated openness per color, 0-1 (strongest = 1), weighted by pack direction.

    2026-10-10: the table's signal was computed per pack and used only as a
    small ranking bonus; it never reached the lane decision. Strong drafters
    settle the second color on what is flowing.
    """
    totals = {color: 0.0 for color in COLOR_ORDER}
    for source_pack, values in store.items():
        weight = _signal_weight(int(source_pack), pack_number)
        for color in COLOR_ORDER:
            totals[color] += weight * float(values.get(color, 0.0))
    peak = max(totals.values())
    return {color: (round(value / peak, 3) if peak else 0.0) for color, value in totals.items()}


def second_color_hold(lane: Lane, pack_number: int, pick_number: int) -> float:
    """How firmly the weaker color is held: open through pack 2; pack 3 settles it by pick 8."""
    hold = lane.second_hold
    if pack_number >= 3:
        hold = max(hold, min(1.0, max(0.0, (pick_number - 2) / 6)))
    return hold


def deck_floor(pool_quality: list[float], candidates: list[tuple[float, int]], supply) -> float:
    """Quality of the weakest spell our deck will play once the rest of the draft is in.

    ``pool_quality``: our castable nonland picks; ``candidates``: (quality,
    grp_id) for every set card castable in our colors. The floor is the
    highest quality q at which the pool's cards of q or better plus the
    looks still to come that offer one (``FutureSupply``, at
    FUTURE_TAKE_RATE) fill DECK_SPELLS. A card adds to our deck only what
    it has above this line: below it, the packs will replace it anyway.
    """
    held = sorted(pool_quality, reverse=True)
    if supply is None:
        return held[DECK_SPELLS - 1] if len(held) >= DECK_SPELLS else -math.inf
    availability = supply.book.availability
    totals = [0.0] * 14
    ranked = sorted(candidates, reverse=True)
    index = 0
    for q in sorted({*held, *(value for value, _ in ranked)}, reverse=True):
        while index < len(ranked) and ranked[index][0] >= q:
            row = availability.get(ranked[index][1])
            if row is not None:
                totals = [a + b for a, b in zip(totals, row, strict=True)]
            index += 1
        coming = sum(n * (1.0 - math.exp(-totals[pick - 1])) for pick, n in supply.schedule.items())
        if sum(value >= q for value in held) + FUTURE_TAKE_RATE * coming >= DECK_SPELLS:
            return q
    return -math.inf


def hate_threat(
    card: Any,
    quality: float,
    floor: float,
    *,
    creature_share: float,
    lifegain: bool,
    signals: dict[str, float] | None,
) -> tuple[float, str]:
    """(threat in quality points, words) of a card we will not play, in a neighbour's deck.

    Strength is what it adds to a deck like ours: its quality above the deck
    floor, a bomb's excess counting twice. Its rules text decides how much
    of that lands on us: HATE_HARM_BASE for a card that is merely good,
    more for removal or attack denial against a creature deck and life-gain
    denial against a lifegain engine. A common or uncommon is denied only in
    part (HATE_RARITY: the neighbour sees other copies), and the threat
    counts by the chance a drafter next to us plays its colors.
    """
    if quality < HATE_MIN_Z:
        return 0.0, ""
    strength = quality - floor + max(0.0, quality - BOMB_BASELINE)
    text = html.unescape(re.sub(r"<[^>]*>", "", str(getattr(card, "oracle", "") or ""))).lower()
    harm, against = HATE_HARM_BASE, ""
    if is_removal(card):
        harm += HATE_HARM_REMOVAL * creature_share
        against = "removal for our creatures"
    if _COMBAT_DENIAL.search(text):
        harm += HATE_HARM_COMBAT * creature_share
        against = "it blanks our attackers"
    if lifegain and _LIFEGAIN_DENIAL.search(text):
        harm += HATE_HARM_LIFEGAIN
        against = "it shuts off our life gain"
    colors = [c for c in card.colors if c in COLOR_ORDER]
    flow = statistics.fmean(float((signals or {}).get(c, 0.0)) for c in colors) if colors else 1.0
    odds = NEIGHBOUR_BASE + (1.0 - NEIGHBOUR_BASE) * flow
    unique = HATE_RARITY.get(card.rarity, HATE_RARITY["C"])
    who = f"the {'-'.join(_COLOR_WORDS[c] for c in colors)} drafter next to us" if colors else "a neighbour"
    why = f"hate-pick: denies it to {who}" + (f"; {against}" if against else "")
    return strength * harm * odds * unique, why


_COLOR_WORDS = {"W": "white", "U": "blue", "B": "black", "R": "red", "G": "green"}


def deck_plan_for(
    pool: list[int],
    primer: SetPrimer | None,
    *,
    pack_number: int = 1,
    pick_number: int = 1,
    mana_costs: dict[int, str] | None = None,
    plan_lane: str = "",
    signals: dict[str, float] | None = None,
    lane: Lane | None = None,
):
    """The live deck hypotheses the ranking uses for this pool (``draft_deck_plan``)."""
    from arenamcp.draft_deck_plan import build_deck_plan

    if primer is None:
        return None
    mana_costs = mana_costs or {}
    lane = lane or pool_lane(pool, primer, mana_costs)
    return build_deck_plan(
        pool,
        primer,
        lane_colors=lane.colors,
        lane_main=lane.main or (lane.colors[:1] if lane.colors else ""),
        commitment=lane.commitment,
        second_hold=second_color_hold(lane, pack_number, pick_number),
        lane_weights=lane.weights,
        plan_lane=plan_lane,
        signals=signals,
        pack_number=pack_number,
        mana_costs=mana_costs,
    )


def rank_pack(
    pack: list[int],
    pool: list[int],
    primer: SetPrimer | None,
    *,
    pack_number: int = 1,
    pick_number: int = 1,
    names: dict[int, str] | None = None,
    mana_costs: dict[int, str] | None = None,
    plan_lane: str = "",
    signals: dict[str, float] | None = None,
    picks_per_pass: int = 1,
) -> list[PickScore]:
    """Best pick first; never returns cards outside the pack.

    Each card's score is a weighted sum of named components (kept on
    ``PickScore.components``): 17Lands quality, the deck we are drafting
    (lane, curve and removal needs, archetype fit, engine slots, rules-text
    links to the pool, the primer's synergy notes) and the table's signals.
    The weights (``SCORE_WEIGHTS``) keep 17Lands to about a quarter of what
    separates the contenders in a pack. What the rest of the draft can still
    offer (``draft_deck_plan.future_supply``) values payoffs by the enablers
    still to come and adds scarcity: removal or a payoff with few comparable
    substitutes left is worth taking now.
    """
    from arenamcp.draft_deck_plan import engine_gain, future_supply, pool_edges, scarcity

    names = names or {}
    mana_costs = mana_costs or {}
    stats = _set_stats(primer)
    lane = pool_lane(pool, primer, mana_costs)
    main = lane.main or (lane.colors[:1] if lane.colors else "")
    second = "".join(color for color in lane.colors if color != main)
    hold = second_color_hold(lane, pack_number, pick_number)
    soft_hold = max(hold, SECOND_COLOR_FLOOR)

    def fits_lane(card):
        return _payable(_mana_needs(card, mana_costs), lane.colors) if lane.colors else True

    openness = color_openness(pack, pick_number, primer)
    pool_names = (
        Counter(primer.card(g).name.lower() for g in pool if primer and primer.card(g))
        if primer
        else Counter()
    )
    playable_pool = [
        card
        for grp_id in pool
        if primer
        and (card := primer.card(grp_id)) is not None
        and not is_ordinary_basic(card.name)
        and "Land" not in card.types
        and (lane.commitment < 0.5 or fits_lane(card))
    ]

    def provides_body(card):
        return rules_profile({"type_line": card.types, "oracle_text": card.oracle})["unconditional_body"]

    bodies = sum(provides_body(card) for card in playable_pool)
    early_bodies = sum(
        provides_body(card) and card.cmc is not None and card.cmc <= 3 for card in playable_pool
    )
    cheap_bodies = sum(
        provides_body(card) and card.cmc is not None and card.cmc <= 2 for card in playable_pool
    )
    expensive = sum(card.cmc is not None and card.cmc >= 5 for card in playable_pool)
    removal = sum(is_removal(card) for card in playable_pool)
    deck_plan = deck_plan_for(
        pool,
        primer,
        pack_number=pack_number,
        pick_number=pick_number,
        mana_costs=mana_costs,
        plan_lane=plan_lane,
        signals=signals,
        lane=lane,
    )
    lead = deck_plan.lead() if deck_plan is not None else None
    supply = (
        future_supply(deck_plan.playbook, pack_number, pick_number, picks_per_pass)
        if deck_plan is not None
        else None
    )
    quality_cache: dict[int, float] = {}
    castable_cache: dict[tuple[int, str], bool] = {}

    def quality_of(grp_id: int) -> float:
        if grp_id not in quality_cache:
            known = primer.card(grp_id) if primer else None
            quality_cache[grp_id] = card_value(known, stats)[0] if known is not None else UNRATED_PRIOR
        return quality_cache[grp_id]

    def castable_in(grp_id: int, colors: str) -> bool:
        key = (grp_id, colors)
        if key not in castable_cache:
            known = primer.card(grp_id) if primer else None
            castable_cache[key] = lane_fit(known, colors, mana_costs) in ("in", "colorless", "fixing")
        return castable_cache[key]

    # Rules-text partners: the lead deck's castable cards once we lean, else the whole pool.
    if lead is not None and (deck_plan.committed or lane.commitment >= 0.5):
        partners = list(lead.ledger.cards)
    else:
        partners = [
            g
            for g in pool
            if primer and primer.card(g) is not None and not is_ordinary_basic(primer.card(g).name)
        ]
    scores: dict[int, PickScore] = {}
    for grp_id in pack:
        if grp_id in scores:
            continue
        card = primer.card(grp_id) if primer else None
        name = card.name if card else names.get(grp_id, f"Card {grp_id}")
        if is_ordinary_basic(name):
            scores[grp_id] = PickScore(
                grp_id=grp_id,
                name=name,
                score=-1000.0,
                reasons=["ordinary basic land; available freely when building the deck"],
            )
            continue
        reasons: list[str] = []
        linked: list[str] = []
        parts = dict.fromkeys(SCORE_WEIGHTS, 0.0)
        castable = (
            {
                h.plan.colors: lane_fit(card, h.plan.colors, mana_costs) in ("in", "colorless", "fixing")
                for h in deck_plan.hypotheses
            }
            if deck_plan is not None and deck_plan.hypotheses and card is not None
            else None
        )
        share = deck_plan.castable_share(castable) if castable is not None else 1.0
        rated, rarely = True, False
        if card is not None:
            base, note, rated = card_value(card, stats)
            rarely = "rarely played" in note
            reasons.append(note)
        elif primer is not None:
            base = UNRATED_PRIOR
            reasons.append("not in the set data; assumed below average")
            rated = False
        else:
            base = RARITY_PRIOR.get("", -0.5)
            reasons.append("unrated; rarity prior")
        parts["quality"] = base

        colors = card.colors if card else ""
        commitment = lane.commitment
        needs = _mana_needs(card, mana_costs) if card is not None else []
        land = card is not None and is_nonbasic_land(card)
        if land:
            # A land is judged by the mana it makes, not its (empty) cost.
            if rated and base > 0:
                parts["quality"] -= min(LAND_GIH_DISCOUNT, base)
                reasons.append("land; GIH discounted")
            how = lane_fit(card, lane.colors)
            if not lane.colors or how == "colorless":
                fit = 0.1
            elif how == "fixing":
                fit = 0.1
                reasons.append(
                    f"fixes lane {lane.colors}" if len(lane.colors) == 2 else f"taps for {lane.colors}"
                )
            elif how == "partial":
                shared = "".join(c for c in land_colors(card) if c in lane.colors)
                fit = PARTIAL_LAND_FIT * commitment
                reasons.append(f"taps for only {shared} of lane {lane.colors}")
            else:
                fit = -1.2 * commitment
                if commitment > 0.3:
                    reasons.append(f"off lane {lane.colors}")
        elif not needs:
            # Colorless: castable in whatever deck we end up in.
            fit = max(0.1, 0.6 * commitment) if castable is not None else 0.1
        elif not lane.colors:
            fit = 0.0 if any(_payable(needs, color) for color in COLOR_ORDER) else -0.15
        elif castable is not None and len(deck_plan.hypotheses) > 1 and share > 0:
            # Undecided between live decks: the lane is worth the chance the
            # card ends up castable (2026-10-06: a mono-blue card's full lane
            # bonus kept black out while it flowed).
            fit = 0.6 * commitment * share
            live = [h.plan.colors for h in deck_plan.hypotheses if castable.get(h.plan.colors)]
            reasons.append(f"in lane {lane.colors}" if share > 0.99 else f"castable in {'/'.join(live)}")
        elif _payable(needs, main):
            fit = 0.6 * commitment
            reasons.append(f"in lane {lane.colors}")
        elif second and _payable(needs, main + second):
            fit = 0.6 * commitment * hold
            reasons.append(f"in lane {lane.colors}")
        elif any(_payable(needs, main + color) for color in COLOR_ORDER if color not in lane.colors):
            # A candidate second color: discounted only as firmly as the
            # current second color is held.
            fit = -1.2 * commitment * soft_hold
            if commitment * soft_hold > 0.3:
                reasons.append(f"off lane {lane.colors}")
            elif commitment > 0.3:
                reasons.append(f"second color still open (lane {lane.colors})")
        else:
            fit = -1.2 * commitment
            if commitment > 0.3:
                reasons.append(f"off lane {lane.colors}")
        parts["lane"] = fit

        if primer is not None and card is not None:
            in_deck = share > 0 if castable is not None and len(deck_plan.hypotheses) > 1 else fits_lane(card)
            if len(playable_pool) >= 4 and in_deck:
                if provides_body(card):
                    if bodies < len(playable_pool) * 0.6:
                        parts["needs"] += 0.45
                        reasons.append("pool needs more creatures")
                    if (
                        card.cmc is not None
                        and card.cmc <= 2
                        and cheap_bodies < max(2, len(playable_pool) * 0.18)
                    ):
                        parts["needs"] += 0.45
                        reasons.append("fills the early creature curve; needs one- and two-mana bodies")
                    elif (
                        card.cmc is not None
                        and card.cmc <= 3
                        and early_bodies < max(3, len(playable_pool) * 0.25)
                    ):
                        parts["needs"] += 0.35
                        reasons.append("fills the early creature curve")
                if card.cmc is not None and card.cmc >= 5 and expensive >= max(2, len(playable_pool) * 0.2):
                    parts["needs"] -= 0.5
                    reasons.append("pool already has enough expensive spells")
                if is_removal(card) and removal < max(2.0, len(playable_pool) * REMOVAL_SHARE):
                    cheap = card.cmc is not None and card.cmc <= 3
                    parts["needs"] += 0.4 if cheap else 0.2
                    reasons.append(f"pool needs removal ({removal} so far)")

            if deck_plan is not None:
                book = deck_plan.playbook
                tags = book.tag(grp_id)
                # Each reason comes from the hypothesis that contributed most.
                fit_note, fit_best = "", FIT_NOTE_MIN * 0.5
                engine_note, engine_best = "", 0.0
                scarce_note, scarce_best = "", 0.0
                for hypothesis in deck_plan.hypotheses:
                    castable = lane_fit(card, hypothesis.plan.colors, mana_costs) in (
                        "in",
                        "colorless",
                        "fixing",
                    )
                    value, why = book.fit(grp_id, hypothesis.plan, castable, roles=False)
                    parts["archetype"] += hypothesis.weight * value
                    if why and value > FIT_NOTE_MIN and hypothesis.weight * value > fit_best:
                        fit_best = hypothesis.weight * value
                        fit_note = why if hypothesis.plan.name in why else f"{why} ({hypothesis.plan.name})"
                    if castable and tags is not None:
                        gain, why = engine_gain(tags, hypothesis, supply)
                        parts["engine"] += hypothesis.weight * gain
                        if why and hypothesis.weight * gain > engine_best:
                            engine_best = hypothesis.weight * gain
                            engine_note = why
                        rare, why = scarcity(tags, base, hypothesis.plan, supply, castable_in, quality_of)
                        ledger = hypothesis.ledger
                        # Scarce or not, a role the deck has filled matters less.
                        if "removal" in tags.roles and ledger.removal >= REMOVAL_ENOUGH or "removal" not in tags.roles and any(
                            ledger.payoffs.get(e, 0) >= PAYOFF_ENOUGH for e in tags.engine_payoffs()
                        ):
                            rare *= 0.3
                        parts["scarcity"] += hypothesis.weight * SCARCITY_GAIN * rare
                        if why and hypothesis.weight * rare > scarce_best:
                            scarce_best, scarce_note = hypothesis.weight * rare, why
                if fit_note:
                    reasons.append(f"fits {fit_note}")
                if engine_note and engine_best >= ENGINE_NOTE_MIN:
                    reasons.append(f"engine: {engine_note}")
                if scarce_note and scarce_best >= SCARCITY_NOTE_MIN:
                    reasons.append(scarce_note)
                parts["edges"], linked = pool_edges(primer, grp_id, partners, book)
                if linked:
                    reasons.append(f"works with {' / '.join(linked[:3])}")
                # Engine slots and pool links often describe one fact (a payoff
                # for the spells we hold): the larger counts in full, the other half.
                smaller = "engine" if parts["engine"] <= parts["edges"] else "edges"
                parts[smaller] *= SYNERGY_OVERLAP

            for note in primer.synergy_notes:
                members = [n.lower() for n in note.get("cards") or []]
                if card.name.lower() in members:
                    paired = sum(pool_names[m] for m in members if m != card.name.lower())
                    if paired:
                        parts["notes"] += 0.2 * paired
                        reasons.append(f"synergy: {note.get('why', '')[:80]}")
            parts["notes"] = min(parts["notes"], 0.6)
            # Picks 1-4 take the best card (2026-10-10 lane discipline): the
            # deck we are drafting toward counts in full once the pool can show it.
            ramp = min(1.0, (len(pool) + 1) / DECK_RAMP_PICKS)
            for key in DECK_CREDIT:
                parts[key] *= ramp
            if castable is not None and len(deck_plan.hypotheses) > 1:
                parts["needs"] *= share
            if rated and base < PLAYABLE_Z:
                # 17Lands GIH is measured in the decks that played the card: one
                # that loses even there fills no slot and carries no synergy
                # (2026-10-13 live P1p4: Inspired Tethermage, 50.0% GIH, won on
                # the research's "Simic payoff" listing).
                playable = max(UNPLAYABLE_CREDIT, 1.0 - (PLAYABLE_Z - base) / (PLAYABLE_Z - UNPLAYABLE_Z))
                for key in (*DECK_CREDIT, "needs"):
                    if parts[key] > 0:
                        parts[key] *= playable
            if rarely:
                # A card drafters leave in the sideboard fills no curve slot
                # either, and being castable matters less when it rarely makes the deck.
                parts["needs"] = min(parts["needs"], 0.0)
                if parts["lane"] > 0:
                    parts["lane"] *= RARELY_PLAYED_LANE
            if not rated and not (stats.young and not rarely):
                # Text and primer lists cannot lift a card the data says
                # nobody plays; a merely unrated one gets a little credit. In a
                # young format unrated says nothing, so rules text counts in full.
                cap = 0.0 if rarely else UNRATED_DECK_CAP
                credit = sum(SCORE_WEIGHTS[k] * parts[k] for k in DECK_CREDIT)
                if credit > cap:
                    scale = max(0.0, cap) / credit
                    for key in DECK_CREDIT:
                        parts[key] *= scale
                    reasons[:] = [
                        r
                        for r in reasons
                        if not r.startswith(("fits ", "engine: ", "works with ", "synergy: ", "scarce: "))
                    ]
                    linked = []

            if pack_number <= 2 and 4 <= pick_number <= 10 and colors:
                signal = 0.3 * max(openness[c] for c in colors)
                if signal >= 0.15:
                    reasons.append(f"{colors} looks open")
                parts["signal"] = signal

            if primer.is_trap(card.name):
                parts["penalty"] -= 0.4
                reasons.append("primer trap")

            copies = pool_names[card.name.lower()]
            if copies and not land and base < PREMIUM_BASELINE and not is_removal(card):
                # Each copy we hold already supplies this card's role and links:
                # another adds less (2026-10-13 live P3p2: a third Tam's Resistance).
                extra = copies if not provides_body(card) else copies - 1
                for key in DECK_CREDIT:
                    if parts[key] > 0 and extra > 0:
                        parts[key] *= DUPLICATE_CREDIT**extra
                # A second body is just a playable; a second trick or pump spell is redundancy.
                redundant = copies - 1 if provides_body(card) else copies
                step = DUPLICATE_STEP_BODY if provides_body(card) else DUPLICATE_STEP_SPELL
                if redundant > 0:
                    parts["penalty"] -= step * redundant
                    reasons.append(f"{copies} already in pool")
        if parts["quality"] > BOMB_BASELINE:
            # A true bomb stays a bomb: quality above this line counts in full.
            parts["bomb"] = parts["quality"] - BOMB_BASELINE
        components = {key: round(SCORE_WEIGHTS[key] * value, 4) for key, value in parts.items()}
        score = sum(components.values())
        scores[grp_id] = PickScore(
            grp_id=grp_id,
            name=name,
            score=round(score, 4),
            reasons=reasons,
            components=components,
            band=quality_band(base, reasons[0] if reasons else ""),
            partners=list(linked),
        )
    if deck_plan is not None and lead is not None and lane.commitment > 0:
        _add_hate(
            scores,
            primer,
            deck_plan,
            supply,
            playable_pool,
            quality_of,
            castable_in,
            lateness=min(1.0, lane.commitment / HATE_COMMITMENT) * hold,
            creature_share=bodies / len(playable_pool) if playable_pool else 0.0,
            signals=signals,
        )
    return sorted(scores.values(), key=lambda pick: (is_ordinary_basic(pick.name), -pick.score))


def _add_hate(
    scores: dict[int, PickScore],
    primer: SetPrimer,
    deck_plan,
    supply,
    playable_pool: list[Any],
    quality_of,
    castable_in,
    *,
    lateness: float,
    creature_share: float,
    signals: dict[str, float] | None,
) -> None:
    """Give a card we will not play the "hate" component when denying it beats our best pick.

    What the best deck pick adds is its quality above our deck's projected
    floor (``deck_floor``) plus what it fills that the floor card would not
    (curve and removal needs, an engine slot, scarcity). Early on the floor is
    open and every playable adds; with a full lane and the packs still to come
    it rises, and a replaceable playable gives way to denying a strong card.
    A winning hate pick is placed just ahead of that pick, by its excess
    threat; a losing one changes nothing.
    """
    from arenamcp.draft_deck_plan import SUBSTITUTE_MARGIN

    lead = deck_plan.lead()
    colors = lead.plan.colors
    book = deck_plan.playbook
    own = [
        pick
        for pick in scores.values()
        if not is_ordinary_basic(pick.name) and pick.components and pick.grp_id in book.tags
    ]
    if not own or lateness <= 0:
        return
    best = max(own, key=lambda pick: pick.score)
    pool_quality = [quality_of(card.grp_id) for card in playable_pool if castable_in(card.grp_id, colors)]
    candidates = [
        (quality_of(grp_id), grp_id)
        for grp_id, tags in book.tags.items()
        if not tags.land and castable_in(grp_id, colors)
    ]
    floor = deck_floor(pool_quality, candidates, supply)
    if floor == -math.inf:
        return  # our deck still has open slots: every playable adds
    # A playable the packs will offer again adds only if none comes: the
    # chance of no comparable card in our remaining looks (2026-10-18 P3p3:
    # two commons whose like a full Selesnya pool would still see).
    best_quality = quality_of(best.grp_id)
    substitutes = [
        grp_id
        for value, grp_id in candidates
        if value >= best_quality - SUBSTITUTE_MARGIN and grp_id != best.grp_id
    ]
    unreplaced = math.exp(-FUTURE_TAKE_RATE * supply.expected(substitutes)) if supply is not None else 1.0
    filled = sum(max(0.0, best.components.get(key, 0.0)) for key in ("needs", "engine", "scarcity"))
    adds = max(0.0, best_quality - floor) * unreplaced + filled
    lifegain = "lifegain" in lead.plan.engine and lead.ledger.payoffs.get("lifegain", 0) > 0
    for pick in own:
        card = primer.card(pick.grp_id)
        if pick is best or card is None or is_nonbasic_land(card):
            continue
        if any(castable_in(pick.grp_id, h.plan.colors) for h in deck_plan.hypotheses):
            continue  # a card we may play is judged as ours
        threat, why = hate_threat(
            card,
            quality_of(pick.grp_id),
            floor,
            creature_share=creature_share,
            lifegain=lifegain,
            signals=signals,
        )
        threat *= lateness
        if threat < HATE_MIN_THREAT or threat <= adds:
            continue
        value = best.score - pick.score + HATE_LEAD * (threat - adds)
        pick.components["hate"] = round(value, 4)
        pick.score = round(pick.score + value, 4)
        # Second, after the quality note: the pick log speaks the first three reasons.
        pick.reasons.insert(1, why)
        pick.reasons.insert(2, f"{best.name} adds little to a deck this full")


# The value of whatever we would otherwise take when a pack comes back: late
# picks are usually filler (score units, ~1 below an average playable).
WHEEL_FILLER_SCORE = -1.0
# Only trade the better card for a wheel when the expected gain is clear and
# the two are close: average-last-seen comes from human pods and other drafters
# vary, so a wheel is never certain (WHEEL_CONFIDENCE discounts it).
WHEEL_MIN_GAIN = 0.15
WHEEL_MAX_GAP = 0.5
WHEEL_CONFIDENCE = 0.8


def wheel_chance(card: Any, wheel_pick: int) -> float:
    """How likely a card is still in the pack at ``wheel_pick`` (17Lands average last seen)."""
    alsa = getattr(card, "alsa", None)
    if alsa is None:
        return 0.0
    return 1.0 / (1.0 + math.exp(-(float(alsa) - wheel_pick) / 0.8))


def wheel_adjust(
    ranked: list[PickScore],
    pack: list[int],
    primer: SetPrimer | None,
    *,
    pick_number: int,
    players: int = 8,
    picks_per_pass: int = 1,
) -> list[PickScore]:
    """Take the second-best card now when the best one will likely come back.

    A pack returns after every player has taken from it once, so at pick n the
    cards left after ``players * picks_per_pass`` more picks reappear at pick
    n + players. Expected value of taking A now is A plus (chance B wheels) x B,
    otherwise filler; we swap only when taking B now is clearly better.
    """
    if primer is None or len(ranked) < 2 or picks_per_pass != 1:
        return ranked
    if len(pack) <= players * picks_per_pass:
        return ranked  # this pack will not come back with anything for us
    wheel_pick = pick_number + players
    first, second = ranked[0], ranked[1]
    card_a, card_b = primer.card(first.grp_id), primer.card(second.grp_id)
    if card_a is None or card_b is None or is_ordinary_basic(second.name):
        return ranked
    if first.score - second.score > WHEEL_MAX_GAP:
        return ranked
    p_a = WHEEL_CONFIDENCE * wheel_chance(card_a, wheel_pick)
    p_b = WHEEL_CONFIDENCE * wheel_chance(card_b, wheel_pick)
    filler = WHEEL_FILLER_SCORE
    take_a = first.score + p_b * second.score + (1 - p_b) * filler
    take_b = second.score + p_a * first.score + (1 - p_a) * filler
    if take_b - take_a < WHEEL_MIN_GAIN:
        return ranked
    swapped = PickScore(
        grp_id=second.grp_id,
        name=second.name,
        score=second.score,
        components=second.components,
        band=second.band,
        partners=second.partners,
        reasons=second.reasons
        + [
            f"{first.name} likely wheels ({p_a:.0%} by 17Lands last-seen); expecting it back at pick {wheel_pick}"
        ],
    )
    return [swapped, first] + ranked[2:]


def choose_picks(
    pack: list[int],
    pool: list[int],
    primer: SetPrimer | None,
    picks_required: int = 1,
    players: int = 8,
    **kwargs: Any,
) -> list[PickScore]:
    """Top picks; for pick-two the second is re-ranked with the first already in the pool."""
    chosen: list[PickScore] = []
    remaining = list(pack)
    working_pool = list(pool)
    for index in range(max(1, min(picks_required, len(pack)))):
        ranked = rank_pack(remaining, working_pool, primer, picks_per_pass=picks_required, **kwargs)
        if not ranked:
            break
        if index == 0:
            ranked = wheel_adjust(
                ranked,
                remaining,
                primer,
                pick_number=int(kwargs.get("pick_number") or 1),
                players=players,
                picks_per_pass=picks_required,
            )
        best = ranked[0]
        chosen.append(best)
        remaining.remove(best.grp_id)
        working_pool.append(best.grp_id)
    return chosen
