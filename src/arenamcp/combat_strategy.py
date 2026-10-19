"""Bounded joint attack-recipient and defending-block assignment search."""

import json
import logging
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from itertools import combinations, product

from arenamcp import combat_solver
from arenamcp.combat_keywords import (
    annotate_cant_be_blocked,
    is_creature_modified,
    is_creature_now,
    modified_conditional_keywords,
    unmet_modified_flag,
)
from arenamcp.combat_solver import (
    _can_block,
    _cards_key,
    _has,
    _material,
    _memoized,
    _resolve_attacker,
    can_attack_next_turn,
    leaves_at_our_end_step,
    optimal_blocks,
    walker_crackback_cost,
)
from arenamcp.combat_targets import attack_candidates, recipient_key, recipient_label

logger = logging.getLogger(__name__)


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
    """A planeswalker's loyalty: its loyalty counters, else the ``loyalty`` field.

    Loyalty IS the number of loyalty counters, and the log parser keeps that
    counter live from each GRE object update. The top-level ``loyalty`` field
    can be stale: bug_20261006_135027's empowered Jace token had
    ``counters={"Loyalty": 1}`` but ``loyalty=0`` from the macOS bridge
    snapshot, so a lethal hit on it was valued at nothing.
    """
    counters = card.get("counters") or {}
    counted = next((count for kind, count in counters.items() if str(kind).lower().endswith("loyalty")), None)
    for value in (counted, card.get("loyalty")):
        if value is None or isinstance(value, bool):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def combat_choice(state: dict, *, budget: int = 20000, deadline_s: float = 0.7) -> CombatChoice | None:
    raw = attack_candidates(state)
    if not raw:
        return None
    # Board-granted "can't be blocked" (Tetsuko Umezawa) must reach _can_block.
    annotate_cant_be_blocked(state.get("battlefield"))
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
    creatures = [card for card in battlefield if is_creature_now(card)]
    yours = [
        card for card in creatures if card.get("controller_seat_id", card.get("owner_seat_id")) == local_id
    ]
    theirs = [
        card for card in creatures if card.get("controller_seat_id", card.get("owner_seat_id")) == opponent_id
    ]
    blockers = [card for card in theirs if not card.get("is_tapped")]
    # Our planeswalkers: their crackback may finish one instead of hitting us.
    our_walkers = tuple(
        value
        for card in battlefield
        if card.get("controller_seat_id", card.get("owner_seat_id")) == local_id
        and "planeswalker" in str(card.get("type_line", "")).lower()
        and (value := loyalty(card)) is not None
        and value > 0
    )
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
            # Fleeting Effigy returns to hand at our end step: no blocker next
            # turn, so its attack is free (2026-10-19 T10, match 92c09c7a).
            defenders = [
                card
                for card in yours
                if not card.get("is_tapped")
                and card["instance_id"] not in dead_attackers
                and (card["instance_id"] not in mapping or _has(card, "vigilance"))
                and not leaves_at_our_end_step(card)
            ]
            died = True if (dead_attackers or dead_blockers) else None
            survivors = [
                card
                for card in theirs
                if card["instance_id"] not in dead_blockers and can_attack_next_turn(card, creature_died=died)
            ]
            response = guarded_block_plan(survivors, defenders, your_life, max_options=2000)
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
                - walker_crackback_cost(crackback, your_life, our_walkers)
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


# --- Losing-attack guard ------------------------------------------------------
#
# bug_20261006_135027: the model sent a lone 1/1 Fblthp at a 1-loyalty Jace
# token past an untapped 3/2 Keeper of the Quiet Hour with both our lands
# tapped. Keeper blocked, Fblthp died, Jace and Keeper survived. The bridge's
# solver override only reviews player-only attacks without explicit
# recipients, and the zero-power filter ignores 1-power creatures, so nothing
# stood between the plan and submission. Everything below is deliberately
# conservative: any visible reason the attack might gain something keeps it.

# The attacker's own text: attack/block/death payoffs, evasion, and fight
# value. A combat-damage-to-a-player payoff (Fblthp's draw) needs the attacker
# to connect, which is exactly what the blocker prevents, so it is not here.
_ATTACKER_PAYOFF = re.compile(
    r"\battack(?:s|ing|ed|ers?)?\b|\bblock(?:s|ed|ing|ers?)?\b"
    r"|\bdies\b|\bdie\b|leaves the battlefield|put into (?:a|your|its owner's|their owner's) graveyard"
    r"|\bis dealt (?:combat )?damage\b|\bdeals? (?:combat )?damage to (?:a|another|that|target|each) creature\b"
    r"|\b(?:exert|exalted|annihilator|battle cry|myriad|melee|afflict|bushido|rampage|flanking|provoke"
    r"|dethrone|mentor|training|renown|ninjutsu|undying|persist|skulk|shadow|horsemanship|fear|intimidate"
    r"|protection|daunt|[a-z]+walk)\b"
)
# An attack trigger that only pings is worth its number, not the creature.
# 2026-10-19 10:25 T15 (match a51dea64): Hired Claw 2/3 ("Whenever you attack
# with one or more Lizards, this creature deals 1 damage to target opponent")
# attacked into two untapped Lyra Dawnbringers; one blocked, killed it and
# gained 6. Its "attack" text had exempted it from the guard.
_ATTACK_PING = re.compile(
    r"^whenever (?:you attack with [^,.]+|[^,.]+? attacks?),\s*"
    r"(?:(?:this creature|it|~|cardname|[^,.]+?) deals (?P<d>\d+) damage to "
    r"(?:target opponent|each opponent|defending player|target player|the player it's attacking)"
    r"|(?:each opponent|defending player|target opponent) loses (?P<l>\d+) life"
    r"|you gain (?P<g>\d+) life)\.?$"
)


def _attack_payoff(card: dict) -> int | None:
    """None when its own text has an attack/block/death payoff the guard can't price.

    Otherwise the damage or life its attack triggers are worth (0: no payoff).
    """
    value = 0
    for line in _rules_text(card).splitlines():
        line = line.strip()
        if not _ATTACKER_PAYOFF.search(line):
            continue
        ping = _ATTACK_PING.match(line)
        if ping is None:
            return None
        value += int(ping.group("d") or ping.group("l") or ping.group("g"))
    return value


def _attack_ping_damage(card: dict) -> int:
    """Damage or life loss its attack triggers deal the opponent (lifegain left out)."""
    total = 0
    for line in _rules_text(card).splitlines():
        ping = _ATTACK_PING.match(line.strip())
        if ping is not None:
            total += int(ping.group("d") or ping.group("l") or 0)
    return total


# Our other permanents: anything that pays off creatures attacking, blocking
# restrictions, or creatures dying.
_SIDE_PAYOFF = re.compile(
    r"\battack(?:s|ing|ed|ers?)?\b|\bblock(?:s|ed|ing|ers?)?\b|\b(?:exalted|battle cry|myriad|melee)\b"
    r"|\bwhenever (?:a|an|another|one or more|each)\b[^.]*?\b(?:dies|die)\b"
    r"|put into a graveyard from the battlefield"
)
# An attack payoff limited to a creature type rewards only attackers of that
# type. 2026-10-19 (match f9e0ec3b): Hired Claw's "Whenever you attack with
# one or more Lizards" switched the guard off for every attacker.
_TYPED_ATTACK = re.compile(
    r"\battack with (?:one or more |two or more |a |an |another )?(?P<a>[a-z]+?)s?\b"
    r"|\b(?:one or more|a|an|another|each) (?:other )?(?P<b>[a-z]+?)s? you control attacks?\b"
)
_GENERIC_TYPES = frozenset({"creature", "creatures", "permanent", "token", "nontoken"})


def _typed_attack_payoff(text: str) -> frozenset[str] | None:
    """Creature types a side payoff is limited to; None when it rewards any attacker.

    Empty when the text pays off nothing (``_SIDE_PAYOFF`` does not match).
    """
    if not _SIDE_PAYOFF.search(text):
        return frozenset()
    types, masked = set(), text
    for match in _TYPED_ATTACK.finditer(text):
        kind = match.group("a") or match.group("b")
        if kind in _GENERIC_TYPES:
            continue
        types.add(kind)
        masked = masked.replace(match.group(0), " ")
    return None if _SIDE_PAYOFF.search(masked) else frozenset(types)


def _has_type(card: dict, kind: str) -> bool:
    words = set(re.findall(r"[a-z]+", _types(card)))
    return kind in words or (kind.endswith("ve") and f"{kind[:-2]}f" in words)


_HAND_PAYOFF = re.compile(r"\braid\b|\bmorbid\b|attacked this turn|died this turn")
_CANT_BLOCK = re.compile(r"can(?:'|no)t block|can block only")
_ANSWER_SEARCH_LIMIT = 20_000


def _rules_text(card: dict) -> str:
    """Lower-case rules text without markup or reminder text."""
    text = re.sub(r"<[^>]*>", "", card.get("oracle_text") or "").lower()
    # Arena writes hybrid symbols in parentheses inside braces ("{o1o(U/R)}");
    # park every brace group so only real reminder text is removed.
    symbols = re.findall(r"\{[^}]*\}", text)
    text = re.sub(r"\{[^}]*\}", lambda _m, n=iter(range(len(symbols))): f"\x00{next(n)}\x00", text)
    while re.search(r"\([^()]*\)", text):
        text = re.sub(r"\([^()]*\)", "", text)
    return re.sub(r"\x00(\d+)\x00", lambda match: symbols[int(match.group(1))], text)


def _types(card: dict) -> str:
    return f"{card.get('type_line') or ''} {' '.join(card.get('card_types') or [])}".lower()


def _controller(card: dict) -> int | None:
    return card.get("controller_seat_id", card.get("owner_seat_id"))


def _mana_symbols(cost: str) -> list[str]:
    """Mana symbols in a cost; Arena writes several per brace ("{o1o(U/R)}")."""
    symbols = []
    for group in re.findall(r"\{([^}]*)\}", cost.lower()):
        symbols.extend(token for token in re.split(r"[o\s]+", group) if token)
    return symbols


def _mana_value(cost: str) -> int:
    """Total mana a cost asks for; colors are ignored, which only overstates affordability."""
    value = 0
    for symbol in _mana_symbols(cost):
        if symbol.isdigit():
            value += int(symbol)
        elif symbol not in ("t", "q", "x"):
            value += 1
    return value


def _activated_abilities(card: dict) -> list[tuple[str, str]]:
    """(cost, effect) of non-mana, non-loyalty activated abilities without a sorcery-speed limit."""
    abilities = []
    for line in _rules_text(card).splitlines():
        line = line.strip()
        if ":" not in line or re.match(r"(?:when|whenever|at the beginning|if|as long as)\b", line):
            continue
        cost, effect = line.split(":", 1)
        if re.match(r"\s*[+\-−–]?\s*(?:\d+|x)\s*$", cost):
            continue  # planeswalker loyalty ability: sorcery speed
        if re.search(r"\badd\b[^.]*?(?:\{|\bmana\b)", effect):
            continue  # mana ability
        if re.search(r"activate only as a sorcery", effect):
            continue
        abilities.append((cost, effect))
    return abilities


# What an affordable instant-speed play can do to OUR attack (2026-10-13
# bug_20261013_201811: a 1/1 attacked into four untapped blockers because the
# guard counted Divining Duelist -- a flash tapper that removes one blocker --
# and Heartwood Crafter's prepared SORCERY as tricks that could save it).
_SAVES_ATTACKER = re.compile(
    r"\bgets? \+\d+/\+\d+|\bindestructible\b|\bhexproof\b|\bfirst strike\b|\bprotection from\b"
    r"|can't be blocked|prevent all (?:combat )?damage|\bphases? out\b|\+\d+/\+\d+ counter"
    # Shrinking the blocker saves the attacker; a sacrifice outlet cashes it in.
    r"|\bgets? [-−]\d+/[-−+]?\d+|\bsacrifice (?:a|an|another|this) creature"
)
_REMOVES_BLOCKER = re.compile(
    r"\btap target creature|\bstun counter|\b(?:destroy|exile) target (?:[a-z]+ )*?creature"
    r"|deals? (?:\d+|x) damage to (?:any target|target (?:[a-z]+ )*?creature)"
    r"|target creature can't block|return target (?:[a-z]+ )*?creature to its owner's hand"
)
# A pump that only makes a creature bigger. It saves an attacker only when the
# bigger creature is no longer answered: Hired Claw's own {1}{R} +1/+1 counter
# made it a 2/3, and an untapped 4/4 still ate it (2026-10-19 T10, match
# abde2a07), yet the guard stepped aside for it as for any pump.
_PUMP = re.compile(
    r"\bgets? \+(?P<p>\d+)/\+(?P<t>\d+)"
    r"|\b(?P<n>a|an|one|two|three|\d+) \+1/\+1 counters?\b"
)
# Arena's ability text says "this" alone ("Put a +1/+1 counter on this.").
_PUMP_SELF = re.compile(r"\b(?:this creature|this permanent|cardname)\b|~|\bthis(?=\s*(?:[.,;]|$|gets?\b))")
_PUMP_OTHERS = re.compile(r"\btarget\b|\beach (?:other )?creature|\bcreatures you control\b|\banother\b")


def _pump_only(text: str | None) -> tuple[int, int, bool] | None:
    """(power, toughness, self only) when a play's only attacker-saving effect is a plain pump.

    None when there is no pump or it comes with anything else that could save
    an attacker (indestructible, first strike, protection, a shrink ...).
    """
    if not text:
        return None
    pumps = list(_PUMP.finditer(text))
    if not pumps or _SAVES_ATTACKER.search(_PUMP.sub(" ", text)):
        return None
    power = toughness = 0
    for pump in pumps:
        if pump.group("p"):
            power += int(pump.group("p"))
            toughness += int(pump.group("t"))
        else:
            count = pump.group("n")
            amount = int(count) if count.isdigit() else _COUNT_WORDS[count]
            power += amount
            toughness += amount
    return power, toughness, bool(_PUMP_SELF.search(text)) and not _PUMP_OTHERS.search(text)


