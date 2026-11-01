"""Bounded entry -> draw -> opposing ping/amass transition (#308/G04).

Staged helper only: it resolves one qualifying creature-entry event through one
draw engine and one opposing draw punisher, returning every supported opponent
ping branch for the caller to score adversarially. It never picks a branch,
never scores one, and never mutates caller state.

The card text exercised here is the real Enduring Innocence / Orcish Bowmasters
text. Simplified creatures in tests are labelled fictional: this reconstructs
mechanics, it does not replay a historical game.

Search integration is deliberately absent from this slice. An earlier draft that
reordered the hand list was withdrawn: changing list order does not demonstrate
that the same-state search compares alternative spell orders, so it was not
evidence of a search-level acceptance case.

Trigger availability is explicit input, never inferred from absence:

* ``EngineAvailability(ENTERED_THIS_LINE, False)`` -- the engine entered during
  the simulated line, so its once-per-turn use is known unused until it fires.
* ``EngineAvailability(..., True)`` -- a known consumed use.
* ``EngineAvailability(PRE_EXISTING)`` -- a pre-existing engine whose use was
  not observed: ``used`` stays ``None`` and the chain abstains.
* A missing mapping entry also abstains instead of reading absence as unused.

Supported resolution order follows the ability resolving completely and then
state-based actions: add the ping to existing marked damage, apply amass to the
single known opposing Army (or create one through the caller's token factory),
then remove dead bodies and apply Enduring Innocence's supported death return
through :func:`arenamcp.persistent_returns.returned_enchantment`.

Opponent choices are enumerated separately: us plus every legally targetable
local creature whose characteristics are known and whose damage/death
consequences are modelled here. The opponent's own permanents are legal targets
too, but damaging your own board cannot improve your position absent a readable
trigger or replacement effect -- any such text abstains the whole chain rather
than silently omitting an option whose value could exceed face damage.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from arenamcp.board_assessment import (
    _Clause,
    _legal_target,
    _protected_from,
    ward_cost,
)
from arenamcp.combat_keywords import has_combat_keyword
from arenamcp.persistent_returns import returned_enchantment
from arenamcp.sacrifice_continuation import UnsupportedContinuation

__all__ = [
    "ENTERED_THIS_LINE",
    "PRE_EXISTING",
    "Branch",
    "Chain",
    "EngineAvailability",
    "Resolution",
    "draw_engine",
    "draw_punisher",
    "resolve",
]

ENTERED_THIS_LINE = "entered_this_line"
PRE_EXISTING = "pre_existing"


@dataclass(frozen=True)
class EngineAvailability:
    """Explicit once-per-turn trigger availability for one draw engine."""

    origin: str
    used: bool | None = None

    def __post_init__(self):
        if self.origin not in (ENTERED_THIS_LINE, PRE_EXISTING):
            raise ValueError(f"unknown engine origin: {self.origin!r}")
        if self.origin == ENTERED_THIS_LINE and self.used is None:
            raise ValueError("an engine that entered this line has a known use state")

    @classmethod
    def entered_unused(cls) -> EngineAvailability:
        return cls(ENTERED_THIS_LINE, False)

    @classmethod
    def entered_used(cls) -> EngineAvailability:
        return cls(ENTERED_THIS_LINE, True)

    @classmethod
    def pre_existing_unknown(cls) -> EngineAvailability:
        return cls(PRE_EXISTING)

    @classmethod
    def pre_existing_used(cls) -> EngineAvailability:
        return cls(PRE_EXISTING, True)


@dataclass(frozen=True)
class Chain:
    """The one resolved entry -> draw -> ping chain."""

    engine_id: int
    punisher_id: int | None
    entering_ids: tuple[int, ...]
    draws: int
    once: bool


@dataclass(frozen=True)
class Branch:
    """One supported opponent ping choice and its post-resolution public state."""

    target: int | None  # None means us (the local player)
    ours: tuple[dict, ...]
    theirs: tuple[dict, ...]
    persistent: tuple[dict, ...]
    our_life: int
    opp_life: int
    draws: int
    army_id: int | None
    army_counters: int
    # Availability after this branch resolves. A dead-and-returned engine is a
    # fresh object whose once-per-turn ability has not been used yet.
    used_after: frozenset[int]
    died: tuple[int, ...] = ()
    returned: tuple[int, ...] = ()


@dataclass(frozen=True)
class Resolution:
    chain: Chain | None
    branches: tuple[Branch, ...]


_DRAW_ENTRY = re.compile(
    r"whenever one or more other creatures you control with power (\d+) or less enter\.?,? draw a card\.?",
    re.I,
)
_ONCE = re.compile(r"this ability triggers only once (?:each|per) turn", re.I)
_RETURN_SENTENCE = re.compile(
    r"when [^.\n]* dies, if it was a creature, return it to the battlefield under its owner's control\.\s+"
    r"it's an enchantment\.?",
    re.I,
)
# A trigger that could fire on OUR draw while controlled by the opponent.
_THEIR_DRAW_TRIGGER = re.compile(
    r"\bwhen(?:ever)?\b[^.\n]*\b(?:opponents?|players?)\b[^.\n]*\bdraws?\b", re.I
)
# A payoff on OUR side that would fire on OUR draw.
_OUR_DRAW_TRIGGER = re.compile(r"\bwhen(?:ever)?\b[^.\n]*\byou draw\b", re.I)
_DRAW_REPLACEMENT = re.compile(
    r"\bwould draw\b|\binstead\b[^.\n]*\bdraw\b|\bcan(?:'t|not) draw\b|\bskip(?:s|ped)? [^.\n]*draw\b",
    re.I,
)
_TRIGGER_DOUBLING = re.compile(
    r"\btrigger(?:s|ed)? (?:an )?additional time\b|\btriggers twice\b|\btwice that many\b", re.I
)
# A permanent entering under either player's control can fire entry payoffs.
# The punisher's own "when this enters" clause names the permanent, so it does
# not match these generic nouns.
_ENTRY_TRIGGER = re.compile(
    r"\bwhen(?:ever)?\b[^.\n]*\b(?:creatures?|permanents?|tokens?|arm(?:y|ies))\b"
    r"[^.\n]*\benters?\b",
    re.I,
)
_CONTROL_SCOPE = re.compile(r"\b(?:you control|under your control)\b", re.I)
_OPPONENT_SCOPE = re.compile(
    r"\bunder (?:an |the )?opponent(?:'s)? control\b|\bunder the control of an opponent\b|"
    r"\bopponent(?:'s)? creatures?\b|\ban opponent controls?\b",
    re.I,
)
_DAMAGE_MODIFIER = re.compile(
    r"\bprevent(?:s|ed)?\b[^.\n]*\bdamage\b|\bdamage\b[^.\n]*\bprevented\b|"
    r"\bdouble\b[^.\n]*\bdamage\b|\bdamage\b[^.\n]*\bdoubled?\b|"
    r"\breplace(?:s|d)?\b[^.\n]*\bdamage\b|\binstead\b[^.\n]*\bdamage\b|\bdamage\b[^.\n]*\binstead\b",
    re.I,
)
_TARGET_RESTRICTION = re.compile(
    r"\bcan(?:'t|not) be targeted\b|\bhexproof\b|\bshroud\b|\bprotection from\b", re.I
)
# Any readable death / leaves-play / dealt-damage trigger abstains rather than deciding whether it observes this death.
_DEATH_TRIGGER = re.compile(
    r"\bwhen(?:ever)?\b[^.\n]*\b(?:dies|die|leaves the battlefield|is dealt damage|are dealt damage)\b", re.I
)


def _text(card: dict) -> str:
    return re.sub(r"<[^>]*>", "", str(card.get("oracle_text") or "")).replace("\u2019", "'")


def _card_of(obj: dict) -> dict:
    return obj.get("_card") or obj


def _name(obj: dict) -> str:
    return str(_card_of(obj).get("name") or obj.get("name") or "permanent")


def _without_return(text: str) -> str:
    return _RETURN_SENTENCE.sub("", text)


def _draw_entry(card: dict):
    matches = list(_DRAW_ENTRY.finditer(_text(card)))
    if len(matches) > 1:
        raise UnsupportedContinuation(f"multiple entry-draw clauses unmodelled: {_name(card)}")
    return matches[0] if matches else None


def _punisher(card: dict):
    name = str(card.get("name") or "")
    if not name:
        return None
    pattern = (
        r"whenever an opponent draws a card"
        r"(?: except the first one they draw in each of their draw steps)?,\s*"
        r"(?:this creature|" + re.escape(name) + r") deals (?P<damage>\d+) damage to any target\. "
        r"then amass orcs (?P<amass>\d+)\."
    )
    return re.search(pattern, _text(card), re.I)


def draw_engine(card: dict) -> tuple[int, bool] | None:
    """(power cap, once-per-turn) of a supported entry-draw engine, else ``None``."""
    match = _draw_entry(card)
    if match is None:
        return None
    return int(match.group(1)), bool(_ONCE.search(_text(card)))


def draw_punisher(card: dict) -> bool:
    """Whether ``card`` is the supported opposing draw punisher."""
    return _punisher(card) is not None


def _is_army(body: dict) -> bool:
    """Use actual subtype/type evidence; a name suffix alone is not evidence."""
    card = _card_of(body)
    raw_subtypes = card.get("subtypes")
    if raw_subtypes is not None:
        subtypes = [str(value).lower().removeprefix("subtype_") for value in raw_subtypes]
        return "army" in subtypes
    type_line = str(card.get("type_line") or "")
    if "\u2014" in type_line:
        suffix = type_line.split("\u2014", 1)[1]
        return "army" in {word.lower() for word in suffix.split()}
    return bool(re.search(r"\barmy\b", type_line, re.I))


def _merge_unique(*groups: Sequence[dict]) -> list[dict]:
    seen: dict[int, dict] = {}
    merged: list[dict] = []
    for group in groups:
        for obj in group:
            iid = obj.get("instance_id")
            if type(iid) is not int:
                raise UnsupportedContinuation(
                    f"permanent without a known instance id: {obj.get('name', 'permanent')}"
                )
            prior = seen.get(iid)
            if prior is None:
                seen[iid] = obj
                merged.append(obj)
                continue
            prior_card = _card_of(prior)
            card = _card_of(obj)
            if str(prior_card.get("name") or "") != str(card.get("name") or "") or str(
                prior_card.get("oracle_text") or ""
            ) != str(card.get("oracle_text") or ""):
                raise UnsupportedContinuation(f"conflicting public data for permanent id {iid}")
    return merged


def _seat(cards: Sequence[dict], side: str) -> int | None:
    seats = {_card_of(obj).get("controller_seat_id") for obj in cards}
    if not seats:
        return None
    if len(seats) != 1 or type(next(iter(seats))) is not int:
        raise UnsupportedContinuation(f"{side} permanents' controllers are unknown or mismatched")
    return next(iter(seats))


def _check_flags(obj: dict) -> None:
    card = _card_of(obj)
    if card.get("is_phased_out") or card.get("face_down") or card.get("is_copy"):
        raise UnsupportedContinuation(f"permanent characteristics unmodelled: {_name(obj)}")


def _engines(cards: Sequence[dict]) -> list[tuple[int, int, bool]]:
    found: list[tuple[int, int, bool]] = []
    for obj in cards:
        card = _card_of(obj)
        match = _draw_entry(card)
        if match is None:
            continue
        iid = obj.get("instance_id")
        if type(iid) is not int:
            raise UnsupportedContinuation(f"draw engine without a known instance id: {_name(obj)}")
        _check_flags(obj)
        found.append((iid, int(match.group(1)), bool(_ONCE.search(_text(card)))))
    return found


def _punishers(cards: Sequence[dict]) -> tuple[dict, re.Match] | None:
    found: list[tuple[dict, re.Match]] = []
    for obj in cards:
        card = _card_of(obj)
        triggers = list(_THEIR_DRAW_TRIGGER.finditer(_text(card)))
        if not triggers:
            continue
        match = _punisher(card)
        if match is None or len(triggers) != 1:
            raise UnsupportedContinuation(f"opposing draw-trigger response unmodelled: {_name(obj)}")
        _check_flags(obj)
        found.append((obj, match))
    if len(found) > 1:
        raise UnsupportedContinuation("multiple opposing draw punishers unmodelled")
    return found[0] if found else None


def _check_draw_event(our_cards: Sequence[dict], their_cards: Sequence[dict]) -> None:
    for obj in our_cards:
        text = _text(_card_of(obj))
        if _OUR_DRAW_TRIGGER.search(text):
            raise UnsupportedContinuation(f"our draw payoff unmodelled: {_name(obj)}")
    # A doubler on either side changes how many times the draw trigger resolves.
    for obj in (*our_cards, *their_cards):
        if _TRIGGER_DOUBLING.search(_text(_card_of(obj))):
            raise UnsupportedContinuation(f"trigger doubling unmodelled: {_name(obj)}")
    for obj in (*our_cards, *their_cards):
        if _DRAW_REPLACEMENT.search(_text(_card_of(obj))):
            raise UnsupportedContinuation(f"draw replacement unmodelled: {_name(obj)}")


def _entry_clauses(card: dict):
    """Entry-trigger clauses, with the supported engine draw trigger removed."""
    text = _DRAW_ENTRY.sub("", _text(card))
    for match in _ENTRY_TRIGGER.finditer(text):
        start = max(text.rfind(".", 0, match.start()), text.rfind("\n", 0, match.start())) + 1
        ends = [pos for pos in (text.find(".", match.end()), text.find("\n", match.end())) if pos != -1]
        yield text[start : min(ends) if ends else len(text)]


def _entry_fires_for_opponent(clause: str, side: str) -> bool:
    """Whether an entry clause observes a creature entering under their control."""
    opponent_scoped = bool(_OPPONENT_SCOPE.search(clause))
    controller_scoped = bool(_CONTROL_SCOPE.search(clause))
    if opponent_scoped and controller_scoped:
        return True
    if opponent_scoped:
        return side == "ours"
    if controller_scoped:
        return side == "theirs"
    return True


def _check_entry_triggers(our_cards: Sequence[dict], their_cards: Sequence[dict]) -> None:
    """A new Army entering can fire entry payoffs on either board."""
    for side, cards in (("ours", our_cards), ("theirs", their_cards)):
        for obj in cards:
            for clause in _entry_clauses(_card_of(obj)):
                if _entry_fires_for_opponent(clause, side):
                    raise UnsupportedContinuation(f"entry trigger unmodelled: {_name(obj)}")


def _check_punisher_keywords(obj: dict) -> None:
    """A granted deathtouch/lifelink source changes the ping's consequences."""
    card = _card_of(obj)
    keywords = {str(value).lower() for value in (obj.get("keywords") or ())}
    keywords |= {str(value).lower() for value in (card.get("keywords") or ())}
    granted = {str(value).lower() for value in (obj.get("granted_abilities") or ())}
    granted |= {str(value).lower() for value in (card.get("granted_abilities") or ())}
    for keyword in ("deathtouch", "lifelink"):
        if (
            keyword in keywords
            or has_combat_keyword(obj, keyword)
            or any(keyword in value for value in granted)
        ):
            raise UnsupportedContinuation(f"punisher's {keyword} unmodelled")


