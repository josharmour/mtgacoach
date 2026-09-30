"""Bounded joint attack-recipient and defending-block assignment search."""

import json
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from itertools import product

from arenamcp.combat_solver import _can_block, _has, _material, _memoized, _resolve_attacker, optimal_blocks
from arenamcp.combat_targets import attack_candidates, recipient_key, recipient_label


def unproductive_attackers(state: dict, pending: dict | None = None) -> set[int]:
    """Known zero-power choices with no visible non-damage reason to attack.

    Preserve mandatory attacks and abstain when rules text offers an attack
    payoff, a pump trick, or damage based on toughness. Unknown power is not zero.
    """
    local = next((p.get("seat_id") for p in state.get("players", []) if p.get("is_local")), None)
    if local is None:
        return set()
    battlefield = state.get("battlefield") or []
    for card in battlefield:
        text = (card.get("oracle_text") or "").lower()
        if "combat damage" in text and "toughness" in text:
            return set()
        if card.get("controller_seat_id", card.get("owner_seat_id")) == local and re.search(
            r"\battack(?:s|ing|ed|ers?)?\b|\b(?:exert|exalted|annihilator|battle cry|myriad|melee)\b"
            r"|gets?\s+\+|base power",
            text,
        ):
            return set()
    for card in state.get("hand") or []:
        text = (card.get("oracle_text") or "").lower()
        if re.search(r"\braid\b|you attacked this turn", text) or (
            "instant" in (card.get("type_line") or "").lower()
            and re.search(r"gets?\s+\+|base power|combat damage", text)
        ):
            return set()
    cards = {card.get("instance_id"): card for card in battlefield}
    result = set()
    for candidate in attack_candidates(state, pending) or []:
        identity = candidate.get("attackerInstanceId")
        if candidate.get("mustAttack") or identity not in cards:
            continue
        try:
            if int(cards[identity].get("power")) <= 0:
                result.add(identity)
        except (ValueError, TypeError):
            continue
    return result


@dataclass
class CombatChoice:
    assignments: dict[int, dict] = field(default_factory=dict)
    player_damage: int = 0
    planeswalkers_removed: list[int] = field(default_factory=list)
    crackback: int = 0
    score: float = 0
    explanation: str = ""
    bounded: bool = False


def loyalty(card: dict) -> int | None:
    counters = card.get("counters") or {}
    value = card.get("loyalty")
    if value is None:
        value = next(
            (count for kind, count in counters.items() if str(kind).lower().endswith("loyalty")), None
        )
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def combat_choice(state: dict, *, budget: int = 20000, deadline_s: float = 0.7) -> CombatChoice | None:
    raw = attack_candidates(state)
    if not raw:
        return None
    relevant = {key: state.get(key) for key in ("players", "battlefield", "decision_context")}
    key = ("combat_recipients", json.dumps(relevant, sort_keys=True, default=str), budget, deadline_s)
    return _memoized(key, lambda: _search(state, raw, budget, deadline_s))


