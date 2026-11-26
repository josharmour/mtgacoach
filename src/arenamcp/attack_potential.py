"""What their attack can add beyond the printed board (ledger #191, #193, #194).

Three effects the incoming-damage math never counted, read from rules text:

* **Instant-speed pumps of their own creatures (#193).** Match 62d61e75 T11
  (2026-10-19 16:56:39, Player.log gsid 298): three Captain Lannery Storms
  ("Whenever you sacrifice a Treasure, Captain Lannery Storm gets +1/+0 until
  end of turn", 2/2, two of them Delina's non-legendary copies doubled by
  Isshin) attacked with Isshin 3/4 and Delina 3/2. The chump guard counted
  3+2+2+2 = 9 and dropped Rabbit->Isshin ("at 19 life we stay at 10"); after
  blocks they sacrificed their four Treasures, each Lannery got +4, and
  3+6+6+6 = 21 killed us from 19. A Treasure is sacrificed for free at
  instant speed, so every such creature's power is its power plus N per
  Treasure they control; a mana-cost pump ("{R}: +1/+0") is bounded by their
  open mana (``combat_strategy._pump_activations``).
* **Attack-trigger team grants (#194).** Harbin, Vanguard Aviator: "Whenever
  you attack with five or more Soldiers, creatures you control get +1/+1 and
  gain flying until end of turn" (match 3ac9d511): the projected attack gets
  the +X/+Y and the keywords when the attackers meet the condition (a count
  of the named type among them; an unreadable condition counts as met).
* **Extra combat phases (#191).** Aurelia, the Warleader: "Whenever Aurelia
  attacks for the first time each turn, untap all creatures you control.
  After this phase, there is an additional combat phase" (match f80ca0cf T13:
  five chumps in the first combat, 7 -> 4, and the second combat killed us).
  An attack with such a creature is followed by a second attack of its
  survivors into the blockers the first one leaves.

``annotate`` marks a side's solver bodies (``board_assessment._body``) with
what they can add (``_atk_pump``, ``_atk_grants``, ``_atk_extra``);
``attack_round`` plays one attack with those effects and its extra combats
(``board_assessment._attack_round_life`` and the line search's attack call it
for annotated bodies only: our own creatures are never annotated). For the
attack being declared now, grants are already in the snapshot's power and
toughness (the trigger resolved) and are not added again; an extra combat
follows only while the evidence says this is the first combat (the untap
trigger has untapped their attacking creatures).

``pumped_attackers`` / ``unpriced_pumps`` / ``extra_combats_now`` are the
raw-card versions the block guards use.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from arenamcp.combat_keywords import has_combat_keyword
from arenamcp.combat_strategy import (
    _activated_abilities,
    _controller,
    _mana_symbols,
    _mana_value,
    _pump_activations,
    _pump_only,
    _rules_text,
    _types,
    loyalty,
)

_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10,
}  # fmt: skip

# --- instant-speed pumps (#193) ---------------------------------------------------------

# "Whenever you sacrifice a Treasure, Captain Lannery Storm gets +1/+0 until end of turn."
_SAC_PUMP = re.compile(
    r"^whenever you sacrifice (?:a|an|one or more|another) (?P<kind>[a-z ]+?), "
    r"(?P<who>[^,.]+?) gets? \+(?P<p>\d+)/\+(?P<t>\d+) until end of turn\.?$"
)
# Any other trigger that pumps on a sacrifice: unpriced (keep the block).
_SAC_TRIGGER = re.compile(r"^whenever you sacrifice\b[^.]*\bgets? \+")
# The tokens that sacrifice themselves, their mana per sacrifice and whether {T} is part of it.
_SELF_SAC = {"treasure": (0, True), "food": (2, True), "clue": (2, False), "blood": (1, True)}
_KIND_WORDS = re.compile(r"[a-z]+")
_PUMP_TEXT = re.compile(r"\bgets? \+\d+/\+\d+|\bget \+\d+/\+\d+")
_NON_MANA_COST = re.compile(r"\b(?:sacrifice|discard|exile|pay|remove|return|tap an?|untap)\b")
_MANA_ABILITY = re.compile(r"\badd\b[^.]*?(?:\{|\bmana\b)")


@dataclass(frozen=True)
class Pump:
    """One way a creature of theirs grows at instant speed: +``power``/+``toughness`` ``times`` times."""

    power: int
    toughness: int
    times: int
    why: str


@lru_cache(maxsize=4096)
def _lines(oracle: str) -> tuple[str, ...]:
    """Rules-text lines (``combat_strategy._rules_text``: lower case, no reminders), cached by text."""
    return tuple(line.strip() for line in _rules_text({"oracle_text": oracle}).splitlines() if line.strip())


def _subtypes(card: dict) -> set[str]:
    words = set()
    for value in card.get("subtypes") or ():
        words.add(str(value).lower().replace("subtype_", ""))
    line = str(card.get("type_line") or "").lower()
    for part in re.split(r"[—–-]", line)[1:]:
        words.update(_KIND_WORDS.findall(part))
    return words


_IRREGULAR = {"dwarves": "dwarf", "elves": "elf", "wolves": "wolf", "mice": "mouse", "fungi": "fungus"}


def _singular(word: str) -> str:
    word = word.lower().strip()
    if word in _IRREGULAR:
        return _IRREGULAR[word]
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def has_kind(card: dict, kind: str) -> bool:
    """Whether ``card`` is of creature/artifact type ``kind`` ("soldiers", "treasure")."""
    words = _subtypes(card) | set(_KIND_WORDS.findall(_types(card)))
    return _singular(kind) in {_singular(word) for word in words}


def _card_id(body: dict) -> Any:
    """The battlefield instance id behind a solver body (its copies get fresh ids)."""
    return (body.get("_card") or body).get("instance_id")


def _self_names(name: str) -> frozenset[str]:
    name = name.lower()
    names = {"this creature", "this permanent", "~", "cardname", "it", "this token"}
    if name:
        names |= {name, name.split(",")[0].strip(), name.split(" ")[0]}
    return frozenset(names)


def side_mana(battlefield: list[dict], seat: Any, *, untapped_only: bool) -> int:
    """Mana a side's permanents make: lands and mana abilities (Treasures included), one each."""
    total = 0
    for card in battlefield:
        if not isinstance(card, dict) or _controller(card) != seat:
            continue
        if untapped_only and card.get("is_tapped"):
            continue
        if "land" in _types(card) or _MANA_ABILITY.search((card.get("oracle_text") or "").lower()):
            total += 1
    return total


_TEAM = re.compile(
    r"^(?P<other>other )?creatures you control$|^each (?P<other2>other )?creature you control$"
)
# "Whenever Captain Lannery Storm attacks, create a Treasure token." (Goblin Shaman's too)
_ATTACK_TREASURE = re.compile(
    r"^whenever (?P<who>[^,.]+?) attacks, create (?P<n>a|an|one|two|three|\d+) treasure tokens?\.?$"
)


