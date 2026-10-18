"""Bounded, Oracle-derived commander recovery estimates for combat search.

This is a resource forecast, not a Magic interpreter. Only recognized entry
effects and unconditional tap-for-mana abilities earn a recovery credit. The
forecast never makes a spell legal and does not issue actions. Unrecognized
costs, modifiers and effects stay with the tactical planner.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from arenamcp.combat_solver import _material, _resolve_attacker, blocker_allowed_attackers_map
from arenamcp.library_counts import observed_library_count
from arenamcp.rules_engine import _normalize_mana_symbols

_NUMBERS = {word: n for n, word in enumerate(("zero", "one", "two", "three", "four", "five", "six"))}
_COUNT = r"(?:a|an|one|two|three|four|five|six|[1-6])"
_COLORS = "WUBRGC"


def _number(text: str) -> int:
    return int(text) if text.isdigit() else _NUMBERS.get(text, 1)


def _text(card: dict) -> str:
    return _normalize_mana_symbols(re.sub(r"<[^>]+>", "", card.get("oracle_text") or ""))


def _creature(card: dict) -> bool:
    return "creature" in str(card.get("type_line", "")).lower()


@dataclass(frozen=True)
class EntryResources:
    copies: int = 0
    # (count, power, toughness, type, keywords)
    tokens: tuple[tuple[int, int, int, str, str], ...] = ()
    cards: int = 0
    evidence: tuple[str, ...] = ()


@lru_cache(maxsize=2048)
def _entry_resources(name: str, oracle: str) -> EntryResources:
    copies = cards = 0
    tokens = []
    evidence = []
    # Source names are matched as data; no named-card policies.
    subject = rf"(?:{re.escape(name)}|this creature|~)"
    for line in oracle.splitlines():
        trigger = re.fullmatch(rf"When {subject} enters?(?: the battlefield)?, (.+)", line.strip(), re.I)
        if not trigger:
            continue
        effect = trigger[1]
        guarded = re.match(r"if (?:it(?:'s| is)|they(?:'re| are)) not a token, (.+)", effect, re.I)
        effect = guarded[1] if guarded else effect
        copy = re.fullmatch(
            rf"create ({_COUNT}) tokens? that (?:are|is) (?:a )?cop(?:y|ies) of "
            rf"(?:it|them|{re.escape(name)}), except (?:the tokens?|they) "
            r"(?:aren't|isn't|are not|is not) legendary\.",
            effect,
            re.I,
        )
        # A self-copy ETB without this guard could recursively trigger.
        if copy and guarded:
            copies += _number(copy[1].lower())
            evidence.append(line)
            continue
        token = re.fullmatch(
            rf"create ({_COUNT}) (\d+)/(\d+) ([\w -]+) creature tokens?"
            r"(?: with (flying|vigilance|reach|haste|lifelink))?\.",
            effect,
            re.I,
        )
        draw = re.fullmatch(rf"draw ({_COUNT}) cards?\.", effect, re.I)
        if token:
            tokens.append((_number(token[1].lower()), int(token[2]), int(token[3]), token[4], token[5] or ""))
            evidence.append(line)
        elif draw:
            cards += _number(draw[1].lower())
            evidence.append(line)
        else:
            # Do not credit one positive entry trigger while dropping an
            # unmodeled condition, sacrifice, life payment or other trigger.
            return EntryResources()
    return EntryResources(copies, tuple(tokens), cards, tuple(evidence))


def entry_resources(card: dict) -> EntryResources:
    return _entry_resources(card.get("name") or "~", _text(card))


def _has_type(card: dict, kind: str) -> bool:
    # Oracle uses singular subtypes in "for each Elf you control".
    return kind.lower() in re.findall(r"[\w]+", str(card.get("type_line", "")).lower())


_CHOSEN_COLOR = re.compile(r"^\{T\}: Choose a colou?r\. Add one mana of that colou?r\b", re.I)


def _power(card: dict) -> int | None:
    try:
        return int(card.get("power"))
    except (TypeError, ValueError):
        return None


def _power_factor(kind: str, card: dict, survivors: list[dict]) -> int:
    """Mana scaled by our creatures' powers: "for each different power among creatures you
    control" (Loot, the Nexus) or "equal to the greatest power among creatures you control"."""
    powers = [p for c in survivors if _creature(c) and (p := _power(c)) is not None]
    if card not in survivors and _creature(card) and _power(card) is not None:
        powers.append(_power(card))
    if kind == "different":
        return len(set(powers))
    return max([p for p in powers if p > 0], default=0)