def _check_damage_modifiers(our_cards: Sequence[dict], their_cards: Sequence[dict]) -> None:
    for obj in (*our_cards, *their_cards):
        if _DAMAGE_MODIFIER.search(_text(_card_of(obj))):
            raise UnsupportedContinuation(f"damage prevention/replacement unmodelled: {_name(obj)}")


def _check_deaths(all_cards: Sequence[dict]) -> None:
    for obj in all_cards:
        text = _without_return(_text(_card_of(obj)))
        if _DEATH_TRIGGER.search(text):
            raise UnsupportedContinuation(f"death/leaves-play trigger unmodelled: {_name(obj)}")


def _by_id(bodies: Sequence[dict]) -> dict[int, dict]:
    found: dict[int, dict] = {}
    for body in bodies:
        iid = body.get("instance_id")
        if type(iid) is not int:
            raise UnsupportedContinuation(f"battlefield body without a known instance id: {_name(body)}")
        if iid in found:
            raise UnsupportedContinuation(f"duplicate battlefield instance id {iid}")
        found[iid] = body
    return found


def _qualifying(
    entering: Sequence[dict],
    ours_by_id: Mapping[int, dict],
    engine_iid: int,
    limit: int,
    local_seat: int,
) -> tuple[int, ...]:
    ids: list[int] = []
    for body in entering:
        card = _card_of(body)
        iid = body.get("instance_id")
        if type(iid) is not int:
            raise UnsupportedContinuation(f"entering creature without a known instance id: {_name(body)}")
        if iid == engine_iid:
            continue  # "other creatures"
        if iid in ids:
            raise UnsupportedContinuation(f"duplicate entering instance id {iid}")
        current = ours_by_id.get(iid)
        if current is None:
            raise UnsupportedContinuation(f"entering creature is not on the battlefield: {_name(body)}")
        _check_flags(body)
        power = body.get("power")
        toughness = body.get("toughness")
        if type(power) is not int or type(toughness) is not int or toughness <= 0:
            raise UnsupportedContinuation(f"entering creature characteristics unknown: {_name(body)}")
        if current.get("power") != power or current.get("toughness") != toughness:
            raise UnsupportedContinuation(f"conflicting public data for entering creature id {iid}")
        seat = card.get("controller_seat_id")
        if seat != local_seat:
            raise UnsupportedContinuation(f"entering creature controller unknown/mismatched: {_name(body)}")
        if power <= limit:
            ids.append(iid)
    return tuple(ids)