@lru_cache(maxsize=4096)
def _sac_clauses(name: str, oracle: str) -> tuple[tuple[str, str, int, int, str], ...]:
    """Each "whenever you sacrifice ..." pump as (kind, scope, power, toughness, line).

    Scope: "self" (the card itself), "team" / "others" (every creature of its
    controller / the others), "unpriced" (a sacrifice pump we can't read).
    """
    selves = _self_names(name)
    out = []
    for line in _lines(oracle):
        match = _SAC_PUMP.match(line)
        if match is None:
            if _SAC_TRIGGER.match(line):
                out.append(("", "unpriced", 0, 0, line))
            continue
        who = match.group("who").strip()
        team = _TEAM.match(who)
        if who in selves:
            scope = "self"
        elif team is not None:
            scope = "others" if team.group("other") or team.group("other2") else "team"
        else:
            scope = "unpriced"
        out.append((match.group("kind"), scope, int(match.group("p")), int(match.group("t")), line))
    return tuple(out)


@lru_cache(maxsize=4096)
def _mana_pump_clauses(oracle: str) -> tuple[tuple[str, str, tuple[int, int, bool] | None], ...]:
    """Each activated ability whose effect pumps, as (cost, effect, ``_pump_only`` of the effect)."""
    out = []
    for cost, effect in _activated_abilities({"oracle_text": oracle}):
        if _PUMP_TEXT.search(effect):
            out.append((cost, effect.strip(), _pump_only(effect)))
    return tuple(out)


@lru_cache(maxsize=4096)
def _attack_treasures(name: str, oracle: str) -> int:
    """Treasures its own attack trigger makes ("whenever ~ attacks, create a Treasure token")."""
    selves = _self_names(name)
    total = 0
    for line in _lines(oracle):
        match = _ATTACK_TREASURE.match(line)
        if match is not None and match.group("who").strip() in selves:
            word = match.group("n")
            total += int(word) if word.isdigit() else _NUMBER_WORDS.get(word, 1)
    return total


class _Side:
    """One side's sacrifice fodder, mana and team sacrifice pumps, read once per board."""

    def __init__(
        self,
        battlefield: list[dict],
        seat: Any,
        *,
        mana: int,
        untapped_only: bool,
        treasures: int = 0,
        hand_size: int | None = None,
    ) -> None:
        self.seat = seat
        self.hand_size = hand_size
        self.cards = [c for c in battlefield if isinstance(c, dict) and _controller(c) == seat]
        self.mana = mana
        self.untapped_only = untapped_only
        self.treasures = treasures  # made by their attack triggers before blocks
        self._counts: dict[str, int | None] = {}
        # (source id, source name, kind, power, toughness, line, scope) of "creatures you control" pumps
        self.team: list[tuple[Any, str, str, int, int, str, str]] = []
        for card in self.cards:
            name = str(card.get("name") or "a permanent")
            for kind, scope, power, toughness, line in _sac_clauses(name, card.get("oracle_text") or ""):
                if scope in ("team", "others"):
                    self.team.append((card.get("instance_id"), name, kind, power, toughness, line, scope))

    def sacrificable(self, kind: str) -> int | None:
        """How many ``kind`` permanents (a self-sacrificing token kind, or "artifact"/"permanent"/
        "token") they can sacrifice now; None when the kind needs an outlet we can't count."""
        if kind in self._counts:
            return self._counts[kind]
        words = set(_KIND_WORDS.findall(kind))
        kinds = [k for k in _SELF_SAC if k in words or f"{k}s" in words]
        if not kinds and words & {"artifact", "artifacts", "permanent", "permanents", "token", "tokens"}:
            kinds = list(_SELF_SAC)  # every self-sacrificing token is an artifact token permanent
        count: int | None = None
        if kinds:
            count, left = 0, self.mana
            for card in self.cards:
                kind_of = next((k for k in kinds if has_kind(card, k)), None)
                if kind_of is None:
                    continue
                cost, taps = _SELF_SAC[kind_of]
                if taps and self.untapped_only and card.get("is_tapped"):
                    continue
                if cost:
                    if left < cost:
                        continue
                    left -= cost
                count += 1
            if "treasure" in kinds:
                count += self.treasures
        self._counts[kind] = count
        return count


