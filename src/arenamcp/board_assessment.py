"""Deterministic, log-derived board assessment: clocks, race, role and lookahead.

Answers, from the same snapshot the planner sees (``server.get_game_state``
shape: ``players``/``turn``/``battlefield``/``hand``), the questions a human
asks before planning a turn:

* **clocks** - how many attacks each side needs to kill the other through the
  other side's best blocks (``combat_solver``), counting flying/reach, menace,
  trample, first strike, deathtouch and printed "can't be blocked";
* **race** - whose killing attack comes first, with turn order;
* **role** - "who's the beatdown": aggressor, defender, race or
  control/stabilize, with a one-line reason;
* **threats** - the opponent's most dangerous permanents and why;
* **mana** - lands, land drops in hand, colours, castability on T/T+1/T+2;
* **lookahead** - deploy the best castable plays each of our next three turns
  (a small knapsack that prefers board presence when defending) and project
  our life if the opponent attacks every turn and we block with what we have;
* **lines** - ``line_search`` compares candidate lines (land, casts and modes,
  attacks, two opponent attacks and a greedy third turn) against that greedy
  projection, which stays the baseline. The best line becomes the lookahead
  and sets dead_in (``dead_in_greedy`` keeps the greedy value), adds the only
  surviving / lethal line flags, the attack posture and the LINES prompt
  facts. While a hand spell castable on T or T+1 does something the search
  can't value (``line_search_moves.unmodelled_effect``), or our own choice or
  effect waits on the stack, those claims are qualified ("best modelled
  line ...; not modelled: X") and force no role; such a cast castable before
  their next attack, or the pending choice, also rules out ALL-IN.
  ARENAMCP_LINE_SEARCH=0, a failed or a truncated search keep the greedy
  facts (with the search switched on, the pending check still applies).

Everything is board-only and deliberately conservative. The opponent's hand,
top-decks, combat tricks, lifelink, cost reductions, engines that add
attackers (token makers) and anthem changes are NOT modelled; they are listed
in ``unknowns`` instead of guessed. No LLM, no I/O; a typical board takes a
few milliseconds.

Example (2026-10-06 FRA game 1, our T12 at 11 life, no creatures, opponent
with four creatures and 7 power): role ``control/stabilize``, their clock 2,
"dead in 2 turns unless we stabilize", lookahead T12 = play Island and cast
Undulating Witness rather than the Murmuring Volume the autopilot chose.
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from itertools import combinations
from types import SimpleNamespace
from typing import Any

from arenamcp.combat_keywords import has_combat_keyword
from arenamcp.combat_solver import _resolve_attacker, _search_blocks, combat_resource_roles
from arenamcp.combat_strategy import _CANT_BLOCK, _rules_text, loyalty
from arenamcp.limited_rules import rules_profile
from arenamcp.mulligan_policy import _land_colors, _mana_value, _pip_matching, hand_card

logger = logging.getLogger(__name__)

ROLE_AGGRESSOR = "aggressor"
ROLE_DEFENDER = "defender"
ROLE_RACE = "race"
ROLE_CONTROL = "control/stabilize"
ROLES = (ROLE_AGGRESSOR, ROLE_DEFENDER, ROLE_RACE, ROLE_CONTROL)

# Attacks simulated for a clock; beyond this the last attack is extrapolated.
_HORIZON = 6
# Clocks longer than this are reported as this value.
_MAX_CLOCK = 20
# Our turns in the lookahead (T, T+1, T+2).
_LOOKAHEAD_TURNS = 3
# Block assignments scored per combat; larger boards use the solver's greedy
# fallback, which keeps one assessment within a few milliseconds.
_MAX_BLOCK_OPTIONS = 2048
# Spells considered together in one turn of the deployment knapsack.
_MAX_SPELLS_PER_TURN = 3

# Plays that neither add to the board nor interact with the opponent's board.
NON_BOARD_ROLES = frozenset({"draw", "selection", "ramp", "cycling"})


# --- small card helpers ------------------------------------------------------


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _types(card: dict) -> str:
    return f"{card.get('type_line') or ''} {' '.join(str(t) for t in card.get('card_types') or [])}".lower()


def _is_creature(card: dict) -> bool:
    return "creature" in _types(card)


def _is_land(card: dict) -> bool:
    return bool(re.search(r"\bland\b", _types(card).replace("cardtype_land", "land")))


def _controller(card: dict) -> int | None:
    return card.get("controller_seat_id") or card.get("owner_seat_id")


def _name(card: dict) -> str:
    return str(card.get("name") or "a card")


def _text(card: dict) -> str:
    """Lower-case rules text without reminder text; the card's name becomes '~'."""
    text = _rules_text(card)
    name = str(card.get("name") or "").lower()
    if name:
        text = text.replace(name, "~")
        short = name.split(",")[0].strip()
        if short and short != name:
            text = text.replace(short, "~")
    return text


def _seats(state: dict) -> tuple[int | None, int | None]:
    players = [p for p in state.get("players") or [] if isinstance(p, dict)]
    local = state.get("local_seat_id") or next((p.get("seat_id") for p in players if p.get("is_local")), None)
    opponent = state.get("opponent_seat_id") or next(
        (p.get("seat_id") for p in players if p.get("seat_id") is not None and p.get("seat_id") != local),
        None,
    )
    return local, opponent


def _life(state: dict, seat: int | None) -> int:
    for player in state.get("players") or []:
        if isinstance(player, dict) and player.get("seat_id") == seat:
            value = _int(player.get("life_total"))
            return 20 if value is None else value
    return 20


def _player(state: dict, seat: int | None) -> dict:
    return next(
        (p for p in state.get("players") or [] if isinstance(p, dict) and p.get("seat_id") == seat),
        {},
    )


# --- card roles ----------------------------------------------------------------

_TARGET_PHRASE = r"target (?P<what>(?:[a-z-]+,? (?:or )?)*?(?:creature|permanent|planeswalker)s?)"
_DESTROY = re.compile(r"\b(?:destroy|exile) " + _TARGET_PHRASE)
_DAMAGE = re.compile(
    r"\bdeals? (?P<n>\d+|x) damage to (?:target|any target|each creature|each (?:other )?creature"
    r"|each creature (?:and planeswalker )?(?:your opponents|an opponent) controls?|up to)"
)
_SHRINK = re.compile(r"\btarget creature (?:an opponent controls )?gets? [-−–](?P<p>\d+|x)/[-−–](?P<n>\d+|x)")
_FIGHT = re.compile(r"\bfights? (?:target|another target|up to one target)")
_BOUNCE = re.compile(
    r"\breturn (?:up to one )?target (?:[a-z-]+ )*?(?:creature|nonland permanent|permanent)[^.]*?\bto (?:its|their) owner'?s?'? hands?"
)
_COUNTER = re.compile(r"\bcounter target\b")
_DRAW = re.compile(r"\bdraws? (?:a|one|two|three|four|five|x|\d+) cards?\b")
_SELECTION = re.compile(r"\b(?:surveil|scry) \d|\blook at the top\b|\bmill (?:a|one|two|three|\d+) cards?\b")
_MANA_ABILITY = re.compile(r"\{o?t\}[^:\n]*:\s*add\b")
_LAND_SEARCH = re.compile(
    r"search your library for (?:a|up to \w+) (?:basic )?lands?[^.]*?\bonto the battlefield"
)
_LIFEGAIN = re.compile(r"\byou gain (\d+) life\b")
_CYCLING = re.compile(r"\b(?:basic )?(?:land|plains|island|swamp|mountain|forest|wastes)?cycling\b")
_PUMP = re.compile(
    r"\btarget creature (?:you control )?gets? \+\d|\benchant creature\b|\+1/\+1 counters? on target"
)


_PERMANENT_TYPES = ("artifact", "enchantment", "creature", "planeswalker", "battle", "land")
_QUOTED = re.compile(r"\"[^\"]*\"|“[^”]*”")
# "When ~ enter": the card's own enters trigger under a plural name (The Notary Hobbits).
_ENTERS_TRIGGER = re.compile(r"(?:when|whenever)\b[^.:]*?\b(?:enters|(?<=~ )enter)\b")
# The subject of an enters trigger ("when <subject> enters"), and the "or another ..." that lets
# the card's own trigger fire again for other permanents.
_ENTERS_SUBJECT = re.compile(r"(?:when|whenever) (?P<subject>[^.:,]*?) (?:enters|(?<=~ )enter)\b")
_OR_ANOTHER = re.compile(r"\s+or (?:another|one or more other)\b.*$")
_CHAPTER_ONE = re.compile(r"i(?:\s*,\s*ii)?(?:\s*,\s*iii)?\s*[—–]")
_TRIGGER_WORDS = re.compile(r"(?:when|whenever|at|if|as long as)\b")
# Player damage and life loss (the line search's face variants; line_guard's "unmodelled finisher").
_FACE_ALT = re.compile(
    r"\bdeals? (?P<n>\d+) damage to (?:any (?:other )?target|target (?:player|opponent)"
    r"(?: or (?:planeswalker|battle))?|target creature or player)\b"
    r"|\btarget (?:player|opponent) loses (?P<m>\d+) life\b"
)
_FACE_EACH = re.compile(
    r"\bdeals? (?P<n>\d+) damage to each (?:opponent|player)\b|\beach opponent loses (?P<m>\d+) life\b"
)
_PLAYER_HARM = re.compile(
    r"\bdamage to (?:any (?:other )?target|target (?:player|opponent)|each (?:opponent|player)"
    r"|target creature or player|that player|its controller)\b|\b(?:player|opponent)s? loses? (?:\d+|x) life\b"
)
_LIFE_LOSS = re.compile(r"\byou lose (\d+) life\b")
_EXTRA_COST = re.compile(r"\bas an additional cost to cast this spell,(?P<what>[^.\n]*)")


def _activated(line: str) -> bool:
    """An activated ability's line ("{2}, {T}: ...", "Discard this card: ...", "[-3]: ...")."""
    head, colon, _rest = line.partition(":")
    return bool(colon) and "." not in head and len(head) < 90 and not _TRIGGER_WORDS.match(head.strip())


def own_subject(subject: str, name: str = "") -> bool:
    """A trigger's subject is the card itself: '~', 'this creature' (any 'this <type>'),
    Arena's 'CARDNAME', the first word of a legendary's name, each optionally followed by
    'or another ...' (that part fires for other permanents too)."""
    subject = _OR_ANOTHER.sub("", " ".join(str(subject).lower().split()))
    first = str(name or "").lower().split(",")[0].split()[:1]
    return (
        subject in ("~", "cardname")
        or bool(re.fullmatch(r"this [a-z]+", subject))
        or bool(first and len(first[0]) > 2 and subject == first[0])
    )


def own_enters_trigger(line: str, name: str = "") -> bool:
    """``line`` (lower-case rules text) starts with the card's own enters trigger
    ("When this creature enters, ..."), not one on another permanent entering
    ("Whenever another creature you control enters, ...")."""
    match = _ENTERS_SUBJECT.match(line)
    return match is not None and own_subject(match.group("subject"), name)


def _cast_text(card: dict) -> str:
    """The rules text that acts when the card is cast (lower case, name as '~').

    A real card (one with a type line) never acts through its activated
    abilities ("{3}{R}: Exile ...", "{1}{W}, Discard this card: ...") or the
    abilities it grants in quotes; a permanent acts only through its own
    enters triggers (with their modes; a trigger on another permanent
    entering, "Whenever another creature you control enters", does not act
    when it is cast) and a Saga's chapter I, and keeps its additional-cost
    line. Text without types (a mode, an ability's effect) is read whole.
    Arena's repeated formatting variants are collapsed.
    """
    text = _text(card)
    types = _types(card)
    if not types.strip():
        return text
    permanent = not re.search(r"\b(?:instant|sorcery)\b", types) and any(t in types for t in _PERMANENT_TYPES)
    kept: list[str] = []
    seen: set[str] = set()
    in_trigger = False
    for raw in _QUOTED.sub("", text).splitlines():
        line = " ".join(raw.split())
        if line in seen:
            continue  # Arena repeats each rules line in up to three formats
        seen.add(line)
        if line.startswith("•"):
            if in_trigger or not permanent:
                kept.append(line)
            continue
        in_trigger = False
        if not line or _activated(line):
            continue
        if permanent:
            if line.startswith("as an additional cost to cast this spell"):
                kept.append(line)  # a casting cost, not an ability
                continue
            if _ENTERS_TRIGGER.match(line):
                if not own_enters_trigger(line, str(card.get("name") or "")):
                    continue
            elif not _CHAPTER_ONE.match(line):
                continue
            in_trigger = True
        kept.append(line)
    return "\n".join(kept)


@dataclass(frozen=True)
class _Clause:
    """One way a card's cast text removes a creature: kind, amount and the target words."""

    kind: str  # destroy / damage / shrink / fight / bounce
    limit: int | None  # damage or -N toughness
    words: str  # the clause from the verb to the end of its sentence
    targeted: bool


def _clause_words(text: str, start: int) -> str:
    end = re.search(r"[.;\n]", text[start:])
    return text[start : start + end.start()] if end else text[start:]


def _ours_only(words: str) -> bool:
    """'target creature you control': our own creature, not removal."""
    return bool(
        re.search(
            r"\b(?:creature|permanent|planeswalker)s?(?: or (?:creature|planeswalker))? you control\b", words
        )
    )