def _pumped(card: dict, power: int, toughness: int) -> dict:
    return {**card, "power": int(card["power"]) + power, "toughness": int(card["toughness"]) + toughness}


_ONCE_EACH_TURN = re.compile(r"\bonly once each turn\b|\bactivate only once\b")


def _pump_activations(text: str, cost: int, mana: int, *, ability: bool) -> int | None:
    """How many times an affordable pump can be used now; None when it is free and repeatable."""
    if not ability or _ONCE_EACH_TURN.search(text):
        return 1
    if cost <= 0:
        return None
    return max(1, mana // cost)


def _prepared_spell(card: dict) -> dict | None:
    """The spell a prepared creature can copy: its linked face, or None when unknown."""
    faces = card.get("related_faces")
    if faces is None and card.get("grp_id"):
        try:
            from arenamcp.match_context import _local_card

            faces = _local_card(int(card["grp_id"]), int(time.monotonic() // 30)).get("related_faces")
        except Exception:
            faces = None
    for face in faces or []:
        kind = str(face.get("type_line") or "").lower()
        if "instant" in kind or "sorcery" in kind:
            return face
    return None


def _untapped_mana(state: dict, local: dict) -> int:
    total = 0
    for card in state.get("battlefield") or []:
        if _controller(card) != local.get("seat_id") or card.get("is_tapped"):
            continue
        raw = (card.get("oracle_text") or "").lower()
        if "land" in _types(card) or re.search(r"\badd\b[^.]*?(?:\{|\bmana\b)", raw):
            total += 1
    pool = local.get("mana_pool") or {}
    amounts = pool.values() if isinstance(pool, dict) else pool if isinstance(pool, list) else []
    for amount in amounts:
        if isinstance(amount, (int, float)) and not isinstance(amount, bool):
            total += max(0, int(amount))
        elif amount:
            total += 1
    return total


def _possible_tricks(state: dict, local: dict) -> list[tuple[str, int, str | None, int | None]]:
    """Instant-speed plays visible to us now, as (source, mana value, effect text, permanent id).

    0 means free; the text is None when the effect is unknown (treated as a
    trick that could matter). The id is the battlefield permanent whose
    ability it is (None for cards in hand or graveyard): a self-pump helps
    only that creature.
    """
    seat = local.get("seat_id")
    tricks = []
    for card in state.get("hand") or []:
        if _controller(card) not in (None, seat):
            continue
        text = _rules_text(card)
        flash = "flash" in (card.get("keywords") or ()) or re.search(r"(?m)^\s*flash\b", text)
        if "instant" not in _types(card) and not flash:
            continue
        alternative = re.search(r"rather than pay|without paying|\bconvoke\b|\bdelve\b|\bimprovise\b", text)
        cost = 0 if alternative else _mana_value(card.get("mana_cost") or "")
        tricks.append((card.get("name") or "an instant", cost, text, None))
    for card in state.get("graveyard") or []:
        if card.get("owner_seat_id", seat) != seat or "instant" not in _types(card):
            continue
        text = _rules_text(card)
        if re.search(r"\b(?:flashback|jump-start|retrace|escape)\b", text):
            flashback = re.search(r"flashback\s*\S?\s*((?:\{[^}]*\})+)", text)
            cost = flashback.group(1) if flashback else card.get("mana_cost") or "{1}"
            tricks.append((card.get("name") or "a graveyard instant", max(1, _mana_value(cost)), text, None))
    for card in state.get("battlefield") or []:
        if _controller(card) != seat or "planeswalker" in _types(card):
            continue
        name = card.get("name") or "a permanent"
        if re.search(r"\bprepared\b", _rules_text(card)):
            spell = _prepared_spell(card)
            if spell is None:
                tricks.append((name, 1, None, None))  # unknown spell: assume it could matter
            elif "instant" in str(spell.get("type_line") or "").lower():
                text = str(spell.get("oracle_text") or "").lower()
                tricks.append((name, max(1, _mana_value(spell.get("mana_cost") or "{1}")), text, None))
            # A prepared sorcery cannot be cast in combat.
        raw = (card.get("oracle_text") or "").lower()
        is_mana_source = not card.get("is_tapped") and (
            "land" in _types(card) or bool(re.search(r"\badd\b[^.]*?(?:\{|\bmana\b)", raw))
        )
        for cost, effect in _activated_abilities(card):
            symbols = _mana_symbols(cost)
            if "t" in symbols and card.get("is_tapped"):
                continue
            tap_self_mana = 1 if ("t" in symbols and is_mana_source) else 0
            tricks.append(
                (
                    name,
                    _mana_value(cost) + tap_self_mana,
                    f"{cost}: {effect}".lower(),
                    card.get("instance_id"),
                )
            )
    return tricks


def _trick_changes_combat(text: str | None, attackers: list[dict], blockers: list[dict]) -> bool:
    """Whether a play could keep the planned attack from simply feeding a blocker.

    Unknown effects and anything that saves an attacker count; removing or
    tapping one blocker counts only when the rest can no longer answer every
    attacker; anything else (card draw, untap, surveil) does not.
    """
    if text is None or _SAVES_ATTACKER.search(text):
        return True
    if _REMOVES_BLOCKER.search(text):
        return any(
            _full_answer(attackers, [b for b in blockers if b is not removed]) is None for removed in blockers
        )
    return False


def _resolved_this_turn(state: dict) -> list[dict]:
    """Our spells that resolved this turn (their effects may not show on the board)."""
    turn = (state.get("turn") or {}).get("turn_number")
    known = {
        card.get("instance_id"): card
        for zone in ("graveyard", "exile", "battlefield")
        for card in state.get(zone) or []
    }
    resolved = []
    for event in state.get("recent_events") or []:
        if (
            isinstance(event, dict)
            and event.get("turn") == turn
            and (event.get("category") == "Resolve" or event.get("type") == "resolution_complete")
            and event.get("instance_id") in known
        ):
            resolved.append(known[event["instance_id"]])
    return resolved


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _eligible_blockers(state: dict, opponent_seat: int) -> list[dict]:
    blockers = []
    for card in state.get("battlefield") or []:
        if (
            _controller(card) != opponent_seat
            or not is_creature_now(card)
            or card.get("is_tapped")
            or card.get("is_attacking")
            or card.get("is_phased_out")
            or not _is_int(card.get("power"))
            or not _is_int(card.get("toughness"))
            or _CANT_BLOCK.search(_rules_text(card))
        ):
            continue
        damage = card.get("damage")
        if _is_int(damage) and damage > 0:
            # Marked damage makes the blocker easier to kill: never overrate it.
            card = {**card, "toughness": card["toughness"] - damage}
        blockers.append(card)
    return blockers


_EVASION_CLAIM_RE = re.compile(
    r"\b(?:evasive|flying|flyer|flier|unblockable|uncontested|free attack|in the air"
    r"|can(?:'|no)t be blocked|can(?:'|no)t block|swings? uncontested)\b",
    re.I,
)
_TRADE_INTENT_RE = re.compile(r"\btrad(?:e|es|ing)\b", re.I)


def _group_can_block(attacker: dict, group: list[dict]) -> bool:
    if not group or (_has(attacker, "menace") and len(group) < 2):
        return False
    return all(_can_block(attacker, blocker) for blocker in group)


def _modified_would_change_block(
    attacker: dict, group: list[dict], battlefield: list[dict] | None
) -> bool:
    """True when an unmodified creature's conditional keywords would stop or survive this block."""
    oracle = str(attacker.get("oracle_text") or "")
    cond_kws = modified_conditional_keywords(oracle)
    if not cond_kws:
        return False
    if is_creature_modified(attacker, battlefield=battlefield):
        return False
    hypothetical = {
        **attacker,
        "keywords": list({*(attacker.get("keywords") or ()), *cond_kws}),
    }
    if not _group_can_block(hypothetical, group):
        return True
    hyp_outcome = _resolve_attacker(hypothetical, group)
    return not hyp_outcome.attacker_died or hyp_outcome.damage_through > 0


def _names_any(sentence: str, cards: list[dict]) -> bool:
    """The sentence names one of the cards (full name, or a distinctive word of it)."""
    lowered = sentence.lower()
    for card in cards:
        name = str(card.get("name") or "").lower()
        if not name:
            continue
        if name in lowered or name.split(",")[0] in lowered:
            return True
        if any(
            len(word) >= 4 and re.search(rf"\b{re.escape(word)}\b", lowered)
            for word in re.findall(r"[a-z']+", name)
        ):
            return True
    return False


def _false_evasion_trade(attacker: dict, group: list[dict], outcome, strategy_text: str) -> bool:
    """A trade the plan did not price: its evasion claim is wrong against these blockers, and we lose more.

    2026-10-19 08:09:19 (match a74bbc90): the plan said "flying" and the guard
    held Slickshot Show-Off 1/2 back from Moseo, Vein's New Dean 2/1, a flier
    that trades with it, as if Slickshot died for nothing. A claim counts only
    when its sentence names the attacker or one of these blockers and these
    blockers actually defeat it (a flying claim needs a flier or reach among
    them, or an attacker that doesn't fly); and a trade that takes as much
    material as it costs (blocker lifelink counted against us) is a trade,
    whatever the plan said.
    """
    defeated = False
    for sentence in re.split(r"(?<=[.!?;])\s+|\n", strategy_text or ""):
        claim = _EVASION_CLAIM_RE.search(sentence)
        if claim is None or not _names_any(sentence, [attacker, *group]):
            continue
        if re.match(r"(?i)fly|flyer|flier|in the air", claim.group(0)):
            defeated = not _has(attacker, "flying") or any(
                _has(blocker, "flying") or _has(blocker, "reach") for blocker in group
            )
        else:
            defeated = True  # unblockable / can't block / uncontested: the block itself refutes it
        if defeated:
            break
    if not defeated:
        return False
    gained = sum(_lifelink_gain(blocker) for blocker in group)
    return _material(attacker) + gained > sum(_material(card) for card in outcome.blockers_died)


def _lifelink_gain(blocker: dict) -> int:
    """Life a blocker's lifelink gains its controller when it deals combat damage."""
    return max(0, int(blocker.get("power") or 0)) if _has(blocker, "lifelink") else 0


def _answers(
    attacker: dict,
    group: list[dict],
    *,
    battlefield: list[dict] | None = None,
    strategy_text: str = "",
) -> bool:
    """The group can block the attacker, kill it, lose nothing (or foil false evasion), and let nothing through."""
    if not _group_can_block(attacker, group):
        return False
    outcome = _resolve_attacker(attacker, group)
    if not outcome.attacker_died or outcome.damage_through != 0:
        return False
    if not outcome.blockers_died:
        return True
    if _TRADE_INTENT_RE.search(strategy_text or ""):
        return False
    if _modified_would_change_block(attacker, group, battlefield):
        return True
    return bool(strategy_text) and _false_evasion_trade(attacker, group, outcome, strategy_text)


def _full_answer(
    attackers: list[dict],
    blockers: list[dict],
    *,
    battlefield: list[dict] | None = None,
    strategy_text: str = "",
) -> dict[int, list[dict]] | None:
    """Blocks answering EVERY planned attacker at once, or None.

    One blocker blocks one attacker. When some attacker cannot be answered
    while the rest are (too few blockers, an evasive or large attacker), the
    attack applies pressure the opponent cannot absorb for free.
    """
    options = [
        [None, *(index for index, attacker in enumerate(attackers) if _can_block(attacker, blocker))]
        for blocker in blockers
    ]
    combinations_count = 1
    for choice in options:
        combinations_count *= len(choice)
    if combinations_count > _ANSWER_SEARCH_LIMIT:
        return None
    for selection in product(*options):
        groups: dict[int, list[dict]] = {}
        for blocker, index in zip(blockers, selection, strict=True):
            if index is not None:
                groups.setdefault(index, []).append(blocker)
        if len(groups) == len(attackers) and all(
            _answers(
                attacker,
                groups.get(index, []),
                battlefield=battlefield,
                strategy_text=strategy_text,
            )
            for index, attacker in enumerate(attackers)
        ):
            return groups
    return None


def _answer_groups(
    attacker: dict,
    blockers: list[dict],
    *,
    battlefield: list[dict] | None = None,
    strategy_text: str = "",
) -> list[list[dict]]:
    """The smallest blocker groups that answer the attacker (``_answers``), up to three creatures."""
    largest = 3 if len(blockers) <= 10 else 2
    for size in range(1, min(largest, len(blockers)) + 1):
        groups = [
            list(group)
            for group in combinations(blockers, size)
            if _answers(attacker, list(group), battlefield=battlefield, strategy_text=strategy_text)
        ]
        if groups:
            return groups
    return []


def _best_through(attackers: list[dict], blockers: list[dict], opp_life: int) -> int:
    """Damage our attackers get through the opponent's best blocks (combat solver, their view)."""
    if not attackers:
        return 0
    plan = combat_solver._search_blocks(attackers, blockers, opp_life)
    if plan is None:
        return sum(_resolve_attacker(card, []).damage_through for card in attackers)
    return plan.damage_through


class _TheirBlocks:
    """Their best blocks (combat solver, their view) against parts of one declaration, cached (#168).

    2026-10-19 match 639c5fe1 T11: the losing-attack guard charged Lathiel's
    2 lifelink to every Rabbit, though she blocks one, and re-judged the
    shrinking attack one creature at a time: 10 attackers getting 6 through
    their best blocks became Leonardo alone, for 0. What an attacker adds is
    read from the blocks they actually make: the damage through with it minus
    without it, less the lifelink of the blockers on it in that plan, so each
    blocker's lifelink counts once.
    """

    def __init__(self, blockers: list[dict], opp_life: int):
        self.blockers = blockers
        self.opp_life = opp_life
        self._by_id = {card.get("instance_id"): card for card in blockers}
        self._plans: dict[frozenset, tuple[int, dict[int, list[dict]]]] = {}

    def blocks(self, attackers: list[dict]) -> tuple[int, dict[int, list[dict]]]:
        """(damage through, their blockers on each attacker id) under their best blocks."""
        key = frozenset(card["instance_id"] for card in attackers)
        found = self._plans.get(key)
        if found is None:
            plan = (
                combat_solver._search_blocks(attackers, self.blockers, self.opp_life) if attackers else None
            )
            on: dict[int, list[dict]] = {}
            if plan is None:
                found = (sum(_resolve_attacker(card, []).damage_through for card in attackers), on)
            else:
                for blocker_id, attacker_id in plan.assignments.items():
                    if blocker_id in self._by_id:
                        on.setdefault(attacker_id, []).append(self._by_id[blocker_id])
                found = (plan.damage_through, on)
            self._plans[key] = found
        return found

    def net(self, attackers: list[dict]) -> int:
        """Damage through their best blocks less what their lifelink blockers gain in them."""
        through, on = self.blocks(attackers)
        return through - sum(_lifelink_gain(blocker) for group in on.values() for blocker in group)

    def marginal(self, attacker: dict, attacking: list[dict]) -> tuple[int, int, list[dict]]:
        """(damage through with it minus without it, the lifelink its own blockers gain, those blockers)."""
        through, on = self.blocks(attacking)
        without, _on = self.blocks([card for card in attacking if card is not attacker])
        mine = on.get(attacker["instance_id"], [])
        return through - without, sum(_lifelink_gain(blocker) for blocker in mine), mine


def _copy_key(card: dict) -> tuple:
    """Identical copies for the guard's copy cap (#168): same name and power/toughness."""
    return (str(card.get("name") or ""), card.get("power"), card.get("toughness"))


def _copies_answered(
    attacker: dict, blockers: list[dict], *, battlefield: list[dict], strategy_text: str
) -> int:
    """How many copies of ``attacker`` their blockers stop at once: disjoint answering groups (#168).

    With no group that answers it (``_answers``), the single blockers that stop
    it with nothing through (``_wasted_attack``'s free blocks and trades).
    """
    groups = _answer_groups(attacker, blockers, battlefield=battlefield, strategy_text=strategy_text)
    if not groups:
        groups = [
            [blocker]
            for blocker in blockers
            if _group_can_block(attacker, [blocker])
            and not _resolve_attacker(attacker, [blocker]).damage_through
        ]
    used: set = set()
    count = 0
    for group in groups:
        ids = {card.get("instance_id") for card in group}
        if ids & used:
            continue
        used |= ids
        count += 1
    return count


def _line_search_attackers(state: dict) -> frozenset[int] | None:
    """The attackers the line search's best line sends this turn (#168), or None.

    None when there is no usable search before our attack, when its best line
    dies, or when its attackers can't be told from its names.
    """
    try:
        from arenamcp import line_guard
        from arenamcp.board_assessment import assess

        result = getattr(assess(state), "line_search", None)
        if not line_guard._usable(result) or not line_guard._our_turn(state):
            return None
        if (
            not result.model.our_turn
            or not result.model.our_attack_pending
            or result.best.cls == line_guard.DEAD
        ):
            return None
        return line_guard._attack_ids(result.best)
    except Exception:  # the strategic layer never decides a guard on its own failure
        logger.debug("line search attackers unavailable", exc_info=True)
        return None


@dataclass
class _Doomed:
    group: list[dict]
    shield: int  # extra damage the rest of the attack gets through while ``group`` eats it
    gained: int  # the opponent's lifelink gain from the block
    deficit: int  # its own damage minus what it buys (the larger, the worse the attack)
    kind: str = "fed"  # "fed": dies for nothing; "bounce" / "engine": see _wasted_attack
    note: str = ""  # bounce: their trick exposure; engine: the burn spell that kills the blocker


# A creature our noncreature casts grow (decision_memory._CAST_PAYOFF_RE): the burn deck's engine.
_CAST_ENGINE = re.compile(
    r"\bprowess\b|\bmagecraft\b|\bwhenever you cast (?:a|an|your first|your second) "
    r"(?:noncreature|instant|sorcery|instant or sorcery)\b|\bwhenever you cast a spell\b"
)


def _hand_burn(state: dict) -> list[tuple[str, int]]:
    """(name, damage) of the fixed-damage spells in our hand that hit a creature, unkicked."""
    from arenamcp.play_safety import damage_effect

    found = []
    for card in state.get("hand") or []:
        if not isinstance(card, dict):
            continue
        try:
            effect = damage_effect(card, kicked=False)
        except Exception:  # an unreadable card is no burn
            effect = None
        if effect is not None:
            found.append((str(card.get("name") or "a burn spell"), effect[0]))
    return found


def _wasted_attack(
    attacker: dict,
    attacking: list[dict],
    blockers: list[dict],
    *,
    opp_life: int,
    removal_tricks: list[str],
    payoff: int,
    pumps: list[tuple[int | None, int, int]],
    exposure: combat_solver.TrickExposure | None,
    burn: list[tuple[str, int]],
) -> _Doomed | None:
    """An attack one blocker answers for free although it does not kill it (``_doomed``'s other case).

    - "bounce" (ledger #120, 2026-10-19 T10, match 2c67eed8): Emberheart
      Challenger 2/2 attacked into an untapped Youthful Valkyrie 1/3 with the
      opponent holding Shock and mana open. The blocker and the attacker both
      survive and nothing gets through, so the attack buys nothing when
      blocked; with their open mana and cards in hand (``exposure``) a pump on
      the blocker (+``combat_solver.TRICK_PUMP``/+``TRICK_PUMP``) makes that
      block a kill.
    - "engine" (ledger #113, 2026-10-19 T4, match ba060a28): Emberheart traded
      with an untapped Vampire Gourmand 2/2 while two Shocks in hand killed the
      Gourmand on their own next turn. A creature our spells grow (prowess,
      magecraft, "whenever you cast...") is not traded for a blocker with no
      bigger a body (power + toughness) that burn in hand (``burn``,
      unkicked) kills.

    Only without an attack trigger (``payoff``), a pump of ours for it, or an
    affordable removal trick that changes the combat, and only when the rest of
    the attack gets nothing more through (net of lifelink) while that blocker
    is busy.
    """
    if payoff or any(source in (None, attacker.get("instance_id")) for source, _p, _t in pumps):
        return None
    if any(_trick_changes_combat(text, [attacker], blockers) for text in removal_tricks):
        return None
    keywords = " ".join(str(keyword) for keyword in attacker.get("keywords") or [])
    engine = bool(_CAST_ENGINE.search(f"{_rules_text(attacker)} {keywords.lower()}"))
    rest = [card for card in attacking if card is not attacker]
    free = None
    for blocker in blockers:
        if not _group_can_block(attacker, [blocker]):
            continue
        outcome = _resolve_attacker(attacker, [blocker])
        if outcome.damage_through:
            continue
        if not outcome.attacker_died and not outcome.blockers_died:
            if exposure is None or not exposure.live:
                continue
            tricked = _pumped(blocker, combat_solver.TRICK_PUMP, combat_solver.TRICK_PUMP)
            if not _resolve_attacker(attacker, [tricked]).attacker_died:
                continue
            kind, note = "bounce", exposure.describe()
        elif outcome.attacker_died and len(outcome.blockers_died) == 1 and engine:
            # Bodies, not _material: that prices the Gourmand's card draw and not our prowess.
            if int(blocker["power"]) + int(blocker["toughness"]) > int(attacker["power"]) + int(
                attacker["toughness"]
            ):
                continue  # trading into a bigger body is still worth it
            from arenamcp.play_safety import damage_would_kill

            note = next((name for name, damage in burn if damage_would_kill(blocker, damage) is True), "")
            if not note:
                continue
            kind = "engine"
        else:
            continue
        if free is None:
            free = _best_through(rest, blockers, opp_life)
        others = [card for card in blockers if card is not blocker]
        shield = _best_through(rest, others, opp_life) - free
        gained = _lifelink_gain(blocker)
        if shield - gained > 0:
            continue
        return _Doomed([blocker], shield, gained, 1, kind=kind, note=note)
    return None


def _doomed(
    attacker: dict,
    attacking: list[dict],
    blockers: list[dict],
    *,
    opp_life: int,
    battlefield: list[dict],
    strategy_text: str,
    removal_tricks: list[str],
    payoff: int = 0,
    pumps: list[tuple[int | None, int, int]] = (),
    exposure: combat_solver.TrickExposure | None = None,
    burn: list[tuple[str, int]] = (),
    fed: bool = True,
) -> _Doomed | None:
    """Whether this attacker only feeds a blocker, judged on its own (``losing_attackers``).

    ``fed=False`` judges only the blocks with no answering group
    (``_wasted_attack``): an attacker some group answers is then None.

    The opponent can eat it with some group that answers it (``_answers``),
    also after any affordable pump of ours (``pumps``: (only this creature's id
    or None, power, toughness)). While that group is busy, the rest of the
    attack gets ``shield`` more damage through their best blocks than it would
    with every blocker free; the group's lifelink gain counts against that, and
    its own attack ping (``payoff``, ``_attack_payoff``) for it: the trigger
    fires on attack whether or not it is blocked. The attacker is doomed when
    what it buys is less than the creature it loses, priced at what it deals
    each attack: its damage plus that same ping, which every later attack
    fires again. (#78, 2026-10-19 T10 match abde2a07: Hired Claw 1/2 pinging
    1 into an untapped 4/4 was "1 damage for 1 damage" and attacked; it bought
    the ping once and lost the creature that pings every turn.) With no such
    group, an attack a blocker absorbs for free under their trick exposure, or
    an engine creature's even trade into a blocker our burn kills, is held too
    (``_wasted_attack``).
    """
    groups = _answer_groups(attacker, blockers, battlefield=battlefield, strategy_text=strategy_text)
    if not groups:
        return _wasted_attack(
            attacker,
            attacking,
            blockers,
            opp_life=opp_life,
            removal_tricks=removal_tricks,
            payoff=payoff,
            pumps=pumps,
            exposure=exposure,
            burn=burn,
        )
    if not fed or any(_trick_changes_combat(text, [attacker], blockers) for text in removal_tricks):
        return None
    mine = [pump for pump in pumps if pump[0] in (None, attacker.get("instance_id"))]
    variants = [(power, toughness) for _source, power, toughness in mine]
    if len(variants) > 1:
        variants.append((sum(p for p, _t in variants), sum(t for _p, t in variants)))
    for power, toughness in variants:
        bigger = _pumped(attacker, power, toughness)
        groups = [
            group
            for group in groups
            if _answers(bigger, group, battlefield=battlefield, strategy_text=strategy_text)
        ]
        if not groups:
            # The pumped attacker is answered by none of the smallest groups; a
            # larger one may still eat it, but this guard only holds clear losers.
            return None
    rest = [card for card in attacking if card is not attacker]
    free = _best_through(rest, blockers, opp_life)
    best = None
    for group in groups:
        others = [card for card in blockers if all(card is not member for member in group)]
        busy = _best_through(rest, others, opp_life)
        gained = sum(_lifelink_gain(card) for card in group)
        verdict = _Doomed(group, busy - free, gained, 0)
        if best is None or verdict.shield - verdict.gained < best.shield - best.gained:
            best = verdict
    own = max(1, _resolve_attacker(attacker, []).damage_through)
    bought = best.shield + payoff - best.gained
    body = own + payoff
    best.deficit = body - bought
    return best if best.deficit > 0 else None


def _all_in(state: dict) -> bool:
    """The board assessment's all-in verdict; False when it cannot be computed."""
    try:
        from arenamcp.board_assessment import assess

        assessment = assess(state)
    except Exception:
        return False
    return bool(assessment is not None and getattr(assessment, "all_in", False))


def _walker_target_id(target, cards: dict, opponent_seat) -> int | None:
    """The opponent's planeswalker an attack recipient names, or None (the player, unknown, ambiguous).

    ``target`` is what the caller has: a GRE ``damageRecipient`` dict (the
    bridge), a planner label ("Ajani Resolute [341]", "Ajani Resolute",
    "Opponent") or an instance id.
    """
    identity = None
    if isinstance(target, dict):
        try:
            kind, identity = recipient_key(target)
        except (TypeError, ValueError):
            return None
        if kind != "planeswalker":
            return None
    elif _is_int(target):
        identity = target
    elif isinstance(target, str) and target.strip():
        tagged = re.search(r"\[(?:id:)?(\d+)\]\s*$", target.strip())
        if tagged:
            identity = int(tagged.group(1))
        else:
            wanted = target.strip().casefold()
            named = [
                iid
                for iid, card in cards.items()
                if str(card.get("name") or "").casefold() == wanted
                and _controller(card) == opponent_seat
                and "planeswalker" in _types(card)
            ]
            identity = named[0] if len(named) == 1 else None
    walker = cards.get(identity) if identity is not None else None
    if walker is None or _controller(walker) != opponent_seat or "planeswalker" not in _types(walker):
        return None
    return identity


def _walker_kills(
    attackers: list[dict], blockers: list[dict], cards: dict, attacker_targets: dict, opponent_seat
) -> dict[int, str]:
    """Attackers aimed at a planeswalker of theirs that dies whatever they block, with the reason.

    #127 (2026-10-19 T15, match 3e545401): Slickshot Show-Off 3/2 flier and
    Giant Cindermaw 4/3 trampler were both sent at Ajani Resolute (loyalty 1)
    past one untapped blocker, Unflinching Hortimancer 7/6 flying first
    strike. Whichever it blocked, the other killed Ajani; the guard priced
    face damage only ("only 3 more damage through") and held both. Their best
    blocks against the attackers aimed at the walker, every blocker on it, are
    judged with its loyalty as the life they protect: when that still leaves
    damage >= loyalty, the walker dies whatever they do, and killing a
    planeswalker is worth the creature they eat. Unknown loyalty keeps nothing.
    """
    aimed: dict[int, list[dict]] = {}
    for card in attackers:
        walker = _walker_target_id(attacker_targets.get(card["instance_id"]), cards, opponent_seat)
        if walker is not None:
            aimed.setdefault(walker, []).append(card)
    kept: dict[int, str] = {}
    for walker, group in aimed.items():
        remaining = loyalty(cards[walker])
        if remaining is None or remaining <= 0:
            continue
        if _best_through(group, blockers, remaining) < remaining:
            continue
        names = " and ".join(str(card.get("name") or card["instance_id"]) for card in group)
        verb = "attack" if len(group) > 1 else "attacks"
        walker_name = cards[walker].get("name") or "their planeswalker"
        reason = (
            f"{names} {verb} {walker_name} (loyalty {remaining}), "
            f"and their best blocks still leave at least {remaining} damage on it: "
            "it dies whatever they block, worth the creature they eat"
        )
        for card in group:
            kept[card["instance_id"]] = reason
    return kept


def losing_attackers(
    state: dict,
    attacker_ids: list[int],
    pending: dict | None = None,
    *,
    strategy_text: str = "",
    attacker_targets: dict | None = None,
) -> dict[int, str]:
    """Planned attackers that only feed an untapped blocker, with the reason.

    ``attacker_targets`` maps an attacker's instance id to its recipient (a
    GRE ``damageRecipient``, a planner label such as "Ajani Resolute [341]",
    or a planeswalker instance id); attackers without one attack the player.

    Each attacker is judged on its own (2026-10-19 T10, match f9e0ec3b: a 1/2
    Hired Claw added to the attack kept Emberheart Challenger 3/3 in against
    an untapped Lyra Dawnbringer 5/5 first strike lifelink, because Lyra could
    not block both; she ate the Challenger and gained 5). An attacker is
    dropped only when ALL of these hold:
      - some group of the opponent's untapped creatures can block it so that
        it dies, no blocker dies (or the attack relies on unmet 'modified'
        evasion, or trades at a loss on a false evasion claim) and nothing
        gets through, with flying/reach, menace, first strike, deathtouch,
        trample and indestructible modelled by the combat solver; or one
        blocker absorbs it with nothing dying while their open mana and hand
        make a trick the kill, or it is a creature our spells grow trading
        evenly with a blocker our burn in hand kills (``_wasted_attack``);
      - what it buys is less than its own damage: the rest of the attack
        gets fewer extra points through their best blocks while that group
        is busy, minus the group's lifelink gain (``_doomed``);
      - (#168, match 639c5fe1 T11) their best blocks against the attack
        block it, and it adds nothing through them: the damage through
        with it minus without it, less the lifelink of its own blockers in
        those blocks (each blocker's lifelink counted once), is 0 or less;
        or it adds less than its own damage and the line search's best line
        keeps it, and every attacker the guard leaves in, at home. The
        attack is re-judged after each hold, and the holds stand only if
        the thinned attack gets at least as much through their best blocks
        (net of lifelink) as the declaration, or matches that best line;
        never more identical copies than their blockers answer at once,
        unless every copy is held (``_thinned_declaration``);
      - the whole attack is not lethal through their best blocks;
      - it is not required to attack (GRE ``mustAttack`` in the state or the
        live ``pending`` bridge request);
      - it is not aimed at a planeswalker of theirs that the attackers aimed
        at it kill whatever they block (``_walker_kills``, #127);
      - its rules text names no attack/block/death payoff, evasion, or
        damage-to-creature value, except an attack trigger that only deals
        damage or changes life (Hired Claw's ping), which counts toward what
        it buys;
      - none of our other permanents rewards attacking with it (a payoff
        limited to a creature type covers only that type), restricts
        blocking or pays off creatures dying, and no raid/morbid card waits
        in hand;
      - no spell we resolved this turn mentions blocking;
      - no instant, flash card, instant flashback, prepared spell or
        activated ability we control is affordable with our untapped mana
        and could save it (free ones always count; colors are ignored,
        erring toward keeping).
    Unknown power/toughness or anything ambiguous keeps the attack.
    """
    players = state.get("players") or []
    local = next((player for player in players if player.get("is_local")), None)
    opponent = next((player for player in players if not player.get("is_local")), None)
    if local is None or opponent is None or not attacker_ids:
        return {}
    if _all_in(state):
        return {}  # nothing to lose: a chump attack still might matter
    battlefield = state.get("battlefield") or []
    # bug_20261006_184540: "Cadet 2/2 can block Yuriko" held back a creature
    # that Tetsuko Umezawa made unblockable. Mark board grants for _can_block.
    annotate_cant_be_blocked(battlefield)
    cards = {card.get("instance_id"): card for card in battlefield}
    attackers = [cards.get(identity) for identity in attacker_ids]
    if any(
        card is None
        or _controller(card) != local.get("seat_id")
        or not _is_int(card.get("power"))
        or not _is_int(card.get("toughness"))
        for card in attackers
    ):
        return {}
    rewarded: set[str] = set()  # creature types a typed attack payoff rewards
    for card in battlefield:
        if _controller(card) != local.get("seat_id") or card.get("instance_id") in attacker_ids:
            continue
        payoff = _typed_attack_payoff(_rules_text(card))
        if payoff is None:
            return {}
        rewarded |= payoff
    if any(_HAND_PAYOFF.search(_rules_text(card)) for card in state.get("hand") or []):
        return {}
    if any("block" in _rules_text(card) for card in _resolved_this_turn(state)):
        return {}
    mana = _untapped_mana(state, local)
    blockers = _eligible_blockers(state, opponent.get("seat_id"))
    if not blockers:
        return {}
    affordable = [
        (text, cost, identity)
        for _source, cost, text, identity in _possible_tricks(state, local)
        if cost <= mana
    ]
    # A plain pump is judged per attacker (_doomed): it saves one only if the
    # bigger creature (every affordable activation of it) is no longer
    # answered. Anything else that could save it keeps the whole attack.
    pumps: list[tuple[int | None, int, int]] = []
    for text, cost, identity in affordable:
        pump = _pump_only(text)
        times = _pump_activations(text, cost, mana, ability=identity is not None) if pump else None
        if pump is not None and times is not None:
            power, toughness, self_only = pump
            pumps.append((identity if self_only else None, power * times, toughness * times))
        elif text is None or _SAVES_ATTACKER.search(text):
            return {}
    removal_tricks = [text for text, _cost, _identity in affordable if text and _REMOVES_BLOCKER.search(text)]
    opp_life = opponent.get("life_total")
    opp_life = opp_life if _is_int(opp_life) else 20
    pings = sum(_attack_ping_damage(card) for card in attackers)
    if _best_through(attackers, blockers, opp_life) + pings >= opp_life:
        return {}  # lethal through their best blocks (attack pings included): every attacker counts
    forced = {
        int(entry.get("attackerInstanceId") or 0)
        for entry in [
            *(attack_candidates(state) or []),
            *(attack_candidates(state, pending) or [] if pending else []),
        ]
        if entry.get("mustAttack")
    }
    walker_kills = _walker_kills(attackers, blockers, cards, attacker_targets or {}, opponent.get("seat_id"))
    for identity, reason in walker_kills.items():
        logger.info("Losing-attack guard: keeping %s: %s", cards[identity].get("name") or identity, reason)
    payoffs = {card["instance_id"]: _attack_payoff(card) for card in attackers}
    # What a blocker that survives can do with their tricks (#120), and what our burn kills (#113).
    exposure = combat_solver.attacking_trick_exposure(state, opponent.get("seat_id"))
    burn = _hand_burn(state)
    judged = [
        card
        for card in attackers
        if card["instance_id"] not in forced
        and card["instance_id"] not in walker_kills
        and payoffs[card["instance_id"]] is not None
        and not any(_has_type(card, kind) for kind in rewarded)
    ]
    common = {
        "opp_life": opp_life,
        "battlefield": battlefield,
        "strategy_text": strategy_text,
        "removal_tricks": removal_tricks,
        "pumps": pumps,
        "exposure": exposure,
        "burn": burn,
    }
    # Blocks that cost us nothing to refuse (#120's free bounce under their tricks, #113's
    # engine trade into a blocker our burn kills) are judged as before: what they risk if
    # they block, whatever their best blocks are.
    remaining = list(attackers)
    losing: dict[int, str] = {}
    while True:
        worst = None
        for attacker in judged:
            if attacker not in remaining:
                continue
            verdict = _doomed(
                attacker,
                remaining,
                blockers,
                payoff=payoffs[attacker["instance_id"]] or 0,
                fed=False,
                **common,
            )
            if verdict is not None and (worst is None or verdict.deficit > worst[1].deficit):
                worst = (attacker, verdict)
        if worst is None:
            break
        attacker, verdict = worst
        losing[attacker["instance_id"]] = _losing_reason(
            attacker,
            verdict,
            battlefield,
            several=len(remaining) > 1,
            payoff=payoffs[attacker["instance_id"]] or 0,
        )
        remaining = [card for card in remaining if card is not attacker]
    # #168 (match 639c5fe1 T11): an attacker their blockers eat is held only for what it adds
    # through the blocks they actually make, net of the lifelink of its own blockers there
    # (each blocker counted once): nothing at all ("useless"), or less than its own damage
    # when the line search's best line keeps it home ("costly"). The shrinking attack is
    # re-judged after each hold, then the holds must beat the declaration (_thinned_declaration).
    theirs = _TheirBlocks(blockers, opp_life)
    line_attack = _line_search_attackers(state)
    if line_attack is not None and any(
        card not in judged
        or not _answer_groups(card, blockers, battlefield=battlefield, strategy_text=strategy_text)
        for card in remaining
        if card["instance_id"] not in line_attack
    ):
        line_attack = None  # it keeps home a creature this guard never holds: no costly hold can match it
    removed: list[tuple[dict, str, bool]] = []  # (attacker, reason, costly)
    nets = [theirs.net(remaining)]
    thinning = list(remaining)
    while True:
        _through, on = theirs.blocks(thinning)
        worst = None
        for attacker in judged:
            identity = attacker["instance_id"]
            if attacker not in thinning or identity not in on:
                continue  # unblocked in their best blocks: it deals its damage
            marginal, taken, mine = theirs.marginal(attacker, thinning)
            added = marginal - taken
            costly = added > 0
            if costly and (line_attack is None or identity in line_attack):
                continue  # it adds damage, and no line search says to keep it home
            verdict = _doomed(attacker, thinning, blockers, payoff=payoffs[identity] or 0, **common)
            if verdict is None or verdict.kind != "fed":
                continue
            if {card.get("instance_id") for card in mine} == {
                card.get("instance_id") for card in verdict.group
            }:
                verdict.shield, verdict.gained = marginal, taken  # the blocks they make
            rank = (added, -verdict.deficit)
            if worst is None or rank < worst[0]:
                worst = (rank, attacker, verdict, costly)
        if worst is None:
            break
        _rank, attacker, verdict, costly = worst
        reason = _losing_reason(
            attacker,
            verdict,
            battlefield,
            several=len(thinning) > 1,
            payoff=payoffs[attacker["instance_id"]] or 0,
        )
        removed.append((attacker, reason, costly))
        thinning = [card for card in thinning if card is not attacker]
        nets.append(theirs.net(thinning))
    keep = _thinned_declaration(
        remaining,
        removed,
        nets,
        blockers,
        line_attack=line_attack,
        battlefield=battlefield,
        strategy_text=strategy_text,
    )
    for attacker, reason, _costly in removed[:keep]:
        losing[attacker["instance_id"]] = reason
    remaining = [card for card in remaining if card["instance_id"] not in losing]
    if strategy_text and _EVASION_CLAIM_RE.search(strategy_text):
        for attacker in remaining:
            trade = _trade_note(attacker, blockers)
            if trade:
                logger.info("Losing-attack guard: keeping %s: %s", attacker.get("name") or "attacker", trade)
    return losing


def _thinned_declaration(
    attackers: list[dict],
    removed: list[tuple[dict, str, bool]],
    nets: list[int],
    blockers: list[dict],
    *,
    line_attack: frozenset[int] | None,
    battlefield: list[dict],
    strategy_text: str,
) -> int:
    """How many of the guard's removals (in order) to apply: the thinned attack must beat the declaration (#168).

    ``removed`` holds (attacker, reason, costly) in removal order and
    ``nets[i]`` the damage the attack less its first ``i`` removals gets
    through their best blocks, net of their lifelink. Never chosen:
      - a count that holds more identical copies (same name and
        power/toughness) than their blockers can answer at once, unless every
        copy in the declaration is held: the copies past that number get
        through whatever they block (match 639c5fe1 T11: 7 Rabbits held
        against 4 blockers);
      - a count with a "costly" hold (one that adds damage, less than its
        own) unless what still attacks is no more than the line search's
        best line sends: that line is what says the damage is not worth the
        creature (#127's face attack into Hortimancer: hold both, as the
        search does), never a half-thinned attack (T13: Leonardo alone).
    The count with the most net damage wins; on a tie, the one that holds
    more (holding loses no creature). A count whose costly holds the line
    search backs wins outright: the damage it gives up is the line's choice.
    """
    declared = Counter(_copy_key(card) for card in attackers)
    caps: dict[tuple, int] = {}
    held: Counter = Counter()
    left = {card["instance_id"] for card in attackers}
    admissible, backed = [0], []
    any_costly = False
    for count, (attacker, _reason, costly) in enumerate(removed, start=1):
        key = _copy_key(attacker)
        held[key] += 1
        left.discard(attacker["instance_id"])
        any_costly = any_costly or costly
        if key not in caps:
            caps[key] = _copies_answered(
                attacker, blockers, battlefield=battlefield, strategy_text=strategy_text
            )
        if not all(held[k] <= caps[k] or held[k] == declared[k] for k in held):
            continue
        if not any_costly:
            admissible.append(count)
        elif line_attack is not None and left <= line_attack:
            backed.append(count)
    if backed:
        best = max(backed)
        logger.info(
            "Losing-attack guard: holding %s as the line search's best line does: %d through their best "
            "blocks net of lifelink with them, %d without",
            ", ".join(
                str(attacker.get("name") or attacker["instance_id"]) for attacker, *_ in removed[:best]
            ),
            nets[0],
            nets[best],
        )
        return best
    best = max(admissible, key=lambda count: (nets[count], count))
    if best < len(removed):
        logger.info(
            "Losing-attack guard: keeping %s: with them the attack gets %d through their best blocks net of "
            "lifelink, holding them %d",
            ", ".join(
                str(attacker.get("name") or attacker["instance_id"]) for attacker, *_ in removed[best:]
            ),
            nets[best],
            nets[len(removed)],
        )
    return best


def _losing_reason(
    attacker: dict, verdict: _Doomed, battlefield: list[dict], *, several: bool, payoff: int = 0
) -> str:
    group = verdict.group
    name = attacker.get("name") or attacker.get("instance_id")
    body = f"{attacker['power']}/{attacker['toughness']}"
    blocked_by = " and ".join(
        f"{card.get('name') or 'a creature'} {card['power']}/{card['toughness']}" for card in group
    )
    outcome = _resolve_attacker(attacker, group)
    unmet_mod = unmet_modified_flag(attacker, battlefield=battlefield)
    if verdict.kind == "bounce":
        reason = (
            f"{blocked_by} can block {name} {body} for free: neither dies and nothing gets through, "
            f"while their {verdict.note} make a trick on that block a kill"
        )
    elif verdict.kind == "engine":
        reason = (
            f"{blocked_by} would trade with {name} {body}, which our spells grow, while {verdict.note} "
            f"in hand kills {group[0].get('name') or 'that blocker'} on its own: keep the engine"
        )
    elif outcome.blockers_died and unmet_mod:
        reason = (
            f"{name} {body} is {unmet_mod}, so {blocked_by} can legally block and kill it "
            "with 0 damage through"
        )
    elif outcome.blockers_died:
        lost = sum(_material(card) for card in outcome.blockers_died)
        reason = (
            f"{blocked_by} can legally block {name} {body} and trade with it at a loss "
            f"({_material(attacker)} for {lost} in material, 0 damage through): the plan counts on "
            "evasion it does not have against that blocker"
        )
    else:
        payoff_note = (
            f"its attack trigger is worth only {payoff}" if payoff else "it has no attack or death payoff"
        )
        reason = (
            f"{blocked_by} can block {name} {body}, kill it and survive; nothing gets through, "
            f"{payoff_note}, and no affordable trick could change that combat"
        )
    if verdict.gained:
        reason += f"; lifelink gains them {verdict.gained} life"
    if several:
        reason += (
            f"; while it is blocked the rest of the attack gets only {verdict.shield} more damage through"
            if verdict.shield > 0
            else "; the rest of the attack gets nothing more through for it"
        )
    return reason


def _trade_note(attacker: dict, blockers: list[dict]) -> str:
    """'trades with X' when a single blocker trades with the attacker (kept: a trade is not a loss)."""
    for blocker in blockers:
        if not _group_can_block(attacker, [blocker]):
            continue
        outcome = _resolve_attacker(attacker, [blocker])
        if outcome.attacker_died and outcome.blockers_died and outcome.damage_through == 0:
            return (
                f"{blocker.get('name') or 'a blocker'} {blocker['power']}/{blocker['toughness']} can block "
                f"and trade with it ({_material(attacker)} for {_material(blocker)} in material): "
                "a trade, not a loss"
            )
    return ""


def pump_before_blocks_wasted(state: dict, source: dict, effect: str, cost: str = "") -> str:
    """Why pumping our attacking ``source`` now, before blockers, buys nothing; '' otherwise (#78).

    2026-10-19 T10 (match abde2a07, 11:31:39): Hired Claw 1/2 attacked alone
    past an untapped Creakwood Safewright 4/4; after its ping the planner spent
    {1}{R} on its +1/+1 counter "to improve its odds against non-deathtouch
    blockers", and the 4/4 blocked the 2/3 and killed it. Before blocks, a
    self-pump the opponent can still answer (some untapped group of theirs
    blocks the bigger creature, kills it and loses nothing) changes nothing;
    the same mana still pumps it after blocks, when it is unblocked or the
    pump wins the fight it got. Only a pump of the source itself is judged,
    with every activation our open mana (``cost`` each) pays for.
    """
    effect = str(effect or "").lower()
    pump = _pump_only(effect)
    local = _local_player(state)
    if pump is None or not pump[2] or local is None or not source:
        return ""
    times = _pump_activations(effect, _mana_value(cost), _untapped_mana(state, local), ability=True)
    if times is None:
        return ""  # repeatable at no known cost: no clear verdict
    if _controller(source) != local.get("seat_id") or not source.get("is_attacking"):
        return ""
    if not _is_int(source.get("power")) or not _is_int(source.get("toughness")):
        return ""
    from arenamcp.board_model import canonical_phase_step

    turn = state.get("turn") or {}
    if turn.get("active_player") != local.get("seat_id"):
        return ""
    if canonical_phase_step(turn)[1] != "Step_DeclareAttack":
        return ""
    battlefield = state.get("battlefield") or []
    if any(card.get("is_blocking") for card in battlefield):
        return ""  # blocks are in: the pump decides a known fight
    opponent = next((player for player in state.get("players") or [] if not player.get("is_local")), None)
    if opponent is None:
        return ""
    annotate_cant_be_blocked(battlefield)
    blockers = _eligible_blockers(state, opponent.get("seat_id"))
    bigger = _pumped(source, pump[0] * times, pump[1] * times)
    groups = _answer_groups(bigger, blockers, battlefield=battlefield)
    if not groups:
        return ""
    group = groups[0]
    blocked_by = " and ".join(
        f"{card.get('name') or 'a creature'} {card['power']}/{card['toughness']}" for card in group
    )
    name = source.get("name") or "the attacker"
    return (
        f"pumping the attacking {name} to {bigger['power']}/{bigger['toughness']} before blocks saves "
        f"nothing: {blocked_by} can still block it, kill it and survive; after blocks the same mana "
        "pumps it if it is unblocked or the pump wins its fight"
    )


# --- Free-attack rule ----------------------------------------------------------
#
# 2026-10-08 FRA QuickDraft review (WP13): the legacy planner answered three
# declare-attackers windows with "develop / hold blockers" while the board
# facts said the attack was free, and in game 2 Arni looted before combat every
# turn instead of attacking into an empty board. An attack is FREE when the
# opponent's best blocks (worst case for us, tricks excluded) kill none of the
# attackers, at least one point reaches a recipient, and the creatures left
# home still stop every point of crackback that holding everything would stop.
# Crackback is ``combat_solver.evaluate_attack``'s: a creature that leaves at our
# end step (Fleeting Effigy) stops nothing at home, and a chump the chump guard
# would refuse stops nothing either (2026-10-19 T10, match c36db3a3: Slickshot
# Show-Off "held back to chump" Elenda at 18 life).
# The autopilot attacks with that set whatever the model's plan says; the model
# may add attackers but never drop free ones (``ActionPlanner._ensure_free_attacks``),
# and a tap ability that would forfeit a free attack is withheld before combat
# (``play_safety.tap_forfeits_free_attack``).

_DEFENDER = re.compile(r"(?:^|\n)\s*defender\b", re.IGNORECASE)
_CANT_ATTACK_TEXT = re.compile(r"(?:^|\n)\s*(?:~|this creature)? ?can't attack\b", re.IGNORECASE)


@dataclass
class FreeAttack:
    attacker_ids: list[int]
    attacker_names: list[str]
    damage: int  # through their best blocks
    crackback: int  # worst case next turn with the rest home
    hold_crackback: int  # worst case next turn holding everything
    explanation: str = ""


def able_attackers(state: dict) -> list[dict]:
    """Our creatures that could be declared as attackers now (no live request needed).

    Untapped, not summoning sick (or haste), no defender/can't-attack text,
    with known power and toughness. Used before combat, where the GRE has not
    yet listed the legal attackers.
    """
    local = _local_player(state)
    if local is None:
        return []
    turn = int((state.get("turn") or {}).get("turn_number") or 0)
    result = []
    for card in state.get("battlefield") or []:
        if _controller(card) != local.get("seat_id") or "creature" not in _types(card):
            continue
        if card.get("is_tapped") or card.get("is_phased_out"):
            continue
        if not _is_int(card.get("power")) or not _is_int(card.get("toughness")):
            continue
        text = _rules_text(card)
        if _DEFENDER.search(text) or _CANT_ATTACK_TEXT.search(text) or _has(card, "defender"):
            continue
        entered = card.get("turn_entered_battlefield")
        sick = card.get("summoning_sickness")
        if not _has(card, "haste") and (sick or (_is_int(entered) and turn and entered >= turn)):
            continue
        result.append(card)
    return result


def free_attackers(
    state: dict, pending: dict | None = None, *, candidate_ids: list[int] | None = None
) -> FreeAttack | None:
    """The largest attack set that is free, or None.

    Candidates are the live request's legal attackers (``candidate_ids`` or
    ``attack_candidates``), else :func:`able_attackers`. Zero-power and
    mandatory-attack bookkeeping is left to the existing guards. Returns None
    with an all-in board (that rule attacks with everything anyway), when
    nothing survives their best blocks, when nothing gets through, or when
    the attack lets more crackback through than holding would.
    """
    from arenamcp.combat_solver import _search_blocks, evaluate_attack

    players = state.get("players") or []
    local = next((player for player in players if player.get("is_local")), None)
    opponent = next((player for player in players if not player.get("is_local")), None)
    if local is None or opponent is None:
        return None
    if _all_in(state):
        return None
    battlefield = state.get("battlefield") or []
    annotate_cant_be_blocked(battlefield)
    cards = {card.get("instance_id"): card for card in battlefield}
    if candidate_ids is None:
        raw = attack_candidates(state, pending)
        if raw:
            candidate_ids = [int(entry.get("attackerInstanceId") or 0) for entry in raw]
        else:
            candidate_ids = [card["instance_id"] for card in able_attackers(state)]
    candidates = []
    for identity in candidate_ids:
        card = cards.get(identity)
        if (
            card is None
            or _controller(card) != local.get("seat_id")
            or not _is_int(card.get("power"))
            or not _is_int(card.get("toughness"))
            or int(card["power"]) <= 0
        ):
            continue
        candidates.append(card)
    if not candidates or len(candidates) > 8:
        return None  # a wide board is the solver's call, not a cheap rule's
    our_life = int(local.get("life_total", 20))
    opp_life = int(opponent.get("life_total", 20))
    theirs = [
        card
        for card in battlefield
        if _controller(card) == opponent.get("seat_id") and is_creature_now(card)
    ]
    blockers = _eligible_blockers(state, opponent.get("seat_id"))
    ours_home = [
        card
        for card in battlefield
        if _controller(card) == local.get("seat_id")
        and "creature" in _types(card)
        and not card.get("is_tapped")
        and _is_int(card.get("power"))
        and _is_int(card.get("toughness"))
    ]
    hold = evaluate_attack([], ours_home, blockers, opp_life, our_life, theirs)
    attacking = list(candidates)
    damage = 0
    for _ in range(len(candidates) + 1):
        if not attacking:
            return None
        plan = _search_blocks(attacking, blockers, opp_life)
        if plan is None:
            damage = sum(int(card["power"]) for card in attacking)
            dead = []
        else:
            groups: dict[int, list[dict]] = {}
            by_id = {card["instance_id"]: card for card in blockers}
            for blocker_id, attacker_id in plan.assignments.items():
                if attacker_id and blocker_id in by_id:
                    groups.setdefault(attacker_id, []).append(by_id[blocker_id])
            dead = [
                card
                for card in attacking
                if _resolve_attacker(card, groups.get(card["instance_id"], [])).attacker_died
            ]
            damage = plan.damage_through
        if not dead:
            break
        attacking = [card for card in attacking if card not in dead]
    if not attacking or damage <= 0:
        return None
    home = [card for card in ours_home if card not in attacking or _has(card, "vigilance")]
    attack = evaluate_attack(attacking, home, blockers, opp_life, our_life, theirs)
    if attack.worst_case_crackback > hold.worst_case_crackback:
        return None
    names = [str(card.get("name") or card["instance_id"]) for card in attacking]
    return FreeAttack(
        attacker_ids=[card["instance_id"] for card in attacking],
        attacker_names=names,
        damage=damage,
        crackback=attack.worst_case_crackback,
        hold_crackback=hold.worst_case_crackback,
        explanation=(
            f"{', '.join(names)}: {damage} through their best blocks, none of them dies, "
            f"crackback {attack.worst_case_crackback} (holding everything: {hold.worst_case_crackback})"
        ),
    )


# --- Declare-blockers safety ----------------------------------------------------
#
# 2026-10-06 G2 T10 (match 3da54de9): at 20 life the model chump-blocked a
# trampling 4/4 Beast with Mindseeker Oculus (2/1). Three damage trampled
# over anyway; the block saved one point of life for a creature.
# 2026-10-06 15:23:16: a Declare Blockers request with a legal no-block and a
# legal block went MANUAL REQUIRED because the planner returned no actions and
# the safe default refused to declare anything. The helpers below give that
# safe default a validated declaration and drop chump blocks that buy little.

# At or below this life after combat, a chump that saves damage is kept.
CHUMP_DANGER_LIFE = 5
# The blocker's own text pays off dying or blocking: the chump has value.
_BLOCKER_PAYOFF = re.compile(
    r"\bwhen(?:ever)?\b[^.]*\b(?:dies|die|blocks?|is put into (?:a|your) graveyard|leaves the battlefield)\b"
    r"|\b(?:undying|persist|afterlife)\b"
    r"|:[^.\n]*\bgets? \+\d+/"
)
# Damage the attacker deals to a player is worth more than its number. "deal"
# too: 2026-10-19 07:34:34 the guard dropped a Rabbit's block on Dark Leo &
# Shredder ("Whenever Dark Leo & Shredder deal combat damage to a player,
# create a 1/1 black Ninja creature token...") as a one-damage chump.
_ATTACKER_HIT_PAYOFF = re.compile(
    r"deals? (?:combat )?damage to (?:a player|an opponent|that player|you)\b|\b(?:infect|toxic|poisonous)\b"
)
# An instant-speed trick that can turn a chump into a winning block.
_BLOCK_TRICK = re.compile(
    r"\bgets? \+(?:\d+|x)/\+(?:\d+|x)\b"
    r"|\bgains? (?:first strike|double strike|deathtouch|indestructible)\b"
    r"|\bprevent all combat damage\b"
)


def _local_player(state: dict) -> dict | None:
    return next((player for player in state.get("players") or [] if player.get("is_local")), None)


def _next_attack_damage(threats: list[dict], blockers: list[dict], factors: dict | None = None) -> int:
    """Their next attack's damage through our ``blockers``, each chumping one of their biggest attackers (#171).

    2026-10-19 match 2b77f14d T9: the chump guard compared our life with their
    whole board's power (47 against our 19), so every chump looked worthless
    and all four were dropped (19 -> 6; the next attack was lethal). A blocker
    absorbs an attacker it can block (flying/reach, two for menace; a
    trampler's excess over the blockers' toughness still gets through), their
    biggest attackers first and our least flexible blocker first. No first
    strike, deathtouch or pump: a forecast. ``factors``: controller seat ->
    damage multiplier (``_damage_factors``).
    """
    factors = factors or {}

    def hits(card: dict) -> int:
        return max(0, int(card["power"])) * factors.get(_controller(card), 1)

    reach = {id(blocker): sum(1 for threat in threats if _can_block(threat, blocker)) for blocker in blockers}
    free = sorted(blockers, key=lambda blocker: reach[id(blocker)])
    total = 0
    for threat in sorted(threats, key=hits, reverse=True):
        need = 2 if _has(threat, "menace") else 1
        able = [blocker for blocker in free if _can_block(threat, blocker)][:need]
        if len(able) < need:
            total += hits(threat)
            continue
        used = {id(blocker) for blocker in able}
        free = [blocker for blocker in free if id(blocker) not in used]
        if _has(threat, "trample"):
            excess = max(0, int(threat["power"]) - sum(max(0, int(blocker["toughness"])) for blocker in able))
            total += excess * factors.get(_controller(threat), 1)
    return total


def _after_combat(
    attackers: list[dict], plan: dict[int, int], cards: dict, ours: list[dict], threats: list[dict]
) -> tuple[list[dict], list[dict]]:
    """(our creatures, their attackers) left for their next attack after this combat under ``plan`` (#171)."""
    died: set = set()
    killed: set = set()
    for attacker in attackers:
        identity = attacker.get("instance_id")
        group = [
            cards[blocker] for blocker, target in plan.items() if target == identity and blocker in cards
        ]
        if not group:
            continue
        outcome = _resolve_attacker(attacker, group)
        died |= {card.get("instance_id") for card in outcome.blockers_died}
        if outcome.attacker_died:
            killed.add(identity)
    return (
        [card for card in ours if card.get("instance_id") not in died],
        [card for card in threats if card.get("instance_id") not in killed],
    )


def _chump_for_next_attack(
    life_with: int,
    life_without: int,
    next_with: int,
    next_without: int,
    *,
    wider: bool,
    line_survives=None,
) -> bool:
    """Whether a chump stays for their next attack (#171).

    ``next_with`` / ``next_without``: that attack's damage through our blockers
    left with the chump (its blocker dead) and without it (``_next_attack_damage``).
    It stays when it is what keeps us out of lethal range then. When we are dead
    then either way, it stays if they have more creatures than we do (``wider``:
    each body we keep absorbs only one of many attackers, the life it saves now
    is the scarcer resource), else as the line search says: its best line
    living through that attack (``line_survives()``, None without a search)
    counts on the life.
    """
    dead_with, dead_without = life_with <= next_with, life_without <= next_without
    if not dead_without:
        return False
    if not dead_with:
        return True
    return wider or bool(line_survives is not None and line_survives())


def _line_search_survives_their_next_attack(state: dict) -> bool | None:
    """Whether the line search's best line lives through their next attack (#171); None without a search."""
    try:
        from arenamcp import line_guard
        from arenamcp.board_assessment import assess

        result = getattr(assess(state), "line_search", None)
        if not line_guard._usable(result):
            return None
        best = result.best
        if best.cls != line_guard.DEAD or best.dead_turn is None:
            return True
        turn = state.get("turn") or {}
        number = turn.get("turn_number")
        local = _local_player(state)
        if not _is_int(number) or local is None:
            return None
        ours_now = turn.get("active_player") == local.get("seat_id")
        return best.dead_turn > number + (1 if ours_now else 2)
    except Exception:  # the strategic layer never decides a guard on its own failure
        logger.debug("line search death turn unavailable", exc_info=True)
        return None


def _refused_chumps(
    plan, attackers: list[dict], defenders: dict[int, dict], life: int
) -> list[tuple[list[int], int]]:
    """The chumps in ``plan`` that ``wasteful_chump_blocks`` would drop, as (blocker ids, attacker id).

    Mirrors the guard without the board context a crackback forecast lacks
    (planeswalkers, tricks in hand, the rest of their board, a line search):
    their next attack is these attackers again. Each is judged against the
    whole plan.
    """
    groups: dict[int, list[int]] = {}
    for blocker, attacker in plan.assignments.items():
        groups.setdefault(attacker, []).append(blocker)
    refused = []
    life_with = life - plan.damage_through
    ours = list(defenders.values())
    threats = [card for card in attackers if can_attack_next_turn(card)]
    wider = len(attackers) > len(ours)
    for attacker in attackers:
        identity = int(attacker.get("instance_id") or 0)
        blockers = groups.get(identity, [])
        unit = 2 if _has(attacker, "menace") and len(blockers) == 2 else 1
        if len(blockers) != unit:
            continue
        bodies = [defenders[blocker] for blocker in blockers]
        outcome = _resolve_attacker(attacker, bodies)
        if outcome.attacker_died or any(body not in outcome.blockers_died for body in bodies):
            continue
        if any(_BLOCKER_PAYOFF.search(_rules_text(body)) for body in bodies):
            continue
        if outcome.damage_through == 0 and _ATTACKER_HIT_PAYOFF.search(_rules_text(attacker)):
            continue
        life_without = life_with - (_resolve_attacker(attacker, []).damage_through - outcome.damage_through)
        if life_without <= CHUMP_DANGER_LIFE:
            continue
        without = {b: a for b, a in plan.assignments.items() if b not in blockers}
        left_with, coming_with = _after_combat(attackers, plan.assignments, defenders, ours, threats)
        left_without, coming_without = _after_combat(attackers, without, defenders, ours, threats)
        if _chump_for_next_attack(
            life_with,
            life_without,
            _next_attack_damage(coming_with, left_with),
            _next_attack_damage(coming_without, left_without),
            wider=wider,
        ):
            continue
        refused.append((blockers, identity))
    return refused


def guarded_block_plan(
    attackers: list[dict], defenders: list[dict], life: int, *, max_options: int = 50_000
):
    """Our best blocks against ``attackers`` (a crackback forecast) without chumps the chump guard refuses.

    2026-10-19 T10 (match c36db3a3): the free-attack rule held Slickshot
    Show-Off back because, kept home, it would chump Elenda, Saint of Dusk
    4/4 at 18 life and stop the whole crackback. ``wasteful_chump_blocks``
    drops exactly that chump above CHUMP_DANGER_LIFE (on T11 we did not block
    and took 4), so holding stopped nothing. The refused chumps are forbidden
    and the search re-run once (at most two searches, as the attack budget
    assumes); if dropping them all together leaves us at CHUMP_DANGER_LIFE or
    less, the chumps stay.
    """
    if not attackers:
        return None
    key = ("guarded_blocks", _cards_key(attackers), _cards_key(defenders), life, max_options)
    return _memoized(key, lambda: _guarded_block_plan(attackers, defenders, life, max_options))


def _guarded_block_plan(attackers: list[dict], defenders: list[dict], life: int, max_options: int):
    plan = combat_solver._search_blocks(attackers, defenders, life, max_options=max_options)
    by_id = {int(card.get("instance_id") or 0): card for card in defenders}
    attacker_ids = {int(card.get("instance_id") or 0) for card in attackers}
    if (
        plan is None
        or not _is_int(life)
        or 0 in by_id
        or len(by_id) != len(defenders)
        or 0 in attacker_ids
        or len(attacker_ids) != len(attackers)
        or not all(_known_body(card) for card in [*attackers, *defenders])
    ):
        return plan
    refused = _refused_chumps(plan, attackers, by_id, life)
    if not refused:
        return plan
    forbidden: dict[int, set[int]] = {}
    for blockers, attacker in refused:
        for blocker in blockers:
            forbidden.setdefault(blocker, set()).add(attacker)
    allowed = {blocker: attacker_ids - ids for blocker, ids in forbidden.items()}
    guarded = combat_solver._search_blocks(
        attackers, defenders, life, blocker_allowed_attackers=allowed, max_options=max_options
    )
    if guarded is None or life - guarded.damage_through <= CHUMP_DANGER_LIFE:
        return plan
    return guarded


# --- Combat-damage triggers that add damage this combat ----------------------
#
# 2026-10-19 T21 (match cd41ad5a, Brawl): "Whenever Shark Shredder deals combat
# damage to a player, put up to one target creature card from that player's
# graveyard onto the battlefield under your control. It enters tapped and
# attacking that player." Shark has first strike, so the Hare Apparent it took
# from our graveyard dealt 2 more in the regular damage step. The block guard
# counted 12 into 14 life ("survival at 2"), Leonardo blocked the Hare, and the
# real 14 was exactly lethal; Leonardo on Shark kept us at 4.

_HIT_TRIGGER = re.compile(
    r"\bwhenever (?P<who>[^,.\n]+?) deals? combat damage to (?:a player|an opponent|a player or planeswalker)"
    r"\b,? (?P<effect>[^\n]*)"
)
_GROUP_SUBJECT = re.compile(r"^(?:a|an|one or more|another|each|equipped|enchanted|other)\b")
_REANIMATE_ATTACKING = re.compile(
    r"\b(?:put|return)s? (?:up to )?(?P<n>a|an|one|two|three|\d+)? ?(?:target )?creature cards? from "
    r"(?P<whose>that player's|your|a|an opponent's|their) graveyards? (?:onto|to) the battlefield"
    r"[^\n]*?\battacking\b"
)
_TOKEN_ATTACKING = re.compile(
    r"\bcreate (?P<n>a|an|one|two|three|\d+) (?:tapped )?(?P<p>\d+)/\d+ [^.\n]*?tokens?[^.\n]*?\battacking\b"
)
_HIT_LOSS = re.compile(
    r"\b(?:that player|defending player|each opponent|target (?:player|opponent)) loses (?P<n>\d+) life"
    r"|\bdeals? (?P<d>\d+) damage to (?:that player|defending player|each opponent|target (?:player|opponent)"
    r"|any target)"
)
_COUNT_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, None: 1}


def _count(word: str | None) -> int:
    return int(word) if word and word.isdigit() else _COUNT_WORDS.get(word, 1)


def hit_bonus(attacker: dict, state: dict, defender_seat) -> int:
    """Extra damage this combat when ``attacker`` deals combat damage to the defending player.

    Its own trigger only: "that player loses N life" / "deals N damage to that
    player" (twice with double strike); and, for a first or double striker
    whose trigger resolves before the regular damage step, a creature card
    returned from a graveyard (the biggest ones there) or tokens that enter
    attacking. 0 when nothing applies.
    """
    total = 0
    striker = _has(attacker, "first strike") or _has(attacker, "double strike")
    for trigger in _HIT_TRIGGER.finditer(_rules_text(attacker)):
        if _GROUP_SUBJECT.match(trigger.group("who").strip()):
            continue
        effect = trigger.group("effect")
        for loss in _HIT_LOSS.finditer(effect):
            total += int(loss.group("n") or loss.group("d")) * (2 if _has(attacker, "double strike") else 1)
        if not striker:
            continue
        reanimate = _REANIMATE_ATTACKING.search(effect)
        if reanimate:
            whose = reanimate.group("whose")
            seats = (
                {defender_seat}
                if whose in ("that player's", "an opponent's", "their")
                else {_controller(attacker)}
                if whose == "your"
                else None
            )
            powers = sorted(
                (
                    int(card["power"])
                    for card in state.get("graveyard") or []
                    if "creature" in _types(card)
                    and _is_int(card.get("power"))
                    and (seats is None or card.get("owner_seat_id") in seats)
                ),
                reverse=True,
            )
            total += sum(max(0, power) for power in powers[: _count(reanimate.group("n"))])
        token = _TOKEN_ATTACKING.search(effect)
        if token:
            total += _count(token.group("n")) * int(token.group("p"))
    return total


def with_hit_bonus(attackers: list[dict], state: dict, defender_seat) -> list[dict]:
    """``attackers`` with ``combat_hit_bonus`` set where ``hit_bonus`` applies (copies; others unchanged)."""
    marked = []
    for card in attackers:
        bonus = hit_bonus(card, state, defender_seat) if _known_body(card) else 0
        marked.append({**card, "combat_hit_bonus": bonus} if bonus > 0 else card)
    return marked


def _legal_block_map(state: dict, pending: dict | None) -> dict[int, set[int]] | None:
    """Blocker id -> attacker ids it may block, from the live request or the logged one.

    A blocker the request shows selected counts with its options from this
    combat (``combat_solver.blocker_options``), not as unable to block.
    """
    from arenamcp.combat_solver import blocker_options

    context = state.get("decision_context") or {}
    raw = pending.get("blockers") if isinstance(pending, dict) else None
    if not isinstance(raw, list):
        raw = context.get("raw_blockers")
    if not isinstance(raw, list):
        return None
    return blocker_options(raw, context.get("blocker_options"))


def _incoming_attackers(
    state: dict, local_seat: int | None, legal: dict[int, set[int]] | None
) -> list[dict] | None:
    """Opposing attackers this combat; None when one is named but not on the board.

    Attackers whose combat-damage trigger adds damage this combat carry
    ``combat_hit_bonus`` (``with_hit_bonus``).
    """
    cards = {card.get("instance_id"): card for card in state.get("battlefield") or []}
    ids: set[int] = set()
    for identity in (state.get("decision_context") or {}).get("attacker_ids") or []:
        if str(identity).isdigit():
            ids.add(int(identity))
    for allowed in (legal or {}).values():
        ids.update(allowed)
    for card in cards.values():
        if card.get("is_attacking") and _controller(card) != local_seat:
            ids.add(card.get("instance_id"))
    if any(identity not in cards for identity in ids):
        return None
    ids -= phantom_attackers(state, ids)
    return with_hit_bonus([cards[identity] for identity in sorted(ids)], state, local_seat)


def phantom_attackers(state: dict, attacker_ids, raw_blockers: list | None = None) -> set[int]:
    """Attacker ids Arena's DeclareBlockers request shows are not attacking.

    2026-10-19 T9 (match 6175f002, Player.log gsid 268-270): the opponent
    declared Momo, Playful Pet and Guiding Hydra, then withdrew the Hydra
    before submitting; our log state kept Hydra attacking and the block
    guard counted its 4 damage. The request's blockers list every attacker
    each may block. An attacker missing from the list of a blocker that could
    legally block it by keywords and text is not attacking. Menace attackers
    and blockers with blocking restrictions prove nothing; an unblockable
    attacker (a flyer against ground blockers) is never in the list and stays,
    and so does any attacker while a "can't be blocked" effect may apply to it.
    """
    if raw_blockers is None:
        raw_blockers = (state.get("decision_context") or {}).get("raw_blockers")
    if not isinstance(raw_blockers, list):
        return set()
    battlefield = state.get("battlefield") or []
    annotate_cant_be_blocked(battlefield)
    if any(_UNBLOCKABLE_TEXT.search(_rules_text(card)) for card in _resolved_this_turn(state)):
        return set()
    cards = {card.get("instance_id"): card for card in battlefield}
    phantoms: set[int] = set()
    for entry in raw_blockers:
        if not isinstance(entry, dict):
            continue
        try:
            blocker = cards.get(int(entry.get("blockerInstanceId") or entry.get("instanceId") or 0))
            listed = {int(identity) for identity in entry.get("attackerInstanceIds") or []}
        except (TypeError, ValueError):
            continue
        if blocker is None or not listed or _CANT_BLOCK.search(_rules_text(blocker)):
            continue
        for identity in attacker_ids:
            attacker = cards.get(identity)
            if (
                attacker is not None
                and identity not in listed
                and not _has(attacker, "menace")
                and not _UNBLOCKABLE_TEXT.search(_rules_text(attacker))
                and _can_block(attacker, blocker)
            ):
                phantoms.add(identity)
    return phantoms


_UNBLOCKABLE_TEXT = re.compile(r"can(?:'|no)t be blocked|\bunblockable\b")


def _known_body(card: dict | None) -> bool:
    return card is not None and _is_int(card.get("power")) and _is_int(card.get("toughness"))


# Replacement effects that multiply a source's damage to us. 2026-10-12 T9
# (bug_20261012_224324): Absorbing Man and Titania ("Double all damage that
# creature sources you control would deal") was out; the chump guard counted
# Railway Brawler's 11 at face value, dropped the Arboreal Grazer block ("at 21
# life we stay at 10") and the doubled 22 killed us — the block left us at 5.
_DAMAGE_MULTIPLIER = re.compile(
    r"\b(?P<all>double|triple) all damage that [^.]*?sources? (?P<all_whose>you control )?would deal"
    r"|\bif an? (?:[a-z]+ )?(?:source|creature)s? (?P<whose>you control )?would deal (?:combat )?damage "
    r"to (?P<to>[^,.]*), (?:it|that source|that creature) deals (?P<mult>double|triple) that damage"
)
_FACTORS = {"double": 2, "triple": 3}


def damage_to_us_factor(state: dict, attacker_seat, local_seat) -> int:
    """How many times over ``attacker_seat``'s creatures' damage reaches us (1 = printed power).

    Each doubler on the battlefield counts once per permanent (Arena's text
    sometimes repeats a clause), and several stack. A colour-restricted
    clause ("a red source") is counted anyway: overstating their damage only
    keeps a block.
    """
    factor = 1
    for card in state.get("battlefield") or []:
        controller = _controller(card)
        clauses = set()
        for match in _DAMAGE_MULTIPLIER.finditer(_rules_text(card)):
            whose = match.group("all_whose") or match.group("whose")
            if whose and controller != attacker_seat:
                continue  # their sources only when they control the doubler
            to = match.group("to")
            if to is not None and not (
                "player" in to
                or ("opponent" in to and controller != local_seat)
                or (re.search(r"\byou\b", to) and controller == local_seat)
            ):
                continue
            clauses.add(match.group("all") or match.group("mult"))
        for word in clauses:
            factor *= _FACTORS[word]
    return factor


def _damage_through(
    attackers: list[dict], assignments: dict[int, int], cards: dict, factors: dict | None = None
) -> int:
    """Damage reaching us, with ``factors`` (controller seat -> multiplier) applied per attacker."""
    total = 0
    for attacker in attackers:
        group = [
            cards[blocker]
            for blocker, target in assignments.items()
            if target == attacker.get("instance_id") and blocker in cards
        ]
        total += _resolve_attacker(attacker, group).damage_through * (factors or {}).get(
            _controller(attacker), 1
        )
    return total


def _damage_factors(state: dict, creatures: list[dict], local_seat) -> dict:
    """Controller seat -> damage multiplier for the controllers of ``creatures``."""
    seats = {_controller(card) for card in creatures}
    return {seat: damage_to_us_factor(state, seat, local_seat) for seat in seats}


def _affordable_block_trick(state: dict, local: dict) -> str:
    """Name of an instant/flash pump or protection trick our untapped mana pays for."""
    mana = _untapped_mana(state, local)
    for card in state.get("hand") or []:
        text = _rules_text(card)
        flash = "flash" in (card.get("keywords") or ()) or re.search(r"(?m)^\s*flash\b", text)
        if ("instant" in _types(card) or flash) and _BLOCK_TRICK.search(text):
            if _mana_value(card.get("mana_cost") or "") <= mana:
                return card.get("name") or "an instant"
    return ""


def _may_guard_planeswalker(attacker: dict, damage_with_block: int, cards: dict, local_seat) -> bool:
    """The block may be protecting one of our planeswalkers rather than our life.

    The log path does not record whom an attacker attacks, so an unknown
    target is checked against every planeswalker we control.
    """
    target = attacker.get("attack_target_id")
    if _is_int(target) and target in cards:
        return True  # attacking a permanent of ours
    if _is_int(target) and target > 0:
        return False  # attacking a player
    unblocked = _resolve_attacker(attacker, []).damage_through
    for card in cards.values():
        if _controller(card) != local_seat or "planeswalker" not in _types(card):
            continue
        remaining = loyalty(card)
        if remaining is None or damage_with_block < remaining <= unblocked:
            return True
    return False


def walker_saving_blocks(
    attackers: list[dict],
    blockers: list[dict],
    assignments: dict[int, int],
    legal: dict[int, set[int]],
    cards: dict,
    local_seat,
) -> dict[int, int]:
    """Extra blocks that keep one of our attacked planeswalkers alive, as blocker id -> attacker id.

    The block solver only weighs our life, so an attack on our walker was
    never blocked unless the planner proposed it (2026-10-18 pro game, match 1
    turn 10: Teyo chump-blocked a 5/5 Saheeli to keep Garruk, Curse Breaker
    alive). For each walker the attackers aimed at it would finish, the
    unassigned blocker that brings that damage below its loyalty is added,
    the cheapest body first, and only when the body is worth less than the
    walker (``walker_value``). Attackers whose target is unknown are left alone.
    """
    from arenamcp.combat_solver import walker_value

    added: dict[int, int] = {}
    used = set(assignments)
    by_id = {card.get("instance_id"): card for card in blockers}
    for walker in cards.values():
        if _controller(walker) != local_seat or "planeswalker" not in _types(walker):
            continue
        remaining = loyalty(walker)
        if remaining is None or remaining <= 0:
            continue
        aimed = [a for a in attackers if a.get("attack_target_id") == walker.get("instance_id")]

        def damage(plan: dict[int, int], aimed=aimed) -> int:
            return sum(
                _resolve_attacker(
                    a, [by_id[b] for b, t in plan.items() if t == a["instance_id"]]
                ).damage_through
                for a in aimed
            )

        plan = {**assignments, **added}
        if not aimed or damage(plan) < remaining:
            continue
        options = []
        for blocker_id, allowed in legal.items():
            blocker = by_id.get(blocker_id)
            if blocker is None or blocker_id in used or _material(blocker) >= walker_value(remaining):
                continue
            for attacker in aimed:
                if attacker["instance_id"] in allowed:
                    trial = {**plan, blocker_id: attacker["instance_id"]}
                    if damage(trial) < remaining:
                        options.append((_material(blocker), blocker_id, attacker["instance_id"]))
        if options:
            _cost, blocker_id, attacker_id = min(options)
            added[blocker_id] = attacker_id
            used.add(blocker_id)
    return added


def wasteful_chump_blocks(
    state: dict, assignments: dict[int, int], pending: dict | None = None
) -> dict[int, str]:
    """Planned chump blocks that buy too little, as blocker id -> reason.

    A chump block here is a single blocker that dies while its attacker
    survives. Against menace two blockers are the smallest legal block, so a
    pair whose creatures both die without killing the attacker is one chump:
    both go or both stay, never one (a lone blocker on a menace attacker is
    illegal; Arena rejects the declaration). A chump is dropped only when
    everything below holds:
      - without it we are still above CHUMP_DANGER_LIFE after this combat
        (not lethal or near-lethal), and it is not what keeps us out of
        lethal range of their next attack: every creature of theirs that can
        attack then and survives this combat, into the creatures this combat
        leaves us, each absorbing one of their biggest attackers
        (``_next_attack_damage``; #171, match 2b77f14d T9: their raw board
        power, 47 against our 19, dropped all four chumps). Dead then either
        way, it stays when they have more creatures than we do, or when the
        line search's best line lives through that attack;
      - the blocker has no death/block payoff or self-pump ability;
      - the attacker has no damage-to-player payoff (or infect/toxic) that the
        block fully stops;
      - the block cannot be what keeps one of our planeswalkers alive (the
        attacker is attacking us, or no walker's loyalty sits between the
        damage with and without the block; unknown loyalty keeps the block);
      - no instant-speed pump or protection trick in hand is affordable.
    Gang blocks, trades, blocks that kill the attacker (deathtouch, first
    strike) and anything with unknown power/toughness are always kept.
    """
    local = _local_player(state)
    if local is None or not assignments:
        return {}
    life = local.get("life_total")
    if not _is_int(life) or life <= 0:
        return {}
    local_seat = local.get("seat_id")
    cards = {card.get("instance_id"): card for card in state.get("battlefield") or []}
    attackers = _incoming_attackers(state, local_seat, _legal_block_map(state, pending))
    if not attackers or not all(_known_body(card) for card in attackers):
        return {}
    if _affordable_block_trick(state, local):
        return {}
    groups: dict[int, list[int]] = {}
    for blocker, attacker in assignments.items():
        groups.setdefault(attacker, []).append(blocker)
    # Their next attack (#171): every opposing creature that can attack then, less those this
    # combat kills, into our creatures this combat leaves (_next_attack_damage).
    theirs = [
        card
        for card in cards.values()
        if _controller(card) not in (None, local_seat)
        and "creature" in _types(card)
        and _is_int(card.get("power"))
    ]
    threats = [card for card in theirs if can_attack_next_turn(card)]
    ours = [
        card
        for card in cards.values()
        if _controller(card) == local_seat
        and "creature" in _types(card)
        and _known_body(card)
        and not _CANT_BLOCK.search(_rules_text(card))
        and not leaves_at_our_end_step(card)
    ]
    wider = len(theirs) > sum(
        1 for card in cards.values() if _controller(card) == local_seat and "creature" in _types(card)
    )
    factors = _damage_factors(state, attackers + threats, local_seat)
    through = _damage_through(attackers, assignments, cards, factors)
    line_survives: list = []  # the line search's verdict, asked once and only when needed

    def survives() -> bool | None:
        if not line_survives:
            line_survives.append(_line_search_survives_their_next_attack(state))
        return line_survives[0]

    def next_attack(plan: dict[int, int]) -> int:
        left, coming = _after_combat(attackers, plan, cards, ours, threats)
        return _next_attack_damage(coming, left, factors)

    # Candidates first, judged one at a time against the full plan; then dropped
    # cumulatively (smallest saving first) against what is still kept. 2026-10-19
    # 13:48 (match bd34f972 T9, ledger #149): two chumps were each "not needed"
    # (stay at 8 / stay at 6) and together exactly lethal at 14; both were dropped.
    candidates: list[tuple[int, int, list[int], dict, list[dict]]] = []
    for attacker_id, blockers in groups.items():
        attacker = cards.get(attacker_id)
        bodies = [cards.get(blocker) for blocker in blockers]
        unit = 2 if attacker is not None and _has(attacker, "menace") and len(blockers) == 2 else 1
        if len(blockers) != unit or not _known_body(attacker) or not all(_known_body(b) for b in bodies):
            continue
        outcome = _resolve_attacker(attacker, bodies)
        if outcome.attacker_died or any(body not in outcome.blockers_died for body in bodies):
            continue  # a trade, a kill or a blocker that survives
        if any(_BLOCKER_PAYOFF.search(_rules_text(body)) for body in bodies):
            continue
        if outcome.damage_through == 0 and _ATTACKER_HIT_PAYOFF.search(_rules_text(attacker)):
            continue
        if _may_guard_planeswalker(attacker, outcome.damage_through, cards, local_seat):
            continue
        alone = {b: a for b, a in assignments.items() if b not in blockers}
        saved_alone = _damage_through(attackers, alone, cards, factors) - through
        candidates.append((saved_alone, attacker_id, blockers, attacker, bodies))
    wasteful: dict[int, str] = {}
    kept = dict(assignments)
    kept_through = through
    for _saved_alone, _attacker_id, blockers, attacker, bodies in sorted(candidates, key=lambda c: (c[0], c[1])):
        without = {b: a for b, a in kept.items() if b not in blockers}
        through_without = _damage_through(attackers, without, cards, factors)
        life_without, life_with = life - through_without, life - kept_through
        if life_without <= CHUMP_DANGER_LIFE:
            continue  # lethal or near-lethal (with the other drops): the chump buys survival
        if _chump_for_next_attack(
            life_with,
            life_without,
            next_attack(kept),
            next_attack(without),
            wider=wider,
            line_survives=survives,
        ):
            continue  # it keeps us out of lethal range next turn, or we are dead then either way
        saved = through_without - kept_through
        kept, kept_through = without, through_without
        who = " and ".join(
            f"{body.get('name') or 'blocker'} {body['power']}/{body['toughness']}" for body in bodies
        )
        reason = (
            f"{who} would die to "
            f"{attacker.get('name') or 'the attacker'} {attacker['power']}/{attacker['toughness']} without "
            f"killing it to save {saved} damage; at {life} life we stay at {life_without} without the block"
        )
        wasteful.update(dict.fromkeys(blockers, reason))
    return wasteful


def repair_menace_blocks(
    state: dict, assignments: dict[int, int], pending: dict | None = None
) -> tuple[dict[int, int], list[str]]:
    """Make a block declaration legal against menace: never one lone blocker on a menace attacker.

    2026-10-19 07:34:34 (match cd41ad5a): Rabbit 485 alone blocked Biting-Palm
    Ninja, menace from a counter (ability grpId 142); Arena answered the
    SubmitBlockersReq with FailureReason_CombatRestrictionViolated.

    A lone block is dropped, unless dropping it lets lethal damage through:
    then the cheapest unused creature that may block that attacker joins it.
    Returns the declaration (blocker id -> attacker id) and one note per change.
    """
    cards = {card.get("instance_id"): card for card in state.get("battlefield") or []}
    declared = dict(assignments)
    counts = Counter(declared.values())
    lone = [
        attacker_id
        for attacker_id, count in counts.items()
        if count == 1 and attacker_id in cards and _has(cards[attacker_id], "menace")
    ]
    if not lone:
        return declared, []
    local = _local_player(state) or {}
    life = local.get("life_total")
    legal = _legal_block_map(state, pending) or {}
    attackers = _incoming_attackers(state, local.get("seat_id"), legal)
    known = (
        _is_int(life)
        and attackers is not None
        and all(_known_body(card) for card in attackers)
        and all(_known_body(cards.get(blocker)) for blocker in declared)
    )
    factors = _damage_factors(state, attackers or [], local.get("seat_id")) if known else {}

    def name(identity: int) -> str:
        return f"{cards.get(identity, {}).get('name') or 'creature'} [{identity}]"

    notes: list[str] = []
    for attacker_id in lone:
        blocker_id = next(blocker for blocker, target in declared.items() if target == attacker_id)
        without = {blocker: target for blocker, target in declared.items() if blocker != blocker_id}
        partners = sorted(
            (_material(cards[blocker]), blocker)
            for blocker, allowed in legal.items()
            if attacker_id in allowed and blocker not in declared and _known_body(cards.get(blocker))
        )
        if partners and known and _damage_through(attackers, without, cards, factors) >= life:
            partner = partners[0][1]
            declared[partner] = attacker_id
            notes.append(
                f"{name(partner)} joins {name(blocker_id)} on {name(attacker_id)}: it has menace, "
                "and dropping the block is lethal"
            )
            continue
        declared = without
        notes.append(
            f"not blocking {name(attacker_id)} with {name(blocker_id)} alone: it has menace "
            "and can only be blocked by two or more creatures"
        )
    return declared, notes


# The safe default searches every block assignment up to this many (about
# 1.2 us each: 4 attackers x 8 blockers, 390625, took 0.45 s), past the
# solver's 50000 default. 2026-10-19 07:34:45, 4 attackers x 7 blockers: the
# greedy fallback chumped Shark Shredder where the full search kills Dark Leo
# & Shredder, Biting-Palm Ninja and the Ninja token; it runs once per window.
SAFE_DEFAULT_BLOCK_OPTIONS = 400_000


def safe_default_blocks(state: dict, pending: dict | None = None) -> tuple[dict[int, int], str] | None:
    """A validated block declaration for a Declare Blockers request the planner could not answer.

    Returns (blocker id -> attacker id, reason). An empty mapping declares no
    blockers. Blocks come from the combat solver (survive lethal, then the
    best material exchange) minus wasteful chumps, legal against menace.
    Blockers the request already shows selected are choices like any other
    (``_legal_block_map``). None when the board cannot be checked (unknown
    life, attackers or power): no blind declaration then.
    """
    local = _local_player(state)
    if local is None:
        return None
    life = local.get("life_total")
    if not _is_int(life) or life <= 0:
        return None
    legal = _legal_block_map(state, pending)
    if legal is None:
        return None
    legal = {blocker: allowed for blocker, allowed in legal.items() if allowed}
    if not legal:
        return {}, "no creature can block any attacker"
    cards = {card.get("instance_id"): card for card in state.get("battlefield") or []}
    attackers = _incoming_attackers(state, local.get("seat_id"), legal)
    if not attackers or not all(_known_body(card) for card in attackers):
        return None
    blockers = [cards[blocker] for blocker in legal if _known_body(cards.get(blocker))]
    factors = _damage_factors(state, attackers, local.get("seat_id"))
    unblocked = _damage_through(attackers, {}, cards, factors)
    if not blockers:
        if unblocked >= life:
            return None  # only unknown blockers could save us: leave it to a human
        return {}, f"no blocker with known power and toughness; {unblocked} damage at {life} life"
    # The solver counts printed power: x-times damage is lethal at 1/x of our life.
    factor = max(factors.values(), default=1)
    plan = optimal_blocks(
        attackers,
        blockers,
        math.ceil(life / factor),
        blocker_allowed_attackers=legal,
        max_options=SAFE_DEFAULT_BLOCK_OPTIONS,
        trick_exposure=combat_solver.attacking_trick_exposure(state),
    )
    assignments = dict(plan.assignments) if plan is not None else {}
    for blocker in wasteful_chump_blocks(state, assignments, pending):
        assignments.pop(blocker, None)
    assignments.update(
        walker_saving_blocks(attackers, blockers, assignments, legal, cards, local.get("seat_id"))
    )
    # The greedy fallback (large boards) can put one blocker on a menace attacker.
    assignments = repair_menace_blocks(state, assignments, pending)[0]
    through = _damage_through(attackers, assignments, cards, factors)
    if not assignments:
        return {}, f"no blocks: {through} damage at {life} life"

    def label(identity: int) -> str:
        return cards[identity].get("name") or f"creature {identity}"

    blocks = ", ".join(f"{label(b)} blocks {label(a)}" for b, a in assignments.items())
    return assignments, f"{blocks}: {through} damage at {life} life (no-block {unblocked})"


# --- Free-block rule -------------------------------------------------------------
#
# 2026-10-19 T9 (match a74bbc90): a 1/1 Pest token and a 1/1 deathtouch Virulent
# Emissary attacked; Slickshot Show-Off 1/2 could block the Pest, kill it and
# survive, but the plan was "Preserve Slickshot as the sole wincon; absorb minor
# damage" and nothing blocked. The mirror of the free-attack rule: a block
# that kills its attacker while the blocker survives (combat solver, first
# strike and deathtouch included) is always declared.

# Combat math the solver does not model: triggers on being blocked.
_BLOCKED_PAYOFF = re.compile(
    r"becomes? blocked|blocks or becomes blocked|is blocked\b"
    r"|\b(?:bushido|rampage|flanking|afflict|provoke)\b"
)
# Their visible instant-speed ways to win the fight: a pump or protection
# ability on the battlefield (their hand is hidden, as for the free-attack rule).
_FIGHT_TRICK = re.compile(
    r"\bgets? \+(?:\d+|x)/\+(?:\d+|x)|\bgains? (?:first strike|double strike|deathtouch|indestructible)\b"
    r"|\bprevent all combat damage\b"
)


def _their_fight_tricks(state: dict, seat) -> list[dict]:
    """Their permanents with an affordable activated pump/protection ability."""
    player = next((p for p in state.get("players") or [] if p.get("seat_id") == seat), None)
    if player is None:
        return []
    mana = _untapped_mana(state, player)
    sources = []
    for card in state.get("battlefield") or []:
        if _controller(card) != seat:
            continue
        for cost, effect in _activated_abilities(card):
            if "t" in _mana_symbols(cost) and card.get("is_tapped"):
                continue
            if _FIGHT_TRICK.search(effect) and _mana_value(cost) <= mana:
                sources.append(card)
                break
    return sources


def free_blocks(state: dict, pending: dict | None = None) -> dict[int, tuple[int, str]]:
    """Blocks that kill an attacker for free, as blocker id -> (attacker id, reason).

    A free block is one legal blocker on one attacker (no menace) that kills
    it and survives, by the combat solver. Attackers with a trigger on being
    blocked, and every attacker while the attacking player has an affordable
    pump or protection ability on the battlefield, are left alone. Each
    attacker gets at most one free blocker, the most valuable attackers
    first, each taking the cheapest body that does the job.
    """
    local = _local_player(state)
    if local is None:
        return {}
    legal = _legal_block_map(state, pending)
    if not legal:
        return {}
    cards = {card.get("instance_id"): card for card in state.get("battlefield") or []}
    attackers = _incoming_attackers(state, local.get("seat_id"), legal)
    if not attackers or not all(_known_body(card) for card in attackers):
        return {}
    tricky = {_controller(card) for card in attackers if _their_fight_tricks(state, _controller(card))}
    used: set[int] = set()
    found: dict[int, tuple[int, str]] = {}
    for attacker in sorted(attackers, key=lambda card: -_material(card)):
        if (
            _has(attacker, "menace")
            or _controller(attacker) in tricky
            or _BLOCKED_PAYOFF.search(_rules_text(attacker))
        ):
            continue
        options = []
        for blocker_id, allowed in legal.items():
            blocker = cards.get(blocker_id)
            if blocker_id in used or attacker["instance_id"] not in allowed or not _known_body(blocker):
                continue
            if not _can_block(attacker, blocker):
                continue
            outcome = _resolve_attacker(attacker, [blocker])
            if outcome.attacker_died and blocker not in outcome.blockers_died:
                options.append((_material(blocker), -int(blocker["toughness"]), blocker_id, blocker))
        if not options:
            continue
        _cost, _toughness, blocker_id, blocker = min(options, key=lambda option: option[:3])
        used.add(blocker_id)
        found[blocker_id] = (
            attacker["instance_id"],
            f"{blocker.get('name') or 'blocker'} {blocker['power']}/{blocker['toughness']} blocks "
            f"{attacker.get('name') or 'attacker'} {attacker['power']}/{attacker['toughness']}, "
            "kills it and survives",
        )
    return found
