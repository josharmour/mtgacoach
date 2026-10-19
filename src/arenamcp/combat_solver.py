"""Deterministic combat-decision solver.

Enumerates legal block assignments (and attacker subsets) and picks the
best outcome under a simple scoring model. Combat is one of the few MTG
decisions that's fully deterministic once declarations are made, so a
targeted solver is more reliable than the LLM for the mechanical part.

Used two ways:
  1. `optimal_blocks()` / `optimal_attacks()` produce a recommended
     declaration that can be injected into the coach prompt as grounded
     "Computed optimal ..." advice.
  2. When autopilot is driving, the planner can compare the LLM's chosen
     declaration against the solver's pick and override if the LLM's
     choice is materially worse.

Scoring is a weighted sum over (life_preserved, material_gained,
material_lost) with life dominant when it's low.
"""

from __future__ import annotations

import copy
import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import combinations, product
from typing import Any

from arenamcp.combat_keywords import can_be_blocked_by, has_combat_keyword

logger = logging.getLogger(__name__)

# The planner prompt, the tactical line, the coach's advice prompt and the
# game-plan prompt all solve the same board within seconds of each other
# (2026-09-24: four ~35s runs back to back froze the coach for 2.5 minutes).
_MEMO: OrderedDict[tuple, Any] = OrderedDict()
_MEMO_LOCK = threading.Lock()
_MEMO_SIZE = 64


def _card_key(c: dict) -> tuple:
    """Everything the solver reads from a creature."""
    return (
        c.get("instance_id"),
        c.get("name"),
        c.get("power"),
        c.get("toughness"),
        c.get("oracle_text"),
        tuple(c.get("keywords") or ()),
        c.get("cant_be_blocked"),
        c.get("combat_hit_bonus"),
        c.get("is_tapped"),
        c.get("_engine_value"),
    )


def _cards_key(cards: list[dict]) -> tuple:
    return tuple(_card_key(c) for c in cards)


def _memoized(key: tuple, compute: Callable[[], Any]) -> Any:
    with _MEMO_LOCK:
        if key in _MEMO:
            _MEMO.move_to_end(key)
            return copy.deepcopy(_MEMO[key])
    value = compute()
    with _MEMO_LOCK:
        _MEMO[key] = value
        while len(_MEMO) > _MEMO_SIZE:
            _MEMO.popitem(last=False)
    return copy.deepcopy(value)


# --- Helpers -----------------------------------------------------------


def _text(c: dict) -> str:
    return (c.get("oracle_text") or "").lower()


def _has(card: dict, keyword: str) -> bool:
    """Check for a keyword ability on a creature, case-insensitive."""
    return has_combat_keyword(card, keyword)


def _pt(card: dict) -> tuple[int, int]:
    return int(card.get("power") or 0), int(card.get("toughness") or 0)


@lru_cache(maxsize=2048)
def _resource_roles(oracle: str) -> tuple[str, ...]:
    """Recognize ongoing resource abilities, not resources from a spent ETB."""
    roles: set[str] = set()
    text = re.sub(r"<[^>]*>", "", oracle.lower())
    for ability in text.splitlines():
        ability = ability.strip()
        activated = (
            ":" in ability
            and not re.match(r"(?:when|whenever|at the beginning)\b", ability)
            and not re.search(r"sacrifice (?:this|~)[^:]*:", ability)
        )
        recurring = "whenever" in ability or "at the beginning" in ability
        if not (activated or recurring):
            continue
        if re.search(r"\badd\b[^.]*?(?:\{(?:o?[wubrgc])+\}|\bmana\b)", ability):
            roles.add("mana production")
            if re.search(r"\bfor each\b|\badd x\b|\badditional\b|\binstead\b", ability):
                roles.add("mana scaling")
        if re.search(r"\bdraw\b[^.]*?\bcards?\b", ability):
            roles.add("card draw")
        if re.search(r"\bcreate\b[^.]*?\btokens?\b", ability):
            roles.add("token production")
        if re.search(r"\b(?:return|put)\b[^.]*?\bfrom (?:your|a|the) graveyard\b", ability):
            roles.add("recursion")
    return tuple(sorted(roles))


def combat_resource_roles(card: dict) -> tuple[str, ...]:
    """Strategic roles used by the approximate combat valuation."""
    return _resource_roles(_text(card))


def mana_spending_restriction(card: dict) -> str:
    """Surface a mana source's printed restriction alongside its resource value."""
    text = re.sub(r"<[^>]*>", "", card.get("oracle_text") or "")
    match = re.search(r"\bSpend (?:this|that) mana only\b[^.]*", text, re.IGNORECASE)
    return match.group(0) if match else ""


def _material(card: dict) -> int:
    """Value a body plus ongoing resources; this is not a mana/castability estimate.

    ``_engine_value`` (``board_model.engine_facts``, set by callers that read the board): what its
    engine does each turn beyond the flat +3 a role — Squirrel Girl's 12 Squirrels a turn, Biotech
    Specialist's drain (ledger #172, #132)."""
    power, toughness = _pt(card)
    engine = int(round(float(card.get("_engine_value") or 0)))
    return max(1, power + toughness) + 2 + 3 * len(combat_resource_roles(card)) + engine


def _can_block(attacker: dict, blocker: dict) -> bool:
    """Return True iff the blocker can legally block the attacker.

    Checks keyword-level legality (flying/reach + menace partial) and
    "can't be blocked": the attacker's ``cant_be_blocked`` mark (static grants
    such as Tetsuko Umezawa's, see ``combat_keywords.annotate_cant_be_blocked``)
    and its own text, including checkable per-blocker restrictions ("can't be
    blocked by creatures with power 2 or greater"). Other defender-imposed
    restrictions (e.g. skulk) are not modeled — callers should filter those
    externally via the GRE-provided `attackerInstanceIds` list.
    """
    if _has(attacker, "flying") and not (_has(blocker, "flying") or _has(blocker, "reach")):
        return False
    if not can_be_blocked_by(attacker, blocker):
        return False
    # Menace: attacker can't be blocked except by two or more creatures.
    # We model this as a per-attacker constraint enforced later (>=2 blockers).
    return True


# --- Who is still there next turn --------------------------------------------
#
# 2026-10-19 T10 (match 92c09c7a): the safe-default attack kept Fleeting Effigy
# home "to chump" the 7/8 Chocobo, but Effigy returns to its owner's hand at
# our end step ("At the beginning of your end step, return this creature to
# its owner's hand"); we had no blocker on T11 and died. 10:14:31 (match
# 1e4ccb0c) the bridge's attack override cancelled a free Effigy attack for the
# same reason, and counted Slumbering Cerberus ("doesn't untap during your
# untap step", tapped) and Perforating Artist, which died blocking in the very
# attack being scored, in the crackback.

_LEAVES_AT_END_STEP = re.compile(
    r"(?:^|\n)\s*at the beginning of (?:your|the|each) end step, (?:return|sacrifice|exile) "
    r"(?P<who>[^,.\n]+?)(?: to (?:its|their) owner's hand| from the battlefield)?\s*(?:\.|$|\n)"
)
_DOESNT_UNTAP = re.compile(r"doesn't untap during (?:your|its controller's) untap step")
_UNTAPS_ITSELF = re.compile(r"\buntap (?:this creature|this permanent|cardname|~|it)\b")
_CANT_ATTACK = re.compile(r"(?m)^\s*(?:this creature |cardname |~ )?can't attack(?: or block)?\.?\s*$")


def _plain_rules(card: dict) -> str:
    """Lower-case rules text without markup or parenthesized reminder text."""
    return _without_reminders(_text(card))


@lru_cache(maxsize=2048)
def _without_reminders(text: str) -> str:
    text = re.sub(r"<[^>]*>", "", text)
    while re.search(r"\([^()]*\)", text):
        text = re.sub(r"\([^()]*\)", "", text)
    return text


def leaves_at_our_end_step(card: dict) -> bool:
    """The creature returns to hand, is sacrificed or exiled at this turn's end step.

    Only an unconditional clause on the creature itself counts (Fleeting
    Effigy, Ball Lightning); "if ..." conditions and reminder text (unearth)
    keep it as a blocker. Called on our turn, where "your end step" is today's.
    """
    match = _LEAVES_AT_END_STEP.search(_plain_rules(card))
    if match is None:
        return False
    who = match.group("who").strip()
    name = str(card.get("name") or "").lower()
    return who in ("this creature", "this permanent", "cardname", "~", "it") or (
        bool(name) and (who == name or who == name.split(",")[0])
    )


def can_attack_next_turn(card: dict, *, creature_died: bool | None = None) -> bool:
    """Whether an opposing creature can attack on its controller's next turn.

    Defenders and "can't attack" creatures never do. A tapped creature that
    "doesn't untap during your untap step" stays tapped unless its own text
    untaps it; a morbid untap ("if a creature died this turn") counts unless
    ``creature_died`` is known to be False.
    """
    if _has(card, "defender"):
        return False
    text = _plain_rules(card)
    if _CANT_ATTACK.search(text):
        return False
    if card.get("is_tapped") and _DOESNT_UNTAP.search(text):
        if not _UNTAPS_ITSELF.search(text):
            return False
        if "died this turn" in text and creature_died is False:
            return False
    return True


