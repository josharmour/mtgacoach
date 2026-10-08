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
- Archetype role: payoffs, enablers and key commons the primer lists for the
  archetype the pool is heading toward (or the format's best archetypes early).
- Synergy: primer synergy notes and archetype lists shared with picked cards.
- Signals: in pack 1, good cards arriving well after their average pick (ATA)
  suggest their colors are open upstream.

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
ROLE_WEIGHT = {"payoffs": 0.35, "enablers": 0.3, "key_uncommons": 0.25, "key_commons": 0.25}
TIER_WEIGHT = {1: 1.0, 2: 0.7, 3: 0.4}

# 17lands hides GIH below ~500 games in hand. In a mature format a common or
# uncommon that short of games is one drafters leave in the sideboard
# (2026-10-06 FRA: Winter, Team Player 27% played / 49.9% game win rate,
# Arni 20% / 48.5%, Yargle 5%), so "unrated" means "assume below average".
UNRATED_PRIOR = -1.0
UNRATED_RARE_PRIOR = {"M": 0.2, "R": 0.0}
RARELY_PLAYED_PRIOR = -1.4
RARELY_PLAYED_RATE = 0.35  # 17lands play rate; the median rated card is ~0.7
RARELY_PLAYED_SHARE = 0.45  # games vs. the median rated card of the same rarity
UNRATED_ROLE_CAP = 0.1  # the model-written primer's roles cannot carry an unrated card
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
PREMIUM_BASELINE = 0.75
BASIC_LAND_COLORS = {"plains": "W", "island": "U", "swamp": "B", "mountain": "R", "forest": "G"}

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


def is_ordinary_basic(name: str) -> bool:
    """These five basics can be added freely after drafting, even without ratings."""
    return name.strip().casefold() in {"plains", "island", "swamp", "mountain", "forest"}


@dataclass
class PickScore:
    grp_id: int
    name: str
    score: float
    reasons: list[str] = field(default_factory=list)

    def as_evaluation(self) -> dict[str, Any]:
        return {
            "grp_id": self.grp_id,
            "name": self.name,
            "score": round(self.score, 3),
            "reason": "; ".join(self.reasons),
        }


@dataclass
class Lane:
    colors: str
    weights: dict[str, float]
    commitment: float
    main: str = ""
    # How firmly the weaker lane color is held: 0 = still open, 1 = settled.
    second_hold: float = 1.0


@dataclass
class _SetStats:
    median_games: dict[str, float]
    game_wr_mean: float | None
    game_wr_sd: float | None


def _set_stats(primer: SetPrimer | None) -> _SetStats:
    """What a typical rated card of each rarity looks like, to judge unrated ones."""
    if primer is None:
        return _SetStats({}, None, None)
    rated = [card for card in primer.cards.values() if card.baseline is not None]
    median_games = {}
    for rarity in {card.rarity for card in rated}:
        games = [card.games for card in rated if card.rarity == rarity and card.games > 0]
        if len(games) >= 3:
            median_games[rarity] = float(statistics.median(games))
    game_wrs = [card.game_wr for card in rated if card.game_wr is not None]
    if len(game_wrs) >= 10 and statistics.pstdev(game_wrs) > 0:
        return _SetStats(median_games, statistics.fmean(game_wrs), statistics.pstdev(game_wrs))
    return _SetStats(median_games, None, None)


def _rarely_played(card: Any, stats: _SetStats) -> str | None:
    """Why an unrated card looks like one drafters leave out, or None."""
    if card.play_rate is not None:
        if card.play_rate < RARELY_PLAYED_RATE:
            return f"{card.play_rate:.0%} of drafted copies played"
        return None
    median = stats.median_games.get(card.rarity)
    if median and card.games / median < RARELY_PLAYED_SHARE:
        return f"{card.games / median:.0%} of a typical card's games"
    return None


def card_value(card: Any, stats: _SetStats) -> tuple[float, str, bool]:
    """(base score in z units, reason, rated) for a primer card."""
    if card.baseline is not None:
        return card.baseline, f"GIH {card.gih_wr:.1%} (z {card.baseline:+.2f})", True
    if "Land" in card.types and not card.colors and card.rarity in {"C", ""}:
        return -2.0, "basic/utility land", False
    rarely = _rarely_played(card, stats)
    if rarely:
        return RARELY_PLAYED_PRIOR, f"unrated; rarely played ({rarely})", False
    if card.game_wr is not None and stats.game_wr_sd:
        z = max(-2.0, min(1.5, (card.game_wr - stats.game_wr_mean) / stats.game_wr_sd))
        return round(z, 3), f"unrated; game win rate {card.game_wr:.1%}", False
    if card.rarity in UNRATED_RARE_PRIOR:
        return UNRATED_RARE_PRIOR[card.rarity], "unrated rare; rarity prior", False
    return UNRATED_PRIOR, "unrated; assumed below average", False


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


def rank_pack(
    pack: list[int],
    pool: list[int],
    primer: SetPrimer | None,
    *,
    pack_number: int = 1,
    pick_number: int = 1,
    names: dict[int, str] | None = None,
    mana_costs: dict[int, str] | None = None,
) -> list[PickScore]:
    """Best pick first; never returns cards outside the pack."""
    names = names or {}
    mana_costs = mana_costs or {}
    stats = _set_stats(primer)
    lane = pool_lane(pool, primer, mana_costs)
    main = lane.main or (lane.colors[:1] if lane.colors else "")
    second = "".join(color for color in lane.colors if color != main)
    # The weaker color stays open through pack 2; pack 3 settles it by pick 8.
    hold = lane.second_hold
    if pack_number >= 3:
        hold = max(hold, min(1.0, max(0.0, (pick_number - 2) / 6)))
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
        score = base

        colors = card.colors if card else ""
        commitment = lane.commitment
        needs = _mana_needs(card, mana_costs) if card is not None else []
        land = card is not None and is_nonbasic_land(card)
        if land:
            # A land is judged by the mana it makes, not its (empty) cost.
            if rated and base > 0:
                score -= min(LAND_GIH_DISCOUNT, base)
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
            fit = 0.1
        elif not lane.colors:
            fit = 0.0 if any(_payable(needs, color) for color in COLOR_ORDER) else -0.15
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
        score += fit

        if primer is not None and card is not None:
            if len(playable_pool) >= 4 and fits_lane(card):
                if provides_body(card):
                    if bodies < len(playable_pool) * 0.6:
                        score += 0.45
                        reasons.append("pool needs more creatures")
                    if (
                        card.cmc is not None
                        and card.cmc <= 2
                        and cheap_bodies < max(2, len(playable_pool) * 0.18)
                    ):
                        score += 0.45
                        reasons.append("fills the early creature curve; needs one- and two-mana bodies")
                    elif (
                        card.cmc is not None
                        and card.cmc <= 3
                        and early_bodies < max(3, len(playable_pool) * 0.25)
                    ):
                        score += 0.35
                        reasons.append("fills the early creature curve")
                if card.cmc is not None and card.cmc >= 5 and expensive >= max(2, len(playable_pool) * 0.2):
                    score -= 0.5
                    reasons.append("pool already has enough expensive spells")
                if is_removal(card) and removal < max(2.0, len(playable_pool) * REMOVAL_SHARE):
                    cheap = card.cmc is not None and card.cmc <= 3
                    score += 0.4 if cheap else 0.2
                    reasons.append(f"pool needs removal ({removal} so far)")
            best_role = 0.0
            role_notes = []
            for arch_colors, role in primer.card_roles(card.name):
                archetype = primer.archetype(arch_colors) or {}
                if lane.colors and commitment >= 0.3:
                    shared = len(set(arch_colors) & set(lane.colors))
                    relevance = 1.0 if arch_colors == lane.colors else 0.6 if shared else 0.1
                else:
                    relevance = 0.5 * TIER_WEIGHT.get(archetype.get("tier", 2), 0.7)
                value = ROLE_WEIGHT.get(role, 0.2) * relevance
                if value > best_role:
                    best_role = value
                    role_notes = [
                        f"{role.rstrip('s').replace('_', ' ')} for {archetype.get('name', arch_colors)}"
                    ]
            if not rated:
                # The primer's archetype lists are model-written; they cannot
                # lift a card the data says nobody plays.
                best_role = min(best_role, 0.0 if rarely else UNRATED_ROLE_CAP)
                role_notes = role_notes if best_role > 0 else []
            score += best_role
            reasons += role_notes

            synergy = 0.0
            for note in primer.synergy_notes:
                members = [n.lower() for n in note.get("cards") or []]
                if card.name.lower() in members:
                    partners = sum(pool_names[m] for m in members if m != card.name.lower())
                    if partners:
                        synergy += 0.2 * partners
                        reasons.append(f"synergy: {note.get('why', '')[:80]}")
            score += min(synergy, 0.6 if rated else 0.0 if rarely else UNRATED_ROLE_CAP)

            if pack_number <= 2 and 4 <= pick_number <= 10 and colors:
                signal = 0.3 * max(openness[c] for c in colors)
                if signal >= 0.15:
                    reasons.append(f"{colors} looks open")
                score += signal

            if primer.is_trap(card.name):
                score -= 0.4
                reasons.append("primer trap")

            copies = pool_names[card.name.lower()]
            if copies and not land and base < PREMIUM_BASELINE and not is_removal(card):
                # A second body is just a playable; a second trick or pump spell is redundancy.
                redundant = copies - 1 if provides_body(card) else copies
                step = DUPLICATE_STEP_BODY if provides_body(card) else DUPLICATE_STEP_SPELL
                if redundant > 0:
                    score -= step * redundant
                    reasons.append(f"{copies} already in pool")
        scores[grp_id] = PickScore(grp_id=grp_id, name=name, score=round(score, 4), reasons=reasons)
    return sorted(scores.values(), key=lambda pick: (is_ordinary_basic(pick.name), -pick.score))


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
        ranked = rank_pack(remaining, working_pool, primer, **kwargs)
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