def mana_options(card: dict, survivors: list[dict]) -> tuple[tuple[int, ...], ...]:
    """One activation per surviving source after the next ordinary untap.

    Fixed multimana and per-type scaling retain their quantities. Alternatives
    on one source are exclusive. Costly/restricted/conditional activations are
    omitted, so this is a supported lower bound, not total possible mana.
    Mana scaled by our creatures' powers counts the bodies in ``survivors``:
    "Choose a color. Add one mana of that color for each different power among
    creatures you control" (Loot, the Nexus; pro YFRA game 2026-10-18) and
    "Add an amount of {G} equal to the greatest power among creatures you control".
    """
    oracle = _text(card)
    if (
        card.get("is_phased_out")
        or any("stun" in str(k).lower() and v for k, v in (card.get("counters") or {}).items())
        or re.search(r"(?:doesn't|don't|cannot|can't) untap|loses? all abilities", oracle, re.I)
        or re.search(r"(?m)^(?:Spend (?:this|that) mana only|Activate only)", oracle, re.I)
    ):
        return ()
    outputs = set()
    for line in oracle.splitlines():
        line = line.strip().strip("()")
        line = _CHOSEN_COLOR.sub("{T}: Add one mana of any color", line)
        match = re.fullmatch(r"\{T\}: Add (.+?)\.?", line, re.I)
        if not match:
            continue
        output = match[1].rstrip(".")
        factor = 1
        by_power = re.fullmatch(
            r"(.+?) for each different power among creatures you control", output, re.I
        ) or re.fullmatch(
            r"an amount of ((?:\{[WUBRGC]\})) equal to the greatest power among creatures you control",
            output,
            re.I,
        )
        scaling = None
        if not by_power:
            scaling = re.fullmatch(r"(.+) for each (other )?(\w+) you control", output, re.I)
        if by_power:
            output = by_power[1]
            kind = "different" if "different" in match[1].lower() else "greatest"
            factor = _power_factor(kind, card, survivors)
            if not factor:
                continue
        if scaling:
            output = scaling[1]
            factor = sum(
                _has_type(other, scaling[3]) for other in survivors if not scaling[2] or other is not card
            )
        if output.lower() == "one mana of any color":
            choices = [f"{{{color}}}" for color in "WUBRG"]
        else:
            choices = re.split(r",? or |, ", output)
        if any(not re.fullmatch(r"(?:\{[WUBRGC]\})+", choice) for choice in choices):
            continue
        for choice in choices:
            outputs.add(tuple(choice.count(f"{{{color}}}") * factor for color in _COLORS))
    return tuple(sorted(outputs))


def _cost(card: dict, casts: object) -> tuple[int, tuple[int, ...]] | None:
    if not isinstance(casts, int) or isinstance(casts, bool) or casts < 0:
        return None
    cost = _normalize_mana_symbols(card.get("mana_cost") or "")
    if not re.fullmatch(r"(?:\{(?:\d+|[WUBRGC])\})+", cost):
        return None
    symbols = re.findall(r"\{([^}]+)\}", cost)
    generic = 2 * casts + sum(int(symbol) for symbol in symbols if symbol.isdigit())
    pips = tuple(symbols.count(color) for color in _COLORS)
    return generic + sum(pips), pips


def _payable(sources: list[tuple[tuple[int, ...], ...]], cost: tuple[int, tuple[int, ...]]) -> bool:
    total, pips = cost
    # Track required colored pips plus total; cap excess to bound the state
    # space. A dual land is one choice, never two simultaneous mana sources.
    possibilities = {(0, (0,) * 6)}
    for options in sources:
        if not options:
            continue
        possibilities = {
            (
                min(total, have + sum(option)),
                tuple(min(need, old + new) for need, old, new in zip(pips, colors, option, strict=True)),
            )
            for have, colors in possibilities
            for option in options
        }
        if (total, pips) in possibilities:
            return True
        if len(possibilities) > 4096:
            return False  # unsupported expensive search, never optimistic
    return False


def _mana_text(sources: list[tuple[tuple[int, ...], ...]]) -> str:
    total = sum(max(map(sum, options), default=0) for options in sources)
    return str(total)


@dataclass
class Recovery:
    credit: float
    explanation: str
    commander_id: int
    surviving_ids: frozenset[int]
    payable: bool