# --- Combat resolution for a single attacker + assigned blockers -------


@dataclass
class CombatOutcome:
    damage_through: int = 0
    attacker_died: bool = False
    blockers_died: list[dict] = field(default_factory=list)
    life_gained: int = 0  # a lifelink attacker's damage dealt (to blockers and through)


def _damage_order(power: int, blockers: list[dict], deathtouch: bool) -> list[dict]:
    """The attacker's damage assignment order (rule 510.1c): the blockers it kills first.

    With ``power`` to assign and lethal damage per blocker (its toughness, 1 with
    deathtouch), the attacker picks the set it can kill outright — the most
    blockers, then the most power and toughness among them — and puts those
    first (cheapest lethal leading), the rest after, indestructible last. Up to
    eight killable blockers are searched exactly; beyond that the cheapest
    kills lead.
    """
    killable = [b for b in blockers if not _has(b, "indestructible")]
    lethal = {id(b): (1 if deathtouch else _pt(b)[1]) for b in killable}
    chosen: tuple[dict, ...] = ()
    if len(killable) <= 8:
        for size in range(len(killable), 0, -1):
            best = None
            for combo in combinations(killable, size):
                if sum(lethal[id(b)] for b in combo) <= power:
                    key = sum(_pt(b)[0] + _pt(b)[1] for b in combo)
                    if best is None or key > best[0]:
                        best = (key, combo)
            if best is not None:
                chosen = best[1]
                break
    chosen_ids = {id(b) for b in chosen}
    first = sorted(chosen, key=lambda b: lethal[id(b)])
    rest = sorted((b for b in killable if id(b) not in chosen_ids), key=lambda b: lethal[id(b)])
    return first + rest + [b for b in blockers if _has(b, "indestructible")]


def _resolve_attacker(attacker: dict, assigned: list[dict]) -> CombatOutcome:
    """Resolve one attacker against its assigned blockers.

    Simplified damage model:
      - If no blockers: all power goes through.
      - If blockers: the attacker orders them (rule 510.1c) to kill as many as
        it can, the biggest bodies among equal counts (``_damage_order``),
        then remaining damage spills to player iff attacker has trample. A
        3/2 double-blocked by a 1/5 and a 1/2 kills the 1/2; it does not dump
        its damage into the 1/5.
      - First strike: attacker strikes first. Blockers killed in the FS
        step don't deal damage back.
      - Deathtouch: any damage to a blocker is lethal; when choosing how
        to spread damage with deathtouch+trample, 1 point per blocker is
        "lethal" for spillover math.
      - An attacker marked ``combat_hit_bonus`` (``combat_strategy.with_hit_bonus``:
        a combat-damage-to-a-player trigger that adds damage this combat,
        such as Shark Shredder's reanimation into an attacker) deals that
        much more when it connects.
    """
    atk_p, atk_t = _pt(attacker)
    atk_dth = _has(attacker, "deathtouch")
    atk_trample = _has(attacker, "trample")
    atk_fs = _has(attacker, "first strike") or _has(attacker, "double strike")
    atk_ds = _has(attacker, "double strike")
    atk_lifelink = _has(attacker, "lifelink")
    atk_indestructible = _has(attacker, "indestructible")

    out = CombatOutcome()

    if not assigned:
        out.damage_through = atk_p * (2 if atk_ds else 1)
        out.life_gained = out.damage_through if atk_lifelink else 0
        if out.damage_through > 0 and "combat_hit_bonus" in attacker:
            out.damage_through += int(attacker["combat_hit_bonus"] or 0)
        return out

    assigned = _damage_order(atk_p, assigned, atk_dth)

    def first_striker(card: dict) -> bool:
        return _has(card, "first strike") or _has(card, "double strike")

    fs_blockers = [b for b in assigned if first_striker(b)]
    if not atk_fs and not fs_blockers:
        # One simultaneous damage step: the common case, kept cheap for the line search.
        dmg_left = atk_p
        for blk in assigned:
            lethal = 1 if atk_dth else _pt(blk)[1]
            take = min(dmg_left, lethal)
            if take >= lethal and not _has(blk, "indestructible"):
                out.blockers_died.append(blk)
            dmg_left -= take
            if dmg_left <= 0:
                break
        if atk_trample and dmg_left > 0:
            out.damage_through = dmg_left + int(attacker.get("combat_hit_bonus") or 0)
        # All of its damage is dealt: excess without trample still goes on the blockers.
        out.life_gained = atk_p if atk_lifelink else 0
        back = sum(_pt(b)[0] for b in assigned)
        if not atk_indestructible and (
            back >= atk_t or (back > 0 and any(_has(b, "deathtouch") and _pt(b)[0] > 0 for b in assigned))
        ):
            out.attacker_died = True
        return out

    # Damage marked on each blocker and on the attacker carries from the
    # first-strike step into the regular step (a double striker's first hit
    # counts toward lethal on its second).
    marked: dict[int, int] = {id(b): 0 for b in assigned}
    taken = 0
    returning_deathtouch = False

    def attacker_strikes() -> None:
        # A creature whose blockers are all gone deals no damage without trample (702.19e).
        if atk_lifelink and (atk_trample or any(b not in out.blockers_died for b in assigned)):
            out.life_gained += atk_p
        dmg_left = atk_p
        for blk in assigned:
            if blk in out.blockers_died:
                continue
            blk_t = _pt(blk)[1]
            lethal = (1 if marked[id(blk)] == 0 else 0) if atk_dth else max(0, blk_t - marked[id(blk)])
            take = min(dmg_left, lethal)
            marked[id(blk)] += take
            if take >= lethal and not _has(blk, "indestructible"):
                out.blockers_died.append(blk)
            dmg_left -= take
            if dmg_left <= 0:
                break
        if atk_trample and dmg_left > 0:
            out.damage_through += dmg_left

    def attacker_dead() -> bool:
        return not atk_indestructible and ((returning_deathtouch and taken > 0) or taken >= atk_t)

    # --- First-strike step: first and double strikers on both sides, simultaneously ---
    if atk_fs or fs_blockers:
        if atk_fs and atk_p > 0:
            attacker_strikes()
        taken += sum(_pt(b)[0] for b in fs_blockers)
        returning_deathtouch = any(_has(b, "deathtouch") and _pt(b)[0] > 0 for b in fs_blockers)
        if attacker_dead():
            out.attacker_died = True
            if out.damage_through > 0:
                out.damage_through += int(attacker.get("combat_hit_bonus") or 0)
            return out

    # --- Regular step: what has no first strike, and double strikers again ---
    alive = [b for b in assigned if b not in out.blockers_died]
    if atk_p > 0 and (atk_ds or not atk_fs):
        attacker_strikes()
    regular = [b for b in alive if not first_striker(b) or _has(b, "double strike")]
    taken += sum(_pt(b)[0] for b in regular)
    returning_deathtouch = returning_deathtouch or any(
        _has(b, "deathtouch") and _pt(b)[0] > 0 for b in regular
    )
    out.attacker_died = attacker_dead()
    if out.damage_through > 0:
        out.damage_through += int(attacker.get("combat_hit_bonus") or 0)
    return out


# --- Declare blockers solver -------------------------------------------


@dataclass
class BlockPlan:
    # blocker_instance_id -> attacker_instance_id (0 = do not block)
    assignments: dict[int, int] = field(default_factory=dict)
    damage_through: int = 0
    attackers_killed_material: int = 0
    blockers_lost_material: int = 0
    explanation: str = ""
    score: float = 0.0
    evaluated: int = 0  # block assignments scored (search cost)
    blockers_lost_ids: frozenset[int] = frozenset()
    recovery_credit: float = 0.0
    attacker_life_gained: int = 0  # the attacking side's lifelink gain under these blocks
    attackers_killed_ids: frozenset[int] = frozenset()
    # Expected material lost to a combat trick of the attacking player (TrickExposure).
    trick_cost: float = 0.0
    # The blocks in this plan that count on exact damage or two or more blockers into open mana.
    trick_risk: str = ""
    # The best plan before trick risk was priced, when trick risk rejected it.
    trick_rejected: str = ""


# --- Combat tricks after blocks (#74) -------------------------------------------
#
# 2026-10-19 T13 (match abde2a07): a lone Creakwood Safewright 5/5 attacked into
# our three untapped creatures with Swamp + Forest open and 4 cards in hand. The
# solver scored the triple block with exactly 5 power (2+2+1) at 0.0 against
# -6.67 for no block, at every life total from 8 to 20; the coach prompt called
# it "Computed optimal blocks" and the model followed. Scarblade's Malice
# (deathtouch + lifelink, {B}) killed all three blockers and gained them 5.
# A block that kills the attacker only by exact damage, or only with two or
# more blockers, is what a pump, a deathtouch grant or a removal spell on one
# blocker turns into a blowout. Its expected cost is charged when the attacking
# player has open mana and cards in hand, hardest for a lone attacker swinging
# into two or more untapped blockers: an attack that only makes sense with a trick.

