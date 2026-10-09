"""The line search's transition model: our turn's moves and the opponent's reply.

``line_search`` drives the search; this module says what a turn does. Our turn:
one land drop (one option per land class), up to three spells (each spell's
variants: plain cast, one per 'Choose one —' mode, removal on each of the top
two legally targetable bodies it kills, an 'any target' spell aimed at the
opponent, landcycling for its basic type) and an attack (none / all / the ones
that survive their best blocks / the evasive ones). A cast pays its additional
mana cost and the life it costs; a set that would kill us is never cast, nor is
a spell whose "can't cast unless" condition is unmet. Their turn: everything
untaps, our held instants answer (a flash creature or token maker blocks only
while blocks are still to come), and the opponent attacks with the policy worst
for us (``ParanoidOpponent``). Mana, colours, summoning sickness and tapped
creatures follow the rules the board assessment uses; all combat is
``board_assessment._combat``. Pure: no I/O, no LLM, nothing logged.

What a cast does: a creature's body; creature tokens a spell (or a mode, or a
permanent's enters trigger) makes when their count, power/toughness and
keywords are printed ("Create two 3/1 red Elemental creature tokens with
haste."); removal, bounce, life gain, damage to the opponent, and a mana rock,
from the spell's text or a permanent's own enters trigger (not a fight; not
one with a condition or a reflexive "when you do", ``applied_text``; not a
trigger on another permanent entering; nothing under Hushbringer).
``unmodelled_effect`` names what a card does beyond that (an aura, a pump, a
copy token, an attack trigger, an X spell, ...): a line is no evidence about
such a cast, and a cast that does nothing else the search values is never
made in a line.
"""

from __future__ import annotations

import dataclasses
import re
import time
from dataclasses import dataclass, field
from itertools import combinations, product
from types import SimpleNamespace
from typing import Any, Protocol