class CombatRecovery:
    """Per-search forecasts keyed by the complete set of combat deaths."""

    def __init__(self, state: dict):
        local = next((p for p in state.get("players", []) if p.get("is_local")), {})
        seat = local.get("seat_id") or state.get("local_seat_id")
        ids = set(local.get("commander_ids") or [])
        self.own = [
            c
            for c in state.get("battlefield", [])
            if (c.get("controller_seat_id") or c.get("owner_seat_id")) == seat
        ]
        self.commanders = [
            c
            for c in self.own
            if c.get("instance_id") in ids
            and c.get("owner_seat_id") == seat
            and not c.get("is_token")
            and "token" not in str(c.get("object_kind", "")).lower()
        ]
        self.casts = state.get("commander_casts") or {}
        self.catalog = state.get("deck_catalog") or {}
        self.library_count = observed_library_count(state)
        self.attached = {
            c.get("attached_to_id") or c.get("parent_instance_id")
            for c in state.get("battlefield", [])
            if "aura" in str(c.get("type_line", "")).lower()
            or "equipment" in str(c.get("type_line", "")).lower()
        }
        self.cache: dict[frozenset[int], tuple[Recovery, ...]] = {}
        # Visible static modifiers can invalidate both the payment and payoff.
        # Decline numerical credit rather than claim to implement their rules.
        all_rules = "\n".join(_text(c) for c in state.get("battlefield", []))
        self.obstructed = bool(
            re.search(
                r"(?:can't|cannot) cast|spells?[^.\n]*cost[^.\n]*more|"
                r"(?:don't|doesn't|can't|cannot) (?:cause|trigger|untap|be activated)|"
                r"(?:lose|loses) all abilities|if[^.\n]*(?:tokens?|mana)[^.\n]*instead|"
                r"(?:whenever|when)[^.\n]*dies|(?:would|if)[^.\n]*die[^.\n]*instead",
                all_rules,
                re.I,
            )
        ) or bool(state.get("stack"))

    def forecasts(self, dead_ids: frozenset[int]) -> tuple[Recovery, ...]:
        if dead_ids in self.cache:
            return self.cache[dead_ids]
        result = []
        # Multiple simultaneous commanders need a joint payment/sequence
        # search; do not spend the same surviving mana on both forecasts.
        dying = [c for c in self.commanders if c["instance_id"] in dead_ids]
        if len(dying) != 1 or self.obstructed:
            self.cache[dead_ids] = ()
            return ()
        card = dying[0]
        effect = entry_resources(card)
        if not effect.evidence:
            self.cache[dead_ids] = ()
            return ()
        gid = card.get("grp_id")
        cost = _cost(card, self.casts.get(gid, self.casts.get(str(gid))))
        survivors = [c for c in self.own if c.get("instance_id") not in dead_ids]
        sources = [mana_options(c, survivors) for c in survivors]
        affordable = cost is not None and _payable(sources, cost)
        count = sum(_creature(c) for c in survivors)
        # Counters, equipment and altered characteristics are investments, not
        # printed characteristics. Do not restore them on a fresh instance.
        invested = (
            bool(card.get("counters"))
            or bool(card.get("attachments"))
            or bool(card.get("is_copy"))
            or card["instance_id"] in self.attached
        )
        printed = self.catalog.get(gid, self.catalog.get(str(gid), {}))
        try:
            base_power, base_toughness = int(printed["power"]), int(printed["toughness"])
            known_stats = True
        except (KeyError, ValueError, TypeError):
            base_power = base_toughness = 0
            known_stats = False
        enough_library = not effect.cards or (
            isinstance(self.library_count, int) and self.library_count >= effect.cards
        )
        fresh = {**card, "power": base_power, "toughness": base_toughness, "counters": {}, "is_tapped": False}
        new = [fresh] + [{**fresh, "is_token": True} for _ in range(effect.copies)]
        for number, power, toughness, kind, keywords in effect.tokens:
            new.extend(
                {
                    "power": power,
                    "toughness": toughness,
                    "type_line": f"Creature — {kind}",
                    "oracle_text": keywords,
                }
                for _ in range(number)
            )
        after = survivors + new
        future_sources = [mana_options(c, after) for c in after]
        total_cost = cost[0] if cost else "UNKNOWN"
        # Same material units as the combat solver. Discount delayed material
        # and charge for the full cast, rather than treating recovery as free.
        credit = (
            max(0.0, 0.75 * (sum(_material(c) for c in new) + 4 * effect.cards) - cost[0])
            if affordable and not invested and known_stats and enough_library
            else 0.0
        )
        explanation = (
            f"Recovery [id:{card['instance_id']}]: {count} surviving creatures; "
            f"supported mana after untap = {_mana_text(sources)} (excludes ALL combat deaths). "
            f"Recast cost including observed tax = {total_cost}; "
            f"payment {'supported' if affordable else 'NOT established'} including colored pips. "
            f"If returned to command zone, recast and entry resolve: {count} survivors + "
            f"1 original + {len(new) - 1} NEW tokens = {count + len(new)} creatures; "
            f"draw {effect.cards}. Mana capacity once all are ready = {_mana_text(future_sources)}. "
            "New creatures cannot tap immediately; the recast spends mana that could fund another play. "
            + (
                "Existing counters/attachments/changed identity are unpriced; NO recovery credit. "
                if invested
                else ""
            )
            + ("Printed stats unavailable; NO recovery credit. " if not known_stats else "")
            + (
                "Library cannot establish safe entry draws; NO recovery credit. "
                if not enough_library
                else ""
            )
            + "Assumes next ordinary untap and no intervening disruption; verify hand, triggers and timing."
        )
        result.append(
            Recovery(
                credit,
                explanation,
                card["instance_id"],
                frozenset(c.get("instance_id") for c in survivors),
                affordable,
            )
        )
        self.cache[dead_ids] = tuple(result)
        return self.cache[dead_ids]

    def credit(self, dead_ids: frozenset[int]) -> float:
        return sum(f.credit for f in self.forecasts(dead_ids))