def _targets(punisher_card: dict, damage: int, ours: Sequence[dict]) -> list[int | None]:
    clause = _Clause("damage", damage, f"deals {damage} damage to any target", True)
    targets: list[int | None] = [None]
    for body in ours:
        iid = body.get("instance_id")
        power = body.get("power")
        toughness = body.get("toughness")
        if type(iid) is not int or type(power) is not int or type(toughness) is not int or toughness <= 0:
            raise UnsupportedContinuation(f"ping target characteristics unknown: {_name(body)}")
        ward = ward_cost(body)
        legal = _legal_target(clause, body, attacking=False, ward_mana=ward) and not _protected_from(
            body, punisher_card
        )
        if not legal:
            continue  # legally untargetable/protected targets are sound omissions
        if ward > 0:
            raise UnsupportedContinuation(f"ward payment unmodelled for ping target: {_name(body)}")
        if _TARGET_RESTRICTION.search(_text(_card_of(body))):
            raise UnsupportedContinuation(f"ping target restriction unmodelled: {_name(body)}")
        targets.append(iid)
    return targets


def _dying(
    targets: Sequence[int | None], ours_by_id: Mapping[int, dict], damage: int
) -> list[tuple[int, int]]:
    dying: list[tuple[int, int]] = []
    for iid in targets:
        if iid is None:
            continue
        body = ours_by_id[iid]
        marked = body.get("_combat_damage", 0)
        if type(marked) is not int or marked < 0:
            raise UnsupportedContinuation(f"marked damage unknown for ping target: {_name(body)}")
        total = marked + damage
        if total >= body["toughness"] and not has_combat_keyword(body, "indestructible"):
            dying.append((iid, total))
    return dying