def _removal_clauses(card: dict) -> list[_Clause]:
    """Every clause of the card's cast text that removes an opposing creature."""
    text = _cast_text(card)
    found: list[_Clause] = []

    def add(kind: str, limit: int | None, start: int) -> None:
        words = _clause_words(text, start)
        if not _ours_only(words):
            found.append(_Clause(kind, limit, words, "target" in words))

    for match in _DESTROY.finditer(text):
        what = match.group("what")
        if "noncreature" not in what and ("creature" in what or "permanent" in what):
            add("destroy", None, match.start())
    for match in _DAMAGE.finditer(text):
        amount = _int(match.group("n"))
        add("damage", 3 if amount is None else amount, match.start())
    for match in _SHRINK.finditer(text):
        amount = _int(match.group("n"))
        if amount:
            add("shrink", amount, match.start())
    for match in _FIGHT.finditer(text):
        add("fight", None, match.start())
    for match in _BOUNCE.finditer(text):
        add("bounce", None, match.start())
    return found


def removal_reach(card: dict) -> tuple[str, int | None] | None:
    """How a spell's text kills an opposing creature: (kind, toughness limit).

    ``("destroy", None)`` kills any creature; ``("damage", 3)`` kills
    toughness <= 3; ``("bounce", None)`` removes it for a turn. Returns None
    when the text has no recognisable creature removal (noncreature-only
    removal such as "destroy target noncreature permanent" is not creature
    removal, nor is "exile target creature you control"). Only the text that
    acts on casting counts (``_cast_text``): a permanent's activated or granted
    abilities are not removal. Target restrictions ("with toughness 4 or
    greater", hexproof, fight needing our creature) are ``_kills``' job.
    """
    clauses = _removal_clauses(card)
    for kind in ("destroy", "damage", "shrink", "fight", "bounce"):
        limits = [c.limit for c in clauses if c.kind == kind]
        if limits:
            # Modal burn: the strongest mode (Fulminous Forte: 1 to each, or 5 to one).
            return kind, (max(limit or 0 for limit in limits) if kind in ("damage", "shrink") else None)
    return None


def face_damage(card: dict) -> tuple[int, int]:
    """(damage to an opponent instead of a creature, damage to each opponent as well) from cast text.

    "deals 2 damage to any target" can go face (the first); "Exile target
    creature. ~ deals 1 damage to each opponent" always does (the second).
    "target player loses N life" counts as the first, "each opponent loses N
    life" as the second.
    """
    text = _cast_text(card)
    alt = max((_int(m.group("n") or m.group("m")) or 0 for m in _FACE_ALT.finditer(text)), default=0)
    each = _per_mode(
        text, lambda line: sum(_int(m.group("n") or m.group("m")) or 0 for m in _FACE_EACH.finditer(line))
    )
    return alt, each


def _per_mode(text: str, amount: Any) -> int:
    """``amount`` summed over the lines outside 'choose one' bullets, plus the largest bullet's."""
    lines = text.splitlines()
    base = sum(amount(line) for line in lines if not line.startswith("•"))
    return base + max((amount(line) for line in lines if line.startswith("•")), default=0)


def harms_players(card: dict) -> bool:
    """The card's text can damage a player or make one lose life (activated and triggered text included)."""
    return bool(_PLAYER_HARM.search(_text(card)))


def life_loss(card: dict) -> int:
    """Life we lose by casting it: "You lose N life." in its cast text (the costliest mode of a
    modal card), plus N life paid as an additional cost."""
    text = _cast_text(card)
    return _per_mode(text, lambda line: sum(int(m.group(1)) for m in _LIFE_LOSS.finditer(line))) + (
        extra_life_cost(card)
    )


def extra_life_cost(card: dict) -> int:
    """Life an additional cost asks for ("As an additional cost to cast this spell, pay 3 life")."""
    extra = _EXTRA_COST.search(_cast_text(card))
    paid = re.search(r"\bpay (\d+) life\b", extra.group("what")) if extra else None
    return int(paid.group(1)) if paid else 0


def extra_mana_cost(card: dict) -> int:
    """Generic mana an additional cost asks for ("As an additional cost ..., ... or pay {3}").

    A sacrifice / behold alternative is not modelled: the mana is counted.
    """
    extra = _EXTRA_COST.search(_cast_text(card))
    if not extra:
        return 0
    paid = re.search(r"\bpay \{o?(\d+)\}", extra.group("what"))
    return int(paid.group(1)) if paid else 0


def card_role(card: dict) -> str:
    """Strategic role of a card for deployment and the role guard.

    One of: land, creature, planeswalker, removal, bounce, counter, lifegain,
    draw, selection, ramp, pump, other. Board bodies win over their ETB text
    (Archive Arbiter is a creature, not removal).
    """
    types = _types(card)
    if _is_land(card) and "creature" not in types:
        return "land"
    if "creature" in types:
        return "creature"
    if "planeswalker" in types:
        return "planeswalker"
    profile = rules_profile(card)
    if profile.get("unconditional_body"):
        return "creature"
    reach = removal_reach(card)
    if reach is not None:
        return "bounce" if reach[0] == "bounce" else "removal"
    text = _text(card)
    if _COUNTER.search(text):
        return "counter"
    if ("artifact" in types or "enchantment" in types) and _spendable_mana(text):
        return "ramp"
    if _LAND_SEARCH.search(text):
        return "ramp"
    if _DRAW.search(text):
        return "draw"
    if _SELECTION.search(text):
        return "selection"
    if _LIFEGAIN.search(text):
        return "lifegain"
    if _PUMP.search(text):
        return "pump"
    return "other"


def _source_card(state: dict, meta: dict) -> tuple[dict, str]:
    """The option's source card and its zone (hand/battlefield/graveyard/...)."""
    instance_id = meta.get("instanceId")
    if not instance_id:
        return {}, ""
    for zone in ("hand", "battlefield", "graveyard", "exile", "command", "stack"):
        for card in state.get(zone) or []:
            if isinstance(card, dict) and card.get("instance_id") == instance_id:
                return card, zone
    return {}, ""


def option_role(option: Any, state: dict) -> str:
    """Role of one ActionsAvailable option (land/creature/removal/draw/...)."""
    meta = getattr(option, "meta", None) or {}
    option_id = getattr(option, "option_id", "")
    action = str(meta.get("actionType") or "").removeprefix("ActionType_").lower()
    if option_id == "pass" or action == "pass":
        return "pass"
    if action in ("play", "playland"):
        return "land"
    source, zone = _source_card(state, meta)
    if action == "cast":
        return card_role(source) if source else "other"
    if action == "activate":
        # The label carries the ability's own rules text (built from the
        # ability database); the source's text is the fallback.
        label = str(getattr(option, "label", "") or "")
        detail = label.split("[", 1)[1] if "[" in label else ""
        ability = {"name": _name(source), "oracle_text": detail, "type_line": ""}
        lowered = detail.lower()
        if zone == "hand" and _CYCLING.search(lowered):
            return "cycling"
        role = card_role(ability) if detail else "other"
        if role in ("removal", "bounce", "creature", "draw", "selection", "lifegain"):
            return role
        if "cycling" in lowered:
            return "cycling"
        return "pump" if role == "pump" else "other"
    return "other"


# --- creatures in combat -----------------------------------------------------

_GLOBAL_UNBLOCKABLE = re.compile(
    r"creatures you control with power or toughness (\d+) or less can't be blocked"
)
_GLOBAL_ALL_UNBLOCKABLE = re.compile(r"(?:^|\n)\s*(?:other )?creatures you control can't be blocked\.")
_SELF_UNBLOCKABLE = re.compile(r"(?:^|\n)\s*(?:~|this creature|this card)? ?can't be blocked\.\s*(?:$|\n)")
_GLOBAL_HASTE = re.compile(r"(?:^|\n)\s*(?:other )?creatures you control have haste\b")
_CANT_ATTACK = re.compile(r"(?:^|\n)\s*(?:~|this creature)? ?can't attack\b")


def _side_rules(battlefield: list[dict], seat: int | None) -> dict:
    """Static grants from a side's permanents that the snapshot doesn't bake in."""
    rules = {"unblockable_max": None, "all_unblockable": False, "haste": False}
    for card in battlefield:
        if not isinstance(card, dict) or _controller(card) != seat:
            continue
        text = _text(card)
        match = _GLOBAL_UNBLOCKABLE.search(text)
        if match:
            limit = int(match.group(1))
            rules["unblockable_max"] = max(rules["unblockable_max"] or 0, limit)
        if _GLOBAL_ALL_UNBLOCKABLE.search(text):
            rules["all_unblockable"] = True
        if _GLOBAL_HASTE.search(text):
            rules["haste"] = True
    return rules


def _body(
    card: dict, turn: int, rules: dict, *, attached: dict[int, list[dict]] | None = None
) -> dict | None:
    """A solver-ready creature, or None when its power/toughness is unknown."""
    power, toughness = _int(card.get("power")), _int(card.get("toughness"))
    if power is None or toughness is None:
        return None
    text = _text(card)
    haste = rules.get("haste") or has_combat_keyword(card, "haste")
    entered = _int(card.get("turn_entered_battlefield"))
    sick = entered is not None and entered >= 0 and entered >= turn and not haste
    can_attack = not has_combat_keyword(card, "defender") and not _CANT_ATTACK.search(text)
    can_block = not _CANT_BLOCK.search(text)
    for aura in (attached or {}).get(_int(card.get("instance_id")) or -1, []):
        aura_text = _text(aura)
        if "enchanted creature can't attack" in aura_text:
            can_attack = False
        if "enchanted creature can't block" in aura_text or "can't attack or block" in aura_text:
            can_block = False
    # cant_be_blocked: combat_keywords.annotate_cant_be_blocked (own text named
    # by card name, attached Auras/Equipment) when the snapshot was marked.
    unblockable = (
        bool(_SELF_UNBLOCKABLE.search(text))
        or rules.get("all_unblockable")
        or bool(card.get("cant_be_blocked"))
    )
    limit = rules.get("unblockable_max")
    if limit is not None and (power <= limit or toughness <= limit):
        unblockable = True
    return {
        "instance_id": _int(card.get("instance_id")) or id(card),
        "name": _name(card),
        "power": max(0, power),
        "toughness": max(0, toughness),
        "oracle_text": card.get("oracle_text") or "",
        "keywords": list(card.get("keywords") or []),
        "type_line": card.get("type_line") or "",
        "is_token": "token" in str(card.get("object_kind") or "").lower() or "token" in _types(card),
        "_tapped": bool(card.get("is_tapped")),
        "_attacking": bool(card.get("is_attacking")),
        "_sick": sick,
        "_can_attack": can_attack,
        "_can_block": can_block,
        "_unblockable": bool(unblockable),
        "_card": card,
    }


def _flying(body: dict) -> bool:
    return has_combat_keyword(body, "flying")


def _reach_or_flying(body: dict) -> bool:
    return has_combat_keyword(body, "flying") or has_combat_keyword(body, "reach")


def _combat(attackers: list[dict], blockers: list[dict], life: int) -> tuple[int, set[int], set[int]]:
    """One combat with the defender's best blocks: (damage, dead attackers, dead blockers)."""
    if not attackers:
        return 0, set(), set()
    blockers = [b for b in blockers if b["_can_block"]]
    groups: dict[int, list[dict]] = {}
    if blockers:
        allowed = None
        if any(a["_unblockable"] for a in attackers):
            blockable = {a["instance_id"] for a in attackers if not a["_unblockable"]}
            allowed = {b["instance_id"]: set(blockable) for b in blockers}
        plan = _search_blocks(
            attackers,
            blockers,
            max(1, life),
            blocker_allowed_attackers=allowed,
            max_options=_MAX_BLOCK_OPTIONS,
        )
        by_id = {b["instance_id"]: b for b in blockers}
        for blocker_id, attacker_id in (plan.assignments if plan else {}).items():
            if blocker_id in by_id:
                groups.setdefault(attacker_id, []).append(by_id[blocker_id])
    damage, dead_attackers, dead_blockers = 0, set(), set()
    for attacker in attackers:
        group = groups.get(attacker["instance_id"], [])
        outcome = _resolve_attacker(attacker, group)
        damage += outcome.damage_through
        if group and outcome.attacker_died:
            dead_attackers.add(attacker["instance_id"])
        dead_blockers.update(dead["instance_id"] for dead in outcome.blockers_died)
    return damage, dead_attackers, dead_blockers


def _attack_round(attackers: list[dict], blockers: list[dict], life: int) -> tuple[int, set[int], set[int]]:
    """The attacker's better of 'everyone attacks' and 'hold back what just dies'.

    Attacking with everything and letting the defender's best blocks answer it
    is the worst case for the defender's life; withholding attackers that only
    die keeps the attacker's board for the next turn. The attacker picks the
    line with more damage (ties keep the creatures home).
    """
    full = _combat(attackers, blockers, life)
    if not full[1]:
        return full
    survivors = [a for a in attackers if a["instance_id"] not in full[1]]
    held = _combat(survivors, blockers, life) if survivors else (0, set(), set())
    return full if full[0] > held[0] else held