# Chance one unknown card in hand is a castable combat trick.
TRICK_RATE_PER_CARD = 0.12
# One open mana casts fewer tricks than two.
ONE_MANA_TRICK_FACTOR = 0.6
# A lone attacker into two or more blockers that can kill it: half the remaining doubt.
LONE_ATTACKER_TRICK_WEIGHT = 0.5
TRICK_PROBABILITY_CAP = 0.9
# The trick shapes priced: a +2/+2 pump, and a deathtouch grant (Scarblade's Malice).
TRICK_PUMP = 2


@dataclass(frozen=True)
class TrickExposure:
    """What the attacking player has for a trick once blocks are declared."""

    open_mana: int
    hand_size: int

    @property
    def live(self) -> bool:
        return self.open_mana > 0 and self.hand_size > 0

    def describe(self) -> str:
        cards = "card" if self.hand_size == 1 else "cards"
        return f"{self.open_mana} open mana and {self.hand_size} {cards} in hand"


def trick_probability(exposure: TrickExposure | None, *, lone_attacker: bool = False) -> float:
    """Approximate chance the attacking player holds a castable combat trick."""
    if exposure is None or not exposure.live:
        return 0.0
    chance = 1.0 - (1.0 - TRICK_RATE_PER_CARD) ** min(exposure.hand_size, 7)
    if exposure.open_mana < 2:
        chance *= ONE_MANA_TRICK_FACTOR
    if lone_attacker:
        chance += (1.0 - chance) * LONE_ATTACKER_TRICK_WEIGHT
    return min(chance, TRICK_PROBABILITY_CAP)


def attacking_trick_exposure(state: dict | None, attacking_seat: Any = None) -> TrickExposure | None:
    """The attacking player's open mana and hand size from a planner-shape state; None when unknown.

    The attacker defaults to the opponent (we are the ones blocking). Mana is
    their untapped sources that can tap now plus their pool, mana that cannot
    cast spells left out (``opponent_tricks._mana_sources``).
    """
    if not isinstance(state, dict):
        return None
    players = [p for p in state.get("players") or [] if isinstance(p, dict)]
    local = state.get("local_seat_id") or next((p.get("seat_id") for p in players if p.get("is_local")), None)
    if attacking_seat is None:
        attacking_seat = state.get("opponent_seat_id") or next(
            (p.get("seat_id") for p in players if p.get("seat_id") not in (None, local)), None
        )
    if attacking_seat is None or attacking_seat == local:
        return None
    zones = state.get("zones") if isinstance(state.get("zones"), dict) else {}
    hand = next(
        (
            value
            for value in (zones.get("opponent_hand_count"), state.get("opponent_hand_count"))
            if isinstance(value, int) and not isinstance(value, bool)
        ),
        None,
    )
    if hand is None or hand < 0:
        return None
    try:
        from arenamcp.opponent_tricks import _mana_sources

        sources = _mana_sources(state, attacking_seat)
    except Exception as error:  # never let the trick estimate break a block search
        logger.debug("trick exposure: no mana sources (%s)", error)
        return None
    open_mana = sum(1 for source in sources if not getattr(source, "restricted", False))
    return TrickExposure(open_mana=open_mana, hand_size=hand)


def _with_keyword(card: dict, keyword: str) -> dict:
    return {**card, "keywords": [*(card.get("keywords") or ()), keyword]}


def _fight_value(attacker: dict, outcome: CombatOutcome) -> int:
    """Material the blocking side nets in one fight: the attacker if it dies, minus dead blockers."""
    killed = _material(attacker) if outcome.attacker_died else 0
    return killed - sum(_material(dead) for dead in outcome.blockers_died)


def _blowout_loss(attacker: dict, assigned: list[dict], outcome: CombatOutcome) -> int:
    """Material a trick of the attacker's controller costs this block; 0 when the block is not exposed.

    Exposed: the block kills the attacker, and it does so only by exact
    damage (one more toughness and it lives) or only with two or more
    blockers. The cost is the worst of a +2/+2 pump and a deathtouch grant
    against what the block was counted to net.
    """
    if not assigned or not outcome.attacker_died:
        return 0
    power, toughness = _pt(attacker)
    exact = not _resolve_attacker({**attacker, "toughness": toughness + 1}, assigned).attacker_died
    if len(assigned) < 2 and not exact:
        return 0
    planned = _fight_value(attacker, outcome)
    pumped = {**attacker, "power": power + TRICK_PUMP, "toughness": toughness + TRICK_PUMP}
    worst = min(
        _fight_value(attacker, _resolve_attacker(pumped, assigned)),
        _fight_value(attacker, _resolve_attacker(_with_keyword(attacker, "deathtouch"), assigned)),
    )
    return max(0, planned - worst)


def _can_block_this_attacker(attacker: dict, blocker: dict, allowed_ids: set[int] | None) -> bool:
    if allowed_ids is not None and int(attacker.get("instance_id") or 0) not in allowed_ids:
        return False
    return _can_block(attacker, blocker)


def optimal_blocks(
    attackers: list[dict],
    blockers: list[dict],
    your_life: int = 20,
    *,
    blocker_allowed_attackers: dict[int, set[int]] | None = None,
    max_options: int = 50_000,
    recovery_credit: Callable[[frozenset[int]], float] | None = None,
    trick_exposure: TrickExposure | None = None,
) -> BlockPlan | None:
    """Best block assignment (memoized; see `_search_blocks`)."""
    if recovery_credit is not None:
        # Recovery reads the whole live board, tax and surviving mana. Never
        # reuse the body-only cache for a stateful forecast.
        return _search_blocks(
            attackers,
            blockers,
            your_life,
            blocker_allowed_attackers=blocker_allowed_attackers,
            max_options=max_options,
            recovery_credit=recovery_credit,
            trick_exposure=trick_exposure,
        )
    allowed = (
        None
        if blocker_allowed_attackers is None
        else tuple(sorted((k, tuple(sorted(v))) for k, v in blocker_allowed_attackers.items()))
    )
    key = (
        "blocks",
        _cards_key(attackers),
        _cards_key(blockers),
        your_life,
        allowed,
        max_options,
        trick_exposure,
    )
    return _memoized(
        key,
        lambda: _search_blocks(
            attackers,
            blockers,
            your_life,
            blocker_allowed_attackers=blocker_allowed_attackers,
            max_options=max_options,
            trick_exposure=trick_exposure,
        ),
    )