def _army_index(theirs: Sequence[dict]) -> int | None:
    indexes = [index for index, body in enumerate(theirs) if _is_army(body)]
    if len(indexes) > 1:
        raise UnsupportedContinuation("multiple opposing Armies unmodelled")
    return indexes[0] if indexes else None


def _validate_existing_army(body: dict) -> None:
    power = body.get("power")
    toughness = body.get("toughness")
    if type(power) is not int or type(toughness) is not int or toughness <= 0:
        raise UnsupportedContinuation(f"Army characteristics unknown: {_name(body)}")
    counters = body.get("counters") or {}
    if not isinstance(counters, Mapping):
        raise UnsupportedContinuation(f"Army counters unknown: {_name(body)}")
    for value in counters.values():
        if type(value) is not int or value < 0:
            raise UnsupportedContinuation(f"Army counters unknown: {_name(body)}")


def _validate_new_army(token: dict, reserved_ids: set[int], opponent_seat: int | None) -> None:
    if not isinstance(token, dict):
        raise UnsupportedContinuation("new Army token factory did not return a body")
    iid = token.get("instance_id")
    if type(iid) is not int or iid in reserved_ids:
        raise UnsupportedContinuation(f"new Army token id conflicts with a known permanent id {iid!r}")
    _check_flags(token)
    card = _card_of(token)
    if not _is_army(token):
        raise UnsupportedContinuation(f"new Army token factory did not return an Army: {_name(token)}")
    if not any(subtype.lower() == "orc" for subtype in _subtypes_of(token)):
        raise UnsupportedContinuation(f"new Army token factory did not return an Orc Army: {_name(token)}")
    power = token.get("power")
    toughness = token.get("toughness")
    if type(power) is not int or type(toughness) is not int or power != 0 or toughness != 0:
        raise UnsupportedContinuation(f"new Army token must be a base 0/0 Army: {_name(token)}")
    counters = token.get("counters") or {}
    if counters:
        raise UnsupportedContinuation(f"new Army token must start without counters: {_name(token)}")
    seat = card.get("controller_seat_id")
    if type(seat) is not int or (opponent_seat is not None and seat != opponent_seat):
        raise UnsupportedContinuation(f"new Army token controller unknown/mismatched: {_name(token)}")