def _simulate_attacks(
    attackers: list[dict],
    defenders: list[dict],
    life: int,
    *,
    first_attackers: list[dict] | None,
    first_blockers: list[dict] | None,
    horizon: int = _HORIZON,
) -> tuple[int | None, list[int]]:
    """Attacks needed to kill the defender (None within the horizon) and life after each.

    Board-only: no new creatures on either side. ``first_attackers`` /
    ``first_blockers`` restrict the immediate attack to creatures that can act
    now (untapped, not summoning sick); later attacks use everything alive.
    """
    alive_att = list(attackers)
    alive_def = list(defenders)
    lives: list[int] = []
    defender_start = life
    for index in range(horizon):
        if index == 0 and first_attackers is not None:
            able = [a for a in first_attackers if a["_can_attack"]]
            blockers = list(first_blockers if first_blockers is not None else alive_def)
        else:
            able = [a for a in alive_att if a["_can_attack"]]
            blockers = list(alive_def)
        damage, dead_att, dead_def = _attack_round(able, blockers, life)
        life -= damage
        lives.append(life)
        alive_att = [a for a in alive_att if a["instance_id"] not in dead_att]
        alive_def = [d for d in alive_def if d["instance_id"] not in dead_def]
        if life <= 0:
            return index + 1, lives
        if damage <= 0 and not dead_att and not dead_def and index > 0:
            return None, lives  # a stalled board repeats; no progress
    # Past the horizon the boards have settled: extrapolate the last attack
    # (a lone 1/2 flyer is a 20-turn clock, not "no clock").
    last = (lives[-2] if len(lives) > 1 else defender_start) - lives[-1] if lives else 0
    if lives and last > 0 and len(lives) == horizon:
        return min(_MAX_CLOCK, horizon + math.ceil(lives[-1] / last)), lives
    return None, lives


# --- mana and deployment -----------------------------------------------------


_RESTRICTED_MANA = re.compile(r"\bspend this mana only\b|\bcan't be spent to cast\b")


def _spendable_mana(text: str) -> bool:
    """A mana ability whose mana casts spells from hand (not "Spend this mana only to ...")."""
    return any(_MANA_ABILITY.search(line) and not _RESTRICTED_MANA.search(line) for line in text.splitlines())


def _mana_source(card: dict, turn: int) -> SimpleNamespace | None:
    """A permanent that taps for mana, with its colours (C for colourless).

    A nonland source whose mana can't cast spells from hand (Heartwood
    Crafter, Gideon's Memorial) is not one.
    """
    text = _text(card)
    is_land = _is_land(card) and not _is_creature(card)
    if not is_land and not _spendable_mana(text):
        return None
    if _is_creature(card):
        entered = _int(card.get("turn_entered_battlefield"))
        if entered is not None and entered >= turn and not has_combat_keyword(card, "haste"):
            return None  # summoning-sick mana creature
    colors = set(_land_colors(card)) or {"C"}
    return SimpleNamespace(produces=frozenset(colors), name=_name(card))


def _enters_tapped(card: dict) -> bool:
    return bool(re.search(r"enters (?:the battlefield )?tapped", _text(card)))


@dataclass
class _Spell:
    card: dict
    name: str
    role: str
    mana_value: int  # mana to cast it: printed mana value plus any generic additional cost
    pips: tuple
    has_x: bool
    value: float = 0.0
    uncastable: bool = False  # e.g. Threshold "can't cast this spell unless ..." not met
    # "command": our commander, cast from the command zone (``mana_value`` includes the tax).
    zone: str = "hand"


@dataclass
class TurnProjection:
    """One of our upcoming turns in the lookahead."""

    label: str  # "T", "T+1", "T+2"
    turn: int  # absolute turn number
    mana: int  # mana sources available that turn
    colors: str  # producible colours, e.g. "GU"
    land: str  # land played that turn ("" when none)
    casts: list[str]  # the knapsack's deployment
    castable: list[str]  # every hand spell castable alone that turn
    life_after: int | None = None  # our life after the opponent's following attack
    opponent_creatures_after: int | None = None
    # Producible colours per mana source that turn, before any rock the
    # board-math schedule casts (plan validation adds the plan's own rocks).
    source_colors: list[str] = field(default_factory=list)
    # From the line search's best line (empty/None on the greedy projection):
    attack: list[str] = field(default_factory=list)  # our attackers that turn
    opp_life_after: int | None = None  # their life after our attack
    modes: dict[str, str] = field(default_factory=dict)  # card -> chosen mode
    held: str = ""  # instants used on their following turn
    posture: str = ""  # T only: lethal / attack / hold / either
    cycles: list[str] = field(default_factory=list)  # landcycled cards


@dataclass
class Threat:
    name: str
    instance_id: int | None
    score: float
    why: str


@dataclass
class BoardAssessment:
    """Deterministic facts for the strategic layer (see module docstring)."""

    turn: int
    our_turn: bool
    phase: str
    our_life: int
    opp_life: int
    our_creatures: int
    their_creatures: int
    our_power: int
    their_power: int
    our_hand: int
    their_hand: int | None
    our_lands: int
    their_lands: int
    lands_in_hand: int
    land_drop_available: bool
    colors: str
    missing_colors: str
    our_clock: int | None
    their_clock: int | None
    race: str  # ahead / behind / even / stalled
    race_detail: str
    role: str
    role_reason: str
    lethal_now: bool
    lethal_next_turn: bool
    opp_lethal_on_board: bool
    dead_in: int | None  # opponent attacks until we die, with our best deployment
    our_life_now_attack: int | None  # after an opponent attack already pending now
    card_advantage: int | None
    board_advantage: int
    deck_curve: float | None
    flags: list[str] = field(default_factory=list)
    # No defensive line survives the opponent's next attack, so holding back
    # blockers gains nothing: attack with everything (bug_20261006_180436).
    all_in: bool = False
    threats: list[Threat] = field(default_factory=list)
    lookahead: list[TurnProjection] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    library_count: int | None = None
    elapsed_ms: float = 0.0
    # The line search (``arenamcp.line_search``): its result, kept even when
    # truncated (consumers check ``.truncated``); the top lines, the attack
    # posture and the greedy projection's dead_in it is compared with. Empty
    # when the search is off (ARENAMCP_LINE_SEARCH=0), failed or truncated.
    line_search: Any = field(default=None, repr=False, compare=False)
    lines: list = field(default_factory=list)  # top 5 line_search.Line, best first
    posture: str = ""  # lethal / attack / hold / either
    posture_reason: str = ""
    dead_in_greedy: int | None = None
    search_stats: dict[str, Any] = field(default_factory=dict)  # nodes, combats, ms, bounded, truncated
    # What the line facts can't see: hand spells castable on T or T+1 whose effect the search
    # can't value (``_unmodelled_castable``), and our own pending stack objects (``_our_pending``).
    unmodelled: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    # Commander games: per commander of ours in the command zone, its cost now (tax included)
    # and when the lookahead can cast it (``_commander_facts``); empty elsewhere.
    commander: list[str] = field(default_factory=list)

    @property
    def survival_mode(self) -> bool:
        """Defending, losing the race, or dead within two turns (and no lethal of our own)."""
        if self.lethal_now:
            return False
        if self.opp_lethal_on_board or (self.dead_in is not None and self.dead_in <= 2):
            return True
        under_pressure = self.their_clock is not None and self.their_clock <= _HORIZON
        return under_pressure and (self.role in (ROLE_DEFENDER, ROLE_CONTROL) or self.race == "behind")

    def _clock_text(self, clock: int | None) -> str:
        return "—" if clock is None else str(clock)

    def headline(self) -> str:
        return f"{self.role.upper()} — {self.role_reason}"

    @property
    def plan_turn(self) -> int:
        """Absolute turn that 'T' refers to: this turn if ours, else our next turn."""
        return self.lookahead[0].turn if self.lookahead else turns_label(self.our_turn, self.turn)

    def suggestion(self, index: int) -> str:
        """The board-math deployment for lookahead turn ``index``."""
        if index >= len(self.lookahead):
            return ""
        step = self.lookahead[index]
        bits = []
        lethal = index == 0 and self.lethal_now
        if lethal:
            bits.append("ATTACK FOR LETHAL (their best blocks can't stop it)")
        if step.land:
            bits.append(f"play {step.land}")
        modes = step.modes or {}
        casts = [f"{name} (choose: {modes[name]})" if modes.get(name) else name for name in step.casts]
        if casts or not step.cycles:
            bits.append("cast " + " + ".join(casts) if casts else "no castable board play")
        bits += [f"landcycle {name}" for name in step.cycles]
        # Attack or hold only when the line had attackers to choose from.
        if step.attack and not lethal:
            bits.append("attack with " + ", ".join(step.attack))
        elif index == 0 and self.posture == "hold":
            bits.append("hold back (no attack)")
        text = "; ".join(bits)
        if step.life_after is not None and not (index == 0 and self.lethal_now):
            text += f" (life after their attack: {step.life_after})"
        return text

    def facts_line(self) -> str:
        line = (
            f"they kill us in {self._clock_text(self.their_clock)} attack(s), we kill them in "
            f"{self._clock_text(self.our_clock)} | race {self.race.upper()} ({self.race_detail}) "
            f"| life {self.our_life} vs {self.opp_life}"
        )
        if self.flags:
            line += " | !! " + "; ".join(self.flags)
        return line

    def prompt_block(
        self,
        *,
        this_turn: str | None = None,
        next_turns: str | None = None,
        role_note: str = "",
        with_lines: bool = True,
    ) -> str:
        """Short, decisive facts for per-decision prompts: ROLE, this turn, facts, next.

        ``this_turn`` / ``next_turns`` replace the board-math deployment with a
        validated game-plan step; ``role_note`` explains a plan/role mismatch.
        ``with_lines=False`` leaves out the '  LINES ...' line (a prompt that
        shows the search's lines elsewhere, e.g. above a typed decision's options).
        """
        when = "now" if self.our_turn else "our next turn"
        lines = [f"STRATEGIC ROLE (deterministic board math, recomputed now): {self.headline()}{role_note}"]
        # The search's own T step is the best it can value while a cast it can't value, or our
        # pending choice, could change it.
        modelled = (
            " (best modelled)" if not this_turn and self.lines and (self.unmodelled or self.pending) else ""
        )
        lines.append(f"  THIS TURN (T{self.plan_turn}, {when}): {this_turn or self.suggestion(0)}{modelled}")
        lines.append(f"  FACTS: {self.facts_line()}")
        if next_turns is None:
            next_turns = " | ".join(
                f"T{step.turn}: {self.suggestion(k)}" for k, step in enumerate(self.lookahead[1:], start=1)
            )
        if next_turns:
            lines.append(f"  NEXT: {next_turns}")
        if with_lines and self.line_search is not None and self.lines:
            lines.append("  " + self.lines_line(318))
        if self.commander:
            lines.append("  " + self.commander_line())
        hand = "?" if self.their_hand is None else str(self.their_hand)
        lines.append(
            f"  Board: us {self.our_creatures} creatures/{self.our_power} power vs them "
            f"{self.their_creatures}/{self.their_power} | hand {self.our_hand} vs {hand} | lands "
            f"{self.our_lands}{' + land drop' if self.land_drop_available else ''} vs {self.their_lands}"
        )
        if self.threats:
            lines.append("  Threats: " + "; ".join(f"{t.name} ({t.why})" for t in self.threats[:3]))
        if self.survival_mode:
            lines.append(
                "  Priority: survive first — creatures that block, removal on attackers and life gain "
                "before card draw, mana rocks, cycling or bouncing cheap creatures."
            )
        elif self.role == ROLE_AGGRESSOR:
            lines.append(
                "  Priority: pressure — attack when the attack math is good, spend removal on blockers, "
                "don't durdle."
            )
        else:
            lines.append("  Priority: develop the board on curve; card draw and rocks only with spare mana.")
        return "\n".join(lines)

    def commander_line(self) -> str:
        """'COMMANDER ...': our commanders in the command zone, their cost now and when castable."""
        if not self.commander:
            return ""
        return (
            "COMMANDER (command zone: cast it like a hand card; each cast from there adds {2} to the next): "
            + " | ".join(self.commander)
        )

    def lines_line(self, max_chars: int = 320) -> str:
        """The search's 'LINES ...' prompt line, marking casts it can't value and our pending choice."""
        if self.line_search is None or not self.lines:
            return ""
        return self.line_search.prompt_line(max_chars, unmodelled=self.unmodelled, pending=self.pending)

    def planning_block(self) -> str:
        """Fuller facts for the background strategic plan call."""
        lines = [self.prompt_block().replace("recomputed now", "at plan time")]
        if self.lookahead:
            lines.append(
                "BOARD-MATH PROJECTION (we deploy the best castable plays and block; they attack every turn): "
                + self._lookahead_text()
            )
        if self.line_search is not None and self.lines:
            lines.append(
                "CANDIDATE LINES (2-turn search + greedy third turn; they attack each turn with their "
                "worst-for-us attack; their new cards and tricks are not modelled):"
            )
            lines += [
                f"  {n}. {_candidate_text(line, self.unmodelled)}"
                for n, line in enumerate(self.lines[:5], start=1)
            ]
            if self.pending:
                lines.append(
                    f"  (These lines read the board before our pending {', '.join(self.pending)} resolves.)"
                )
            lines.append("Prefer one of these lines; a deviation needs a concrete card or combat reason.")
        lines.append("MANA BUDGET BY TURN (lands + mana permanents; one land drop per turn from hand):")
        for step in self.lookahead:
            castable = ", ".join(step.castable) or "nothing in hand"
            land = f", play {step.land}" if step.land else ""
            lines.append(
                f"  {step.label} = turn {step.turn}: {step.mana} mana ({step.colors or 'no colours'}){land}; "
                f"castable alone: {castable}"
            )
        if self.deck_curve is not None:
            lines.append(f"Our deck curve: average mana value {self.deck_curve:.1f}.")
        if self.library_count is not None:
            lines.append(f"Our library: {self.library_count} cards.")
        if self.card_advantage is not None:
            lines.append(f"Cards in hand advantage: {self.card_advantage:+d}.")
        if self.unknowns:
            lines.append("Not modelled (unknown, do not assume): " + "; ".join(self.unknowns))
        return "\n".join(lines)

    def _lookahead_text(self) -> str:
        parts = []
        if self.our_life_now_attack is not None:
            parts.append(f"their attack now -> life {self.our_life_now_attack}")
        for step in self.lookahead:
            casts = " + ".join(step.casts) or "nothing"
            land = f"{step.land}, " if step.land else ""
            attack = f", attack with {', '.join(step.attack)}" if step.attack else ""
            life = "" if step.life_after is None else f" -> life {step.life_after}"
            parts.append(f"T{step.turn}: {land}{casts}{attack}{life}")
        return " | ".join(parts)

    def as_payload(self) -> dict[str, Any]:
        """JSON-safe summary for the desktop plan card."""
        return {
            "role": self.role,
            "role_reason": self.role_reason,
            "our_clock": self.our_clock,
            "their_clock": self.their_clock,
            "race": self.race,
            "our_life": self.our_life,
            "opp_life": self.opp_life,
            "flags": list(self.flags),
            "dead_in": self.dead_in,
            "lethal_now": self.lethal_now,
            "lookahead": [
                {"turn": s.turn, "label": s.label, "casts": list(s.casts), "life_after": s.life_after}
                for s in self.lookahead
            ],
            "threats": [{"name": t.name, "why": t.why} for t in self.threats[:3]],
            "lines": [line.as_payload() for line in self.lines[:3]],
            "posture": self.posture,
            "search": dict(self.search_stats),
        }