def _search_blocks(
    attackers: list[dict],
    blockers: list[dict],
    your_life: int = 20,
    *,
    blocker_allowed_attackers: dict[int, set[int]] | None = None,
    max_options: int = 50_000,
    recovery_credit: Callable[[frozenset[int]], float] | None = None,
    trick_exposure: TrickExposure | None = None,
    symmetric: bool = False,
) -> BlockPlan | None:
    """Enumerate legal block assignments and pick the best.

    ``symmetric``: past ``max_options`` assignments, enumerate one assignment
    per permutation of interchangeable blockers (``_canonical_choices``)
    before falling back to the greedy plan (the attack search on big boards,
    ledger #162). Off by default: the other callers keep their greedy facts.

    `blocker_allowed_attackers` maps a blocker's instance_id to the set
    of attacker instance_ids it is allowed to block (from the GRE
    `DeclareBlockersReq.blockers[].attackerInstanceIds` list). When None,
    any attacker the blocker can legally block by keyword is allowed.

    `trick_exposure` (the attacking player's open mana and hand) charges each
    block that counts on exact damage or two or more blockers its expected
    blowout (``_blowout_loss`` times ``trick_probability``); None charges
    nothing (the opponent's view, forecasts, unknown hands).

    Returns None if there are no attackers.
    """
    if not attackers:
        return None

    # Cap search space — 4 attackers x 6 blockers = 5^6 = 15625 which is
    # cheap; but 8 attackers × 8 blockers = 9^8 ≈ 43M which is not. Fall
    # back to a greedy heuristic when combinatorial search would be slow.
    n_attackers = len(attackers)
    n_blockers = len(blockers)
    n_options = (n_attackers + 1) ** n_blockers

    # Pre-compute per-blocker option list: indices into `attackers` plus
    # "None" for no-block.
    blocker_options: list[list[int | None]] = []
    for blk in blockers:
        blk_iid = int(blk.get("instance_id") or 0)
        allowed = None
        if blocker_allowed_attackers is not None:
            allowed = blocker_allowed_attackers.get(blk_iid)
        opts: list[int | None] = [None]  # do not block
        for i, atk in enumerate(attackers):
            if _can_block_this_attacker(atk, blk, allowed):
                opts.append(i)
        blocker_options.append(opts)

    choices: Iterable[tuple] = ()
    if n_options <= max_options:
        choices = product(*blocker_options)
    else:
        # Big boards (Brawl token swarms, ledger #162): interchangeable blockers (thirteen 2/2
        # Squirrels) make most assignments permutations of each other. Enumerate one per
        # permutation class (``_canonical_choices``); only past the cap is the block greedy.
        groups = _symmetric_groups(blockers, blocker_options) if symmetric else []
        interchangeable = any(len(group) > 1 for group in groups)
        if not interchangeable or _canonical_count(groups, blocker_options) > max_options:
            return _greedy_block_plan(
                attackers,
                blockers,
                your_life,
                blocker_allowed_attackers=blocker_allowed_attackers,
                trick_exposure=trick_exposure,
            )
        choices = _canonical_choices(groups, blocker_options)

    # A lone attacker into two or more creatures that can block it.
    lone = n_attackers == 1 and sum(1 for opts in blocker_options if len(opts) > 1) >= 2
    trick_p = trick_probability(trick_exposure, lone_attacker=lone)

    menace = [_has(a, "menace") for a in attackers]
    # An attacker's fight depends only on which blockers it gets, and the
    # same (attacker, blockers) pairs recur across thousands of choices.
    fights: dict[tuple[int, tuple[int, ...]], tuple[int, int, int, frozenset[int], int]] = {}

    best_score: float | None = None
    best_choice: tuple = ()
    raw_best: float | None = None  # the same search with no trick priced
    raw_choice: tuple = ()
    evaluated = 0
    for choice in choices:
        # choice[b] is either None or an attacker index
        assigned_to_atk: dict[int, list[int]] = {}
        for b_idx, atk_idx in enumerate(choice):
            if atk_idx is None:
                continue
            assigned_to_atk.setdefault(atk_idx, []).append(b_idx)

        # Menace check — attackers with menace need >=2 blockers or none.
        if any(menace[atk_idx] and len(blks) == 1 for atk_idx, blks in assigned_to_atk.items()):
            continue

        evaluated += 1
        total_damage = 0
        atk_killed_material = 0
        blocker_lost_material = 0
        blowout = 0
        lost_ids: set[int] = set()
        for a_idx in range(n_attackers):
            key = (a_idx, tuple(assigned_to_atk.get(a_idx, ())))
            fight = fights.get(key)
            if fight is None:
                assigned = [blockers[b] for b in key[1]]
                outcome = _resolve_attacker(attackers[a_idx], assigned)
                fight = (
                    outcome.damage_through,
                    _material(attackers[a_idx]) if outcome.attacker_died else 0,
                    sum(_material(dead) for dead in outcome.blockers_died),
                    frozenset(dead.get("instance_id", 0) for dead in outcome.blockers_died),
                    _blowout_loss(attackers[a_idx], assigned, outcome) if trick_p > 0 else 0,
                )
                fights[key] = fight
            total_damage += fight[0]
            atk_killed_material += fight[1]
            blocker_lost_material += fight[2]
            lost_ids.update(fight[3])
            blowout += fight[4]

        score = _block_score(total_damage, atk_killed_material, blocker_lost_material, your_life)
        if recovery_credit is not None and total_damage < your_life:
            score += recovery_credit(frozenset(lost_ids))
        if raw_best is None or score > raw_best:
            raw_best = score
            raw_choice = choice
        score -= trick_p * blowout
        if best_score is None or score > best_score:
            best_score = score
            best_choice = choice

    # Rebuild the winner in full (plan, kills, explanation) once.
    plan = _rebuild_block_plan(
        attackers, blockers, best_choice, your_life, recovery_credit=recovery_credit, trick_p=trick_p
    )
    plan.evaluated = evaluated
    if plan.trick_cost > 0:
        plan.trick_risk = _trick_risk_note(attackers, blockers, best_choice, trick_exposure)
    if raw_choice != best_choice:
        rejected = _rebuild_block_plan(attackers, blockers, raw_choice, your_life, trick_p=trick_p)
        if rejected.trick_cost > 0:
            plan.trick_rejected = (
                f"{rejected.explanation}: {_trick_risk_note(attackers, blockers, raw_choice, trick_exposure)}"
            )
    return plan


def symmetry_key(card: dict) -> tuple:
    """A creature as the solver sees it, without its identity: interchangeable bodies (a
    swarm of identical Rabbit tokens) share it. Conservative: any field that differs keeps two
    bodies apart."""

    def plain(node: dict) -> tuple:
        return tuple(
            sorted((key, repr(value)) for key, value in node.items() if key not in ("instance_id", "_card"))
        )

    inner = card.get("_card")
    return plain(card), plain(inner) if isinstance(inner, dict) else ()


def _symmetric_groups(blockers: list[dict], options: list[list]) -> list[list[int]]:
    """Blocker indices in classes of interchangeable bodies with the same options, in order."""
    groups: dict[tuple, list[int]] = {}
    for index, blocker in enumerate(blockers):
        groups.setdefault((symmetry_key(blocker), tuple(options[index])), []).append(index)
    return list(groups.values())


def _canonical_count(groups: list[list[int]], options: list[list]) -> int:
    """Block assignments one per permutation class of interchangeable blockers."""
    from math import comb

    total = 1
    for group in groups:
        k = len(options[group[0]])
        total *= comb(len(group) + k - 1, len(group))
    return total


def _canonical_choices(groups: list[list[int]], options: list[list]) -> Iterable[tuple]:
    """Each block assignment up to permuting interchangeable blockers, once: within a class
    the chosen options never decrease (``combinations_with_replacement``)."""
    from itertools import combinations_with_replacement

    size = sum(len(group) for group in groups)
    per_group = [list(combinations_with_replacement(options[group[0]], len(group))) for group in groups]
    for picks in product(*per_group):
        choice: list = [None] * size
        for group, values in zip(groups, picks, strict=True):
            for index, value in zip(group, values, strict=True):
                choice[index] = value
        yield tuple(choice)


def block_search_size(
    attackers: list[dict], blockers: list[dict], max_options: int = 50_000, *, symmetric: bool = False
) -> int:
    """Assignments ``_search_blocks`` scores for these bodies (``symmetric``: canonical ones past
    the plain cap; greedy past the cap)."""
    n_options = (len(attackers) + 1) ** len(blockers)
    if n_options <= max_options:
        return n_options
    if not symmetric:
        return len(attackers) * len(blockers) + 1
    options = [
        [None] + [i for i, attacker in enumerate(attackers) if _can_block(attacker, blocker)]
        for blocker in blockers
    ]
    groups = _symmetric_groups(blockers, options)
    canonical = _canonical_count(groups, options)
    if any(len(group) > 1 for group in groups) and canonical <= max_options:
        return canonical
    return len(attackers) * len(blockers) + 1


def _rebuild_block_plan(
    attackers: list[dict],
    blockers: list[dict],
    choice: tuple,
    your_life: int,
    *,
    recovery_credit: Callable[[frozenset[int]], float] | None = None,
    trick_p: float = 0.0,
) -> BlockPlan:
    """One block assignment in full: plan, kills, losses, score (trick cost included) and explanation."""
    assigned_to_atk: dict[int, list[dict]] = {}
    for b_idx, atk_idx in enumerate(choice):
        if atk_idx is not None:
            assigned_to_atk.setdefault(atk_idx, []).append(blockers[b_idx])
    plan = BlockPlan(
        assignments={
            int(blockers[b_idx].get("instance_id") or 0): int(attackers[atk_idx].get("instance_id") or 0)
            for b_idx, atk_idx in enumerate(choice)
            if atk_idx is not None
        },
    )
    killed_attackers: list[dict] = []
    lost_blockers: list[dict] = []
    blowout = 0
    for a_idx, atk in enumerate(attackers):
        assigned = assigned_to_atk.get(a_idx, [])
        outcome = _resolve_attacker(atk, assigned)
        plan.damage_through += outcome.damage_through
        plan.attacker_life_gained += outcome.life_gained
        if assigned:
            if outcome.attacker_died:
                plan.attackers_killed_material += _material(atk)
                killed_attackers.append(atk)
            for dead in outcome.blockers_died:
                plan.blockers_lost_material += _material(dead)
                lost_blockers.append(dead)
            if trick_p > 0:
                blowout += _blowout_loss(atk, assigned, outcome)
    plan.score = _score_block_plan(plan, your_life)
    plan.blockers_lost_ids = frozenset(c.get("instance_id", 0) for c in lost_blockers)
    plan.attackers_killed_ids = frozenset(c.get("instance_id", 0) for c in killed_attackers)
    if recovery_credit is not None and plan.damage_through < your_life:
        plan.recovery_credit = recovery_credit(plan.blockers_lost_ids)
        plan.score += plan.recovery_credit
    plan.trick_cost = trick_p * blowout
    plan.score -= plan.trick_cost
    plan.explanation = _explain_block_plan(plan, attackers, blockers, choice, killed_attackers, lost_blockers)
    return plan


