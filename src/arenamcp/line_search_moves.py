"""The line search's transition model: our turn's moves and the opponent's reply.

``line_search`` drives the search; this module says what a turn does. Our turn:
one land drop (one option per land class), up to three spells (each spell's
variants: plain cast, one per 'Choose one —' mode, removal on each of the top
two legally targetable bodies it kills, targeted damage as one piece of a
combined kill (two or three burn spells, or one plus the damage already
marked this turn, reaching a creature's toughness: ``HandSpell.chips``), an
'any target' spell aimed at the opponent or at one of their planeswalkers,
landcycling for its basic type) and an attack (none / all / the ones that
survive their best blocks / the evasive ones / the survivors with the fewest
that can finish a planeswalker of theirs sent at it, their blockers protecting
it first). Their planeswalkers (``Node.walkers``) are worth
``combat_solver.walker_value`` to them and go up one loyalty each of their
turns (more when their own life-gain trigger feeds them). Lifelink heals its
side with the combat damage. A cast pays its additional
mana cost and the life it costs; a set that would kill us is never cast, nor is
a spell whose "can't cast unless" condition is unmet. A creature of ours that
returns to hand at our end step (Fleeting Effigy) is gone before their turn,
back in hand; one entering under their Authority of the Consuls enters tapped
and gains them life. Their turn: everything
untaps, they play a land (one, plus one per "play an additional land"
permanent) and their landfall creatures grow (``landfall_growth``), as do
their creatures that grow when they cast a spell, by one spell's worth
(``cast_growth``: a +1/+1 counter trigger or connive), and those that grow on
their life gain and draws (``TheirEngines``: their planeswalkers' gain
abilities, their gain triggers, our creatures under their Consuls, one draw a
turn or two once they have shown a cantrip; a lifelink hit of theirs grows them
for the next attack; nothing while "players can't gain life"); their drain
engines (``drain_engine``) deal us their damage; their life-threshold anthems
switch on or off with their life after our attack (``life_anthem``, Righteous
Valkyrie: ledger #177); our held
instants answer (a flash creature or token maker blocks only while blocks are
still to come), and the opponent attacks with the policy worst for us
(``ParanoidOpponent``). Mana, colours, summoning sickness and tapped creatures
follow the rules the board assessment uses; all combat is
``board_assessment._combat_life``. Held damage removal is worth killing a
creature they could cast while we are not under a clock (``held_removal_floor``).
Pure: no I/O, no LLM, nothing logged.

What a cast does: a creature's body; creature tokens a spell (or a mode, or a
permanent's enters trigger) makes when their count, power/toughness and
keywords are printed ("Create two 3/1 red Elemental creature tokens with
haste."); removal, bounce, life gain, damage to the opponent, damage to each
of their creatures (``Variant.sweep``), and a mana rock, from the spell's text
or a permanent's own enters trigger (not a fight; not one with a condition or
a reflexive "when you do", ``applied_text``; not a trigger on another
permanent entering; nothing under Hushbringer). A creature's fixed ping to
players when its side attacks (Hired Claw, ``attack_pings``) hits on each
attack. ``unmodelled_effect`` names what a card does beyond that (an aura, a
pump, a copy token, another attack trigger, an X spell, ...): a line is no evidence about
such a cast, and a cast that does nothing else the search values is never
made in a line.
"""

from __future__ import annotations

import dataclasses
import functools
import re
import time
from dataclasses import dataclass, field
from itertools import combinations, product
from types import SimpleNamespace
from typing import Any, Protocol

from arenamcp.attack_potential import annotated, attack_round
from arenamcp.board_assessment import (
    _LIFEGAIN,
    _MANA_ABILITY,
    _MAX_BLOCK_OPTIONS,
    _MAX_SPELLS_PER_TURN,
    _MAX_TOKEN_BODIES,
    _OR_ANOTHER,
    _QUOTED,
    _body,
    _body_colors,
    _cast_text,
    _combat_life,
    _controller,
    _enters_tapped,
    _flying,
    _int,
    _is_creature,
    _is_land,
    _kills,
    _lifelink,
    _name,
    _protected_from,
    _reach_or_flying,
    _removal_clauses,
    _Spell,
    _spell_value,
    _text,
    _types,
    burn_damage,
    card_role,
    engine_role,
    extra_life_cost,
    face_damage,
    harms_players,
    life_loss,
    made_tokens,
    own_subject,
    removal_reach,
    ward_cost,
)
from arenamcp.board_model import BoardModel, _regular_damage_part, cast_bans, spell_banned
from arenamcp.cast_triggers import cast_pumps, spell_kinds
from arenamcp.combat_keywords import has_combat_keyword
from arenamcp.combat_solver import walker_value
from arenamcp.combat_strategy import loyalty
from arenamcp.cost_reduction import parse_cost_reduction, reduced_mana_value, reduction_amount
from arenamcp.mulligan_policy import _land_colors, _mana_value, _pip_matching, hand_card

# Outcome classes, compared before timing and value.
WIN, ALIVE, DEAD = 2, 1, 0
NOTHING = "nothing"
LABELS = ("T", "T+1", "T+2")
_MAX_LAND_CLASSES = 3
_TARGETS = 2  # removal targets tried per spell: the top two killable bodies
_BASICS = {"W": "Plains", "U": "Island", "B": "Swamp", "R": "Mountain", "G": "Forest", "C": "Wastes"}
_CHOOSE_ONE = re.compile(r"\bchoose one\s*[—–-]", re.IGNORECASE)
# Typed landcycling finds that basic only; basic landcycling (and landcycling) lets us choose.
_CYCLE_KIND = re.compile(
    r"\b(basic landcycling|landcycling|plainscycling|islandcycling|swampcycling|mountaincycling"
    r"|forestcycling|wastescycling)\b"
)
_CYCLE_FETCH = {
    "plainscycling": "W", "islandcycling": "U", "swampcycling": "B", "mountaincycling": "R",
    "forestcycling": "G", "wastescycling": "C",
}  # fmt: skip
# Blocks are declared before we get priority in these steps: a flash creature cast now can't block.
_BLOCKS_LOCKED = ("Step_DeclareBlock", "Step_FirstStrikeDamage")
# Creature tokens with a printed count and power/toughness: "create two 3/1 red elemental
# creature tokens with haste", "create a 2/2 colorless wizard soldier creature token named cadet".
_TOKEN = re.compile(
    r"\bcreate (?P<n>a|an|one|two|three|four|five|six|\d+) (?P<p>\d+)/(?P<t>\d+) (?P<desc>[a-z ,'-]*?)"
    r"\bcreature tokens?\b(?P<rest>[^.\n]*)"
)
_COUNT_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
# A "create ..." clause that makes creatures (as opposed to Food, Treasure or Clue tokens).
_CREATES_CREATURES = re.compile(
    r"\bcreature tokens?\b|\bcop(?:y|ies)\b|\btokens? that's\b|\btokens? that are\b"
)
# Token clauses read as unknown: a variable count, copies, tokens that enter attacking.
_TOKEN_UNREAD = re.compile(r"\b(?:for each|equal to|where x|tapped|attacking|blocking|cop(?:y|ies))\b")
# A count read from the board when it resolves (``named_token_specs``, ledger #157): Hare Apparent's
# "create a number of 1/1 white Rabbit creature tokens equal to the number of other creatures you
# control named Hare Apparent" (its own name reads "~").
_NAMED_COUNT = re.compile(
    r"\bcreate (?:a number of|x) (?P<p>\d+)/(?P<t>\d+) (?P<desc>[a-z ,'-]*?)\bcreature tokens?\b(?P<rest>[^.\n]*?)"
    r",?\s*(?:where x is|equal to) the number of (?P<other>other )?creatures you control named "
    r"(?P<named>~|[a-z0-9' ,-]+?)\s*(?=[.\n]|$)"
)
# "Create X 2/2 ... creature tokens named Cadet, where X is the amount of life you gained this turn."
_LIFE_SCALED_TOKENS = re.compile(
    r"\bcreate x ((?:\d+)/(?:\d+) [^.\n]*?\bcreature tokens?\b[^.\n]*?),? where x is the (?:amount of )?"
    r"life you(?:'ve)? gained this turn"
)
# "Put a +1/+1 counter on target creature." (Seed Suture): a lasting +N/+N on our biggest body.
_COUNTER_ON_TARGET = re.compile(
    r"\bput (?P<n>a|an|one|two|three|\d+) \+1/\+1 counters? on (?:another )?target creature\b"
)
# A creature whose text matters to this turn's attack even while it is summoning sick.
_ATTACK_RELEVANT = re.compile(
    r"\bflash\b|\bhaste\b|\bcreatures? you control (?:get|gets|have|has|gain)\b|\battacks?\b"
    r"|\bcan't block\b|\btarget creature\b|\bcombat\b|\btap target\b"
    r"|\b(?:number of|for each) (?:other )?[a-z' -]*?\bcreatures? you control\b"
)
# Ids of countered bodies (a fresh id per body and count, below the token ids).
_COUNTER_IDS = 800_000_000
# Ids of their attackers pumped by landfall for one attack (``Moves._pumped``).
_PUMP_IDS = 700_000_000
# "For every 3 life you gained this game, create an additional one of those tokens."
_LIFE_GAME_EXTRA = re.compile(
    r"\bfor (?:every|each) (\w+) life you(?:'ve)? gained this game, create an additional one of those tokens\b"
)
_TOKEN_CONDITION = re.compile(r"\b(?:if|unless|for each|otherwise|instead)\b")
_TOKEN_TEMPORARY = re.compile(
    r"\b(?:exile|sacrifice) (?:it|them|that token|those tokens)\b[^.]*\bnext end step\b"
)
_TOKEN_HASTE_NOW = re.compile(r"\b(?:they|it|those tokens|that token) gains? haste\b")
_TOKEN_KEYWORDS = (
    "flying", "haste", "vigilance", "lifelink", "deathtouch", "menace", "reach", "trample",
    "first strike", "double strike", "defender", "indestructible",
)  # fmt: skip
_TOKEN_DESC_SKIP = frozenset(
    {"white", "blue", "black", "red", "green", "colorless", "artifact", "enchantment", "legendary", "snow", "and"}
)  # fmt: skip
# Token instance ids: past any real instance id, one per token body a search creates.
_TOKEN_IDS = 900_000_000
# A permanent's triggered ability: "when(ever) <subject> <event> <rest of the trigger>, <effect>"
# (the effect runs to the end of its line, so a reflexive "When you do, ..." stays with it).
# "When ~ enter" is the card's own enters trigger under a plural name (The Notary Hobbits).
_CREATURE_TRIGGER = re.compile(
    r"\bwhen(?:ever)?\b(?P<subject>[^.,]*?)\b(?P<event>enters|(?<=~ )enter|attacks|dies|cast this spell)\b"
    r"(?P<rest>[^.,]*),\s*(?P<effect>[^\n]*)"
)
# An effect that applies only sometimes: an intervening or later 'if' ("if you control six or
# more lands", "if it was kicked", "if you do"), a reflexive "when you do" or any later sentence
# that is itself a trigger, a variable amount.
_EFFECT_CONDITION = re.compile(
    r"\b(?:if|unless|otherwise|instead|for each|equal to|where x|as long as)\b"
    r"|\bwhen(?:ever)? you do\b|(?:^|[.;]\s*)when(?:ever)?\b"
)
# A sentence that only moves cards (no board or life change within the search's horizon).
_CARD_FLOW = re.compile(
    r"^(?:then |you may |(?:target|each|that) (?:player|opponent)s? (?:may )?)?"
    r"(?:draws?|scry|scries|surveils?|looks? at|mills?|discards?|investigates?|connives?|reveals?|shuffles?)\b"
)
# Effects whose triggers creatures entering don't cause (Hushbringer, Torpor Orb): a creature's
# enters trigger then does nothing.
_NO_ENTERS_TRIGGERS = re.compile(
    r"\bcreatures entering(?: the battlefield)?(?: or dying)? don't cause abilities\b"
)


def card_flow(text: str) -> bool:
    """Every sentence of ``text`` (lower case; a trigger's head is skipped) only moves cards:
    draw, scry, surveil, mill, discard, reveal, look at..."""
    sentences: list[str] = []
    for line in str(text or "").splitlines():
        line = re.sub(r"^\s*(?:•\s*)?(?:when(?:ever)?\b[^,]*,\s*)?", "", line.strip())
        if re.match(r"choose (?:one|two|one or more|any number)\b", line):
            continue  # a modal header: its bullets follow
        sentences += [s.strip(" ,") for s in re.split(r"[.;]\s*", line) if s.strip(" ,")]
    return bool(sentences) and all(_CARD_FLOW.match(s) for s in sentences)


# A fixed ping to players when we attack: "Whenever you attack with one or more Lizards, this
# creature deals 1 damage to target opponent." (Hired Claw), "Whenever ~ attacks, it deals 1
# damage to each opponent", "... defending player loses 2 life". Its subject is who must attack:
# anyone ("you attack"), a creature type ("with one or more lizards") or the card itself.
_ATTACK_PING = re.compile(
    r"^whenever (?:(?P<anyone>you attack)(?: with one or more (?P<kind>[a-z'-]+))?"
    r"|(?:~|this creature|cardname) attacks)\s*,\s*"
    r"(?:(?:~|this creature|it|cardname) deals (?P<n>\d+) damage to (?:target opponent|each opponent|any target"
    r"|target player|(?:the )?defending player|target (?:player|opponent) or planeswalker)"
    r"|(?:each opponent|target opponent|(?:the )?defending player) loses (?P<m>\d+) life)\.?$"
)


def attack_ping_line(line: str) -> tuple[int, str] | None:
    """(damage, who must attack: 'any' / 'self' / a creature type) of one lower-case rules line
    (name as '~') that is a fixed attack-trigger ping to players, else None."""
    match = _ATTACK_PING.match(line.strip())
    if match is None:
        return None
    amount = int(match.group("n") or match.group("m"))
    if not match.group("anyone"):
        return amount, "self"
    kind = match.group("kind") or ""
    if kind.endswith("ves"):  # elves, wolves
        return amount, kind[:-3] + "f"
    return amount, (re.sub(r"(?<!s)s$", "", kind) if kind else "any")


# A creature that grows when a land enters under its controller's control: "Landfall — Whenever a
# land you control enters, put a +1/+1 counter on this creature." (Sazh's Chocobo), or "... this
# creature gets +2/+2 until end of turn".
_LANDFALL = re.compile(
    r"^(?:landfall\s*[—–-]\s*)?whenever a land (?:you control )?enters(?: the battlefield)?"
    r"(?: under your control)?\s*,\s*(?:put (?P<n>a|an|one|two|three|\d+) \+1/\+1 counters? on "
    r"(?:~|this creature|it)|(?:~|this creature|it) gets \+(?P<p>\d+)/\+(?P<t>\d+) until end of turn)\.?$"
)
# "You may play an additional land on each of your turns." (Icetill Explorer, Azusa, Exploration).
_EXTRA_LAND = re.compile(r"\byou may play (?:an|one|two|\d+) additional lands?\b")
# "Landfall — Whenever a land you control enters, double Tifa Lockhart's power until end of turn."
_LANDFALL_DOUBLE = re.compile(
    r"^(?:landfall\s*[—–-]\s*)?whenever a land (?:you control )?enters(?: the battlefield)?"
    r"(?: under your control)?\s*,\s*double (?:~|cardname|this [a-z]+|its)(?:'s)? power until end of turn\.?$"
)
# "If a land or Bird you control entering the battlefield causes a triggered ability of a permanent
# you control to trigger, that ability triggers an additional time." (Traveling Chocobo)
_LANDFALL_DOUBLER = re.compile(
    r"\bif a land (?:or [a-z]+ )?you control entering(?: the battlefield)? causes a triggered ability of a "
    r"permanent you control to trigger, that ability triggers an additional time\b"
)
# Basic lands an effect puts onto the battlefield: "Search your library for a basic land card, put it
# onto the battlefield tapped" (Escape Tunnel), "... up to two basic land cards, put them onto ...".
_LAND_FETCH = re.compile(
    r"\bsearch your library for (?:up to )?(?P<n>a|an|one|two|three|\d+) basic land cards?,? (?:and )?"
    r"put (?:it|them|those cards) onto the battlefield\b"
)
# The card itself: its name ('~'), Arena's CARDNAME, "this Aura", "this land", ...
_SELF = r"(?:~|cardname\b|this [a-z]+\b)"
# Ordeal of Nylea: "Whenever enchanted creature attacks, put a +1/+1 counter on it. Then if it has
# three or more +1/+1 counters on it, sacrifice this Aura." and "When you sacrifice this Aura, ...".
_ORDEAL = re.compile(
    r"\bwhenever enchanted creature attacks, put a \+1/\+1 counter on it\. then if it has "
    r"(?P<k>two|three|four|five|\d+) or more \+1/\+1 counters on it, sacrifice " + _SELF
)
_ON_SACRIFICE = re.compile(r"^when you sacrifice " + _SELF + r",\s*(?P<effect>.*)$")
_SACRIFICE_SELF = re.compile(r"\bsacrifice " + _SELF)
# A creature that grows when its controller casts a spell: "Whenever you cast a black spell from
# your hand, Baron Helmut Zemo connives." (a nonland discard puts a +1/+1 counter on it), "Whenever
# you cast a noncreature spell, put a +1/+1 counter on this creature." Only an unconditional "a/an
# <kind> spell" trigger; "your second spell each turn", "a spell that targets ~" and temporary
# pumps (prowess) are not growth.
_CAST_GROWTH = re.compile(
    r"^whenever you cast an? (?:[a-z-]+ |or ){0,4}spells?(?: from your hand)?\s*,\s*"
    r"(?:put (?P<n>a|an|one|two|three|\d+) \+1/\+1 counters? on (?:~|this creature|it|cardname)"
    r"|(?:~|this creature|it|cardname) connives)\.?$"
)


def landfall_growth(card: dict) -> tuple[int, int, int]:
    """(+1/+1 counters, temporary +power, temporary +toughness) the card's own landfall gives it per land."""
    counters = power = toughness = 0
    text = _QUOTED.sub("", _text(card))
    if "land" not in text:
        return 0, 0, 0
    for line in _own_lines(text):
        match = _LANDFALL.match(line)
        if match is None:
            continue
        if match.group("n"):
            counters += _COUNT_WORDS.get(match.group("n")) or _int(match.group("n")) or 0
        else:
            power, toughness = power + int(match.group("p")), toughness + int(match.group("t"))
    return counters, power, toughness


def landfall_doubles(card: dict) -> int:
    """How many times the card's own landfall doubles its power per land (Tifa Lockhart: 1).

    Ledger #96 (match 18b1601a T10, 2026-10-19): Tifa's two doublings (1 -> 4)
    and Sazh's Chocobo's counters met our 9 life; neither was in the lines.
    """
    text = _QUOTED.sub("", _text(card))
    if "double" not in text:
        return 0
    return sum(1 for line in _own_lines(text) if _LANDFALL_DOUBLE.match(line))


def landfall_doubler(card: dict) -> bool:
    """A land entering under its controller makes their triggered abilities trigger an additional
    time (Traveling Chocobo): each land is two landfall triggers."""
    return bool(_LANDFALL_DOUBLER.search(_QUOTED.sub("", _text(card))))


def land_fetch(card: dict, host: dict | None = None) -> int:
    """Basic lands the permanent can put onto the battlefield on its controller's next turn: an
    activated "Sacrifice ~: Search your library for a basic land card, put it onto the battlefield"
    (Escape Tunnel), or Ordeal of Nylea's sacrifice once ``host`` (the enchanted creature) reaches
    its counters on its next attack (it then also gets that attack's +1/+1 counter: ``ordeal_host``).
    0 when it can't, or when that is not written in a form read here."""
    text = _QUOTED.sub("", _text(card))
    if "search your library" not in text:
        return 0

    def count(effect: str) -> int:
        match = _LAND_FETCH.search(effect)
        return (_COUNT_WORDS.get(match.group("n")) or _int(match.group("n")) or 0) if match else 0

    total = 0
    for line in _own_lines(text):
        head, colon, effect = line.partition(":")
        if colon and _SACRIFICE_SELF.search(head) and "." not in head and not re.search(r"\bonly\b", effect):
            total += count(effect)
            continue
        sacrifice = _ON_SACRIFICE.match(line)
        if sacrifice is not None and ordeal_host(card, host):
            total += count(sacrifice.group("effect"))
    return total


def ordeal_host(card: dict, host: dict | None) -> bool:
    """``card`` is an Ordeal (``_ORDEAL``) on ``host`` and the +1/+1 counter of the host's next
    attack makes it sacrificed: the host has the counters it needs but one."""
    if host is None:
        return False
    match = _ORDEAL.search(_QUOTED.sub("", _text(card)))
    if match is None:
        return False
    need = _COUNT_WORDS.get(match.group("k")) or _int(match.group("k")) or 0
    return need > 0 and plus_counters(host) + 1 >= need


def plus_counters(card: dict) -> int:
    """+1/+1 counters on a card ("P1P1" from the GRE, "+1/+1", "CounterType_P1P1")."""
    counters = card.get("counters") or {}
    if not isinstance(counters, dict):
        return 0
    return sum(
        _int(count) or 0
        for kind, count in counters.items()
        if str(kind).lower().replace("countertype_", "") in ("p1p1", "+1/+1")
    )


def cast_growth(card: dict) -> int:
    """+1/+1 counters the card's own "whenever you cast a ... spell" trigger gives it per spell (connive: 1).

    Ledger #82 (match 772233af T14, 2026-10-19): Baron Helmut Zemo connives on
    each black spell its controller casts; the line projected a static 4/4
    ("dead on T17") and Mabel made it the 5/5 whose 8 damage met our 8 life on T15.
    """
    text = _QUOTED.sub("", _text(card))
    if "cast" not in text:
        return 0
    counters = 0
    for line in _own_lines(text):
        match = _CAST_GROWTH.match(line)
        if match is None:
            continue
        found = match.group("n")
        counters += (_COUNT_WORDS.get(found) or _int(found) or 0) if found else 1
    return counters