from arenamcp.board_assessment import (
    _LIFEGAIN,
    _MAX_BLOCK_OPTIONS,
    _MAX_SPELLS_PER_TURN,
    _OR_ANOTHER,
    _QUOTED,
    _body,
    _cast_text,
    _combat,
    _enters_tapped,
    _flying,
    _int,
    _is_creature,
    _kills,
    _name,
    _reach_or_flying,
    _Spell,
    _spell_value,
    _text,
    _types,
    card_role,
    extra_life_cost,
    face_damage,
    harms_players,
    life_loss,
    own_subject,
    removal_reach,
    ward_cost,
)
from arenamcp.board_model import BoardModel, _regular_damage_part, cast_bans, spell_banned
from arenamcp.combat_keywords import has_combat_keyword
from arenamcp.mulligan_policy import _land_colors, _pip_matching, hand_card

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
    keys: frozenset = frozenset()
    plays: tuple = field(
        default=(), repr=False, compare=False
    )  # (land, ((variant, target), ...), attack ids)

    @property
    def sig(self) -> tuple:
        """What was played: two steps with the same sig are the same choice (cast order ignored)."""
        return (
            self.turn, self.land, tuple(sorted(self.casts)), tuple(sorted(self.modes)),
            tuple(sorted(self.targets)), tuple(sorted(self.cycles)), self.attack,
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
        text = " + ".join(parts)
        if self.attack:
            text += f"{', ' if text else ''}attack with {', '.join(self.attack)}"
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

    @property
    def terminal(self) -> bool:
        return self.dead_at is not None or self.win_at is not None


class Mid:
    """Our turn in progress: a mutable scratch copy of a node."""

    __slots__ = (
        "node", "land", "casts", "sources", "base_sources", "used_mv", "used_pips", "our_life", "opp_life",
        "ours", "theirs", "our_tapped", "entered", "hand", "lands", "played", "rocks", "returning",
        "attack_first", "applied", "modes", "targets", "cycles", "cast_names", "keys",
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

    Kinds: removal, bounce, lifegain, tokens (creature tokens ``token_specs``
    reads) and other.
    """
    pseudo = {"name": name, "oracle_text": bullet, "type_line": ""}
    match = _LIFEGAIN.search(_text(pseudo))
    gain = int(match.group(1)) if match else 0
    reach = removal_reach(pseudo)
    if reach is not None:
        return ("bounce" if reach[0] == "bounce" else "removal"), gain, pseudo, reach[0] == "bounce"
    if gain:
        return "lifegain", gain, None, False
    return ("tokens" if token_specs(_cast_text(pseudo)) else "other"), 0, None, False


def token_specs(text: str) -> list[tuple[int, int, int, tuple[str, ...], str]] | None:
    """The creature tokens ``text`` (lower-case cast text) makes: [(count, power, toughness, keywords, name)].

    [] when it makes none; None when it makes creature tokens that can't be
    read: a variable count ("for each", "equal to", X), copies, tokens that
    enter tapped or attacking, a condition ("if", "unless", "instead") before
    the "create", or tokens exiled or sacrificed at the next end step.
    "They gain haste until end of turn" after it gives them haste.
    """
    specs: list[tuple[int, int, int, tuple[str, ...], str]] = []
    for line in (text or "").splitlines():
        for create in re.finditer(r"\bcreate\b", line):
            start = line.rfind(".", 0, create.start()) + 1
            end = line.find(".", create.end())
            clause = line[create.start() : end if end >= 0 else len(line)]
            if not re.search(r"\btokens?\b", clause) or not _CREATES_CREATURES.search(clause):
                continue  # Food, Treasure, Clue...: no creature
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
    return specs


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
        skip = trigger is not None and (
            not enters_triggers or bool(_EFFECT_CONDITION.search(trigger["effect"]))
        )
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
        if card_flow(effect):
            continue
        subject, rest = match.group("subject").strip(), match.group("rest")
        if (
            match.group("event") in ("enters", "enter")
            and own_subject(subject, name)
            and not _OR_ANOTHER.search(subject)
            and not re.search(r"\b(?:attacks|dies|blocks|leaves)\b", rest)
            and not _EFFECT_CONDITION.search(effect)
            and _etb_modelled(name, effect)
        ):
            continue
        return True
    return False


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
    unconditional enters trigger of those kinds (a fight aside). Card flow
    (draw, scry, discard) and counterspells change nothing it values.
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
        if token_specs(_cast_text(card)) is None:
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
            if role == "planeswalker" or (role == "other" and not card_flow(_cast_text(card))):
                return "its effect"  # (a discard spell is card flow)
        if result is not None and harms_players(card):
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
        var.body is not None or var.tokens or var.reach is not None or var.gain or var.face or var.rock
    )


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
        # Hushbringer / Torpor Orb on the battlefield: a creature we cast triggers nothing on entering.
        self.enters_triggers = not any(_NO_ENTERS_TRIGGERS.search(_text(c)) for c in model.battlefield)
        self._prepare()

    # -- precomputation ------------------------------------------------------------------

    def _prepare(self) -> None:
        model = self.model
        theirs = list(model.theirs)
        # Copies of one name in hand share a key; a commander (zone "command") keeps its own.
        lowest: dict[tuple[str, str], int] = {}
        for spell in model.spells:
            iid = _int(spell.card.get("instance_id")) or 0
            lowest[(spell.name, spell.zone)] = min(lowest.get((spell.name, spell.zone), iid), iid)
        self.spells: list[HandSpell] = []
        for index, original in enumerate(model.spells):
            spell = dataclasses.replace(original)
            value = _spell_value(spell, survival=self.survival, theirs=theirs, ours=list(model.ours))
            iid = _int(spell.card.get("instance_id")) or 0
            hs = HandSpell(index, spell, iid, lowest[(spell.name, spell.zone)], spell.name, max(1.0, value))
            hs.variants, hs.plain = self._variants(hs)
            self.spells.append(hs)
        self.hand0 = tuple(
            sorted(range(len(self.spells)), key=lambda i: (self.spells[i].name, self.spells[i].iid))
        )
        lands = []
        for card in model.hand_lands:
            colors = frozenset(_land_colors(card))
            source = SimpleNamespace(produces=colors or frozenset({"C"}), name=_name(card))
            lands.append(
                Land(_int(card.get("instance_id")) or 0, _name(card), colors, _enters_tapped(card), source)
            )
        self.lands0 = tuple(sorted(lands, key=lambda land: (land.tapped, land.name, land.iid)))

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
        base_specs = token_specs(outside) or []

        def make(
            mode=None, text="", gain=0, reach=None, bounce=False, rock=False, loss=0, face=0, aim="", specs=()
        ):
            return Variant(
                hs.index, "cast", mode, text, body, gain, reach, bounce, rock, spell.mana_value, spell.pips,
                bool(info.instant_speed), loss, face, aim, tokens=self._tokens([*base_specs, *specs]),
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
            match = _LIFEGAIN.search(_text(etb)) if spell.role == "lifegain" else None
            alt, each = face_damage(etb)
        plain = make(
            reach=etb_reach,
            bounce=bounce,
            gain=int(match.group(1)) if match else 0,
            rock=spell.role == "ramp",
            loss=life_loss(etb),
            face=each,
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
                effect = (
                    kind,
                    gain,
                    reach is not None and removal_reach(reach),
                    loss,
                    mode_alt,
                    mode_each,
                    specs,
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
        return tuple(variants), plain

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
        attackers = sorted(attackers, key=lambda b: b["instance_id"])
        blockers = sorted(blockers, key=lambda b: b["instance_id"])
        able = sum(1 for b in blockers if b["_can_block"])
        if first and attackers and able and (len(attackers) + 1) ** able > _MAX_BLOCK_OPTIONS:
            self.exact_first = False  # the solver falls back to greedy blocks
        key = (tag, ids_of(attackers), ids_of(blockers), max(1, life))
        found = self._memo.get(key)
        if found is None:
            self.combats += 1
            self.work += 2 + len(attackers) + able
            damage, dead_att, dead_blk = _combat(attackers, blockers, life)
            found = self._memo[key] = (damage, frozenset(dead_att), frozenset(dead_blk))
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
        """m(b) = power + toughness / 2, +1 if it flies or can't be blocked."""
        found = self._m_memo.get(id(body))
        if found is None:
            evasive = _flying(body) or body["_unblockable"]
            found = self._m_memo[id(body)] = (
                body["power"] + 0.5 * body["toughness"] + (1.0 if evasive else 0.0)
            )
        return found

    def value(self, life, opp_life, ours, theirs, hand) -> float:
        """V = u(life) - 0.6 opp life + our material - theirs + 0.3 hand (hand-tuned)."""
        u = life + min(life, 10) + 2 * min(life, 5)
        return (
            u
            - 0.6 * opp_life
            + sum(self.material(b) for b in ours)
            - sum(self.material(b) for b in theirs)
            + 0.3 * sum(self.spells[i].hand_value for i in hand)
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
            self.value(node.our_life, node.opp_life, node.ours, node.theirs + node.returning, node.hand),
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
            (b for b in theirs if self.kills(reach, b, ours, mana=mana)),
            key=lambda b: (-b["power"], -b["toughness"], b["name"], b["instance_id"]),
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
        elif var.reach is None and not var.gain and not var.tokens:
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

    def choice_value(self, choice, theirs) -> float:
        return sum(self.item_value(v, t, theirs) for v, t in choice)

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
        key = (
            node.hand,
            tuple(sorted("".join(sorted(s.produces)) for s in sources)),
            ids_of(theirs),
            their_tapped,
            instant_only,
            max((b["power"] for b in node.ours), default=-1),
            node.our_life,
            bans,
        )
        found = self._cast_memo.get(key)
        if found is not None:
            return found
        options: list[tuple[int, list]] = []
        for position, index in enumerate(node.hand):
            items = []
            banned = self.banned(self.spells[index], bans)
            for var in self.spells[index].variants:
                if (instant_only and not var.instant) or var.mana_value > len(sources):
                    continue
                if (banned and var.kind == "cast") or var.loss >= node.our_life:
                    continue
                if var.reach is not None:  # no killable target: hold it
                    spare = len(sources) - var.mana_value
                    aims = self.targets(var.reach, theirs, their_tapped, node.ours, spare)
                    items.extend((var, t) for t in aims)
                    if not aims and var.body is not None:
                        items.append((var, None))  # a creature whose enters trigger finds no target
                else:
                    items.append((var, None))
            if items:
                options.append((position, items))
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
                    hit = [t["instance_id"] for _, t in choice if t is not None]
                    total = sum(v.mana_value + (self.ward(t) if t is not None else 0) for v, t in choice)
                    if len(hit) != len(set(hit)) or total > len(sources):
                        continue
                    if sum(v.loss for v, _ in choice) >= node.our_life:
                        continue
                    pips = tuple(p for v, _ in choice for p in v.pips)
                    if pips and not _pip_matching(pips, sources):
                        continue
                    scored.append((choice, self.choice_value(choice, theirs), total))
        scored.sort(key=lambda item: (-round(item[1], 6), -item[2], self.choice_text(item[0])))
        result = self._cast_memo[key] = [item[0] for item in scored]
        return result

    def play_keys(self, land, casts) -> set:
        keys: set = set()
        if land is not None:
            keys.add(("land", land.key))
        for var, _target in casts:
            hs = self.spells[var.spell]
            if var.kind == "cycle":
                keys.add(("cycle", hs.key_iid))
            else:
                keys.update({("cast", hs.key_iid, None), ("cast", hs.key_iid, var.mode)})
        if not casts:
            keys.add(("nocast",))
        return keys

    # -- our turn --------------------------------------------------------------------------

    def begin(self, node: Node, land: Land | None, casts: tuple) -> Mid:
        """Our turn ``node.k`` with this land and these casts (cast now unless we attack first)."""
        mid = Mid.__new__(Mid)
        mid.node, mid.land, mid.casts = node, land, casts
        mid.base_sources, mid.sources = self.sources(node, land)
        mid.used_mv = sum(v.mana_value + (self.ward(t) if t is not None else 0) for v, t in casts)
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
        mid.keys = self.play_keys(land, casts)
        if not mid.attack_first:
            self._apply_casts(mid)
        return mid

    def _apply_casts(self, mid: Mid) -> None:
        mid.applied = True
        for var, target in mid.casts:
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
            if var.mode_text:
                mid.modes.append((hs.name, var.mode_text))
            if var.aim:
                mid.targets.append((hs.name, var.aim))
            mid.our_life -= var.loss
            mid.opp_life -= var.face
            for body in ([var.body] if var.body is not None else []) + list(var.tokens):
                mid.ours.append(body)
                mid.entered[body["instance_id"]] = mid.node.abs_turn
            if target is not None and any(b is target for b in mid.theirs):
                mid.theirs = [b for b in mid.theirs if b is not target]
                mid.targets.append((hs.name, target["name"]))
                if var.bounce:
                    mid.returning.append(target)
            mid.our_life += var.gain
            if var.rock:
                mid.rocks.append(SimpleNamespace(produces=frozenset("WUBRGC"), name=hs.name))

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
        _damage, dead, _ = self.combat(able, blockers, mid.opp_life)
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
        return out, True

    def finish(self, mid: Mid, attack: tuple[str, tuple], *, greedy: bool = False) -> Node:
        """Our attack (and casts after it), then their attack: the next node or a terminal one."""
        self.tick()
        mid = mid.copy()
        node = mid.node
        ids = attack[1]
        attackers = [b for b in mid.ours if b["instance_id"] in ids]
        opp_after = None
        if attackers:
            blockers = [b for b in mid.theirs if b["instance_id"] not in node.their_tapped]
            fighting, tag = attackers, ""
            if attack[0] == "declared" and self.model.step == "Step_FirstStrikeDamage":
                # Their life already shows our first-strike damage: only the regular part is left.
                fighting, tag = _regular_damage_part(attackers), "fs"
            damage, dead_ours, dead_theirs = self.combat(fighting, blockers, mid.opp_life, tag=tag)
            mid.opp_life -= damage
            opp_after = mid.opp_life
            mid.ours = [b for b in mid.ours if b["instance_id"] not in dead_ours]
            mid.theirs = [b for b in mid.theirs if b["instance_id"] not in dead_theirs]
            # Non-vigilance attackers stay tapped through their turn.
            mid.our_tapped |= {b["instance_id"] for b in attackers if not has_combat_keyword(b, "vigilance")}
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
        }
        if mid.opp_life <= 0:
            return dataclasses.replace(
                node, **carry, history=node.history + (Step(**step),), win_at=node.k + 1,
                win_turn=node.abs_turn, overkill=-mid.opp_life,
            )  # fmt: skip
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
                node, **{**carry, "our_life": hit["life"]}, history=history, attacks=attacks,
                dead_at=attacks, dead_turn=node.abs_turn + 1, overkill=-hit["life"],
            )  # fmt: skip
        carry.update(
            our_life=hit["life"], ours=tuple(hit["ours"]), theirs=tuple(hit["theirs"]), hand=hit["hand"]
        )
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
                var = max(killers, key=lambda v: _reach_strength(v.reach))
                candidates.append((0, -_reach_strength(var.reach), self.spells[index].name, var))
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
        blocks_locked=False,
    ) -> dict:  # fmt: skip
        """Their attack: any lethal policy kills us; else the non-suicidal one worst for us.

        Our held instants answer first: a flash creature joins our blockers,
        removal takes the biggest killable declared attacker before blocks.
        ``fixed`` attackers (already declared) leave no policy choice.
        ``variant`` (their attack under way only): our 'best' blocks, 'none',
        or 'crackback' (best blocks without the creatures our crackback needs).
        ``blocks_locked``: blocks are already being declared (their attack under
        way at declare blockers / first-strike damage), so a flash creature cast
        now can't block; it still enters for our next turn.
        """
        self.opponent.chance(self, life)  # v1: always NOTHING
        removal, flash, left = self._held(hand, spare, life)
        flash_bodies = [b for v in flash for b in _bodies(v)]
        hand_left = tuple(i for i in hand if i not in {v.spell for v in flash})
        entered = dict(entered)
        for body in flash_bodies:
            entered[body["instance_id"]] = turn
        ours_all = list(ours) + flash_bodies
        defenders = list(blockers) + ([] if blocks_locked else flash_bodies)
        if variant == "none":
            defenders = []
        elif variant == "crackback":
            defenders = [b for b in defenders if b["instance_id"] not in crack_exclude]
        if fixed is not None:
            policies = [("declared", tuple(fixed))]
        else:
            policies = self.opponent.attack_policies(self, able, defenders, life)
        context = (life, opp_life, ours_all, theirs, defenders, removal, entered, keep_tapped, returning, index, next_k, next_abs, tag, left)  # fmt: skip
        seen, outcomes = set(), []
        for name, attackers in policies:
            if ids_of(attackers) not in seen:
                seen.add(ids_of(attackers))
                outcomes.append(self._policy(name, attackers, hand_left, *context))
        held = tuple(sorted(self.spells[v.spell].name for v in flash))
        lethal = [o for o in outcomes if o["dead"]]
        if lethal:  # paranoid: a lethal attack is never filtered out
            worst = min(lethal, key=lambda o: o["life"])
            return {**worst, "held": held + worst["used"]}
        safe = [o for o in outcomes if not o["suicidal"]]
        if not safe and fixed is None and () not in seen:
            none = self._policy("none", (), hand_left, *context)
            outcomes.append(none)
            safe = [] if none["suicidal"] else [none]
        chosen = min(enumerate(safe or outcomes), key=lambda item: (item[1]["v"], item[0]))[1]
        return {**chosen, "held": held + chosen["used"]}

    def _policy(
        self, name, attackers, hand, life, opp_life, ours, theirs, defenders, removal, entered, keep_tapped,
        returning, index, next_k, next_abs, tag, left=0,
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
        damage, dead_att, dead_blk = self.combat(
            fighting, defenders, life, first=index == 1, tag=tag + ("proxy" if boost else "")
        )
        dead_att = {abs(i) for i in dead_att}
        after = life - damage
        if after <= 0:
            return {
                "dead": True,
                "life": after,
                "policy": name,
                "used": tuple(used),
                "suicidal": False,
                "v": 0.0,
            }
        ours_after = [b for b in ours if b["instance_id"] not in dead_blk]
        theirs_after = (
            [b for b in theirs if b["instance_id"] not in dead_att | removed] + list(returning) + bounced
        )
        alive = {b["instance_id"] for b in theirs_after}
        # Their non-vigilance attackers stay tapped through our next turn.
        their_tapped = (
            frozenset(
                {a["instance_id"] for a in declared if not has_combat_keyword(a, "vigilance")}
                | set(keep_tapped)
            )
            & alive
        )
        crack = [b for b in ours_after if b["_can_attack"] and not self.sick(b, next_k, next_abs, entered)]
        crack_blockers = [b for b in theirs_after if b["instance_id"] not in their_tapped]
        crack_damage, crack_dead_ours, crack_dead_theirs = (
            self.combat(crack, crack_blockers, opp_life) if crack else (0, set(), set())
        )
        suicidal = bool(crack) and crack_damage >= opp_life
        # Worst for us means after our reply too: their attackers can't block on our
        # turn, so a policy is valued at the better of our holding and our crackback
        # (bug_20261007_182945: all-out 1/4 fliers every turn "took" 24 life to 5).
        v = self.value(after, opp_life, ours_after, theirs_after, hand)
        if crack:
            v = max(
                v,
                self.value(
                    after, opp_life - crack_damage,
                    [b for b in ours_after if b["instance_id"] not in crack_dead_ours],
                    [b for b in theirs_after if b["instance_id"] not in crack_dead_theirs],
                    hand,
                ),
            )  # fmt: skip
        return {
            "dead": False,
            "life": after,
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
        for body in model.our_stack_bodies:
            ours.append(body)
            entered[body["instance_id"]] = model.turn
        effect = self.root_effect or {}
        for body in ([effect["body"]] if effect.get("body") is not None else []) + list(
            effect.get("tokens") or ()
        ):
            ours.append(body)
            entered[body["instance_id"]] = model.turn
        returning = []
        if effect.get("target") is not None:
            theirs = [b for b in theirs if b is not effect["target"]]
            returning += [effect["target"]] if effect.get("bounce") else []
        return Node(
            k=0,
            abs_turn=self.first_turn,
            our_life=model.our_life + int(effect.get("gain") or 0),
            opp_life=model.opp_life - int(effect.get("face") or 0),
            ours=tuple(ours),
            theirs=tuple(theirs),
            our_tapped=frozenset(b["instance_id"] for b in model.ours if b["_tapped"])
            if self.our_turn
            else frozenset(),
            their_tapped=frozenset(b["instance_id"] for b in theirs if b["_tapped"]),
            entered=tuple(sorted(entered.items())),
            hand=self.hand0,
            lands=self.lands0,
            played=(),
            rocks=(),
            returning=tuple(returning),
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
        if not locked:  # bodies entering now can block only while blocks are still to come
            first_ids |= dict(node.entered).keys()
        blockers = [b for b in node.ours if b["instance_id"] in first_ids]
        variants, crack = ["best"], frozenset()
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
            )
            block = variant if fixed is not None else ""
            if hit["dead"]:
                out.append(
                    (variant, dataclasses.replace(node, our_life=hit["life"], attacks=1, now_life=hit["life"],
                                                  block=block, dead_at=1, dead_turn=model.turn,
                                                  overkill=-hit["life"]))
                )  # fmt: skip
                continue
            out.append(
                (
                    variant,
                    dataclasses.replace(
                        node,
                        our_life=hit["life"],
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
                round(self.choice_value(item[1], node.theirs), 6),
                sum(v.mana_value for v, _ in item[1]),
                -item[0],
            ),
        )[1]
        mid = self.begin(node, land, best)
        options, _possible = self.attack_choices(mid)
        if cheap and len(options) > 1:
            everyone = next((o for o in options if o[0] == "all"), options[0])
            attackers = [b for b in mid.ours if b["instance_id"] in everyone[1]]
            blockers = [b for b in mid.theirs if b["instance_id"] not in node.their_tapped]
            lethal = self.combat(attackers, blockers, mid.opp_life)[0] >= mid.opp_life
            options = [everyone if lethal else options[0]]
        children = [self.finish(mid, option, greedy=True) for option in options]
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