def _candidate_text(line: Any, unmodelled: Any = ()) -> str:
    """One searched line for the plan prompt: each turn's play, attack and lives, then the outcome.

    A turn casting a card named in ``unmodelled`` (casts the search can't value) is marked
    "(not modelled)" (its only cast) or "(X not modelled)".
    """
    parts = []
    blocks = {"none": "no blocks now", "crackback": "block now without our counterattackers"}
    if line.block in blocks:
        parts.append(blocks[line.block])
    if line.now_life is not None:
        parts.append(f"their attack now -> life {line.now_life}")
    for step in line.steps:
        life = "" if step.life_after is None else f" -> life {step.life_after}"
        opp = "" if step.opp_life_after is None else f", opponent {step.opp_life_after}"
        marks = [name for name in dict.fromkeys(step.casts) if name in unmodelled]
        mark = ""
        if marks:
            mark = (
                " (not modelled)" if list(step.casts) == marks[:1] else f" ({', '.join(marks)} not modelled)"
            )
        parts.append(f"T{step.turn}: {step.text()}{mark}{life}{opp}")
    if line.outcome == "win":
        outcome = f"lethal on T{line.win_turn}"
    elif line.outcome == "dead":
        outcome = f"dead on T{line.dead_turn}"
    else:
        outcome = "alive"
    return " | ".join(parts) + f" => {outcome}" + (" (greedy)" if line.baseline else "")


# --- assessment ---------------------------------------------------------------

_CACHE: OrderedDict[tuple, BoardAssessment] = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_SIZE = 32


def _signature(state: dict) -> tuple:
    from arenamcp.board_model import canonical_phase_step

    turn = state.get("turn") or {}
    # _assess reads phase/step only in log form: 'Main1'/'None' and 'Phase_Main1'/'' share one entry.
    phase, step = canonical_phase_step(turn)
    cards = tuple(
        (
            c.get("instance_id"),
            c.get("name"),
            c.get("power"),
            c.get("toughness"),
            c.get("is_tapped"),
            c.get("is_attacking"),
            _controller(c),
            c.get("turn_entered_battlefield"),
            tuple(c.get("keywords") or ()),
        )
        for c in state.get("battlefield") or []
        if isinstance(c, dict)
    )
    hand = tuple(
        c.get("instance_id") or c.get("name") for c in state.get("hand") or [] if isinstance(c, dict)
    )
    players = tuple(
        (p.get("seat_id"), p.get("life_total"), p.get("lands_played"), p.get("is_local"))
        for p in state.get("players") or []
        if isinstance(p, dict)
    )
    zones = state.get("zones") if isinstance(state.get("zones"), dict) else {}
    # Commander games: the command zone and what our commanders cost now, tax included
    # (Arena's cast action when there is one); empty elsewhere.
    command = tuple(
        (c.get("instance_id"), c.get("name"), c.get("owner_seat_id"), c.get("mana_cost"))
        for c in state.get("command") or []
        if isinstance(c, dict)
    )
    if command:
        from arenamcp.board_model import our_commanders

        command += tuple((c.name, c.cost, c.casts) for c in our_commanders(state))
    casts = state.get("commander_casts") if isinstance(state.get("commander_casts"), dict) else {}
    return (
        state.get("match_id"),
        turn.get("turn_number"),
        turn.get("active_player"),
        phase,
        step,
        players,
        cards,
        hand,
        len(state.get("graveyard") or []),
        zones.get("library_count"),
        zones.get("opponent_hand_count"),
        bool(state.get("deck_catalog")),  # the deck curve only exists on prepared states
        command,
        tuple(sorted((str(k), str(v)) for k, v in casts.items())),
    )


# Signatures being assessed right now: concurrent callers (coaching loop, plan
# reform thread, autopilot) wait for the first one instead of searching again.
_INFLIGHT: dict[tuple, threading.Event] = {}
_INFLIGHT_WAIT_S = 0.5  # past the search's hard cap: a stuck owner never blocks for long


def assess(state: dict | None) -> BoardAssessment | None:
    """Assess a planner-shape snapshot; None when seats/turn are unknown.

    One assessment per board signature: the result is cached, and a caller
    that arrives while the same signature is being assessed waits for that
    result (at most ``_INFLIGHT_WAIT_S``) rather than running its own search.
    """
    if not isinstance(state, dict):
        return None
    try:
        # The kill switch is part of the key: flipping it never serves the other pipeline's facts.
        key = (_signature(state), _line_search_enabled())
    except Exception:
        key = None
    owner, event = False, None
    if key is not None:
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if cached is not None:
                _CACHE.move_to_end(key)
                return cached
            event = _INFLIGHT.get(key)
            if event is None:
                owner, event = True, threading.Event()
                _INFLIGHT[key] = event
        if not owner:
            event.wait(_INFLIGHT_WAIT_S)
            with _CACHE_LOCK:
                cached = _CACHE.get(key)
                if cached is not None:
                    _CACHE.move_to_end(key)
                    return cached
    try:
        result = _assess(state)
    except Exception as error:  # never break a decision on the strategic layer
        logger.debug("board assessment failed: %s", error, exc_info=True)
        result = None
    if key is not None:
        with _CACHE_LOCK:
            if result is not None:
                _CACHE[key] = result
                while len(_CACHE) > _CACHE_SIZE:
                    _CACHE.popitem(last=False)
            if owner:
                _INFLIGHT.pop(key, None)
        if owner and event is not None:
            event.set()
    return result


def _line_search_enabled() -> bool:
    """False when ARENAMCP_LINE_SEARCH=0: the greedy projection alone, as before the line search."""
    return os.environ.get("ARENAMCP_LINE_SEARCH", "").strip() != "0"


def _run_line_search(model: Any, *, survival: bool, lethal_now: bool) -> Any:
    """``line_search.search_lines`` on this board, or None (switched off or failed)."""
    if not _line_search_enabled():
        return None
    try:
        # Imported here: line_search builds on this module's helpers.
        from arenamcp import line_search

        return line_search.search_lines(model, survival=survival, lethal_now=lethal_now)
    except Exception as error:  # never break the assessment on the search
        logger.debug("line search failed: %s", error, exc_info=True)
        return None


def _leaf_race(search: Any, line: Any) -> float | None:
    """The race term of the line's last board (+15/our clock when ours is no slower, else -15/theirs)."""
    leaf = getattr(line, "_leaf", None)
    engine = getattr(search, "_search", None)
    if leaf is None or engine is None or getattr(leaf, "terminal", True):
        return None
    try:
        return float(engine.race(leaf))
    except Exception:
        logger.debug("line race term failed", exc_info=True)
        return None


def _clock_facts(model: Any) -> SimpleNamespace:
    """Board-only clocks, race and the survival hint the deployment values (and the search) use."""
    ours, theirs = list(model.ours), list(model.theirs)
    our_turn = model.our_turn
    our_attack_pending, their_attack_pending = model.our_attack_pending, model.their_attack_pending
    first_our_attackers = None if model.first_our_attackers is None else list(model.first_our_attackers)
    first_their_attackers = None if model.first_their_attackers is None else list(model.first_their_attackers)
    our_clock, our_lives = _simulate_attacks(
        ours,
        theirs,
        model.opp_life,
        first_attackers=first_our_attackers,
        first_blockers=list(model.untapped_theirs) if our_attack_pending else None,
    )
    their_clock, their_lives = _simulate_attacks(
        theirs,
        ours,
        model.our_life,
        first_attackers=first_their_attackers,
        # On our turn, creatures that attacked stay tapped through theirs.
        first_blockers=list(model.our_first_blockers),
    )
    lethal_now = our_attack_pending and our_clock == 1
    if our_attack_pending and not lethal_now:
        next_clock, _ = _simulate_attacks(
            ours, theirs, model.opp_life, first_attackers=None, first_blockers=None, horizon=1
        )
        lethal_next_turn = next_clock == 1
    else:
        lethal_next_turn = (not lethal_now) and our_clock == 1
    opp_lethal_on_board = their_clock == 1

    # Whose killing attack lands first (our k-th vs their j-th attack).
    def attack_time(index: int, ours_side: bool) -> int:
        if our_attack_pending:
            first_ours = True
        elif their_attack_pending:
            first_ours = False
        else:
            first_ours = not our_turn  # their turn, post-combat: our attack comes next
        offset = 0 if first_ours == ours_side else 1
        return 2 * (index - 1) + offset

    if our_clock is None and their_clock is None:
        race, race_detail = "stalled", "neither side gets damage through the other's blocks"
    elif their_clock is None:
        race, race_detail = "ahead", f"we kill in {our_clock}, they get nothing through"
    elif our_clock is None:
        race, race_detail = "behind", f"they kill in {their_clock}, we get nothing through"
    else:
        ours_at, theirs_at = attack_time(our_clock, True), attack_time(their_clock, False)
        if ours_at < theirs_at:
            race = "ahead"
        elif theirs_at < ours_at:
            race = "behind"
        else:
            race = "even"
        first = "we strike first" if attack_time(1, True) < attack_time(1, False) else "they strike first"
        race_detail = f"our clock {our_clock} vs their {their_clock}, {first}"
    survival_hint = (
        opp_lethal_on_board
        or race == "behind"
        or (their_clock is not None and their_clock <= 2)
        or (
            len(theirs) > len(ours) + 1
            and sum(b["power"] for b in theirs) > sum(b["power"] for b in ours) + 2
        )
    )
    return SimpleNamespace(
        our_clock=our_clock,
        our_lives=our_lives,
        their_clock=their_clock,
        their_lives=their_lives,
        lethal_now=lethal_now,
        lethal_next_turn=lethal_next_turn,
        opp_lethal_on_board=opp_lethal_on_board,
        race=race,
        race_detail=race_detail,
        survival_hint=survival_hint,
    )


def search_hints(model: Any) -> tuple[bool, bool]:
    """(survival, lethal_now) exactly as ``_assess`` passes them to the line search; no search runs."""
    clocks = _clock_facts(model)
    return bool(clocks.survival_hint and not clocks.lethal_now), bool(clocks.lethal_now)