def _subtypes_of(body: dict) -> list[str]:
    card = _card_of(body)
    raw = card.get("subtypes")
    if raw is not None:
        return [str(value).removeprefix("Subtype_").removeprefix("subtype_") for value in raw]
    type_line = str(card.get("type_line") or "")
    if "\u2014" in type_line:
        return type_line.split("\u2014", 1)[1].split()
    return []


def _with_orc_subtype(body: dict) -> dict:
    """Amass Orcs adds Orc to an existing Army that does not have it."""
    subtypes = _subtypes_of(body)
    if any(subtype.lower() == "orc" for subtype in subtypes):
        return body
    army_at = next((index for index, subtype in enumerate(subtypes) if subtype.lower() == "army"), None)
    if army_at is None:
        updated_subtypes = [*subtypes, "Orc"]
    else:
        updated_subtypes = [*subtypes[:army_at], "Orc", *subtypes[army_at:]]
    updated = {**body, "subtypes": updated_subtypes}
    card = _card_of(body)
    type_line = str(card.get("type_line") or "")
    if "\u2014" in type_line:
        prefix = type_line.split("\u2014", 1)[0].rstrip()
        updated["type_line"] = f"{prefix} \u2014 {' '.join(updated_subtypes)}"
    if card is not body:
        updated_card = {**card, "subtypes": updated_subtypes}
        if "type_line" in updated:
            updated_card["type_line"] = updated["type_line"]
        updated["_card"] = updated_card
    return updated