def counter_growth_pump(card: dict, side: _Side) -> list[Pump]:
    """Affordable public counter doubling and a ready hand-size ultimate.

    Shared team activations grow every affected body for the same payment.
    Current counters/P/T are already resolved; only additional legal capacity
    is projected. No mana remains after the observed five-Forest activation.
    """
    from arenamcp.land_mana import mana_source
    from arenamcp.mulligan_policy import _pip_matching, _pips

    counters = card.get("counters") or {}
    count = (
        sum(
            v
            for k, v in counters.items()
            if str(k).lower().removeprefix("countertype_") in {"p1p1", "+1/+1"} and type(v) is int and v > 0
        )
        if isinstance(counters, dict)
        else 0
    )
    replacements = sum(
        bool(
            re.search(
                r"if one or more \+1/\+1 counters would be put on a creature you control, that many plus one \+1/\+1 counters",
                _rules_text(c),
            )
        )
        for c in side.cards
        if not c.get("is_phased_out")
    )
    sources = [
        mana_source(c, side.cards)
        for c in side.cards
        if "land" in _types(c)
        and not c.get("is_phased_out")
        and (not side.untapped_only or not c.get("is_tapped"))
    ]
    best = 0
    reason = ""
    for source in side.cards:
        if source.get("is_phased_out"):
            continue
        text = _rules_text(source)
        for match in re.finditer(
            r"((?:\{[^}]+\}\s*)+):\s*(double the number of \+1/\+1 counters on each creature you control\.)",
            text,
        ):
            cost, effect = match.groups()
            if not re.fullmatch(
                r"double the number of \+1/\+1 counters on each creature you control\.?", effect.strip()
            ):
                continue
            if re.sub(r"\{[^}]*\}|\s", "", cost) or any(t in _mana_symbols(cost) for t in ("t", "q")):
                continue
            value = _mana_value(cost)
            if value <= 0 or count <= 0:
                continue
            repeats = min(8, len(sources) // value)
            while repeats and not _pip_matching(_pips(cost) * repeats, sources):
                repeats -= 1
            grown = count
            for _ in range(repeats):
                grown = grown * 2 + replacements
            if grown - count > best:
                best, reason = (
                    grown - count,
                    f"{source.get('name') or 'counter ability'}: affordable counter doubling",
                )
        # This projects their upcoming turn, not a second loyalty activation
        # during a turn already underway. One normal draw plus the printed draw.
        if not side.untapped_only and type(side.hand_size) is int:
            match = re.search(
                r"[−-](\d+): draw (one|two|three|\d+) cards?\. then put x \+1/\+1 counters on each creature you control, where x is the number of cards in your hand",
                text,
            )
            if match and (loyalty(source) or 0) >= int(match[1]):
                drawn = _NUMBER_WORDS.get(match[2], int(match[2]) if match[2].isdigit() else 0)
                growth = side.hand_size + 1 + drawn + replacements
                if growth > best:
                    best, reason = (
                        growth,
                        f"{source.get('name') or 'planeswalker'}: ready counter ultimate after draw",
                    )
    return [Pump(best, best, 1, reason)] if best else []


def instant_pumps(card: dict, side: _Side) -> tuple[list[Pump], list[str]]:
    """(the instant-speed pumps ``card`` gets, the pump texts we can't price) on its controller's board.

    Its own "whenever you sacrifice a Treasure, ~ gets +1/+0" (one per
    Treasure they can sacrifice), every other permanent's "creatures you
    control get +N/+N" sacrifice trigger, and its own mana-cost pump ("{R}: ~
    gets +1/+0", as often as their mana pays for it). A sacrifice pump that
    needs an outlet, a pump with a non-mana cost, one of another creature or a
    free repeatable one is returned unpriced.
    """
    name = str(card.get("name") or "creature")
    oracle = card.get("oracle_text") or ""
    identity = card.get("instance_id")
    pumps: list[Pump] = []
    unpriced: list[str] = []
    clauses = [(kind, scope, p, t, line, name) for kind, scope, p, t, line in _sac_clauses(name, oracle)]
    clauses += [
        (kind, "team", p, t, line, source)
        for source_id, source, kind, p, t, line, _scope in side.team
        if source_id != identity  # its own team pump is in its own clauses
    ]
    for kind, scope, power, toughness, line, source in clauses:
        if scope == "unpriced":
            unpriced.append(f"{source}: '{line}'")
            continue
        if scope == "others" and source == name:
            continue
        count = side.sacrificable(kind)
        if count is None:
            unpriced.append(f"{source}: '{line}'")
        elif count:
            pumps.append(Pump(power, toughness, count, f"{count} {kind}"))
    for cost, effect, pump in _mana_pump_clauses(oracle):
        symbols = _mana_symbols(cost)
        if "t" in symbols or "q" in symbols:
            continue  # an attacking creature is tapped (and an untap cost is not instant-speed damage)
        value = _mana_value(cost)
        if pump is None or not pump[2] or _NON_MANA_COST.search(re.sub(r"\{[^}]*\}", " ", cost)):
            if value <= side.mana:
                unpriced.append(f"{name}: '{cost}: {effect}'")
            continue
        times = _pump_activations(effect, value, side.mana, ability=True)
        if times is None:
            unpriced.append(f"{name}: '{cost}: {effect}'")
        elif value <= side.mana:
            pumps.append(Pump(pump[0], pump[1], times, f"{times}x {cost.strip()}"))
    pumps.extend(counter_growth_pump(card, side))
    return pumps, unpriced


def pump_total(pumps: list[Pump]) -> tuple[int, int]:
    return sum(p.power * p.times for p in pumps), sum(p.toughness * p.times for p in pumps)


def _known(card: dict) -> bool:
    return all(
        isinstance(card.get(k), int) and not isinstance(card.get(k), bool) for k in ("power", "toughness")
    )


def _player(state: dict, seat: Any) -> dict:
    return next((p for p in state.get("players") or [] if p.get("seat_id") == seat), {})


def _pool(player: dict) -> int:
    pool = player.get("mana_pool") or {}
    amounts = pool.values() if isinstance(pool, dict) else pool if isinstance(pool, list) else []
    total = 0
    for amount in amounts:
        if isinstance(amount, (int, float)) and not isinstance(amount, bool):
            total += max(0, int(amount))
        elif amount:
            total += 1
    return total


def _sides(cards: list[dict], state: dict, *, next_turn: bool) -> dict[Any, _Side]:
    battlefield = [c for c in state.get("battlefield") or [] if isinstance(c, dict)]
    sides: dict[Any, _Side] = {}
    for card in cards:
        seat = _controller(card)
        if seat in sides:
            continue
        mana = side_mana(battlefield, seat, untapped_only=not next_turn)
        made = 0
        if next_turn:  # their attack still to come: its Treasure triggers resolve before blocks
            made = sum(
                _attack_treasures(str(c.get("name") or ""), c.get("oracle_text") or "")
                for c in cards
                if _controller(c) == seat
            )
        else:
            mana += _pool(_player(state, seat))
        sides[seat] = _Side(battlefield, seat, mana=mana, untapped_only=not next_turn, treasures=made)
    return sides


def pumped_attackers(attackers: list[dict], state: dict, *, next_turn: bool = False) -> list[dict]:
    """``attackers`` (raw cards) with every priced instant-speed pump of theirs applied (copies).

    ``next_turn``: their next attack, after their untap step (all their mana
    and Treasures, and the Treasures their attackers' attack triggers make);
    otherwise the attack under way (what is untapped now). A pumped copy
    carries ``potential_pump`` (the +power it was given) and ``potential_pump_note``.
    """
    sides = _sides(attackers, state, next_turn=next_turn)
    out = []
    for card in attackers:
        if not _known(card):
            out.append(card)
            continue
        pumps, _unpriced = instant_pumps(card, sides[_controller(card)])
        power, toughness = pump_total(pumps)
        if power or toughness:
            pumped = {**card, "power": card["power"] + power, "toughness": card["toughness"] + toughness}
            pumped["potential_pump"] = power
            pumped["potential_pump_note"] = ", ".join(p.why for p in pumps)
            out.append(pumped)
        else:
            out.append(card)
    return out


def unpriced_pumps(attackers: list[dict], state: dict) -> list[str]:
    """Pump texts of the attackers (or a pump of another creature on their board) we can't price."""
    sides = _sides(attackers, state, next_turn=False)
    found: list[str] = []
    for card in attackers:
        for text in instant_pumps(card, sides[_controller(card)])[1]:
            if text not in found:
                found.append(text)
    # Their other permanents' activated pumps of another creature ("{1}: target creature gets +1/+0").
    attacking = {card.get("instance_id") for card in attackers}
    for side in sides.values():
        for card in side.cards:
            if card.get("instance_id") in attacking:
                continue
            for cost, effect, pump in _mana_pump_clauses(card.get("oracle_text") or ""):
                if pump is not None and pump[2]:
                    continue  # it pumps only itself, and it is not attacking
                if _mana_value(cost) > side.mana or ("t" in _mana_symbols(cost) and card.get("is_tapped")):
                    continue
                text = f"{card.get('name') or 'permanent'}: '{cost}: {effect}'"
                if text not in found:
                    found.append(text)
    return found


# --- attack-trigger grants (#194) ----------------------------------------------------------

_GRANT = re.compile(
    r"^whenever (?:(?P<group>you attack with (?P<n>a|an|one|two|three|four|five|six|seven|eight|\d+) or more "
    r"(?P<kind>[a-z]+))|(?P<any>you attack)|(?P<who>[^,.]+?) attacks(?P<first> for the first time each turn)?)"
    r",\s*(?P<recipients>[^.]*?) gets? \+(?P<p>\d+)/\+(?P<t>\d+)"
    r"(?: and (?:gains?|have|has) (?P<kws>[a-z ,]+?))? until end of turn\.?$"
)
_GRANTABLE = (
    "flying", "trample", "menace", "first strike", "double strike", "deathtouch", "lifelink",
    "indestructible", "vigilance", "haste",
)  # fmt: skip
_RECIPIENTS = re.compile(
    r"^(?P<other>other )?(?P<attacking>attacking )?(?:(?P<kind>[a-z]+?)s|creatures) you control$"
    r"|^each (?P<other2>other )?(?P<attacking2>attacking )?creature you control$"
)


@dataclass(frozen=True)
class Grant:
    """An attack trigger that pumps their attackers: +power/+toughness and keywords, when met."""

    source_id: Any
    source: str
    need: int  # attackers of ``kind`` needed (0: any attack)
    kind: str  # creature type the condition counts ("" any creature)
    self_attack: bool  # "whenever ~ attacks": the source must attack
    power: int
    toughness: int
    keywords: tuple[str, ...]
    recipient_kind: str  # "" every creature of theirs
    attacking_only: bool
    others_only: bool
    once: bool  # "for the first time each turn"


@lru_cache(maxsize=4096)
def _grant_lines(name: str, oracle: str) -> tuple[tuple, ...]:
    out = []
    selves = _self_names(name)
    for line in _lines(oracle):
        match = _GRANT.match(line)
        if match is None:
            continue
        rec = _RECIPIENTS.match(match.group("recipients").strip())
        if rec is None:
            continue
        kind = (rec.group("kind") or "").strip()
        if kind in ("creature", "other creature"):
            kind = ""
        keywords = tuple(k for k in _GRANTABLE if match.group("kws") and k in match.group("kws"))
        if match.group("kws") and "can't be blocked" in match.group("kws"):
            keywords += ("unblockable",)
        who = (match.group("who") or "").strip()
        if match.group("who") and who not in selves:
            continue  # "whenever another creature attacks": not read
        need = 0
        if match.group("group"):
            word = match.group("n")
            need = int(word) if word.isdigit() else _NUMBER_WORDS.get(word, 1)
        out.append(
            (
                need,
                (match.group("kind") or "").strip(),
                bool(match.group("who")),
                int(match.group("p")),
                int(match.group("t")),
                keywords,
                kind,
                bool(rec.group("attacking") or rec.group("attacking2")),
                bool(rec.group("other") or rec.group("other2")),
                bool(match.group("first")),
            )
        )
    return tuple(out)


def grants_of(card: dict) -> list[Grant]:
    """The attack-trigger team grants on ``card`` (``_GRANT``)."""
    card = card.get("_card") or card
    found = []
    for need, kind, own, p, t, kws, rkind, attacking, others, once in _grant_lines(
        str(card.get("name") or ""), card.get("oracle_text") or ""
    ):
        found.append(
            Grant(
                source_id=card.get("instance_id"),
                source=str(card.get("name") or "a permanent"),
                need=need,
                kind=kind,
                self_attack=own,
                power=p,
                toughness=t,
                keywords=kws,
                recipient_kind=rkind,
                attacking_only=attacking,
                others_only=others,
                once=once,
            )  # fmt: skip
        )
    return found


def grant_met(grant: Grant, attackers: list[dict]) -> bool:
    """Whether ``attackers`` meet the trigger's condition (an unreadable type counts as met)."""
    if grant.self_attack:
        return grant.source_id in {_card_id(b) for b in attackers}
    if not grant.need:
        return bool(attackers)
    if not grant.kind:
        return len(attackers) >= grant.need
    counted = [b for b in attackers if has_kind(b.get("_card") or b, grant.kind)]
    return len(counted) >= grant.need


def apply_grants(attackers: list[dict], grants: list[Grant]) -> tuple[list[dict], list[str]]:
    """``attackers`` with each met grant applied (copies), and 'Harbin: +1/+1 and flying' notes."""
    out = list(attackers)
    notes: list[str] = []
    for grant in grants:
        if not grant_met(grant, attackers):
            continue
        changed = False
        for index, body in enumerate(out):
            card = body.get("_card") or body
            if grant.others_only and _card_id(body) == grant.source_id:
                continue
            if grant.recipient_kind and not has_kind(card, grant.recipient_kind):
                continue
            new = {
                **body,
                "power": body["power"] + grant.power,
                "toughness": body["toughness"] + grant.toughness,
            }
            keywords = list(body.get("keywords") or ())
            for keyword in grant.keywords:
                if keyword == "unblockable":
                    new["_unblockable"] = True
                    new["cant_be_blocked"] = True
                elif keyword not in keywords:
                    keywords.append(keyword)
            new["keywords"] = keywords
            out[index] = new
            changed = True
        if changed:
            extra = f" and {', '.join(grant.keywords)}" if grant.keywords else ""
            notes.append(f"{grant.source}: +{grant.power}/+{grant.toughness}{extra}")
    return out, notes


# --- extra combat phases (#191) -----------------------------------------------------------

_EXTRA_COMBAT = re.compile(
    r"^whenever (?P<who>[^,.]+?) attacks(?P<first> for the first time each turn)?, (?P<effect>[^\n]*?"
    r"\badditional combat phase\b[^\n]*)$"
)


@dataclass(frozen=True)
class ExtraCombat:
    source_id: Any
    source: str
    untap_all: bool  # "untap all creatures you control": everyone attacks again


@lru_cache(maxsize=4096)
def _extra_lines(name: str, oracle: str) -> tuple[bool, ...]:
    selves = _self_names(name)
    out = []
    for line in _lines(oracle):
        match = _EXTRA_COMBAT.match(line)
        if match is None or match.group("who").strip() not in selves:
            continue
        out.append("untap all creatures you control" in match.group("effect"))
    return tuple(out)


def extra_combat_of(card: dict) -> ExtraCombat | None:
    """Its "whenever ~ attacks (for the first time each turn), ... additional combat phase", or None."""
    card = card.get("_card") or card
    lines = _extra_lines(str(card.get("name") or ""), card.get("oracle_text") or "")
    if not lines:
        return None
    return ExtraCombat(card.get("instance_id"), str(card.get("name") or "a creature"), any(lines))


def modelled_attack_damage(card: dict, line: str) -> bool:
    """Whether an attack-trigger line is an attack damage/drain modelled by ``attack_trigger_drain``."""
    name = str((card.get("_card") or card).get("name") or "")
    return bool(_attack_damage_lines(name, line.strip()))


def modelled_attack_trigger(card: dict, line: str) -> bool:
    """Whether an attack-trigger line is played by the attack projections (grant, extra combat,
    attack-created tokens, or attack damage/drain)."""
    name = str((card.get("_card") or card).get("name") or "")
    line = line.strip()
    return bool(
        _grant_lines(name, line)
        or _extra_lines(name, line)
        or _attack_token_lines(name, line, (line,))
        or _attack_damage_lines(name, line)
    )


# --- attack-created tokens (Mobilize N, tapped-and-attacking tokens) ------------------------

_MOBILIZE = re.compile(r"\bmobilize\s+(?P<n>\d+)\b")
_GRANTED_MOBILIZE = re.compile(r"\b(?:has|have|gains?|equipped|enchanted|until end of turn)\b")
_ATTACK_TOKEN = re.compile(
    r"^whenever (?:(?P<any>you attack)|(?P<who>[^.]+?) attacks(?: for the first time each turn)?),\s*"
    r"create (?P<n>a|an|one|two|three|four|five|six|seven|eight|\d+) "
    r"(?P<p>\d+)/(?P<t>\d+) (?P<desc>[^.]*?) creature tokens?"
    r"(?: with (?P<kws>(?:(?!that are|that's|that is|tapped and attacking)[^.])*?))?"
    r"(?:\s+(?:that are|that's|that is))?\s+tapped and attacking\b(?P<rest>.*)$"
)
_EPHEMERAL_CLAUSE = re.compile(r"\b(?:sacrifice|exile)\b[^.]*\b(?:end step|end of combat)\b")
_TOKEN_BODY_ID_START = 651_000_000
_MAX_ATTACK_TOKENS = 8


@dataclass(frozen=True)
class AttackTokens:
    """Attack trigger that creates creature token(s) tapped and attacking."""

    source_id: Any
    source: str
    self_attack: bool
    count: int
    power: int
    toughness: int
    keywords: tuple[str, ...] = ()
    ephemeral: bool = False
    subtypes: tuple[str, ...] = ()


@lru_cache(maxsize=4096)
def _attack_token_lines(
    name: str, oracle: str, keywords: tuple[str, ...] = ()
) -> tuple[tuple[bool, int, int, int, tuple[str, ...], bool, tuple[str, ...]], ...]:
    selves = _self_names(name)
    out: list[tuple[bool, int, int, int, tuple[str, ...], bool, tuple[str, ...]]] = []
    seen_mobilize = False
    for line in _lines(oracle):
        mob = _MOBILIZE.search(line)
        if mob is not None and not _GRANTED_MOBILIZE.search(line):
            n = min(max(0, int(mob.group("n"))), _MAX_ATTACK_TOKENS)
            if n:
                out.append((True, n, 1, 1, (), True, ("warrior",)))
                seen_mobilize = True
            continue
        match = _ATTACK_TOKEN.match(line)
        if match is None:
            continue
        who = (match.group("who") or "").strip()
        self_attack = not bool(match.group("any"))
        if self_attack and who not in selves:
            continue
        word = match.group("n")
        count = min(max(0, int(word) if word.isdigit() else _NUMBER_WORDS.get(word, 1)), _MAX_ATTACK_TOKENS)
        if not count:
            continue
        kws_text = match.group("kws") or ""
        tok_kws = tuple(k for k in _GRANTABLE if k in kws_text)
        ephemeral = bool(_EPHEMERAL_CLAUSE.search(match.group("rest") or ""))
        desc_words = tuple(_KIND_WORDS.findall(match.group("desc") or ""))
        out.append(
            (self_attack, count, int(match.group("p")), int(match.group("t")), tok_kws, ephemeral, desc_words)
        )
    if not seen_mobilize:
        for kw in keywords:
            mob = _MOBILIZE.search(str(kw).lower())
            if mob is not None:
                n = min(max(0, int(mob.group("n"))), _MAX_ATTACK_TOKENS)
                if n:
                    out.append((True, n, 1, 1, (), True, ("warrior",)))
                    break
    return tuple(out)


def attack_tokens_of(card: dict) -> list[AttackTokens]:
    """The attack-created token triggers (``Mobilize N`` or ``create ... tapped and attacking``) on ``card``."""
    card = card.get("_card") or card
    kws = tuple(str(k).lower() for k in (card.get("keywords") or ()))
    found: list[AttackTokens] = []
    for self_attack, count, p, t, tok_kws, ephemeral, subtypes in _attack_token_lines(
        str(card.get("name") or ""), card.get("oracle_text") or "", kws
    ):
        found.append(
            AttackTokens(
                source_id=card.get("instance_id"),
                source=str(card.get("name") or "a permanent"),
                self_attack=self_attack,
                count=count,
                power=p,
                toughness=t,
                keywords=tok_kws,
                ephemeral=ephemeral,
                subtypes=subtypes,
            )
        )
    return found


def make_attack_tokens(
    attackers: list[dict],
    *,
    team_tokens: tuple[AttackTokens, ...] = (),
    base_id: int = _TOKEN_BODY_ID_START,
) -> list[dict]:
    """Synthetic tapped-and-attacking token bodies created when ``attackers`` attack."""
    if not attackers:
        return []
    specs: list[tuple[AttackTokens, Any]] = []
    seen_team: set[tuple[Any, int, int, int]] = set()
    seat_default = _controller(attackers[0].get("_card") or attackers[0])
    for body in attackers:
        raw_seat = _controller(body.get("_card") or body)
        seat = raw_seat if raw_seat is not None else seat_default
        toks = body.get("_atk_tokens")
        if toks is None:
            toks = tuple(attack_tokens_of(body))
        for tok in toks:
            if not tok.self_attack:
                key = (tok.source_id, tok.count, tok.power, tok.toughness)
                if key in seen_team:
                    continue
                seen_team.add(key)
            specs.append((tok, seat))
        for tok in body.get("_atk_team_tokens") or ():
            if tok.self_attack:
                continue
            key = (tok.source_id, tok.count, tok.power, tok.toughness)
            if key not in seen_team:
                seen_team.add(key)
                specs.append((tok, seat))
    for tok in team_tokens:
        if tok.self_attack:
            continue
        key = (tok.source_id, tok.count, tok.power, tok.toughness)
        if key not in seen_team:
            seen_team.add(key)
            specs.append((tok, seat_default))
    created: list[dict] = []
    next_id = base_id
    for tok, seat in specs:
        for _ in range(tok.count):
            if len(created) >= _MAX_ATTACK_TOKENS:
                return created
            created.append(
                {
                    "instance_id": next_id,
                    "name": f"{tok.source} token",
                    "power": tok.power,
                    "toughness": tok.toughness,
                    "keywords": list(tok.keywords),
                    "subtypes": list(tok.subtypes),
                    "type_line": "Token Creature",
                    "is_token": True,
                    "oracle_text": "",
                    "controller_seat_id": seat,
                    "_tapped": True,
                    "is_tapped": True,
                    "_attacking": True,
                    "is_attacking": True,
                    "_sick": False,
                    "_can_attack": True,
                    "_can_block": False,
                    "_unblockable": False,
                    "_ward": 0,
                    "_created_attack_token": True,
                    "_ephemeral_attack_token": tok.ephemeral,
                }
            )
            next_id += 1
    return created


# --- attack-trigger direct damage / life loss / gain ---------------------------------------

_POWER_LE_DAMAGE = re.compile(
    r"^whenever (?P<who>[^.]+?) attacks(?:\s+for the first time each turn)?,\s*"
    r"(?:it|[^,.]+?) deals x damage to (?:each opponent|defending player|target opponent)"
    r"(?P<gain> and you gain x life)?,\s*"
    r"where x is the number of (?P<other>other )?creatures you control with power (?P<maxp>\d+) or less\.?$"
)
_COUNT_DAMAGE = re.compile(
    r"^whenever (?P<who>[^.]+?) attacks(?:\s+for the first time each turn)?,\s*"
    r"(?:(?:it|[^,.]+?) deals x damage to (?:each opponent|defending player|target opponent)"
    r"|(?:each opponent|defending player) loses x life)"
    r"(?P<gain> and you gain x life)?,\s*"
    r"where x is the number of (?P<other>other )?(?P<att>attacking )?creatures(?: you control)?\.?$"
)
_FIXED_SELF_DAMAGE = re.compile(
    r"^whenever (?P<who>[^.]+?) attacks(?:\s+for the first time each turn)?,\s*"
    r"(?:(?:it|[^,.]+?) deals (?P<n>\d+) damage to (?:each opponent|defending player|target opponent)"
    r"|(?:each opponent|defending player) loses (?P<n2>\d+) life)"
    r"(?: and you gain (?P<g>\d+) life)?\.?$"
)
_PER_ATTACKER_DAMAGE = re.compile(
    r"^whenever a creature you control attacks,\s*"
    r"(?:[^,.]+? deals (?P<n>\d+) damage to "
    r"(?:each opponent|defending player|target opponent|the player or planeswalker it's attacking)"
    r"|(?:each opponent|defending player) loses (?P<n2>\d+) life)"
    r"(?: and you gain (?P<g>\d+) life)?\.?$"
)


@dataclass(frozen=True)
class AttackDamage:
    """Attack trigger that directly deals damage / causes life loss (and optional life gain)."""

    source_id: Any
    source: str
    self_attack: bool
    kind: str  # "fixed" | "per_attacker" | "count_creatures" | "count_attackers" | "count_power_le"
    amount: int
    life_gain: int = 0
    gains_x: bool = False
    other_only: bool = False


@lru_cache(maxsize=4096)
def _attack_damage_lines(name: str, oracle: str) -> tuple[tuple[bool, str, int, int, bool, bool], ...]:
    selves = _self_names(name)
    out: list[tuple[bool, str, int, int, bool, bool]] = []
    seen_lines: set[str] = set()
    for line in _lines(oracle):
        if line in seen_lines:
            continue
        seen_lines.add(line)
        if (m := _PER_ATTACKER_DAMAGE.match(line)) is not None:
            amt = int(m.group("n") or m.group("n2"))
            gain = int(m.group("g") or 0)
            out.append((False, "per_attacker", amt, gain, False, False))
            continue
        if (m := _POWER_LE_DAMAGE.match(line)) is not None and m.group("who").strip() in selves:
            out.append(
                (
                    True,
                    "count_power_le",
                    int(m.group("maxp")),
                    0,
                    bool(m.group("gain")),
                    bool(m.group("other")),
                )
            )
            continue
        if (m := _COUNT_DAMAGE.match(line)) is not None and m.group("who").strip() in selves:
            kind = "count_attackers" if m.group("att") else "count_creatures"
            out.append((True, kind, 1, 0, bool(m.group("gain")), bool(m.group("other"))))
            continue
        if (m := _FIXED_SELF_DAMAGE.match(line)) is not None and m.group("who").strip() in selves:
            amt = int(m.group("n") or m.group("n2"))
            gain = int(m.group("g") or 0)
            out.append((True, "fixed", amt, gain, False, False))
            continue
    return tuple(out)


def attack_damage_of(card: dict) -> list[AttackDamage]:
    """Direct attack-trigger damage/drain effects on ``card``."""
    card = card.get("_card") or card
    found: list[AttackDamage] = []
    for self_attack, kind, amount, life_gain, gains_x, other_only in _attack_damage_lines(
        str(card.get("name") or ""), card.get("oracle_text") or ""
    ):
        found.append(
            AttackDamage(
                source_id=card.get("instance_id"),
                source=str(card.get("name") or "a permanent"),
                self_attack=self_attack,
                kind=kind,
                amount=amount,
                life_gain=life_gain,
                gains_x=gains_x,
                other_only=other_only,
            )
        )
    return found


def attack_trigger_drain(
    attackers: list[dict],
    *,
    side_creatures: list[dict] | None = None,
    team_damage: tuple[AttackDamage, ...] = (),
    declared: bool = False,
) -> tuple[int, int]:
    """(damage dealt to defender, life gained by attacker) from attack triggers on ``attackers``.

    When ``declared`` is True (attackers already declared), triggers have resolved and 0 is returned.
    Synthetic ``_created_attack_token`` bodies count as controlled creatures/attackers for X-counts
    (e.g. Arabella + Mobilize), but do NOT trigger per-declared-attacker triggers (e.g. Hellrider).
    """
    if declared or not attackers:
        return 0, 0
    declared_attackers = [a for a in attackers if not a.get("_created_attack_token")]
    if not declared_attackers:
        return 0, 0
    attacker_ids = {_card_id(a) for a in declared_attackers if _card_id(a) is not None}

    collected: list[AttackDamage] = []
    seen_keys: set[tuple[Any, str, int, bool]] = set()

    def add_dmg(dmg: AttackDamage) -> None:
        key = (dmg.source_id, dmg.kind, dmg.amount, dmg.other_only)
        if key not in seen_keys:
            seen_keys.add(key)
            collected.append(dmg)

    for a in declared_attackers:
        dmgs = a.get("_atk_damage")
        if dmgs is None:
            dmgs = tuple(attack_damage_of(a))
        for dmg in dmgs:
            add_dmg(dmg)
        for dmg in a.get("_atk_team_damage") or ():
            if not dmg.self_attack:
                add_dmg(dmg)
    for dmg in team_damage:
        if not dmg.self_attack:
            add_dmg(dmg)
    if side_creatures:
        for c in side_creatures:
            dmgs = c.get("_atk_damage")
            if dmgs is None:
                dmgs = tuple(attack_damage_of(c))
            for dmg in dmgs:
                if not dmg.self_attack:
                    add_dmg(dmg)

    if not collected:
        return 0, 0

    all_creatures: list[dict] = list(attackers)
    seen_cids = {_card_id(c) for c in attackers if _card_id(c) is not None}
    if side_creatures:
        for c in side_creatures:
            cid = _card_id(c)
            if cid is None or cid not in seen_cids:
                all_creatures.append(c)
                if cid is not None:
                    seen_cids.add(cid)

    total_damage = 0
    total_gain = 0
    for dmg in collected:
        if dmg.self_attack and (dmg.source_id is None or dmg.source_id not in attacker_ids):
            continue
        deal = 0
        gain = 0
        if dmg.kind == "fixed":
            deal = dmg.amount
            gain = dmg.life_gain
        elif dmg.kind == "per_attacker":
            deal = dmg.amount * len(declared_attackers)
            gain = dmg.life_gain * len(declared_attackers)
        elif dmg.kind == "count_attackers":
            deal = sum(1 for c in attackers if not (dmg.other_only and _card_id(c) == dmg.source_id))
            gain = deal if dmg.gains_x else dmg.life_gain
        elif dmg.kind == "count_creatures":
            deal = sum(1 for c in all_creatures if not (dmg.other_only and _card_id(c) == dmg.source_id))
            gain = deal if dmg.gains_x else dmg.life_gain
        elif dmg.kind == "count_power_le":
            deal = sum(
                1
                for c in all_creatures
                if not (dmg.other_only and _card_id(c) == dmg.source_id)
                and isinstance(c.get("power"), int)
                and not isinstance(c.get("power"), bool)
                and c["power"] <= dmg.amount
            )
            gain = deal if dmg.gains_x else dmg.life_gain
        total_damage += deal
        total_gain += gain
    return total_damage, total_gain


def projected_attack(
    attackers: list[dict],
    *,
    side_creatures: list[dict] | None = None,
    team_tokens: tuple[AttackTokens, ...] = (),
    team_damage: tuple[AttackDamage, ...] = (),
    declared: bool = False,
) -> tuple[list[dict], int, int]:
    """Return ``(expanded_attackers, trig_damage, trig_gain)`` including attack-created tokens and triggers."""
    expanded = attack_bodies(attackers, declared=declared, team_tokens=team_tokens)
    trig_damage, trig_gain = attack_trigger_drain(
        expanded,
        side_creatures=side_creatures,
        team_damage=team_damage,
        declared=declared,
    )
    return expanded, trig_damage, trig_gain


def extra_combats_now(state: dict, attackers: list[dict]) -> ExtraCombat | None:
    """The extra combat that follows the combat under way, or None.

    One of the attackers has the trigger, and this is its first combat: with
    "untap all creatures you control" the trigger has untapped their
    attacking creatures (an attacker without vigilance is untapped while it
    attacks); without that clause we can't tell, and assume it follows.
    """
    for card in attackers:
        extra = extra_combat_of(card)
        if extra is None:
            continue
        if not extra.untap_all:
            return extra
        if any(
            not a.get("is_tapped") and not has_combat_keyword(a, "vigilance")
            for a in attackers
            if a.get("is_attacking", True)
        ):
            return extra
    return None


# --- solver bodies ----------------------------------------------------------------------------


def annotate(
    bodies: list[dict],
    battlefield: list[dict],
    seat: Any,
    *,
    untapped_only: bool,
    declared: bool,
    hand_size: int | None = None,
) -> list[str]:
    """Mark ``seat``'s solver bodies with what their attack adds; returns 'Name (why)' notes.

    ``_atk_pump``: (power, toughness) of their priced instant-speed pumps, with
    the mana and Treasures of their next attack (``untapped_only``: their turn,
    what is untapped now). ``_atk_grants``: the side's attack-trigger grants
    (on every body: a "whenever you attack with ..." source need not attack).
    ``_atk_extra``: the body's extra-combat trigger. ``declared`` (their
    attack is under way): its grants are in the snapshot already.
    """
    battlefield = [c for c in battlefield if isinstance(c, dict)]
    made = 0
    if not declared:  # their attack still to come: its Treasure triggers resolve before blocks
        made = sum(
            _attack_treasures(str(b.get("name") or ""), b.get("oracle_text") or "")
            for b in bodies
            if b.get("_can_attack", True) and not b.get("_sick")
        )
    side = _Side(
        battlefield, seat, mana=side_mana(battlefield, seat, untapped_only=untapped_only),
        untapped_only=untapped_only, treasures=made, hand_size=hand_size,
    )  # fmt: skip
    grants: list[Grant] = []
    team_tokens: list[AttackTokens] = []
    team_damage: list[AttackDamage] = []
    for card in side.cards:
        grants += grants_of(card)
        team_tokens += [t for t in attack_tokens_of(card) if not t.self_attack]
        team_damage += [d for d in attack_damage_of(card) if not d.self_attack]
    notes: list[str] = []
    for body in bodies:
        card = body.get("_card") or body
        pumps, _unpriced = instant_pumps(card, side)
        power, toughness = pump_total(pumps)
        if power or toughness:
            body["_atk_pump"] = (power, toughness)
            notes.append(
                f"{body.get('name') or 'creature'} +{power}/+{toughness} ({', '.join(p.why for p in pumps)})"
            )
        if grants and not declared:
            body["_atk_grants"] = tuple(grants)
        toks = attack_tokens_of(card)
        if toks:
            body["_atk_tokens"] = tuple(toks)
            for tok in toks:
                notes.append(f"{tok.source} (creates {tok.count} {tok.power}/{tok.toughness} attacker(s))")
        dmgs = attack_damage_of(card)
        if dmgs:
            body["_atk_damage"] = tuple(dmgs)
            notes.append(f"{body.get('name') or 'creature'} (attack damage trigger)")
        if team_tokens and not declared:
            body["_atk_team_tokens"] = tuple(team_tokens)
        if team_damage and not declared:
            body["_atk_team_damage"] = tuple(team_damage)
        extra = extra_combat_of(card)
        if extra is not None:
            body["_atk_extra"] = extra
            notes.append(f"{body.get('name') or 'creature'} (an extra combat)")
    for grant in grants:
        notes.append(f"{grant.source} (attack trigger: +{grant.power}/+{grant.toughness})")
    return notes


def annotated(attackers) -> bool:
    return any(
        b.get("_atk_pump")
        or b.get("_atk_grants")
        or b.get("_atk_extra")
        or b.get("_atk_tokens")
        or b.get("_atk_damage")
        or b.get("_atk_team_tokens")
        or b.get("_atk_team_damage")
        for b in attackers
    )


def attack_bodies(
    attackers: list[dict],
    *,
    declared: bool = False,
    team_tokens: tuple[AttackTokens, ...] = (),
) -> list[dict]:
    """Annotated ``attackers`` with their pumps, created attacking tokens, and met grants applied."""
    out = []
    for body in attackers:
        pump = body.get("_atk_pump")
        out.append(
            {**body, "power": body["power"] + pump[0], "toughness": body["toughness"] + pump[1]}
            if pump
            else body
        )
    if declared:
        return out
    created = make_attack_tokens(out, team_tokens=team_tokens)
    if created:
        out = [*out, *created]
    grants: list[Grant] = []
    for body in attackers:
        for grant in body.get("_atk_grants") or ():
            if grant not in grants:
                grants.append(grant)
    if grants:
        out, _notes = apply_grants(out, grants)
    return out


def extra_combats(attackers: list[dict], *, declared: bool = False) -> ExtraCombat | None:
    """The extra combat an annotated attack gets: a source among the attackers (for the attack
    under way, only while their attacking creatures stand untapped by its trigger)."""
    for body in attackers:
        extra = body.get("_atk_extra")
        if extra is None:
            continue
        if not declared or not extra.untap_all:
            return extra
        if any(not b.get("_tapped") and not has_combat_keyword(b, "vigilance") for b in attackers):
            return extra
    return None


Round = Callable[[list[dict], list[dict], int], tuple]


def attack_round(
    attackers: list[dict],
    blockers: list[dict],
    life: int,
    once: Round,
    *,
    declared: bool = False,
    side: list[dict] | None = None,
) -> tuple:
    """``once`` (a ``_combat_life``-shaped round: damage, dead attackers, dead blockers, attacker
    gain, defender gain) with the attackers' pumps, created attacking tokens, attack-trigger
    damage/drain, and grants, plus the extra combat it gets: its surviving attackers again (all of
    them untapped by the trigger, the source's vigilance aside; ephemeral attack tokens excluded)
    into the blockers left alive. Damage and gains add up; the dead sets are the union."""
    from arenamcp.combat_infect import add_deltas, apply_deltas, apply_marked_damage
    from arenamcp.combat_loss import CombatDetail, LossState

    fighting = attack_bodies(attackers, declared=declared)
    trig_dmg, trig_gain = attack_trigger_drain(fighting, side_creatures=side, declared=declared)
    created_ids = {b["instance_id"] for b in fighting if b.get("_created_attack_token")}
    extra = extra_combats(attackers, declared=declared)
    if extra is not None:
        fighting = [{**b, "_track_combat_damage": True} for b in fighting]

    def _clean_dead(dead: set) -> set:
        return set(dead) - created_ids if created_ids else set(dead)

    def _with_trigger(
        res: tuple, t_dmg: int, t_gain: int, dead_a: set, dead_b: set, extra_loss=None
    ) -> tuple:
        if len(res) < 7:
            d, _, _, g, h = res[:5]
            base = (d + t_dmg, dead_a, dead_b, g + t_gain, h)
        else:
            d, _, _, g, h, llg, llh = res
            base = (d + t_dmg, dead_a, dead_b, g + t_gain, h, llg, llh)
        if isinstance(res, CombatDetail) or extra_loss is not None:
            return CombatDetail(
                base,
                extra_loss if extra_loss is not None else getattr(res, "loss", LossState()),
                getattr(res, "terminal", False),
                getattr(res, "counter_deltas", {}),
                getattr(res, "damage_deltas", {}),
            )
        return base

    # CR 508.2 / 704.5a: attack triggers resolve before blockers and combat damage.
    if trig_dmg > 0 and trig_dmg >= life:
        empty = once([], blockers, 1)
        return _with_trigger(empty, trig_dmg, trig_gain, set(), set())

    result = once(fighting, blockers, life - trig_dmg)
    changes = getattr(result, "counter_deltas", {})
    marked = getattr(result, "damage_deltas", {})

    def survivors(cards, dead):
        return apply_marked_damage(
            apply_deltas(
                [
                    b
                    for b in cards
                    if b.get("instance_id") not in dead and not b.get("_ephemeral_attack_token")
                ],
                changes,
            ),
            marked,
        )

    dead_att0 = _clean_dead(result[1])
    dead_blk0 = set(result[2])
    if extra is None or not fighting:
        if trig_dmg or trig_gain or created_ids:
            return _with_trigger(result, trig_dmg, trig_gain, dead_att0, dead_blk0)
        return result

    damage, _, _, gain, healed = result[:5]
    left = life - (damage + trig_dmg) + healed
    if left <= 0 or getattr(result, "terminal", False):
        if trig_dmg or trig_gain or created_ids:
            return _with_trigger(result, trig_dmg, trig_gain, dead_att0, dead_blk0)
        return result

    raw_side = side if side is not None else attackers
    side_after = [b for b in raw_side if b.get("instance_id") not in dead_att0]
    again = survivors(list(attackers), dead_att0)
    if not extra.untap_all:
        again = [b for b in again if has_combat_keyword(b, "vigilance")]
    if not again:
        if trig_dmg or trig_gain or created_ids:
            return _with_trigger(result, trig_dmg, trig_gain, dead_att0, dead_blk0)
        return result

    standing = survivors(blockers, dead_blk0)
    fighting2 = attack_bodies(again, declared=False)
    trig_dmg2, trig_gain2 = attack_trigger_drain(fighting2, side_creatures=side_after, declared=False)
    created_ids2 = {b["instance_id"] for b in fighting2 if b.get("_created_attack_token")}
    if trig_dmg2 > 0 and trig_dmg2 >= left:
        more = once([], standing, 1)
        dead_att2 = set()
        dead_blk2 = set()
    else:
        more = once(fighting2, standing, left - trig_dmg2)
        dead_att2 = set(more[1]) - created_ids2 if created_ids2 else set(more[1])
        dead_blk2 = set(more[2])

    if len(result) < 7:
        damage2, _, _, gain2, healed2 = more[:5]
        return CombatDetail(
            (
                damage + trig_dmg + damage2 + trig_dmg2,
                dead_att0 | dead_att2,
                dead_blk0 | dead_blk2,
                gain + trig_gain + gain2 + trig_gain2,
                healed + healed2,
            ),
            getattr(more, "loss", getattr(result, "loss", LossState())),
            getattr(more, "terminal", False),
            add_deltas(changes, getattr(more, "counter_deltas", {})),
            add_deltas(marked, getattr(more, "damage_deltas", {})),
        )
    ll_gain, ll_heal = result[5], result[6]
    damage2, _, _, gain2, healed2, ll_gain2, ll_heal2 = more
    return CombatDetail(
        (
            damage + trig_dmg + damage2 + trig_dmg2,
            dead_att0 | dead_att2,
            dead_blk0 | dead_blk2,
            gain + trig_gain + gain2 + trig_gain2,
            healed + healed2,
            ll_gain + ll_gain2,
            ll_heal + ll_heal2,
        ),
        getattr(more, "loss", getattr(result, "loss", LossState())),
        getattr(more, "terminal", False),
        add_deltas(changes, getattr(more, "counter_deltas", {})),
        add_deltas(marked, getattr(more, "damage_deltas", {})),
    )