def _search(state: dict, raw: list[dict], budget: int, deadline_s: float) -> CombatChoice | None:
    players = state.get("players") or []
    local = next((player for player in players if player.get("is_local")), None)
    opponent = next((player for player in players if not player.get("is_local")), None)
    if local is None or opponent is None:
        return None
    local_id, opponent_id = local["seat_id"], opponent["seat_id"]
    your_life, opponent_life = int(local.get("life_total", 20)), int(opponent.get("life_total", 20))
    battlefield = state.get("battlefield") or []
    cards = {card["instance_id"]: card for card in battlefield if card.get("instance_id")}
    creatures = [
        card
        for card in battlefield
        if "creature" in str(card.get("type_line", "")).lower()
        or "CardType_Creature" in (card.get("card_types") or [])
    ]
    yours = [
        card for card in creatures if card.get("controller_seat_id", card.get("owner_seat_id")) == local_id
    ]
    theirs = [
        card for card in creatures if card.get("controller_seat_id", card.get("owner_seat_id")) == opponent_id
    ]
    blockers = [card for card in theirs if not card.get("is_tapped")]
    candidates, choices = [], []
    for entry in raw:
        identity = int(entry.get("attackerInstanceId") or 0)
        card = cards.get(identity)
        if card not in yours:
            continue
        legal = []
        for recipient in entry.get("legalDamageRecipients") or []:
            try:
                kind, target_id = recipient_key(recipient)
            except (TypeError, ValueError):
                continue
            if (kind == "player" and target_id == opponent_id) or (
                kind == "planeswalker" and target_id in cards
            ):
                legal.append(recipient)
        if not legal:
            return None
        candidates.append(card)
        choices.append(legal if entry.get("mustAttack") else [None, *legal])
    if not candidates:
        return None
    started = time.monotonic()
    remaining = budget
    best = None
    seen = set()

    def assignments():
        yield tuple(None for _card in candidates)
        targets = {recipient_key(recipient) for options in choices for recipient in options if recipient}
        for target in sorted(targets):
            yield tuple(
                next(
                    (recipient for recipient in options if recipient and recipient_key(recipient) == target),
                    None,
                )
                for options in choices
            )
        yield from product(*choices)

    for selection in assignments():
        if any(recipient not in options for recipient, options in zip(selection, choices, strict=True)):
            continue
        signature = tuple(recipient_key(recipient) if recipient else None for recipient in selection)
        if signature in seen:
            continue
        seen.add(signature)
        if best is not None and (remaining <= 0 or time.monotonic() - started > deadline_s):
            best.bounded = True
            break
        attacking = [card for card, recipient in zip(candidates, selection, strict=True) if recipient]
        mapping = {
            card["instance_id"]: recipient
            for card, recipient in zip(candidates, selection, strict=True)
            if recipient
        }
        block_options = [
            [None, *(card["instance_id"] for card in attacking if _can_block(card, blocker))]
            for blocker in blockers
        ]
        cost = 1
        for options in block_options:
            cost *= len(options)
        if cost > remaining:
            if best is not None:
                best.bounded = True
            continue
        worst = None
        for blocks in product(*block_options):
            if best is not None and time.monotonic() - started > deadline_s:
                best.bounded = True
                remaining = 0
                worst = None
                break
            remaining -= 1
            counts = Counter(identity for identity in blocks if identity is not None)
            if any(_has(card, "menace") and counts[card["instance_id"]] == 1 for card in attacking):
                continue
            damage = Counter()
            dead_attackers, dead_blockers = set(), set()
            for attacker in attacking:
                assigned = [
                    blocker
                    for blocker, identity in zip(blockers, blocks, strict=True)
                    if identity == attacker["instance_id"]
                ]
                outcome = _resolve_attacker(attacker, assigned)
                damage[recipient_key(mapping[attacker["instance_id"]])] += outcome.damage_through
                if outcome.attacker_died:
                    dead_attackers.add(attacker["instance_id"])
                dead_blockers.update(card["instance_id"] for card in outcome.blockers_died)
            defenders = [
                card
                for card in yours
                if not card.get("is_tapped")
                and card["instance_id"] not in dead_attackers
                and (card["instance_id"] not in mapping or _has(card, "vigilance"))
            ]
            survivors = [
                card
                for card in theirs
                if card["instance_id"] not in dead_blockers and not _has(card, "defender")
            ]
            response = optimal_blocks(survivors, defenders, your_life, max_options=2000)
            crackback = response.damage_through if response else 0
            player_damage = damage[("player", opponent_id)]
            removed = []
            walker_value = 0.0
            for (kind, identity), amount in damage.items():
                if kind != "planeswalker":
                    continue
                remaining_loyalty = loyalty(cards[identity])
                if remaining_loyalty is not None and remaining_loyalty > 0:
                    if amount >= remaining_loyalty:
                        removed.append(identity)
                        walker_value += 9 + min(remaining_loyalty, 6)
                    else:
                        walker_value += min(amount, remaining_loyalty) * 0.5
            material = sum(_material(cards[identity]) for identity in dead_blockers) - sum(
                _material(cards[identity]) for identity in dead_attackers
            )
            score = (
                player_damage * 5 / max(1, opponent_life - player_damage)
                + walker_value
                + material
                - crackback * 5 / max(1, your_life - crackback)
            )
            if player_damage >= opponent_life:
                score = 10000
            elif crackback >= your_life:
                score = -1000 + material
            evaluated = CombatChoice(mapping, player_damage, removed, crackback, score)
            if worst is None or evaluated.score < worst.score:
                worst = evaluated
        if worst is not None:
            # Equal scores used to keep the first all-in attack, including
            # zero-power mana creatures. Prefer real pressure at equal value,
            # then keep unnecessary attackers available for mana and defense.
            tied = best is not None and math.isclose(worst.score, best.score, abs_tol=1e-9)
            if (
                best is None
                or (worst.score > best.score and not tied)
                or (
                    tied
                    and (worst.player_damage, -len(worst.assignments))
                    > (best.player_damage, -len(best.assignments))
                )
            ):
                best = worst
    if best is not None:
        attacks = (
            "; ".join(
                f"{cards[identity]['name']} -> {recipient_label(recipient, state)}"
                for identity, recipient in best.assignments.items()
            )
            or "hold all attackers"
        )
        removed_names = (
            ", ".join(cards[identity]["name"] for identity in best.planeswalkers_removed) or "none"
        )
        best.explanation = f"{attacks}; {best.player_damage} damage to player, planeswalkers removed: {removed_names}; counterattack {best.crackback}. Approximate visible-board search; unknown tricks and triggers excluded."
    return best
