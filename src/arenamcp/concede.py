"""Deterministic loss confidence and the cancellable auto-concede countdown.

When the board says we are dead no matter what, the coach recommends
conceding (once per game). With autoplay on it also offers a countdown the
user can cancel, then concedes the current game through the bridge
(``GREBridge.concede``: the client's own ``GreInterface.ConcedeGame``).

``estimate_loss`` is board math on top of ``board_assessment.assess``: no LLM,
no I/O. It returns a confidence >= 0.95 only when all of these hold:

* the opponent's next attack kills us through our best blocks, even after our
  best castable plays, by a clear margin (damage through >= our life + 2) or
  with evasion we cannot block;
* that attack comes before our next draw: their turn before combat damage, or
  our own turn once our combat is over (or when even an unblocked all-in attack
  cannot kill them), and we have no lethal of our own;
* we have no possible out: no card or activated ability we can use with our
  untapped mana that could prevent damage, gain life, remove or tap an
  attacker, or add a blocker; nothing we cast or activated this turn whose
  lingering effect (a resolved fog, "doesn't untap") the board cannot show.
  Anything unclear counts as an out;
* nothing is unknown or in flight: empty stack, no half-finished decision,
  known power/toughness, and their attackers are untapped or will untap.

Every unknown is resolved in our favour first (``_best_case_view``): an
attacker aimed at a planeswalker deals us nothing, a creature that may be
summoning sick stays home, an "until end of turn" pump on our turn wears off,
an aura we can't place sits on their best attacker. The verdict only stands
if we still die in that best case.

bug_20261006_180436 (FRA, T22, opponent's Main1): 3 life, a 3/3 flying Thopter
and 17 damage through our best blocks, one Island in hand. The coach played
on; this module recommends conceding there.
"""

from __future__ import annotations

import itertools
import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from arenamcp.board_assessment import (
    _body,
    _controller,
    _enters_tapped,
    _flying,
    _int,
    _is_creature,
    _is_land,
    _mana_source,
    _name,
    _reach_or_flying,
    _seats,
    _side_rules,
    _text,
    _types,
    assess,
    card_role,
)
from arenamcp.combat_keywords import has_combat_keyword
from arenamcp.mulligan_policy import _land_colors, _mana_value, _pip_matching, _pips, _symbols, hand_card

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 0.95
DEFAULT_COUNTDOWN_S = 10
# Confidence when every condition holds. Below 1.0: the opponent's hand and
# our next draw are never certain, but none of them can arrive in time.
DEAD_CONFIDENCE = 0.97
THRESHOLD_RANGE = (0.90, 0.99)
COUNTDOWN_RANGE = (3, 60)
# Countdowns per game: a flickering estimate must not re-announce forever.
MAX_ARMS_PER_GAME = 3
# How long to watch for the game end after a concede before warning.
CONFIRM_S = 10.0
# The verdict must hold on this many observations, at least MIN_GAP_S apart,
# before the coach recommends or offers anything: one odd snapshot (a half
# applied diff, the first-strike damage step) is never enough.
CONFIRM_OBSERVATIONS = 2
MIN_GAP_S = 0.5
# Caps for "maybe": below the lowest allowed threshold (0.90).
_MAYBE = 0.85
_LIKELY_OUT = 0.6

# Steps after which a combat's damage has been dealt.
_COMBAT_DONE_STEPS = ("Step_CombatDamage", "Step_EndCombat")
# Decisions that leave the board as it is (priority, combat declarations).
_BENIGN_DECISIONS = {"", "action required", "priority", "declare blockers", "declare attackers"}

# Roles that never stop an attack, unless their text says more (below).
_HARMLESS_ROLES = {"land", "draw", "selection", "ramp", "cycling"}
# Card draw and selection: harmless themselves, but they dig for an answer.
_DIG_ROLES = {"draw", "selection", "cycling"}
# Words that could make a card or ability an out. Anything matching is
# treated as an out; only text with none of them is harmless.
_OUT_HINT = re.compile(
    r"prevent|gain|lifelink|destroy|exile|\btaps?\b|untap|can't|cannot|return|damage|sacrific|fight"
    r"|create|flash|phase|indestructible|hexproof|protection|block|counter|loses?\b|becomes?|copy"
    r"|switch|skip|remove|token|reach|flying|gets?\b|power|toughness|attack|regenerate|fog|life"
    r"|stun|shuffle|owner|choose|each|you may|cast"
)
# "Counter target spell": stops nothing already on the battlefield.
_PURE_COUNTER = re.compile(r"counter target (?:[a-z-]+ )*?spell\b(?![^.\n]*abilit)[^.\n]*\.?")
# "cost: effect" lines; costs mention mana, a loyalty change or a cost verb.
_ABILITY = re.compile(r"^\s*(?P<cost>[^:\n\"“”]{1,90}?)\s*:\s*(?P<effect>.+)$")
_COSTISH = re.compile(
    r"\{|^[+−–-]?(?:\d+|x)$|\b(?:sacrifice|discard|pay|exile|remove|tap|untap|return|reveal|mill"
    r"|collect evidence|forage|exert)\b|^channel|^crew"
)
_LOYALTY_COST = re.compile(r"^[+−–-]?(?:\d+|x)$")
_TAP_COST = re.compile(r"\{o?t\}")
# Abilities that only work while the card is in hand or in the graveyard.
_HAND_COST = re.compile(r"discard (?:this card|~)|^channel\b")
_GRAVE_COST = re.compile(r"(?:exile|return) (?:this card|~) from your graveyard")
_MANA_EFFECT = re.compile(r"^add\b")
# Keyword abilities used from hand: cycling (and its variants), reinforce.
_HAND_KEYWORD = re.compile(
    r"(?m)^\s*(?P<keyword>[a-z]*cycling|reinforce \d+)\s*[—–-]?\s*(?P<cost>(?:\{[^}]*\})+|\d+)"
)
_CYCLE_TRIGGER = re.compile(r"when(?:ever)? you cycle (?:~|this card|it)[^,]*,\s*(?P<effect>[^\n]+)")
_ALT_COST = re.compile(
    r"\b(?:convoke|delve|improvise|affinity for|emerge|evoke|madness|miracle|surge|spectacle|prowl)\b"
    r"|costs? \{?[^.]*\}? less|costs? less|without paying (?:its|their) mana cost|rather than pay"
)
_FLASH_GRANT = re.compile(
    r"as though (?:it|they) had flash|spells? (?:you cast )?(?:have|has) flash"
    r"|you may cast (?:[a-z-]+ )*spells? (?:as though|any time)"
)
_GRAVE_CAST = re.compile(
    r"\b(?:flashback|jump-start|retrace|escape|disturb|aftermath)\b|cast (?:this card|~) from your graveyard"
)
_EXILE_CAST = re.compile(r"\b(?:foretell|foretold|plot|plotted)\b|adventure")
_SORCERY_KEYWORDS = re.compile(r"(?m)^\s*(?:equip|reconfigure|fortify|outlast|level up)\b")
_PROTECTIVE = re.compile(
    r"\bprevent\b|can't lose the game|can't attack you|can't attack unless|attacking you unless"
    r"|damage (?:can't|that would) be dealt to you|can't be dealt"
)
# Permanents that stay tapped: Claustrophobia, Blossombind, exert.
_NO_UNTAP = re.compile(r"doesn't untap|don't untap|can't become untapped|won't untap|\bexert")
# Auras and equipment that take a creature out of the attack.
_NEUTRALIZES = re.compile(
    r"(?:enchanted|equipped) creature (?:can't attack|doesn't untap|can't become untapped|loses all abilities"
    r"|phases out|has base power)"
)
# Effects that outlive the spell or ability that made them: a resolved fog,
# "tap target creature, it doesn't untap", "can't attack this turn".
_LINGERING = re.compile(
    r"prevent|can't|cannot|\btaps?\b|untap|skip|phase|\bfog\b|redirect|loses? all abilities|base power"
    r"|gain control|exchange control|switch"
)
# Of those, the ones that last into the next turn.
_NEXT_TURN = re.compile(
    r"next untap step|until your next turn|next turn|don't untap|doesn't untap|won't untap"
    r"|can't become untapped|next combat"
)
# Ways our attack deals more than its creatures' power.
_HIDDEN_REACH = re.compile(
    r"whenever [^.\n]*\battacks?\b|battle cry|\bexalted\b|each opponent|deals? double|that much damage plus"
    r"|damage plus|deals? twice"
)
_LANDFALL = re.compile(r"landfall|whenever (?:a|one or more) lands? (?:you control )?enters?")
_MULTI_BLOCK = re.compile(
    r"can block any number of creatures|can block an additional|can block (?:two|three|four|\d+) "
    r"(?:additional )?creatures"
)
_WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
# A mana ability whose amount we cannot count ("add {G} for each..."): assume plenty.
_UNKNOWN_MANA = 4
_FACE_SPLIT = re.compile(r"\n\s*(?:---|//)\s*\n")
_EMBLEM = re.compile(r"\bemblem\b")


