"""The deck we are drafting toward: live archetype hypotheses and what each still needs.

Strong drafters hold two or three possible decks early, narrow to one as the
pool and the table's signals agree, and value each pick by what it adds to
that deck. This module keeps that picture for one pick:

- Hypotheses: archetypes from the playbook, supported by how well the pool
  already fits each (rules-text fit, lightly weighted by card quality), the
  archetype's tier, and the colors the table is passing. Pack 1 keeps three,
  pack 2 two, pack 3 one; a lane the advisor has committed to is the only
  hypothesis.
- Ledger: per hypothesis, what the castable pool already holds -- creatures,
  early creatures, removal, card advantage, finishers, and each engine's
  enablers and payoffs.
- Marginal value: what a candidate adds -- an engine payoff is worth the
  enablers already drafted; an enabler is worth the payoffs it would feed,
  less once there are plenty; rules-text links to cards in the pool count.

Nothing here reads 17lands win rates except a pool card's quality weight on
hypothesis support.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from arenamcp.draft_playbook import (
    BROAD_MECHANICS,
    ArchetypePlan,
    CardTags,
    Playbook,
    engine_label,
    family,
    playbook_for,
)
from arenamcp.limited_rules import synergy_evidence
from arenamcp.set_primer import SetPrimer, normalize_colors

COMMIT_AT = 0.6
COMMIT_HOLD = 0.5
STAGE_KEEP = {1: 3, 2: 2}
STAGE_KEEP_LATE = 1
TIER_SUPPORT = {1: 1.0, 2: 0.5, 3: 0.0}
SIGNAL_SUPPORT = 3.0  # times the pair's mean openness (0-1)
# Pack 3 signals come too late to move in on: the pool settles the second color.
SIGNAL_PACK_FACTOR = {1: 1.0, 2: 1.0, 3: 0.3}
OPEN_KEEP_LATE = 2
# The pool's quality-weighted color mass (``pool_lane`` weights) behind a
# pair, as a share of the candidate colors: the same evidence the lane uses,
# so hypotheses and lane agree on the second color.
COLOR_SUPPORT = 4.0
SUPPORT_TEMPERATURE = 1.5
# Pool quality only scales a pool card's contribution to support: 0.5x-1.5x.
QUALITY_SUPPORT_SLOPE = 0.25

PAYOFF_GAIN = 0.5
# Of the future picks that will offer an engine card, the share we would spend
# on one (other needs compete): enablers we can expect to add, payoffs likewise.
ENABLER_TAKE_RATE = 0.35
PAYOFF_TAKE_RATE = 0.25
PAYOFF_FULL_ENABLERS = 8.0
# A planeswalker activates one loyalty ability a turn, so payoffs that grant
# planeswalkers abilities share them: each wants this many of its own.
WALKERS_PER_PAYOFF = 2.0
# Jace token makers together field this many planeswalkers (``engine_enablers``).
TOKEN_WALKERS = 1.0
PAYOFF_SATURATION = 6
ENABLER_GAIN = 0.4
EDGE_GAIN = 0.6
EDGE_SCALE = 3.0
EDGE_INDIRECT = 0.5
# How an engine's enablers are counted aloud ("a payoff for our 8 noncreature spells").
ENGINE_NOUNS = {
    "noncreature_spell": "noncreature spells",
    "instant_sorcery": "instants and sorceries",
    "surveil": "scry and surveil cards",
    "planeswalker": "Jace and planeswalker cards",
    "graveyard": "graveyard fillers",
    "creature_entry": "creatures",
    "artifact_entry": "artifacts",
    "lifegain": "life-gain cards",
    "plus_counters": "+1/+1 counter cards",
    "draw": "card-draw cards",
    "noncombat_damage": "damage pingers",
    "dies": "sacrifice outlets",
}


@dataclass
class Ledger:
    colors: str
    cards: list[int] = field(default_factory=list)
    creatures: int = 0
    early: int = 0
    removal: int = 0
    card_advantage: int = 0
    finishers: int = 0
    enablers: dict[str, float] = field(default_factory=dict)
    payoffs: dict[str, int] = field(default_factory=dict)
    # Planeswalker cards, and cards that only make or empower a Jace token.
    walkers: int = 0
    walker_makers: int = 0

    def as_prompt(self, plan: ArchetypePlan) -> dict[str, Any]:
        return {
            "castable_cards": len(self.cards),
            "creatures": self.creatures,
            "two_drops_or_less": self.early,
            "removal": self.removal,
            "card_advantage": self.card_advantage,
            "finishers": self.finishers,
            "engine": {
                engine_label(m): {
                    "enablers": round(self.enablers.get(m, 0.0), 1),
                    "payoffs": self.payoffs.get(m, 0),
                }
                for m in plan.engine
            },
        }


@dataclass
class Hypothesis:
    plan: ArchetypePlan
    weight: float
    support: float
    ledger: Ledger


@dataclass
class DeckPlan:
    playbook: Playbook
    hypotheses: list[Hypothesis]
    committed: bool = False

    def lead(self) -> Hypothesis | None:
        return self.hypotheses[0] if self.hypotheses else None

    def castable_share(self, castable: dict[str, bool]) -> float:
        """How plausible a deck that casts this card is, against the leading deck (0-1).

        The best live hypothesis whose colors pay for the card, relative to
        the lead: a card for a second pair nearly as likely as the lead
        counts nearly in full, rather than splitting the lane four ways.
        """
        lead = self.hypotheses[0].weight if self.hypotheses else 0.0
        best = max((h.weight for h in self.hypotheses if castable.get(h.plan.colors)), default=0.0)
        return min(1.0, best / lead) if lead > 0 else 0.0

    def as_prompt(self) -> list[dict[str, Any]]:
        return [
            {
                "colors": h.plan.colors,
                "archetype": h.plan.name,
                "tier": h.plan.tier,
                "weight": round(h.weight, 2),
                "engine": [engine_label(m) for m in h.plan.engine],
                "pool": h.ledger.as_prompt(h.plan),
            }
            for h in self.hypotheses
        ]


def _card_dict(primer: SetPrimer, grp_id: int) -> dict[str, Any] | None:
    card = primer.card(grp_id)
    if card is None:
        return None
    return {"grp_id": card.grp_id, "name": card.name, "type_line": card.types, "oracle_text": card.oracle}


def _castable(primer: SetPrimer, grp_id: int, colors: str, mana_costs: dict[int, str]) -> bool:
    from arenamcp.draft_autopick import lane_fit

    return lane_fit(primer.card(grp_id), colors, mana_costs) in ("in", "colorless", "fixing")


def _ledger(book: Playbook, pool: list[int], colors: str, mana_costs: dict[int, str]) -> Ledger:
    ledger = Ledger(colors=colors)
    for grp_id in pool:
        tags = book.tag(grp_id)
        if tags is None or not _castable(book.primer, grp_id, colors, mana_costs):
            continue
        ledger.cards.append(grp_id)
        roles = tags.roles
        ledger.creatures += "body" in roles
        ledger.early += "early_body" in roles
        ledger.removal += "removal" in roles
        ledger.card_advantage += "card_advantage" in roles
        ledger.finishers += "finisher" in roles
        if tags.land:
            continue
        for engine in tags.engine_payoffs():
            ledger.payoffs[engine] = ledger.payoffs.get(engine, 0) + 1
        for engine in tags.engine_enables():
            ledger.enablers[engine] = ledger.enablers.get(engine, 0.0) + 1.0
        if tags.planeswalker:
            ledger.walkers += 1
        elif "planeswalker" in tags.engine_enables():
            ledger.walker_makers += 1
        for engine in tags.engine_indirect():
            ledger.enablers[engine] = ledger.enablers.get(engine, 0.0) + EDGE_INDIRECT
    return ledger


def build_deck_plan(
    pool: list[int],
    primer: SetPrimer | None,
    *,
    lane_colors: str = "",
    lane_main: str = "",
    commitment: float = 0.0,
    second_hold: float = 1.0,
    lane_weights: dict[str, float] | None = None,
    plan_lane: str = "",
    signals: dict[str, float] | None = None,
    pack_number: int = 1,
    mana_costs: dict[int, str] | None = None,
) -> DeckPlan | None:
    """The live archetype hypotheses for this pool, strongest first.

    Support for an archetype: its tier, the table's signals for its colors
    (less in pack 3), the pool's quality-weighted color mass behind its
    colors (``pool_lane`` weights; against the other candidate second colors
    once the main color is set), and the archetype-specific fit (engine
    pieces, key cards) of the pool cards it can cast.
    """
    from arenamcp.draft_autopick import _mana_needs, _payable, _set_stats, card_value

    book = playbook_for(primer)
    if book is None or not book.archetypes:
        return None
    mana_costs = mana_costs or {}
    signals = signals or {}
    weights = lane_weights or {}
    committed_key = normalize_colors(plan_lane)
    committed = book.archetype(committed_key) if len(committed_key) == 2 else None
    if committed is None and commitment >= COMMIT_AT and second_hold >= COMMIT_HOLD and len(lane_colors) == 2:
        # The pool alone commits only when both colors are held: a main color
        # with an open second one keeps its pairs alive (2026-10-06 P2p7).
        committed = book.archetype(lane_colors)
    if committed is not None:
        ledger = _ledger(book, pool, committed.colors, mana_costs)
        return DeckPlan(book, [Hypothesis(committed, 1.0, 0.0, ledger)], committed=True)

    stats = _set_stats(primer)
    settled_main = bool(lane_main) and commitment >= 0.3
    open_second = settled_main and second_hold < COMMIT_HOLD
    signal_factor = SIGNAL_SUPPORT * SIGNAL_PACK_FACTOR.get(pack_number, SIGNAL_PACK_FACTOR[3])
    candidates = book.archetypes
    if settled_main:
        candidates = [plan for plan in candidates if lane_main in plan.colors] or candidates
    # The pool's color mass: with a main color, the candidates differ only in
    # their second color, so share it among the non-main colors.
    counted = {c: v for c, v in weights.items() if not (settled_main and c == lane_main)}
    mass_total = sum(counted.values())
    scored: list[tuple[float, ArchetypePlan, Ledger]] = []
    for plan in candidates:
        ledger = _ledger(book, pool, plan.colors, mana_costs)
        support = TIER_SUPPORT.get(plan.tier, 0.5)
        support += signal_factor * sum(signals.get(c, 0.0) for c in plan.colors) / 2
        if mass_total > 0:
            support += COLOR_SUPPORT * sum(counted.get(c, 0.0) for c in plan.colors) / mass_total
        for grp_id in ledger.cards:
            card = primer.card(grp_id)
            # A main-color card is castable in every candidate: being on one
            # archetype's key-card list is no evidence for that pair's second
            # color. Engine pieces still are (surveil cards point to WU).
            main_only = (
                settled_main and card is not None and _payable(_mana_needs(card, mana_costs), lane_main)
            )
            fit = book.fit(grp_id, plan, roles=False, key_lists=not main_only)[0]
            if fit <= 0:
                continue
            quality = card_value(card, stats)[0] if card is not None else 0.0
            support += fit * max(0.5, min(1.5, 1.0 + QUALITY_SUPPORT_SLOPE * quality))
        scored.append((support, plan, ledger))
    # Ties (an empty pool early) go to the archetypes that win most.
    scored.sort(key=lambda row: (-row[0], -(row[1].win_rate or 0.0), row[1].colors))
    keep = STAGE_KEEP.get(pack_number, STAGE_KEEP_LATE)
    if open_second:
        # The main color is settled, the second is not: every pair with the
        # main color stays alive (two in pack 3), weighted by support.
        keep = len(scored) if pack_number < 3 else OPEN_KEEP_LATE
    kept = scored[:keep]
    top = kept[0][0] if kept else 0.0
    raw = [math.exp((support - top) / SUPPORT_TEMPERATURE) for support, _plan, _ledger in kept]
    total = sum(raw) or 1.0
    hypotheses = [
        Hypothesis(plan, weight / total, round(support, 3), ledger)
        for weight, (support, plan, ledger) in zip(raw, kept, strict=True)
    ]
    return DeckPlan(book, hypotheses)


# Scarcity: a card with few comparable substitutes still to come is worth
# taking now. Substitutes share its role, fit the deck, and are within this
# much quality (z) of it.
SUBSTITUTE_MARGIN = 0.25
SCARCITY_SCALE = 4.0
SCARCITY_GAIN = 0.5


@dataclass
class FutureSupply:
    """What the rest of the draft can still offer: our remaining looks at packs.

    schedule counts our future looks by how picked-over the pack is
    (1 = fresh, 14 = last card). expected(cards) is how many of those
    looks will offer at least one of cards, from each card's copies per
    pack (rarity) and survival (17lands last-seen pick).
    """

    book: Playbook
    schedule: Counter

    @property
    def looks(self) -> int:
        return sum(self.schedule.values())

    def expected(self, grp_ids) -> float:
        totals = [0.0] * 14
        for grp_id in set(grp_ids):
            row = self.book.availability.get(grp_id)
            if row is not None:
                totals = [a + b for a, b in zip(totals, row, strict=True)]
        return sum(count * (1.0 - math.exp(-totals[pick - 1])) for pick, count in self.schedule.items())


def future_supply(
    book: Playbook, pack_number: int, pick_number: int, picks_per_pass: int = 1, packs: int = 3
) -> FutureSupply:
    """Our looks after this pick: the rest of this pack and the packs still to open."""
    per_pass = max(1, picks_per_pass)
    looks_per_pack = max(1, 14 // per_pass)
    schedule: Counter = Counter()
    for pack in range(max(1, pack_number), packs + 1):
        first = pick_number + 1 if pack == pack_number else 1
        for look in range(first, looks_per_pack + 1):
            # How picked-over the pack is when it reaches us, in single-pick terms.
            schedule[min(14, per_pass * (look - 1) + 1)] += 1
    return FutureSupply(book, schedule)


def scarcity(
    tags: CardTags,
    quality: float,
    plan: ArchetypePlan,
    supply: FutureSupply,
    castable_in,
    quality_of,
) -> tuple[float, str]:
    """(0-1, words) how rare a card like this will be in the packs still to come.

    Only roles a deck cannot fill from any common: removal, and payoffs for
    this archetype's engine. castable_in(grp_id, colors) and
    quality_of(grp_id) come from the ranking.
    """
    roles = []
    if "removal" in tags.roles:
        roles.append(("removal", lambda t: "removal" in t.roles))
    for engine in plan.engine:
        if engine in tags.engine_payoffs() and not tags.land:
            roles.append((f"payoffs for {engine_label(engine)}", lambda t, e=engine: e in t.engine_payoffs()))
    best, why = 0.0, ""
    for words, has_role in roles:
        # Another copy of this very card counts: it may still come.
        substitutes = [
            grp_id
            for grp_id, other in supply.book.tags.items()
            if has_role(other)
            and quality_of(grp_id) >= quality - SUBSTITUTE_MARGIN
            and castable_in(grp_id, plan.colors)
        ]
        chances = supply.expected(substitutes)
        value = math.exp(-chances / SCARCITY_SCALE)
        if value > best:
            best = value
            few = "little" if words == "removal" else "few"
            verb = "is" if words == "removal" else "are"
            why = (
                f"scarce: {few} {words} this good {verb} left to come"
                if chances < 1.0
                else f"scarce: about {chances:.0f} more chances at {words} this good"
            )
    return round(best, 4), why


def engine_gain(
    tags: CardTags, hypothesis: Hypothesis, supply: FutureSupply | None = None
) -> tuple[float, str]:
    """What a card adds to this archetype's engine, given what the pool holds and what is still to come."""
    if tags.land:
        return 0.0, ""
    ledger = hypothesis.ledger
    payoffs, enables, indirect = tags.engine_payoffs(), tags.engine_enables(), tags.engine_indirect()
    best, why = 0.0, ""
    for engine in hypothesis.plan.engine:
        label = engine_label(engine)
        have_enablers = ledger.enablers.get(engine, 0.0)
        have_payoffs = ledger.payoffs.get(engine, 0)
        if engine in payoffs:
            # A payoff is worth the triggers already drafted plus those the
            # remaining packs will realistically give us.
            have_enablers = engine_enablers(tags, ledger, engine)
            coming = ENABLER_TAKE_RATE * _engine_supply(supply, hypothesis.plan, engine, "enables")
            value = PAYOFF_GAIN * min(1.0, (have_enablers + coming) / PAYOFF_FULL_ENABLERS)
            value *= tags.payoff_strength
            if have_payoffs >= PAYOFF_SATURATION:
                value *= 0.5
            noun = ENGINE_NOUNS.get(engine, f"{label} cards")
            reason = (
                f"a payoff for our {have_enablers:.0f} {noun}"
                if have_enablers >= 1
                else f"a payoff for {label}"
            )
        elif engine in enables or engine in indirect:
            strength = BROAD_MECHANICS.get(engine, (0, 1.0))[1] * (
                1.0 if engine in enables else EDGE_INDIRECT
            )
            # Enablers matter once something pays them off (or payoffs are still
            # likely to come); past ~3 per payoff they are plentiful.
            coming = PAYOFF_TAKE_RATE * _engine_supply(supply, hypothesis.plan, engine, "payoffs")
            demand = min(1.0, (have_payoffs + coming + 0.25) / 2.0)
            room = max(0.0, 1.0 - have_enablers / max(6.0, 3.0 * have_payoffs))
            value = ENABLER_GAIN * strength * demand * room
            reason = (
                f"feeds our {have_payoffs} {label} payoff{'s' if have_payoffs != 1 else ''}"
                if have_payoffs
                else f"an enabler for {label}"
            )
        else:
            continue
        if value > best:
            best, why = value, reason
    return round(best, 4), why


