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
        self, battlefield: list[dict], seat: Any, *, mana: int, untapped_only: bool, treasures: int = 0
    ) -> None:
        self.seat = seat
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


def modelled_attack_trigger(card: dict, line: str) -> bool:
    """Whether an attack-trigger line is played by the attack projections (a grant or an extra combat)."""
    name = str((card.get("_card") or card).get("name") or "")
    line = line.strip()
    return bool(_grant_lines(name, line) or _extra_lines(name, line))


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
    bodies: list[dict], battlefield: list[dict], seat: Any, *, untapped_only: bool, declared: bool
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
        untapped_only=untapped_only, treasures=made,
    )  # fmt: skip
    grants: list[Grant] = []
    for card in side.cards:
        grants += grants_of(card)
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
        extra = extra_combat_of(card)
        if extra is not None:
            body["_atk_extra"] = extra
            notes.append(f"{body.get('name') or 'creature'} (an extra combat)")
    for grant in grants:
        notes.append(f"{grant.source} (attack trigger: +{grant.power}/+{grant.toughness})")
    return notes


def annotated(attackers) -> bool:
    return any(b.get("_atk_pump") or b.get("_atk_grants") or b.get("_atk_extra") for b in attackers)


def attack_bodies(attackers: list[dict], *, declared: bool = False) -> list[dict]:
    """Annotated ``attackers`` with their pumps and met grants applied (copies, the same ids)."""
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
    attackers: list[dict], blockers: list[dict], life: int, once: Round, *, declared: bool = False
) -> tuple:
    """``once`` (a ``_combat_life``-shaped round: damage, dead attackers, dead blockers, attacker
    gain, defender gain) with the attackers' pumps and grants, plus the extra combat it gets: its
    surviving attackers again (all of them untapped by the trigger, the source's vigilance aside)
    into the blockers left alive. Damage and gains add up; the dead sets are the union."""
    fighting = attack_bodies(attackers, declared=declared)
    result = once(fighting, blockers, life)
    extra = extra_combats(attackers, declared=declared)
    if extra is None or not fighting:
        return result
    damage, dead_att, dead_blk, gain, healed = result[:5]
    left = life - damage + healed
    if left <= 0:
        return result
    again = [b for b in fighting if b.get("instance_id") not in dead_att]
    if not extra.untap_all:
        again = [b for b in again if has_combat_keyword(b, "vigilance")]
    if not again:
        return result
    standing = [b for b in blockers if b.get("instance_id") not in dead_blk]
    damage2, dead_att2, dead_blk2, gain2, healed2 = once(again, standing, left)[:5]
    return (
        damage + damage2,
        set(dead_att) | set(dead_att2),
        set(dead_blk) | set(dead_blk2),
        gain + gain2,
        healed + healed2,
    )