def _assess(state: dict) -> BoardAssessment | None:
    started = time.perf_counter()
    # Imported here: board_model builds on this module's helpers.
    from arenamcp.board_model import build_board_model

    # The board facts (seats, timing, bodies, mana, the hand's spells). Phase
    # and step come back in the log's names, so a bridge snapshot ("Main1",
    # step "None") gets the same attack timing as Player.log ("Phase_Main1").
    model = build_board_model(state)
    if model is None:
        return None
    opponent, turn, our_turn, phase = model.opponent, model.turn, model.our_turn, model.phase
    our_life, opp_life = model.our_life, model.opp_life
    battlefield, hand = list(model.battlefield), list(model.hand)
    our_rules = dict(model.our_rules)
    ours, theirs = list(model.ours), list(model.theirs)
    unknowns = model.unknowns  # a fresh list: unknown bodies, then X spells
    their_attack_pending = model.their_attack_pending
    first_their_attackers = None if model.first_their_attackers is None else list(model.first_their_attackers)
    # Our blockers for their next attack: what is untapped now when that
    # attack comes before our untap step, otherwise everything.
    our_first_blockers = list(model.our_first_blockers)

    # --- clocks (board only) -------------------------------------------------
    clocks = _clock_facts(model)
    our_clock, our_lives = clocks.our_clock, clocks.our_lives
    their_clock, their_lives = clocks.their_clock, clocks.their_lives
    lethal_now, lethal_next_turn = clocks.lethal_now, clocks.lethal_next_turn
    opp_lethal_on_board = clocks.opp_lethal_on_board
    race, race_detail = clocks.race, clocks.race_detail

    # --- mana ----------------------------------------------------------------
    our_permanents = list(model.our_permanents)
    sources_all, sources_now = list(model.sources_all), list(model.sources_now)
    our_lands, their_lands = model.our_lands, model.their_lands
    hand_lands = list(model.hand_lands)
    land_drop_now, land_drop_available = model.land_drop_now, model.land_drop_available
    colors_all = model.colors_all
    spells = list(model.spells)
    missing = list(model.missing_colors)

    # --- threats -------------------------------------------------------------
    our_air = any(_reach_or_flying(b) for b in ours)
    threats = _threats(battlefield, opponent, theirs, our_air, len(ours))
    if any("adds attackers" in t.why or "token" in t.why for t in threats):
        unknowns.append("token/copy engines add attackers each turn (clocks may be faster)")
    unknowns.append("opponent's hand, draws and combat tricks")

    # --- preliminary role (board only) feeds the deployment values -----------
    survival_hint = clocks.survival_hint
    # At our ending phase T is instant-speed only: no land drop, no sorcery-speed casts.
    budgets_turns = _budget_turns(
        our_turn,
        turn,
        sources_now,
        sources_all,
        hand_lands,
        land_drop_now and not model.t_instant_only,
    )
    schedule = _schedule(
        spells,
        budgets_turns,
        survival=survival_hint and not lethal_now,
        theirs=theirs,
        ours=ours,
        instant_only_first=model.t_instant_only,
    )
    lookahead, dead_in, now_life = _project(
        schedule=schedule,
        budgets=budgets_turns,
        spells=spells,
        ours=ours,
        theirs=theirs,
        our_life=our_life,
        turn=turn,
        our_rules=our_rules,
        their_attack_pending=their_attack_pending,
        first_their_attackers=first_their_attackers,
        untapped_ours=our_first_blockers,
    )
    dead_in_greedy = dead_in

    # --- line search: candidate lines replace the greedy projection ------------
    # The greedy line above stays the baseline (and the fallback): a failed or
    # truncated search keeps its facts.
    search = _run_line_search(model, survival=survival_hint and not lethal_now, lethal_now=lethal_now)
    found = None  # the search, when its facts are used
    best_text, race_term = "", None
    if search is not None and not search.truncated:
        try:
            rows = search.to_projections()
            for row, step in zip(rows, search.best.steps, strict=False):
                row.cycles = list(step.cycles)
            best_text, race_term = search.best.summary(), _leaf_race(search, search.best)
            lookahead, dead_in, now_life = rows, search.dead_in, search.now_life
            found = search
        except Exception as error:
            logger.debug("line search facts failed: %s", error, exc_info=True)
            best_text, race_term = "", None
    best = found.best if found is not None else None

    # --- what the line facts can't see -----------------------------------------
    # Casts the search can't value (castable on T or T+1; for claims about their next
    # attack, castable before it) and our own pending choice or effect on the stack: the
    # line search's "only" and "lethal" claims, ALL-IN and the roles they force are
    # qualified while any of them could change the outcome. The casts only with the
    # search's facts in use (its fallbacks keep today's greedy facts); the pending check
    # reads only the stack, so it applies whenever the search is switched on, even when
    # the search failed or was cut short.
    pending: list[str] = []
    unmodelled: list[str] = []  # castable on T or T+1
    unmodelled_first: list[str] = []  # castable before their next attack
    if _line_search_enabled():
        try:
            pending = _our_pending(state, model)
        except Exception as error:
            logger.debug("pending check failed: %s", error, exc_info=True)
    if found is not None:
        try:
            unmodelled, unmodelled_first = _unmodelled_castable(model, found, lookahead)
        except Exception as error:
            logger.debug("unmodelled checks failed: %s", error, exc_info=True)
    ours_pending = [f"our pending {name}" for name in pending]
    caveat = ", ".join([*unmodelled, *ours_pending])
    first_caveat = ", ".join([*unmodelled_first, *ours_pending])
    pending_note = f" — before our pending {', '.join(pending)} resolves" if pending else ""

    # --- advantages ----------------------------------------------------------
    their_hand = _opponent_hand(state)
    card_advantage = None if their_hand is None else len(hand) - their_hand
    our_nonland = sum(1 for c in our_permanents if not _is_land(c))
    their_nonland = sum(1 for c in battlefield if _controller(c) == opponent and not _is_land(c))
    board_advantage = our_nonland - their_nonland
    deck_curve = _deck_curve(state)

    our_power = sum(b["power"] for b in ours)
    their_power = sum(b["power"] for b in theirs)

    # --- flags ---------------------------------------------------------------
    flags: list[str] = []
    if lethal_now:
        flags.append(
            f"LETHAL AVAILABLE NOW ({opp_life - our_lives[0]} through their blocks vs {opp_life} life)"
        )
    elif lethal_next_turn:
        flags.append("LETHAL AVAILABLE NEXT TURN on board if nothing changes")
    if opp_lethal_on_board:
        flags.append(
            f"OPPONENT HAS LETHAL ON BOARD ({our_life - their_lives[0]} through our best blocks vs {our_life} life)"
        )
    if dead_in is not None and dead_in <= 2:
        saved = their_clock is not None and dead_in > their_clock
        # Casts the search can't value may be the play that saves us (for their next attack,
        # only those castable before it); a pending choice of ours (a 'gain 4 life' mode)
        # resolves before their attack.
        names = unmodelled_first if dead_in == 1 else unmodelled
        unsure = (f" (not modelled: {', '.join(names)})" if names else "") + pending_note
        if dead_in == 1:
            plays = "our best modelled plays" if names else "our best castable plays"
            flags.append(f"DEAD NEXT ATTACK even after {plays}{unsure}")
        else:
            flags.append(
                "DEAD IN 2 TURNS UNLESS WE STABILIZE"
                + (" (our castable blockers buy one turn)" if saved else "")
                + unsure
            )
    elif their_clock is not None and their_clock <= 2 and dead_in is None:
        flags.append(f"their board kills us in {their_clock} but our castable plays stabilize")
    # Nothing to lose: dead next attack even after our best castable plays,
    # by a clear margin or to evasion we cannot block. Requires the facts to be
    # unambiguous, since an all-in attack throws away blockers.
    through = our_life - their_lives[0] if their_lives else 0
    evasive = 0 if our_air else sum(b["power"] for b in theirs if _flying(b))
    # Dead to their next attack whatever we do: every T step of an exact search
    # dies there (exact_first_attack needs a complete root, and all_dead_at_first
    # reads only root nodes, so a T+1 cut short by the soft budget doesn't
    # matter); otherwise the greedy line's verdict. A searched line that survives
    # their first attack disproves it either way (the greedy line can't see what
    # the search models, such as an enters trigger's life gain).
    if found is not None and (dead_in is None or dead_in > 1):
        dead_first = False
    elif found is not None and found.exact_first_attack:
        dead_first = found.all_dead_at_first
    else:
        dead_first = dead_in_greedy == 1
    # Our own pending choice (a 'gain 4 life' mode on the stack) resolves before their attack,
    # and a cast the search can't value (a fog, an aura) may be castable before it: "no
    # defensive line survives" is not established while either is.
    all_in = bool(
        opp_lethal_on_board
        and dead_first
        and not lethal_now
        and our_power > 0
        and (through >= our_life + 2 or evasive >= our_life)
        and not pending
        and not unmodelled_first
    )
    if all_in:
        flags.append(
            "ALL-IN: no defensive line survives their next attack — attack with everything; "
            "holding back blockers changes nothing"
        )
    if found is not None and best is not None and not found.dead_now:
        unsure = f"not modelled: {caveat}"
        if found.only_survivor:
            flags.append(
                f"BEST MODELLED LINE (the other modelled lines die; {unsure}): {best_text}"
                if caveat
                else f"ONLY SURVIVING LINE: {best_text}"
            )
        elif getattr(found, "greedy_dies", False):
            flags.append(
                f"GREEDY LINE DIES; BEST MODELLED LINE ({unsure}): {best_text}"
                if caveat
                else f"GREEDY LINE DIES; BEST SURVIVING LINE: {best_text}"
            )
        elif opp_lethal_on_board and found.only_first_attack_survivor:
            flags.append(
                f"BEST MODELLED LINE THROUGH THEIR NEXT ATTACK (not modelled: {first_caveat}): {best_text}"
                if first_caveat
                else f"ONLY LINE THAT SURVIVES THEIR NEXT ATTACK: {best_text}"
            )
        if _wins_by_next_turn(best) and not lethal_now:
            # A win the search found stands whatever the casts it can't value do (they are
            # ours to cast or not): named, since one of them may win sooner.
            flags.append(f"LETHAL LINE: {best_text}" + (f" ({unsure})" if caveat else ""))

    # --- role ----------------------------------------------------------------
    role, reason = _role(
        all_in=all_in,
        lethal_now=lethal_now,
        opp_lethal=opp_lethal_on_board,
        dead_in=dead_in,
        race=race,
        our_clock=our_clock,
        their_clock=their_clock,
        our_power=our_power,
        their_power=their_power,
        ours=len(ours),
        theirs=len(theirs),
        our_life=our_life,
        opp_life=opp_life,
        our_lives=our_lives,
        their_lives=their_lives,
        card_advantage=card_advantage,
        deck_curve=deck_curve,
        lookahead=lookahead,
        best_line=best,
        line_text=best_text,
        # "Only" claims force no role while a cast or pending effect they can't see could change them.
        only_survivor=bool(found and found.only_survivor) and not caveat,
        greedy_dies=bool(found and getattr(found, "greedy_dies", False)) and not caveat,
        only_first_attack_survivor=bool(found and found.only_first_attack_survivor),
        race_term=race_term,
        caveat=first_caveat,
    )
    posture = found.posture if found is not None else ""
    posture_reason = found.posture_reason if found is not None else ""
    if posture in ("attack", "hold") and posture_reason:
        reason = f"{reason}; {posture_reason}"
    reason += pending_note

    commander: list[str] = []
    if model.commanders:
        try:
            commander = _commander_facts(model, lookahead, found)
        except Exception as error:
            logger.debug("commander facts failed: %s", error, exc_info=True)

    zones = state.get("zones") if isinstance(state.get("zones"), dict) else {}
    library = _int(zones.get("library_count", state.get("library_count")))
    if str(zones.get("library_count_source") or "") == "unknown":
        library = None

    result = BoardAssessment(
        turn=turn,
        our_turn=our_turn,
        phase=phase,
        our_life=our_life,
        opp_life=opp_life,
        our_creatures=len(ours),
        their_creatures=len(theirs),
        our_power=our_power,
        their_power=their_power,
        our_hand=len(hand),
        their_hand=their_hand,
        our_lands=our_lands,
        their_lands=their_lands,
        lands_in_hand=len(hand_lands),
        land_drop_available=land_drop_available,
        colors="".join(sorted(c for c in colors_all if c != "C")),
        missing_colors="".join(missing),
        our_clock=our_clock,
        their_clock=their_clock,
        race=race,
        race_detail=race_detail,
        role=role,
        role_reason=reason,
        lethal_now=lethal_now,
        lethal_next_turn=lethal_next_turn,
        opp_lethal_on_board=opp_lethal_on_board,
        all_in=all_in,
        dead_in=dead_in,
        our_life_now_attack=now_life,
        card_advantage=card_advantage,
        board_advantage=board_advantage,
        deck_curve=deck_curve,
        flags=flags,
        threats=threats,
        lookahead=lookahead,
        unknowns=unknowns,
        library_count=library,
        line_search=search,
        lines=list(found.lines) if found is not None else [],
        posture=posture,
        posture_reason=posture_reason,
        dead_in_greedy=dead_in_greedy,
        search_stats=search.stats() if search is not None else {},
        unmodelled=unmodelled,
        pending=pending,
        commander=commander,
    )
    result.elapsed_ms = (time.perf_counter() - started) * 1000
    return result