_SELF_DEATH_RE = re.compile(
    r"^When (?:this (?:creature|permanent)|~)[^.\n]*(?:dies|leaves|is put into)", re.I
)
_IDENTITY_OBSTRUCTION_RE = re.compile(
    r"(?:nontoken|non-token|legendary|commander|token)[^.\n]*(?:dies|die|leaves|put into|graveyard)"
    r"|(?:dies|die|leaves)[^.\n]*(?:nontoken|non-token|legendary|commander|token)",
    re.I,
)


def _token(card: dict) -> bool:
    return bool(card.get("is_token")) or "token" in str(card.get("object_kind", "")).lower()


def _label(card: dict) -> str:
    return f"{'*' if _token(card) else ''}{card.get('name', 'Creature')} [id:{card.get('instance_id')}]"


def _identity_obstructed(state: dict) -> bool:
    """Any visible rule that tells a token's death from a nontoken/legendary/commander's."""
    for card in state.get("battlefield", []):
        name = re.escape(card.get("name") or "~")
        for line in _text(card).splitlines():
            line = line.strip()
            if _SELF_DEATH_RE.match(line) or re.match(rf"^When {name}[^.\n]*(?:dies|leaves)", line, re.I):
                continue  # a card's own death trigger does not tell our copies apart
            if _IDENTITY_OBSTRUCTION_RE.search(line):
                return True
    return False


def _identical_copy(commander: dict, copy: dict, attached: set) -> bool:
    """A token copy of the commander with the same printed body and no investment on either."""
    if not _token(copy) or _token(commander) or commander.get("is_copy"):
        return False
    grp = commander.get("grp_id")
    if not grp or copy.get("grp_id") != grp:
        return False
    for card in (commander, copy):
        if any(v for v in (card.get("counters") or {}).values()) or card.get("attachments"):
            return False
        if card.get("instance_id") in attached:
            return False
    if (commander.get("power"), commander.get("toughness")) != (copy.get("power"), copy.get("toughness")):
        return False
    return _text(commander) == _text(copy) or not (commander.get("oracle_text") and copy.get("oracle_text"))