def engine_enablers(tags: CardTags, ledger: Ledger, engine: str) -> float:
    """The pool's enablers for this payoff's engine.

    "Empower Jace" creates a Jace token only when we have none and otherwise
    adds loyalty, so all the token makers together field one planeswalker
    (TOKEN_WALKERS): a payoff for planeswalkers we control counts real
    planeswalker cards (2026-10-18 pro draft: Way of the Healer over the
    pool's lifegain engine on four empower cards and one Garruk). A
    loyalty-counter payoff gains from every empower, so it counts them all.
    A payoff that empowers Jace itself fields the token too.
    """
    if engine != "planeswalker" or "loyalty" in tags.payoffs:
        return ledger.enablers.get(engine, 0.0)
    makers = ledger.walker_makers + (not tags.planeswalker and engine in tags.engine_enables())
    return ledger.walkers + tags.planeswalker + min(TOKEN_WALKERS, makers)


def payoff_support(tags: CardTags, hypothesis: Hypothesis, supply: FutureSupply | None) -> dict[str, float]:
    """engine -> 0-1: how well the pool and the packs to come feed this card's payoffs.

    Only planeswalker payoffs are scaled: "planeswalkers you control have ..."
    pays off only the planeswalkers we field (``engine_enablers``, plus the
    planeswalkers the remaining packs will realistically give), and payoffs
    already held share them (WALKERS_PER_PAYOFF each). A walker-rich pool
    (2026-10-18, the pro's second draft: Ajani Resolute and Chandra beside
    the empower cards) backs them in full; four empower cards and one Garruk
    back the first such payoff, not a second. Other engines' triggers feed every
    payoff at once, and their payoff value already follows the enablers
    (``engine_gain``).
    """
    if "planeswalker" not in hypothesis.plan.engine or "planeswalker" not in tags.engine_payoffs():
        return {}
    if "loyalty" in tags.payoffs:
        return {}  # every empower triggers it
    ledger = hypothesis.ledger
    have = engine_enablers(tags, ledger, "planeswalker")
    coming = ENABLER_TAKE_RATE * _engine_supply(supply, hypothesis.plan, "planeswalker", "walkers")
    if have - ledger.walkers - tags.planeswalker < TOKEN_WALKERS:
        coming += TOKEN_WALKERS  # empower cards are common: the token will come
    held = ledger.payoffs.get("planeswalker", 0)
    return {"planeswalker": min(1.0, (have + coming) / (WALKERS_PER_PAYOFF * (held + 1)))}