def _amass(
    theirs_list: Sequence[dict],
    amount: int,
    army_index: int | None,
    new_token: dict | None,
) -> tuple[list[dict], int | None, int]:
    out = list(theirs_list)
    if army_index is not None:
        body = out[army_index]
        grown = _with_orc_subtype(body)
        counters = dict(grown.get("counters") or {})
        total = int(counters.get("+1/+1", 0)) + amount
        counters["+1/+1"] = total
        out[army_index] = {
            **grown,
            "power": grown["power"] + amount,
            "toughness": grown["toughness"] + amount,
            "counters": counters,
        }
        return out, body["instance_id"], total
    assert new_token is not None  # validated by the caller before any branch exists
    grown = {
        **new_token,
        "power": amount,
        "toughness": amount,
        "counters": {"+1/+1": amount},
    }
    out.append(grown)
    return out, grown["instance_id"], amount


def _branch(
    *,
    target: int | None,
    damage: int,
    amass_amount: int,
    army_index: int | None,
    new_token: dict | None,
    ours_list: Sequence[dict],
    theirs_list: Sequence[dict],
    persistent_list: Sequence[dict],
    our_life: int,
    opp_life: int,
    engine_iid: int,
    once: bool,
) -> Branch:
    ours_new = list(ours_list)
    theirs_new = list(theirs_list)
    persistent_new = list(persistent_list)
    life = our_life
    died: tuple[int, ...] = ()
    returned: tuple[int, ...] = ()

    # 1. The ping resolves: face damage or marked damage added to the target.
    if target is None:
        life -= damage
    else:
        index = next(i for i, body in enumerate(ours_new) if body["instance_id"] == target)
        body = ours_new[index]
        marked = int(body.get("_combat_damage") or 0) + damage
        ours_new[index] = {**body, "_combat_damage": marked}

    # 2. The same ability's amass resolves before state-based actions.
    army_id: int | None = None
    army_counters = 0
    if amass_amount:
        theirs_new, army_id, army_counters = _amass(theirs_new, amass_amount, army_index, new_token)

    # 3. State-based actions remove dead bodies; the supported engine return applies.
    if target is not None:
        index = next(i for i, body in enumerate(ours_new) if body["instance_id"] == target)
        body = ours_new[index]
        marked = int(body.get("_combat_damage") or 0)
        if marked >= body["toughness"] and not has_combat_keyword(body, "indestructible"):
            ours_new.pop(index)
            died = (target,)
            if target == engine_iid:
                returned_card = returned_enchantment(_card_of(body))
                if returned_card is None:
                    raise UnsupportedContinuation(f"engine death/return unmodelled: {_name(body)}")
                persistent_new.append(returned_card)
                returned = (target,)

    # A returned engine is a fresh object: the old object's consumed use does
    # not carry over to it. A surviving engine keeps its consumed use.
    if once:
        used_after = frozenset() if (target == engine_iid and died) else frozenset({engine_iid})
    else:
        used_after = frozenset()

    return Branch(
        target=target,
        ours=tuple(ours_new),
        theirs=tuple(theirs_new),
        persistent=tuple(persistent_new),
        our_life=life,
        opp_life=opp_life,
        draws=1,
        army_id=army_id,
        army_counters=army_counters,
        used_after=used_after,
        died=died,
        returned=returned,
    )