def improve_block_recovery(
    state: dict, context: dict, assignments: dict[int, int]
) -> tuple[dict[int, int], str] | None:
    """Compare the planner's single-block trades with commander recovery.

    Keep its combat intent: same attackers blocked, damage through, attackers
    killed and number of bodies lost. Two swaps are supported:

    1. Identical copy (2026-10-11 Notary Hobbits game, turn 9): the planner
       chump/trades a TOKEN COPY of our commander while the commander itself
       could make the same block with the same result. The surviving board is
       identical either way, but a dead token is gone for good while the
       commander returns to the command zone with an optional, taxed recast
       that replays its entry trigger. No pricing is needed and cards in hand
       do not change the comparison, so this swap applies regardless of hand.
    2. Priced recovery: a losing blocker is replaced by the commander when
       the forecast recast is affordable and the continuation scores
       materially better. Unknown/occupied hands leave that opportunity-cost
       decision to the planner.

    Nothing here keys on a name or subtype; token identity comes from Arena.
    """
    if not assignments or state.get("stack"):
        return None
    recovery = CombatRecovery(state)
    if len(recovery.commanders) != 1:
        return None
    allowed = blocker_allowed_attackers_map(context.get("raw_blockers") or [])
    cards = {c.get("instance_id"): c for c in state.get("battlefield", [])}
    own_ids = {c.get("instance_id") for c in recovery.own}
    commander = recovery.commanders[0]
    cid = commander["instance_id"]
    if cid in assignments or cid not in allowed:
        return None
    attackers = [c for c in cards.values() if c.get("is_attacking") and c.get("instance_id") not in own_ids]
    if not attackers:
        return None
    # Unsupported stats and incomplete GRE legality must not authorize a swap.
    if any(
        not isinstance(c.get(k), (int, float))
        for c in attackers + [commander] + [cards.get(i, {}) for i in assignments]
        for k in ("power", "toughness")
    ):
        return None
    attack_ids = {c["instance_id"] for c in attackers}
    if any(
        b not in own_ids or a not in attack_ids or a not in allowed.get(b, set())
        for b, a in assignments.items()
    ):
        return None

    def outcome(plan):
        dead = set()
        killed = set()
        damage = 0
        for attacker in attackers:
            blocks = [cards[b] for b, a in plan.items() if a == attacker["instance_id"]]
            fight = _resolve_attacker(attacker, blocks)
            damage += fight.damage_through
            dead.update(c["instance_id"] for c in fight.blockers_died)
            if fight.attacker_died:
                killed.add(attacker["instance_id"])
        dead = frozenset(dead)
        value = recovery.credit(dead) - sum(_material(cards[i]) for i in dead)
        return (damage, killed, len(dead)), dead, value

    original_outcome, original_dead, original_value = outcome(assignments)
    local = next((p for p in state.get("players", []) if p.get("is_local")), {})
    if original_outcome[0] >= local.get("life_total", 0):
        return None

    def swappable(blocker, attacker):
        if blocker not in original_dead or attacker not in allowed[cid]:
            return False
        return (
            list(assignments.values()).count(attacker) == 1
        )  # damage order and multiblocks stay with the planner

    # 1. Identical copy: same board after combat, plus a recoverable commander.
    if not _identity_obstructed(state):
        for blocker, attacker in assignments.items():
            copy = cards.get(blocker, {})
            if not swappable(blocker, attacker) or not _identical_copy(commander, copy, recovery.attached):
                continue
            candidate = {b: a for b, a in assignments.items() if b != blocker}
            candidate[cid] = attacker
            candidate_outcome, dead, _value = outcome(candidate)
            if candidate_outcome != original_outcome or cid not in dead:
                continue
            gid = commander.get("grp_id")
            cost = _cost(commander, recovery.casts.get(gid, recovery.casts.get(str(gid))))
            recast = (
                f"{cost[0]} mana including observed commander tax"
                if cost
                else f"printed {commander.get('mana_cost') or 'UNKNOWN'} plus UNKNOWN commander tax"
            )
            explanation = (
                f"Identical copy: {_label(commander)} blocks {_label(cards[attacker])} instead of its token copy "
                f"{_label(copy)}. Same combat result ({original_outcome[0]} damage through, {original_outcome[2]} "
                "body lost) and the same surviving board, but the commander returns to the command zone where "
                f"an optional later cast ({recast}) replays its entry trigger; a token copy that dies is gone for good."
            )
            return candidate, explanation

    # 2. Priced recovery against a different body.
    if state.get("hand") != [] or recovery.obstructed:
        return None
    best = None
    best_value = original_value + 3  # require a meaningful gain, not a tie
    for blocker, attacker in assignments.items():
        if not swappable(blocker, attacker):
            continue
        candidate = {b: a for b, a in assignments.items() if b != blocker}
        candidate[cid] = attacker
        candidate_outcome, dead, value = outcome(candidate)
        if candidate_outcome != original_outcome or cid not in dead or value <= best_value:
            continue
        forecasts = recovery.forecasts(dead)
        if not forecasts or forecasts[0].credit <= 0:
            continue
        best_value = value
        best = (candidate, forecasts[0].explanation)
    return best
