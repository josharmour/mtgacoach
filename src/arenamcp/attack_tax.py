"""Public attack-cost checks; never spend an attacker as its own mana source (#210)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from arenamcp.combat_keywords import has_combat_keyword, is_creature_now
from arenamcp.combat_targets import recipient_key


class UnsupportedAttackTaxProjection(Exception):
    """Multi-turn search does not yet represent attack payments/source tapping."""


def controller(card):
    return card.get("controller_seat_id") or card.get("owner_seat_id")


def text(card):
    value = re.sub(r"<[^>]*>", "", str(card.get("oracle_text") or ""))
    return re.sub(r"\([^)]*\)", "", value).lower().replace("’", "'")


@dataclass(frozen=True)
class Tax:
    name: str
    controller: int | None
    amount: int | None
    walkers: bool = False
    everywhere: bool = False

    def applies(self, recipient, cards):
        kind, iid = recipient_key(recipient)
        if self.everywhere:
            return True
        if kind == "player":
            return iid == self.controller
        return self.walkers and kind == "planeswalker" and controller(cards.get(iid, {})) == self.controller


def taxes(state, *, future=False):
    """Generic per-creature clauses, conditional on public facts, with unknowns retained."""
    result = []
    players = {p.get("seat_id"): p for p in state.get("players") or []}
    for card in state.get("battlefield") or []:
        if card.get("is_phased_out") or card.get("face_down"):
            continue
        for line in dict.fromkeys(text(card).splitlines()):
            line = " ".join(line.split())
            if not re.search(r"can't attack[^.]*unless[^.]*pay", line):
                continue
            amount = None
            m = re.fullmatch(
                r"(?P<condition>.*?)creatures can't attack(?P<scope> you(?: or planeswalkers you control)?)? "
                r"unless their controller pays \{o?(?P<amount>\d+)\} for each "
                r"(?:of those creatures|creature they control that's attacking you(?: or a planeswalker you control)?|creature attacking you)\.",
                line,
            )
            active = None
            if m:
                condition = m["condition"].strip()
                if not condition:
                    active = True
                elif condition.startswith("as long as ") and condition.endswith(" is untapped,"):
                    subject = condition[len("as long as ") : -len(" is untapped,")]
                    if subject in {str(card.get("name") or "").lower(), "this creature", "this permanent"}:
                        active = not card.get("is_tapped", False)
                elif condition == "as long as you have an enduring story,":
                    active = players.get(controller(card), {}).get("has_enduring_story")
                if active is False and not future:
                    continue
                if active is True and not card.get("removed_abilities"):
                    amount = int(m["amount"])
            # Unknown conditional/X/coloured/additional costs are not a free attack.
            result.append(
                Tax(
                    str(card.get("name") or "?"),
                    controller(card),
                    amount,
                    "planeswalkers you control" in line,
                    bool(re.search(r"creatures can't attack unless", line)),
                )
            )
    return tuple(result)


@dataclass(frozen=True)
class Payment:
    cost: int
    tapped_creatures: frozenset[int] = frozenset()


class AttackBudget:
    def __init__(self, state):
        from arenamcp.combat_recovery import _text, mana_options
        from arenamcp.land_mana import basic_types
        from arenamcp.mana_potential import _TAP_BONUS_RE, _ready, _symbols

        self.taxes = taxes(state)
        self.cards = {c.get("instance_id"): c for c in state.get("battlefield") or []}
        local = next((p for p in state.get("players") or [] if p.get("is_local")), {})
        self.seat = local.get("seat_id", state.get("local_seat_id"))
        self.opponent = next(
            (p.get("seat_id") for p in state.get("players") or [] if p.get("seat_id") != self.seat), None
        )
        turn = state.get("turn") or {}
        # Mana empties at the end of each step/phase. Main1 and beginning-of-combat
        # floating mana cannot pay a later declaration. Pool restriction metadata is
        # not serialized: do not treat it as known spendable attack-cost mana.
        self.sources = []
        if not self.taxes:
            return
        own = [c for c in self.cards.values() if controller(c) == self.seat and not c.get("is_phased_out")]
        for c in own:
            iid = c.get("instance_id")
            if not iid or c.get("is_tapped") or c.get("face_down") or c.get("removed_abilities"):
                continue
            if is_creature_now(c) and (
                (c.get("summoning_sickness") and not has_combat_keyword(c, "haste"))
                or not _ready(c, int(turn.get("turn_number") or 0))
            ):
                continue
            oracle = text(c)
            if re.search(
                r"spend (?:this|that) mana only|(?:can't|cannot) (?:be activated|activate)|loses? all abilities",
                oracle,
            ):
                continue
            amount = max((sum(o) for o in mana_options(c, own)), default=0)
            # Basic land types grant intrinsic mana, including on animated lands.
            if basic_types(c) & {"plains", "island", "swamp", "mountain", "forest"}:
                amount = max(amount, 1)
            if amount and is_creature_now(c):
                amount += sum(
                    sum(_symbols(m[1])) for other in own for m in [_TAP_BONUS_RE.search(_text(other))] if m
                )
            if amount:
                self.sources.append((iid, amount, is_creature_now(c)))
        self.sources.sort(key=lambda s: (s[2], -s[1], s[0]))

    def cost(self, assignments):
        total = 0
        for recipient in assignments.values():
            for tax in self.taxes:
                if tax.applies(recipient, self.cards):
                    if tax.amount is None:
                        return None
                    total += tax.amount
        return total

    def payment(self, assignments):
        cost = self.cost(assignments)
        if cost is None:
            return None
        left, tapped = cost, set()
        for iid, amount, creature in self.sources:
            if left <= 0:
                break
            # Vigilance permits post-declaration tapping, but that payment timing
            # is not modeled here; leave those special lines to the planner.
            if iid in assignments:
                continue
            left -= amount
            if creature:
                tapped.add(iid)
        return Payment(cost, frozenset(tapped)) if left <= 0 else None

    def player_payment(self, attackers):
        return self.payment({c["instance_id"]: {"playerSystemSeatId": self.opponent} for c in attackers})

    @property
    def key(self):
        return (self.taxes, tuple(self.sources), self.opponent)


def affordable_entries(state, entries, pending=None):
    """Keep a valid intended selection; otherwise choose among its payable subsets."""
    budget = AttackBudget(state)
    mapping = {e["attackerInstanceId"]: e["damageRecipient"] for e in entries}
    if not budget.taxes or budget.payment(mapping) is not None:
        return entries
    from arenamcp.combat_strategy import _search
    from arenamcp.combat_targets import attack_candidates

    candidates = {c.get("attackerInstanceId"): c for c in attack_candidates(state, pending) or []}
    raw = []
    for iid, recipient in mapping.items():
        candidate = candidates.get(iid, {})
        legal = [recipient]
        if candidate.get("mustAttack"):
            # If paying is declined, a mandatory creature still has to attack
            # a free legal recipient. Only add such alternatives when thinning.
            legal += [
                r
                for r in candidate.get("legalDamageRecipients") or []
                if r not in legal and budget.cost({iid: r}) == 0
            ]
        raw.append(
            {
                "attackerInstanceId": iid,
                "legalDamageRecipients": legal,
                "mustAttack": bool(candidate.get("mustAttack")),
            }
        )
    choice = _search(state, raw, 20000, 0.4)
    if choice is not None:
        return [
            {**e, "damageRecipient": choice.assignments[e["attackerInstanceId"]]}
            for e in entries
            if e["attackerInstanceId"] in choice.assignments
        ]
    # Unknown combat stats cannot justify a paid attack.
    return [e for e in entries if budget.cost({e["attackerInstanceId"]: e["damageRecipient"]}) == 0]