def _commander_facts(model: Any, lookahead: list[TurnProjection], search: Any) -> list[str]:
    """One fact per commander of ours in the command zone; [] outside commander games.

    Its cost now, tax included (Arena's own when the snapshot has its cast
    action; an unknown tax is called that), when it is castable (now at
    instant speed on their turn, else the first lookahead turn that can pay
    for it), when the best line (or, without the search, the board-math
    projection) casts it, and what its cast does that the search can't value.
    """
    from arenamcp.line_search_moves import hand_info, unmodelled_effect

    best = search.best if search is not None else None
    facts: list[str] = []
    for commander in getattr(model, "commanders", ()):
        name = commander.name
        if not commander.cost:
            facts.append(
                f"{name}: cost unknown (no card data, no cast action from Arena) — judge it yourself"
            )
            continue
        plural = "s" if commander.casts != 1 else ""
        if commander.casts is None:
            cost = f"{commander.cost} now" if commander.from_action else f"{commander.cost} + unknown tax"
        elif commander.casts:
            cost = (
                f"{commander.cost} now ({{{commander.tax}}} tax for {commander.casts} previous cast{plural})"
            )
        else:
            cost = f"{commander.cost} now (no tax yet)"
        parts = [f"{name} {cost}"]
        spell = next((s for s in model.spells if s.zone == "command" and s.name == name), None)
        flash_now = (
            spell is not None
            and not model.our_turn
            and bool(hand_info(spell.card).instant_speed)
            and spell.mana_value <= len(model.sources_now)
            and _pip_matching(spell.pips, list(model.sources_now))
        )
        when = next((k for k, row in enumerate(lookahead) if name in row.castable), None)
        if flash_now:
            parts.append("castable now at instant speed")
        elif when == 0:
            parts.append(
                "castable this turn"
                if model.our_turn
                else f"castable on our next turn (T{lookahead[0].turn})"
            )
        elif when is not None:
            parts.append(f"castable from T{lookahead[when].turn}")
        elif lookahead:
            parts.append(f"not castable by T{lookahead[-1].turn} ({commander.mana_value} mana needed)")
        if best is not None:
            cast = next((f"casts it on T{s.turn}" for s in best.steps if name in s.casts), "")
            cast = cast or next((f"flashes it in on T{s.turn + 1}" for s in best.steps if name in s.held), "")
            leaf = getattr(best, "_leaf", None)
            if not cast and spell is not None and leaf is not None and model.their_attack_pending:
                spells = getattr(getattr(search, "_search", None), "spells", None) or []
                index = next((h.index for h in spells if h.spell.zone == "command" and h.name == name), None)
                if index is not None and index not in leaf.hand:
                    cast = "flashes it in against their attack now"
            if cast or flash_now or when is not None:
                parts.append(f"the best line {cast}" if cast else "the best line does not cast it")
        elif when is not None:
            cast_at = next((row.turn for row in lookahead if name in row.casts), None)
            parts.append(
                f"the board-math projection casts it on T{cast_at}"
                if cast_at
                else "the board-math projection does not cast it"
            )
        why = unmodelled_effect(spell.card, search) if spell is not None else ""
        if why:
            parts.append(f"not modelled: {why} — weigh that yourself")
        facts.append("; ".join(parts))
    return facts


def _wins_by_next_turn(line: Any) -> bool:
    """The line kills them with our attack at T or T+1."""
    return line is not None and line.outcome == "win" and line.win_at is not None and line.win_at <= 2


def _unmodelled_castable(
    model: Any, search: Any, lookahead: list[TurnProjection]
) -> tuple[list[str], list[str]]:
    """Hand spells whose effect the line search can't value, by name: (castable on T or T+1,
    castable before their next attack).

    ``line_search_moves.unmodelled_effect``: an aura, a fog, a planeswalker,
    tokens it can't read, an attack trigger, an X spell that touches the
    board or life totals (X card flow and counterspells change nothing it
    values). A combat trick counts only with a creature of ours to pump (on
    the battlefield, or castable on T). Before their next attack: on our
    turn (or theirs, after their attack), what T can cast; while their attack
    is still to come this turn, only instants payable from our untapped mana.
    Our commanders in the command zone are spells like the hand's; one whose
    cost is unknown (``BoardModel.commanders``) counts as castable now.
    """
    from arenamcp.line_search_moves import hand_info, unmodelled_effect

    rows = lookahead[:2]

    def payable(spell: _Spell, mana: int, sources: Any = None, colors: str = "") -> bool:
        need = spell.mana_value + (1 if spell.has_x else 0)  # X = 1 at least to do anything
        if need > mana:
            return False
        if sources is not None:
            return not spell.pips or _pip_matching(spell.pips, list(sources))
        return all(pip & (set(colors) | {"C"}) for pip in spell.pips)

    creature_on_t = bool(rows) and any(
        s.role == "creature" and s.name in rows[0].castable for s in model.spells
    )
    found: list[str] = []
    first: list[str] = []
    for spell in model.spells:
        if spell.uncastable or spell.name in found:
            continue
        why = unmodelled_effect(spell.card, search)
        if not why or (why == "a combat trick" and not (model.ours or creature_on_t)):
            continue
        turns = [
            k
            for k, row in enumerate(rows)
            if (payable(spell, row.mana, colors=row.colors) if spell.has_x else spell.name in row.castable)
        ]
        if not turns:
            continue
        found.append(spell.name)
        instant = bool(hand_info(spell.card).instant_speed)
        if model.their_attack_pending:
            before = instant and payable(spell, len(model.sources_now), model.sources_now)
        else:
            before = 0 in turns and (instant or not model.t_instant_only)
        if before:
            first.append(spell.name)
    # Our commander whose cost is unknown is never cast in a line: it may be castable now.
    for commander in getattr(model, "commanders", ()):
        if commander.cost or commander.name in found:
            continue
        found.append(commander.name)
        instant = bool(hand_info(commander.card).instant_speed)
        if instant if model.their_attack_pending else (instant or not model.t_instant_only):
            first.append(commander.name)
    return found, first


def _our_pending(state: dict, model: Any) -> list[str]:
    """Our own spells and abilities on the stack whose choice or effect the board facts don't count.

    A 'choose one' trigger or spell with a mode that changes the board or
    life totals ("Archive Arbiter trigger (choose one)"; a draw-or-scry
    charm is not listed); one that removes a creature, gains life, makes
    creature tokens or damages the opponent; any other effect the line
    search can't value (a fog, a tap-down, an aura: ``unmodelled_effect``)
    that is not card flow. A creature spell's body is already counted: only
    its own enters trigger is. The facts read the board before any of them
    resolves.
    """
    from arenamcp.line_search_moves import bullets, card_flow, classify, token_specs, unmodelled_effect

    cards: dict[int, dict] = {}
    for zone in ("battlefield", "graveyard", "exile", "hand", "command"):
        for card in state.get(zone) or []:
            if isinstance(card, dict) and _int(card.get("instance_id")) is not None:
                cards[_int(card.get("instance_id"))] = card
    found: list[str] = []
    for obj in state.get("stack") or []:
        if not isinstance(obj, dict) or _controller(obj) != model.local:
            continue
        ability = "ability" in str(obj.get("object_kind") or obj.get("type_line") or "").lower()
        parent = cards.get(_int(obj.get("parent_instance_id"))) if ability else None
        name = _name(parent) if parent else ("our ability" if ability else _name(obj))
        label = f"{name} trigger" if parent else name
        text = str(obj.get("oracle_text") or "")
        modes = bullets(text)
        if modes and any(classify(name, mode)[0] != "other" or not card_flow(mode.lower()) for mode in modes):
            found.append(f"{label} (choose one)")
            continue
        pseudo = {
            "name": name,
            "oracle_text": text,
            "type_line": "" if ability else obj.get("type_line") or "",
            "card_types": [] if ability else obj.get("card_types") or [],
        }
        cast = _cast_text(pseudo)
        if (
            removal_reach(pseudo) is not None
            or _LIFEGAIN.search(cast)
            or token_specs(cast) != []
            or any(face_damage(pseudo))
        ):
            found.append(label)
        elif _is_creature(pseudo):
            # Its body is counted; its own enters trigger (``_cast_text``) is not.
            effect = "\n".join(
                line for line in cast.splitlines() if not line.startswith("as an additional cost")
            )
            if effect.strip() and not card_flow(effect):
                found.append(label)
        elif unmodelled_effect(pseudo) and not card_flow(cast):
            found.append(label)
    return found


def _opponent_hand(state: dict) -> int | None:
    """Opponent hand size when the snapshot knows it (None otherwise)."""
    zones = state.get("zones") if isinstance(state.get("zones"), dict) else {}
    for value in (state.get("opponent_hand_count"), zones.get("opponent_hand_count")):
        count = _int(value)
        if count is not None:
            return max(0, count)
    return None


def _deck_curve(state: dict) -> float | None:
    catalog = state.get("deck_catalog")
    deck = [g for g in state.get("deck_cards") or [] if isinstance(g, int)]
    if not isinstance(catalog, dict) or not deck:
        return None
    values = []
    for grp_id in deck:
        info = catalog.get(grp_id) or catalog.get(str(grp_id)) or {}
        if "land" in str(info.get("type_line") or "").lower():
            continue
        cmc = info.get("cmc")
        try:
            values.append(float(cmc))
        except (TypeError, ValueError):
            continue
    return sum(values) / len(values) if values else None


def _threats(
    battlefield: list[dict],
    opponent: int | None,
    theirs: list[dict],
    our_air: bool,
    our_count: int,
    *,
    limit: int | None = 5,
) -> list[Threat]:
    """The opponent's most dangerous permanents, best first (the top ``limit``; None: all)."""
    threats: list[Threat] = []
    for body in theirs:
        score = float(body["power"]) + 0.3 * body["toughness"]
        why = [f"{body['power']}/{body['toughness']}"]
        if body["_unblockable"]:
            score += body["power"] * 1.0
            why.append("can't be blocked")
        elif _flying(body) and not our_air:
            score += body["power"] * 0.8 + 1
            why.append("flying, we have no flyer/reach")
        elif _flying(body):
            why.append("flying")
        for keyword, bonus in (
            ("trample", 1.0),
            ("deathtouch", 2.0),
            ("lifelink", 1.0),
            ("double strike", 2.0),
        ):
            if has_combat_keyword(body, keyword):
                score += bonus
                why.append(keyword)
        if has_combat_keyword(body, "menace") and our_count < 2:
            score += 1
            why.append("menace")
        roles = combat_resource_roles(body["_card"])
        if roles:
            score += 2 * len(roles)
            why.append(" + ".join(roles))
        threats.append(Threat(body["name"], body["instance_id"], score, ", ".join(why)))
    for card in battlefield:
        if _controller(card) != opponent or _is_creature(card) or _is_land(card):
            continue
        text = _text(card)
        roles = combat_resource_roles(card)
        why = []
        score = 0.0
        if "planeswalker" in _types(card):
            loyal = loyalty(card)
            score += 4 + (loyal or 0) * 0.5
            why.append(f"planeswalker{f' (loyalty {loyal})' if loyal is not None else ''}")
        if "token production" in roles or re.search(r"create a token that's a copy", text):
            score += 5
            why.append("adds attackers each turn (token/copy engine)")
        if "card draw" in roles:
            score += 2
            why.append("card advantage engine")
        if re.search(r"creatures you control (?:get|have)\b", text):
            score += 3
            why.append("anthem/keyword grant")
        if re.search(r"deals? \d+ damage to each opponent|each opponent loses \d+ life", text):
            score += 2
            why.append("direct damage")
        if score:
            threats.append(Threat(_name(card), _int(card.get("instance_id")), score, ", ".join(why)))
    threats.sort(key=lambda t: (-t.score, t.name))
    return threats if limit is None else threats[:limit]


def _budget_turns(
    our_turn: bool,
    turn: int,
    sources_now: list,
    sources_all: list,
    hand_lands: list[dict],
    land_drop_now: bool,
) -> list[dict]:
    """Mana sources for T, T+1, T+2 (land drops from hand, one per turn).

    Untapped-entering lands are played first; a land that enters tapped adds
    mana from the following turn. Rocks cast on a turn are added by
    ``_schedule`` for later turns.
    """
    lands = sorted(hand_lands, key=lambda c: (_enters_tapped(c), _name(c)))
    played: list[SimpleNamespace] = []
    plan = []
    first_turn = turn if our_turn else turn + 1
    for k in range(_LOOKAHEAD_TURNS):
        base = list(sources_now if (k == 0 and our_turn) else sources_all)
        land_name = ""
        new_land = None
        can_drop = land_drop_now if k == 0 else True
        if can_drop and lands:
            card = lands.pop(0)
            land_name = _name(card)
            new_land = SimpleNamespace(produces=frozenset(set(_land_colors(card)) or {"C"}), name=land_name)
        current = base + [s for s in played]
        if new_land is not None and not _enters_tapped(
            next((c for c in hand_lands if _name(c) == land_name), {})
        ):
            current.append(new_land)
        plan.append(
            {
                "turn": first_turn + 2 * k,
                "label": "T" if k == 0 else f"T+{k}",
                "sources": current,
                "land": land_name,
            }
        )
        if new_land is not None:
            played.append(new_land)
    return plan