def _trick_risk_note(
    attackers: list[dict], blockers: list[dict], choice: tuple, exposure: TrickExposure | None
) -> str:
    """Why the blocks of ``choice`` are exposed to a trick, one clause per exposed attacker."""
    groups: dict[int, list[dict]] = {}
    for b_idx, atk_idx in enumerate(choice):
        if atk_idx is not None:
            groups.setdefault(atk_idx, []).append(blockers[b_idx])

    def killed(count: int, of: int) -> str:
        if of == 1:
            return "the blocker"
        return f"all {of} blockers" if count == of else f"{count} of the {of} blockers"

    notes = []
    for atk_idx, assigned in groups.items():
        attacker = attackers[atk_idx]
        outcome = _resolve_attacker(attacker, assigned)
        if _blowout_loss(attacker, assigned, outcome) <= 0:
            continue
        power, toughness = _pt(attacker)
        exact = not _resolve_attacker({**attacker, "toughness": toughness + 1}, assigned).attacker_died
        pumped = _resolve_attacker(
            {**attacker, "power": power + TRICK_PUMP, "toughness": toughness + TRICK_PUMP}, assigned
        )
        deathtouch = _resolve_attacker(_with_keyword(attacker, "deathtouch"), assigned)
        names = [str(card.get("name") or "a creature") for card in assigned]
        who = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"
        how = "exactly lethal damage" if exact else f"{len(assigned)} blockers"
        pump = f"a +{TRICK_PUMP}/+{TRICK_PUMP} pump"
        tricks = []
        if not pumped.attacker_died:
            kills = (
                f" and kills {killed(len(pumped.blockers_died), len(assigned))}"
                if pumped.blockers_died
                else ""
            )
            tricks.append(f"{pump} saves it{kills}")
        elif len(pumped.blockers_died) > len(outcome.blockers_died):
            tricks.append(f"{pump} kills {killed(len(pumped.blockers_died), len(assigned))}")
        if len(deathtouch.blockers_died) > len(outcome.blockers_died):
            tricks.append(f"a deathtouch trick kills {killed(len(deathtouch.blockers_died), len(assigned))}")
        notes.append(
            f"{who} on {attacker.get('name') or 'the attacker'} {power}/{toughness} needs {how}: "
            + (", ".join(tricks) if tricks else "a trick turns the fight")
        )
    if not notes:
        return ""
    held = f" (they have {exposure.describe()})" if exposure is not None else ""
    return "; ".join(notes) + held


def _block_score(
    damage_through: int, attackers_killed_material: int, blockers_lost_material: int, your_life: int
) -> float:
    """Score a block assignment.

    Nonlethal damage still spends a resource. Value life on the same scale
    as body/material points at a healthy total, increasing its cost as the
    remaining life shrinks. A resource creature can justify a small hit,
    but "not lethal yet" must not make a ten-point hit nearly free.
    """
    # If this block plan kills you, it's worst possible.
    life_after = your_life - damage_through
    if life_after <= 0:
        life_score = -1000.0
    else:
        # Normalize around 20 remaining life = one material point per life.
        # The old factor of 5 priced a 24 -> 14 hit at just 3.57, cheaper
        # than losing ANY mana creature regardless of the hit's magnitude.
        life_score = -damage_through * (20.0 / max(1, life_after))

    material_score = attackers_killed_material - blockers_lost_material
    return life_score + material_score


def _score_block_plan(plan: BlockPlan, your_life: int) -> float:
    return _block_score(
        plan.damage_through, plan.attackers_killed_material, plan.blockers_lost_material, your_life
    )


def _explain_block_plan(
    plan: BlockPlan,
    attackers: list[dict],
    blockers: list[dict],
    choice: tuple,
    killed_attackers: list[dict],
    lost_blockers: list[dict],
) -> str:
    parts: list[str] = []
    for b_idx, atk_idx in enumerate(choice):
        if atk_idx is None:
            continue
        blk = blockers[b_idx]
        atk = attackers[atk_idx]
        parts.append(f"{blk.get('name', '?')} blocks {atk.get('name', '?')}")
    block_desc = ", ".join(parts) if parts else "no blocks"
    kills = [a.get("name", "?") for a in killed_attackers]
    losses = [b.get("name", "?") for b in lost_blockers]
    tail = []
    if plan.damage_through > 0:
        tail.append(f"{plan.damage_through} dmg through")
    if kills:
        tail.append(f"kills {', '.join(kills)}")
    if losses:
        tail.append(f"loses {', '.join(losses)}")
    return f"{block_desc}" + (f" ({'; '.join(tail)})" if tail else "")


def _greedy_block_plan(
    attackers: list[dict],
    blockers: list[dict],
    your_life: int,
    *,
    blocker_allowed_attackers: dict[int, set[int]] | None,
    trick_exposure: TrickExposure | None = None,
) -> BlockPlan:
    """Fallback for large combat — one blocker per attacker, biggest attackers first.

    Each attacker gets the single blocker whose block scores best
    (``_block_score`` on that fight alone, at the life left after the
    attackers already handled): a free block beats a chump, a chump beats a
    hit that matters, and no block at all when every block loses more than
    the hit costs. The old rule took the cheapest body that could block and
    chump-blocked with it (2026-10-08: Rank Rat 1/1 died to Hallway Heckler
    2/3 while Prudent Fateseer 1/4 stood idle, and the line search read the
    0 damage as "their attacks are stopped"). Multi-blocks are not tried; an
    exactly lethal trade into open mana is charged its trick risk (#74).
    """
    trick_p = trick_probability(trick_exposure)
    sorted_atk = sorted(enumerate(attackers), key=lambda ia: -_pt(ia[1])[0])
    remaining_blockers = list(blockers)
    assignments: dict[int, int] = {}
    total_damage = 0
    atk_killed_material = 0
    blocker_lost_material = 0
    killed_attackers: list[dict] = []
    lost_blockers: list[dict] = []
    trick_cost = 0.0

    for _atk_idx, atk in sorted_atk:
        atk_iid = int(atk.get("instance_id") or 0)
        life_left = max(1, your_life - total_damage)
        unblocked = _resolve_attacker(atk, [])
        best: tuple[float, int, int, dict, CombatOutcome] | None = None
        for blk in remaining_blockers:
            blk_iid = int(blk.get("instance_id") or 0)
            allowed = (
                blocker_allowed_attackers.get(blk_iid) if blocker_allowed_attackers is not None else None
            )
            if not _can_block_this_attacker(atk, blk, allowed):
                continue
            outcome = _resolve_attacker(atk, [blk])
            killed = _material(atk) if outcome.attacker_died else 0
            lost = sum(_material(dead) for dead in outcome.blockers_died)
            score = _block_score(outcome.damage_through, killed, lost, life_left)
            if trick_p > 0:
                score -= trick_p * _blowout_loss(atk, [blk], outcome)
            # Ties: the cheapest surviving body, then the tougher one.
            key = (score, -_material(blk), _pt(blk)[1])
            if best is None or key > best[:3]:
                best = (*key, blk, outcome)
        no_block = _block_score(unblocked.damage_through, 0, 0, life_left)
        if best is None or best[0] <= no_block:
            total_damage += unblocked.damage_through
            continue
        chosen, outcome = best[3], best[4]
        remaining_blockers.remove(chosen)
        assignments[int(chosen.get("instance_id") or 0)] = atk_iid
        total_damage += outcome.damage_through
        if trick_p > 0:
            trick_cost += trick_p * _blowout_loss(atk, [chosen], outcome)
        if outcome.attacker_died:
            atk_killed_material += _material(atk)
            killed_attackers.append(atk)
        for dead in outcome.blockers_died:
            blocker_lost_material += _material(dead)
            lost_blockers.append(dead)

    plan = BlockPlan(
        assignments=assignments,
        damage_through=total_damage,
        attackers_killed_material=atk_killed_material,
        blockers_lost_material=blocker_lost_material,
        blockers_lost_ids=frozenset(c.get("instance_id", 0) for c in lost_blockers),
        attackers_killed_ids=frozenset(c.get("instance_id", 0) for c in killed_attackers),
        trick_cost=trick_cost,
    )
    plan.score = _score_block_plan(plan, your_life) - trick_cost
    plan.explanation = "greedy: " + ", ".join(
        f"{b.get('name', '?')} -> atk#{assignments[int(b.get('instance_id') or 0)]}"
        for b in blockers
        if int(b.get("instance_id") or 0) in assignments
    )
    no_blocks = BlockPlan(
        damage_through=sum(_resolve_attacker(attacker, []).damage_through for attacker in attackers)
    )
    no_blocks.score = _score_block_plan(no_blocks, your_life)
    if no_blocks.score >= plan.score:
        no_blocks.explanation = f"greedy: no blocks ({no_blocks.damage_through} dmg through)"
        return no_blocks
    return plan


# --- Declare attackers solver ------------------------------------------


@dataclass
class AttackPlan:
    # ordered list of attacker instance_ids
    attacker_ids: list[int] = field(default_factory=list)
    attacker_names: list[str] = field(default_factory=list)
    damage_through: int = 0  # expected damage to opponent after worst-case blocks
    worst_case_crackback: int = 0  # damage we'd take next opponent turn
    attackers_lost_material: int = 0
    blockers_killed_material: int = 0
    explanation: str = ""
    score: float = 0.0
    life_gained: int = 0  # our lifelink attackers' gain under their best blocks
    walker_crackback_cost: float = 0.0  # crackback that finishes one of our planeswalkers


# Attack searches with more candidates than this are "big" for the callers that only show the
# solver's pick (the sidebar's tactical line, the coach's main-phase prompt): they get
# ``QUICK_DEADLINE_S`` instead of the declaration's full 2 s (ledger #162/#182, 2026-10-19
# 15:50:04-15:50:24: 13-17 attackers against 31 creatures, every main-phase state change of
# ours spent the full 2.0 s on the coaching thread, five times in 20 s).
BIG_ATTACK_SEARCH = 12
# Attack searches with this many attackers or more score all-in and the crackback reserve first.
SEED_ATTACKERS = 8
QUICK_DEADLINE_S = 0.25


