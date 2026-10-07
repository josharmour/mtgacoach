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
  our life if the opponent attacks every turn and we block with what we have.

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
from arenamcp.mulligan_policy import _land_colors, _pip_matching, hand_card

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


def removal_reach(card: dict) -> tuple[str, int | None] | None:
    """How a spell's text kills an opposing creature: (kind, toughness limit).

    ``("destroy", None)`` kills any creature; ``("damage", 3)`` kills
    toughness <= 3; ``("bounce", None)`` removes it for a turn. Returns None
    when the text has no recognisable creature removal (noncreature-only
    removal such as "destroy target noncreature permanent" is not creature
    removal).
    """
    text = _text(card)
    for match in _DESTROY.finditer(text):
        what = match.group("what")
        if "noncreature" not in what and ("creature" in what or "permanent" in what):
            return "destroy", None
    amounts = [_int(match.group("n")) for match in _DAMAGE.finditer(text)]
    if amounts:
        # Modal burn: the strongest mode (Fulminous Forte: 1 to each, or 5 to one).
        return "damage", max(3 if amount is None else amount for amount in amounts)
    match = _SHRINK.search(text)
    if match:
        amount = _int(match.group("n"))
        if amount:
            return "shrink", amount
    if _FIGHT.search(text):
        return "fight", None
    if _BOUNCE.search(text):
        return "bounce", None
    return None


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
    if ("artifact" in types or "enchantment" in types) and _MANA_ABILITY.search(text):
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


def _mana_source(card: dict, turn: int) -> SimpleNamespace | None:
    """A permanent that taps for mana, with its colours (C for colourless)."""
    text = _text(card)
    is_land = _is_land(card) and not _is_creature(card)
    if not is_land and not _MANA_ABILITY.search(text):
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
    mana_value: int
    pips: tuple
    has_x: bool
    value: float = 0.0


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
        if index == 0 and self.lethal_now:
            bits.append("ATTACK FOR LETHAL (their best blocks can't stop it)")
        if step.land:
            bits.append(f"play {step.land}")
        bits.append("cast " + " + ".join(step.casts) if step.casts else "no castable board play")
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
        self, *, this_turn: str | None = None, next_turns: str | None = None, role_note: str = ""
    ) -> str:
        """Short, decisive facts for per-decision prompts: ROLE, this turn, facts, next.

        ``this_turn`` / ``next_turns`` replace the board-math deployment with a
        validated game-plan step; ``role_note`` explains a plan/role mismatch.
        """
        when = "now" if self.our_turn else "our next turn"
        lines = [f"STRATEGIC ROLE (deterministic board math, recomputed now): {self.headline()}{role_note}"]
        lines.append(f"  THIS TURN (T{self.plan_turn}, {when}): {this_turn or self.suggestion(0)}")
        lines.append(f"  FACTS: {self.facts_line()}")
        if next_turns is None:
            next_turns = " | ".join(
                f"T{step.turn}: {self.suggestion(k)}" for k, step in enumerate(self.lookahead[1:], start=1)
            )
        if next_turns:
            lines.append(f"  NEXT: {next_turns}")
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

    def planning_block(self) -> str:
        """Fuller facts for the background strategic plan call."""
        lines = [self.prompt_block().replace("recomputed now", "at plan time")]
        if self.lookahead:
            lines.append(
                "BOARD-MATH PROJECTION (we deploy the best castable plays and block; they attack every turn): "
                + self._lookahead_text()
            )
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
            life = "" if step.life_after is None else f" -> life {step.life_after}"
            parts.append(f"T{step.turn}: {land}{casts}{life}")
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
        }


# --- assessment ---------------------------------------------------------------

_CACHE: OrderedDict[tuple, BoardAssessment] = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_SIZE = 32


def _signature(state: dict) -> tuple:
    turn = state.get("turn") or {}
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
    return (
        state.get("match_id"),
        turn.get("turn_number"),
        turn.get("active_player"),
        turn.get("phase"),
        turn.get("step"),
        players,
        cards,
        hand,
        len(state.get("graveyard") or []),
        zones.get("library_count"),
        zones.get("opponent_hand_count"),
        bool(state.get("deck_catalog")),  # the deck curve only exists on prepared states
    )