# Ledger #177 (match c0af235f T12, 2026-10-19): Righteous Valkyrie, "As long as you have at least 7
# life more than your starting life total, creatures you control get +2/+2.", was on at 32 of a
# 25 start; one point of our damage would have switched it off for all seven of their creatures.
_LIFE_ANTHEM = re.compile(
    r"^as long as you have at least (?P<n>\w+) life more than your starting life total, "
    r"(?P<who>other creatures you control|creatures you control|~|this creature) gets? "
    r"\+(?P<p>\d+)/\+(?P<t>\d+)\.?$"
)
_NUMBER_WORDS = {**_COUNT_WORDS, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_ANTHEM_IDS = 650_000_000


def life_anthem(card: dict) -> tuple[int, int, int, str] | None:
    """(life above the starting total it needs, +power, +toughness, 'all' / 'others' / 'self') of
    the card's "As long as you have at least N life more than your starting life total, creatures
    you control get +P/+T" (Righteous Valkyrie, Leyline of Hope); None without one."""
    text = _QUOTED.sub("", _text(card))
    if "starting life total" not in text:
        return None
    for line in _own_lines(text):
        match = _LIFE_ANTHEM.match(line)
        need = match and (_NUMBER_WORDS.get(match.group("n")) or _int(match.group("n")))
        if need:
            who = match.group("who")
            scope = "others" if who.startswith("other") else "all" if who.startswith("creatures") else "self"
            return need, int(match.group("p")), int(match.group("t")), scope
    return None


def life_anthem_facts(model: BoardModel) -> list[str]:
    """Board-facts lines for their life-threshold anthems (``life_anthem``): '1 damage switches off
    Righteous Valkyrie's +2/+2 (they are at 32: on from 32, starting life 25)' while it is on, how
    much life switches it on while it is off (and no more than N away)."""
    lines = []
    for card in model.battlefield:
        if not isinstance(card, dict) or _controller(card) != model.opponent:
            continue
        found = life_anthem(card)
        if found is None:
            continue
        need, power, toughness, scope = found
        threshold = model.starting_life + need
        whose = {"all": "", "others": " to their other creatures", "self": ""}[scope]
        bonus = f"{_name(card)}'s +{power}/+{toughness}{whose}"
        where = f"they are at {model.opp_life}: on from {threshold}, starting life {model.starting_life}"
        if model.opp_life >= threshold:
            damage = model.opp_life - threshold + 1
            lines.append(f"{damage} damage switches off {bonus} ({where})")
        elif threshold - model.opp_life <= need:  # within reach of their life gain
            gap = threshold - model.opp_life
            lines.append(f"{bonus} is off; {gap} life they gain switches it on ({where})")
    return lines


# Ledger #180 (match c0af235f T8/T12): Faramir, Prince of Ithilien — "... At the beginning of that
# player's next end step, you draw a card if they didn't attack you that turn. Otherwise, create
# three 1/1 white Human Soldier creature tokens." Each of our holds drew them a card.
_HOLD_DRAW = re.compile(
    r"\bdraw (?P<n>a|one|two|three|\d+) cards? if (?:they|that player) (?:didn't|did not) attack you\b"
    r"|\bif no creatures attacked you this turn, (?:you )?draw (?P<m>a|one|two|three|\d+) cards?"
)
_OTHERWISE_TOKENS = re.compile(
    r"\botherwise, create (?P<n>a|an|one|two|three|four|five|\d+) (?P<p>\d+)/(?P<t>\d+)\b"
)
# A card they draw, in line value (our hand counts 0.3 x each card's value; theirs are unseen).
PUNISH_CARD_VALUE = 1.5


def attack_punisher(card: dict) -> tuple[int, int, int, int]:
    """(cards they draw when we don't attack them, tokens they create when we do, the tokens'
    power, toughness) of a "draw a card if they didn't attack you" permanent; zeros without one."""
    text = _QUOTED.sub("", _text(card))
    if "attack" not in text or "draw" not in text:
        return 0, 0, 0, 0
    draw = _HOLD_DRAW.search(text)
    if draw is None:
        return 0, 0, 0, 0
    word = draw.group("n") or draw.group("m")
    cards = _COUNT_WORDS.get(word) or _int(word) or 1
    tokens = _OTHERWISE_TOKENS.search(text)
    if tokens is None:
        return cards, 0, 0, 0
    count = _COUNT_WORDS.get(tokens.group("n")) or _int(tokens.group("n")) or 1
    return cards, count, int(tokens.group("p")), int(tokens.group("t"))


def hold_punishers(model: BoardModel) -> tuple[float, str]:
    """What holding our attack costs this turn against their "draw if you didn't attack"
    permanents (``attack_punisher``), net of the tokens attacking gives them instead, in line value
    (positive: holding costs more), and the text: 'holding draws them a card (Faramir, Prince of
    Ithilien; attacking gives them three 1/1 tokens instead)'. Our turn before our attack only; a
    permanent that entered this turn chose no one at their last end step."""
    if not model.our_turn or not model.our_attack_pending:
        return 0.0, ""
    cost, parts = 0.0, []
    for card in model.battlefield:
        if not isinstance(card, dict) or _controller(card) != model.opponent:
            continue
        entered = _int(card.get("turn_entered_battlefield"))
        if entered is not None and entered >= model.turn:
            continue
        cards, tokens, power, toughness = attack_punisher(card)
        if not cards:
            continue
        cost += PUNISH_CARD_VALUE * cards - tokens * (power + 0.5 * toughness)
        drawn = "a card" if cards == 1 else f"{cards} cards"
        instead = f"; attacking gives them {tokens} {power}/{toughness} tokens instead" if tokens else ""
        parts.append(f"holding draws them {drawn} ({_name(card)}{instead})")
    return cost, "; ".join(parts)


# --- their growth and drain engines (ledger #128, #129, #132, #133, #143) ----------------------
#
# What the opponent's permanents do on each of their turns beyond combat, read from the rules
# text only. A creature that grows when they gain life ("Whenever you gain life, put a +1/+1
# counter on this creature.": Unflinching Hortimancer, Ajani's Pridemate; "... on each Angel you
# control": Lyra, Archangel of Dawn) or when they draw ("Whenever you draw a card, ...": Ravenhill
# Flock; "... your second card each turn, ...": Lakeshore Apothecary), and a recurring trigger
# that deals us damage or makes us lose life (Biotech Specialist: "Whenever you sacrifice an
# artifact, this creature deals 2 damage to target opponent."). Conservative: an unconditional
# trigger of a known event, a fixed amount, nothing temporary.

_COUNTS = r"(?P<n>a|an|one|two|three|\d+)"
# "Whenever you gain life, put a +1/+1 counter on this creature." / "... on each Angel you
# control." / "... put a loyalty counter on Ajani." ("for the first time each turn": once a turn).
_LIFEGAIN_GROWTH = re.compile(
    r"^whenever you gain life(?P<first> for the first time each turn)?\s*,\s*put " + _COUNTS
    + r" (?P<kind>\+1/\+1|loyalty) counters? on (?P<who>[^.,]+?)\.?$"
)  # fmt: skip
_DRAW_GROWTH = re.compile(
    r"^whenever you draw (?P<which>a card|your second card each turn)\s*,\s*put " + _COUNTS
    + r" \+1/\+1 counters? on (?P<who>[^.,]+?)\.?$"
)  # fmt: skip
_TEAM = re.compile(r"^each (?:other )?(?P<kind>[a-z' -]+?) you control$")
# A planeswalker's loyalty ability that gains life: Scryfall's "0: You gain 1 life." or Arena's
# card text, which leaves the loyalty costs out ("You gain 1 life." on its own line).
_WALKER_GAIN = re.compile(r"^(?:[+−–-]?\d+:\s*)?you gain (?P<n>\d+) life\b")
# Their life-gain triggers on a turn of theirs before their attack, keyed by what makes them
# happen: their upkeep (or another of their phases) or a creature of theirs entering (paranoid:
# one a turn, as their spell is one a turn), and a creature of OURS entering (Authority of the
# Consuls: "Whenever a creature an opponent controls enters, you gain 1 life.").
_GAIN_TRIGGER = re.compile(
    r"^(?:when(?:ever)? (?P<who>(?:a|another) (?:nontoken )?creature(?: (?:you|an opponent) controls?)?) "
    r"enters(?: the battlefield)?(?: under (?P<under>your|an opponent's) control)?"
    r"|at the beginning of (?P<phase>your upkeep|your end step|each upkeep|your precombat main phase"
    r"|your first main phase|combat on your turn))"
    r",\s*(?:you may )?(?:you )?gain (?P<n>\d+) life\.?$"
)
# "Players can't gain life." (Giant Cindermaw); "Your opponents can't gain life." on our side.
_NO_GAIN = re.compile(r"\b(?:players|each player) can't gain life\b")
_NO_GAIN_THEM = re.compile(r"\byour opponents can't gain life\b")
_NO_GAIN_SELF = re.compile(r"\byou can't gain life\b")
# "Creatures your opponents control enter tapped." (Authority of the Consuls, Blind Obedience).
_ENTER_TAPPED = re.compile(
    r"\b(?:artifacts and |nonland )?(?:creatures|permanents) your opponents control enter(?: the battlefield)? tapped\b"
)
# A recurring trigger that hurts us: "Whenever you sacrifice an artifact, this creature deals 2
# damage to target opponent.", "At the beginning of your upkeep, each opponent loses 1 life."
# An ability word may lead ("Landfall — ...").
_DRAIN = re.compile(
    r"^(?:[a-z' ]+\s*[—–-]\s*)?(?P<head>(?:whenever|at the beginning of) [^,]+),\s*"
    r"(?:(?:~|cardname|this [a-z]+|it|they) deals? (?P<n>\d+|x) damage to (?:target opponent|each opponent"
    r"|any target|target player|each player|target player or planeswalker|target opponent or planeswalker)"
    r"|(?:target opponent|each opponent|target player|each player) loses (?P<m>\d+|x) life)"
    r"(?P<rest>[^\n]*)$"
)
# A drain counted by their permanents (ledger #197, match 45de8f40: The Scarab God, "At the beginning
# of your upkeep, each opponent loses X life and you scry X, where X is the number of Zombies you
# control." drained 3, 4, 5 and 7 and the clocks saw none of it): "where X is the number of <kind>s
# you control", "... for each <kind> you control".
_DRAIN_COUNT = re.compile(
    r",?\s*where x is the number of (?P<x>[a-z' -]+?) you control\b|\s+for each (?P<each>[a-z' -]+?) you control\b"
)
_DRAIN_SACRIFICE = re.compile(
    r"^whenever you sacrifice (?P<count>an?|one or more|another) (?P<what>[a-z ]+?)s?$"
)
_DRAIN_PHASE = re.compile(
    r"^at the beginning of (?P<whose>your|each|the|each opponent's|each player's) "
    r"(?:upkeep|end step|precombat main phase|first main phase|draw step|combat on your turn)"
)
_ARTIFACT_KINDS = ("artifact", "food", "clue", "treasure", "blood", "map", "lander", "powerstone", "gold")
# An enters ping (match b62b73fc, 2026-10-19): "Whenever a creature you control enters, this enchantment
# deals 1 damage to each opponent." (Impact Tremors): once per creature entering, their token makers' too.
_DRAIN_ENTERS = re.compile(
    r"^whenever (?P<count>a|an|another|one or more) (?P<nontoken>nontoken )?[a-z' -]+? (?:you control )?"
    r"enters?(?: the battlefield)?(?: under your control)?$"
)
# A sacrifice outlet: "Sacrifice a creature: This enchantment deals 1 damage to any target." (Goblin
# Bombardment): every creature of theirs is that much reach, at instant speed.
_SAC_OUTLET = re.compile(
    r"^(?P<cost>(?:\{[^}]+\},\s*)*)sacrifice (?:a|an|another) (?:creature|[a-z' -]+?)\s*:\s*"
    r"(?:(?:~|cardname|this [a-z]+|it) deals (?P<n>\d+) damage to (?:any (?:other )?target|each opponent"
    r"|target opponent|target player|target player or planeswalker|target opponent or planeswalker)"
    r"|(?:each opponent|target opponent|target player) loses (?P<m>\d+) life)\b"
)
_MAX_ACTIVATIONS = 3  # a token engine's activations a turn, at most
_MAX_TOKENS = 16  # tokens one engine makes a turn, at most
# A token engine (ledger #172): an activated ability paid with mana ("{1}{G}{G}{G}: Create X 1/1 green
# Squirrel creature tokens, where X is the number of Squirrels you control.") or a trigger once a turn
# ("At the beginning of your upkeep, create ...", "Whenever ~ attacks, create ..."); an ability word may
# lead. The count is printed or "where X is the number of <kind>s you control".
_TOKEN_ENGINE = re.compile(
    r"^(?:[^:{]*?[—–]\s*)?(?:(?P<cost>(?:\{[^}]+\}(?:,\s*)?)+)\s*:\s*"
    r"|(?P<trigger>at the beginning of your (?:upkeep|end step|precombat main phase)|whenever (?:~|this creature)"
    r"(?: enters or)? attacks),\s*)"
    r"create (?P<n>x|a|an|one|two|three|four|\d+) (?P<p>\d+)/(?P<t>\d+) (?P<desc>[a-z ,'-]*?)\bcreature tokens?"
    r"(?:,? where x is the number of (?P<kind>[a-z' -]+?) you control)?\.?$"
)
# A land's mana doubled: "Whenever a land's ability causes you to add one or more mana of the chosen color,
# add an additional one mana of that color." (Caged Sun), "Whenever you tap a land for mana, add ...".
_LAND_MANA_DOUBLER = re.compile(
    r"\bwhenever (?:a land's ability causes you to add|you tap a land for mana|a (?:land|forest|plains|island|swamp"
    r"|mountain) (?:you control )?is tapped for mana)\b[^.]*\badd(?:s)? (?:an additional|one (?:additional )?mana)"
)


def _count_word(found: str | None) -> int:
    return (_COUNT_WORDS.get(found) or _int(found) or 0) if found else 1


def _key(card: dict) -> tuple[str, str]:
    """What the engine readers read of a card: its name and rules text (their cache key)."""
    return str(card.get("name") or ""), str(card.get("oracle_text") or "")


@functools.lru_cache(maxsize=4096)
def _engine_text(name: str, oracle: str) -> str:
    """``_text`` without quoted abilities, for the engine readers (cached: boards repeat cards)."""
    return _QUOTED.sub("", _text({"name": name, "oracle_text": oracle}))


def lifegain_growth(card: dict) -> tuple[int, int, tuple[tuple[str, int], ...], bool]:
    """(+1/+1 counters on itself, loyalty counters on itself, ((creature type or 'creature',
    counters on each of theirs), ...), once a turn) per life-gain event of its controller's.

    Ledger #128 (match 3e545401 T17, 2026-10-19): Unflinching Hortimancer 8/7 and
    Ajani's Pridemate 2/2 grew on Ajani Resolute's life gain to 9 + 3 = 12 into our
    12 life; the crackback guard counted 10.
    """
    return _lifegain_growth(*_key(card))


@functools.lru_cache(maxsize=4096)
def _lifegain_growth(name: str, oracle: str) -> tuple[int, int, tuple[tuple[str, int], ...], bool]:
    text = _engine_text(name, oracle)
    if "gain life" not in text:
        return 0, 0, (), False
    own = loyal = 0
    team: list[tuple[str, int]] = []
    first = False
    for line in _own_lines(text):
        match = _LIFEGAIN_GROWTH.match(line)
        if match is None:
            continue
        n, who = _count_word(match.group("n")), match.group("who").strip()
        first = first or bool(match.group("first"))
        grouped = _TEAM.match(who)
        if match.group("kind") == "loyalty":
            loyal += n if (who == "it" or own_subject(who, name)) else 0
        elif grouped is not None:
            kind = grouped.group("kind").strip()
            team.append(("creature" if kind in ("creature", "creatures") else re.sub(r"s$", "", kind), n))
        elif who == "it" or own_subject(who, name):
            own += n
    return own, loyal, tuple(team), first


def draw_growth(card: dict) -> tuple[int, int]:
    """(+1/+1 counters per card its controller draws, counters for their second card each turn).

    Ledger #143 (match 970d9b41, 2026-10-19): Ravenhill Flock grew a counter on
    every draw and Lakeshore Apothecary on the second card a turn; the lines
    projected a static 1/2 (19, 18, 17) against a real 18, 15, 10, 2.
    """
    return _draw_growth(*_key(card))


@functools.lru_cache(maxsize=4096)
def _draw_growth(name: str, oracle: str) -> tuple[int, int]:
    text = _engine_text(name, oracle)
    if "draw" not in text:
        return 0, 0
    each = second = 0
    for line in _own_lines(text):
        match = _DRAW_GROWTH.match(line)
        if match is None or not (match.group("who") == "it" or own_subject(match.group("who"), name)):
            continue
        if match.group("which") == "a card":
            each += _count_word(match.group("n"))
        else:
            second += _count_word(match.group("n"))
    return each, second


def walker_gain(card: dict) -> int:
    """Life a planeswalker's loyalty ability gains (0 without one): a repeatable gain each turn."""
    if "planeswalker" not in _types(card):
        return 0
    text = _engine_text(*_key(card))
    found = (_WALKER_GAIN.match(line) for line in _own_lines(text) if not _TRIGGER_WORDS_RE.match(line))
    return max((int(m.group("n")) for m in found if m is not None), default=0)


_TRIGGER_WORDS_RE = re.compile(r"^(?:when|whenever|at the beginning|as long as|if)\b")
# Beginning-of-combat +1/+1 counters (ledger #198: Siege Veteran, Luminarch Aspirant):
# "At the beginning of combat on your turn, put a +1/+1 counter on target creature you control."
_COMBAT_COUNTER = re.compile(
    r"^at the beginning of combat on your turn,\s*(?:you may )?put (?P<n>a|an|one|two|three|\d+) "
    r"\+1/\+1 counters? on (?:up to one )?(?P<other>another )?"
    r"(?:target (?P<kind>[a-z'-]+?)(?: you control)?|(?P<self>~|this creature|it|cardname))\b"
)


def combat_counter_growth(card: dict) -> tuple[tuple[int, str, bool], ...]:
    """((+1/+1 counters, target kind or 'self', other_only), ...) from the card's own
    'At the beginning of combat on your turn, put N +1/+1 counters on ...' triggers (ledger #198)."""
    return _combat_counter_growth(*_key(card))


@functools.lru_cache(maxsize=4096)
def _combat_counter_growth(name: str, oracle: str) -> tuple[tuple[int, str, bool], ...]:
    text = _engine_text(name, oracle)
    if "beginning of combat" not in text or "counter" not in text:
        return ()
    found = []
    for line in _own_lines(text):
        match = _COMBAT_COUNTER.match(line)
        if match is None:
            continue
        first_sentence = line.split(".")[0]
        if _EFFECT_CONDITION.search(first_sentence[match.end() :]):
            continue
        n = _count_word(match.group("n"))
        if match.group("self"):
            found.append((n, "self", False))
        else:
            raw = (match.group("kind") or "creature").strip()
            kind = "creature" if raw in ("creature", "creatures") else re.sub(r"s$", "", raw)
            found.append((n, kind, bool(match.group("other"))))
    return tuple(found)


def gain_triggers(card: dict) -> tuple[int, int, int]:
    """(their gain events a turn of theirs, events per creature of ours entering, life per creature
    of ours entering) from the card's own life-gain triggers (``_GAIN_TRIGGER``)."""
    return _gain_triggers(*_key(card))


@functools.lru_cache(maxsize=4096)
def _gain_triggers(name: str, oracle: str) -> tuple[int, int, int]:
    text = _engine_text(name, oracle)
    if "gain" not in text:
        return 0, 0, 0
    turn = ours = life = 0
    for line in _own_lines(text):
        match = _GAIN_TRIGGER.match(line)
        if match is None:
            continue
        if match.group("phase"):
            turn += 2 if match.group("phase") == "each upkeep" else 1
            continue
        who, under = match.group("who") or "", match.group("under") or ""
        if "an opponent" in who or under == "an opponent's":
            ours, life = ours + 1, life + int(match.group("n"))
        elif "you control" in who or under == "your" or who.startswith("another"):
            # Their own creature a turn (paranoid, like their one spell a turn); "another creature"
            # without a controller also sees ours (counted with the creatures we cast).
            turn += 1
            if "you control" not in who and under != "your":
                ours, life = ours + 1, life + int(match.group("n"))
    return turn, ours, life


def drain_engine(card: dict) -> tuple[tuple[int, str], ...]:
    """((damage or life loss to us, the trigger's event), ...) of the card's own recurring
    "whenever ..."/"at the beginning of ..." triggers with a printed amount; () without one
    (``_DRAIN``; a drain counted by their permanents: ``counted_drain_engine``).

    Ledger #132/#133 (match 16ed84d0, 2026-10-19): Biotech Specialist ("Whenever you
    sacrifice an artifact, this creature deals 2 damage to target opponent.") dealt
    13 of our 20; the threat model and the clocks never saw it.
    """
    return tuple((amount, event) for amount, event, kind in _drain_engine(*_key(card)) if not kind)


def counted_drain_engine(card: dict) -> tuple[tuple[int, str, str], ...]:
    """``drain_engine`` with the drains counted by their permanents: ((amount, event, kind), ...),
    the amount per permanent of that kind they control ("where X is the number of Zombies you
    control": 1 per Zombie, ``TheirEngines.kind_count``); kind "" for a printed amount. An X we
    can't read stays out (ledger #197)."""
    return _drain_engine(*_key(card))


def _drain_kind(words: str) -> str:
    words = words.strip()
    return "creature" if words in ("creature", "creatures") else re.sub(r"s$", "", words)


@functools.lru_cache(maxsize=4096)
def _drain_engine(name: str, oracle: str) -> tuple[tuple[int, str, str], ...]:
    text = _engine_text(name, oracle)
    if "damage" not in text and "loses" not in text:
        return ()
    found = []
    for line in _own_lines(text):
        match = _DRAIN.match(line)
        if match is None:
            continue
        rest, amount, kind = match.group("rest"), match.group("n") or match.group("m"), ""
        counted = _DRAIN_COUNT.search(rest)
        if counted is not None:
            if (amount == "x") != bool(counted.group("x")):
                continue  # "x damage ... for each", "1 life ... where x": not read
            kind = _drain_kind(counted.group("x") or counted.group("each"))
            rest = rest[: counted.start()] + rest[counted.end() :]
        elif amount == "x":
            continue  # an X we don't read
        if _EFFECT_CONDITION.search(rest):
            continue
        found.append((1 if amount == "x" else int(amount), match.group("head").strip(), kind))
    return tuple(found)


def token_engine(card: dict) -> tuple[tuple[int | None, bool, int, str, int, int, str], ...]:
    """((mana cost or None for a trigger, needs {T}, printed count or 0 for X, the kind X counts,
    power, toughness, the token's description), ...) of the card's own token engines
    (``_TOKEN_ENGINE``); () without one. An activated ability with a cost other than mana and {T}
    (a sacrifice, a discard) or a sorcery-speed limit is still read: the opponent acts on their turn.

    Ledger #172 (match 2b77f14d T7, 2026-10-19): The Unbeatable Squirrel Girl ("{1}{G}{G}{G}:
    Create X 1/1 green Squirrel creature tokens, where X is the number of Squirrels you control.")
    with Caged Sun doubling their Forests made 4, then 8 Squirrels on T9; the value table gave her
    a flat +3 for "token production".
    """
    return _token_engine(*_key(card))


@functools.lru_cache(maxsize=4096)
def _token_engine(name: str, oracle: str) -> tuple[tuple[int | None, bool, int, str, int, int, str], ...]:
    text = _engine_text(name, oracle)
    if "token" not in text:
        return ()
    found = []
    for line in _own_lines(text):
        match = _TOKEN_ENGINE.match(line)
        if match is None:
            continue
        cost = match.group("cost")
        if cost is not None and re.search(r"\b(?:sacrifice|discard|exile|pay \d+ life)\b", cost):
            continue
        mana = None if cost is None else _mana_value(cost)
        tap = cost is not None and bool(re.search(r"\{o?t\}", cost))
        counted = match.group("n") == "x"
        if counted and not match.group("kind"):
            continue  # an X we don't read
        n = 0 if counted else _count_word(match.group("n"))
        kind = re.sub(r"s$", "", (match.group("kind") or "").strip())
        found.append(
            (mana, tap, n, kind, int(match.group("p")), int(match.group("t")), match.group("desc").strip())
        )
    return tuple(found)


def sac_outlet(card: dict) -> int:
    """Damage (or life loss) to us the card's free sacrifice outlet deals per creature sacrificed:
    "Sacrifice a creature: This enchantment deals 1 damage to any target." (Goblin Bombardment); 0
    without one, or with a mana cost (their mana limits it).

    Match b62b73fc (2026-10-19 T13, 15:27:32): the facts said "CONTROL/STABILIZE — we win the long
    game" at 12 life; Krenko's 10 Goblins pinged us 10 through Impact Tremors and Goblin Bombardment
    sacrificed two for the last 2, 26 s later, without an attack.
    """
    return _sac_outlet(*_key(card))


@functools.lru_cache(maxsize=4096)
def _sac_outlet(name: str, oracle: str) -> int:
    text = _engine_text(name, oracle)
    if "sacrifice" not in text:
        return 0
    best = 0
    for line in _own_lines(text):
        match = _SAC_OUTLET.match(line)
        if match is None or re.search(r"\{o?[0-9wubrgcx]", match.group("cost") or ""):
            continue
        best = max(best, int(match.group("n") or match.group("m")))
    return best


def drain_triggers(
    event: str,
    counts: dict[str, int],
    *,
    draws: int = 1,
    gains: int = 0,
    drops: int = 1,
    entering: int = 1,
    after_upkeep: bool = False,
) -> int:
    """How many times a drain trigger's event (``drain_engine``) happens on a turn of theirs: one
    a turn for their own phase, two for each player's; a sacrifice trigger once per permanent of
    theirs it can sacrifice (their artifacts, tokens, ...; at least one); one cast; their draws,
    gains and land drops; their creatures ``entering`` (their token makers' output, at least one:
    Impact Tremors). 0 for an event not read here (a death, an attack: ``attack_pings``)."""
    event = " ".join(event.lower().split())
    enters = _DRAIN_ENTERS.match(event)
    if enters is not None and not re.search(r"\blands?\b", event):
        return 1 if enters.group("count") == "one or more" or enters.group("nontoken") else entering
    phase = _DRAIN_PHASE.match(event)
    if phase is not None:
        whose = phase.group("whose")
        if after_upkeep and "upkeep" in event:
            # Their upkeep this turn has already happened (ledger #198); ours is still to come
            # (review 2026-10-20: "each upkeep" and "each opponent's upkeep" drains were dropped too).
            return 0 if whose in ("your", "the") else 1
        return 2 if whose in ("each", "each player's") else 1
    sacrifice = _DRAIN_SACRIFICE.match(event)
    if sacrifice is not None:
        if sacrifice.group("count") == "one or more":
            return 1
        words = set(re.findall(r"[a-z]+", sacrifice.group("what")))
        if "permanent" in words:
            total = counts.get("artifact", 0) + counts.get("token", 0)
        elif "token" in words:
            total = counts.get("token", 0)
        elif "artifact" in words:
            total = counts.get("artifact", 0)
        elif words & set(_ARTIFACT_KINDS):  # "a Food", "a Clue"
            total = sum(counts.get(kind, 0) for kind in words & set(_ARTIFACT_KINDS))
        else:
            total = 1  # a creature, a land, ...: one a turn
        return max(1, total)
    if event.startswith("whenever you cast"):
        return 1
    if event.startswith("whenever you draw a card"):
        return draws
    if event.startswith("whenever you draw your second card"):
        return 1 if draws >= 2 else 0
    if event.startswith("whenever you gain life"):
        return min(gains, 1) if "first time" in event else gains
    if re.match(r"^whenever a land (?:you control )?enters", event):
        return drops
    return 0


def fodder_counts(battlefield, seat: int | None) -> dict[str, int]:
    """Permanents of ``seat`` a sacrifice trigger can count: 'artifact', 'token' and artifact kinds."""
    counts: dict[str, int] = {}
    for card in battlefield:
        if not isinstance(card, dict) or _controller(card) != seat:
            continue
        subtypes = " ".join(str(s) for s in card.get("subtypes") or [])
        for kind in _fodder_kinds(_types(card), subtypes, str(card.get("object_kind") or "")):
            counts[kind] = counts.get(kind, 0) + 1
    return counts


@functools.lru_cache(maxsize=1024)
def _fodder_kinds(types: str, subtypes: str, kind: str) -> tuple[str, ...]:
    # ("CardType_Artifact", "SubType_Lander": the words "artifact", "lander")
    words = set(re.findall(r"[a-z]+", f"{types} {subtypes}".lower()))
    found = []
    if "artifact" in words:
        found += ["artifact", *sorted(words & set(_ARTIFACT_KINDS[1:]))]
    if "token" in kind.lower() or "token" in words:
        found.append("token")
    return tuple(found)


def has_kind(card: dict, kind: str) -> bool:
    """The card (or a body's card) has creature type ``kind`` (its type line or subtypes)."""
    card = card.get("_card") or card
    words = f"{card.get('type_line') or ''} {' '.join(str(s) for s in card.get('subtypes') or [])}"
    return kind in re.findall(r"[a-z'-]+", words.lower().replace("subtype_", ""))


@dataclass(frozen=True)
class TheirEngines:
    """What the opponent's permanents do on each turn of theirs beyond combat (``their_engines``)."""

    gains: int = 0  # their life-gain events a turn, besides their planeswalkers' and ours entering
    gain_walkers: frozenset = frozenset()  # their planeswalkers (ids) with a life-gain ability
    per_our_creature: int = 0  # their gain events per creature that enters under our control
    life_per_our_creature: int = 0  # the life those events gain them
    draws: int = 1  # cards they draw each turn of theirs (two once they have shown card draw)
    no_gain: bool = False  # "players can't gain life" (Giant Cindermaw): no gain, no growth from it
    enter_tapped: bool = False  # creatures we cast enter tapped (Authority of the Consuls)
    static_drain: int = 0  # damage their noncreature engines deal us each turn of theirs
    static_drain_after_upkeep: int = 0  # static_drain excluding upkeep triggers already past this turn
    team: tuple = ()  # (source id, creature type or 'creature', counters per gain event)
    combat_counters: tuple = ()  # (source id, is_creature, counters, target kind or 'self', other_only)
    counts: Any = None  # ``fodder_counts`` of their permanents
    drops: int = 1  # their land drops a turn (landfall drains)
    names: tuple = ()  # 'Biotech Specialist (2 a turn)', ...: the engines the clocks count
    # Token engines (#172): their mana on their next turn and untapped now (a land doubled by Caged
    # Sun counts twice), their permanents by creature type ('creature': all their creatures) for an
    # X that counts them, and the power/toughness of a token of theirs by type (their anthems on it).
    mana: int = 0
    mana_now: int = 0
    kinds: Any = None
    token_pt: Any = None
    # Their creatures entering a turn (their token makers' output, at least one) for their enters
    # pings, and their sacrifice outlets' damage a creature (the reach of their board this turn).
    entering: int = 1
    outlet: int = 0

    def reach(self, creatures: int) -> int:
        """Damage their sacrifice outlets deal us with ``creatures`` creatures to sacrifice."""
        return self.outlet * max(0, creatures)

    def combat_source_counters(self, card: dict) -> int:
        """+1/+1 counters ``card``'s beginning-of-combat triggers place each combat (ledger #198)."""
        return sum(n for n, _kind, _other in combat_counter_growth(card))

    def combat_targets(self, bodies: list[dict], *, alive_origins: set[int] | None = None) -> dict[int, int]:
        """{body instance_id: +1/+1 counters} placed by ``combat_counters`` at the beginning of combat."""
        if not self.combat_counters or not bodies:
            return {}
        out: dict[int, int] = {}
        for source_id, is_creature, n, kind, other in self.combat_counters:
            if is_creature and alive_origins is not None and source_id not in alive_origins:
                continue
            eligible = []
            for b in bodies:
                if not isinstance(b.get("power"), int):
                    continue
                origin = b.get("_origin") or _int((b.get("_card") or b).get("instance_id")) or b.get("instance_id")
                if kind == "self":
                    if origin == source_id:
                        eligible.append(b)
                elif (not other or origin != source_id) and (kind == "creature" or has_kind(b, kind)):
                    eligible.append(b)
            if not eligible:
                continue
            target = max(
                eligible,
                key=lambda b: (
                    bool(b.get("_can_attack", True)),
                    not bool(b.get("_sick", False)),
                    bool(b.get("_unblockable") or b.get("cant_be_blocked") or has_combat_keyword(b, "flying")),
                    has_combat_keyword(b, "double strike"),
                    has_combat_keyword(b, "trample") or has_combat_keyword(b, "menace"),
                    b.get("power") or 0,
                    b.get("toughness") or 0,
                    -(_int(b.get("instance_id")) or 0),
                ),
            )
            tid = _int(target.get("instance_id"))
            if tid is not None:
                out[tid] = out.get(tid, 0) + n
        return out

    def body_tokens(
        self, card: dict, *, mana: int | None = None, activated: bool = False
    ) -> tuple[int, int, int]:
        """(creature tokens, their power, toughness) the card's token engines make on a turn of
        theirs (``token_engine``): a trigger once; an activated ability as often as their ``mana``
        (default: their next turn's) pays for it (at most three times; {T}: once), its X counting
        their permanents of that type and the tokens it made of that type (Squirrel Girl: 4, 8);
        ``activated``: the activated abilities only (at instant speed, at our end step)."""
        found = token_engine(card)
        if not found:
            return 0, 0, 0
        budget = self.mana if mana is None else mana
        total, power, toughness = 0, 0, 0
        for cost, tap, n, kind, p, t, desc in found:
            if cost is None:
                if activated:
                    continue
                times = 1
            elif cost <= 0:
                times = 1
            else:
                times = min(1 if tap else _MAX_ACTIVATIONS, budget // cost)
                budget -= times * cost
            x = n or (self.kinds or {}).get(kind or "creature", 0)
            words = desc.split()
            grows = bool(kind) and kind in words
            for _ in range(times):
                made = n or x
                total += made
                x += made if grows else 0
            if times:
                power, toughness = (self.token_pt or {}).get(words[-1] if words else "", (p, t))
        return min(total, _MAX_TOKENS), power, toughness

    def gain_events(self, walkers=None, our_entries: int = 0) -> int:
        """Their life-gain events on one turn of theirs before their attack: their gain triggers,
        one per planeswalker of theirs with a gain ability (``walkers``: the ids still alive, all
        when None) and ``our_entries`` creatures of ours that entered since their last turn."""
        if self.no_gain:
            return 0
        alive = self.gain_walkers if walkers is None else self.gain_walkers & frozenset(walkers)
        return self.gains + len(alive) + self.per_our_creature * our_entries

    def kind_count(self, kind: str) -> int:
        """Their permanents of ``kind`` for a drain that counts them: their creatures by type
        ('creature': all of them), else their artifacts, tokens and artifact kinds (``counts``)."""
        if kind in (self.kinds or {}):
            return int(self.kinds[kind])
        return int((self.counts or {}).get(kind, 0))

    def body_drain(self, card: dict, *, gains: int | None = None, after_upkeep: bool = False) -> int:
        """Damage one permanent's drain triggers deal us on a turn of theirs (``drain_engine``; one
        counted by their permanents as many times as they control them, ``counted_drain_engine``)."""
        found = counted_drain_engine(card)
        if not found:
            return 0
        events = self.gain_events() if gains is None else gains
        return sum(
            amount
            * (self.kind_count(kind) if kind else 1)
            * drain_triggers(
                event,
                self.counts or {},
                draws=self.draws,
                gains=events,
                drops=self.drops,
                entering=self.entering,
                after_upkeep=after_upkeep,
            )
            for amount, event, kind in found
        )

    def per_gain(self, card: dict, *, team: tuple | None = None) -> int:
        """+1/+1 counters ``card`` (theirs) gets per life-gain event of theirs: its own trigger
        and the team triggers whose sources are alive (``team``; all of them when None)."""
        if self.no_gain:
            return 0
        counters = lifegain_growth(card)[0]
        for _source, kind, n in self.team if team is None else team:
            if kind == "creature" or has_kind(card, kind):
                counters += n
        return counters

    def growth(self, card: dict, *, gains: int, team: tuple | None = None) -> int:
        """+1/+1 counters ``card`` (theirs) gets on one turn of theirs with ``gains`` life-gain
        events before their attack and their draws (``draws``)."""
        counters = 0
        if gains and not self.no_gain:
            own, _loyal, _team, first = lifegain_growth(card)
            counters = own * (min(gains, 1) if first else gains)
            counters += (self.per_gain(card, team=team) - own) * gains
        each, second = draw_growth(card)
        return counters + each * self.draws + (second if self.draws >= 2 else 0)


_KICKED_DAMAGE = re.compile(
    r"\bif (?:this spell|it) was kicked, (?:~|it|this spell) deals (?P<n>\d+) damage instead\b"
)
_KICKER_COST = re.compile(r"\bkicker ((?:\{[^}]+\})+)")


def held_removal_floor(card: dict, role: str, *, mana: int) -> float:
    """What holding damage removal is worth while it has no target: killing a creature they could
    cast, an X/D for its damage D (``_spell_value``: 3 + power, D/D taken as typical), D being its
    kicked damage when base cost plus kicker is within ``mana``. 0 for anything else.

    Ledger #147 (match 1b4af0f9 T6, 2026-10-19): held removal was valued at
    ``max(1.0, value) x 0.3`` against 0.6 a point of face damage, so burn into an
    empty board always won; held, Tectonic Hazard kills an X/1 and Burst Lightning
    (#154, f5bc17dc T6: three burn spells went face at their 14; Baron Helmut Zemo
    came down on T8) kills an X/4 once its kicker is two land drops away.
    """
    sweep = _sweep(_cast_text(card))
    if role != "removal" and not sweep:
        return 0.0
    damage = max(
        [sweep, *(c.limit or 0 for c in _removal_clauses(card) if c.kind == "damage" and c.targeted)]
    )
    text = _text(card)
    kicked, kicker = _KICKED_DAMAGE.search(text), _KICKER_COST.search(text)
    if (
        kicked
        and kicker
        and _mana_value(str(card.get("mana_cost") or "")) + _mana_value(kicker.group(1)) <= mana
    ):
        damage = max(damage, int(kicked.group("n")))
    return 3.0 + min(damage, 4) if damage > 0 else 0.0


def their_engines(
    battlefield, local: int | None, opponent: int | None, *, draws: int = 1, drops: int = 1
) -> TheirEngines:
    """The opponent's engines on this battlefield (``TheirEngines``): life-gain sources and the
    growth they feed, draws, "enter tapped", "can't gain life" and their drains."""
    cards = [c for c in battlefield if isinstance(c, dict) and not c.get("is_phased_out")]
    theirs = [c for c in cards if _controller(c) == opponent]
    no_gain = any(_NO_GAIN.search(_engine_text(*_key(c))) for c in cards) or any(
        _NO_GAIN_THEM.search(_engine_text(*_key(c))) for c in cards if _controller(c) == local
    ) or any(_NO_GAIN_SELF.search(_engine_text(*_key(c))) for c in theirs)  # fmt: skip
    gains = per_ours = life_ours = 0
    walkers: set[int] = set()
    team: list[tuple[int, str, int]] = []
    combat_counters: list[tuple[int, bool, int, str, bool]] = []
    spells = [c for c in theirs if not _is_land(c)]  # (a land's own triggers are not engines here)
    for card in spells:
        turn, ours, life = gain_triggers(card)
        gains, per_ours, life_ours = gains + turn, per_ours + ours, life_ours + life
        if walker_gain(card):
            walkers.add(_int(card.get("instance_id")) or 0)
        for kind, n in lifegain_growth(card)[2]:
            team.append((_int(card.get("instance_id")) or 0, kind, n))
        cid = _int(card.get("instance_id")) or 0
        is_creat = _is_creature(card)
        for n, kind, other in combat_counter_growth(card):
            combat_counters.append((cid, is_creat, n, kind, other))
    # Their mana (#172): each land (twice under a land-mana doubler, Caged Sun), each mana ability.
    doubled = any(_LAND_MANA_DOUBLER.search(_engine_text(*_key(c))) for c in spells)
    lands = [c for c in theirs if _is_land(c)]
    rocks = [c for c in spells if _MANA_ABILITY.search(_engine_text(*_key(c)))]
    mana = len(lands) * (2 if doubled else 1) + len(rocks)
    mana_now = sum(1 for c in lands if not c.get("is_tapped")) * (2 if doubled else 1) + sum(
        1 for c in rocks if not c.get("is_tapped")
    )
    kinds: dict[str, int] = {}
    token_pt: dict[str, tuple[int, int]] = {}
    for card in spells:
        if not _is_creature(card):
            continue
        kinds["creature"] = kinds.get("creature", 0) + 1
        words = set(re.findall(r"[a-z'-]+", _types(card).split("—")[-1].replace("cardtype_creature", "")))
        words |= {str(s).lower().replace("subtype_", "") for s in card.get("subtypes") or []}
        for word in words:
            kinds[word] = kinds.get(word, 0) + 1
        power, toughness = _int(card.get("power")), _int(card.get("toughness"))
        if (
            "token" in str(card.get("object_kind") or "").lower()
            and power is not None
            and toughness is not None
        ):
            for word in words:
                token_pt.setdefault(word, (power, toughness))
    outlet = max((sac_outlet(c) for c in spells), default=0)
    engines = TheirEngines(
        gains=gains, gain_walkers=frozenset(walkers), per_our_creature=per_ours, life_per_our_creature=life_ours,
        draws=max(1, draws), no_gain=no_gain, outlet=outlet,
        enter_tapped=any(_ENTER_TAPPED.search(_engine_text(*_key(c))) for c in spells),
        team=tuple(team), combat_counters=tuple(combat_counters),
        counts=fodder_counts(theirs, opponent), drops=max(0, drops),
        mana=mana, mana_now=mana_now, kinds=kinds, token_pt=token_pt,
    )  # fmt: skip
    # Their creatures entering a turn (their enters pings, Impact Tremors): their token makers'.
    entering = sum(engines.body_tokens(c)[0] for c in spells)
    engines = dataclasses.replace(engines, entering=max(1, entering))
    static = static_after = 0
    names: list[str] = []
    for card in spells:
        amount = engines.body_drain(card)
        if amount:
            names.append(f"{_name(card)} ({amount} a turn)")
            if not _is_creature(card):
                static += amount
                static_after += engines.body_drain(card, after_upkeep=True)
        combat_n = engines.combat_source_counters(card)
        if combat_n and not _is_creature(card):
            names.append(f"{_name(card)} (+{combat_n}/+{combat_n} a combat)")
    return dataclasses.replace(
        engines, static_drain=static, static_drain_after_upkeep=static_after, names=tuple(names)
    )


def attack_pings(card: dict) -> tuple[tuple[int, str], ...]:
    """The card's own fixed attack-trigger pings to players (``attack_ping_line``), () without one.

    Ledger #19 (2026-10-19, matches 7efad315 T2 and f9e0ec3b T8): Hired
    Claw's ping was "its damage to players", the search couldn't value it, so
    the line guard and the plan check skipped every Claw choice.
    """
    text = _QUOTED.sub("", _text(card))
    if "attack" not in text:
        return ()
    return tuple(found for line in _own_lines(text) if (found := attack_ping_line(line)) is not None)


def _own_lines(text: str) -> list[str]:
    """The text's lines, whitespace-normalised, each once: Arena repeats a rules line in up to
    three formats ("<i>Landfall</i><nobr> —</nobr> ..."), which must not count twice."""
    seen: dict[str, None] = {}
    for line in text.splitlines():
        seen.setdefault(" ".join(line.split()), None)
    return [line for line in seen if line]


# The soft limit is a work budget, not a clock, so the same board always gets the same
# lines. Work is counted in roughly 10-microsecond units (M-series Mac, 2026-10-07): a
# node expansion 6, a combat solve 2 + its attackers and able blockers (the solver is
# ~85% of a big board's time). Past the budget the remaining T steps finish by greedy
# rollout. Sized so the slowest recorded real boards (bug_20260928_212608: ~8600 units,
# 67 ms unbounded) stay near the 50 ms assess() target; ordinary boards never reach it.
WORK_BUDGET = 3000
_NODE_WORK = 6
# Legacy ``soft_ms`` callers: today's 120 ms soft deadline stands for WORK_BUDGET.
WORK_PER_MS = WORK_BUDGET / 120.0


class OpponentModel(Protocol):
    """How the opponent acts between our turns (v1: ``ParanoidOpponent``)."""

    def attack_policies(
        self, moves: Any, able: list[dict], blockers: list[dict], life: int
    ) -> list[tuple[str, tuple[dict, ...]]]: ...

    def chance(self, moves: Any, life: int) -> list[tuple[float, str]]: ...


class ParanoidOpponent:
    """The default attack policies; no draws, deploys or tricks (``chance`` is NOTHING)."""

    def attack_policies(self, moves, able, blockers, life):
        return moves.default_policies(able, blockers, life)

    def chance(self, moves, life):
        return [(1.0, NOTHING)]


@dataclass(frozen=True)
class Step:
    """One of our turns in a line and the opponent's attack that follows it."""

    k: int
    turn: int
    label: str
    land: str = ""
    casts: tuple[str, ...] = ()  # card names, in cast order
    modes: tuple[tuple[str, str], ...] = ()  # (card, chosen mode) for modal casts
    targets: tuple[tuple[str, str], ...] = ()  # (card, target) for removal and bounce
    cycles: tuple[str, ...] = ()  # landcycled cards
    attack: tuple[str, ...] = ()  # our attackers
    held: tuple[str, ...] = ()  # instants we used on their following turn
    mana: int = 0
    colors: str = ""
    source_colors: tuple[str, ...] = ()  # per source, before rocks this line cast
    castable: tuple[str, ...] = ()
    opp_life_after: int | None = None  # their life after our attack
    life_after: int | None = None  # our life after their following attack
    their_creatures_after: int | None = None
    policy: str = ""  # their attack policy
    greedy: bool = False
    # Casts that change nothing about this turn's attack: cast them after combat (main 2).
    post_combat: tuple[str, ...] = ()
    keys: frozenset = frozenset()
    plays: tuple = field(
        default=(), repr=False, compare=False
    )  # (land, ((variant, target), ...), attack ids)
    # Attackers sent at one of their planeswalkers: ((attacker, planeswalker), ...); the rest go
    # at the player. ``walker_attack`` is the attack option's name, for replays.
    at_walkers: tuple[tuple[str, str], ...] = ()
    walker_attack: str = ""

    @property
    def sig(self) -> tuple:
        """What was played: two steps with the same sig are the same choice (cast order ignored)."""
        return (
            self.turn, self.land, tuple(sorted(self.casts)), tuple(sorted(self.modes)),
            tuple(sorted(self.targets)), tuple(sorted(self.cycles)), self.attack, self.at_walkers,
        )  # fmt: skip

    def text(self) -> str:
        parts = [self.land] if self.land else []
        modes, targets = dict(self.modes), dict(self.targets)
        for name in self.casts:
            extra = ", ".join(
                x for x in (modes.get(name, ""), f"on {targets[name]}" if name in targets else "") if x
            )
            parts.append(f"{name} ({extra})" if extra else name)
        parts += [f"landcycle {name}" for name in self.cycles]
        later = [p for p in parts[1 if self.land else 0 :] if p in self.post_combat]
        if self.attack and later:
            parts = [p for p in parts if p not in later]
        text = " + ".join(parts)
        if self.attack:
            at = dict(self.at_walkers)
            who = [f"{name} (at {at[name]})" if name in at else name for name in self.attack]
            text += f"{', ' if text else ''}attack with {', '.join(who)}"
            if later:
                text += f", then cast {' + '.join(later)} after combat"
        text = text or "no play"
        if self.held:
            text += f", hold {', '.join(self.held)}"
        return text


@dataclass(frozen=True, eq=False)
class Variant:
    spell: int  # index into Moves.spells
    kind: str  # "cast" or "cycle"
    mode: int | None  # 'Choose one —' bullet
    mode_text: str
    body: dict | None
    gain: int
    reach: dict | None  # what _kills reads: the card, or one bullet
    bounce: bool
    rock: bool
    mana_value: int
    pips: tuple
    instant: bool
    loss: int = 0  # life we lose casting it ("You lose 2 life.", life paid as an additional cost)
    face: int = 0  # damage to the opponent (or life they lose) when it resolves
    aim: str = ""  # "opponent": the face-damage variant of an 'any target' spell
    fetch: str = ""  # landcycling: the basic's colour ("" = we choose the most needed one)
    tokens: tuple = ()  # creature token bodies it makes (each with its own id)
    # Creature tokens it makes per point of life gained this turn (Germinate Recruits): the
    # count is read when it resolves, after the casts before it in the turn.
    life_tokens: tuple = ()
    counter: int = 0  # +1/+1 counters it puts on target creature (we pick our biggest)
    # Damage it deals its creature target as one piece of a combined kill (``HandSpell.chips``):
    # the target dies once the pieces aimed at it this turn reach its toughness.
    chip: int = 0
    # Damage it deals each of their creatures (Tectonic Hazard): it adds to the pieces' damage.
    sweep: int = 0
    # Damage it deals one of their planeswalkers ("any target", "target creature or planeswalker").
    walker: int = 0
    # Creature tokens counted on the board as it resolves (``named_token_specs``: Hare Apparent).
    named_tokens: tuple = ()


@dataclass(eq=False)
class HandSpell:
    index: int
    spell: _Spell  # a copy: _schedule overwrites .value
    iid: int
    key_iid: int  # lowest instance id of this name in hand, a commander's own (ActionKeys use it)
    name: str
    hand_value: float
    variants: tuple = ()
    plain: Variant | None = None  # today's greedy semantics: no mode, whole-card removal
    reduction: Any = None  # its own "this spell costs {X} less" (``cost_reduction``)
    chips: tuple = ()  # its targeted-damage variants as pieces of a combined kill (``Variant.chip``)
    walker_vars: tuple = ()  # its damage aimed at one of their planeswalkers (``Variant.walker``)
    # Its hand value is the held-removal floor (``held_removal_floor``): greedy play weighs holding it.
    floored: bool = False


@dataclass(frozen=True, eq=False)
class Land:
    iid: int
    name: str
    colors: frozenset
    tapped: bool
    source: Any

    @property
    def key(self) -> tuple:
        return (self.colors, self.tapped)


@dataclass(frozen=True, slots=True)
class Node:
    """The board at the start of our turn ``k`` (or a terminal one)."""

    k: int  # 0 = T
    abs_turn: int
    our_life: int
    opp_life: int
    ours: tuple
    theirs: tuple
    our_tapped: frozenset
    their_tapped: frozenset
    entered: tuple  # ((iid, absolute turn), ...) for bodies this line added
    hand: tuple  # spell indices still in hand
    lands: tuple  # Land still in hand, fetched basics included
    played: tuple  # sources of lands played on earlier turns
    rocks: tuple  # rocks cast on earlier turns
    returning: tuple  # their bounced bodies, back after their next attack
    history: tuple = ()
    attacks: int = 0  # their attacks resolved, the pending one included
    now_life: int | None = None
    block: str = ""
    dead_at: int | None = None
    dead_turn: int | None = None
    win_at: int | None = None
    win_turn: int | None = None
    overkill: int = 0
    rolled: bool = False  # finished by greedy rollout (deadline or beam overflow)
    # Their planeswalkers: ((instance id, name, loyalty), ...) (ledger #42, 2026-10-19 a51dea64).
    walkers: tuple = ()

    @property
    def terminal(self) -> bool:
        return self.dead_at is not None or self.win_at is not None


class Mid:
    """Our turn in progress: a mutable scratch copy of a node."""

    __slots__ = (
        "node", "land", "casts", "sources", "base_sources", "used_mv", "used_pips", "our_life", "opp_life",
        "ours", "theirs", "our_tapped", "entered", "hand", "lands", "played", "rocks", "returning",
        "attack_first", "applied", "modes", "targets", "cycles", "cast_names", "keys", "gained", "walkers",
        "pumps",
    )  # fmt: skip

    def copy(self) -> Mid:
        other = Mid.__new__(Mid)
        for name in Mid.__slots__:
            value = getattr(self, name)
            setattr(other, name, value.copy() if isinstance(value, (list, set, dict)) else value)
        return other


class Deadline(Exception):
    pass


def hand_info(card: dict) -> Any:
    """``hand_card`` without its rarity lookup, which can query the card database."""
    return hand_card({**card, "rarity": card.get("rarity") or "-"})


def ids_of(bodies) -> tuple:
    return tuple(sorted(b["instance_id"] for b in bodies))


def bullets(text: str) -> list[str]:
    """A 'Choose one —' card's modes, without Arena's triplicated text."""
    text = re.sub(r"<[^>]*>", "", text or "")
    match = _CHOOSE_ONE.search(text)
    if not match:
        return []
    out, seen = [], set()
    for part in text[match.end() :].split("•")[1:]:
        bullet = part.split("\n")[0].strip()
        norm = " ".join(bullet.lower().split())
        if bullet and norm not in seen:
            seen.add(norm)
            out.append(bullet)
    return out


def classify(name: str, bullet: str) -> tuple[str, int, dict | None, bool]:
    """(kind, life gained, removal 'card' for _kills, bounce) of one mode; kind 'other' is worth 0.

    Kinds: removal, sweep (damage to each creature they control and nothing
    targeted: ``mode_sweep``, no removal card), bounce, lifegain, tokens
    (creature tokens ``token_specs`` reads) and other.

    Ledger #87 (2026-10-19 c8ea2b6c T14): Iroh's Demonstration's "1 damage to
    each creature your opponents control" was 'removal' of one 1/1 and tied
    with "4 damage to target creature" on Healer's Hawk; it kills the Hawk and
    both Soldier tokens.
    """
    pseudo = {"name": name, "oracle_text": bullet, "type_line": ""}
    match = _LIFEGAIN.search(_text(pseudo))
    gain = int(match.group(1)) if match else 0
    if mode_sweep(name, bullet) and not any(c.targeted for c in _removal_clauses(pseudo)):
        return "sweep", gain, None, False
    reach = removal_reach(pseudo)
    if reach is not None:
        return ("bounce" if reach[0] == "bounce" else "removal"), gain, pseudo, reach[0] == "bounce"
    if gain:
        return "lifegain", gain, None, False
    return ("tokens" if token_specs(_cast_text(pseudo)) else "other"), 0, None, False


def _life_scaled(text: str, gained: int | None) -> str:
    """``text`` with "create X ... tokens, where X is the amount of life you gained this turn"
    read as "create <gained> ..." (unchanged when ``gained`` is None)."""
    if gained is None:
        return text
    return _LIFE_SCALED_TOKENS.sub(lambda m: f"create {max(0, min(int(gained), 10))} {m.group(1)}", text)


def life_scaled_tokens(text: str) -> bool:
    """``text`` (lower-case cast text) makes X creature tokens, X the life gained this turn."""
    return bool(_LIFE_SCALED_TOKENS.search(text or ""))


def token_specs(
    text: str, *, life_gained: int | None = None, life_gained_game: int | None = None
) -> list[tuple[int, int, int, tuple[str, ...], str]] | None:
    """The creature tokens ``text`` (lower-case cast text) makes: [(count, power, toughness, keywords, name)].

    [] when it makes none; None when it makes creature tokens that can't be
    read: a variable count ("for each", "equal to", X), copies, tokens that
    enter tapped or attacking, a condition ("if", "unless", "instead") before
    the "create", or tokens exiled or sacrificed at the next end step.
    "They gain haste until end of turn" after it gives them haste.

    ``life_gained``: the life we gained this turn, which reads "create X ...
    tokens, where X is the amount of life you gained this turn" (Germinate
    Recruits; 0 makes none of them). ``life_gained_game``: this game's, which
    reads "For every 3 life you gained this game, create an additional one of
    those tokens" (Flourishing Forces). Without them those counts stay unread
    (X) or at the printed minimum (the additional tokens).
    """
    text = _life_scaled(text or "", life_gained)
    specs: list[tuple[int, int, int, tuple[str, ...], str]] = []
    for line in text.splitlines():
        for create in re.finditer(r"\bcreate\b", line):
            start = line.rfind(".", 0, create.start()) + 1
            end = line.find(".", create.end())
            clause = line[create.start() : end if end >= 0 else len(line)]
            if not re.search(r"\btokens?\b", clause) or not _CREATES_CREATURES.search(clause):
                continue  # Food, Treasure, Clue...: no creature
            if _NAMED_COUNT.match(line, create.start()):
                continue  # counted on the board when it resolves (``named_token_specs``)
            prefix = re.sub(r"^\s*[•]?\s*(?:when(?:ever)?\b[^,]*,\s*)?", "", line[start : create.start()])
            match = _TOKEN.match(line, create.start())
            after = line[end:] if end >= 0 else ""
            if (
                match is None
                or _TOKEN_CONDITION.search(prefix)
                or _TOKEN_UNREAD.search(match.group("rest"))
                or _TOKEN_TEMPORARY.search(after)
            ):
                return None
            count = _COUNT_WORDS.get(match.group("n")) or _int(match.group("n")) or 0
            if count == 0 and match.group("n") == "0":
                continue  # "where X is the life you gained this turn" with none gained
            if not 0 < count <= 10:
                return None
            rest = match.group("rest")
            named = re.search(r"\bnamed ([a-z' -]+?)(?:\s+with\b|,|$)", rest)
            granted = re.search(r"\bwith (.+)$", rest)
            keywords = {k for k in _TOKEN_KEYWORDS if granted and re.search(rf"\b{k}\b", granted.group(1))}
            following = after.split(".")[1] if "." in after else ""
            if _TOKEN_HASTE_NOW.search(following):
                keywords.add("haste")
            if named:
                name = named.group(1).strip().title()
            else:
                words = [
                    w for w in re.split(r"[\s,]+", match.group("desc")) if w and w not in _TOKEN_DESC_SKIP
                ]
                name = (" ".join(words).title() or "Creature") + " token"
            specs.append((count, int(match.group("p")), int(match.group("t")), tuple(sorted(keywords)), name))
    extra = _LIFE_GAME_EXTRA.search(text)
    if extra and specs and life_gained_game is not None:
        step = _COUNT_WORDS.get(extra.group(1)) or _int(extra.group(1)) or 0
        if step > 0:
            count, power, toughness, keywords, name = specs[-1]
            specs[-1] = (min(10, count + int(life_gained_game) // step), power, toughness, keywords, name)
    return specs


def named_token_specs(text: str, own_name: str = "") -> tuple[tuple[int, int, tuple[str, ...], str, str, bool], ...]:
    """Creature tokens whose count is the number of creatures we control with a name, read when
    the effect resolves: ((power, toughness, keywords, token name, counted name, other), ...).

    Hare Apparent (ledger #157, match be1a945f): "create a number of 1/1 white Rabbit creature
    tokens equal to the number of other creatures you control named Hare Apparent". The count was
    unread (``token_specs`` None), so every Hare cast was "its tokens, not modelled" and the game
    plan's line check skipped every plan casting one. ``own_name`` replaces "~".
    """
    out = []
    for match in _NAMED_COUNT.finditer(text or ""):
        rest = match.group("rest")
        granted = re.search(r"\bwith (.+)$", rest)
        keywords = {k for k in _TOKEN_KEYWORDS if granted and re.search(rf"\b{k}\b", granted.group(1))}
        words = [w for w in re.split(r"[\s,]+", match.group("desc")) if w and w not in _TOKEN_DESC_SKIP]
        token = (" ".join(words).title() or "Creature") + " token"
        named = match.group("named").strip()
        counted = own_name if named == "~" else named.title()
        if not counted:
            continue
        out.append((int(match.group("p")), int(match.group("t")), tuple(sorted(keywords)), token, counted,
                    bool(match.group("other"))))  # fmt: skip
    return tuple(out)


def named_count(spec, bodies, itself: dict | None = None) -> int:
    """How many tokens one ``named_token_specs`` entry makes with ``bodies`` (our creatures, the
    new creature among them) on the battlefield; at most 20."""
    _p, _t, _kw, _token, counted, other = spec
    wanted = counted.casefold()
    count = sum(
        1
        for b in bodies
        if str(b.get("name") or "").casefold() == wanted and not (other and itself is not None and b is itself)
    )
    return max(0, min(20, count))


def _effect_condition(effect: str) -> bool:
    """``_EFFECT_CONDITION`` on an effect, a board-counted token clause (``_NAMED_COUNT``) aside."""
    return bool(_EFFECT_CONDITION.search(_NAMED_COUNT.sub("", effect or "")))


def make_tokens(specs, turn: int, rules, first_id: int) -> tuple[dict, ...]:
    """Solver bodies for ``token_specs`` output, ids ``first_id``, ``first_id + 1``, ..."""
    bodies = []
    for count, power, toughness, keywords, name in specs or ():
        for _ in range(count):
            card = {
                "instance_id": first_id + len(bodies), "name": name, "power": power, "toughness": toughness,
                "keywords": list(keywords), "oracle_text": "\n".join(k.capitalize() for k in keywords),
                "type_line": "Token Creature", "card_types": ["CardType_Creature"], "object_kind": "TOKEN",
            }  # fmt: skip
            body = _body(card, turn, rules)
            if body is not None:
                bodies.append(body)
    return tuple(bodies)


def _etb_modelled(name: str, effect: str) -> bool:
    """A creature's enters effect the search applies: tokens it reads, removal (not a fight),
    life gain or damage to the opponent."""
    pseudo = {"name": name, "oracle_text": effect, "type_line": ""}
    reach = removal_reach(pseudo)
    return bool(
        token_specs(_cast_text(pseudo))
        or named_token_specs(_cast_text(pseudo), name)
        or (reach is not None and reach[0] != "fight")
        or _LIFEGAIN.search(_text(pseudo))
        or any(face_damage(pseudo))
    )


def _is_permanent(card: dict) -> bool:
    types = _types(card)
    return bool(types.strip()) and not re.search(r"\b(?:instant|sorcery)\b", types)


def applied_text(card: dict, *, enters_triggers: bool = True) -> str:
    """The part of ``_cast_text(card)`` the search applies when ``card`` is cast.

    A permanent's own enters trigger counts only without a condition
    (``_EFFECT_CONDITION``: "if you control six or more lands", "if it was
    kicked", a reflexive "you may discard a card. When you do, ...", a
    variable amount), and only with ``enters_triggers`` (no creature's enters
    trigger fires under Hushbringer); a trigger left out takes its modes with
    it. A spell's text, and text without types, is used whole.
    """
    text = _cast_text(card)
    if not _is_permanent(card):
        return text
    kept: list[str] = []
    skip = False
    for line in text.splitlines():
        if line.startswith("•"):
            if not skip:
                kept.append(line)
            continue
        trigger = _CREATURE_TRIGGER.match(line)
        skip = trigger is not None and (not enters_triggers or _effect_condition(trigger["effect"]))
        if not skip:
            kept.append(line)
    return "\n".join(kept)


def _trigger_unmodelled(card: dict, name: str) -> bool:
    """A permanent's triggered ability does something the search can't see.

    Card flow aside, only the card's own enters trigger ("When this creature
    enters, ...", ``own_subject``) with an effect the search applies and no
    condition (``applied_text``) is modelled. An attack, dies or cast
    trigger, a trigger on another permanent entering ("Whenever another
    creature you control enters"), one that also fires for others ("this
    creature or another creature ... enters") or on another event ("enters or
    attacks") is not.
    """
    text = _QUOTED.sub("", _text(card))
    for match in _CREATURE_TRIGGER.finditer(text):
        effect = match.group("effect").strip()
        if card_flow(effect) or attack_ping_line(match.group(0)):
            continue  # card flow, or a fixed ping to players on attack (``Moves.pinged``)
        subject, rest = match.group("subject").strip(), match.group("rest")
        if (
            match.group("event") in ("enters", "enter")
            and own_subject(subject, name)
            and not _OR_ANOTHER.search(subject)
            and not re.search(r"\b(?:attacks|dies|blocks|leaves)\b", rest)
            and not _effect_condition(effect)
            and _etb_modelled(name, effect)
        ):
            continue
        return True
    return False


# A sentence of a spell's text the search applies in full: damage or life loss to each opponent,
# damage to each creature they control, life gain (Boltwave; Tectonic Hazard: "deals 1 damage to
# each opponent and each creature they control").
_APPLIED_SENTENCE = re.compile(
    r"^(?:~ deals \d+ damage to (?:each opponent(?: and each creature (?:they|your opponents) control)?"
    r"|each creature (?:they|your opponents|an opponent) controls?)"
    r"|each opponent loses \d+ life|you gain \d+ life)$"
)
# Damage to each of their creatures (a one-sided sweep): ``Variant.sweep``.
_SWEEP = re.compile(
    r"\bdeals (?P<n>\d+) damage to (?:each opponent and )?each creature (?:they|your opponents|an opponent) controls?\b"
)


# Damage a spell can aim at one of their planeswalkers: "any target", "target creature or
# planeswalker", "target player or planeswalker".
_WALKER_BURN = re.compile(
    r"\bdeals? (?P<n>\d+) damage to (?:any (?:other )?target|(?:up to one )?target (?:[a-z]+(?:,| or) )*?planeswalker)"
)


def walker_burn(text: str) -> int:
    """Damage ``text`` (lower case) can deal one of their planeswalkers (0: none)."""
    return max((int(m.group("n")) for m in _WALKER_BURN.finditer(text or "")), default=0)


def _sweep(text: str) -> int:
    """Damage ``text`` (lower-case cast text) deals each creature the opponent controls (0: none)."""
    return max((int(m.group("n")) for m in _SWEEP.finditer(text or "")), default=0)


def mode_sweep(name: str, bullet: str) -> int:
    """Damage one 'choose one' mode deals each creature the opponent controls (0: none)."""
    return _sweep(_cast_text({"name": name, "oracle_text": bullet, "type_line": ""}))


def swept_now(body: dict, amount: int, card: dict | None) -> bool:
    """A sweep of ``amount`` damage from ``card`` resolving now kills this creature of theirs:
    not indestructible, no protection from the spell, toughness within the damage plus what is
    already marked on it."""
    if amount <= 0 or has_combat_keyword(body, "indestructible"):
        return False
    if _protected_from(body, card or {}, targeted=False):
        return False
    marked = max(0, _int((body.get("_card") or {}).get("damage")) or 0)
    return body["toughness"] <= amount + marked


def fully_applied(card: dict) -> bool:
    """Every sentence of the card's cast text is one the search applies (``_APPLIED_SENTENCE``) or
    card flow. Ledger #20 / #43 (2026-10-19): Boltwave ("3 damage to each opponent") and Tectonic
    Hazard were "its effect", which kept every option on their menus from a "LINE best" tag and
    skipped the game plan's line check (27 and 28 times that morning)."""
    sentences = [
        s.strip(" ,")
        for line in _cast_text(card).splitlines()
        for s in re.split(r"[.;]\s*", line)
        if s.strip(" ,")
    ]
    return bool(sentences) and all(_APPLIED_SENTENCE.match(s) or card_flow(s) for s in sentences)


def _unapplied_harm(card: dict) -> bool:
    """``harms_players`` without the fixed attack-trigger pings the search applies (Hired Claw)."""
    if not harms_players(card):
        return False
    text = _QUOTED.sub("", _text(card))
    kept = "\n".join(line for line in text.splitlines() if not attack_ping_line(line))
    return harms_players({"name": "", "oracle_text": kept})


def _x_unmodelled(card: dict, role: str) -> bool:
    """An X spell (the search never casts one) whose effect touches the board or life totals.

    X card flow ("Target player draws X cards", "discards X cards at
    random") and X counterspells change nothing the search values.
    """
    if "x" not in str(card.get("mana_cost") or "").lower():
        return False
    return role not in ("draw", "selection", "counter") and not card_flow(_cast_text(card))


def unmodelled_effect(card: dict | None, result: Any = None) -> str:
    """What casting ``card`` does that the line search does not model; '' when it models it all.

    The search (``Moves._variants``) values a creature's body, creature
    tokens it can read, removal, bounce, life gain, damage to the opponent, a
    mana rock, 'choose one' modes of those kinds and a permanent's own
    unconditional enters trigger of those kinds (a fight aside), a fixed
    attack-trigger ping to players (Hired Claw, ``attack_pings``) and a
    spell that only deals damage to each opponent and each of their creatures
    (Boltwave, Tectonic Hazard: ``fully_applied``). Card flow (draw, scry,
    discard) and counterspells change nothing it values.
    Anything else counts as nothing, so a line starting with such a cast is
    no evidence against it: an X spell that touches the board or life ("its
    X cost"), creature tokens it can't read ("its tokens"), any other
    triggered ability (``_trigger_unmodelled``: "its triggered ability"),
    modes that are all unmodelled ("its modes"), a pump ("a combat trick"),
    an aura, a planeswalker or another noncreature card ("its effect"), and,
    with ``result`` (the board's ``LineSearchResult``), damage to players it
    has no face variant for ("its damage to players"). '' on any error.
    """
    if not isinstance(card, dict) or not card.get("name"):
        return ""
    if card.get("_card_unknown"):  # a commander without card data (``board_model.our_commanders``)
        return "its rules text is unknown"
    try:
        role = card_role(card)
        if role == "land":
            return ""
        name = str(card["name"])
        if _x_unmodelled(card, role):
            return "its X cost"
        if token_specs(_cast_text(card), life_gained=0) is None:
            return "its tokens"
        modes = bullets(str(card.get("oracle_text") or ""))
        if modes and all(classify(name, mode)[0] == "other" for mode in modes):
            if _is_creature(card) or any(not card_flow(m.lower()) for m in modes):
                return "its modes"
        if _is_permanent(card) and not modes and _trigger_unmodelled(card, name):
            return "its triggered ability"
        if not _is_creature(card):
            if re.search(r"\benchant (?:creature|permanent)\b", _text(card)):
                return "its effect"  # an aura: Pacifism is removal, not a combat trick
            if role == "pump":
                return "a combat trick"
            if role == "planeswalker" or (
                role == "other" and not card_flow(_cast_text(card)) and not fully_applied(card)
            ):
                return "its effect"  # (a discard spell is card flow)
        if result is not None and _unapplied_harm(card):
            iid = _int(card.get("instance_id"))
            spells = getattr(getattr(result, "_search", None), "spells", None) or []
            if not any(hs.iid == iid and any(v.face for v in hs.variants) for hs in spells):
                return "its damage to players"
        return ""
    except Exception:  # unknown: treated as modelled, as before the check existed
        return ""


def mode_label(kind: str, gain: int, bullet: str, name: str = "") -> str:
    """A short name for one mode: 'gain 4 life', 'deals 5 damage to target creature…'."""
    if kind == "lifegain":
        return f"gain {gain} life"
    if name:
        bullet = re.sub(re.escape(name), "", bullet, flags=re.IGNORECASE)
    words = bullet.rstrip(".").split()
    return " ".join(words[:6]).lower() + ("…" if len(words) > 6 else "")


def _modelled(var: Variant) -> bool:
    """The cast does something the search values: a body, tokens, removal, life, face damage, a rock."""
    return bool(
        var.body is not None
        or var.tokens
        or var.life_tokens
        or var.named_tokens
        or var.counter
        or var.reach is not None
        or var.gain
        or var.face
        or var.rock
        or var.sweep
    )


# An enters trigger's +1/+1 counters on a creature we choose: "Put a +1/+1 counter on it if
# it's a creature" after "target permanent you control" (Teyo), "put two +1/+1 counters on
# target creature you control". The creature itself is a legal choice unless it says "another".
_ENTERS_COUNTERS = re.compile(
    r"\bput (?P<n>a|an|one|two|three|\d+) \+1/\+1 counters? on (?:it|that creature|that permanent"
    r"|target creature(?: you control)?|target permanent you control)\b"
)
_OWN_TARGET = re.compile(r"\btarget (?:creature|permanent)(?: you control)?\b")
_NOT_SELF = re.compile(r"\b(?:another target|other target|you don't control|an opponent controls)\b")


def enters_counters(card: dict) -> int:
    """+1/+1 counters a creature's own enters trigger can put on itself (0: none, or not on itself).

    A flash creature like Teyo, Lightshield Expert is worth more than its printed 1/1 as a
    surprise blocker: its trigger may target itself. Where it is better spent on another
    creature the search undervalues it, never overvalues it.
    """
    from arenamcp.opponent_tricks import _enters_text, clean_oracle

    total = 0
    for line in _enters_text(clean_oracle(str(card.get("oracle_text") or ""))).lower().splitlines():
        if line.startswith("•") or _NOT_SELF.search(line) or not _OWN_TARGET.search(line):
            continue
        for match in _ENTERS_COUNTERS.finditer(line):
            word = match.group("n")
            total += int(word) if word.isdigit() else _COUNT_WORDS.get(word, 0)
    return total


def _bodies(var: Variant) -> list[dict]:
    """The creatures a cast puts onto the battlefield: its own body and its tokens."""
    return ([var.body] if var.body is not None else []) + list(var.tokens)


def _reach_strength(reach: dict) -> float:
    kind, limit = removal_reach(reach) or ("", 0)
    return {"destroy": 99.0, "bounce": 0.5, "fight": 1.0}.get(kind, float(limit or 0))


def _colors(sources) -> str:
    return "".join(sorted({c for s in sources for c in s.produces if c != "C"}))


class Moves:
    """Moves, transitions and values over one ``BoardModel`` (subclassed by the search)."""

    def __init__(
        self,
        model: BoardModel,
        *,
        survival: bool,
        lethal_now: bool,
        soft_ms: float | None = None,
        hard_ms: float,
        opponent: OpponentModel | None,
        root_effect: dict | None = None,
        proxy: bool = False,
        budget: int | None = None,
    ) -> None:
        self.model = model
        self.survival, self.lethal_now = bool(survival), bool(lethal_now)
        self.started = time.perf_counter()
        self.hard = hard_ms / 1000.0
        if budget is None:
            budget = WORK_BUDGET if soft_ms is None else round(max(0.0, soft_ms) * WORK_PER_MS)
        self.budget = int(budget)  # deterministic soft limit (work units)
        self.opponent = opponent or ParanoidOpponent()
        self.root_effect = root_effect  # compare_modes: a mode applied before T
        self.proxy = proxy  # +2/+0 on their biggest attacker each attack
        self.free = False  # the baseline and replays ignore the hard cap
        self.nodes = self.combats = 0
        self.work = 0  # deterministic cost so far (see WORK_BUDGET)
        self.bounded = self.truncated = False
        self.exact_first = True
        self.our_turn = model.our_turn
        self.first_turn = model.turn if model.our_turn else model.turn + 1
        self.haste = bool(model.our_rules.get("haste"))
        self._memo: dict = {}
        self._kill_memo: dict = {}
        self._ward_memo: dict = {}
        self._ban_memo: dict = {}
        self._m_memo: dict = {}
        self._value_memo: dict = {}
        self._cast_memo: dict = {}
        self._boosted: dict = {}
        self._token_id = _TOKEN_IDS  # the next token body's instance id (deterministic)
        self._life_token_memo: dict = {}
        self._counter_memo: dict = {}
        self._neutral_memo: dict = {}
        self._burn_memo: dict = {}
        self._strength_memo: dict = {}
        self._ping_memo: dict = {}
        self._growth_memo: dict = {}
        self._spell_growth_memo: dict = {}
        self._pump_memo: dict = {}
        self._cast_pump_memo: dict = {}  # id(body) -> its cast pumps (``cast_triggers.cast_pumps``)
        self._cast_pumped_memo: dict = {}  # (id(body), power, toughness) -> (body, its pumped copy)
        self._kinds_memo: dict = {}
        self._next_memo: dict = {}
        # "+N/+0 for each other creature you control" (Leonardo, Big Brother; ledger #157): instance
        # id -> (base power, base toughness, +power, +toughness, other only), recomputed per board.
        self._scale_base: dict[int, tuple[int, int, int, int, bool]] = {}
        self._scaled_memo: dict = {}
        self._named_memo: dict = {}  # (id(variant), counts) -> the board-counted token bodies
        # Lands they play each of their turns, for their landfall creatures (ledger #27, match
        # 92c09c7a: Sazh's Chocobo grew +1/+1 a turn while the lines attacked with a static 7/8).
        # Paranoid: one a turn, plus their "play an additional land" permanents.
        self.their_drops = 1 + sum(
            1
            for c in model.battlefield
            if isinstance(c, dict) and _controller(c) == model.opponent and _EXTRA_LAND.search(_text(c))
        )
        # Ledger #96 (match 18b1601a T10): lands their permanents fetch once ((lands, Ordeal host id
        # or 0): Escape Tunnel; Ordeal of Nylea when its creature's next attack sacrifices it), on
        # their first turn in a line whose attack isn't declared yet; and their noncreature
        # permanents that make each land trigger landfall again (their creatures count while alive).
        by_id = {_int(c.get("instance_id")): c for c in model.battlefield if isinstance(c, dict)}
        self.fetches: list[tuple[int, int]] = []
        for c in model.battlefield:
            if not isinstance(c, dict) or _controller(c) != model.opponent:
                continue
            host = by_id.get(_int(c.get("attached_to_id")))
            lands = land_fetch(c, host)
            if lands:
                self.fetches.append(
                    (lands, (_int(host.get("instance_id")) or 0) if ordeal_host(c, host) else 0)
                )
        self.board_doublers = sum(
            1
            for c in model.battlefield
            if isinstance(c, dict)
            and _controller(c) == model.opponent
            and not _is_creature(c)
            and landfall_doubler(c)
        )
        self.fetch_index = 2 if (model.their_attack_pending and model.any_theirs_attacking) else 1
        # Their life-threshold anthems (ledger #177, ``life_anthem``): (source id, its creature-ness,
        # (need, +power, +toughness, scope)); ``anthem_bodies`` switches them with their life.
        self.their_anthems = tuple(
            (_int(c.get("instance_id")) or 0, _is_creature(c), found)
            for c in model.battlefield
            if isinstance(c, dict)
            and _controller(c) == model.opponent
            and (found := life_anthem(c)) is not None
        )
        self._anthem_memo: dict = {}
        self._doubler_memo: dict = {}
        self._double_memo: dict = {}
        # Their planeswalkers (ledger #42, a51dea64 10:19–10:25: Kaito and Ajani Resolute were
        # invisible; face burn was "~best" while Strike on Youthful Valkyrie let Slickshot kill Ajani).
        self._walker_memo: dict = {}
        self.walkers0 = tuple(
            sorted(
                (_int(c.get("instance_id")) or 0, _name(c), loyal)
                for c in model.battlefield
                if isinstance(c, dict)
                and _controller(c) == model.opponent
                and "planeswalker" in _types(c)
                and (loyal := loyalty(c)) is not None
                and loyal > 0
            )
        )
        # Hushbringer / Torpor Orb on the battlefield: a creature we cast triggers nothing on entering.
        self.enters_triggers = not any(_NO_ENTERS_TRIGGERS.search(_text(c)) for c in model.battlefield)
        # Their engines (``TheirEngines``, ledger #128-#133, #143): their creatures' growth on each
        # turn of theirs (life gain, draws), their drains, Authority of the Consuls on our casts.
        self.engines = getattr(model, "engines", None)
        engines = self.engines
        self.growers = engines is not None and (
            bool(engines.combat_counters)
            or any(
                engines.per_gain(b.get("_card") or b) or any(draw_growth(b.get("_card") or b))
                for b in model.theirs
            )
        )
        self.drains = engines is not None and bool(
            engines.static_drain or any(b.get("_drain") for b in model.theirs)
        )
        # Their token engines (#172): their tokens of a turn join their board after it; their
        # sacrifice outlets' damage a creature (b62b73fc: Goblin Bombardment).
        self.token_engines = any(b.get("_tokens") for b in model.theirs)
        self.outlet = engines.outlet if engines is not None else 0
        # Loyalty counters their planeswalkers get per life-gain event (Ajani Resolute).
        self.walker_gains = {
            _int(c.get("instance_id")) or 0: lifegain_growth(c)[1]
            for c in model.battlefield
            if isinstance(c, dict) and _controller(c) == model.opponent and "planeswalker" in _types(c)
        }
        self.walker_gains = {iid: n for iid, n in self.walker_gains.items() if n}
        self.consuls = engines is not None and (engines.enter_tapped or engines.per_our_creature > 0)
        self._their_creature_ids = {_int((b.get("_card") or b).get("instance_id")) for b in model.theirs}
        self._turn_growth_memo: dict = {}
        self._leaves_memo: dict = {}
        self._prepare()
        # compare_modes: the spell whose mode it applies before T is being cast now, so its cast
        # triggers pump T's attack like any cast of the line's (ledger #94).
        self.root_pumps: dict = {}
        source = (root_effect or {}).get("source")
        if isinstance(source, dict) and self.pumps_possible and self.our_turn and model.our_attack_pending:
            scratch = SimpleNamespace(ours=list(model.ours), pumps=self.root_pumps)
            self._pump_kinds(scratch, spell_kinds(source))

    # -- precomputation ------------------------------------------------------------------

    def _register_scaling(self, body: dict | None, creatures: int | None) -> None:
        """Note a body whose stats grow with our creature count (``sneak.per_creature_bonus``):
        ``creatures`` is the count its snapshot stats already include (None: printed stats)."""
        if body is None:
            return
        from arenamcp.sneak import per_creature_bonus

        bonus = per_creature_bonus(body.get("_card") or body)
        if bonus is None:
            return
        per_p, per_t, other = bonus
        counted = 0 if creatures is None else max(0, creatures - (1 if other else 0))
        self._scale_base[body["instance_id"]] = (
            body["power"] - per_p * counted, body["toughness"] - per_t * counted, per_p, per_t, other,
        )  # fmt: skip

    def rescale(self, bodies: list[dict]) -> list[dict]:
        """``bodies`` (all our creatures) with every per-creature bonus recomputed for their count."""
        if not self._scale_base or not bodies:
            return bodies
        count = len(bodies)
        out = []
        for body in bodies:
            base = self._scale_base.get(body["instance_id"])
            if base is None:
                out.append(body)
                continue
            base_p, base_t, per_p, per_t, other = base
            counted = max(0, count - (1 if other else 0))
            power, toughness = max(0, base_p + per_p * counted), max(0, base_t + per_t * counted)
            if (power, toughness) == (body["power"], body["toughness"]):
                out.append(body)
                continue
            key = (body["instance_id"], power, toughness)
            found = self._scaled_memo.get(key)
            if found is None:
                found = self._scaled_memo[key] = {**body, "power": power, "toughness": toughness}
            out.append(found)
        return out

    def _named_bodies(self, var: Variant, ours: list[dict]) -> tuple[dict, ...]:
        """The tokens a board-counted token maker (``named_token_specs``) makes as it resolves with
        ``ours`` on the battlefield (its own body included), memoised per count."""
        if not var.named_tokens:
            return ()
        counts = tuple(named_count(spec, ours, var.body) for spec in var.named_tokens)
        key = (id(var), counts)
        found = self._named_memo.get(key)
        if found is None:
            specs = [
                (n, p, t, kw, token)
                for n, (p, t, kw, token, _c, _o) in zip(counts, var.named_tokens, strict=True)
                if n
            ]
            found = self._named_memo[key] = (var, self._tokens(specs))
        return found[1]

    def _prepare(self) -> None:
        model = self.model
        for body in model.ours:
            self._register_scaling(body, len(model.ours))
        theirs = list(model.theirs)
        # Copies of one name in hand share a key; a commander (zone "command") keeps its own.
        lowest: dict[tuple[str, str], int] = {}
        for spell in model.spells:
            iid = _int(spell.card.get("instance_id")) or 0
            lowest[(spell.name, spell.zone)] = min(lowest.get((spell.name, spell.zone), iid), iid)
        self.spells: list[HandSpell] = []
        self.floored = False  # a hand spell's value is the held-removal floor
        reach = len(model.sources_all) + 2  # our mana two land drops from now (a kicker's)
        for index, original in enumerate(model.spells):
            spell = dataclasses.replace(original)
            value = _spell_value(spell, survival=self.survival, theirs=theirs, ours=list(model.ours))
            # Held damage removal is worth killing a creature they could cast, unless we are
            # under a clock (ledger #147, #154: ``held_removal_floor``).
            floor = 0.0 if self.survival else held_removal_floor(spell.card, spell.role, mana=reach)
            iid = _int(spell.card.get("instance_id")) or 0
            hs = HandSpell(
                index, spell, iid, lowest[(spell.name, spell.zone)], spell.name, max(1.0, value, floor),
                floored=floor > max(1.0, value),
            )  # fmt: skip
            self.floored = self.floored or hs.floored
            hs.reduction = parse_cost_reduction(str(spell.card.get("oracle_text") or ""))
            hs.variants, hs.plain = self._variants(hs)
            hs.chips = self._chips(hs.variants)
            hs.walker_vars = self._walker_vars(hs) if self.walkers0 else ()
            self.spells.append(hs)
        self.hand0 = tuple(
            sorted(range(len(self.spells)), key=lambda i: (self.spells[i].name, self.spells[i].iid))
        )
        self._hand_pos = {index: position for position, index in enumerate(self.hand0)}
        # The hand spell each creature body comes from: a body that returns to hand at our end step
        # (Fleeting Effigy) goes back to the hand (``finish``).
        self._body_spell = {
            id(var.body): hs.index for hs in self.spells for var in hs.variants if var.body is not None
        }
        self.any_named = any(var.named_tokens for hs in self.spells for var in hs.variants)
        lands = []
        for card in model.hand_lands:
            colors = frozenset(_land_colors(card))
            source = SimpleNamespace(produces=colors or frozenset({"C"}), name=_name(card))
            lands.append(
                Land(_int(card.get("instance_id")) or 0, _name(card), colors, _enters_tapped(card), source)
            )
        self.lands0 = tuple(sorted(lands, key=lambda land: (land.tapped, land.name, land.iid)))
        # Our "whenever you cast a noncreature spell, ~ gets +2/+0" creatures and prowess (ledger
        # #94): a team pump on a noncreature permanent of ours, or any creature of ours (on the
        # battlefield, on the stack or in hand) with one. Without any, casts pump nothing.
        self.board_pumps = tuple(
            pump
            for card in model.our_permanents
            if isinstance(card, dict) and not _is_creature(card)
            for pump in cast_pumps(card)
            if pump[3] == "team"
        )
        self.pumps_possible = (
            bool(self.board_pumps)
            or any(self.cast_pump(body) for body in (*model.ours, *model.our_stack_bodies))
            or any(cast_pumps(hs.spell.card) for hs in self.spells if _is_creature(hs.spell.card))
        )

    def _walker_vars(self, hs: HandSpell) -> tuple:
        """Its damage aimed at one of their planeswalkers, one per cast variant (per mode)."""
        modes = bullets(str(hs.spell.card.get("oracle_text") or ""))
        whole = _cast_text(hs.spell.card)
        out, seen = [], set()
        for var in hs.variants:
            if var.kind != "cast" or var.aim or var.chip:
                continue
            text = modes[var.mode].lower() if var.mode is not None and var.mode < len(modes) else whole
            amount = walker_burn(text)
            if amount and (var.mode, amount) not in seen:
                seen.add((var.mode, amount))
                out.append(dataclasses.replace(var, reach=None, bounce=False, aim="walker", walker=amount))
        return tuple(out)

    def walker_body(self, iid: int, name: str, loyalty: int) -> dict:
        """A stable target dict for one of their planeswalkers at this loyalty (the memos key on ids)."""
        key = (iid, loyalty)
        found = self._walker_memo.get(key)
        if found is None:
            found = self._walker_memo[key] = {
                "instance_id": iid,
                "name": name,
                "loyalty": loyalty,
                "_walker": True,
            }
        return found

    @staticmethod
    def _chips(variants) -> tuple:
        """One combined-kill piece per targeted-damage cast variant (per mode): its damage as ``chip``.

        Match 41753907 (2026-10-19): Lightning Strike (3) + Burst Lightning (2)
        on a 4/4 lifelink flier at T7, Lightning Strike + Shock on it as a 5/5 at
        T11, were never offered: removal was one spell per target.
        """
        chips, seen = [], set()
        for var in variants:
            if var.kind != "cast" or var.reach is None or var.bounce or var.aim or var.chip:
                continue
            amount = max(
                (c.limit or 0 for c in _removal_clauses(var.reach) if c.kind == "damage" and c.targeted),
                default=0,
            )
            if amount > 0 and (var.mode, amount) not in seen:
                seen.add((var.mode, amount))
                chips.append(dataclasses.replace(var, chip=amount))
        return tuple(chips)

    def _tokens(self, specs) -> tuple[dict, ...]:
        """Token bodies for ``token_specs`` output, each with a fresh id (unique within the search)."""
        bodies = make_tokens(specs, self.model.turn, self.model.our_rules, self._token_id)
        self._token_id += len(bodies)
        return bodies

    def _variants(self, hs: HandSpell) -> tuple[tuple, Variant | None]:
        spell, card = hs.spell, hs.spell.card
        info = hand_info(card)
        creature = spell.role == "creature" and _is_creature(card)
        body = _body(card, self.model.turn, self.model.our_rules) if creature else None
        counters = enters_counters(card) if body is not None and self.enters_triggers else 0
        if counters:  # its enters trigger puts its +1/+1 counters on itself
            body = {**body, "power": body["power"] + counters, "toughness": body["toughness"] + counters}
        self._register_scaling(body, None)  # printed stats: grows with our board once in
        extra_life = extra_life_cost(card)
        # What acts on casting: a spell's text; a permanent's own enters trigger without a
        # condition (``applied_text``). ``etb`` reads it: the card itself when nothing was left out.
        cast_text = applied_text(card, enters_triggers=self.enters_triggers or not creature)
        etb = (
            card
            if cast_text == _cast_text(card)
            else {"name": spell.name, "oracle_text": cast_text, "type_line": ""}
        )
        # Modes: a spell's, or those of a permanent's applied enters trigger.
        has_bullets = any(line.startswith("•") for line in cast_text.splitlines())
        oracle_modes = bullets(str(card.get("oracle_text") or "")) if has_bullets else []
        # Outside any 'choose one' bullet: creature tokens there come with every variant.
        outside = "\n".join(line for line in cast_text.splitlines() if not line.startswith("•"))
        base_specs = token_specs(outside, life_gained=0, life_gained_game=self.model.life_gained_game) or []
        # "... tokens equal to the number of other creatures you control named ~": counted as it resolves.
        named = named_token_specs(outside, spell.name)
        # "Create X ... tokens, where X is the amount of life you gained this turn": per life.
        per_life = tuple(
            token_specs(_life_scaled(outside, 1), life_gained=0) or ()
            if life_scaled_tokens(outside)
            else ()
        )
        if per_life:
            per_life = tuple(s for s in per_life if s not in base_specs) or per_life
        counter_match = _COUNTER_ON_TARGET.search(_text(etb)) if not creature else None
        counters = (
            (_COUNT_WORDS.get(counter_match.group("n")) or _int(counter_match.group("n")) or 0)
            if counter_match
            else 0
        )

        colors = _body_colors(card)

        def make(
            mode=None, text="", gain=0, reach=None, bounce=False, rock=False, loss=0, face=0, aim="", specs=(),
            sweep=0,
        ):  # fmt: skip
            if reach is not None and reach is not card and not _types(reach).strip():
                # A mode's or an enters trigger's text: its card's type and colours decide
                # protection and "hexproof from instants" (``board_assessment._protected_from``).
                reach = {**reach, "_source_types": _types(card), "_source_colors": colors}
            return Variant(
                hs.index, "cast", mode, text, body, gain, reach, bounce, rock, spell.mana_value, spell.pips,
                bool(info.instant_speed), loss, face, aim, tokens=self._tokens([*base_specs, *specs]),
                life_tokens=per_life, counter=counters, sweep=sweep, named_tokens=named,
            )  # fmt: skip

        def aimed(var: Variant, alt: int) -> list[Variant]:
            """An 'any target' spell's face-damage variant: the damage goes to the opponent."""
            if not alt:
                return []
            return [dataclasses.replace(var, reach=None, bounce=False, face=var.face + alt, aim="opponent")]

        if creature:
            # A creature's own text acts through its modes (bullets) or, without modes, its
            # enters trigger: removal (a fight needs this body and is not modelled), life gain,
            # damage to the opponent.
            reach = removal_reach(etb) if not oracle_modes else None
            etb_reach = etb if reach is not None and reach[0] != "fight" else None
            match = _LIFEGAIN.search(cast_text) if not oracle_modes else None
            alt, each = (0, 0) if oracle_modes else face_damage(etb)
            bounce = etb_reach is not None and reach[0] == "bounce"
        elif spell.role == "creature":
            # A noncreature that makes creature tokens (card_role calls it a creature): the rest
            # of its text acts too ("Create a 2/2 ... token. You gain 2 life.").
            reach = removal_reach(etb)
            etb_reach = etb if reach is not None and reach[0] != "fight" else None
            bounce = etb_reach is not None and reach[0] == "bounce"
            match = _LIFEGAIN.search(cast_text)
            alt, each = face_damage(etb)
        else:
            reach = removal_reach(etb) if spell.role in ("removal", "bounce") else None
            etb_reach = etb if reach is not None else None
            bounce = etb_reach is not None and spell.role == "bounce"
            match = _LIFEGAIN.search(_text(etb)) if spell.role == "lifegain" or counters else None
            alt, each = face_damage(etb)
        plain = make(
            reach=etb_reach,
            bounce=bounce,
            gain=int(match.group(1)) if match else 0,
            rock=spell.role == "ramp",
            loss=life_loss(etb),
            face=each,
            sweep=0 if oracle_modes else _sweep(_cast_text(etb)),
        )
        if spell.has_x or spell.role == "counter" or spell.uncastable:
            plain, variants = None, []  # never cast proactively (or can't be cast now)
        elif (
            not _modelled(plain)
            and not any(classify(spell.name, b)[0] != "other" for b in oracle_modes)
            and unmodelled_effect(card)
        ):
            # Nothing it does is valued (a pump, an aura, a fog, a planeswalker): never cast in a
            # line, where a placeholder value would put it ahead of holding it.
            plain, variants = None, []
        else:
            modes = [(i, b, *classify(spell.name, b)) for i, b in enumerate(oracle_modes)]
            if all(kind == "other" for _i, _b, kind, *_rest in modes):
                modes = []  # no mode changes anything we model: a plain cast
            variants, seen = [], set()
            for mode, bullet, kind, gain, reach, bounce in modes:
                pseudo = {"name": spell.name, "oracle_text": bullet, "type_line": ""}
                loss = life_loss(pseudo) + extra_life
                mode_alt, mode_each = face_damage(pseudo)
                specs = tuple(token_specs(_cast_text(pseudo)) or ())
                # Damage to each of their creatures (#87: Iroh's Demonstration's first mode).
                sweep = mode_sweep(spell.name, bullet)
                effect = (
                    kind,
                    gain,
                    reach is not None and removal_reach(reach),
                    loss,
                    mode_alt,
                    mode_each,
                    specs,
                    sweep,
                )
                if (kind == "other" and body is None and not mode_alt and not mode_each) or effect in seen:
                    continue  # a noncreature's 'other' mode does nothing; duplicates add nothing
                seen.add(effect)
                var = make(
                    mode,
                    mode_label(kind, gain, bullet, spell.name),
                    gain,
                    reach,
                    bounce,
                    loss=loss,
                    face=mode_each,
                    specs=specs,
                    sweep=sweep,
                )
                variants += [var, *aimed(var, mode_alt)]
            if not variants and plain is not None:
                variants = [plain, *aimed(plain, alt)]
        if info.landcycling is not None:
            kind = _CYCLE_KIND.search(_text(card))
            fetch = _CYCLE_FETCH.get(kind.group(1), "") if kind else ""
            variants.append(
                Variant(
                    hs.index, "cycle", None, "", None, 0, None, False, False, info.landcycling, (), True,
                    fetch=fetch,
                )
            )  # fmt: skip
        sneak = self._sneak_variant(hs, body)
        if sneak is not None:
            variants.append(sneak)
        return tuple(variants), plain

    def _sneak_variant(self, hs: HandSpell, body: dict | None) -> Variant | None:
        """A creature with Sneak cast in our declare-blockers step (``sneak``; ledger #156): its
        Sneak cost (plus the commander tax from the command zone), applied in ``finish`` after
        their blocks by returning one of our unblocked attackers. Its enters trigger is not
        applied (none on Leonardo, Big Brother, the case it was built for)."""
        from arenamcp.sneak import sneak_cost, with_tax

        card = hs.spell.card
        printed = sneak_cost(card) if body is not None else ""
        if not printed:
            return None
        casts = 0
        if hs.spell.zone == "command":
            commander = next(
                (c for c in self.model.commanders if _int(c.card.get("instance_id")) == hs.iid), None
            )
            casts = (commander.casts or 0) if commander is not None else 0
        cost = with_tax(printed, casts)
        info = hand_info({**card, "mana_cost": cost})
        return Variant(
            hs.index, "sneak", None, "", body, 0, None, False, False, info.mana_value, info.pips, False,
        )  # fmt: skip

    # -- small helpers -----------------------------------------------------------------------

    def tick(self) -> None:
        """Count a node expansion; past the hard deadline, stop the search."""
        self.nodes += 1
        self.work += _NODE_WORK
        if not self.free and time.perf_counter() - self.started > self.hard:
            raise Deadline

    def past_soft(self) -> bool:
        """Past the work budget: deterministic, unlike the hard cap's clock."""
        return self.work > self.budget

    def combat(self, attackers, blockers, life, *, first=False, tag=""):
        """``_combat`` memoised per search on (attacker ids, blocker ids, life)."""
        return self.combat_life(attackers, blockers, life, first=first, tag=tag)[:3]

    def combat_life(self, attackers, blockers, life, *, first=False, tag=""):
        """``_combat_life`` (lifelink gains too) memoised like ``combat``."""
        return self.combat_detail(attackers, blockers, life, first=first, tag=tag)[:5]

    def combat_detail(self, attackers, blockers, life, *, first=False, tag=""):
        """``combat_life`` plus each attacker's damage through, as sorted (id, damage) pairs, and
        the ids of the blockers that blocked."""
        attackers = sorted(attackers, key=lambda b: b["instance_id"])
        blockers = sorted(blockers, key=lambda b: b["instance_id"])
        able = sum(1 for b in blockers if b["_can_block"])
        if first and attackers and able and (len(attackers) + 1) ** able > _MAX_BLOCK_OPTIONS:
            self.exact_first = False  # the solver falls back to greedy blocks
        key = (tag, ids_of(attackers), ids_of(blockers), max(1, life))
        if self._scale_base:  # a per-creature body's power follows the board, not its id (#157)
            key += (
                tuple(
                    (b["instance_id"], b["power"], b["toughness"])
                    for b in (*attackers, *blockers)
                    if b["instance_id"] in self._scale_base
                ),
            )
        found = self._memo.get(key)
        if found is None:
            self.combats += 1
            self.work += 2 + len(attackers) + able
            damage, dead_att, dead_blk, gained, healed, through, used = _combat_life(
                attackers, blockers, life, detail=True
            )
            found = self._memo[key] = (
                damage, frozenset(dead_att), frozenset(dead_blk), gained, healed, tuple(sorted(through.items())),
                frozenset(used),
            )  # fmt: skip
        return found

    def kills(self, reach: dict, body: dict, ours=(), *, attacking: bool = False, mana: int = 0) -> bool:
        """``_kills`` memoised: a legal target, removed (a fight needs one of ``ours``).

        ``mana``: what is left to pay a ward with (a ward's mana is part of the cast).
        """
        power = max((b["power"] for b in ours), default=-1)
        key = (id(reach), id(body), power, attacking)
        found = self._kill_memo.get(key)
        if found is None:
            fighter = [{"power": power}] if power >= 0 else []
            found = self._kill_memo[key] = _kills(
                reach, body, ours=fighter, attacking=attacking, ward_mana=10**6
            )
        return found and self.ward(body) <= mana

    def ward(self, body: dict) -> int:
        found = self._ward_memo.get(id(body))
        if found is None:
            found = self._ward_memo[id(body)] = ward_cost(body)
        return found

    def bans(self, node: Node) -> frozenset:
        """Spell kinds a permanent this line put onto the battlefield stops us casting.

        "You can't cast permanent spells." (Codie, Vociferous Codex) cast on T
        leaves T+1 and T+2 with instants and sorceries only; the search never
        pays for a body with the rest of the hand. A restriction already on the
        board is ``_Spell.uncastable`` (``board_model.board_cast_bans``).
        """
        key = ids_of(node.ours)
        found = self._ban_memo.get(key)
        if found is None:
            kinds: set = set()
            for body in node.ours:
                kinds |= cast_bans(body.get("_card") or body, ours=True)
            found = self._ban_memo[key] = frozenset(kinds)
        return found

    def banned(self, hs: HandSpell, bans: frozenset) -> bool:
        return bool(bans) and spell_banned(hs.spell.card, bans)

    def material(self, body: dict) -> float:
        """m(b) = power + toughness / 2, +1 if it flies or can't be blocked, + its engine role
        (``engine_role``: what one of theirs drains and grows each turn)."""
        found = self._m_memo.get(id(body))
        if found is None:
            evasive = _flying(body) or body["_unblockable"]
            found = self._m_memo[id(body)] = (
                body["power"] + 0.5 * body["toughness"] + (1.0 if evasive else 0.0) + engine_role(body)
            )
        return found

    def value(self, life, opp_life, ours, theirs, hand, walkers=()) -> float:
        """V = u(life) - 0.6 opp life + our material - theirs + 0.3 hand (hand-tuned), less each
        planeswalker of theirs (``combat_solver.walker_value``: 9 + its loyalty up to 6)."""
        u = life + min(life, 10) + 2 * min(life, 5)
        return (
            u
            - 0.6 * opp_life
            + sum(self.material(b) for b in ours)
            - sum(self.material(b) for b in theirs)
            + 0.3 * sum(self.spells[i].hand_value for i in hand)
            - sum(walker_value(loyal) for _iid, _name, loyal in walkers)
        )

    def score(self, node: Node) -> tuple:
        """(class, timing, V), higher is better; V carries no race term here."""
        if node.win_at is not None:
            return (WIN, -node.win_at, 0.1 * node.overkill)
        if node.dead_at is not None:
            return (DEAD, node.dead_at, -0.1 * node.overkill)
        return (
            ALIVE,
            0,
            self.value(
                node.our_life, node.opp_life, node.ours, node.theirs + node.returning, node.hand, node.walkers
            ),
        )

    def sick(self, body, k, abs_turn, entered) -> bool:
        if self.haste or has_combat_keyword(body, "haste"):
            return False
        turn = entered.get(body["instance_id"])
        if turn is not None:
            return turn >= abs_turn
        return k == 0 and self.our_turn and body["_sick"]  # the snapshot's own bodies

    def sources(self, node: Node, land: Land | None) -> tuple[list, list]:
        """(sources before this line's rocks, all sources) on our turn ``node.k``."""
        base = self.model.sources_now if (node.k == 0 and self.our_turn) else self.model.sources_all
        before_rocks = list(base) + list(node.played)
        if land is not None and not land.tapped:
            before_rocks.append(land.source)
        return before_rocks, before_rocks + list(node.rocks)

    def land_options(self, node: Node) -> list:
        """One land per class (colours, enters tapped), at most three; [None] when no drop.

        Ranked first by the hand's pips the land is the only way to pay (a colour
        no source on the board makes yet), then by every pip it can pay. Four
        Forests in play and Forest + Mountain in hand with a {U/R} spell: the
        Mountain adds a colour, the fifth Forest adds nothing (bug 2026-10-09
        07:58, sealed FRA: Forest kept, Twinned Vision stranded).
        """
        if node.k == 0 and self.our_turn and (not self.model.land_drop_now or self.model.t_instant_only):
            return [None]
        if not node.lands:
            return [None]
        pips = [p for i in node.hand for p in self.spells[i].spell.pips]
        base = self.model.sources_now if (node.k == 0 and self.our_turn) else self.model.sources_all
        have: set = set()
        for source in list(base) + list(node.played) + list(node.rocks):
            have |= set(source.produces)
        have.discard("C")
        by_key: dict = {}
        for land in node.lands:
            by_key.setdefault(land.key, land)
        ranked = sorted(
            by_key.values(),
            key=lambda land: (
                land.tapped,
                -sum(1 for p in pips if p & land.source.produces and not p & have),
                -sum(1 for p in pips if p & land.source.produces),
                "".join(sorted(land.colors)),
                land.name,
                land.iid,
            ),
        )
        return ranked[:_MAX_LAND_CLASSES]

    def targets(self, reach: dict, theirs, their_tapped, ours=(), mana: int = 10**6) -> list[dict]:
        """The top two killable bodies by (power, toughness), identical bodies once.

        ``mana``: what is left for a ward after the spell itself.
        """
        killable = sorted(
            (b for b in theirs if self.kills(reach, b, ours, mana=mana)), key=self._threat_order
        )
        out, seen = [], set()
        for body in killable:
            sig = (
                body["name"],
                body["power"],
                body["toughness"],
                tuple(body["keywords"]),
                body["instance_id"] in their_tapped,
            )
            if sig not in seen:
                seen.add(sig)
                out.append(body)
            if len(out) == _TARGETS:
                break
        return out

    @staticmethod
    def _threat_order(body: dict) -> tuple:
        """Removal targets, most dangerous first: power, a lifelinker's counted twice (each hit is a
        swing of twice its power in the race), plus its engine role (``engine_role``), then toughness."""
        power = body["power"] * (2 if _lifelink(body) else 1) + engine_role(body)
        return (-power, -body["power"], -body["toughness"], body["name"], body["instance_id"])

    def marked(self, body: dict, node: Node) -> int:
        """Damage already marked on their creature: only on our turn T (it wears off at end of turn)."""
        if node.k or not self.our_turn:
            return 0
        return max(0, _int((body.get("_card") or {}).get("damage")) or 0)

    def burn(self, var: Variant, body: dict, mana: int) -> int:
        """``burn_damage`` memoised: what the chip ``var`` deals ``body`` (0: not a legal target),
        ``mana`` being what is left for a ward."""
        key = (id(var), id(body))
        found = self._burn_memo.get(key)
        if found is None:
            found = self._burn_memo[key] = (
                min(var.chip, burn_damage(var.reach, body, ward_mana=10**6)) if var.chip else 0
            )
        return found if found and self.ward(body) <= mana else 0

    def swept(self, var: Variant, body: dict) -> bool:
        """A sweep (``Variant.sweep``) damages this creature of theirs: not indestructible, no
        protection from the spell."""
        key = ("sweep", id(var), id(body))
        found = self._burn_memo.get(key)
        if found is None:
            card = self.spells[var.spell].spell.card
            found = self._burn_memo[key] = bool(var.sweep) and not (
                has_combat_keyword(body, "indestructible") or _protected_from(body, card, targeted=False)
            )
        return found

    def piece(self, index: int, body: dict, sources: int) -> int:
        """The most damage hand spell ``index`` can add to killing ``body`` this turn: a chip or a sweep."""
        best = max((self.burn(c, body, sources) for c in self.spells[index].chips), default=0)
        sweeps = [v.sweep for v in self.spells[index].variants if v.sweep and self.swept(v, body)]
        return max([best, *sweeps])

    def chip_targets(self, node: Node, position: int, var: Variant, sources: int) -> list[dict]:
        """Up to two of their creatures ``var`` (a chip) can be one piece of killing this turn.

        A target it can't kill alone (``targets`` offers those) but can with
        the damage already marked on it this turn, or with at most two other
        damage spells in hand (each one's best piece, mana aside: ``cast_sets``
        checks that), most dangerous first.
        """
        spare = sources - var.mana_value
        found = []
        for body in sorted(node.theirs, key=self._threat_order):
            mine = self.burn(var, body, spare)
            need = body["toughness"] - self.marked(body, node)
            if not mine or need <= 0 or mine >= body["toughness"]:
                continue  # not a legal target, already dead, or a single-spell kill
            if mine < need:
                others = sorted(
                    (self.piece(i, body, sources) for p, i in enumerate(node.hand) if p != position),
                    reverse=True,
                )
                if mine + sum(others[: _MAX_SPELLS_PER_TURN - 1]) < need:
                    continue
            found.append(body)
            if len(found) == _TARGETS:
                break
        return found

    def item_value(self, var: Variant, target: dict | None, theirs) -> float:
        """``_spell_value`` of one variant against the node's enemy board."""
        key = (id(var), id(target), any(_flying(b) for b in theirs))
        found = self._value_memo.get(key)
        if found is not None:
            return found
        hs = self.spells[var.spell]
        value = 0.0
        if var.kind == "cycle":
            value = 0.5
        elif var.body is not None:
            value = _spell_value(hs.spell, survival=self.survival, theirs=list(theirs))
        elif var.rock:
            value = 1.5
        elif var.reach is None and not var.gain and not var.tokens and not var.life_tokens:
            value = 1.0
        for token in var.tokens:  # a token is worth what the same creature spell would be
            pseudo = _Spell(token["_card"], token["name"], "creature", 0, (), False)
            value += _spell_value(pseudo, survival=self.survival, theirs=list(theirs))
        value += 0.5 * var.face
        if var.reach is not None and target is not None:
            pseudo = _Spell(
                var.reach, hs.name, "bounce" if var.bounce else "removal", var.mana_value, var.pips, False
            )
            value += _spell_value(pseudo, survival=self.survival, theirs=[target])
        if var.gain:
            value += (1.0 if var.body is None and var.reach is None else 0.0) + (
                var.gain / 2 if self.survival else 0.0
            )
        self._value_memo[key] = value
        return value

    def choice_value(self, choice, theirs, gained: int = 0, ours=()) -> float:
        """The items' values; a life-scaled token maker counts the life ``gained`` this turn
        before it plus what the rest of the set gains (``_apply_casts`` casts the gains first);
        a board-counted one (Hare Apparent) counts ``ours`` plus the set's bodies before it."""
        value = sum(self.item_value(v, t, theirs) for v, t in choice)
        if any(v.named_tokens for v, _ in choice):
            board = list(ours)
            for var, _target in choice:
                if var.kind != "cast":
                    continue
                board += [var.body] if var.body is not None else []
                for token in self._named_bodies(var, board):
                    pseudo = _Spell(token["_card"], token["name"], "creature", 0, (), False)
                    value += _spell_value(pseudo, survival=self.survival, theirs=list(theirs))
                    board.append(token)
        # A combined kill is worth one removal of its target (``_spell_value``'s), however many pieces.
        for target in {id(t): t for v, t in choice if v.chip and t is not None}.values():
            value += 3 + target["power"] * (1.5 if self.survival else 1.0)
        # Burn at a planeswalker: what it takes off ``walker_value`` (all of it when it dies).
        burnt: dict[int, tuple[int, int]] = {}
        for var, target in choice:
            if var.walker and target is not None:
                iid, before = target["instance_id"], burnt.get(target["instance_id"], (target["loyalty"], 0))
                burnt[iid] = (before[0], before[1] + var.walker)
        for loyal, damage in burnt.values():
            value += walker_value(loyal) - (walker_value(loyal - damage) if damage < loyal else 0.0)
        # A sweep: each creature of theirs it kills by itself.
        for var in (v for v, _ in choice if v.sweep):
            value += self.sweep_value(var, theirs)
        if any(v.life_tokens for v, _ in choice):
            x = min(10, gained + sum(v.gain for v, _ in choice if not v.life_tokens))
            for var, _target in choice:
                for token in self._life_bodies(var, x):
                    pseudo = _Spell(token["_card"], token["name"], "creature", 0, (), False)
                    value += _spell_value(pseudo, survival=self.survival, theirs=list(theirs))
        return value

    def sweep_value(self, var: Variant, theirs) -> float:
        """What a sweep (``Variant.sweep``) is worth: each creature of theirs it kills by itself."""
        if not var.sweep:
            return 0.0
        return sum(
            1 + b["power"] * (1.5 if self.survival else 1.0)
            for b in theirs
            if b["toughness"] <= var.sweep and self.swept(var, b)
        )

    def _life_bodies(self, var: Variant, gained: int) -> tuple[dict, ...]:
        """The token bodies a life-scaled token maker makes with ``gained`` life gained
        this turn (memoised, so each body keeps one id and identity within the search)."""
        if not var.life_tokens or gained <= 0:
            return ()
        key = (id(var), gained)
        found = self._life_token_memo.get(key)
        if found is None:
            specs = [(min(10, c * gained), p, t, kw, n) for c, p, t, kw, n in var.life_tokens]
            found = self._life_token_memo[key] = (var, self._tokens(specs))
        return found[1]

    def set_cost(self, ours, choice) -> int:
        """The mana a set of casts costs, cast in ``begin``'s order: a spell's own cost
        reduction counts our creatures then, the set's other bodies and counters included."""
        total = sum(v.mana_value + (self.ward(t) if t is not None else 0) for v, t in choice)
        reducing = [v for v, _ in choice if self.spells[v.spell].reduction is not None]
        if not reducing:
            return total
        others = [v for v, _ in choice if self.spells[v.spell].reduction is None]
        powers = [b["power"] for b in ours] + [b["power"] for v in others for b in _bodies(v)]
        counters = sum(v.counter for v in others)
        if counters and powers:
            powers[powers.index(max(powers))] += counters
        permanents = list(self.model.our_permanents)
        for var in reducing:
            saved = reduction_amount(self.spells[var.spell].reduction, powers, permanents)
            total -= var.mana_value - reduced_mana_value(var.mana_value, len(var.pips), saved)
        return total

    def anthem_bodies(self, bodies, opp_life: int, alive=None) -> list[dict]:
        """``bodies`` (theirs) with their life-threshold anthems (``their_anthems``) as ``opp_life``
        has them: on at their starting life + N or more while the source is on the battlefield
        (``alive``, default ``bodies``), off below it. The snapshot's power/toughness already has
        each anthem that is on at the root; a switched body gets a fresh id (the combat memo keys on
        ids) and keeps its original one as ``_origin``. Ledger #177 (c0af235f T12): their seven
        creatures had Righteous Valkyrie's +2/+2 at 32 life (25 + 7); our attack ended it."""
        if not self.their_anthems:
            return list(bodies)
        start, root_life = self.model.starting_life, self.model.opp_life
        present = {b.get("_origin", b["instance_id"]) for b in (bodies if alive is None else alive)}

        def covers(index: int, origin: int) -> bool:
            source, _creature, (_need, _p, _t, scope) = self.their_anthems[index]
            return scope == "all" or (scope == "others") == (origin != source)

        def on(index: int, life: int, root: bool) -> bool:
            source, creature, (need, _p, _t, _scope) = self.their_anthems[index]
            return life >= start + need and (root or not creature or source in present)

        out = []
        for body in bodies:
            origin = body.get("_origin", body["instance_id"])
            indices = [i for i in range(len(self.their_anthems)) if covers(i, origin)]
            had = body.get("_anthems")
            if had is None:
                had = frozenset(i for i in indices if on(i, root_life, True))
            want = frozenset(i for i in indices if on(i, opp_life, False))
            if want == had:
                out.append(body)
                continue
            power = sum(self.their_anthems[i][2][1] for i in want - had) - sum(
                self.their_anthems[i][2][1] for i in had - want
            )
            toughness = sum(self.their_anthems[i][2][2] for i in want - had) - sum(
                self.their_anthems[i][2][2] for i in had - want
            )
            key = (body["instance_id"], want)
            found = self._anthem_memo.get(key)
            if found is None:
                found = self._anthem_memo[key] = {
                    **body,
                    "instance_id": _ANTHEM_IDS + len(self._anthem_memo),
                    "_origin": origin,
                    "_anthems": want,
                    "power": body["power"] + power,
                    "toughness": body["toughness"] + toughness,
                }
            if found["toughness"] > 0:
                out.append(found)
        return out

    def _countered(self, body: dict, n: int) -> dict:
        """``body`` with ``n`` more +1/+1 counters: a fresh id (the combat memo keys on ids)."""
        key = (body["instance_id"], n)
        found = self._counter_memo.get(key)
        if found is None:
            found = self._counter_memo[key] = {
                **body,
                "instance_id": _COUNTER_IDS + len(self._counter_memo),
                "power": body["power"] + n,
                "toughness": body["toughness"] + n,
            }
        return found

    def gained_at_start(self, node: Node) -> int:
        """Life we gained this turn before the line acts: the snapshot's on T, 0 on later turns."""
        return int(self.model.life_gained_turn or 0) if (node.k == 0 and self.our_turn) else 0

    # -- their engines (ledger #128, #129, #132, #133, #143) ----------------------------------

    def _enters_under_them(self, body: dict, tapped: set) -> int:
        """A creature of ours entering under their Authority of the Consuls: it enters tapped (no
        attack, no block on their next turn: ``tapped`` gets its id) and they gain life (returned)."""
        engines = self.engines
        if engines.enter_tapped:
            tapped.add(body["instance_id"])
        return 0 if engines.no_gain else engines.life_per_our_creature

    def leaves(self, body: dict) -> bool:
        """Our creature returns to hand (or is sacrificed) at our end step (Fleeting Effigy)."""
        found = self._leaves_memo.get(id(body))
        if found is None:
            from arenamcp.combat_solver import leaves_at_our_end_step

            found = self._leaves_memo[id(body)] = (body, leaves_at_our_end_step(body.get("_card") or body))
        return found[1]

    def _end_step(self, mid: Mid) -> None:
        """Our end step: a creature that returns to its owner's hand then (Fleeting Effigy) is no
        blocker on their turn; one cast from our hand in this line goes back to the hand.

        Ledger #129 (match 3e545401 T17, 2026-10-19): the lines kept Fleeting Effigy
        as a permanent untapped blocker ('LINE best' as one).
        """
        leaving = [b for b in mid.ours if self.leaves(b)]
        if not leaving:
            return
        mid.ours = self.rescale([b for b in mid.ours if not self.leaves(b)])  # one creature fewer (#157)
        back = {self._body_spell[id(b)] for b in leaving if id(b) in self._body_spell} - set(mid.hand)
        if back:
            mid.hand = sorted([*mid.hand, *back], key=lambda i: self._hand_pos.get(i, i))

    def their_gains(self, mid: Mid) -> int:
        """Their life-gain events on their turn after our turn ``mid``: their gain triggers, their
        planeswalkers' gain abilities (those still alive) and our creatures that entered this turn
        under their Authority of the Consuls. 0 without growth or loyalty for them to feed."""
        engines = self.engines
        if engines is None or not (self.growers or self.walker_gains):
            return 0
        entries = sum(1 for turn in mid.entered.values() if turn == mid.node.abs_turn)
        return engines.gain_events(mid.walkers.keys(), entries)

    def walker_growth(self, iid: int, gains: int) -> int:
        """Loyalty one of their planeswalkers gets on their turn: a plus ability (paranoid: +1), or
        its own life-gain trigger's counters when more (Ajani Resolute with Authority of the
        Consuls and our creature cast: +2)."""
        return max(1, self.walker_gains.get(iid, 0) * gains)

    def turn_growth(self, theirs, gains: int, *, combat_only: bool = False) -> dict[int, int]:
        """+1/+1 counters each of their creatures gets on one turn of theirs before their attack
        (``TheirEngines.growth``: ``gains`` life-gain events, their draws, and beginning-of-combat
        counters ``combat_targets``), by instance id; a team trigger (Lyra: '... on each Angel you
        control') counts while its source is alive."""
        engines = self.engines
        if engines is None or not self.growers:
            return {}
        origins = {_int((b.get("_card") or b).get("instance_id")) for b in theirs}
        team = tuple(t for t in engines.team if t[0] in origins or t[0] not in self._their_creature_ids)
        key = (ids_of(theirs), gains, team, combat_only)
        found = self._turn_growth_memo.get(key)
        if found is None:
            combat_grow = engines.combat_targets(theirs, alive_origins=origins)
            if combat_only:
                found = combat_grow
            else:
                found = {
                    b["instance_id"]: n
                    for b in theirs
                    if (
                        n := engines.growth(b.get("_card") or b, gains=gains, team=team)
                        + combat_grow.get(b["instance_id"], 0)
                    )
                }
            self._turn_growth_memo[key] = found
        return found

    def their_tokens(self, theirs, generation: int, *, now: bool = False) -> list[dict]:
        """The creature tokens their token engines make on a turn of theirs (``_tokens``, at most
        ``board_assessment._MAX_TOKEN_BODIES`` bodies; ``made_tokens``: the same bodies for the
        same engine and turn ``generation``); ``now``: the turn under way, its activations paid with
        the mana they have untapped. Ledger #172 (match 2b77f14d: 4, then 8 Squirrels)."""
        out: list[dict] = []
        for body in theirs:
            room = _MAX_TOKEN_BODIES - len(out)
            count = body.get("_tokens") or 0
            if count and now:
                # (its triggers have happened: the snapshot shows their tokens)
                count = self.engines.body_tokens(
                    body.get("_card") or body, mana=self.engines.mana_now, activated=True
                )[0]
            if count and room > 0:
                out += made_tokens(body, min(count, room), generation, dict(self.model.their_rules))
        return out

    def their_drain(self, theirs, *, after_upkeep: bool = False) -> int:
        """Damage their drain engines deal us on one turn of theirs: their noncreature engines and
        each creature of theirs still alive (``_drain``; ``after_upkeep`` skips upkeep-only drains
        that already resolved this turn, ledger #198). Ledger #133 (match 16ed84d0)."""
        if not self.drains:
            return 0
        if after_upkeep:
            return self.engines.static_drain_after_upkeep + sum(
                b.get("_drain_after_upkeep", b.get("_drain") or 0) for b in theirs
            )
        return self.engines.static_drain + sum(b.get("_drain") or 0 for b in theirs)

    def _lifelink_growth(self, theirs_after: list, their_tapped: frozenset, gained: int, present) -> tuple:
        """After their lifelink attackers gained them life: their creatures that grow on it
        (``_per_gain``), one gain event (their next attack sees it; this one's damage is dealt).
        Returns (their bodies, their tapped ids) with the grown copies' ids."""
        if gained <= 0 or not self.growers or self.engines.no_gain:
            return theirs_after, their_tapped
        alive = {b["instance_id"] for b in present}  # on the battlefield (not bounced)
        grown, tapped = [], set(their_tapped)
        for body in theirs_after:
            n = body.get("_per_gain") or 0
            if not n or body["instance_id"] not in alive:
                grown.append(body)
                continue
            new = self._countered(body, n)
            if body["instance_id"] in tapped:
                tapped.add(new["instance_id"])
            grown.append(new)
        return grown, frozenset(tapped)

    def choice_text(self, choice) -> str:
        return " + ".join(
            f"{self.spells[v.spell].name}:{v.kind}:{v.mode}:{t['instance_id'] if t else ''}"
            for v, t in choice
        )

    def cast_sets(self, node: Node, sources: list, *, instant_only: bool) -> list[tuple]:
        """Every affordable set of up to three spells (one variant each), best value first; () included.

        A set whose life payments would kill us is never cast.
        """
        theirs, their_tapped = node.theirs, node.their_tapped
        bans = self.bans(node)
        marking = node.k == 0 and self.our_turn  # damage marked this turn still counts
        key = (
            node.hand,
            tuple(sorted("".join(sorted(s.produces)) for s in sources)),
            ids_of(theirs),
            their_tapped,
            instant_only,
            max((b["power"] for b in node.ours), default=-1),
            node.our_life,
            bans,
            marking,
            node.walkers,
            self.gained_at_start(node),
            ids_of(node.ours) if self.any_named else (),
        )
        found = self._cast_memo.get(key)
        if found is not None:
            return found
        options: list[tuple[int, list]] = []
        for position, index in enumerate(node.hand):
            items = []
            banned = self.banned(self.spells[index], bans)
            reduced = self.spells[index].reduction is not None
            hs = self.spells[index]
            for var in hs.variants + hs.chips + hs.walker_vars:
                if (instant_only and not var.instant) or (var.mana_value > len(sources) and not reduced):
                    continue
                if (banned and var.kind in ("cast", "sneak")) or var.loss >= node.our_life:
                    continue
                if var.chip:  # one piece of a combined kill (completed below, or the set is dropped)
                    items.extend((var, t) for t in self.chip_targets(node, position, var, len(sources)))
                elif var.walker:  # burn at one of their planeswalkers
                    items.extend((var, self.walker_body(*w)) for w in node.walkers)
                elif var.reach is not None:  # no killable target: hold it
                    spare = len(sources) - var.mana_value
                    aims = self.targets(var.reach, theirs, their_tapped, node.ours, spare)
                    items.extend((var, t) for t in aims)
                    if not aims and var.body is not None:
                        items.append((var, None))  # a creature whose enters trigger finds no target
                else:
                    items.append((var, None))
            if items:
                options.append((position, items))
        need = {b["instance_id"]: b["toughness"] - self.marked(b, node) for b in theirs}
        names = [self.spells[i].name for i in node.hand]
        scored: list[tuple] = [((), 0.0, 0)]
        for size in range(1, min(_MAX_SPELLS_PER_TURN, len(options)) + 1):
            for combo in combinations(options, size):
                if not self.free and time.perf_counter() - self.started > self.hard:
                    raise Deadline  # a huge hand: stop enumerating
                positions = [p for p, _ in combo]
                # Copies of one card are interchangeable: always the earliest copies.
                if any(names[p] == names[q] and q not in positions for p in positions for q in range(p)):
                    continue
                for choice in product(*(items for _, items in combo)):
                    if any(
                        names[combo[a][0]] == names[combo[b][0]]
                        and combo[a][1].index(choice[a]) > combo[b][1].index(choice[b])
                        for a in range(size)
                        for b in range(a + 1, size)
                    ):
                        continue
                    hit = [t["instance_id"] for v, t in choice if t is not None and not (v.chip or v.walker)]
                    if any(v.walker for v, _ in choice) and self._spare_walker_burn(choice):
                        continue
                    if any(v.chip for v, _ in choice):
                        pieces = self._pieces(choice, need)
                        if pieces is None:
                            continue  # a piece whose kill the set doesn't complete, or one too many
                        hit += pieces
                    total = self.set_cost(node.ours, choice)
                    if len(hit) != len(set(hit)) or total > len(sources):
                        continue
                    if sum(v.loss for v, _ in choice) >= node.our_life:
                        continue
                    pips = tuple(p for v, _ in choice for p in v.pips)
                    if pips and not _pip_matching(pips, sources):
                        continue
                    value = self.choice_value(choice, theirs, self.gained_at_start(node), node.ours)
                    scored.append((choice, value, total))
        scored.sort(key=lambda item: (-round(item[1], 6), -item[2], self.choice_text(item[0])))
        result = self._cast_memo[key] = [item[0] for item in scored]
        return result

    @staticmethod
    def _spare_walker_burn(choice) -> bool:
        """Burn at a planeswalker that its loyalty doesn't need (it dies without that spell)."""
        aimed: dict[int, list[int]] = {}
        loyal: dict[int, int] = {}
        for var, target in choice:
            if var.walker and target is not None:
                aimed.setdefault(target["instance_id"], []).append(var.walker)
                loyal[target["instance_id"]] = target["loyalty"]
        return any(sum(d) - min(d) >= loyal[iid] for iid, d in aimed.items())

    def _pieces(self, choice, need: dict) -> list | None:
        """The ids a set's chips kill, or None unless every chip target dies and no piece is spare.

        Each chip target needs its pieces' damage, plus the set's sweeps
        (Tectonic Hazard's 1 to each), to reach its toughness less the damage
        already marked (``need``); a piece the kill doesn't need (Shock + Shock
        + Strike on a 4/5) makes the set a worse copy of a smaller one.
        """
        aimed: dict[int, tuple[dict, list[int]]] = {}
        for var, target in choice:
            if var.chip and target is not None:
                aimed.setdefault(target["instance_id"], (target, []))[1].append(var.chip)
        sweeps = [v for v, _ in choice if v.sweep]
        for iid, (target, damage) in aimed.items():
            total = sum(damage) + sum(v.sweep for v in sweeps if self.swept(v, target))
            if total < need.get(iid, 10**6) or total - min(damage) >= need.get(iid, 10**6):
                return None
        return list(aimed)

    def play_keys(self, land, casts) -> set:
        keys: set = set()
        if land is not None:
            keys.add(("land", land.key))
        for var, _target in casts:
            hs = self.spells[var.spell]
            if var.kind == "cycle":
                keys.add(("cycle", hs.key_iid))
            elif var.kind == "sneak":  # not the card's ordinary cast (its own option, its own key)
                keys.add(("sneak", hs.key_iid))
            else:
                keys.update({("cast", hs.key_iid, None), ("cast", hs.key_iid, var.mode)})
        if not casts:
            keys.add(("nocast",))
        return keys

    # -- our turn --------------------------------------------------------------------------

    def begin(self, node: Node, land: Land | None, casts: tuple) -> Mid:
        """Our turn ``node.k`` with this land and these casts (cast now unless we attack first)."""
        mid = Mid.__new__(Mid)
        if self.pumps_possible and len(casts) > 1:
            # Creature spells first, then the noncreature spells whose casts pump them (ledger #94:
            # Slickshot Show-Off, then Tectonic Hazard); the order below still comes first.
            casts = tuple(
                sorted(
                    casts,
                    key=lambda c: (
                        bool(c[0].life_tokens or self.spells[c[0].spell].reduction is not None),
                        c[0].kind == "cast" and "noncreature" in self.kinds(c[0].spell),
                    ),
                )
            )
        elif any(v.life_tokens or self.spells[v.spell].reduction is not None for v, _ in casts):
            # Gain life first, then cast what scales with the life gained this turn (pro game
            # 2026-10-18, YFRA match 1 T11: Something Worth Saving, then Germinate Recruits);
            # bodies and counters before a spell they make cheaper (T15: Seed Suture, then Ghalta).
            casts = tuple(
                sorted(
                    casts,
                    key=lambda c: bool(c[0].life_tokens or self.spells[c[0].spell].reduction is not None),
                )
            )
        mid.node, mid.land, mid.casts = node, land, casts
        mid.gained = self.gained_at_start(node)
        mid.base_sources, mid.sources = self.sources(node, land)
        mid.used_mv = self.set_cost(node.ours, casts)
        mid.used_pips = tuple(p for v, _ in casts for p in v.pips)
        mid.our_life, mid.opp_life = node.our_life, node.opp_life
        mid.ours, mid.theirs = list(node.ours), list(node.theirs)
        mid.our_tapped = set(node.our_tapped)
        mid.entered = dict(node.entered)
        mid.hand = list(node.hand)
        mid.lands = [x for x in node.lands if x is not land]
        mid.played = list(node.played) + ([land.source] if land is not None else [])
        mid.rocks = list(node.rocks)
        mid.returning = list(node.returning)
        mid.attack_first = node.k == 0 and self.our_turn and self.model.t_casts_post_combat
        mid.applied = False
        mid.modes, mid.targets, mid.cycles, mid.cast_names = [], [], [], []
        mid.walkers = {iid: (name, loyal) for iid, name, loyal in node.walkers}
        mid.pumps = dict(self.root_pumps) if node.k == 0 else {}
        mid.keys = self.play_keys(land, casts)
        if not mid.attack_first:
            self._apply_casts(mid)
        return mid

    def _apply_casts(self, mid: Mid) -> None:
        mid.applied = True
        chipped: dict[int, int] = {}  # damage combined-kill pieces dealt each target so far
        for var, target in mid.casts:
            if var.kind == "sneak":
                continue  # cast after their blocks (``finish``)
            hs = self.spells[var.spell]
            if var.spell in mid.hand:
                mid.hand.remove(var.spell)
            if var.kind == "cycle":  # an untapped basic (its type, or the most needed colour), next turn
                mid.cycles.append(hs.name)
                color = var.fetch or self._needed_color(mid.hand)
                source = SimpleNamespace(produces=frozenset({color}), name=_BASICS[color])
                colors = frozenset({color}) - {"C"}
                mid.lands.append(Land(-hs.iid - 1_000_000, _BASICS[color], colors, False, source))
                continue
            mid.cast_names.append(hs.name)
            if self.pumps_possible:  # its cast triggers resolve first: the bodies already in play
                self._cast_triggers(mid, var.spell)
            if var.mode_text:
                mid.modes.append((hs.name, var.mode_text))
            if var.aim and not var.walker:  # (a planeswalker target is named below)
                mid.targets.append((hs.name, var.aim))
            mid.our_life -= var.loss
            mid.opp_life -= var.face
            if var.counter and mid.ours:
                old = max(mid.ours, key=lambda b: (b["power"], b["toughness"], -b["instance_id"]))
                new = self._countered(old, var.counter)
                mid.ours = [new if b is old else b for b in mid.ours]
                if old["instance_id"] in mid.pumps:
                    mid.pumps[new["instance_id"]] = mid.pumps[old["instance_id"]]
                if old["instance_id"] in mid.entered:
                    mid.entered[new["instance_id"]] = mid.entered[old["instance_id"]]
                if old["instance_id"] in mid.our_tapped:
                    mid.our_tapped.add(new["instance_id"])
            made = list(var.tokens) + list(self._life_bodies(var, mid.gained))
            for body in ([var.body] if var.body is not None else []) + made:
                mid.ours.append(body)
                mid.entered[body["instance_id"]] = mid.node.abs_turn
                if self.consuls:
                    mid.opp_life += self._enters_under_them(body, mid.our_tapped)
            for body in self._named_bodies(var, mid.ours):  # counted with its own body in (#157)
                mid.ours.append(body)
                mid.entered[body["instance_id"]] = mid.node.abs_turn
                if self.consuls:
                    mid.opp_life += self._enters_under_them(body, mid.our_tapped)
            if target is not None and var.walker:  # burn at one of their planeswalkers
                iid = target["instance_id"]
                if iid in mid.walkers:
                    name, loyal = mid.walkers[iid]
                    mid.targets.append((hs.name, name))
                    if loyal - var.walker > 0:
                        mid.walkers[iid] = (name, loyal - var.walker)
                    else:
                        del mid.walkers[iid]
            elif target is not None and var.chip and any(b is target for b in mid.theirs):
                iid = target["instance_id"]
                chipped[iid] = chipped.get(iid, self.marked(target, mid.node)) + var.chip
                mid.targets.append((hs.name, target["name"]))
                if chipped[iid] >= target["toughness"]:  # the last piece: it dies
                    mid.theirs = [b for b in mid.theirs if b is not target]
            elif target is not None and any(b is target for b in mid.theirs):
                mid.theirs = [b for b in mid.theirs if b is not target]
                mid.targets.append((hs.name, target["name"]))
                if var.bounce:
                    mid.returning.append(target)
            if var.sweep:  # damage to each of their creatures, on top of any pieces aimed at them
                for body in list(mid.theirs):
                    if self.swept(var, body):
                        iid = body["instance_id"]
                        chipped[iid] = chipped.get(iid, self.marked(body, mid.node)) + var.sweep
                        if chipped[iid] >= body["toughness"]:
                            mid.theirs = [b for b in mid.theirs if b is not body]
            mid.our_life += var.gain
            mid.gained += var.gain
            if var.rock:
                mid.rocks.append(SimpleNamespace(produces=frozenset("WUBRGC"), name=hs.name))
        mid.ours = self.rescale(mid.ours)  # "+1/+0 for each other creature you control" (#157)

    def cast_pump(self, body: dict) -> tuple:
        """``cast_triggers.cast_pumps`` of a body, memoised."""
        found = self._cast_pump_memo.get(id(body))
        if found is None:
            found = self._cast_pump_memo[id(body)] = (body, cast_pumps(body.get("_card") or body))
        return found[1]

    def kinds(self, index: int) -> frozenset:
        """The cast-trigger kinds hand spell ``index`` counts as (``cast_triggers.spell_kinds``)."""
        found = self._kinds_memo.get(index)
        if found is None:
            found = self._kinds_memo[index] = spell_kinds(self.spells[index].spell.card)
        return found

    def _cast_triggers(self, mid: Mid, index: int) -> None:
        """Casting hand spell ``index`` pumps our creatures until end of turn (``mid.pumps``).

        Prowess and "whenever you cast a noncreature spell, ~ gets +2/+0 until
        end of turn" (Slickshot Show-Off) trigger on the cast, before it
        resolves: every body already on the battlefield, those cast earlier
        this turn included, gets its pump; a spell whose removal finds its
        target gone was still cast. Ledger #94 (match 18b1601a T9, 2026-10-19).
        """
        self._pump_kinds(mid, self.kinds(index))

    def _pump_kinds(self, mid: Any, kinds: frozenset) -> None:
        """``_cast_triggers`` for a cast of these kinds (``cast_triggers.spell_kinds``)."""
        adds: dict[int, tuple[int, int]] = {}

        def add(bodies, power: int, toughness: int) -> None:
            for body in bodies:
                p, t = adds.get(body["instance_id"], (0, 0))
                adds[body["instance_id"]] = (p + power, t + toughness)

        for kind, power, toughness, _who in self.board_pumps:
            if kind in kinds:
                add(mid.ours, power, toughness)
        for body in mid.ours:
            for kind, power, toughness, who in self.cast_pump(body):
                if kind not in kinds:
                    continue
                if who == "self":
                    add([body], power, toughness)
                else:
                    add([b for b in mid.ours if who == "team" or b is not body], power, toughness)
        for iid, (power, toughness) in adds.items():
            p, t = mid.pumps.get(iid, (0, 0))
            mid.pumps[iid] = (p + power, t + toughness)

    def cast_pumped(self, bodies, pumps: dict) -> tuple[list[dict], str]:
        """``bodies`` with this turn's cast pumps (``_cast_triggers``) for our attack, and a
        combat-memo tag naming them (a pumped body keeps its id: entered, tapped and the attack
        keys still find it, so the tag keeps the memo apart)."""
        if not pumps:
            return list(bodies), ""
        out, sig = [], []
        for body in bodies:
            pump = pumps.get(body["instance_id"])
            if not pump or pump == (0, 0):
                out.append(body)
                continue
            key = (id(body), pump)
            found = self._cast_pumped_memo.get(key)
            if found is None:
                found = self._cast_pumped_memo[key] = (
                    body,
                    {**body, "power": body["power"] + pump[0], "toughness": body["toughness"] + pump[1]},
                )
            out.append(found[1])
            sig.append(f"{body['instance_id']}+{pump[0]}/{pump[1]}")
        return out, ("cp" + ",".join(sorted(sig))) if sig else ""

    def _needed_color(self, hand) -> str:
        have = set().union(*(s.produces for s in self.model.sources_all)) if self.model.sources_all else set()
        counts: dict[str, float] = {}
        for index in hand:
            for pip in self.spells[index].spell.pips:
                for color in pip & set(_BASICS):
                    counts[color] = counts.get(color, 0.0) + (2.0 if not pip & have else 1.0) / len(pip)
        if not counts:
            return min(have & set(_BASICS), key="WUBRG".index, default="U")
        return min(counts, key=lambda c: (-counts[c], "WUBRG".index(c)))

    def attack_choices(self, mid: Mid) -> tuple[list[tuple[str, tuple]], bool]:
        """([(name, attacker ids)], whether this turn still has our attack) for our attack this turn."""
        node, model = mid.node, self.model
        if node.k == 0 and self.our_turn:
            if model.any_ours_attacking and model.in_combat_before_damage:
                return [("declared", ids_of(b for b in mid.ours if b["_attacking"]))], True
            if not model.our_attack_pending:
                return [("none", ())], False
        able = [
            b
            for b in mid.ours
            if b["instance_id"] not in mid.our_tapped
            and b["_can_attack"]
            and not self.sick(b, node.k, node.abs_turn, mid.entered)
        ]
        if not able:
            return [("none", ())], True
        blockers = [b for b in mid.theirs if b["instance_id"] not in node.their_tapped]
        fighting, tag = self.cast_pumped(able, mid.pumps)
        _damage, dead, _ = self.combat(fighting, blockers, mid.opp_life, tag=tag)
        air = any(_reach_or_flying(b) and b["_can_block"] for b in blockers)
        options = [
            ("none", ()),
            ("all", ids_of(able)),
            ("survivors", ids_of(a for a in able if a["instance_id"] not in dead)),
            ("evasive", ids_of(a for a in able if a["_unblockable"] or (_flying(a) and not air))),
        ]
        out, seen = [], set()
        for name, ids in options:
            if ids not in seen and (ids or name == "none"):
                seen.add(ids)
                out.append((name, ids))
        # (pumped: a Slickshot Strike pumped finishes a planeswalker its printed power can't)
        out += self._walker_attacks(mid, [a for a in fighting if a["instance_id"] not in dead], air)
        return out, True

    def _walker_attacks(self, mid: Mid, survivors: list[dict], air: bool) -> list[tuple[str, tuple]]:
        """For each planeswalker of theirs (the two with most loyalty), the survivors' attack with
        the fewest of them (evasive first, then the smallest) that can finish it sent at it, the
        rest at the player: ("at:<walker id>:<attacker ids>", all survivor ids)."""
        if not mid.walkers or not survivors:
            return []
        order = sorted(
            survivors,
            key=lambda a: (not (a["_unblockable"] or (_flying(a) and not air)), a["power"], a["instance_id"]),
        )
        out = []
        walkers = sorted(mid.walkers.items(), key=lambda item: (-item[1][1], item[0]))
        for iid, (_walker, loyal) in walkers[:2]:
            sent, power = [], 0
            for attacker in order:
                if power >= loyal:
                    break
                if attacker["power"] > 0:
                    sent.append(attacker["instance_id"])
                    power += attacker["power"]
            if power >= loyal:
                out.append((f"at:{iid}:{','.join(str(i) for i in sorted(sent))}", ids_of(survivors)))
        return out

    def finish(self, mid: Mid, attack: tuple[str, tuple], *, greedy: bool = False) -> Node:
        """Our attack (and casts after it), then their attack: the next node or a terminal one."""
        self.tick()
        mid = mid.copy()
        node = mid.node
        ids = attack[1]
        attackers = [b for b in mid.ours if b["instance_id"] in ids]
        opp_after = None
        # "at:<walker>:<attackers>": those attackers go at one of their planeswalkers.
        at: dict[int, int] = {}
        if attack[0].startswith("at:"):
            _at, walker, sent = attack[0].split(":", 2)
            at = {int(i): int(walker) for i in sent.split(",") if i}
        at_walkers: list[tuple[str, str]] = []
        if attackers:
            # Attack triggers that ping the opponent (Hired Claw) resolve before blocks.
            if attack[0] != "declared":
                mid.opp_life -= self.pinged(mid.ours, attackers)
            blockers = [b for b in mid.theirs if b["instance_id"] not in node.their_tapped]
            fighting, tag = attackers, ""
            if attack[0] == "declared" and self.model.step == "Step_FirstStrikeDamage":
                # Their life already shows our first-strike damage: only the regular part is left.
                fighting, tag = _regular_damage_part(attackers), "fs"
            if mid.pumps:  # this turn's cast triggers (ledger #94)
                fighting, pumped = self.cast_pumped(fighting, mid.pumps)
                tag += pumped
            walker_id = next(iter(at.values()), None)
            if walker_id in mid.walkers:
                # Their blockers protect the planeswalker first (its loyalty is the "life" they
                # defend), then the player with the blockers left over.
                name, loyal = mid.walkers[walker_id]
                sent = [b for b in fighting if b["instance_id"] in at]
                hit, w_dead_ours, w_dead_theirs, w_gained, w_healed, _t, used = self.combat_detail(
                    sent, blockers, loyal, tag=tag + "walker"
                )
                rest = [b for b in fighting if b["instance_id"] not in at]
                spare = [b for b in blockers if b["instance_id"] not in used]
                damage, dead_ours, dead_theirs, gained, healed = self.combat_life(
                    rest, spare, mid.opp_life, tag=tag
                )
                dead_ours, dead_theirs = dead_ours | w_dead_ours, dead_theirs | w_dead_theirs
                gained, healed = gained + w_gained, healed + w_healed
                at_walkers = [(b["name"], name) for b in sent]
                if loyal - hit > 0:
                    mid.walkers[walker_id] = (name, loyal - hit)
                else:
                    del mid.walkers[walker_id]
            else:
                damage, dead_ours, dead_theirs, gained, healed = self.combat_life(
                    fighting, blockers, mid.opp_life, tag=tag
                )
                swap = self._sneak_swap(mid, fighting, blockers, tag)
                if swap is not None:  # a Sneak creature in for an unblocked attacker (#156)
                    gain, returned, entered = swap
                    damage += gain
                    attackers = [a for a in attackers if a["instance_id"] != returned["instance_id"]] + [entered]
            # Lifelink on both sides lands with the combat damage: the net change decides lethal.
            mid.opp_life -= damage - healed
            mid.our_life += gained
            mid.gained += gained
            opp_after = mid.opp_life
            mid.ours = self.rescale([b for b in mid.ours if b["instance_id"] not in dead_ours])
            mid.theirs = [b for b in mid.theirs if b["instance_id"] not in dead_theirs]
            # Non-vigilance attackers stay tapped through their turn.
            mid.our_tapped |= {b["instance_id"] for b in attackers if not has_combat_keyword(b, "vigilance")}
        post_combat: tuple[str, ...] = ()
        # A creature cast before combat grows an attacker counting our creatures (Leonardo): it is
        # never "after combat" then (#157).
        if attackers and not mid.attack_first and not any(a["instance_id"] in self._scale_base for a in attackers):
            post_combat = tuple(
                self.spells[var.spell].name for var, _target in mid.casts if self._attack_neutral(var)
            )
        if not mid.applied:
            self._apply_casts(mid)
        step = {
            "k": node.k,
            "turn": node.abs_turn,
            "label": LABELS[min(node.k, len(LABELS) - 1)],
            "land": mid.land.name if mid.land is not None else "",
            "casts": tuple(mid.cast_names),
            "modes": tuple(mid.modes),
            "targets": tuple(mid.targets),
            "cycles": tuple(mid.cycles),
            "attack": tuple(b["name"] for b in sorted(attackers, key=lambda b: b["instance_id"])),
            "mana": len(mid.sources),
            "colors": _colors(mid.sources),
            "source_colors": tuple("".join(sorted(s.produces)) for s in mid.base_sources),
            "castable": self.castable(mid.sources, mid.ours),
            "opp_life_after": opp_after,
            "greedy": greedy,
            "keys": frozenset(mid.keys | {("attack", frozenset(ids)) if ids else ("noattack",)}),
            "plays": (mid.land, tuple(mid.casts), ids),
            "post_combat": post_combat,
            "at_walkers": tuple(sorted(at_walkers)),
            "walker_attack": attack[0] if at else "",
        }
        carry = {
            "our_life": mid.our_life,
            "opp_life": mid.opp_life,
            "ours": tuple(mid.ours),
            "theirs": tuple(mid.theirs),
            "hand": tuple(mid.hand),
            "lands": tuple(mid.lands),
            "played": tuple(mid.played),
            "rocks": tuple(mid.rocks),
            "walkers": tuple((iid, name, loyal) for iid, (name, loyal) in sorted(mid.walkers.items())),
        }
        if mid.opp_life <= 0:
            return dataclasses.replace(
                node, **carry, history=node.history + (Step(**step),), win_at=node.k + 1,
                win_turn=node.abs_turn, overkill=-mid.opp_life,
            )  # fmt: skip
        # Our end step (Fleeting Effigy goes back to the hand), then their turn's life gains.
        self._end_step(mid)
        carry.update(ours=tuple(mid.ours), hand=tuple(mid.hand))
        gains = self.their_gains(mid)
        hit = self.their_attack(
            life=mid.our_life,
            opp_life=mid.opp_life,
            ours=mid.ours,
            theirs=mid.theirs,
            able=[b for b in mid.theirs if b["_can_attack"]],
            blockers=[b for b in mid.ours if b["instance_id"] not in mid.our_tapped],
            hand=tuple(mid.hand),
            entered=mid.entered,
            keep_tapped=frozenset(),
            spare=(mid.sources, mid.used_mv, mid.used_pips),
            returning=tuple(mid.returning),
            index=node.attacks + 1,
            turn=node.abs_turn + 1,
            next_k=node.k + 1,
            next_abs=node.abs_turn + 2,
            landfall=True,
            gains=gains,
        )
        after = Step(
            **step,
            held=hit["held"],
            life_after=hit["life"],
            their_creatures_after=None if hit["dead"] else len(hit["theirs"]),
            policy=hit["policy"],
        )
        history, attacks = node.history + (after,), node.attacks + 1
        if hit["dead"]:
            return dataclasses.replace(
                node, **{**carry, "our_life": hit["life"], "opp_life": hit["opp_life"]}, history=history,
                attacks=attacks, dead_at=attacks, dead_turn=node.abs_turn + 1, overkill=-hit["life"],
            )  # fmt: skip
        # Their lifelink attackers (and our lifelink blockers) changed both life totals; each of
        # their planeswalkers used a plus ability (paranoid: +1 loyalty a turn), or grew more on
        # their life gain (``walker_growth``).
        walkers = tuple(
            (iid, name, loyal + self.walker_growth(iid, gains)) for iid, name, loyal in carry["walkers"]
        )
        carry.update(
            our_life=hit["life"], opp_life=hit["opp_life"], ours=tuple(self.rescale(list(hit["ours"]))),
            theirs=tuple(hit["theirs"]), hand=hit["hand"], walkers=walkers,
        )  # fmt: skip
        return dataclasses.replace(
            node,
            **carry,
            k=node.k + 1,
            abs_turn=node.abs_turn + 2,
            our_tapped=frozenset(),
            their_tapped=hit["their_tapped"],
            entered=tuple(sorted(hit["entered"].items())),
            returning=(),
            history=history,
            attacks=attacks,
        )

    def _sneak_swap(self, mid: Mid, fighting: list[dict], blockers: list[dict], tag: str):
        """A Sneak cast after their blocks (a "sneak" variant in this turn's casts, ledger #156):
        the Sneak creature enters tapped and attacking for our unblocked attacker dealing the
        least, which returns to hand. (+damage, the returned attacker, the entered body), or None
        when there is no Sneak in the set or no unblocked attacker (the card stays in hand).

        Applies the swap to ``mid`` (board, hand, cast names). The entered body's power is the
        board's (Leonardo, Big Brother: +1/+0 for each other creature we control).
        """
        sneaks = [var for var, _t in mid.casts if var.kind == "sneak" and var.spell in mid.hand]
        if not sneaks or not fighting:
            return None
        through = dict(self.combat_detail(fighting, blockers, mid.opp_life, tag=tag)[5])
        open_ = [a for a in fighting if a["power"] > 0 and through.get(a["instance_id"], 0) >= a["power"]]
        if not open_:
            return None
        returned = min(
            open_, key=lambda a: (through[a["instance_id"]], a["power"] + a["toughness"], a["instance_id"])
        )
        var = sneaks[0]
        hs = self.spells[var.spell]
        ours = self.rescale([b for b in mid.ours if b["instance_id"] != returned["instance_id"]] + [var.body])
        entered = ours[-1]
        mid.ours = ours
        mid.hand.remove(var.spell)
        mid.entered[entered["instance_id"]] = mid.node.abs_turn
        mid.our_tapped.add(entered["instance_id"])
        if self.consuls:  # it enters under their Authority of the Consuls: they gain life
            mid.opp_life += self._enters_under_them(entered, mid.our_tapped)
        mid.cast_names.append(hs.name)
        mid.modes.append((hs.name, f"Sneak after blocks, returning {returned['name']}"))
        return entered["power"] - through[returned["instance_id"]], returned, entered

    def sneak_gain(self, mid: Mid, fighting: list[dict], blockers: list[dict], tag: str) -> int:
        """What ``_sneak_swap`` would add to this attack, without applying it (greedy lethal checks)."""
        if not any(var.kind == "sneak" and var.spell in mid.hand for var, _t in mid.casts):
            return 0
        scratch = mid.copy()
        swap = self._sneak_swap(scratch, fighting, blockers, tag)
        return swap[0] if swap is not None else 0

    def _attack_neutral(self, var: Variant) -> bool:
        """A creature cast that changes nothing about our attack this turn: a summoning-sick
        body (no haste, no flash to hold) with no removal, damage, counters or hasty tokens and
        no static or attack ability for our team. Pro YFRA game 2026-10-18 (match 3 turn 7):
        attack first, then Enlightened Confidant and Valgavoth after combat."""
        if var.kind != "cast" or var.body is None or self.haste:
            return False
        if var.reach is not None or var.face or var.counter or var.life_tokens or var.named_tokens:
            return False
        found = self._neutral_memo.get(id(var))
        if found is None:
            found = self._neutral_memo[id(var)] = not (
                has_combat_keyword(var.body, "haste")
                or any(has_combat_keyword(t, "haste") for t in var.tokens)
                or _ATTACK_RELEVANT.search(_text(self.spells[var.spell].spell.card))
            )
        return found

    def castable(self, sources, ours=()) -> tuple[str, ...]:
        """Every hand spell castable alone with these sources (as ``_schedule`` lists them),
        ``ours`` being the bodies on the battlefield (a cast restriction among them applies)."""
        bans: frozenset = frozenset()
        for body in ours:
            bans |= cast_bans(body.get("_card") or body, ours=True)
        return tuple(
            hs.name
            for hs in self.spells
            if not hs.spell.has_x
            and not hs.spell.uncastable
            and not self.banned(hs, bans)
            and hs.spell.mana_value <= len(sources)
            and _pip_matching(hs.spell.pips, sources)
        )

    # -- their turn ------------------------------------------------------------------------

    def default_policies(self, able, blockers, life) -> list[tuple[str, tuple]]:
        """Everything; hold back what just dies (``_attack_round``); keep the best one or two blockers home."""
        able = sorted(able, key=lambda b: b["instance_id"])
        policies = [("all", tuple(able))]
        damage, dead, _ = self.combat(able, blockers, life)
        if dead:
            survivors = tuple(a for a in able if a["instance_id"] not in dead)
            if self.combat(survivors, blockers, life)[0] >= damage:
                policies.append(("hold-dying", survivors))
        guards = sorted(
            (a for a in able if a["_can_block"]),
            key=lambda b: (-b["toughness"], -b["power"], b["name"], b["instance_id"]),
        )
        for count in (1, 2):
            home = {g["instance_id"] for g in guards[:count]}
            if len(guards) >= count and len(able) > count:
                policies.append((f"keep-{count}", tuple(a for a in able if a["instance_id"] not in home)))
        return policies

    def pinged(self, side, attackers) -> int:
        """Damage ``side``'s attack-trigger pings (``attack_pings``) deal the defending player when
        ``attackers`` (of that side) attack: a 'self' ping needs its creature among them, a
        creature-type ping one attacker of that type, 'you attack' anyone."""
        if not attackers:
            return 0
        total = 0
        for body in side:
            pings = self._ping_memo.get(id(body))
            if pings is None:
                pings = self._ping_memo[id(body)] = attack_pings(body.get("_card") or body)
            for amount, who in pings:
                if who == "self":
                    met = any(a is body for a in attackers)
                else:
                    met = who == "any" or any(self._is_kind(a, who) for a in attackers)
                total += amount if met else 0
        return total

    @staticmethod
    def _is_kind(body: dict, kind: str) -> bool:
        card = body.get("_card") or body
        words = f"{card.get('type_line') or ''} {' '.join(str(s) for s in card.get('subtypes') or [])}"
        return kind in re.findall(r"[a-z'-]+", words.lower().replace("subtype_", ""))

    def growth(self, body: dict) -> tuple[int, int, int]:
        """``landfall_growth`` of a body, memoised."""
        found = self._growth_memo.get(id(body))
        if found is None:
            found = self._growth_memo[id(body)] = landfall_growth(body.get("_card") or body)
        return found

    def spell_growth(self, body: dict) -> int:
        """``cast_growth`` of a body, memoised."""
        found = self._spell_growth_memo.get(id(body))
        if found is None:
            found = self._spell_growth_memo[id(body)] = cast_growth(body.get("_card") or body)
        return found

    def _landfall(
        self, bodies, entries: int | None = None, extra: dict | None = None, *, spells: bool = True,
        grow: dict | None = None,
    ) -> list[dict]:  # fmt: skip
        """``bodies`` (theirs) after their land drops and their spell: each landfall creature with its
        +1/+1 counters (``entries`` landfall triggers, default ``their_drops``; ``extra`` counters by
        id: an Ordeal's), and each creature that grows when they cast a spell with one spell's worth
        (paranoid: they cast one a turn; ledger #82), or on their life gain and draws (``grow``: the
        turn's ``turn_growth``) (``_countered``: a fresh id, so the memos see a new body; it keeps
        growing every turn)."""
        entries = self.their_drops if entries is None else entries
        out = []
        for body in bodies:
            counters = (
                self.growth(body)[0] * entries
                + (self.spell_growth(body) if spells else 0)
                + (extra.get(body["instance_id"], 0) if extra else 0)
                + (grow.get(body["instance_id"], 0) if grow else 0)
            )
            out.append(self._countered(body, counters) if counters else body)
        return out

    def _pumped(self, attackers, entries: int | None = None) -> tuple[list[dict], dict]:
        """Their attackers with this turn's temporary landfall pumps (``entries`` triggers: +N/+N each,
        then power doubled each, Tifa Lockhart), and pumped id -> original id."""
        entries = self.their_drops if entries is None else entries
        out, back = [], {}
        for body in attackers:
            _n, power, toughness = self.growth(body)
            doubles = self.doubles(body)
            if not (power or toughness or doubles):
                out.append(body)
                continue
            key = (body["instance_id"], entries)
            found = self._pump_memo.get(key)
            if found is None:
                found = self._pump_memo[key] = {
                    **body,
                    "instance_id": _PUMP_IDS + len(self._pump_memo),
                    "power": (body["power"] + power * entries) * 2 ** min(doubles * entries, 10),
                    "toughness": body["toughness"] + toughness * entries,
                }
            out.append(found)
            back[found["instance_id"]] = body["instance_id"]
        return out, back

    def _next_turn_spells(self, hand: tuple, sources) -> tuple[int, tuple[int, ...]]:
        """(damage at their face, spells cast) of our next turn's spells: the hand's face damage
        ('any target' at the opponent) most first, then, with cast triggers of ours, the cheapest
        other noncreature spells, up to three, within the mana and colours of ``sources``."""
        if not hand or not sources:
            return 0, ()
        key = (hand, tuple(sorted("".join(sorted(s.produces)) for s in sources)))
        found = self._next_memo.get(key)
        if found is not None:
            return found
        options = []
        for index in hand:
            hs = self.spells[index]
            casts = [v for v in hs.variants if v.kind == "cast"]
            if not casts:
                continue
            face = max(v.face for v in casts)
            if face > 0 or (self.pumps_possible and "noncreature" in self.kinds(index)):
                options.append((-face, hs.spell.mana_value, index, hs.spell.pips, face))
        damage, cast, mana, pips = 0, [], 0, ()
        for _neg, mana_value, index, spell_pips, face in sorted(options, key=lambda o: o[:3]):
            if len(cast) == _MAX_SPELLS_PER_TURN:
                break
            if mana + mana_value > len(sources) or (
                spell_pips and not _pip_matching(pips + spell_pips, sources)
            ):
                continue
            damage, mana, pips = damage + face, mana + mana_value, pips + spell_pips
            cast.append(index)
        found = self._next_memo[key] = (damage, tuple(cast))
        return found

    def _land_entries(self, theirs, able, drops: int, fetch: bool) -> tuple[int, dict]:
        """(landfall triggers per landfall ability, {their creature id: extra +1/+1 counters}) for one
        turn of theirs: ``drops`` land drops plus, with ``fetch``, the lands their permanents fetch
        (``fetches``; an Ordeal only when its creature can attack, which also gets the attack's
        counter), each triggering once more per doubler of theirs (Traveling Chocobo)."""
        lands, extra = drops, {}
        if fetch and self.fetches:
            ready = {b["instance_id"] for b in able}
            for count, host in self.fetches:
                if host and host not in ready:
                    continue
                lands += count
                if host:
                    extra[host] = extra.get(host, 0) + 1
        if not lands:
            return 0, extra
        doublers = self.board_doublers + sum(1 for b in theirs if self._doubler(b))
        return lands * (1 + doublers), extra

    def _doubler(self, body: dict) -> bool:
        found = self._doubler_memo.get(id(body))
        if found is None:
            found = self._doubler_memo[id(body)] = (body, landfall_doubler(body.get("_card") or body))
        return found[1]

    def doubles(self, body: dict) -> int:
        """``landfall_doubles`` of a body, memoised."""
        found = self._double_memo.get(id(body))
        if found is None:
            found = self._double_memo[id(body)] = (body, landfall_doubles(body.get("_card") or body))
        return found[1]

    def _strength(self, var: Variant) -> float:
        """``_reach_strength`` memoised: re-reading the removal text on every attack of theirs
        was a sixth of a burn-heavy hand's search (2026-10-19)."""
        found = self._strength_memo.get(id(var.reach))
        if found is None:
            found = self._strength_memo[id(var.reach)] = _reach_strength(var.reach)
        return found

    def _held(self, hand, spare, life) -> tuple[list[Variant], list[Variant], int]:
        """Instants we keep up for their turn, affordable together: (removal, flash creatures, mana left)."""
        sources, used_mv, used_pips = spare
        candidates = []
        for index in hand:
            instant = [
                v for v in self.spells[index].variants if v.kind == "cast" and v.instant and v.loss < life
            ]
            killers = [v for v in instant if v.reach is not None]
            bodied = [v for v in instant if _bodies(v)]
            if killers:  # the mode that kills the most: destroy, then the most damage
                var = max(killers, key=self._strength)
                candidates.append((0, -self._strength(var), self.spells[index].name, var))
            elif bodied:  # a flash creature, or an instant that makes creature tokens
                var = max(bodied, key=lambda v: sum(self.material(b) for b in _bodies(v)))
                candidates.append(
                    (1, -sum(self.material(b) for b in _bodies(var)), self.spells[index].name, var)
                )
        removal, flash = [], []
        mv, pips = used_mv, tuple(used_pips)
        for *_rank, var in sorted(candidates, key=lambda c: c[:3]):
            if mv + var.mana_value > len(sources) or (
                var.pips and not _pip_matching(pips + var.pips, sources)
            ):
                continue
            mv, pips = mv + var.mana_value, pips + var.pips
            (removal if var.reach is not None else flash).append(var)
        return removal, flash, max(0, len(sources) - mv)

    def _boost(self, body: dict) -> dict:
        """The +2/+0 trick proxy: a copy with a negative id (one per body, kept for the memo)."""
        found = self._boosted.get(body["instance_id"])
        if found is None:
            found = self._boosted[body["instance_id"]] = {
                **body,
                "instance_id": -body["instance_id"],
                "power": body["power"] + 2,
            }
        return found

    def their_attack(
        self, *, life, opp_life, ours, theirs, able, blockers, hand, entered, keep_tapped, spare, returning,
        index, turn, next_k, next_abs, fixed=None, variant="best", crack_exclude=frozenset(), tag="",
        blocks_locked=False, landfall=False, drops=None, crack_sources=None, spells=True, gains=0, drain_now=True,
        combat_counters=True,
        after_upkeep=False,
    ) -> dict:  # fmt: skip
        """Their attack: any lethal policy kills us; else the non-suicidal one worst for us, and
        their most damaging one when every policy leaves them dead to our next turn anyway.

        Our held instants answer first: a flash creature joins our blockers,
        removal takes the biggest killable declared attacker before blocks.
        ``fixed`` attackers (already declared) leave no policy choice.
        ``variant`` (their attack under way only): our 'best' blocks, 'none',
        or 'crackback' (best blocks without the creatures our crackback needs).
        ``blocks_locked``: blocks are already being declared (their attack under
        way at declare blockers / first-strike damage), so a flash creature cast
        now can't block; it still enters for our next turn.
        ``landfall``: their turn's growth comes first (their ``drops`` land drops,
        default ``their_drops``, and the lands their permanents fetch; with ``spells``,
        their spell's growth too, and their creatures' growth on their ``gains`` life-gain
        events and their draws: ``turn_growth``).
        ``crack_sources``: our mana on our next turn (default ``spare``'s sources).
        ``drain_now``: their drain engines hit us this turn (``their_drain``; not on the
        attack already declared at the root).
        ``combat_counters``: their beginning-of-combat counters are still to come (#198; False
        at the root once their combat has begun, when the counters are on the snapshot).
        """
        self.opponent.chance(self, life)  # v1: always NOTHING
        entries = 0
        if landfall:  # their turn: their land drop(s) grow their landfall creatures first
            entries, extra = self._land_entries(
                theirs,
                able,
                self.their_drops if drops is None else drops,
                index == self.fetch_index and fixed is None,
            )
            if spells:
                grow = self.turn_growth(theirs, gains)
            elif combat_counters:
                grow = self.turn_growth(theirs, gains, combat_only=True)
            else:
                grow = None
            theirs = self._landfall(theirs, entries, extra, spells=spells, grow=grow)
            able = self._landfall(able, entries, extra, spells=spells, grow=grow)
        if self.their_anthems:  # ledger #177: their life after our attack switches them
            theirs = self.anthem_bodies(theirs, opp_life)
            able = self.anthem_bodies(able, opp_life, alive=theirs)
        drain = self.their_drain(theirs, after_upkeep=after_upkeep) if drain_now else 0
        removal, flash, left = self._held(hand, spare, life)
        flash_bodies = [b for v in flash for b in _bodies(v)]
        hand_left = tuple(i for i in hand if i not in {v.spell for v in flash})
        entered = dict(entered)
        for body in flash_bodies:
            entered[body["instance_id"]] = turn
        ours_all = list(ours) + flash_bodies
        # Under their Authority of the Consuls a flash creature enters tapped (no block) and
        # gains them life.
        tapped_in = bool(flash_bodies) and self.consuls and self.engines.enter_tapped
        if flash_bodies and self.consuls:
            opp_life += sum(self._enters_under_them(b, set()) for b in flash_bodies)
        defenders = list(blockers) + ([] if blocks_locked or tapped_in else flash_bodies)
        if variant == "none":
            defenders = []
        elif variant == "crackback":
            defenders = [b for b in defenders if b["instance_id"] not in crack_exclude]
        if fixed is not None:
            policies = [("declared", tuple(fixed))]
        else:
            policies = self.opponent.attack_policies(self, able, defenders, life)
        crack_sources = spare[0] if crack_sources is None else crack_sources
        context = (life, opp_life, ours_all, theirs, defenders, removal, entered, keep_tapped, returning, index, next_k, next_abs, tag, left, entries, crack_sources)  # fmt: skip
        seen, outcomes = set(), []
        for name, attackers in policies:
            if ids_of(attackers) not in seen:
                seen.add(ids_of(attackers))
                outcomes.append(self._policy(name, attackers, hand_left, *context, drain=drain))
        held = tuple(sorted(self.spells[v.spell].name for v in flash))
        lethal = [o for o in outcomes if o["dead"]]
        if lethal:  # paranoid: a lethal attack is never filtered out
            worst = min(lethal, key=lambda o: o["life"])
            return {**worst, "held": held + worst["used"]}
        safe = [o for o in outcomes if not o["suicidal"]]
        if not safe and fixed is None and () not in seen:
            none = self._policy("none", (), hand_left, *context, drain=drain)
            outcomes.append(none)
            safe = [] if none["suicidal"] else [none]
        if safe:
            chosen = min(enumerate(safe), key=lambda item: (item[1]["v"], item[0]))[1]
        else:
            # Dead to our next turn whatever they do, holding back included: they race with their
            # most damage (ledger #95, match 18b1601a T9: the softened attack left us "at 8").
            chosen = min(enumerate(outcomes), key=lambda item: (item[1]["life"], item[1]["v"], item[0]))[1]
        return {**chosen, "held": held + chosen["used"]}

    def _policy(
        self, name, attackers, hand, life, opp_life, ours, theirs, defenders, removal, entered, keep_tapped,
        returning, index, next_k, next_abs, tag, left=0, landfall=0, crack_sources=(), *, drain=0,
    ) -> dict:  # fmt: skip
        declared = sorted(attackers, key=lambda b: b["instance_id"])
        removed: set[int] = set()
        bounced: list[dict] = []
        used: list[str] = []
        for var in removal:
            killable = [a for a in declared if self.kills(var.reach, a, ours, attacking=True, mana=left)]
            if var.spell not in hand or not killable:
                continue
            target = max(killable, key=lambda b: (b["power"], b["toughness"], -b["instance_id"]))
            left -= self.ward(target)
            declared = [a for a in declared if a is not target]
            removed.add(target["instance_id"])
            bounced += [target] if var.bounce else []
            hand = tuple(i for i in hand if i != var.spell)
            used.append(self.spells[var.spell].name)
        fighting = declared
        boost = self.proxy and declared and (self.model.their_hand is None or self.model.their_hand >= 1)
        if boost:
            big = max(declared, key=lambda b: (b["power"], b["toughness"], -b["instance_id"]))
            fighting = [self._boost(b) if b is big else b for b in declared]
        back: dict = {}
        if landfall:  # "gets +2/+2 until end of turn" per land (``landfall`` entries): this attack only
            fighting, back = self._pumped(fighting, landfall)
        round_tag = tag + ("proxy" if boost else "")
        if annotated(fighting):
            # Their pumps, attack-trigger grants and extra combat (ledger #191, #193, #194): the
            # bodies change, so their own memo tag.
            damage, dead_att, dead_blk, gained, healed = attack_round(
                fighting, defenders, life,
                lambda a, b, left: self.combat_life(a, b, left, first=index == 1, tag=round_tag + "+atk"),
                declared=name == "declared",
            )  # fmt: skip
        else:
            damage, dead_att, dead_blk, gained, healed = self.combat_life(
                fighting, defenders, life, first=index == 1, tag=round_tag
            )
        dead_att = {abs(back.get(i, i)) for i in dead_att}
        # Lifelink lands with the damage: our lifelink blockers heal us, their lifelink
        # attackers heal them (match 41753907 T7: Dawnwing Marshal's 4 a turn). Their attack
        # triggers' pings hit us too (unless the attack was declared before the snapshot), and
        # so do their drain engines this turn (``their_drain``, ledger #133).
        after = life - damage + healed - (self.pinged(theirs, attackers) if name != "declared" else 0) - drain
        opp_life += gained
        # Their token engines' tokens of this turn (#172): sick now, they block our next attack.
        made = (
            self.their_tokens(
                [b for b in theirs if b["instance_id"] not in removed],
                next_k,
                now=index == 1 and not self.our_turn,
            )
            if self.token_engines
            else []
        )
        # Their sacrifice outlet (Goblin Bombardment): their creatures this turn are that much reach;
        # they use it when it kills (match b62b73fc).
        if after > 0 and self.outlet:
            reach = self.engines.reach(len(theirs) + len(made))
            after = after - reach if after - reach <= 0 else after
        if after <= 0:
            return {
                "dead": True,
                "life": after,
                "opp_life": opp_life,
                "policy": name,
                "used": tuple(used),
                "suicidal": False,
                "v": 0.0,
            }
        ours_after = [b for b in ours if b["instance_id"] not in dead_blk]
        theirs_after = (
            [b for b in theirs if b["instance_id"] not in dead_att | removed] + list(returning) + bounced
        )
        theirs_after += made
        alive = {b["instance_id"] for b in theirs_after}
        # Their non-vigilance attackers stay tapped through our next turn.
        their_tapped = (
            frozenset(
                {a["instance_id"] for a in declared if not has_combat_keyword(a, "vigilance")}
                | set(keep_tapped)
            )
            & alive
        )
        # Their lifelink damage gained them life: what grows on it is bigger from now on (#128).
        theirs_after, their_tapped = self._lifelink_growth(
            theirs_after, their_tapped, gained, [b for b in theirs if b["instance_id"] not in removed]
        )
        crack = [b for b in ours_after if b["_can_attack"] and not self.sick(b, next_k, next_abs, entered)]
        crack_blockers = [b for b in theirs_after if b["instance_id"] not in their_tapped]
        # Our next turn: the burn in hand at their face and the cast triggers of the spells we
        # cast then (ledger #95: a crackback without them softened a lethal attack of theirs).
        burn, cast = self._next_turn_spells(hand, crack_sources)
        fighting, crack_tag = crack, ""
        if cast and crack and self.pumps_possible:
            scratch = SimpleNamespace(ours=crack, pumps={})
            for spell in cast:
                self._cast_triggers(scratch, spell)
            fighting, crack_tag = self.cast_pumped(crack, scratch.pumps)
        crack_damage, crack_dead_ours, crack_dead_theirs, crack_gained, crack_healed = (
            self.combat_life(fighting, crack_blockers, opp_life, tag=crack_tag)
            if crack
            else (0, set(), set(), 0, 0)
        )
        crack_net = crack_damage - crack_healed + self.pinged(ours_after, crack)
        suicidal = (bool(crack) or burn > 0) and crack_net + burn >= opp_life
        # Worst for us means after our reply too: their attackers can't block on our
        # turn, so a policy is valued at the better of our holding and our crackback
        # (bug_20261007_182945: all-out 1/4 fliers every turn "took" 24 life to 5).
        v = self.value(after, opp_life, ours_after, theirs_after, hand)
        if crack:
            v = max(
                v,
                self.value(
                    after + crack_gained, opp_life - crack_net,
                    [b for b in ours_after if b["instance_id"] not in crack_dead_ours],
                    [b for b in theirs_after if b["instance_id"] not in crack_dead_theirs],
                    hand,
                ),
            )  # fmt: skip
        return {
            "dead": False,
            "life": after,
            "opp_life": opp_life,
            "policy": name,
            "used": tuple(used),
            "suicidal": suicidal,
            "v": v,
            "ours": ours_after,
            "theirs": theirs_after,
            "their_tapped": their_tapped,
            "hand": hand,
            "entered": entered,
        }

    # -- the root ---------------------------------------------------------------------------

    def root_node(self) -> Node:
        """The board before T (a forced mode from ``compare_modes`` applied)."""
        model = self.model
        ours, theirs = list(model.ours), list(model.theirs)
        entered: dict = {}
        # Our creature spells on the stack resolve before T's attack (summoning sick on T).
        entering = list(model.our_stack_bodies)
        for body in entering:
            self._register_scaling(body, None)
        effect = self.root_effect or {}
        entering += ([effect["body"]] if effect.get("body") is not None else []) + list(
            effect.get("tokens") or ()
        )
        tapped_in: set = set()
        consuls_life = 0
        for body in entering:
            ours.append(body)
            entered[body["instance_id"]] = model.turn
            if self.consuls:  # their Authority of the Consuls: enters tapped, they gain life
                consuls_life += self._enters_under_them(body, tapped_in)
        returning = []
        if effect.get("target") is not None:
            theirs = [b for b in theirs if b is not effect["target"]]
            returning += [effect["target"]] if effect.get("bounce") else []
        if effect.get("sweep"):  # a mode's damage to each of their creatures (#87)
            theirs = [b for b in theirs if not swept_now(b, int(effect["sweep"]), effect.get("source"))]
        ours = self.rescale(ours)  # creatures entering before T grow Leonardo-style bodies (#157)
        return Node(
            k=0,
            abs_turn=self.first_turn,
            our_life=model.our_life + int(effect.get("gain") or 0),
            opp_life=model.opp_life - int(effect.get("face") or 0) + consuls_life,
            ours=tuple(ours),
            theirs=tuple(theirs),
            our_tapped=frozenset({b["instance_id"] for b in model.ours if b["_tapped"]} | tapped_in)
            if self.our_turn
            else frozenset(),
            their_tapped=frozenset(b["instance_id"] for b in theirs if b["_tapped"]),
            entered=tuple(sorted(entered.items())),
            hand=self.hand0,
            lands=self.lands0,
            played=(),
            rocks=(),
            returning=tuple(returning),
            walkers=self.walkers0,
        )

    def roots(self) -> list[tuple[str, Node]]:
        """[(block variant, node at the start of T)]: their attack under way resolved first."""
        node = self.root_node()
        model = self.model
        if not model.their_attack_pending:
            return [("", node)]
        # A creature a forced mode removed before T (compare_modes) no longer attacks.
        alive = {b["instance_id"] for b in node.theirs}
        attackers = [b for b in (model.first_their_attackers or ()) if b["instance_id"] in alive]
        fixed = tuple(attackers) if model.any_theirs_attacking else None
        able = [b for b in attackers if b["_can_attack"]]
        locked = model.step in _BLOCKS_LOCKED
        first_ids = {b["instance_id"] for b in model.our_first_blockers}
        # Bodies entering now can block only while blocks are still to come (and not tapped by
        # their Authority of the Consuls).
        if not locked and not (self.consuls and self.engines.enter_tapped):
            first_ids |= dict(node.entered).keys()
        blockers = [b for b in node.ours if b["instance_id"] in first_ids]
        variants, crack = ["best"], frozenset()
        # Their attack still to come (ledger #97, match 18b1601a T10 main 1): their land drop, if they
        # have not played it and are still in a main phase, and their fetches come before it.
        before_combat = model.phase in ("Phase_Beginning", "Phase_Main1")
        after_upkeep = model.phase not in ("", "Phase_Beginning") or model.step == "Step_Draw"
        drops = 0
        if fixed is None and before_combat:
            drops = max(0, self.their_drops - model.their_lands_played)
        if fixed is not None:
            # Their attackers are declared: our block variants (advisory).
            attacking = {b["instance_id"] for b in fixed}
            open_blockers = [b for b in node.theirs if b["instance_id"] not in attacking and not b["_tapped"]]
            ready = [b for b in node.ours if b["_can_attack"]]
            _d, dead, _ = self.combat(ready, open_blockers, node.opp_life, tag="root")
            crack = frozenset(b["instance_id"] for b in ready if b["instance_id"] not in dead)
            variants += ["none", "crackback"] if crack else ["none"]
        out = []
        for variant in variants:
            self.tick()
            hit = self.their_attack(
                life=node.our_life,
                opp_life=node.opp_life,
                ours=node.ours,
                theirs=node.theirs,
                able=able,
                blockers=blockers,
                hand=node.hand,
                entered=dict(node.entered),
                keep_tapped=node.their_tapped,
                spare=(list(model.sources_now), 0, ()),
                returning=node.returning,
                index=1,
                turn=model.turn,
                next_k=0,
                next_abs=self.first_turn,
                fixed=fixed,
                variant=variant,
                crack_exclude=crack,
                tag="root",  # at a first-strike step the attackers are regular-damage copies
                blocks_locked=locked,
                # Land entries always (their Ordeal counter and fetched lands, #96/#97); their
                # beginning-of-combat counters only before combat (review 2026-10-20: gating the
                # whole landfall step on it lost a lethal Ordeal attack at Step_BeginCombat).
                landfall=fixed is None,
                combat_counters=before_combat,
                spells=False,
                drops=drops,
                crack_sources=model.sources_all,
                # Their drains this turn: still to come before their attack is declared (#133).
                drain_now=fixed is None,
                after_upkeep=fixed is None and after_upkeep,
            )
            block = variant if fixed is not None else ""
            if hit["dead"]:
                out.append(
                    (variant, dataclasses.replace(node, our_life=hit["life"], opp_life=hit["opp_life"], attacks=1,
                                                  now_life=hit["life"], block=block, dead_at=1, dead_turn=model.turn,
                                                  overkill=-hit["life"]))
                )  # fmt: skip
                continue
            out.append(
                (
                    variant,
                    dataclasses.replace(
                        node,
                        our_life=hit["life"],
                        opp_life=hit["opp_life"],
                        ours=tuple(hit["ours"]),
                        theirs=tuple(hit["theirs"]),
                        our_tapped=frozenset(),
                        their_tapped=hit["their_tapped"],
                        entered=tuple(sorted(hit["entered"].items())),
                        hand=hit["hand"],
                        returning=(),
                        attacks=1,
                        now_life=hit["life"],
                        block=block,
                    ),
                )
            )
        return out

    # -- greedy play ---------------------------------------------------------------------

    def greedy_step(self, node: Node, *, cheap: bool = False) -> Node:
        """The first land class, the best-value casts and the attack option with the best outcome.

        ``cheap`` (past the soft deadline): attack with everything only when
        that is lethal through their best blocks, else hold.
        """
        land = self.land_options(node)[0]
        _base, sources = self.sources(node, land)
        sets = self.cast_sets(
            node, sources, instant_only=node.k == 0 and self.our_turn and self.model.t_instant_only
        )
        best = max(
            enumerate(sets),
            key=lambda item: (
                round(self.choice_value(item[1], node.theirs, ours=node.ours), 6),
                sum(v.mana_value for v, _ in item[1]),
                -item[0],
            ),
        )[1]
        candidates = [best]
        # Held removal worth its floor (#147): the same casts without it (a set with no combined
        # kill) compete on the board they leave; casting it at nothing loses its held value.
        if (
            self.floored
            and not any(v.chip for v, _ in best)
            and any(self.spells[v.spell].floored for v, _ in best)
        ):
            candidates.append(tuple(c for c in best if not self.spells[c[0].spell].floored))
        children = []
        for casts in candidates:
            mid = self.begin(node, land, casts)
            options, _possible = self.attack_choices(mid)
            if cheap and len(options) > 1:
                everyone = next((o for o in options if o[0] == "all"), options[0])
                attackers = [b for b in mid.ours if b["instance_id"] in everyone[1]]
                blockers = [b for b in mid.theirs if b["instance_id"] not in node.their_tapped]
                fighting, tag = self.cast_pumped(attackers, mid.pumps)
                damage = self.combat(fighting, blockers, mid.opp_life, tag=tag)[0]
                lethal = damage + self.sneak_gain(mid, fighting, blockers, tag) >= mid.opp_life
                options = [everyone if lethal else options[0]]
            children += [self.finish(mid, option, greedy=True) for option in options]
        return max(enumerate(children), key=lambda item: (self.score(item[1]), -item[0]))[1]

    def rollout(self, node: Node, *, cheap: bool = False) -> Node:
        while not node.terminal and node.k < len(LABELS):
            node = self.greedy_step(node, cheap=cheap)
        return node

    def fill_steps(self, node: Node, k: int, count: int) -> list[Step]:
        """Deployment rows after a line ends (dead, lethal or cut short): land and casts, no combat."""
        steps = []
        node = dataclasses.replace(node, k=k, abs_turn=self.first_turn + 2 * k, history=())
        while len(steps) < count and node.k < len(LABELS):
            land = self.land_options(node)[0]
            base, sources = self.sources(node, land)
            mid = self.begin(node, land, self.cast_sets(node, sources, instant_only=False)[0])
            steps.append(
                Step(
                    k=node.k, turn=node.abs_turn, label=LABELS[node.k], land=mid.land.name if mid.land else "",
                    casts=tuple(mid.cast_names), modes=tuple(mid.modes), targets=tuple(mid.targets),
                    cycles=tuple(mid.cycles), mana=len(sources), colors=_colors(sources),
                    source_colors=tuple("".join(sorted(s.produces)) for s in base), castable=self.castable(sources, mid.ours),
                    greedy=True,
                )
            )  # fmt: skip
            node = dataclasses.replace(
                node, k=node.k + 1, abs_turn=node.abs_turn + 2, hand=tuple(mid.hand), lands=tuple(mid.lands),
                played=tuple(mid.played), rocks=tuple(mid.rocks), theirs=tuple(mid.theirs),
            )  # fmt: skip
        return steps