def display_deadline(attackers: int) -> float:
    """The attack-search deadline for a caller that only displays the pick: short on big boards."""
    return QUICK_DEADLINE_S if attackers > BIG_ATTACK_SEARCH else 2.0


def optimal_attacks(
    candidate_attackers: list[dict],
    opponent_blockers: list[dict],
    opponent_life: int,
    your_life: int,
    opponent_attackers_next_turn: list[dict],
    your_remaining_blockers: list[dict],
    budget: int = 200_000,
    deadline_s: float = 2.0,
    *,
    our_walker_loyalties: tuple[int, ...] = (),
    creature_died_this_turn: bool | None = None,
    caller: str = "",
) -> AttackPlan | None:
    """Best attack (memoized; see `_search_attacks`). ``caller`` names the asker in the deadline
    log; a deadline under a second is the caller's own cap (``display_deadline``) and logs at
    DEBUG."""
    key = (
        "attacks",
        _cards_key(candidate_attackers),
        _cards_key(opponent_blockers),
        opponent_life,
        your_life,
        _cards_key(opponent_attackers_next_turn),
        _cards_key(your_remaining_blockers),
        budget,
        # A search cut by a short deadline must not answer a caller that allows the full one.
        deadline_s,
        tuple(our_walker_loyalties),
        creature_died_this_turn,
    )
    extra: dict[str, Any] = {}
    if our_walker_loyalties:
        extra["our_walker_loyalties"] = tuple(our_walker_loyalties)
    if creature_died_this_turn is not None:
        extra["creature_died_this_turn"] = creature_died_this_turn
    if caller:
        extra["caller"] = caller
    return _memoized(
        key,
        lambda: _search_attacks(
            candidate_attackers,
            opponent_blockers,
            opponent_life,
            your_life,
            opponent_attackers_next_turn,
            your_remaining_blockers,
            budget,
            deadline_s,
            **extra,
        ),
    )


def _search_attacks(
    candidate_attackers: list[dict],
    opponent_blockers: list[dict],
    opponent_life: int,
    your_life: int,
    opponent_attackers_next_turn: list[dict],
    your_remaining_blockers: list[dict],
    budget: int,
    deadline_s: float,
    *,
    our_walker_loyalties: tuple[int, ...] = (),
    creature_died_this_turn: bool | None = None,
    caller: str = "",
) -> AttackPlan | None:
    """Pick the attacker subset that maximizes damage while surviving crackback.

    Worst-case model: assume the opponent blocks to minimize damage
    through using `optimal_blocks` from their perspective.

    Crackback: the creatures you DON'T attack with remain as blockers for
    the opponent's next turn. Opponent will attack with what they have
    (including the candidates still on their board plus next-turn
    attackers we model), and we block optimally with what's left.

    Past ``deadline_s`` the search stops (the crackback reserve's own
    computation included). A big board's seeds (all-in, all-in less the
    reserve) are scored first; when the deadline stops the search with "attack
    with nobody" ahead of them only by the no-attack tie-break, the best seed is
    returned instead (2026-10-19 15:50:04: "best so far: attack with nobody",
    tied with the all-in at -1000 and 0 through, with the opponent at 5).
    """
    if not candidate_attackers:
        return None

    n = len(candidate_attackers)
    best: AttackPlan | None = None
    best_mask = 0
    # Every subset runs two block searches; 2026-09-24 a 7-attacker board
    # spent 69s here on the coaching thread. The budget caps the block
    # assignments scored across all subsets: the most informative subsets
    # go first, and a subset whose searches no longer fit is skipped rather
    # than scored with a cruder model than its rivals.
    remaining = budget
    started = time.monotonic()
    extra: dict[str, Any] = {}
    if our_walker_loyalties:
        extra["our_walker_loyalties"] = our_walker_loyalties
    if creature_died_this_turn is not None:
        extra["creature_died_this_turn"] = creature_died_this_turn

    seeds: list[tuple[int, ...]] = []
    if n >= SEED_ATTACKERS:
        # Big boards (ledger #162, 2026-10-19 15:05:42: 14 attackers, the 2 s deadline hit after
        # "attack with nobody" and a few one-creature attacks): everyone, and everyone but the
        # weakest bodies their crackback needs us to keep home, are scored before anything else.
        # From eight on (#188, match 9a6ea9be T14: exactly eight attackers were never seeded).
        seeds = [tuple(range(n))]
        reserve = _crackback_reserve(
            candidate_attackers,
            your_remaining_blockers,
            opponent_attackers_next_turn,
            your_life,
            deadline=started + deadline_s,
        )
        if reserve is not None:
            seeds.append(reserve)
    seed_masks = {sum(1 << i for i in seed) for seed in seeds}
    seeded: list[tuple[AttackPlan, int]] = []
    everyone = (1 << n) - 1
    hold_plan: AttackPlan | None = None
    all_in_plan: AttackPlan | None = None
    for subset in _attack_order(candidate_attackers, seeds):
        if seeds and best is not None and best.score >= 10_000.0:
            break  # a big board's all-in (the first seed) is lethal through their best blocks
        # Wall-clock backstop for slow machines: the budget alone keeps a
        # normal board well under a second.
        if best is not None and time.monotonic() - started > deadline_s:
            best, best_mask = _deadline_pick(best, best_mask, seeded)
            logger.log(
                logging.WARNING if deadline_s >= 1.0 else logging.DEBUG,
                "attack search%s hit its %gs deadline with %d attackers; best so far: %s",
                f" ({caller})" if caller else "",
                deadline_s,
                n,
                best.explanation,
            )
            break
        attacking = [candidate_attackers[i] for i in subset]
        held_back = [candidate_attackers[i] for i in range(n) if i not in set(subset)]
        our_defenders = held_back + your_remaining_blockers
        # The crackback search may run twice (combat_strategy.guarded_block_plan
        # re-runs it once without the chumps the chump guard refuses).
        cost = block_search_size(attacking, opponent_blockers, symmetric=True) + 2 * block_search_size(
            opponent_attackers_next_turn, our_defenders
        )
        if cost > remaining:
            continue
        remaining -= cost
        plan = evaluate_attack(
            attacking,
            our_defenders,
            opponent_blockers,
            opponent_life,
            your_life,
            opponent_attackers_next_turn,
            **extra,
        )
        # Ties go to the lowest bitmask (no attack first), whatever order
        # the subsets were searched in. When every attack dies to their crackback anyway
        # (-1000), the one dealing the most damage now is the better loss (board_assessment's
        # ALL-IN: holding blockers back changes nothing).
        mask = sum(1 << i for i in subset)
        if mask in seed_masks:
            seeded.append((plan, mask))
        if best is None or _attack_rank(plan, mask) > _attack_rank(best, best_mask):
            best = plan
            best_mask = mask
        if mask == 0:
            hold_plan = plan
        if mask == everyone:
            all_in_plan = plan
        if _every_attack_doomed(
            hold_plan,
            all_in_plan,
            candidate_attackers,
            opponent_blockers,
            opponent_attackers_next_turn,
            your_life,
        ):
            # Ledger #188 (match 9a6ea9be T14, 16:03:47): holding everything home died to their
            # crackback (44 against our 7) and so did everyone attacking; each single attack then
            # re-ran their sixteen attackers into our blockers (0.28 s each) until the 2 s
            # deadline. Every attack is doomed and none gets more through than all of them, so
            # the full search's answer is already known: all-in when it gets damage through,
            # else nobody (``_attack_rank``).
            break

    return best


def _every_attack_doomed(
    hold: AttackPlan | None,
    all_in: AttackPlan | None,
    candidates: list[dict],
    blockers: list[dict],
    crackers: list[dict],
    your_life: int,
) -> bool:
    """Holding and attacking with everyone are both doomed (-1000), and so is every other attack.

    An attack keeps fewer of ours home than holding, so its crackback is at least holding's
    less what its combat removes from their next attack: at most the power of their creatures
    that can die blocking one of ours (toughness within our biggest attacker's power, or any
    against deathtouch), against at most our lifelink attackers' power in life. No attack is
    lethal when the all-in is not (their best blocks against all of ours stop at least as much of
    any part of it). When even that best case is lethal for them on their turn, every attack
    scores -1000 and the all-in gets the most through.
    """
    if hold is None or all_in is None or hold.score > -1_000.0 or all_in.score > -1_000.0:
        return False
    attacking = [c for c in candidates if _pt(c)[0] > 0]
    if not attacking:
        return True
    strongest = max(_pt(c)[0] for c in attacking)
    deathtouch = any(_has(c, "deathtouch") for c in attacking)
    next_attackers = {c.get("instance_id") for c in crackers if c.get("instance_id")}
    removable = sum(
        max(0, _pt(b)[0])
        for b in blockers
        if b.get("instance_id") in next_attackers
        and (deathtouch or _pt(b)[1] - int(b.get("damage") or 0) <= strongest)
    )
    gained = sum(_pt(c)[0] for c in attacking if _has(c, "lifelink"))
    return hold.worst_case_crackback - removable >= your_life + gained