def _engine_supply(supply: FutureSupply | None, plan: ArchetypePlan, engine: str, side: str) -> float:
    """Future looks offering an enabler (side="enables"), payoff or planeswalker card ("walkers") in these colors.

    Without a supply model: a couple of enablers, no payoffs (the old assumption).
    """
    if supply is None:
        return 2.0 / ENABLER_TAKE_RATE if side == "enables" else 0.0
    key = (plan.colors, engine, side, tuple(sorted(supply.schedule.items())))
    cache = supply.book.__dict__.setdefault("_engine_supply_cache", {})
    if key not in cache:
        cards = [
            grp_id
            for grp_id, tags in supply.book.tags.items()
            if not tags.land
            and (not tags.colors or set(tags.colors) <= set(plan.colors))
            and (
                tags.planeswalker
                if side == "walkers"
                else engine in (tags.engine_enables() if side == "enables" else tags.engine_payoffs())
            )
        ]
        cache[key] = supply.expected(cards)
    return cache[key]


def pool_edges(
    primer: SetPrimer, grp_id: int, partners: list[int], book: Playbook | None = None
) -> tuple[float, list[str]]:
    """(value, partner names) for rules-text links between this card and pool cards.

    A link counts by its mechanic's breadth, half when indirect, and by the
    payoff's strength (a prowess trigger is worth less than a token).
    """
    book = book or playbook_for(primer)
    card = _card_dict(primer, grp_id)
    if card is None:
        return 0.0, []
    weight_total = 0.0
    named: list[tuple[float, str]] = []
    for partner_id in dict.fromkeys(partners):
        partner = _card_dict(primer, partner_id)
        if partner is None or partner_id == grp_id:
            continue
        best = 0.0
        for edge in synergy_evidence(card, partner):
            strength = BROAD_MECHANICS.get(family(edge["mechanic"]), (0, 1.0))[1]
            if edge.get("indirect"):
                strength *= EDGE_INDIRECT
            payoff = book.tag(edge["payoff_id"]) if book is not None else None
            if payoff is not None:
                strength *= payoff.payoff_strength
            best = max(best, strength)
        if best > 0:
            copies = partners.count(partner_id)
            weight_total += best * copies
            named.append((best, partner["name"]))
    value = EDGE_GAIN * (1.0 - math.exp(-weight_total / EDGE_SCALE))
    named.sort(key=lambda row: -row[0])
    return round(value, 4), [name for _strength, name in named]