def assess(state: dict | None) -> BoardAssessment | None:
    """Assess a planner-shape snapshot; None when seats/turn are unknown."""
    if not isinstance(state, dict):
        return None
    try:
        key = _signature(state)
    except Exception:
        key = None
    if key is not None:
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if cached is not None:
                _CACHE.move_to_end(key)
                return cached
    try:
        result = _assess(state)
    except Exception as error:  # never break a decision on the strategic layer
        logger.debug("board assessment failed: %s", error, exc_info=True)
        return None
    if key is not None and result is not None:
        with _CACHE_LOCK:
            _CACHE[key] = result
            while len(_CACHE) > _CACHE_SIZE:
                _CACHE.popitem(last=False)
    return result


def _assess(state: dict) -> BoardAssessment | None:
    started = time.perf_counter()
    local, opponent = _seats(state)
    turn_info = state.get("turn") or {}
    turn = _int(turn_info.get("turn_number")) or 0
    if local is None or opponent is None or turn <= 0:
        return None
    active = _int(turn_info.get("active_player"))
    our_turn = active == local
    phase = str(turn_info.get("phase") or "")
    step = str(turn_info.get("step") or "")
    our_life, opp_life = _life(state, local), _life(state, opponent)
    battlefield = [c for c in state.get("battlefield") or [] if isinstance(c, dict)]
    hand = [c for c in state.get("hand") or [] if isinstance(c, dict)]
    unknowns: list[str] = []

    attached: dict[int, list[dict]] = {}
    for card in battlefield:
        target = _int(card.get("attached_to_id"))
        if target:
            attached.setdefault(target, []).append(card)

    our_rules = _side_rules(battlefield, local)
    their_rules = _side_rules(battlefield, opponent)
    ours: list[dict] = []
    theirs: list[dict] = []
    for card in battlefield:
        if not _is_creature(card):
            continue
        rules = our_rules if _controller(card) == local else their_rules
        body = _body(card, turn, rules, attached=attached)
        if body is None:
            unknowns.append(f"{_name(card)} has unknown power/toughness")
            continue
        (ours if _controller(card) == local else theirs).append(body)

    # --- timing of the next attacks ---------------------------------------
    any_ours_attacking = any(b["_attacking"] for b in ours)
    any_theirs_attacking = any(b["_attacking"] for b in theirs)
    pre_combat = phase in ("Phase_Beginning", "Phase_Main1") or (
        phase == "Phase_Combat" and step in ("", "Step_BeginCombat", "Step_DeclareAttack")
    )
    in_combat_before_damage = phase == "Phase_Combat" and step not in (
        "Step_CombatDamage",
        "Step_EndCombat",
    )
    our_attack_pending = our_turn and pre_combat and not any_ours_attacking
    their_attack_pending = (not our_turn) and (
        pre_combat or (in_combat_before_damage and any_theirs_attacking)
    )

    def able_now(bodies: list[dict]) -> list[dict]:
        if any(b["_attacking"] for b in bodies):
            return [b for b in bodies if b["_attacking"]]
        return [b for b in bodies if not b["_tapped"] and not b["_sick"]]

    untapped_ours = [b for b in ours if not b["_tapped"]]
    untapped_theirs = [b for b in theirs if not b["_tapped"]]
    # Our blockers for their next attack: what is untapped now when that
    # attack comes before our untap step, otherwise everything.
    our_first_blockers = untapped_ours if (our_turn or their_attack_pending) else list(ours)

    # --- clocks (board only) -------------------------------------------------
    our_clock, our_lives = _simulate_attacks(
        ours,
        theirs,
        opp_life,
        first_attackers=able_now(ours) if our_attack_pending else None,
        first_blockers=untapped_theirs if our_attack_pending else None,
    )
    their_clock, their_lives = _simulate_attacks(
        theirs,
        ours,
        our_life,
        first_attackers=able_now(theirs) if their_attack_pending else None,
        # On our turn, creatures that attacked stay tapped through theirs.
        first_blockers=our_first_blockers,
    )
    lethal_now = our_attack_pending and our_clock == 1
    if our_attack_pending and not lethal_now:
        next_clock, _ = _simulate_attacks(
            ours, theirs, opp_life, first_attackers=None, first_blockers=None, horizon=1
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

    # --- mana ----------------------------------------------------------------
    our_permanents = [c for c in battlefield if _controller(c) == local]
    sources_all = [s for c in our_permanents if (s := _mana_source(c, turn + 1))]
    sources_now = [s for c in our_permanents if not c.get("is_tapped") and (s := _mana_source(c, turn))]
    our_lands = sum(1 for c in our_permanents if _is_land(c) and not _is_creature(c))
    their_lands = sum(
        1 for c in battlefield if _controller(c) == opponent and _is_land(c) and not _is_creature(c)
    )
    hand_lands = [c for c in hand if _is_land(c) and not _is_creature(c)]
    lands_played = _int(_player(state, local).get("lands_played")) or 0
    land_drop_now = (not our_turn) or lands_played == 0
    land_drop_available = bool(hand_lands) and land_drop_now
    colors_all = set().union(*(s.produces for s in sources_all)) if sources_all else set()
    for land in hand_lands:
        colors_all |= set(_land_colors(land))

    spells: list[_Spell] = []
    for card in hand:
        if _is_land(card) and not _is_creature(card):
            continue
        info = hand_card(card)
        cost = str(card.get("mana_cost") or "")
        if not cost and not _is_creature(card):
            continue
        spells.append(
            _Spell(
                card=card,
                name=_name(card),
                role=card_role(card),
                mana_value=info.mana_value,
                pips=info.pips,
                has_x="x" in cost.lower(),
            )
        )
    missing = sorted({c for s in spells for pip in s.pips for c in pip if not (pip & colors_all)})
    if any(s.has_x for s in spells):
        unknowns.append("X spells are not scheduled")

    # --- threats -------------------------------------------------------------
    our_air = any(_reach_or_flying(b) for b in ours)
    threats = _threats(battlefield, opponent, theirs, our_air, len(ours))
    if any("adds attackers" in t.why or "token" in t.why for t in threats):
        unknowns.append("token/copy engines add attackers each turn (clocks may be faster)")
    unknowns.append("opponent's hand, draws and combat tricks")

    # --- preliminary role (board only) feeds the deployment values -----------
    survival_hint = (
        opp_lethal_on_board
        or race == "behind"
        or (their_clock is not None and their_clock <= 2)
        or (
            len(theirs) > len(ours) + 1
            and sum(b["power"] for b in theirs) > sum(b["power"] for b in ours) + 2
        )
    )
    budgets_turns = _budget_turns(
        our_turn,
        turn,
        sources_now,
        sources_all,
        hand_lands,
        land_drop_now,
    )
    schedule = _schedule(spells, budgets_turns, survival=survival_hint and not lethal_now, theirs=theirs)
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
        first_their_attackers=able_now(theirs) if their_attack_pending else None,
        untapped_ours=our_first_blockers,
    )

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
        if dead_in == 1:
            flags.append("DEAD NEXT ATTACK even after our best castable plays")
        else:
            flags.append(
                "DEAD IN 2 TURNS UNLESS WE STABILIZE"
                + (" (our castable blockers buy one turn)" if saved else "")
            )
    elif their_clock is not None and their_clock <= 2 and dead_in is None:
        flags.append(f"their board kills us in {their_clock} but our castable plays stabilize")
    # Nothing to lose: dead next attack even after our best castable plays,
    # by a clear margin or to evasion we cannot block. Requires the facts to be
    # unambiguous, since an all-in attack throws away blockers.
    through = our_life - their_lives[0] if their_lives else 0
    evasive = 0 if our_air else sum(b["power"] for b in theirs if _flying(b))
    all_in = bool(
        opp_lethal_on_board
        and dead_in == 1
        and not lethal_now
        and our_power > 0
        and (through >= our_life + 2 or evasive >= our_life)
    )
    if all_in:
        flags.append(
            "ALL-IN: no defensive line survives their next attack — attack with everything; "
            "holding back blockers changes nothing"
        )

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
    )

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
    )
    result.elapsed_ms = (time.perf_counter() - started) * 1000
    return result


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
    battlefield: list[dict], opponent: int | None, theirs: list[dict], our_air: bool, our_count: int
) -> list[Threat]:
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
    return threats[:5]


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