@dataclass
class LossEstimate:
    """How sure we are that the next opposing attack kills us, and why."""

    confidence: float
    reason: str
    facts: dict[str, Any] = field(default_factory=dict)
    # What held the confidence down (outs, unknowns); empty when it is high.
    blockers: list[str] = field(default_factory=list)

    def as_payload(self) -> dict[str, Any]:
        return {
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "blockers": list(self.blockers),
            "facts": dict(self.facts),
        }


def _none(reason: str, **facts: Any) -> LossEstimate:
    return LossEstimate(0.0, reason, facts)


# --- state helpers -------------------------------------------------------------


def _prefixed(value: Any, prefix: str) -> str:
    text = str(value or "").strip()
    bare = text[len(prefix) :] if text.startswith(prefix) else text
    if not bare or bare == "None":
        return ""
    return prefix + bare


def normalize_phases(state: dict) -> dict:
    """A copy with log-style phase/step names ("Phase_Main1", "Step_DeclareAttack").

    The bridge reports ``CurrentPhase.ToString()`` ("Main1", step "None"), but
    ``board_assessment`` only recognises the log's names; with bridge names it
    never sees an attack as pending.
    """
    turn = state.get("turn")
    if not isinstance(turn, dict):
        return state
    phase = _prefixed(turn.get("phase"), "Phase_")
    step = _prefixed(turn.get("step"), "Step_")
    if phase == str(turn.get("phase") or "") and step == str(turn.get("step") or ""):
        return state
    copy = dict(state)
    copy["turn"] = {**turn, "phase": phase, "step": step}
    return copy


def _known_life(state: dict, seat: int | None) -> int | None:
    for player in state.get("players") or []:
        if isinstance(player, dict) and player.get("seat_id") == seat:
            return _int(player.get("life_total"))
    return None


def _player_of(state: dict, seat: int | None) -> dict:
    return next(
        (p for p in state.get("players") or [] if isinstance(p, dict) and p.get("seat_id") == seat), {}
    )


def game_not_live(state: Any) -> str:
    """Why this snapshot is not a game in progress ("" when it is)."""
    if not isinstance(state, dict) or not state:
        return "no game state"
    if not state.get("match_id"):
        return "no match"
    turn = state.get("turn") if isinstance(state.get("turn"), dict) else {}
    if (_int(turn.get("turn_number")) or 0) <= 0:
        return "no turn yet"
    pending = str(state.get("pending_decision") or "").strip().lower()
    if pending == "intermission" or pending.startswith("mulligan"):
        return f"not in play ({pending})"
    bridge_request = str(state.get("_bridge_request_type") or "")
    if bridge_request.startswith(("Intermission", "Mulligan")):
        return f"not in play ({bridge_request})"
    if state.get("_bridge_in_intermission") or state.get("match_ended"):
        return "match ended"
    stage = str(turn.get("stage") or "").removeprefix("GameStage_")
    if stage and stage != "Play":
        return f"game stage is {stage}"
    for player in state.get("players") or []:
        if not isinstance(player, dict):
            continue
        status = str(player.get("status") or "")
        if status and "InGame" not in status:
            return f"player status is {status}"
        life = _int(player.get("life_total"))
        if life is not None and life <= 0:
            return "a player is at 0 life"
    return ""


def _owner(card: dict) -> int | None:
    return _int(card.get("owner_seat_id")) or _controller(card)


# --- mana ------------------------------------------------------------------------


def _mana_amount(card: dict) -> int:
    """Mana one activation makes: Sol Ring 2, Lotus Field 3, a basic land 1."""
    best = 1
    for clause in re.findall(r"\badd\b([^.\n]*)", _text(card)):
        if re.search(r"\bx\b|for each|equal to|amount of", clause):
            best = max(best, _UNKNOWN_MANA)
            continue
        word = re.search(r"\b(one|two|three|four|five) (?:additional )?mana\b", clause)
        if word:
            best = max(best, _WORD_NUMBERS[word.group(1)])
            continue
        symbols = _symbols(clause.upper().replace("O", "o"))
        if " or " in clause or "," in clause:
            continue  # one of several colours
        best = max(best, len(symbols))
    return best


def _sources_of(card: dict, turn: int) -> list:
    """One entry per mana the permanent makes when tapped (none if it makes none)."""
    source = _mana_source(card, turn)
    return [source] * _mana_amount(card) if source is not None else []


def _phyrexian(cost: str) -> int:
    return sum(1 for symbol in _symbols(cost) if "P" in symbol.split("/"))