def gang_block_kill(
    attacker: dict, blockers: list[dict], allowed: dict[int, set[int]] | None = None
) -> tuple[list[dict], int] | None:
    """(``blockers`` whose damage together kills ``attacker``, how many of them it can kill), or
    None: an attacker with first or double strike, indestructible or protection (the block may
    not kill it), or not enough legal blockers. ``allowed``: Arena's blocker id -> attacker ids it
    may block. Of the cheapest blockers first and the strongest first, the set whose losses (the
    attacker killing the most valuable it can, ``_material``) are smaller; a deathtouch blocker
    is lethal alone.

    Ledger #172 (match 2b77f14d T7): three untapped Hare Apparents (2/2) could have killed The
    Unbeatable Squirrel Girl (5/5) before her doubling turn; we chumped with Rabbits.
    """
    text = _plain_rules(attacker)
    if (
        any(_has(attacker, k) for k in ("first strike", "double strike", "indestructible"))
        or "protection" in text
    ):
        return None
    iid = attacker.get("instance_id")
    legal = [
        b
        for b in blockers
        if _pt(b)[0] > 0
        and _can_block(attacker, b)
        and (allowed is None or b.get("instance_id") not in allowed or iid in allowed[b.get("instance_id")])
    ]
    need = _pt(attacker)[1] - int(attacker.get("damage") or 0)

    def pick(order: list[dict]) -> list[dict] | None:
        chosen, dealt = [], 0
        for blocker in order:
            if dealt >= need:
                break
            chosen.append(blocker)
            dealt += need if _has(blocker, "deathtouch") else _pt(blocker)[0]
        if dealt < need or not chosen or (_has(attacker, "menace") and len(chosen) < 2):
            return None
        return chosen

    def losses(chosen: list[dict]) -> tuple[int, list[dict]]:
        power, lost = _pt(attacker)[0], []
        for blocker in sorted(chosen, key=lambda b: (-_material(b), _pt(b)[1])):
            cost = 1 if _has(attacker, "deathtouch") else max(1, _pt(blocker)[1])
            if power >= cost:
                power -= cost
                lost.append(blocker)
        return sum(_material(b) for b in lost), lost

    def key(b: dict) -> int:
        return b.get("instance_id") or 0

    orders = (
        sorted(legal, key=lambda b: (_material(b), -_pt(b)[0], key(b))),
        sorted(legal, key=lambda b: (-_pt(b)[0], _material(b), key(b))),
    )
    sets = [found for order in orders if (found := pick(order)) is not None]
    if not sets:
        return None
    best = min(sets, key=lambda s: (losses(s)[0], len(s)))
    return best, len(losses(best)[1])


def next_turn_engines(state: dict, crackers: list[dict], alive: list[dict]) -> tuple[list[dict], int]:
    """(``crackers`` grown for their next turn, their drains that turn) on this snapshot.

    Their creatures that grow on their life gain or draws (``line_search_moves.TheirEngines``:
    Unflinching Hortimancer and Ajani's Pridemate on Ajani Resolute's life gain) are bigger when
    they attack, and their drain engines (Biotech Specialist) deal us their damage whatever we
    block; ``alive``: their creatures still there after our attack. Ledger #128 (match
    3e545401 T17: the crackback guard counted 10 through, the real attack was 12 into 12)."""
    from arenamcp.board_model import engines_of, their_draws
    from arenamcp.combat_strategy import _controller as controller

    battlefield = [c for c in state.get("battlefield") or [] if isinstance(c, dict)]
    players = [p for p in state.get("players") or [] if isinstance(p, dict)]
    local = next((p.get("seat_id") for p in players if p.get("is_local")), None)
    opponent = next((p.get("seat_id") for p in players if not p.get("is_local")), None)
    engines = engines_of(battlefield, local, opponent, draws=their_draws(state))
    if engines is None:
        return crackers, 0
    gains = engines.gain_events()
    grown = []
    for card in crackers:
        n = engines.growth(card, gains=gains) if controller(card) == opponent else 0
        power, toughness = _pt(card)
        grown.append({**card, "power": power + n, "toughness": toughness + n} if n > 0 else card)
    drain = engines.static_drain + sum(
        engines.body_drain(c, gains=gains) for c in alive if controller(c) == opponent
    )
    # Their token engine at our end step with the mana they have up (#172): those tokens attack.
    budget, made = engines.mana_now, 0
    for card in alive:
        if controller(card) != opponent or made >= 8:
            continue
        count, power, toughness = engines.body_tokens(card, mana=budget, activated=True)
        for index in range(min(count, 8 - made)):
            grown.append({
                "instance_id": 650_000_000 + made + index, "name": f"{card.get('name') or 'their'} token",
                "power": power, "toughness": toughness, "controller_seat_id": opponent, "oracle_text": "",
                "type_line": "Token Creature",
            })  # fmt: skip
        made += min(count, 8 - made)
    return grown, drain


def _attack_rank(plan: AttackPlan, mask: int) -> tuple:
    doomed = plan.score <= -1_000.0
    return (plan.score, plan.damage_through if doomed else 0, -mask)


def _deadline_pick(
    best: AttackPlan, best_mask: int, seeded: list[tuple[AttackPlan, int]]
) -> tuple[AttackPlan, int]:
    """At the deadline: "attack with nobody" ahead of the scored seeds only by the no-attack
    tie-break (same score, same damage when doomed) gives way to the best seed. A finished search
    keeps the tie-break (``test_big_board_combat_20261019``: nobody, not a timeout)."""
    if best_mask != 0 or not seeded:
        return best, best_mask
    plan, mask = max(seeded, key=lambda item: _attack_rank(*item))
    if _attack_rank(plan, mask)[:2] >= _attack_rank(best, best_mask)[:2]:
        return plan, mask
    return best, best_mask


def _crackback_reserve(
    candidates: list[dict],
    remaining: list[dict],
    crackers: list[dict],
    your_life: int,
    *,
    deadline: float | None = None,
) -> tuple[int, ...] | None:
    """Everyone attacks but the fewest weakest bodies (least power, then most toughness) whose
    staying home keeps their crackback below our life; None when even keeping all home dies, or
    when the search's ``deadline`` (a ``time.monotonic`` instant) passes first (15:50:04: 0.55 s
    of crackback block searches, 31 of their creatures against our 13)."""
    from arenamcp.combat_strategy import guarded_block_plan

    def crackback(home: list[dict]) -> int:
        plan = guarded_block_plan(crackers, home, your_life) if crackers else None
        return plan.damage_through if plan else sum(_pt(a)[0] for a in crackers)

    order = sorted(range(len(candidates)), key=lambda i: (_pt(candidates[i])[0], -_pt(candidates[i])[1], i))
    home: list[int] = []
    step = 0.0  # the last crackback search's time: the next one is about as long
    for i in [None, *order]:
        now = time.monotonic()
        if deadline is not None and now + step > deadline:
            return None
        if i is not None:
            home.append(i)
        if crackback([candidates[j] for j in home] + list(remaining)) < your_life:
            return tuple(j for j in range(len(candidates)) if j not in home)
        step = time.monotonic() - now
    return None


# Distinct attacks up to swapping interchangeable attackers scored on a big board (#162).
_MAX_CLASS_SUBSETS = 1024


def _attack_order(candidates: list[dict], seeds: list[tuple[int, ...]]) -> Iterable[tuple[int, ...]]:
    """``seeds`` first, then ``_attack_subsets``; past 8 attackers, every attack up to swapping
    interchangeable bodies (how many of each class attack, ``symmetry_key``) when there are at
    most ``_MAX_CLASS_SUBSETS`` of them, most attackers first."""
    n = len(candidates)
    seen: set[tuple[int, ...]] = set()

    def fresh(subsets: Iterable[tuple[int, ...]]) -> Iterable[tuple[int, ...]]:
        for subset in subsets:
            subset = tuple(sorted(subset))
            if subset not in seen:
                seen.add(subset)
                yield subset

    yield from fresh(seeds)
    groups: dict[tuple, list[int]] = {}
    for index, card in enumerate(candidates):
        groups.setdefault(symmetry_key(card), []).append(index)
    classes = list(groups.values())
    count = 1
    for group in classes:
        count *= len(group) + 1
    if n <= 8 or count > _MAX_CLASS_SUBSETS:
        yield from fresh(_attack_subsets(n))
        return
    yield from fresh([()])
    vectors = product(*(range(len(group), -1, -1) for group in classes))
    yield from fresh(
        tuple(i for group, k in zip(classes, vector, strict=True) for i in group[:k]) for vector in vectors
    )


