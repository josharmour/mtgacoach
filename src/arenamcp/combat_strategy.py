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
# Our other permanents: anything that pays off creatures attacking, blocking
# restrictions, or creatures dying.
_SIDE_PAYOFF = re.compile(
    r"\battack(?:s|ing|ed|ers?)?\b|\bblock(?:s|ed|ing|ers?)?\b|\b(?:exalted|battle cry|myriad|melee)\b"
    r"|\bwhenever (?:a|an|another|one or more|each)\b[^.]*?\b(?:dies|die)\b"
    r"|put into a graveyard from the battlefield"
)
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


def _activated_costs(card: dict) -> list[str]:
    """Costs of non-mana, non-loyalty activated abilities."""
    costs = []
    for line in _rules_text(card).splitlines():
        line = line.strip()
        if ":" not in line or re.match(r"(?:when|whenever|at the beginning|if|as long as)\b", line):
            continue
        cost, effect = line.split(":", 1)
        if re.match(r"\s*[+\-−–]?\s*(?:\d+|x)\s*$", cost):
            continue  # planeswalker loyalty ability: sorcery speed
        if re.search(r"\badd\b[^.]*?(?:\{|\bmana\b)", effect):
            continue  # mana ability
        costs.append(cost)
    return costs


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


def _possible_tricks(state: dict, local: dict) -> list[tuple[str, int]]:
    """Instant-speed plays visible to us now, as (source, mana value); 0 means free."""
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
        tricks.append((card.get("name") or "an instant", cost))
    for card in state.get("graveyard") or []:
        if card.get("owner_seat_id", seat) != seat or "instant" not in _types(card):
            continue
        text = _rules_text(card)
        if re.search(r"\b(?:flashback|jump-start|retrace|escape)\b", text):
            flashback = re.search(r"flashback\s*\S?\s*((?:\{[^}]*\})+)", text)
            cost = flashback.group(1) if flashback else card.get("mana_cost") or "{1}"
            tricks.append((card.get("name") or "a graveyard instant", max(1, _mana_value(cost))))
    for card in state.get("battlefield") or []:
        if _controller(card) != seat or "planeswalker" in _types(card):
            continue
        name = card.get("name") or "a permanent"
        if re.search(r"\bprepared\b", _rules_text(card)):
            tricks.append((name, 1))  # the prepared spell's cost is not on the creature
        for cost in _activated_costs(card):
            if "t" in _mana_symbols(cost) and card.get("is_tapped"):
                continue
            tricks.append((name, _mana_value(cost)))
    return tricks


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
            or "creature" not in _types(card)
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


def _answers(attacker: dict, group: list[dict]) -> bool:
    """The group can block the attacker, kill it, lose nothing, and let nothing through."""
    if not group or (_has(attacker, "menace") and len(group) < 2):
        return False
    if not all(_can_block(attacker, blocker) for blocker in group):
        return False
    outcome = _resolve_attacker(attacker, group)
    return outcome.attacker_died and not outcome.blockers_died and outcome.damage_through == 0


def _full_answer(attackers: list[dict], blockers: list[dict]) -> dict[int, list[dict]] | None:
    """Blocks answering EVERY planned attacker at once, or None.

    One blocker blocks one attacker. When some attacker cannot be answered
    while the rest are (too few blockers, an evasive or large attacker), the
    attack applies pressure the opponent cannot absorb for free.
    """
    options = [
        [None, *(index for index, attacker in enumerate(attackers) if _can_block(attacker, blocker))]
        for blocker in blockers
    ]
    combinations = 1
    for choice in options:
        combinations *= len(choice)
    if combinations > _ANSWER_SEARCH_LIMIT:
        return None
    for selection in product(*options):
        groups: dict[int, list[dict]] = {}
        for blocker, index in zip(blockers, selection, strict=True):
            if index is not None:
                groups.setdefault(index, []).append(blocker)
        if len(groups) == len(attackers) and all(
            _answers(attacker, groups.get(index, [])) for index, attacker in enumerate(attackers)
        ):
            return groups
    return None


def losing_attackers(state: dict, attacker_ids: list[int], pending: dict | None = None) -> dict[int, str]:
    """Planned attackers that only feed an untapped blocker, with the reason.

    An attacker is dropped only when ALL of these hold:
      - the opponent's untapped creatures can block every planned attacker at
        once so that each attacker dies, no blocker dies and no damage gets
        through (so the attack cannot be lethal and is not pressure the
        opponent must absorb), with flying/reach, menace, first strike,
        deathtouch, trample and indestructible modelled by the combat solver;
      - it is not required to attack (GRE ``mustAttack`` in the state or the
        live ``pending`` bridge request);
      - its rules text names no attack/block/death payoff, evasion, or
        damage-to-creature value;
      - none of our other permanents rewards attacking, restricts blocking or
        pays off creatures dying, and no raid/morbid card waits in hand;
      - no spell we resolved this turn mentions blocking;
      - no instant, flash card, instant flashback, prepared spell or
        activated ability we control is affordable with our untapped mana
        (free ones always count; colors are ignored, erring toward keeping).
    Unknown power/toughness or anything ambiguous keeps the attack.
    """
    players = state.get("players") or []
    local = next((player for player in players if player.get("is_local")), None)
    opponent = next((player for player in players if not player.get("is_local")), None)
    if local is None or opponent is None or not attacker_ids:
        return {}
    cards = {card.get("instance_id"): card for card in state.get("battlefield") or []}
    attackers = [cards.get(identity) for identity in attacker_ids]
    if any(
        card is None
        or _controller(card) != local.get("seat_id")
        or not _is_int(card.get("power"))
        or not _is_int(card.get("toughness"))
        for card in attackers
    ):
        return {}
    for card in state.get("battlefield") or []:
        if (
            _controller(card) == local.get("seat_id")
            and card.get("instance_id") not in attacker_ids
            and _SIDE_PAYOFF.search(_rules_text(card))
        ):
            return {}
    if any(_HAND_PAYOFF.search(_rules_text(card)) for card in state.get("hand") or []):
        return {}
    if any("block" in _rules_text(card) for card in _resolved_this_turn(state)):
        return {}
    mana = _untapped_mana(state, local)
    if any(cost <= mana for _source, cost in _possible_tricks(state, local)):
        return {}

    blockers = _eligible_blockers(state, opponent.get("seat_id"))
    answer = _full_answer(attackers, blockers)
    if answer is None:
        return {}
    forced = {
        int(entry.get("attackerInstanceId") or 0)
        for entry in [
            *(attack_candidates(state) or []),
            *(attack_candidates(state, pending) or [] if pending else []),
        ]
        if entry.get("mustAttack")
    }
    losing = {}
    for index, attacker in enumerate(attackers):
        identity = attacker["instance_id"]
        if identity in forced or _ATTACKER_PAYOFF.search(_rules_text(attacker)):
            continue
        group = answer[index]
        blocked_by = " and ".join(
            f"{card.get('name') or 'a creature'} {card['power']}/{card['toughness']}" for card in group
        )
        reason = (
            f"{blocked_by} can block {attacker.get('name') or identity} "
            f"{attacker['power']}/{attacker['toughness']}, kill it and survive; nothing gets through, "
            "it has no attack or death payoff, and no trick is affordable with our untapped mana"
        )
        if len(attackers) > 1:
            reason += "; the opponent can block every planned attacker this way"
        losing[identity] = reason
    return losing