def _mana_needed(cost: str, life: int | None) -> int:
    """Mana to pay ``cost``; each Phyrexian pip may be 2 life instead (never the last 2)."""
    by_life = min(_phyrexian(cost), max(0, ((life or 0) - 1) // 2)) if life is not None else 0
    return max(0, _mana_value(cost) - by_life)


def _affordable(cost: str, sources: list, life: int | None = None) -> bool:
    """The mana in ``cost`` can be paid from distinct untapped ``sources``."""
    return _mana_needed(cost, life) <= len(sources) and _pip_matching(_pips(cost), sources)


# --- outs ------------------------------------------------------------------------


def _abilities(text: str) -> list[tuple[str, str]]:
    """(cost, effect) for each activated-ability line of lower-cased rules text."""
    found: list[tuple[str, str]] = []
    for line in text.split("\n"):
        match = _ABILITY.match(line.strip())
        if not match:
            continue
        cost, effect = match.group("cost").strip(" ,"), match.group("effect").strip()
        if not _COSTISH.search(cost):
            continue
        if (cost, effect) not in found:
            found.append((cost, effect))
    return found


def _hand_abilities(card: dict, text: str) -> list[tuple[str, str]]:
    """(cost, effect) of abilities used from hand: "discard this card" lines, cycling, reinforce."""
    found = [(cost, effect) for cost, effect in _abilities(text) if _HAND_COST.search(cost)]
    for match in _HAND_KEYWORD.finditer(text):
        keyword, cost = match.group("keyword"), match.group("cost")
        if not cost.startswith("{"):
            cost = "{" + cost + "}"
        if keyword.startswith("reinforce"):
            effect = "put +1/+1 counters on target creature"
        elif keyword == "cycling":
            trigger = _CYCLE_TRIGGER.search(text)
            effect = "draw a card. " + (trigger.group("effect") if trigger else "")
        else:  # landcycling and friends find a land
            effect = "search for a land card"
        found.append((cost, effect.strip()))
    return found


def _harmless_text(text: str) -> bool:
    return not _OUT_HINT.search(text)


def _harmless_card(card: dict, text: str) -> bool:
    """A card that cannot stop the attack: pure card draw, selection, ramp or a spell counter."""
    if not text.strip():
        return False  # unknown text is never harmless
    role = card_role(card)
    if role in _HARMLESS_ROLES:
        return _harmless_text(text)
    if role == "counter":
        return _harmless_text(_PURE_COUNTER.sub(" ", text))
    return False


def _faces(card: dict) -> list[dict]:
    """The castable faces of a card: an adventure, a modal DFC or a split card has two."""
    costs = [part.strip() for part in str(card.get("mana_cost") or "").split("//")]
    types = [part.strip() for part in str(card.get("type_line") or "").split("//")]
    count = max(len(costs), len(types))
    if count < 2:
        return [card]
    texts = _FACE_SPLIT.split(str(card.get("oracle_text") or ""))
    faces = []
    for index in range(count):
        face = dict(card)
        face["mana_cost"] = costs[index] if index < len(costs) else ""
        face["type_line"] = types[index] if index < len(types) else str(card.get("type_line") or "")
        face["card_types"] = []  # the type line speaks for this face alone
        if len(texts) == count:
            face["oracle_text"] = texts[index]
        faces.append(face)
    return faces


def _mana_pool_sources(player: dict) -> tuple[list, bool]:
    """Floating mana as wildcard sources; (sources, understood)."""
    pool = player.get("mana_pool")
    if not pool:
        return [], True
    wildcard = SimpleNamespace(produces=frozenset("WUBRGC"), name="floating mana")
    if isinstance(pool, dict):
        total = 0
        for value in pool.values():
            amount = _int(value)
            if amount is None:
                return [wildcard] * 2, False
            total += max(0, amount)
        return [wildcard] * total, True
    if isinstance(pool, list):
        return [wildcard] * len(pool), True
    return [wildcard], False


@dataclass
class _Outs:
    outs: list[str] = field(default_factory=list)
    caps: list[tuple[float, str]] = field(default_factory=list)
    # (name, spare mana after casting) for castable draw/selection.
    digs: list[tuple[str, int]] = field(default_factory=list)


def _castable_faces(
    card: dict, *, flash_granted: bool, sorcery_window: bool, sources: list, life: int | None
) -> tuple[str | None, list[tuple[str, int]]]:
    """('instant speed' | 'castable now', or None) for the card's best face, plus digs."""
    digs: list[tuple[str, int]] = []
    faces = _faces(card)
    for face in faces:
        if _is_land(face) and not _is_creature(face):
            continue
        if not _types(face).strip():
            return "unknown card", digs
        cost = str(face.get("mana_cost") or "")
        if not cost.strip() and len(faces) > 1:
            continue  # a transformed back face is never cast from hand
        info = hand_card(face)
        instant = info.instant_speed or flash_granted
        if not (instant or sorcery_window):
            continue
        text = _text(face)
        free = not cost.strip() or bool(_ALT_COST.search(text))
        if not free and not _affordable(cost, sources, life):
            continue
        if _harmless_card(face, text):
            if card_role(face) in _DIG_ROLES:
                spare = len(sources) - (0 if free else _mana_needed(cost, life))
                digs.append((_name(card), spare))
            continue
        return ("instant speed" if instant else "castable now"), digs
    return None, digs


def _scan_outs(
    state: dict,
    *,
    local: int,
    turn: int,
    our_turn: bool,
    sorcery_window: bool,
    battlefield: list[dict],
    ours: list[dict],
    our_life: int | None,
) -> _Outs:
    """Possible outs (names), digs, and confidence caps for things we cannot rule out."""
    found = _Outs()
    outs, caps = found.outs, found.caps
    ours_permanents = [c for c in battlefield if _controller(c) == local]
    sources = [s for c in ours_permanents if not c.get("is_tapped") for s in _sources_of(c, turn)]
    player = _player_of(state, local)
    floating, understood = _mana_pool_sources(player)
    sources += floating
    if not understood:
        caps.append((_MAYBE, "floating mana in an unknown shape"))
    flash_granted = any(_FLASH_GRANT.search(_text(c)) for c in ours_permanents)

    hand = state.get("hand")
    if not isinstance(hand, list):
        caps.append((_LIKELY_OUT, "our hand is unknown"))
        hand = []
    # Our main phase with the land drop unused: a land from hand is one more
    # mana, and it triggers landfall.
    lands_played = _int(player.get("lands_played"))
    land_drop = sorcery_window and our_turn and not lands_played
    playable_lands = [
        c
        for c in hand
        if isinstance(c, dict)
        and _is_land(c)
        and not _is_creature(c)
        and "//" not in str(c.get("type_line") or "")
    ]
    if land_drop and playable_lands:
        untapped = [c for c in playable_lands if not _enters_tapped(c)]
        if untapped:
            best = max(untapped, key=lambda c: len(_land_colors(c)))
            sources.append(SimpleNamespace(produces=frozenset(_land_colors(best) or {"C"}), name=_name(best)))
        landfall = next(
            (
                c
                for c in ours_permanents
                if _LANDFALL.search(_text(c)) and not _harmless_text(_LANDFALL.split(_text(c), 1)[-1])
            ),
            None,
        )
        if landfall is not None:
            outs.append(f"play a land ({_name(landfall)} landfall)")

    # Hand: spells we can cast now, and abilities that work from hand.
    for card in hand:
        if not isinstance(card, dict):
            caps.append((_LIKELY_OUT, "a card in hand is unknown"))
            continue
        name, text = _name(card), _text(card)
        for cost, effect in _hand_abilities(card, text):
            if not _affordable(cost, sources, our_life):
                continue
            if _harmless_text(effect):
                if "draw" in effect:
                    found.digs.append((name, len(sources) - _mana_needed(cost, our_life)))
                continue
            outs.append(f"{name} (from hand: {cost})")
        how, digs = _castable_faces(
            card, flash_granted=flash_granted, sorcery_window=sorcery_window, sources=sources, life=our_life
        )
        found.digs.extend(digs)
        if how:
            outs.append(f"{name} ({how})")

    # Command zone: a commander we can cast (tax unknown: the base cost counts).
    for card in state.get("command") or []:
        if not isinstance(card, dict) or _owner(card) != local:
            continue
        if _EMBLEM.search(_types(card)) or "emblem" in str(card.get("object_kind") or "").lower():
            continue
        how, _ = _castable_faces(
            card, flash_granted=flash_granted, sorcery_window=sorcery_window, sources=sources, life=our_life
        )
        if how:
            outs.append(f"{_name(card)} (command zone, {how})")

    # Battlefield: activated abilities, crew, sorcery-speed keywords, statics.
    for card in ours_permanents:
        name, text, types = _name(card), _text(card), _types(card)
        tapped = bool(card.get("is_tapped"))
        token = "token" in types or "token" in str(card.get("object_kind") or "").lower()
        if not text.strip() and not token and not _is_land(card) and not _is_creature(card):
            caps.append((_MAYBE, f"{name} has unknown rules text"))
        for cost, effect in _abilities(text):
            if _HAND_COST.search(cost) or _GRAVE_COST.search(cost) or _MANA_EFFECT.match(effect):
                continue  # works only from hand/graveyard, or makes mana
            if _TAP_COST.search(cost) and tapped:
                continue
            loyalty = bool(_LOYALTY_COST.match(cost))
            if (loyalty or "activate only as a sorcery" in effect) and not sorcery_window:
                continue
            if "activate only during your turn" in effect and not our_turn:
                continue
            if _harmless_text(effect) or not _affordable(cost, sources, our_life):
                continue
            outs.append(f"{name} (ability: {cost})")
        crew = re.search(r"\bcrew (\d+)", text)
        if crew and not tapped:
            need = int(crew.group(1))
            power = sum(b["power"] for b in ours if not b["_tapped"] and b["_card"] is not card)
            if power >= need:
                outs.append(f"{name} (crew {need})")
        if sorcery_window and _SORCERY_KEYWORDS.search(text):
            outs.append(f"{name} (equip/level ability)")
        protective = _PROTECTIVE.search(text)
        if protective:
            caps.append((_LIKELY_OUT, f"{name}: '{protective.group(0)}'"))
        if _is_creature(card) and has_combat_keyword(card, "lifelink"):
            caps.append((0.8, f"{name} has lifelink"))

    # Graveyard and exile: cards we may still cast or activate.
    for card in state.get("graveyard") or []:
        if not isinstance(card, dict) or _owner(card) != local:
            continue
        name, text = _name(card), _text(card)
        if _GRAVE_CAST.search(text):
            instant = hand_card(card).instant_speed or flash_granted
            if (instant or sorcery_window) and not _harmless_card(card, text):
                outs.append(f"{name} (from graveyard)")
        for cost, effect in _abilities(text):
            if not _GRAVE_COST.search(cost) or _harmless_text(effect):
                continue
            if "activate only as a sorcery" in effect and not sorcery_window:
                continue
            if _affordable(cost, sources, our_life):
                outs.append(f"{name} (graveyard ability: {cost})")
    for card in state.get("exile") or []:
        if not isinstance(card, dict) or _owner(card) != local:
            continue
        name, text = _name(card), _text(card)
        if _EXILE_CAST.search(text) or "adventure" in _types(card):
            instant = hand_card(card).instant_speed or flash_granted
            if (instant or sorcery_window) and not _harmless_card(card, text):
                outs.append(f"{name} (from exile)")
    return found


def _recent_effects(state: dict, *, local: int, turn: int) -> list[tuple[float, str]]:
    """Caps for what we cast or activated recently that the board cannot show.

    A resolved fog leaves only a card in our graveyard; Frost Breath leaves two
    tapped creatures that look like they untap. ``action_history`` (the log's
    UserActionTaken records) says what we did; the card's text says whether
    its effect may still be running: anything from this turn, and "next untap
    step" / "until your next turn" effects from the turn before.
    """
    history = state.get("action_history")
    if not isinstance(history, list):
        return [(_MAYBE, "what we did this turn is unknown")]
    caps: list[tuple[float, str]] = []
    entries = [e for e in history if isinstance(e, dict)]
    if len(entries) >= 50 and (_int(entries[0].get("turn")) or 0) >= turn - 1:
        caps.append((_MAYBE, "earlier actions this turn fell out of the action log"))
    texts: dict[str, list[str]] = {}
    for zone in ("graveyard", "exile", "battlefield", "stack", "hand", "command"):
        for card in state.get(zone) or []:
            if isinstance(card, dict) and _owner(card) == local:
                texts.setdefault(_name(card), []).append(_text(card))
    for entry in entries:
        if _int(entry.get("seat")) != local or str(entry.get("action") or "") not in ("Cast", "Activate"):
            continue
        when = _int(entry.get("turn")) or 0
        if when not in (turn, turn - 1):
            continue
        name = str(entry.get("card") or "") or "a card"
        known = texts.get(name)
        if not known or not any(t.strip() for t in known):
            if when == turn:
                caps.append((_LIKELY_OUT, f"we used {name} this turn and its effect is unknown"))
            continue
        pattern = _LINGERING if when == turn else _NEXT_TURN
        match = next((m for t in known if (m := pattern.search(t))), None)
        if match:
            ago = "this turn" if when == turn else "last turn"
            caps.append((_LIKELY_OUT, f"{name} ({ago}) may still protect us: '{match.group(0)}'"))
    return caps


# --- best case for us ----------------------------------------------------------------


def _counter_delta(card: dict) -> int:
    delta = 0
    counters = card.get("counters") or {}
    if isinstance(counters, dict):
        for kind, count in counters.items():
            key = str(kind).lower().replace("countertype_", "")
            amount = _int(count) or 0
            if key in ("p1p1", "+1/+1"):
                delta += amount
            elif key in ("m1m1", "-1/-1"):
                delta -= amount
    return delta


def _printed_now(card: dict) -> tuple[int, int] | None:
    """Printed power/toughness plus counters, when the printed values are known."""
    power, toughness = _int(card.get("printed_power")), _int(card.get("printed_toughness"))
    if power is None or toughness is None:
        return None
    delta = _counter_delta(card)
    return power + delta, toughness + delta


def _danger(card: dict) -> tuple[int, int]:
    evasive = has_combat_keyword(card, "flying") or bool(card.get("cant_be_blocked"))
    return (1 if evasive else 0, _int(card.get("power")) or 0)


def _best_case_view(
    state: dict, *, local: int, opponent: int, our_turn: bool, turn: int
) -> tuple[dict, list[tuple[float, str]], list[str]]:
    """The state with every unknown resolved in our favour, plus caps and notes."""
    caps: list[tuple[float, str]] = []
    notes: list[str] = []
    battlefield = [dict(c) if isinstance(c, dict) else c for c in state.get("battlefield") or []]
    cards = [c for c in battlefield if isinstance(c, dict)]
    theirs = [c for c in cards if _controller(c) == opponent and _is_creature(c)]
    removed: set[int] = set()

    # Attackers aimed at a planeswalker or a battle deal us nothing.
    attackers = [c for c in theirs if c.get("is_attacking")]
    if attackers and not our_turn:
        other_targets = [
            c
            for c in cards
            if ("planeswalker" in _types(c) and _controller(c) == local) or "battle" in _types(c)
        ]
        for card in attackers:
            target = _int(card.get("attack_target_id"))
            if not target:
                if other_targets:
                    caps.append(
                        (_LIKELY_OUT, f"{_name(card)} may be attacking {_name(other_targets[0])}, not us")
                    )
            elif target != local:
                removed.add(_int(card.get("instance_id")) or id(card))
                notes.append(f"{_name(card)} attacks something other than us")

    # Their turn before attacks: anything that may be summoning sick stays home.
    if attackers == [] and not our_turn:
        for card in theirs:
            entered = _int(card.get("turn_entered_battlefield"))
            owner = _int(card.get("owner_seat_id"))
            stolen = owner is not None and owner != _controller(card)
            unsure = entered is None or entered < 0
            if (card.get("summoning_sickness") or stolen or unsure) and entered != turn:
                card["turn_entered_battlefield"] = turn
                why = (
                    "is summoning sick"
                    if card.get("summoning_sickness")
                    else "changed control"
                    if stolen
                    else "may have just arrived"
                )
                notes.append(f"{_name(card)} {why}; assuming it can't attack")

    # Our turn: "until end of turn" changes are gone by their attack.
    if our_turn:
        for card in cards:
            if not _is_creature(card):
                continue
            power, toughness = _int(card.get("power")), _int(card.get("toughness"))
            if power is None or toughness is None:
                continue
            mine = _controller(card) == local
            base = _printed_now(card)
            if base is None:
                if not mine and (
                    card.get("modified_power") is not None or card.get("modified_toughness") is not None
                ):
                    caps.append((_MAYBE, f"{_name(card)} may be pumped only until end of turn"))
                continue
            if mine:
                new = (max(power, base[0]), max(toughness, base[1]))
            else:
                new = (min(power, base[0]), min(toughness, base[1]))
            if new != (power, toughness):
                card["power"], card["toughness"] = new
                notes.append(f"{_name(card)} counted as {new[0]}/{new[1]} once this turn ends")

    # An aura or equipment we can't place sits on their most dangerous creature.
    neutralized = {
        _int(c.get("attached_to_id"))
        for c in cards
        if _int(c.get("attached_to_id")) and _NEUTRALIZES.search(_text(c))
    }
    for card in cards:
        types = _types(card)
        if not ("aura" in types or "equipment" in types) or not _NEUTRALIZES.search(_text(card)):
            continue
        if _int(card.get("attached_to_id")) or _int(card.get("parent_instance_id")):
            continue
        candidates = [c for c in theirs if (_int(c.get("instance_id")) or id(c)) not in removed | neutralized]
        if not candidates:
            continue
        target = max(candidates, key=_danger)
        target_id = _int(target.get("instance_id")) or id(target)
        card["attached_to_id"] = target_id
        neutralized.add(target_id)
        notes.append(f"assuming {_name(card)} is on {_name(target)}")

    if removed:
        battlefield = [
            c
            for c in battlefield
            if not (isinstance(c, dict) and (_int(c.get("instance_id")) or id(c)) in removed)
        ]
    view = dict(state)
    view["battlefield"] = battlefield
    return view, caps, notes


# --- the estimate --------------------------------------------------------------------


def estimate_loss(state: Any) -> LossEstimate:
    """Confidence that the opponent's next attack kills us whatever we do (see module doc)."""
    try:
        return _estimate(state)
    except Exception as error:  # never let the concede check break the coaching loop
        logger.debug("loss estimate failed: %s", error, exc_info=True)
        return _none(f"estimate failed: {error}")


def _estimate(state: Any) -> LossEstimate:
    not_live = game_not_live(state)
    if not_live:
        return _none(not_live)
    controlled = state.get("controlled_turn")
    if isinstance(controlled, dict) and any(controlled.values()):
        return _none("a player's turn is being controlled")
    local, opponent = _seats(state)
    if local is None or opponent is None:
        return _none("seats unknown")
    our_life, opp_life = _known_life(state, local), _known_life(state, opponent)
    if our_life is None or opp_life is None:
        return _none("life totals unknown")

    normalized = normalize_phases(state)
    turn_info = normalized.get("turn") or {}
    turn = _int(turn_info.get("turn_number")) or 0
    phase, step = str(turn_info.get("phase") or ""), str(turn_info.get("step") or "")
    our_turn = _int(turn_info.get("active_player")) == local
    view, caps, notes = _best_case_view(
        normalized, local=local, opponent=opponent, our_turn=our_turn, turn=turn
    )
    assessment = assess(view)
    if assessment is None:
        return _none("board assessment unavailable")

    facts: dict[str, Any] = {
        "turn": turn,
        "phase": phase,
        "step": step,
        "our_turn": our_turn,
        "our_life": our_life,
        "opp_life": opp_life,
        "their_clock": assessment.their_clock,
        "dead_in": assessment.dead_in,
        "opp_lethal_on_board": assessment.opp_lethal_on_board,
    }
    if notes:
        facts["assumed"] = notes

    battlefield = [c for c in view.get("battlefield") or [] if isinstance(c, dict)]
    attached: dict[int, list[dict]] = {}
    for card in battlefield:
        target = _int(card.get("attached_to_id"))
        if target:
            attached.setdefault(target, []).append(card)
    our_rules, their_rules = _side_rules(battlefield, local), _side_rules(battlefield, opponent)
    ours: list[dict] = []
    theirs: list[dict] = []
    for card in battlefield:
        if not _is_creature(card):
            continue
        mine = _controller(card) == local
        body = _body(card, turn, our_rules if mine else their_rules, attached=attached)
        if body is None:
            if mine:
                caps.append((_MAYBE, f"{_name(card)} has unknown power/toughness"))
            continue
        (ours if mine else theirs).append(body)

    # --- when does their next attack come, and can we win first? ---------------
    combat_done = phase in ("Phase_Main2", "Phase_Ending") or (
        phase == "Phase_Combat" and step in _COMBAT_DONE_STEPS
    )
    sorcery_window = our_turn and phase in ("Phase_Main1", "Phase_Main2")
    if not phase:
        caps.append((0.5, "phase unknown"))
    if our_turn:
        if phase == "Phase_Beginning":
            caps.append((_LIKELY_OUT, "our draw step may still find an out"))
        if not combat_done:
            if assessment.lethal_now:
                return _none("we have lethal on board", **facts)
            attacking = [b for b in ours if b["_attacking"]]
            able = attacking or [b for b in ours if not b["_tapped"] and not b["_sick"] and b["_can_attack"]]
            power = sum(b["power"] * (2 if has_combat_keyword(b, "double strike") else 1) for b in able)
            if not attacking:
                power += _castable_haste_power(normalized, local, turn, battlefield)
            facts["our_unblocked_power"] = power
            if power >= opp_life:
                return _none("an unblocked all-in attack would kill them first", **facts)
            reach = next(
                (c for c in battlefield if _controller(c) == local and _HIDDEN_REACH.search(_text(c))), None
            )
            if reach is not None:
                found = _HIDDEN_REACH.search(_text(reach))
                caps.append(
                    (_MAYBE, f"our attack may deal more than its power ({_name(reach)}: '{found.group(0)}')")
                )
        when = "their next attack"
        life_after = assessment.lookahead[0].life_after if assessment.lookahead else None
    else:
        if combat_done:
            caps.append((0.5, "our turn (untap and draw) comes before their next attack"))
        if step == "Step_FirstStrikeDamage":
            caps.append((0.5, "first-strike damage was just dealt; the rest of this combat is unclear"))
        when = "this attack"
        life_after = assessment.our_life_now_attack

    if not (assessment.opp_lethal_on_board and assessment.dead_in == 1):
        return _none("not dead next attack", **facts)
    timing_known = life_after is not None
    if life_after is None:
        caps.append((0.5, "their attack timing is unclear"))
        life_after = 0  # a bare kill; the cap keeps this below any threshold
    through = our_life - life_after

    # Who can attack in that combat, and what flies over or past our blockers.
    if our_turn:
        their_able = [b for b in theirs if b["_can_attack"]]
        stuck = [b for b in their_able if b["_tapped"] and _stays_tapped(b["_card"], attached)]
        if stuck:
            caps.append((_LIKELY_OUT, f"{_name(stuck[0]['_card'])} may not untap"))
        our_blockers = [b for b in ours if not b["_tapped"] and b["_can_block"]]
    else:
        attacking = [b for b in theirs if b["_attacking"]]
        their_able = attacking or [
            b for b in theirs if not b["_tapped"] and not b["_sick"] and b["_can_attack"]
        ]
        our_blockers = [b for b in ours if not b["_tapped"] and b["_can_block"]]
    multi = next((b for b in our_blockers if _MULTI_BLOCK.search(_text(b["_card"]))), None)
    if multi is not None and len(their_able) > 1:
        caps.append((_MAYBE, f"{multi['name']} can block more than one attacker"))
    air_cover = any(_reach_or_flying(b) for b in our_blockers)
    evasive = [b for b in their_able if b["_unblockable"] or (_flying(b) and not air_cover)]
    evasive_power = sum(b["power"] for b in evasive)
    facts.update(
        through=through,
        life_after=life_after,
        evasive_power=evasive_power,
        evasive=[f"{b['name']} {b['power']}/{b['toughness']}" for b in evasive],
    )
    clear_margin = life_after <= -2 or evasive_power >= our_life
    if not clear_margin:
        caps.append((_MAYBE, f"thin margin: {through} through vs {our_life} life"))

    # --- outs and unknowns ------------------------------------------------------
    scan = _scan_outs(
        view,
        local=local,
        turn=turn,
        our_turn=our_turn,
        sorcery_window=sorcery_window,
        battlefield=battlefield,
        ours=ours,
        our_life=our_life,
    )
    caps.extend(scan.caps)
    outs = scan.outs
    if outs:
        caps.append((_LIKELY_OUT, "possible out: " + ", ".join(outs[:3])))
    dig = next((name for name, spare in scan.digs if spare >= 1), None)
    if dig:
        caps.append((_MAYBE, f"{dig} could dig for an answer with mana to spare"))
    caps.extend(_recent_effects(view, local=local, turn=turn))
    facts["outs"] = outs
    if view.get("stack"):
        caps.append((0.7, "something is on the stack"))
    pending = str(view.get("pending_decision") or "").strip()
    if pending.lower() not in _BENIGN_DECISIONS:
        caps.append((0.7, f"a decision is in progress ({pending})"))

    confidence = min([DEAD_CONFIDENCE] + [cap for cap, _ in caps])
    blockers = [why for cap, why in sorted(caps, key=lambda item: item[0])]
    if confidence >= DEFAULT_THRESHOLD:
        reason = _concede_reason(evasive, through, our_life, when)
    else:
        numbers = f" ({through} through vs {our_life} life)" if timing_known else ""
        reason = f"Dead next attack{numbers}, but " + blockers[0]
    return LossEstimate(confidence, reason, facts, blockers)


def _stays_tapped(card: dict, attached: dict[int, list[dict]]) -> bool:
    counters = card.get("counters") or {}
    if isinstance(counters, dict) and any("stun" in str(key).lower() for key in counters):
        return True
    if _NO_UNTAP.search(_text(card)):
        return True
    return any(
        _NO_UNTAP.search(_text(aura)) for aura in attached.get(_int(card.get("instance_id")) or -1, [])
    )


def _castable_haste_power(state: dict, local: int, turn: int, battlefield: list[dict]) -> int:
    """Power of haste creatures in hand we can cast now (they join an all-in attack)."""
    sources = [
        s
        for c in battlefield
        if _controller(c) == local and not c.get("is_tapped")
        for s in _sources_of(c, turn)
    ]
    total = 0
    for card in state.get("hand") or []:
        if isinstance(card, dict) and _is_creature(card) and has_combat_keyword(card, "haste"):
            cost = str(card.get("mana_cost") or "").split("//")[0]
            if _affordable(cost, sources):
                total += max(0, _int(card.get("power")) or 0)
    return total


def _concede_reason(evasive: list[dict], through: int, our_life: int, when: str) -> str:
    if evasive:
        names = ", ".join(
            f"{b['power']}/{b['toughness']} {'flying' if _flying(b) and not b['_unblockable'] else 'unblockable'} "
            f"{b['name']}"
            for b in evasive[:2]
        )
        more = through - sum(b["power"] for b in evasive)
        if more > 0:
            head = f"their {names} and {more} more damage get through"
        else:
            head = f"their {names} {'gets' if len(evasive) == 1 else 'get'} through"
    else:
        head = f"{through} damage gets through your best blocks"
    return f"Concede: {head} {when} vs your {our_life} life, and nothing you can cast stops it."


def offer_text(seconds: int) -> str:
    """The spoken auto-concede offer; the countdown starts once it has been heard."""
    return f"Auto-concede in {seconds} seconds — say cancel or press Cancel to keep playing."


# --- the countdown -----------------------------------------------------------------------


@dataclass
class _Armed:
    token: int
    game_key: Any
    match_id: Any
    turn: int
    local: int | None
    seconds: int
    estimate: LossEstimate
    ended_at_arm: bool
    # What to show and say before the countdown starts (reason + offer).
    text: str = ""
    record_state: dict | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    started: bool = False
    claimed: bool = False


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _remember(store: OrderedDict, key: Any, value: Any = True, limit: int = 32) -> None:
    store[key] = value
    store.move_to_end(key)
    while len(store) > limit:
        store.popitem(last=False)


def _default_start(target: Callable[[_Armed], None], armed: _Armed) -> None:
    threading.Thread(target=target, args=(armed,), daemon=True, name="concede-countdown").start()


def _default_wait(event: threading.Event, seconds: float) -> bool:
    return event.wait(seconds)


class ConcedeController:
    """Recommends conceding once per game and runs the cancellable auto-concede.

    Thread model: ``observe`` runs on the coaching-loop thread; ``cancel`` may
    run on any thread; each countdown runs on its own daemon thread. All state
    lives under one lock, and nothing calls the UI, speech or the bridge while
    holding it. Each countdown has its own Event, and a countdown concedes only
    after claiming itself under the lock, so a cancel either wins (no concede)
    or reports "too late"; a game is never conceded twice. A second lock
    (``_emit_lock``) is held across every state change and the event that
    reports it, so the desktop always sees a countdown's events in order.

    A countdown first speaks its offer (``announce`` blocks until it has been
    heard) and only then starts counting: "offering" -> "armed" ->
    "conceding" -> "sent" -> "conceded" (or "cancelled" / "aborted" /
    "failed" / "unconfirmed").
    """

    def __init__(
        self,
        *,
        get_state: Callable[[], dict],
        concede_fn: Callable[[dict], Any],
        advise: Callable[..., None],
        emit: Callable[[dict], None],
        settings_get: Callable[..., Any],
        autopilot_on: Callable[[], bool],
        bridge_ready: Callable[[], bool],
        game_over: Callable[[], bool] = lambda: False,
        draft_active: Callable[[], bool] = lambda: False,
        announce: Callable[[str, dict | None, Callable[[], bool]], bool] | None = None,
        start_thread: Callable[[Callable[[_Armed], None], _Armed], None] = _default_start,
        wait: Callable[[threading.Event, float], bool] = _default_wait,
        confirm_s: float = CONFIRM_S,
        clock: Callable[[], float] = time.monotonic,
        confirm_observations: int = CONFIRM_OBSERVATIONS,
        min_gap_s: float = MIN_GAP_S,
    ) -> None:
        self._get_state = get_state
        self._concede_fn = concede_fn
        self._advise = advise
        self._emit = emit
        self._settings_get = settings_get
        self._autopilot_on = autopilot_on
        self._bridge_ready = bridge_ready
        self._game_over = game_over
        self._draft_active = draft_active
        self._announce_fn = announce
        self._start_thread = start_thread
        self._wait = wait
        self._confirm_s = confirm_s
        self._clock = clock
        self._confirm_observations = max(1, int(confirm_observations))
        self._min_gap_s = min_gap_s
        self._lock = threading.Lock()
        self._emit_lock = threading.RLock()
        self._tokens = itertools.count(1)
        self._armed: _Armed | None = None
        self._recommended: OrderedDict = OrderedDict()
        self._declined: OrderedDict = OrderedDict()
        self._conceded: OrderedDict = OrderedDict()
        self._arms: OrderedDict = OrderedDict()
        self._last_logged: tuple | None = None
        self._last_estimate: LossEstimate | None = None
        # (game_key, consecutive high observations, time of the last counted one)
        self._streak: tuple[Any, int, float] | None = None
        # The bridge answered "Unknown action: concede"; recommend only until
        # it goes away and comes back (an updated plugin).
        self._bridge_unsupported = False
        # (game_key, match_id, turn) of the latest observation, for reloads.
        self._current: tuple[Any, Any, int] | None = None
        self._resume: dict | None = None

    # -- settings (read each time, so changes apply live) ---------------------

    def threshold(self) -> float:
        return _clamp(
            self._settings_get("concede_threshold", DEFAULT_THRESHOLD), *THRESHOLD_RANGE, DEFAULT_THRESHOLD
        )

    def countdown_s(self) -> int:
        return int(
            _clamp(
                self._settings_get("concede_countdown_s", DEFAULT_COUNTDOWN_S),
                *COUNTDOWN_RANGE,
                DEFAULT_COUNTDOWN_S,
            )
        )

    def enabled(self) -> bool:
        return self._settings_get("auto_concede", True) is not False

    # -- queries -----------------------------------------------------------------

    @property
    def armed(self) -> bool:
        return self._armed is not None

    def recommended(self, game_key: Any) -> bool:
        with self._lock:
            return game_key in self._recommended

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            armed = self._armed
            return {
                "armed": None
                if armed is None
                else {
                    "id": armed.token,
                    "game_key": repr(armed.game_key),
                    "turn": armed.turn,
                    "seconds": armed.seconds,
                    "started": armed.started,
                },
                "recommended": [repr(k) for k in self._recommended],
                "declined": [repr(k) for k in self._declined],
                "conceded": [repr(k) for k in self._conceded],
                "bridge_unsupported": self._bridge_unsupported,
                "last_estimate": self._last_estimate.as_payload() if self._last_estimate else None,
                "auto_concede": self.enabled(),
                "threshold": self.threshold(),
                "countdown_s": self.countdown_s(),
            }

    # -- engine reloads ---------------------------------------------------------

    def export_state(self) -> dict[str, Any] | None:
        """This game's recommend/decline/concede record, for an engine reload."""
        current = self._current
        if current is None or not current[1]:
            return None
        game_key, match_id, turn = current
        with self._lock:
            return {
                "match_id": match_id,
                "turn": turn,
                "recommended": game_key in self._recommended,
                "declined": game_key in self._declined,
                "conceded": game_key in self._conceded,
                "arms": int(self._arms.get(game_key, 0)),
            }

    def resume_from(self, saved: Any) -> None:
        """Apply a reloaded engine's record to the first observation of the same game."""
        self._resume = dict(saved) if isinstance(saved, dict) and saved.get("match_id") else None

    def _apply_resume(self, game_key: Any, state: dict) -> None:
        saved = self._resume
        match_id = state.get("match_id") if isinstance(state, dict) else None
        turn = _int(((state or {}).get("turn") or {}).get("turn_number")) or 0
        if saved is None or not match_id or turn <= 0:
            return  # keep it for the first snapshot of a game in progress
        self._resume = None
        if match_id != saved.get("match_id") or turn < (_int(saved.get("turn")) or 0):
            logger.info("[CONCEDE] reload record discarded: the game changed")
            return
        with self._lock:
            for flag, store in (
                ("recommended", self._recommended),
                ("declined", self._declined),
                ("conceded", self._conceded),
            ):
                if saved.get(flag):
                    _remember(store, game_key)
            arms = _int(saved.get("arms")) or 0
            if arms:
                _remember(self._arms, game_key, max(arms, self._arms.get(game_key, 0)))
        logger.info(
            "[CONCEDE] restored after the engine reload (game %r): recommended=%s declined=%s conceded=%s",
            game_key,
            bool(saved.get("recommended")),
            bool(saved.get("declined")),
            bool(saved.get("conceded")),
        )

    # -- the coaching-loop hook ---------------------------------------------------

    def observe(self, state: dict, game_key: Any) -> LossEstimate:
        """Re-estimate; recommend once per game; offer, keep or abort the countdown."""
        self._apply_resume(game_key, state)
        estimate = estimate_loss(state)
        threshold = self.threshold()
        self._last_estimate = estimate
        turn = _int(((state or {}).get("turn") or {}).get("turn_number")) or 0
        self._current = (game_key, (state or {}).get("match_id"), turn)
        self._log_estimate(game_key, estimate)
        if self._bridge_unsupported and not self._safe(self._bridge_ready):
            self._bridge_unsupported = False  # it went away; an update may bring it back able
        drafting = bool(self._draft_active())
        armed = self._armed
        if armed is not None:
            why = ""
            if armed.game_key != game_key:
                why = "the game changed"
            elif drafting:
                why = "a draft is active"
            elif estimate.confidence < threshold:
                why = f"loss confidence dropped to {estimate.confidence:.2f} ({estimate.reason})"
            elif not self._autopilot_on():
                why = "autoplay is no longer on"
            if why:
                self._abort(armed, why)
        if estimate.confidence < threshold or drafting:
            self._streak = None
            return estimate
        if not self._steady(game_key):
            return estimate

        can_arm, why_not = self._can_arm(game_key)
        seconds = self.countdown_s()
        with self._lock:
            first = game_key not in self._recommended
            if first:
                _remember(self._recommended, game_key)
        if first:
            logger.info(
                "[CONCEDE] recommending a concede (confidence %.2f, game %r): %s",
                estimate.confidence,
                game_key,
                estimate.reason,
            )
        offered = False
        if can_arm:
            offer = offer_text(seconds)
            text = f"{estimate.reason} {offer}" if first else offer
            offered = self._arm(game_key, state, estimate, seconds, text, state if first else None)
        if first and not offered:
            self._advise(estimate.reason, state)
            logger.info("[CONCEDE] recommend only: %s", why_not or "the countdown could not start")
        return estimate

    def _steady(self, game_key: Any) -> bool:
        """The verdict held on enough observations, far enough apart."""
        now = self._clock()
        streak = self._streak
        if streak is None or streak[0] != game_key:
            self._streak = (game_key, 1, now)
            return self._confirm_observations <= 1
        _key, count, last = streak
        if now - last >= self._min_gap_s:
            count += 1
            self._streak = (game_key, count, now)
        return count >= self._confirm_observations

    def _log_estimate(self, game_key: Any, estimate: LossEstimate) -> None:
        key = (game_key, round(estimate.confidence, 2), estimate.reason)
        previous = self._last_logged
        if key == previous:
            return
        high = estimate.confidence >= 0.5
        if high or (previous is not None and previous[1] >= 0.5):
            logger.info(
                "[CONCEDE] loss confidence %.2f: %s%s",
                estimate.confidence,
                estimate.reason,
                f" | {'; '.join(estimate.blockers[:3])}" if estimate.blockers and high else "",
            )
        self._last_logged = key

    def _can_arm(self, game_key: Any) -> tuple[bool, str]:
        if not self._autopilot_on():
            return False, "autoplay is off"
        if not self.enabled():
            return False, "auto-concede is turned off"
        if self._bridge_unsupported:
            return False, "this bridge can't concede yet (update the plugin)"
        if not self._bridge_ready():
            return False, "the bridge is not connected or can't concede"
        with self._lock:
            if self._armed is not None:
                return False, "a countdown is already running"
            if game_key in self._declined:
                return False, "you cancelled the auto-concede this game"
            if game_key in self._conceded:
                return False, "already conceded this game"
            if self._arms.get(game_key, 0) >= MAX_ARMS_PER_GAME:
                return False, "too many countdowns this game"
        return True, ""

    def _arm(
        self,
        game_key: Any,
        state: dict,
        estimate: LossEstimate,
        seconds: int,
        text: str,
        record_state: dict | None,
    ) -> bool:
        turn = state.get("turn") or {}
        local, _ = _seats(state)
        with self._emit_lock:
            with self._lock:
                if (
                    self._armed is not None
                    or game_key in self._declined
                    or game_key in self._conceded
                    or self._arms.get(game_key, 0) >= MAX_ARMS_PER_GAME
                ):
                    return False
                armed = _Armed(
                    token=next(self._tokens),
                    game_key=game_key,
                    match_id=state.get("match_id"),
                    turn=_int(turn.get("turn_number")) or 0,
                    local=local,
                    seconds=seconds,
                    estimate=estimate,
                    ended_at_arm=bool(self._safe(self._game_over)),
                    text=text,
                    record_state=record_state,
                )
                self._armed = armed
                _remember(self._arms, game_key, self._arms.get(game_key, 0) + 1)
            logger.warning(
                "[CONCEDE] auto-concede offered (countdown %d, %ds once the offer is heard, "
                "confidence %.2f, game %r)",
                armed.token,
                seconds,
                estimate.confidence,
                game_key,
            )
            self._emit(
                {
                    "state": "offering",
                    "id": armed.token,
                    "seconds": seconds,
                    "reason": estimate.reason,
                    "confidence": round(estimate.confidence, 3),
                }
            )
        self._start_thread(self._run, armed)
        return True

    # -- cancelling -----------------------------------------------------------------

    def cancel(self, source: str = "ui", *, decline: bool = True) -> bool:
        """Stop a running countdown (any thread). False when none, or too late.

        ``decline`` (a user's "keep playing") also stops it for the rest of the
        game; without it the countdown is only aborted (the coach is stopping or
        reloading) and the event says so.
        """
        with self._emit_lock:
            with self._lock:
                armed = self._armed
                if armed is None or armed.claimed:
                    return False
                armed.cancelled.set()
                self._armed = None
                if decline:
                    _remember(self._declined, armed.game_key)
            if decline:
                logger.warning(
                    "[CONCEDE] countdown %d cancelled by %s; keep playing this game", armed.token, source
                )
                self._emit({"state": "cancelled", "id": armed.token, "source": source})
            else:
                logger.warning("[CONCEDE] countdown %d aborted: %s", armed.token, source)
                self._emit({"state": "aborted", "id": armed.token, "reason": source})
        return True

    def _abort(self, armed: _Armed, why: str) -> bool:
        with self._emit_lock:
            with self._lock:
                if self._armed is not armed or armed.claimed:
                    return False
                armed.cancelled.set()
                self._armed = None
            logger.warning("[CONCEDE] countdown %d aborted: %s", armed.token, why)
            self._emit({"state": "aborted", "id": armed.token, "reason": why})
        return True

    # -- the countdown thread -------------------------------------------------------

    def _announce(self, armed: _Armed) -> bool:
        """Show and speak the offer; True once heard (or when nothing is spoken)."""
        if self._announce_fn is None:
            self._advise(armed.text, armed.record_state)
            return True
        try:
            return bool(self._announce_fn(armed.text, armed.record_state, armed.cancelled.is_set))
        except Exception as error:
            logger.warning("[CONCEDE] announcing countdown %d failed: %s", armed.token, error)
            return False

    def _run(self, armed: _Armed) -> None:
        try:
            if armed.cancelled.is_set():
                return  # cancelled before this thread even started
            heard = self._announce(armed)
            if armed.cancelled.is_set():
                return
            with self._emit_lock:
                with self._lock:
                    if self._armed is not armed or armed.cancelled.is_set():
                        return
                    armed.started = True
                logger.warning(
                    "[CONCEDE] countdown %d started: %ds (offer %s)",
                    armed.token,
                    armed.seconds,
                    "heard" if heard else "not confirmed as heard",
                )
                self._emit(
                    {
                        "state": "armed",
                        "id": armed.token,
                        "seconds": armed.seconds,
                        "reason": armed.estimate.reason,
                        "confidence": round(armed.estimate.confidence, 3),
                    }
                )
            if self._wait(armed.cancelled, armed.seconds) or armed.cancelled.is_set():
                return
            problem, fresh = self._verify(armed)
            if problem:
                self._abort(armed, problem)
                return
            with self._emit_lock:
                with self._lock:
                    if (
                        armed.cancelled.is_set()
                        or self._armed is not armed
                        or armed.game_key in self._conceded
                    ):
                        return
                    armed.claimed = True
                    _remember(self._conceded, armed.game_key)
                    self._armed = None
                logger.warning(
                    "[CONCEDE] countdown %d finished; conceding game %r on turn %s",
                    armed.token,
                    armed.game_key,
                    (fresh.get("turn") or {}).get("turn_number"),
                )
                self._emit({"state": "conceding", "id": armed.token})
            try:
                result = self._concede_fn(fresh)
            except Exception as error:  # the bridge raised: report, never retry
                result = {"ok": False, "error": str(error)}
            if not isinstance(result, dict):
                result = {"ok": bool(result)}
            if result.get("ok"):
                logger.warning("[CONCEDE] concede sent for game %r: %s", armed.game_key, result)
                self._emit({"state": "sent", "id": armed.token})
                self._confirm(armed)
                return
            error = str(result.get("error") or "unknown error")
            unknown = bool(result.get("outcome_unknown"))
            unsupported = bool(result.get("unsupported"))
            if unsupported:
                self._bridge_unsupported = True
            logger.warning(
                "[CONCEDE] concede %s for game %r: %s (not retrying)",
                "outcome unknown" if unknown else "unsupported by this bridge" if unsupported else "failed",
                armed.game_key,
                error,
            )
            if unknown:
                message = "Auto-concede may not have gone through. Check Arena, and concede from its menu if you want to."
            elif unsupported:
                message = (
                    "This bridge can't concede yet, so auto-concede is off until the plugin is updated. "
                    "Concede from Arena's menu if you want to."
                )
            else:
                message = "Auto-concede didn't go through. Concede from Arena's menu if you want to."
            self._emit({"state": "failed", "id": armed.token, "error": error, "message": message})
            self._advise(message, None)
        except Exception as error:
            logger.warning("[CONCEDE] countdown %d failed: %s", armed.token, error, exc_info=True)
            self._abort(armed, f"internal error: {error}")

    def _verify(self, armed: _Armed) -> tuple[str, dict]:
        """Re-check everything right before conceding; a non-empty reason aborts."""
        if not self._autopilot_on():
            return "autoplay is no longer on", {}
        if not self.enabled():
            return "auto-concede was turned off", {}
        if not self._bridge_ready():
            return "the bridge disconnected", {}
        if self._draft_active():
            return "a draft is active", {}
        if self._safe(self._game_over) and not armed.ended_at_arm:
            return "the game is over", {}
        try:
            fresh = self._get_state()
        except Exception as error:
            return f"could not read the game state ({error})", {}
        if not isinstance(fresh, dict) or not fresh:
            return "no game state", {}
        not_live = game_not_live(fresh)
        if not_live:
            return f"the game is not in progress ({not_live})", {}
        if fresh.get("match_id") != armed.match_id:
            return "the match changed", {}
        turn_info = fresh.get("turn") or {}
        turn = _int(turn_info.get("turn_number")) or 0
        if turn < armed.turn:
            return "a new game started", {}
        if turn > armed.turn + 1 or (
            _int(turn_info.get("active_player")) == armed.local and turn != armed.turn
        ):
            return "our new turn began (untap and a fresh draw)", {}
        estimate = estimate_loss(fresh)
        threshold = self.threshold()
        if estimate.confidence < threshold:
            return f"loss confidence dropped to {estimate.confidence:.2f} ({estimate.reason})", {}
        logger.info(
            "[CONCEDE] verified before conceding: confidence %.2f, %s", estimate.confidence, estimate.reason
        )
        return "", fresh

    def _confirm(self, armed: _Armed) -> None:
        """Report "conceded" once the game ends; warn (never resend) when it does not."""
        checks = max(1, int(self._confirm_s / 0.5))
        for _ in range(checks):
            ended = ""
            if self._safe(self._game_over) and not armed.ended_at_arm:
                ended = "game end seen"
            else:
                try:
                    fresh = self._get_state()
                except Exception:
                    fresh = {}
                if (
                    not isinstance(fresh, dict)
                    or game_not_live(fresh)
                    or fresh.get("match_id") != armed.match_id
                ):
                    ended = "game no longer in progress"
            if ended:
                logger.info("[CONCEDE] concede confirmed: %s (game %r)", ended, armed.game_key)
                self._emit({"state": "conceded", "id": armed.token, "reason": armed.estimate.reason})
                return
            self._wait(threading.Event(), 0.5)
        logger.warning(
            "[CONCEDE] no game end seen %.0fs after the concede (game %r); not resending",
            self._confirm_s,
            armed.game_key,
        )
        message = (
            "Arena hasn't ended the game after the auto-concede. Check Arena, and concede from its menu "
            "if you want to."
        )
        self._emit({"state": "unconfirmed", "id": armed.token, "message": message})
        self._advise(message, None)

    @staticmethod
    def _safe(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception:
            return None