def _spell_value(
    spell: _Spell, *, survival: bool, theirs: list[dict], ours: list[dict] | tuple = ()
) -> float:
    card = spell.card
    their_air = any(_flying(b) for b in theirs)
    if spell.role == "creature":
        power, toughness = _int(card.get("power")) or 0, _int(card.get("toughness")) or 0
        if survival:
            value = 2 + toughness + 0.5 * power
            if their_air and _reach_or_flying(card):
                value += 2
            if has_combat_keyword(card, "deathtouch"):
                value += 1.5
        else:
            value = 2 + 1.5 * power + 0.5 * toughness
            if has_combat_keyword(card, "flying") or _SELF_UNBLOCKABLE.search(_text(card)):
                value += 2
        if has_combat_keyword(card, "lifelink"):
            value += 1
        return value
    if spell.role in ("removal", "bounce"):
        killable = [b for b in theirs if _kills(spell.card, b, ours=ours)]
        if not killable:
            return 0.0  # no target yet: hold it rather than schedule it
        best = max(killable, key=lambda b: b["power"])
        return (3 if spell.role == "removal" else 1) + best["power"] * (1.5 if survival else 1.0)
    if spell.role == "planeswalker":
        return 4.0
    if spell.role == "lifegain":
        match = _LIFEGAIN.search(_text(card))
        return 1 + (int(match.group(1)) / 2 if match and survival else 0)
    if spell.role == "counter":
        return 0.0  # reactive: hold mana for it, don't schedule it
    if spell.role == "ramp":
        return 1.5
    return 1.0


_COLOR_WORDS = {"white": "W", "blue": "U", "black": "B", "red": "R", "green": "G"}
_STAT_LIMIT = re.compile(
    r"\bwith (?P<stat>power|toughness|mana value|converted mana cost) (?P<n>\d+) or (?P<dir>greater|more|less|fewer)\b"
)


def _keyword_set(body: dict) -> set[str]:
    return {str(k).lower() for k in body.get("keywords") or []}


def _untargetable(body: dict) -> bool:
    """Hexproof or shroud on the body (the snapshot's keywords, or a printed keyword line)."""
    if _keyword_set(body) & {"hexproof", "shroud"}:
        return True
    return bool(re.search(r"(?m)^\s*(?:hexproof|shroud)\s*$", _text(body.get("_card") or body)))


_NO_WARD_PAYMENT = 99  # ward paid with life, a discard or a sacrifice: not modelled, never paid


def ward_cost(body: dict) -> int:
    """Generic mana a spell targeting this body must also pay (0: no ward)."""
    match = re.search(r"(?m)^\s*ward\s*(?:\{o?(\d+)\}|[—–-]\s*(.*))?", _text(body.get("_card") or body))
    if match:
        return int(match.group(1)) if match.group(1) else _NO_WARD_PAYMENT
    if any(k.startswith("ward") for k in _keyword_set(body)):
        return _NO_WARD_PAYMENT  # a ward keyword without its cost in the text
    return 0


def _body_colors(body: dict) -> set[str] | None:
    """The body's colours, None when the snapshot doesn't say."""
    card = body.get("_card") or body
    named = {_COLOR_WORDS.get(str(c).lower().removeprefix("cardcolor_")) for c in card.get("colors") or []}
    named.discard(None)
    if named:
        return named  # type: ignore[return-value]
    cost = str(card.get("mana_cost") or "")
    if cost:
        return {c for c in "WUBRG" if c in cost.upper()}
    return None


def _body_mana_value(body: dict) -> int:
    card = body.get("_card") or body
    return _mana_value(str(card.get("mana_cost") or ""))


def _legal_target(clause: _Clause, body: dict, *, attacking: bool, ward_mana: int) -> bool:
    """The clause's target words allow this creature (hexproof, ward, stats, flying, colour, combat)."""
    words = clause.words
    if clause.targeted and (_untargetable(body) or ward_cost(body) > ward_mana):
        return False
    if re.search(r"\b(?:attacking|blocking)\b|\btapped (?:creature|permanent)", words) and not attacking:
        return False  # never cast proactively; their attack under way only
    for match in _STAT_LIMIT.finditer(words):
        stat, n = match.group("stat"), int(match.group("n"))
        value = (
            body["power"]
            if stat == "power"
            else body["toughness"]
            if stat == "toughness"
            else _body_mana_value(body)
        )
        if (value < n) if match.group("dir") in ("greater", "more") else (value > n):
            return False
    if re.search(r"\bwith flying\b", words) and not _flying(body):
        return False
    target = words.split("target", 1)[1] if "target" in words else words
    noun = re.split(r"\b(?:creature|permanent|planeswalker)s?\b", target, maxsplit=1)
    before = noun[0] if noun else ""
    after = noun[1] if len(noun) > 1 else ""
    wanted = {_COLOR_WORDS[w] for w in re.findall(r"\b(white|blue|black|red|green)\b", before)}
    that = re.match(r"(?: or planeswalker)? that's ([a-z ,]+)", after)
    if that:
        wanted |= {_COLOR_WORDS[w] for w in re.findall(r"\b(white|blue|black|red|green)\b", that.group(1))}
    banned = {_COLOR_WORDS[w] for w in re.findall(r"\bnon(white|blue|black|red|green)\b", before)}
    if wanted or banned:
        colors = _body_colors(body)
        if colors is None or (wanted and not colors & wanted) or colors & banned:
            return False
    return True


def _kills(
    card: dict,
    body: dict,
    *,
    ours: list[dict] | tuple = (),
    attacking: bool = False,
    ward_mana: int = 0,
) -> bool:
    """The card's cast text can legally target this opposing creature and remove it.

    Respects hexproof/shroud, ward (its mana cost must fit in ``ward_mana``,
    the mana left after the spell; other ward costs are never paid), "with
    toughness/power/mana value N or greater/less", "with flying", colour words
    and attacking/blocking/tapped targets (legal only for ``attacking``
    bodies, i.e. during their attack). A fight needs a creature of ``ours``
    whose power reaches the target's toughness. Bounce removes anything it may
    target.
    """
    for clause in _removal_clauses(card):
        if not _legal_target(clause, body, attacking=attacking, ward_mana=ward_mana):
            continue
        if clause.kind in ("damage", "shrink"):
            if (
                not has_combat_keyword(body, "indestructible")
                and clause.limit is not None
                and body["toughness"] <= clause.limit
            ):
                return True
        elif clause.kind == "destroy":
            if not has_combat_keyword(body, "indestructible"):
                return True
        elif clause.kind == "fight":
            if not has_combat_keyword(body, "indestructible") and any(
                b["power"] >= body["toughness"] for b in ours
            ):
                return True
        else:
            return True  # bounce
    return False


def _schedule(
    spells: list[_Spell],
    budgets: list[dict],
    *,
    survival: bool,
    theirs: list[dict],
    ours: list[dict] | tuple = (),
    instant_only_first: bool = False,
) -> list[list[_Spell]]:
    """Per-turn knapsack: the highest-value affordable set (<=3 spells) each turn.

    ``instant_only_first``: T is our ending phase, so only instant-speed spells
    can be cast on it. Spells that can't be cast at all (``_Spell.uncastable``)
    are never scheduled.
    """
    for spell in spells:
        spell.value = _spell_value(spell, survival=survival, theirs=theirs, ours=ours)
    remaining = [s for s in spells if not s.has_x and not s.uncastable and s.value > 0]
    schedule: list[list[_Spell]] = []
    extra: list[SimpleNamespace] = []
    for index, budget in enumerate(budgets):
        sources = list(budget["sources"]) + extra
        best: tuple = ()
        best_key = (0.0, 0)
        pool = remaining
        if index == 0 and instant_only_first:
            pool = [s for s in remaining if hand_card({**s.card, "rarity": "-"}).instant_speed]
        for size in range(1, min(_MAX_SPELLS_PER_TURN, len(pool)) + 1):
            for combo in combinations(pool, size):
                total = sum(s.mana_value for s in combo)
                if total > len(sources):
                    continue
                if not _pip_matching(tuple(p for s in combo for p in s.pips), sources):
                    continue
                key = (round(sum(s.value for s in combo), 3), total)
                if key > best_key:
                    best, best_key = combo, key
        schedule.append(list(best))
        remaining = [s for s in remaining if s not in best]
        for spell in best:
            if spell.role == "ramp":
                extra.append(SimpleNamespace(produces=frozenset("WUBRGC"), name=spell.name))
        budget["castable"] = [
            s.name
            for s in spells
            if not s.has_x
            and not s.uncastable
            and s.mana_value <= len(sources)
            and _pip_matching(s.pips, sources)
        ]
        budget["mana"] = len(sources)
        budget["colors"] = "".join(sorted({c for s in sources for c in s.produces if c != "C"}))
    return schedule


def _project(
    *,
    schedule: list[list[_Spell]],
    budgets: list[dict],
    spells: list[_Spell],
    ours: list[dict],
    theirs: list[dict],
    our_life: int,
    turn: int,
    our_rules: dict,
    their_attack_pending: bool,
    first_their_attackers: list[dict] | None,
    untapped_ours: list[dict],
) -> tuple[list[TurnProjection], int | None, int | None]:
    """Our life if we deploy the schedule and they attack after each of our turns."""
    life = our_life
    board = list(ours)
    blockers_first = list(untapped_ours)
    enemy = list(theirs)
    attacks = 0
    dead_in: int | None = None
    now_life: int | None = None
    if their_attack_pending:
        able = [a for a in (first_their_attackers or []) if a["_can_attack"]]
        damage, dead_att, dead_def = _attack_round(able, blockers_first, life)
        life -= damage
        attacks += 1
        now_life = life
        enemy = [e for e in enemy if e["instance_id"] not in dead_att]
        board = [b for b in board if b["instance_id"] not in dead_def]
        blockers_first = list(board)
        if life <= 0:
            dead_in = attacks
    projections: list[TurnProjection] = []
    for k, (budget, casts) in enumerate(zip(budgets, schedule, strict=False)):
        returning: list[dict] = []
        for spell in casts:
            if spell.role == "creature" and _is_creature(spell.card):
                body = _body(spell.card, turn + 2 * k + 1, our_rules)
                if body is not None:
                    board.append(body)
                    blockers_first.append(body)
            elif spell.role in ("removal", "bounce"):
                killable = [b for b in enemy if _kills(spell.card, b, ours=board)]
                if killable:
                    target = max(killable, key=lambda b: (b["power"], b["toughness"]))
                    enemy = [b for b in enemy if b is not target]
                    if spell.role == "bounce":
                        returning.append(target)
        blockers = blockers_first if k == 0 else board
        life_after = None
        if dead_in is None:
            able = [a for a in enemy if a["_can_attack"]]
            damage, dead_att, dead_def = _attack_round(able, blockers, life)
            life -= damage
            attacks += 1
            life_after = life
            enemy = [e for e in enemy if e["instance_id"] not in dead_att] + returning
            board = [b for b in board if b["instance_id"] not in dead_def]
            if life <= 0:
                dead_in = attacks
        projections.append(
            TurnProjection(
                label=budget["label"],
                turn=budget["turn"],
                mana=budget.get("mana", len(budget["sources"])),
                colors=budget.get("colors", ""),
                land=budget.get("land", ""),
                casts=[s.name for s in casts],
                castable=list(budget.get("castable", [])),
                life_after=life_after,
                opponent_creatures_after=len(enemy),
                source_colors=["".join(sorted(source.produces)) for source in budget["sources"]],
            )
        )
    return projections, dead_in, now_life


