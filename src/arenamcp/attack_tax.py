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


_BASIC_LAND_TYPES = frozenset({"plains", "island", "swamp", "mountain", "forest"})
_LAND_TYPE_TAP_BONUS_RE = re.compile(
    r"Whenever (?:you tap a|a) (plains|island|swamp|mountain|forest|land) "
    r"(?:you control )?(?:is tapped )?for mana, (?:its controller )?adds? an additional "
    r"((?:\{[WUBRGC]\})+)",
    re.I,
)
_ANY_LAND_TAP_BONUS_RE = re.compile(
    r"Whenever you tap a land for mana, add one mana of any type that land produced\b",
    re.I,
)
_TAP_SAC_MANA_RE = re.compile(
    r"^\{T\},\s*Sacrifice (?:this (?:token|creature|artifact|permanent)|~):\s*Add "
    r"(one mana of any color|(?:\{[WUBRGC]\})+)\.?$",
    re.I | re.M,
)


def _is_land_now(card: dict) -> bool:
    types = card.get("card_types")
    if isinstance(types, list) and types:
        return any(str(t).removeprefix("CardType_") == "Land" for t in types)
    return bool(re.search(r"\bland\b", str(card.get("type_line") or "").lower().split("—", 1)[0]))


def _unrestricted_oracle_lines(oracle: str) -> tuple[str, bool]:
    """Drop ability lines carrying spend/activation restrictions while keeping unrestricted lines.

    Example: Delighted Halfling has both ``{T}: Add {C}.`` and a restricted
    legendary-only ability; the unrestricted ``{C}`` can still pay Ghostly Prison.
    """
    unrestricted: list[str] = []
    has_restricted = False
    for raw in oracle.splitlines():
        line = raw.strip()
        if not line:
            continue
        if re.match(r"^(?:spend (?:this|that) mana only|activate only)\b", line, re.I):
            has_restricted = True
            if unrestricted:
                unrestricted.pop()
            continue
        if re.search(r"spend (?:this|that) mana only|activate only", line, re.I):
            has_restricted = True
            continue
        unrestricted.append(line)
    return "\n".join(unrestricted), has_restricted


class AttackBudget:
    def __init__(self, state):
        from arenamcp.combat_recovery import _text, mana_options
        from arenamcp.land_mana import basic_types
        from arenamcp.mana_potential import (
            _AURA_BONUS_RE,
            _SAC_MANA_RE,
            _TAP_BONUS_RE,
            _ready,
            _symbols,
        )

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
        self._self_pay_ids: set[int] = set()
        self._sacrifice_ids: set[int] = set()
        if not self.taxes:
            return
        own = [c for c in self.cards.values() if controller(c) == self.seat and not c.get("is_phased_out")]
        active_own = [c for c in own if not c.get("face_down") and not c.get("removed_abilities")]

        # Precompute static/triggered mana-tap bonuses controlled by us.
        creature_tap_bonus = sum(
            sum(_symbols(m[1])) for other in active_own for m in _TAP_BONUS_RE.finditer(_text(other))
        )
        land_type_bonuses: dict[str, int] = {}
        any_land_bonus = 0
        aura_bonuses: dict[int, int] = {}
        for other in active_own:
            other_text = _text(other)
            for m in _LAND_TYPE_TAP_BONUS_RE.finditer(other_text):
                kind = m[1].lower()
                extra = sum(_symbols(m[2]))
                if kind == "land":
                    any_land_bonus += extra
                else:
                    land_type_bonuses[kind] = land_type_bonuses.get(kind, 0) + extra
            any_land_bonus += len(_ANY_LAND_TAP_BONUS_RE.findall(other_text))
            aura_match = _AURA_BONUS_RE.search(other_text)
            if aura_match and other.get("attached_to_id"):
                val = aura_match[1]
                extra = sum(_symbols(val)) if val.startswith("{") else 1
                host_id = int(other["attached_to_id"])
                aura_bonuses[host_id] = aura_bonuses.get(host_id, 0) + extra

        turn_num = int(turn.get("turn_number") or 0)
        for c in own:
            iid = c.get("instance_id")
            if not iid or c.get("face_down") or c.get("removed_abilities"):
                continue
            oracle = text(c)
            if re.search(
                r"(?:can't|cannot) (?:be activated|activate)|loses? all abilities",
                oracle,
            ):
                continue
            filtered_oracle, has_restricted = _unrestricted_oracle_lines(_text(c))
            is_creature = is_creature_now(c)
            can_tap = not c.get("is_tapped") and not (
                is_creature
                and (
                    (c.get("summoning_sickness") and not has_combat_keyword(c, "haste"))
                    or not _ready(c, turn_num)
                )
            )

            tap_amount = 0
            tap_sac_amount = 0
            if can_tap:
                filtered_card = {**c, "oracle_text": filtered_oracle}
                tap_amount = max((sum(o) for o in mana_options(filtered_card, own)), default=0)
                btypes = basic_types(c) & _BASIC_LAND_TYPES
                # Basic land types grant intrinsic mana, including on animated lands,
                # unless the card's printed mana line is explicitly restricted.
                if btypes and not has_restricted:
                    tap_amount = max(tap_amount, 1)
                if tap_amount:
                    if is_creature:
                        tap_amount += creature_tap_bonus
                    if _is_land_now(c):
                        tap_amount += any_land_bonus + aura_bonuses.get(iid, 0)
                        tap_amount += sum(land_type_bonuses.get(bt, 0) for bt in btypes)
                for m in _TAP_SAC_MANA_RE.finditer(filtered_oracle):
                    val = m[1]
                    tap_sac_amount = max(
                        tap_sac_amount,
                        1 if val.lower() == "one mana of any color" else sum(_symbols(val)),
                    )

            # No-tap sacrifice abilities (e.g. Eldrazi Spawn: "Sacrifice this token: Add {C}.")
            # do not require being untapped or free of summoning sickness.
            sac_amount = max(
                (sum(_symbols(m[1])) for m in _SAC_MANA_RE.finditer(filtered_oracle)),
                default=0,
            )
            best_sac = max(sac_amount, tap_sac_amount)

            if tap_amount and tap_amount >= best_sac:
                self.sources.append((iid, tap_amount, is_creature))
                # CR 508.1f-508.1h: vigilant attackers stay untapped when declared
                # and may tap during step 508.1h to pay attack taxes (e.g. Nissa Forest).
                if is_creature and has_combat_keyword(c, "vigilance"):
                    self._self_pay_ids.add(iid)
            elif best_sac:
                self.sources.append((iid, best_sac, is_creature))
                self._sacrifice_ids.add(iid)

        self.sources.sort(key=lambda s: (s[2], s[0] in self._sacrifice_ids, -s[1], s[0]))

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
        # Non-creatures first, then reusable creature sources before sacrifice
        # tokens, preferring larger sources and non-attacking creatures first.
        ordered = sorted(
            self.sources,
            key=lambda s: (s[2], s[0] in self._sacrifice_ids, -s[1], s[0] in assignments, s[0]),
        )
        for iid, amount, creature in ordered:
            if left <= 0:
                break
            if iid in assignments and iid not in self._self_pay_ids:
                continue
            left -= amount
            if creature:
                tapped.add(iid)
        return Payment(cost, frozenset(tapped)) if left <= 0 else None

    def player_payment(self, attackers):
        return self.payment({c["instance_id"]: {"playerSystemSeatId": self.opponent} for c in attackers})

    @property
    def key(self):
        return (
            self.taxes,
            tuple(self.sources),
            self.opponent,
            frozenset(self._self_pay_ids),
            frozenset(self._sacrifice_ids),
        )


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