def _spell_value(spell: _Spell, *, survival: bool, theirs: list[dict]) -> float:
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
        killable = [b for b in theirs if _kills(spell.card, b)]
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


def _kills(card: dict, body: dict) -> bool:
    reach = removal_reach(card)
    if reach is None:
        return False
    kind, limit = reach
    if kind in ("damage", "shrink"):
        return (
            not has_combat_keyword(body, "indestructible")
            and limit is not None
            and body["toughness"] <= limit
        )
    if kind == "destroy":
        return not has_combat_keyword(body, "indestructible")
    return True  # bounce / fight (fight is approximate)


def _schedule(
    spells: list[_Spell], budgets: list[dict], *, survival: bool, theirs: list[dict]
) -> list[list[_Spell]]:
    """Per-turn knapsack: the highest-value affordable set (<=3 spells) each turn."""
    for spell in spells:
        spell.value = _spell_value(spell, survival=survival, theirs=theirs)
    remaining = [s for s in spells if not s.has_x and s.value > 0]
    schedule: list[list[_Spell]] = []
    extra: list[SimpleNamespace] = []
    for budget in budgets:
        sources = list(budget["sources"]) + extra
        best: tuple = ()
        best_key = (0.0, 0)
        for size in range(1, min(_MAX_SPELLS_PER_TURN, len(remaining)) + 1):
            for combo in combinations(remaining, size):
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
            if not s.has_x and s.mana_value <= len(sources) and _pip_matching(s.pips, sources)
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
                killable = [b for b in enemy if _kills(spell.card, b)]
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
) -> tuple[str, str]:
    """Who's the beatdown: lethal and survival first, then fast clocks, then board/cards/curve.

    A clock within the simulation horizon (<= 6 attacks) is "fast" and drives
    the role; slower clocks only break ties, so a lone 1/2 flyer does not turn
    the game into a race.
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
    if race == "ahead" and fast(our_clock) and (their_clock is None or our_clock <= 3):
        return ROLE_AGGRESSOR, (
            f"our clock {our_clock} beats their {text(their_clock)} ({board}) — we're the beatdown"
        )
    if opp_lethal:
        through = our_life - (their_lives[0] if their_lives else our_life)
        return ROLE_CONTROL, (
            f"opponent has lethal on board ({through} through our best blocks vs {our_life} life) — survive first"
        )
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
    if card_advantage is not None and card_advantage >= 2:
        return ROLE_CONTROL, f"{slow}; +{card_advantage} cards — we win the long game"
    if card_advantage is not None and card_advantage <= -2:
        return ROLE_AGGRESSOR, f"{slow}; {card_advantage} cards — force damage before they out-card us"
    if race == "ahead":
        return ROLE_AGGRESSOR, f"{slow} — we're slightly ahead: keep pressure, add evasion"
    if race == "behind" or their_power > our_power:
        return ROLE_DEFENDER, f"{slow} — hold blockers, develop bigger threats"
    return ROLE_RACE, f"{slow} — find evasion or removal to break the stall"


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
            killable = [b for b in enemy if _kills(source, b)]
            if not killable:
                continue
            target = max(killable, key=lambda b: (b["power"], b["toughness"]))
            loss = _life_loss(state, remove_id=target["instance_id"])
            candidates.append((loss, 1, option.option_id, f"cast {_name(source)} on {target['name']}"))
        elif role == "land" and assessment.our_turn and assessment.land_drop_available:
            enabled = _enabled_by_land(state, option, source, our_rules, enemy, turn)
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
) -> tuple[int, str] | None:
    """Best survival play this land drop makes castable this turn: (loss, description)."""
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
    for card in state.get("hand") or []:
        if not isinstance(card, dict) or card.get("instance_id") == land.get("instance_id"):
            continue
        role = card_role(card)
        if role not in ("creature", "removal"):
            continue
        info = hand_card(card)
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
            killable = [b for b in enemy if _kills(card, b)]
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
