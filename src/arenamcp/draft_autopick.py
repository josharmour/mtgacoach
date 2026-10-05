"""Deterministic draft pick ranking driven by the set primer.

Industry-standard bot drafting, made explicit and explainable:

- Card quality: the card's 17lands games-in-hand win rate as a z-score within
  the set (``SetCard.baseline``); rarity priors when a card is unrated.
- Lane: the pool's colors, weighted by card quality. Commitment grows with the
  pool, so early picks stay flexible and late off-color picks are discounted.
- Archetype role: payoffs, enablers and key commons the primer lists for the
  archetype the pool is heading toward (or the format's best archetypes early).
- Synergy: primer synergy notes and archetype lists shared with picked cards.
- Signals: in pack 1, good cards arriving well after their average pick (ATA)
  suggest their colors are open upstream.

The model-backed DraftAdvisor sees this ranking as evidence; when the model is
unavailable or its answer fails validation, this ranking makes the pick.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from arenamcp.set_primer import COLOR_ORDER, SetPrimer, normalize_colors

RARITY_PRIOR = {"M": 0.3, "R": 0.2, "U": -0.15, "C": -0.4}
ROLE_WEIGHT = {"payoffs": 0.35, "enablers": 0.3, "key_uncommons": 0.25, "key_commons": 0.25}
TIER_WEIGHT = {1: 1.0, 2: 0.7, 3: 0.4}


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


def pool_lane(pool: list[int], primer: SetPrimer | None) -> Lane:
    """The pool's two strongest colors and how committed the drafter should be to them."""
    weights = {color: 0.0 for color in COLOR_ORDER}
    for grp_id in pool:
        card = primer.card(grp_id) if primer else None
        if card is None or not card.colors:
            continue
        quality = max(0.25, 1.0 + (card.baseline if card.baseline is not None else -0.5))
        for color in card.colors:
            weights[color] += quality / len(card.colors)
    ranked = sorted((color for color in COLOR_ORDER if weights[color] > 0), key=lambda c: -weights[c])
    colors = normalize_colors("".join(ranked[:2]))
    total = sum(weights.values())
    focus = (sum(weights[c] for c in colors) / total) if total else 0.0
    commitment = min(1.0, len(pool) / 14) * (0.6 + 0.4 * focus)
    return Lane(colors=colors, weights=weights, commitment=round(commitment, 3))


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
) -> list[PickScore]:
    """Best pick first; never returns cards outside the pack."""
    names = names or {}
    lane = pool_lane(pool, primer)
    openness = color_openness(pack, pick_number, primer)
    pool_names = (
        Counter(primer.card(g).name.lower() for g in pool if primer and primer.card(g))
        if primer
        else Counter()
    )
    scores: dict[int, PickScore] = {}
    for grp_id in pack:
        if grp_id in scores:
            continue
        card = primer.card(grp_id) if primer else None
        name = card.name if card else names.get(grp_id, f"Card {grp_id}")
        reasons: list[str] = []
        if card is not None and card.baseline is not None:
            base = card.baseline
            reasons.append(f"GIH {card.gih_wr:.1%} (z {card.baseline:+.2f})")
        elif card is not None and "Land" in card.types and not card.colors and card.rarity in {"C", ""}:
            base = -2.0
            reasons.append("basic/utility land")
        else:
            base = RARITY_PRIOR.get(card.rarity if card else "", -0.5)
            reasons.append("unrated; rarity prior")
        score = base

        colors = card.colors if card else ""
        commitment = lane.commitment
        if not colors:
            fit = 0.1
        elif not lane.colors:
            fit = -0.15 if len(colors) > 1 else 0.0
        elif set(colors) <= set(lane.colors):
            fit = 0.6 * commitment
            reasons.append(f"in lane {lane.colors}")
        elif set(colors) & set(lane.colors):
            fit = -0.3 * commitment
        else:
            fit = -1.2 * commitment
            if commitment > 0.3:
                reasons.append(f"off lane {lane.colors}")
        score += fit

        if primer is not None and card is not None:
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
            score += min(synergy, 0.6)

            if pack_number == 1 and 4 <= pick_number <= 10 and colors:
                signal = 0.3 * max(openness[c] for c in colors)
                if signal >= 0.15:
                    reasons.append(f"{colors} looks open")
                score += signal

            if primer.is_trap(card.name):
                score -= 0.4
                reasons.append("primer trap")
        scores[grp_id] = PickScore(grp_id=grp_id, name=name, score=round(score, 4), reasons=reasons)
    return sorted(scores.values(), key=lambda pick: -pick.score)


def choose_picks(
    pack: list[int],
    pool: list[int],
    primer: SetPrimer | None,
    picks_required: int = 1,
    **kwargs: Any,
) -> list[PickScore]:
    """Top picks; for pick-two the second is re-ranked with the first already in the pool."""
    chosen: list[PickScore] = []
    remaining = list(pack)
    working_pool = list(pool)
    for _ in range(max(1, min(picks_required, len(pack)))):
        ranked = rank_pack(remaining, working_pool, primer, **kwargs)
        if not ranked:
            break
        best = ranked[0]
        chosen.append(best)
        remaining.remove(best.grp_id)
        working_pool.append(best.grp_id)
    return chosen