def resolve(
    *,
    entering: Sequence[dict],
    ours: Sequence[dict],
    theirs: Sequence[dict],
    persistent: Sequence[dict] = (),
    our_life: int,
    opp_life: int,
    engine_use: Mapping[int, EngineAvailability],
    new_army: Callable[[], dict] | None = None,
    our_walkers: Sequence[dict] = (),
    our_permanents: Sequence[dict] = (),
    their_permanents: Sequence[dict] = (),
) -> Resolution:
    """Resolve one entry event through one draw engine and one opposing punisher.

    ``entering`` must already be present in ``ours`` (post-entry public state).
    ``engine_use`` must explicitly cover every once-per-turn engine that could
    trigger; a missing or unknown entry abstains. Branches are returned in a
    stable order (us first, then local creatures in battlefield order) for the
    caller to score adversarially; this helper never chooses one.
    """
    if type(our_life) is not int or type(opp_life) is not int:
        raise UnsupportedContinuation("life totals unknown")

    our_cards = _merge_unique(ours, persistent, our_permanents)
    their_cards = _merge_unique(theirs, their_permanents)
    local_seat = _seat(our_cards, "local")
    if local_seat is None:
        raise UnsupportedContinuation("local controller unknown")
    opponent_seat = _seat(their_cards, "opponent") if their_cards else None

    engines = _engines(our_cards)
    if len(engines) > 1:
        raise UnsupportedContinuation("multiple draw engines unmodelled")
    if not engines:
        return Resolution(None, ())
    engine_iid, limit, once = engines[0]

    ours_by_id = _by_id(ours)
    entering_ids = _qualifying(entering, ours_by_id, engine_iid, limit, local_seat)
    if not entering_ids:
        return Resolution(None, ())

    if once:
        availability = engine_use.get(engine_iid)
        if availability is None:
            raise UnsupportedContinuation(
                f"draw-trigger availability not supplied for engine id {engine_iid}"
            )
        if availability.used is None:
            raise UnsupportedContinuation(f"draw-trigger availability unknown for engine id {engine_iid}")
        if availability.used:
            return Resolution(None, ())

    _check_draw_event(our_cards, their_cards)

    punisher = _punishers(their_cards)
    chain = Chain(
        engine_id=engine_iid,
        punisher_id=punisher[0].get("instance_id") if punisher else None,
        entering_ids=entering_ids,
        draws=1,
        once=once,
    )
    if punisher is None:
        branch = _branch(
            target=None,
            damage=0,
            amass_amount=0,
            army_index=None,
            new_token=None,
            ours_list=ours,
            theirs_list=theirs,
            persistent_list=persistent,
            our_life=our_life,
            opp_life=opp_life,
            engine_iid=engine_iid,
            once=once,
        )
        return Resolution(chain, (branch,))

    punisher_obj, match = punisher
    punisher_card = _card_of(punisher_obj)
    _check_punisher_keywords(punisher_obj)
    damage = int(match.group("damage"))
    amass_amount = int(match.group("amass"))
    if damage <= 0 or amass_amount <= 0:
        raise UnsupportedContinuation(f"opposing draw-trigger amounts unmodelled: {_name(punisher_obj)}")

    _check_damage_modifiers(our_cards, their_cards)
    if our_walkers:
        raise UnsupportedContinuation("planeswalker ping targets unmodelled")

    targets = _targets(punisher_card, damage, ours)
    dying = _dying(targets, ours_by_id, damage)
    if dying:
        _check_deaths((*our_cards, *their_cards))
        for iid, _total in dying:
            body = ours_by_id[iid]
            returned_card = returned_enchantment(_card_of(body))
            if iid == engine_iid:
                if returned_card is None:
                    raise UnsupportedContinuation(f"engine death/return unmodelled: {_name(body)}")
            elif returned_card is not None:
                raise UnsupportedContinuation(f"non-engine death return unmodelled: {_name(body)}")

    army_index = _army_index(theirs)
    new_token: dict | None = None
    reserved_ids = {body["instance_id"] for body in (*ours, *theirs, *persistent)}
    if army_index is not None:
        _validate_existing_army(theirs[army_index])
    else:
        _check_entry_triggers(our_cards, their_cards)
        if new_army is None:
            raise UnsupportedContinuation("creating an Army needs the caller's token factory")
        new_token = new_army()
        _validate_new_army(new_token, reserved_ids, opponent_seat)

    branches = tuple(
        _branch(
            target=target,
            damage=damage,
            amass_amount=amass_amount,
            army_index=army_index,
            new_token=new_token,
            ours_list=ours,
            theirs_list=theirs,
            persistent_list=persistent,
            our_life=our_life,
            opp_life=opp_life,
            engine_iid=engine_iid,
            once=once,
        )
        for target in targets
    )
    return Resolution(chain, branches)