def evaluate_attack(
    attacking: list[dict],
    our_defenders: list[dict],
    opponent_blockers: list[dict],
    opponent_life: int,
    your_life: int,
    opponent_attackers_next_turn: list[dict],
    *,
    our_walker_loyalties: tuple[int, ...] = (),
    creature_died_this_turn: bool | None = None,
    state: dict | None = None,
) -> AttackPlan:
    """Score one attack: `our_defenders` are the creatures left home to block.

    ``state`` (the snapshot): their next turn's engines count in the crackback
    (``next_turn_engines``: their creatures grow first, their drains add to it).

    ``our_walker_loyalties``: our planeswalkers' loyalty. Their crackback may
    finish one instead of hitting us (``walker_crackback_cost``).

    The crackback is their next attack as we would really meet it: defenders
    that leave at our end step (``leaves_at_our_end_step``) and attackers of
    ours that die in this combat do not block; their creatures that die in it,
    or cannot attack next turn (``can_attack_next_turn``: a tapped "doesn't
    untap" creature unless a morbid untap applies; ``creature_died_this_turn``
    None means unknown), do not attack; and we do not chump where the chump
    guard would refuse to (``combat_strategy.guarded_block_plan``).
    """
    # Opponent blocks optimally — from THEIR perspective, lower
    # "damage_through" is good; we invert by using their life.
    opp_block = _search_blocks(attacking, opponent_blockers, opponent_life, symmetric=True)
    if opp_block is None:
        unblocked = [_resolve_attacker(a, []) for a in attacking]
        damage_through = sum(o.damage_through for o in unblocked)
        life_gained = sum(o.life_gained for o in unblocked)
        atk_lost_mat = 0
        blk_killed_mat = 0
    else:
        damage_through = opp_block.damage_through
        life_gained = opp_block.attacker_life_gained
        # optimal_blocks was called with OUR creatures as `attackers`
        # and THEIRS as `blockers`, so the roles carry over directly:
        # attackers killed = our attackers lost; blockers lost = their
        # creatures we killed. (These were swapped before 2026-06-09,
        # which made suicidal attacks score as profitable trades.)
        atk_lost_mat = opp_block.attackers_killed_material
        blk_killed_mat = opp_block.blockers_lost_material

    # Crackback — the creatures we didn't attack with, plus whatever
    # opponent will attack with next turn.
    killed = opp_block.blockers_lost_ids if opp_block is not None else frozenset()
    lost = opp_block.attackers_killed_ids if opp_block is not None else frozenset()
    died = True if (atk_lost_mat > 0 or blk_killed_mat > 0) else creature_died_this_turn
    crackers = [
        card
        for card in opponent_attackers_next_turn
        if not (card.get("instance_id") and card.get("instance_id") in killed)
        and can_attack_next_turn(card, creature_died=died)
    ]
    defenders = [
        card
        for card in our_defenders
        if not leaves_at_our_end_step(card)
        and not (card.get("instance_id") and card in attacking and card.get("instance_id") in lost)
    ]
    from arenamcp.combat_strategy import guarded_block_plan

    drain = 0
    if state is not None:
        survivors = [
            c
            for c in opponent_attackers_next_turn
            if not (c.get("instance_id") and c.get("instance_id") in killed)
        ]
        crackers, drain = next_turn_engines(state, crackers, survivors)
    crack_block = guarded_block_plan(crackers, defenders, your_life)
    crackback = (crack_block.damage_through if crack_block else sum(_pt(a)[0] for a in crackers)) + drain

    plan = AttackPlan(
        attacker_ids=[int(a.get("instance_id") or 0) for a in attacking],
        attacker_names=[a.get("name", "?") for a in attacking],
        damage_through=damage_through,
        worst_case_crackback=crackback,
        attackers_lost_material=atk_lost_mat,
        blockers_killed_material=blk_killed_mat,
        life_gained=life_gained,
    )
    plan.score = _score_attack_plan(plan, your_life, opponent_life)
    walker_cost = walker_crackback_cost(crackback, your_life + life_gained, our_walker_loyalties)
    if walker_cost and plan.score > -1_000.0:
        plan.score -= walker_cost
        plan.walker_crackback_cost = walker_cost
    plan.explanation = (
        f"attack with {', '.join(plan.attacker_names) or 'nobody'}; "
        f"{damage_through} through, crackback {crackback} "
        f"(lose material {atk_lost_mat}, kill {blk_killed_mat})"
    )
    return plan



def _attack_subsets(n: int) -> Iterable[tuple[int, ...]]:
    """Attacker subsets, most informative first.

    No attack, all-in, each solo, all-but-one; then (N <= 8) every other
    subset in bitmask order. The attack budget may stop the walk early.
    """
    everyone = tuple(range(n))
    seen: set[tuple[int, ...]] = set()
    firsts = [(), everyone, *((i,) for i in range(n)), *(everyone[:i] + everyone[i + 1 :] for i in range(n))]
    for subset in firsts:
        if subset not in seen:
            seen.add(subset)
            yield subset
    if n > 8:
        return
    for mask in range(1 << n):
        subset = tuple(i for i in range(n) if mask & (1 << i))
        if subset not in seen:
            yield subset


def walker_value(loyalty: int) -> float:
    """A planeswalker on the battlefield, on combat_strategy's scale for removing theirs."""
    return 9.0 + min(loyalty, 6)


def walker_crackback_cost(crackback: int, your_life: int, loyalties: Iterable[int]) -> float:
    """How much worse ``crackback`` is when it can finish one of our planeswalkers.

    The opponent sends what kills the walker at it and the rest at us, when
    that costs us more than all of it at our face (2026-10-18 pro game, match 1
    turn 13: the Beast stayed home to keep Garruk, Curse Breaker alive).
    0 when no walker of ours is within reach.
    """

    def face(damage: int) -> float:
        return damage * 5.0 / max(1, your_life - damage)

    base = face(crackback)
    worst = base
    for loyalty in loyalties:
        if 0 < loyalty <= crackback:
            worst = max(worst, walker_value(loyalty) + face(crackback - loyalty))
    return worst - base


def _score_attack_plan(plan: AttackPlan, your_life: int, opponent_life: int) -> float:
    """Attack: damage to opponent is great; dying to crackback is worst."""
    # Lethal this swing is the best possible outcome.
    if plan.damage_through >= opponent_life:
        return 10_000.0

    # Lifelink damage this swing is life we have for their crackback (Dina, Flesh Sower).
    your_life += plan.life_gained
    # Dying to crackback is the worst.
    if plan.worst_case_crackback >= your_life:
        return -1_000.0

    # Weight damage dealt by how close it gets us to lethal.
    opp_pressure = plan.damage_through * (5.0 / max(1, opponent_life - plan.damage_through))
    our_pressure = -plan.worst_case_crackback * (5.0 / max(1, your_life - plan.worst_case_crackback))
    material = plan.blockers_killed_material - plan.attackers_lost_material
    # Penalize attacking with creatures that deal 0 damage and kill 0 material (wastes blocker/taps mana dork)
    waste_penalty = 0.0
    if plan.attacker_names and plan.damage_through == 0 and plan.blockers_killed_material == 0:
        waste_penalty = -2.0 * len(plan.attacker_names)
    return opp_pressure + our_pressure + material + waste_penalty


# --- Game-state adapters -----------------------------------------------


def blocker_allowed_attackers_map(
    raw_blockers: list[dict],
) -> dict[int, set[int]]:
    """Map blocker_instance_id -> allowed attacker_instance_id set."""
    out: dict[int, set[int]] = {}
    for blk in raw_blockers or []:
        bid = blk.get("blockerInstanceId") or blk.get("instanceId")
        try:
            bid = int(bid)
        except (TypeError, ValueError):
            continue
        ids = set()
        for aid in blk.get("attackerInstanceIds") or []:
            try:
                ids.add(int(aid))
            except (TypeError, ValueError):
                continue
        out[bid] = ids
    return out


def selected_blocks(raw_blockers: list[dict]) -> dict[int, set[int]]:
    """Blocks the request already shows selected, as blocker_instance_id -> attacker ids."""
    out: dict[int, set[int]] = {}
    for blk in raw_blockers or []:
        if not isinstance(blk, dict):
            continue
        try:
            bid = int(blk.get("blockerInstanceId") or blk.get("instanceId"))
            ids = {int(aid) for aid in blk.get("selectedAttackerInstanceIds") or []}
        except (TypeError, ValueError):
            continue
        if ids:
            out[bid] = ids
    return out


def blocker_options(raw_blockers: list[dict], remembered: dict | None = None) -> dict[int, set[int]]:
    """Every attacker each blocker may block in this declaration, its current selection included.

    A selected blocker at maxAttackers lists no attackerInstanceIds (Player.log
    2026-10-19 gsid 224: Rabbit 484 kept on 468 after a rejected declaration;
    gsid 225: all seven blockers). Read as-is, "nobody can block" (the 07:35:15
    watchdog declared "[No Blocks]" and committed seven blockers). Its options
    are what it blocks plus what this combat's earlier requests listed for it
    (``remembered``: decision_context["blocker_options"], kept by the log
    parser); a selection can be withdrawn or moved like Arena's own UI does.
    """
    out = blocker_allowed_attackers_map(raw_blockers)
    remembered = remembered or {}
    for bid, selection in selected_blocks(raw_blockers).items():
        earlier = remembered.get(bid, remembered.get(str(bid))) or ()
        out[bid] = out.get(bid, set()) | selection | {int(aid) for aid in earlier}
    return out