def _role(
    *,
    all_in: bool = False,
    lethal_now: bool,
    opp_lethal: bool,
    dead_in: int | None,
    race: str,
    our_clock: int | None,
    their_clock: int | None,
    our_power: int,
    their_power: int,
    ours: int,
    theirs: int,
    our_life: int,
    opp_life: int,
    our_lives: list[int],
    their_lives: list[int],
    card_advantage: int | None,
    deck_curve: float | None,
    lookahead: list[TurnProjection],
    best_line: Any = None,
    line_text: str = "",
    only_survivor: bool = False,
    only_first_attack_survivor: bool = False,
    race_term: float | None = None,
    greedy_dies: bool = False,
    caveat: str = "",
) -> tuple[str, str]:
    """Who's the beatdown: lethal and survival first, then fast clocks, then board/cards/curve.

    A clock within the simulation horizon (<= 6 attacks) is "fast" and drives
    the role; slower clocks only break ties, so a lone 1/2 flyer does not turn
    the game into a race. ``best_line`` (the line search's best line, None
    without the search; ``line_text`` its summary) adds: a line that kills by
    T+1 is the beatdown, the only surviving line is control, and with slow
    clocks a line that attacks for a third of their life by T+2 at a
    non-negative race term, without dropping us below min(life, 10), is the
    beatdown. ``caveat`` names what the lines can't see (casts the search
    can't value, our pending choice): the caller then passes no "only" claims,
    and the opponent-lethal reason calls the line the best modelled one.
    """
    board = f"{ours} vs {theirs} creatures, {our_power} vs {their_power} power"

    def fast(clock: int | None) -> bool:
        return clock is not None and clock <= _HORIZON

    def text(clock: int | None) -> str:
        return "none" if clock is None else str(clock)

    if lethal_now:
        through = opp_life - (our_lives[0] if our_lives else opp_life)
        return ROLE_AGGRESSOR, f"lethal on board now ({through} through their best blocks vs {opp_life} life)"
    if all_in:
        through = our_life - (their_lives[0] if their_lives else our_life)
        return ROLE_AGGRESSOR, (
            f"all-in: {through} gets through our best blocks vs {our_life} life next attack whatever we do — "
            f"attack with everything; blockers held back change nothing"
        )
    if _wins_by_next_turn(best_line):
        return ROLE_AGGRESSOR, f"best line kills on T{best_line.win_turn}: {line_text}"
    if race == "ahead" and fast(our_clock) and (their_clock is None or our_clock <= 3):
        return ROLE_AGGRESSOR, (
            f"our clock {our_clock} beats their {text(their_clock)} ({board}) — we're the beatdown"
        )
    if opp_lethal:
        through = our_life - (their_lives[0] if their_lives else our_life)
        only = ""
        if only_first_attack_survivor and line_text:
            only = (
                f"; best modelled line: {line_text} (not modelled: {caveat})"
                if caveat
                else f"; only line: {line_text}"
            )
        return ROLE_CONTROL, (
            f"opponent has lethal on board ({through} through our best blocks vs {our_life} life) — survive first"
            f"{only}"
        )
    if only_survivor and line_text:
        return ROLE_CONTROL, f"only line that survives: {line_text}"
    if greedy_dies and line_text:
        return ROLE_CONTROL, f"the greedy line dies; best surviving line: {line_text}"
    if dead_in is not None and dead_in <= 2:
        deployed = next((s for s in lookahead if s.casts), None)
        after = (
            f" even after casting {', '.join(deployed.casts)} (T{deployed.turn})"
            if deployed
            else " and nothing castable stops it"
        )
        return ROLE_CONTROL, f"dead in {dead_in} turns{after} ({board}) — stabilize: block, remove, gain life"
    if race == "behind" and fast(their_clock):
        return ROLE_DEFENDER, (
            f"their clock {their_clock} vs ours {text(our_clock)} ({board}) — behind: block, trade, deploy bodies"
        )
    if race == "ahead" and fast(our_clock):
        return (
            ROLE_AGGRESSOR,
            f"our clock {our_clock} beats their {text(their_clock)} ({board}) — keep attacking",
        )
    if race == "even" and fast(our_clock):
        if card_advantage is not None and card_advantage >= 2:
            return ROLE_DEFENDER, (
                f"clocks even ({our_clock} vs {their_clock}) but +{card_advantage} cards — trade and grind"
            )
        if card_advantage is not None and card_advantage <= -2:
            return ROLE_AGGRESSOR, (
                f"clocks even ({our_clock} vs {their_clock}) but {card_advantage} cards — push damage now"
            )
        return ROLE_RACE, (
            f"clocks even ({our_clock} vs {their_clock}) — damage matters most; block only to swing the race"
        )
    # No fast clock on either side.
    if ours == 0 and theirs == 0:
        if deck_curve is not None and deck_curve <= 2.9:
            return (
                ROLE_AGGRESSOR,
                f"empty boards; low curve (avg MV {deck_curve:.1f}) — curve out and pressure",
            )
        curve = f" (avg MV {deck_curve:.1f})" if deck_curve is not None else ""
        return ROLE_DEFENDER, f"empty boards{curve} — develop on curve, trade early"
    slow = f"slow clocks (ours {text(our_clock)}, theirs {text(their_clock)}; {board})"
    pressure = _line_pressure(best_line, our_life=our_life, opp_life=opp_life, race_term=race_term)
    if pressure is not None:
        return (
            ROLE_AGGRESSOR,
            f"{slow}; best line attacks for {pressure[0]} by T+2 while taking {pressure[1]}",
        )
    if card_advantage is not None and card_advantage >= 2:
        return ROLE_CONTROL, f"{slow}; +{card_advantage} cards — we win the long game"
    if card_advantage is not None and card_advantage <= -2:
        return ROLE_AGGRESSOR, f"{slow}; {card_advantage} cards — force damage before they out-card us"
    if race == "ahead":
        return ROLE_AGGRESSOR, f"{slow} — we're slightly ahead: keep pressure, add evasion"
    if race == "behind" or their_power > our_power:
        return ROLE_DEFENDER, f"{slow} — hold blockers, develop bigger threats"
    return ROLE_RACE, f"{slow} — find evasion or removal to break the stall"


def _line_pressure(
    line: Any, *, our_life: int, opp_life: int, race_term: float | None
) -> tuple[int, int] | None:
    """(damage by T+2, life we lose by their second attack) when the line is a safe beatdown.

    Safe: at least ceil(their life / 3) through by T+2, a race term >= 0 at
    the line's last board (a lethal line wins the race), and our life after
    their second attack still >= min(our life, 10). None otherwise.
    """
    if line is None or not line.steps or line.outcome == "dead":
        return None
    if line.outcome != "win" and (race_term is None or race_term < 0):
        return None
    opp_after = [s.opp_life_after for s in line.steps[:3] if s.opp_life_after is not None]
    damage = opp_life - min(opp_after) if opp_after else 0
    if damage <= 0 or damage < math.ceil(opp_life / 3):
        return None
    lives = [s.life_after for s in line.steps if s.life_after is not None]
    if len(lives) < 2 or lives[1] < min(our_life, 10):
        return None
    return damage, max(0, our_life - lives[1])


# --- role guard --------------------------------------------------------------


@dataclass
class GuardVerdict:
    option_id: str
    reason: str  # full reason for the log
    summary: str = ""  # short reasoning for narration


def _life_loss(
    assessment_state: dict,
    *,
    add_body: dict | None = None,
    remove_id: int | None = None,
    attacks: int = 2,
) -> int:
    """Our life lost over the next opponent attacks with one hypothetical change."""
    local, opponent = _seats(assessment_state)
    turn = _int((assessment_state.get("turn") or {}).get("turn_number")) or 0
    battlefield = [c for c in assessment_state.get("battlefield") or [] if isinstance(c, dict)]
    our_rules = _side_rules(battlefield, local)
    their_rules = _side_rules(battlefield, opponent)
    ours = [
        b
        for c in battlefield
        if _is_creature(c) and _controller(c) == local and (b := _body(c, turn, our_rules))
    ]
    theirs = [
        b
        for c in battlefield
        if _is_creature(c) and _controller(c) == opponent and (b := _body(c, turn, their_rules))
    ]
    if remove_id is not None:
        theirs = [b for b in theirs if b["instance_id"] != remove_id]
    blockers = [b for b in ours if not b["_tapped"]]
    if add_body is not None:
        ours.append(add_body)
        blockers.append(add_body)
    life = _life(assessment_state, local)
    _clock, lives = _simulate_attacks(
        theirs, ours, life, first_attackers=None, first_blockers=blockers, horizon=attacks
    )
    final = lives[-1] if lives else life
    return life - final


def role_guard(
    assessment: BoardAssessment | None, decision: Any, chosen_id: str, state: dict
) -> GuardVerdict | None:
    """Steer a non-board pick to a board play when we're defending.

    Fires only when ALL hold:
      * the assessment is in survival mode (defender, control/stabilize,
        losing the race, or dead within two attacks) and we don't have lethal;
      * the request is ActionsAvailable and the pick is card draw, selection,
        a mana rock, cycling, or bouncing a creature while every opposing
        creature is a cheap (MV <= 2) nontoken;
      * another payable option - a creature, creature removal, or the land
        drop that makes one castable this turn - lowers our projected life
        loss over the next two opponent attacks.
    Never overrides a land, creature, removal, counter, life gain or pass.
    """
    if assessment is None or not assessment.survival_mode or assessment.lethal_now:
        return None
    if getattr(decision, "request_type", "") != "ActionsAvailable":
        return None
    chosen = decision.find(chosen_id)
    if chosen is None:
        return None
    chosen_role = option_role(chosen, state)
    if chosen_role == "bounce":
        local, opponent = _seats(state)
        enemy = [c for c in state.get("battlefield") or [] if _is_creature(c) and _controller(c) == opponent]
        cheap = enemy and all(
            "token" not in str(c.get("object_kind") or "").lower() and hand_card(c).mana_value <= 2
            for c in enemy
        )
        if not (cheap and assessment.our_turn):
            return None
    elif chosen_role not in NON_BOARD_ROLES:
        return None

    baseline = _life_loss(state)
    turn = assessment.turn
    local, opponent = _seats(state)
    battlefield = [c for c in state.get("battlefield") or [] if isinstance(c, dict)]
    our_rules = _side_rules(battlefield, local)
    their_rules = _side_rules(battlefield, opponent)
    enemy = [
        b
        for c in battlefield
        if _is_creature(c) and _controller(c) == opponent and (b := _body(c, turn, their_rules))
    ]
    friends = [
        b
        for c in battlefield
        if _is_creature(c) and _controller(c) == local and (b := _body(c, turn, our_rules))
    ]
    candidates: list[tuple[int, int, str, str]] = []  # (loss, preference, option_id, description)
    for option in decision.options:
        if option.option_id == chosen_id or option.payable is False:
            continue
        role = option_role(option, state)
        source, _zone = _source_card(state, option.meta or {})
        if role == "creature" and source and _is_creature(source):
            body = _body(source, turn + 1, our_rules)
            if body is None:
                continue
            loss = _life_loss(state, add_body=body)
            candidates.append(
                (loss, 0, option.option_id, f"cast {_name(source)} ({body['power']}/{body['toughness']})")
            )
        elif role == "removal" and source:
            killable = [b for b in enemy if _kills(source, b, ours=friends)]
            if not killable:
                continue
            target = max(killable, key=lambda b: (b["power"], b["toughness"]))
            loss = _life_loss(state, remove_id=target["instance_id"])
            candidates.append((loss, 1, option.option_id, f"cast {_name(source)} on {target['name']}"))
        elif role == "land" and assessment.our_turn and assessment.land_drop_available:
            enabled = _enabled_by_land(state, option, source, our_rules, enemy, turn, friends)
            if enabled is not None:
                loss, what = enabled
                candidates.append(
                    (loss, 2, option.option_id, f"play {_name(source) or 'a land'} first to {what}")
                )
    better = sorted(c for c in candidates if c[0] < baseline)
    if not better:
        return None
    loss, _pref, option_id, description = better[0]
    reason = (
        f"Role guard: {assessment.role} ({assessment.role_reason}); "
        f"{description} instead of {chosen.label} ({chosen_role}) — projected life loss over the next two "
        f"attacks {baseline} -> {loss}"
    )
    if assessment.opp_lethal_on_board:
        why = "The opponent has lethal on board"
    elif assessment.dead_in is not None and assessment.dead_in <= 2:
        why = f"We're dead in {assessment.dead_in} turns unless we stabilize"
    else:
        why = f"We're behind on board (their clock {assessment.their_clock})"
    summary = f"{why}: {description[0].upper()}{description[1:]} instead of {chosen.label}."
    return GuardVerdict(option_id, reason, summary)


def _enabled_by_land(
    state: dict,
    option: Any,
    land: dict,
    our_rules: dict,
    enemy: list[dict],
    turn: int,
    friends: list[dict] | tuple = (),
) -> tuple[int, str] | None:
    """Best survival play this land drop makes castable this turn: (loss, description).

    The play is a hand card or our commander in the command zone (its cost now, tax included).
    """
    if not land or _enters_tapped(land):
        return None
    local, _opponent = _seats(state)
    sources = [
        s
        for c in state.get("battlefield") or []
        if isinstance(c, dict)
        and _controller(c) == local
        and not c.get("is_tapped")
        and (s := _mana_source(c, turn))
    ]
    sources.append(SimpleNamespace(produces=frozenset(set(_land_colors(land)) or {"C"}), name=_name(land)))
    best: tuple[int, str] | None = None
    cards: list[tuple[Any, str | None]] = [(card, None) for card in state.get("hand") or []]
    if state.get("command"):
        # Our commanders in the command zone, at what casting them costs now (tax included).
        from arenamcp.board_model import our_commanders

        cards += [(c.card, c.cost) for c in our_commanders(state, local) if c.cost]
    for card, cost in cards:
        if not isinstance(card, dict) or card.get("instance_id") == land.get("instance_id"):
            continue
        role = card_role(card)
        if role not in ("creature", "removal"):
            continue
        info = hand_card(
            card if cost is None else {**card, "mana_cost": cost, "rarity": card.get("rarity") or "-"}
        )
        if "x" in str(card.get("mana_cost") or "").lower() or info.mana_value > len(sources):
            continue
        if not _pip_matching(info.pips, sources):
            continue
        if role == "creature":
            body = _body(card, turn + 1, our_rules)
            if body is None:
                continue
            loss = _life_loss(state, add_body=body)
            what = f"cast {_name(card)} ({body['power']}/{body['toughness']})"
        else:
            killable = [b for b in enemy if _kills(card, b, ours=friends)]
            if not killable:
                continue
            target = max(killable, key=lambda b: (b["power"], b["toughness"]))
            loss = _life_loss(state, remove_id=target["instance_id"])
            what = f"cast {_name(card)} on {target['name']}"
        if best is None or loss < best[0]:
            best = (loss, what)
    return best


def turns_label(our_turn: bool, turn: int) -> int:
    """Absolute turn number that 'T' refers to (this turn if ours, else our next)."""
    return turn if our_turn else turn + 1


__all__ = [
    "BoardAssessment",
    "GuardVerdict",
    "NON_BOARD_ROLES",
    "ROLES",
    "ROLE_AGGRESSOR",
    "ROLE_CONTROL",
    "ROLE_DEFENDER",
    "ROLE_RACE",
    "Threat",
    "TurnProjection",
    "assess",
    "card_role",
    "option_role",
    "removal_reach",
    "role_guard",
    "turns_label",
]
