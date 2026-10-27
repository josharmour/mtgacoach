"""When our burn waits and when it goes: the holds, and the release at their end step.

Mono-Red Burn, 2026-10-19 (autopilot-improvements.md #80, #136, #137, #139,
#140). b720163 (12:42) added the #80 hold: in our main phase, a non-lethal
instant burn spell that kills nothing came off the menu, to be cast at the
opponent's end step or in response. The holds helped tactically (9 of 11
later killed a creature), but the release never happened: Arena offered five
opponent end-step windows with a held instant castable (56fc394b T7, T9, T11
twice; 59064e41 T2), and all five passed, by the model's choice or through
the outage fallback, which only drops a land or passes outside our main
phase. Instant burn at the face fell from 63% to 12% of casts (3 of 26), the
opponent's life after our fifth turn rose from 7.1 to 10.1, and the record
went 4-7 against 10-6 before.

Everything here is deterministic (no model), so it also decides while the
model is down:

- :func:`end_step_release` (#137): at the opponent's end step with an empty
  stack, the burn to cast with mana that untaps unused otherwise. A kill the
  line search makes with it on our next turn goes first, then the most face
  damage the mana pays for. A spell stays in hand when the best line for our
  next turn needs it (a kill it aims at, or a win), or when it can kill a
  creature the line does not aim it at.
- :func:`hold_overrides` (#80 re-scoped): a main-phase hold does not apply
  when a cast now pumps an attacker this turn, their life is within reach
  over this turn and the next, or we have more mana than the end step can
  spend on the held spells.
- :func:`sweeper_hold` (#136): a sorcery that damages each of their
  creatures, kills nothing now and pumps nothing waits.
- :func:`own_stack_line_waits` (#139): with only our own spell on the stack,
  the best line's sorcery-speed plays that wait for it to resolve.
- :func:`upkeep_holds` (#140): damage-only instants wait for our main phase;
  :func:`combat_holds`: in our combat they wait for their end step unless
  they kill a creature that matters in this combat (633f56bd T7).
- :class:`HoldFates`: logs what became of each hold ("hold-fate: Shock (held
  T4) -> released at end step at face"), so the policy can be measured.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

logger = logging.getLogger(__name__)

# Damage a spell can deal to the opponent's face (the unkicked amount).
_FACE_RE = re.compile(
    r"deals? (\d+) damage to (?:any target|target player or planeswalker|target player|target opponent"
    r"|each opponent)",
    re.IGNORECASE,
)
# Damage to each of their creatures: Tectonic Hazard, Iroh's Demonstration's first mode.
_SWEEP_RE = re.compile(
    r"deals? \d+ damage to (?:each opponent and )?each creature"
    r"(?: (?:they control|your opponents control|an opponent controls))?",
    re.IGNORECASE,
)
# Anything a spell does besides damage (a damage-only instant has none of it).
_OTHER_EFFECT_RE = re.compile(
    r"\b(?:draw|create|counter target|return|search|exile|destroy|gain control|untap|"
    r"gets? [+-]\d|gains?|discard|sacrifice|copy)\b",
    re.IGNORECASE,
)
_REMINDER_RE = re.compile(r"\([^)]*\)")
_KICKER_LINE_RE = re.compile(r"\bkicker\b[^\n.]*", re.IGNORECASE)


# --- card facts -----------------------------------------------------------------------------------


def _text(card: dict) -> str:
    return _REMINDER_RE.sub(" ", str(card.get("oracle_text") or ""))


def _types(card: dict) -> str:
    types = " ".join(str(t) for t in (card.get("card_types") or card.get("types") or []))
    return f"{types} {card.get('type_line') or ''}".lower()


def _cost(card: dict) -> int:
    from arenamcp.decision_memory import _cmc

    return _cmc(card.get("mana_cost"))


def _x_spell(card: dict) -> bool:
    return "{x}" in str(card.get("mana_cost") or "").lower()


def instant_speed(card: dict) -> bool:
    """An instant or a flash card (``mulligan_policy.hand_card``)."""
    from arenamcp.mulligan_policy import hand_card

    try:
        return bool(card.get("type_line") or card.get("card_types")) and hand_card(card).instant_speed
    except Exception:
        return False


def face_damage(card: dict) -> int:
    """The damage this spell can deal to the opponent (unkicked), 0 when it cannot reach them."""
    return max((int(n) for n in _FACE_RE.findall(_text(card))), default=0)


def is_sweeper(card: dict) -> bool:
    """Damage to each of their creatures (Tectonic Hazard)."""
    return bool(_SWEEP_RE.search(_text(card)))


_SWEEP_AMOUNT_RE = re.compile(r"deals? (\d+) damage to (?:each opponent and )?each creature", re.IGNORECASE)


def sweep_chip(card: dict, creatures: int) -> int:
    """The sweep half's chip weight (#228): its damage to each of their creatures times how many
    they control. Chipping their board down is race progress like face damage is — it was invisible
    to the hold math (Tectonic Hazard's 1, Iroh's Demonstration's mode 1), so the holds froze while
    their board escalated. 0 without their creatures (an empty board is #136's face-only waste)."""
    if creatures <= 0:
        return 0
    amount = max((int(n) for n in _SWEEP_AMOUNT_RE.findall(_text(card))), default=0)
    return amount * creatures


def chip_damage(card: dict, state: dict) -> int:
    """Damage one cast of ``card`` deals right now even though it kills nothing (#228): its face
    half plus its sweep half on each of their creatures."""
    creatures = sum(1 for c in _theirs(state) if "creature" in _types(c))
    return face_damage(card) + sweep_chip(card, creatures)


def damage_only(card: dict, state: dict) -> bool:
    """A noncreature spell whose whole effect is damage (Shock, Lightning Strike, Burst Lightning)."""
    from arenamcp.decision_memory import burn_reach

    if "creature" in _types(card) or _x_spell(card):
        return False
    text = _KICKER_LINE_RE.sub(" ", _text(card))
    if _OTHER_EFFECT_RE.search(text):
        return False
    return face_damage(card) > 0 or burn_reach(str(card.get("oracle_text") or ""), card, state) is not None


# --- seats and turn -------------------------------------------------------------------------------


def _local_seat(state: dict) -> Any:
    seat = state.get("local_seat_id")
    if seat is None:
        seat = next(
            (
                p.get("seat_id")
                for p in state.get("players") or []
                if isinstance(p, dict) and p.get("is_local")
            ),
            None,
        )
    return seat


def _controller(card: dict) -> Any:
    return card.get("controller_seat_id") or card.get("owner_seat_id")


def _our_turn(state: dict) -> bool:
    local = _local_seat(state)
    return local is not None and (state.get("turn") or {}).get("active_player") == local


def their_life(state: dict) -> int | None:
    for player in state.get("players") or []:
        if isinstance(player, dict) and not player.get("is_local") and type(player.get("life_total")) is int:
            return player["life_total"]
    return None


def _theirs(state: dict) -> list[dict]:
    local = _local_seat(state)
    return [
        c for c in state.get("battlefield") or [] if local is not None and _controller(c) not in (None, local)
    ]


def _ours(state: dict) -> list[dict]:
    local = _local_seat(state)
    return [c for c in state.get("battlefield") or [] if local is not None and _controller(c) == local]


def stack_is_ours(state: dict) -> bool:
    """Whether everything on the stack (possibly nothing) is ours."""
    local = _local_seat(state)
    return all(isinstance(entry, dict) and _controller(entry) == local for entry in state.get("stack") or [])


def _window(state: dict) -> str:
    from arenamcp.decision_memory import _TURN_STEPS, turn_window

    index = turn_window(state)
    return _TURN_STEPS[index] if index is not None else ""


def _option_card(option: Any, state: dict) -> dict:
    from arenamcp.decision_memory import _source_card

    return _source_card(option, state)


def _casts(decision: Any, state: dict, *, confirmed: bool = False) -> list[tuple[Any, dict]]:
    """(option, card) for the menu's casts: payable (Arena's autotap; ``confirmed`` requires it) and known."""
    found = []
    for option in getattr(decision, "options", ()) or ():
        meta = getattr(option, "meta", None) or {}
        if meta.get("actionType") != "ActionType_Cast":
            continue
        if option.payable is False or (confirmed and option.payable is not True):
            continue
        card = _option_card(option, state)
        if card:
            found.append((option, card))
    return found


# --- cast pumps and reach -------------------------------------------------------------------------


def _pump_bonus(card: dict) -> int:
    """+N power a noncreature cast gives this creature: Slickshot Show-Off's +2, prowess +1."""
    from arenamcp.decision_memory import _CAST_PUMP_RE

    text = f"{_text(card)} {' '.join(map(str, card.get('keywords') or []))}"
    return sum(int(n) for n in _CAST_PUMP_RE.findall(text)) + (
        1 if re.search(r"\bprowess\b", text, re.I) else 0
    )


def cast_pumped_attackers(state: dict) -> list[str]:
    """Our creatures a noncreature cast now pumps for an attack still to come this turn.

    Our turn before attackers are declared, creatures that can attack
    (``combat_strategy.able_attackers``) with prowess or "whenever you cast a
    noncreature spell, this creature gets +N/+0". After combat the pump buys
    nothing, so a cast in main 2 or at their end step is worth the same.
    """
    from arenamcp.decision_memory import _TURN_STEPS, turn_window

    if not _our_turn(state):
        return []
    window = turn_window(state)
    if window is None or window > _TURN_STEPS.index("declareattack"):
        return []
    from arenamcp.combat_strategy import able_attackers

    return [str(c.get("name") or c.get("instance_id")) for c in able_attackers(state) if _pump_bonus(c)]


def _power(card: dict) -> int:
    power = card.get("modified_power")
    power = power if type(power) is int else card.get("power")
    return power if type(power) is int and power > 0 else 0


def two_turn_reach(state: dict) -> str:
    """Their life within our most damage over this turn and the next (a cheap overestimate), or ''.

    Our turn: the creatures that can still attack this turn, all our creatures
    again next turn, and the face damage of the burn in hand the two turns'
    mana pays for, each spell with the cast pumps of our creatures. A sweeper's
    damage to each of their creatures counts at that weight too (#228): chipping
    their board down is race progress, and it was invisible to the hold math.
    Blockers, removal and their life gain are ignored: it only stops a hold.
    """
    life = their_life(state)
    if life is None or life <= 0 or not _our_turn(state):
        return ""
    from arenamcp.decision_memory import _TURN_STEPS, mana_available, turn_window

    window = turn_window(state)
    creatures = [c for c in _ours(state) if "creature" in _types(c)]
    now = 0
    if window is not None and window <= _TURN_STEPS.index("declareattack"):
        from arenamcp.combat_strategy import able_attackers

        now = sum(_power(c) for c in able_attackers(state))
    later = sum(_power(c) for c in creatures)
    per_spell = sum(_pump_bonus(c) for c in creatures)
    hand = [c for c in state.get("hand") or [] if isinstance(c, dict)]
    lands = sum(1 for c in _ours(state) if "land" in _types(c))
    budget = mana_available(state) + lands + (1 if any("land" in _types(c) for c in hand) else 0)
    their_creatures = sum(1 for c in _theirs(state) if "creature" in _types(c))
    spells = [
        (_cost(c), face_damage(c) + sweep_chip(c, their_creatures) + per_spell)
        for c in hand
        if "creature" not in _types(c)
        and "land" not in _types(c)
        and not _x_spell(c)
        and (face_damage(c) or sweep_chip(c, their_creatures))
    ]
    best = [0] * (budget + 1)
    for cost, damage in spells:
        for spend in range(budget, cost - 1, -1):
            best[spend] = max(best[spend], best[spend - cost] + damage)
    burn = best[budget] if budget >= 0 else 0
    total = now + later + burn
    if total < life:
        return ""
    return (
        f"up to {total} damage this turn and next (attacks {now} + {later}, burn and cast pumps {burn}) "
        f"against their {life} life"
    )


def planned_mana(decision: Any, state: dict, exclude: set[str]) -> int:
    """Mana this turn's other plays need: the searched line's casts this turn, else the menu's sorcery-speed casts."""
    hand = [c for c in state.get("hand") or [] if isinstance(c, dict)]
    try:
        from arenamcp.board_assessment import assess

        assessment = assess(state)
        if assessment is not None and assessment.our_turn and assessment.lookahead:
            need, used = 0, set()
            for name in assessment.lookahead[0].casts:
                if name in exclude:
                    continue
                card = next(
                    (
                        c
                        for c in hand
                        if c.get("name") == name and id(c) not in used and "land" not in _types(c)
                    ),
                    None,
                )
                if card is not None:
                    used.add(id(card))
                    need += _cost(card)
            return need
    except Exception as error:  # the strategic layer never blocks a decision
        logger.debug("planned mana: no assessment (%s)", error)
    return sum(
        _cost(card)
        for option, card in _casts(decision, state)
        if str(card.get("name") or "") not in exclude and not instant_speed(card)
    )


def hold_overrides(decision: Any, state: dict, held: list[tuple[Any, dict]]) -> str:
    """Why the #80 hold does not apply to ``held`` (option, card) in our main phase, or ''.

    - a noncreature cast now pumps an attacker this turn (Slickshot Show-Off's
      +2/+0, prowess): the cast trigger is the value;
    - their life is within reach over this turn and the next
      (:func:`two_turn_reach`): every point of face damage now counts;
    - we have more mana than the end step can spend: the mana left after this
      turn's other plays (:func:`planned_mana`) is more than the held spells
      cost, so casting one now still leaves the rest castable there.
    """
    pumped = cast_pumped_attackers(state)
    if pumped:
        return f"a cast now pumps {', '.join(pumped)} for this turn's attack"
    reach = two_turn_reach(state)
    if reach:
        return f"their life is within reach over two turns ({reach})"
    if held:
        from arenamcp.decision_memory import mana_available

        names = {str(card.get("name") or "") for _option, card in held}
        cost = sum(_cost(card) for _option, card in {id(card): (o, card) for o, card in held}.values())
        spare = mana_available(state) - planned_mana(decision, state, names)
        if spare > cost:
            return (
                f"we have more mana than the end step can spend on them ({spare} left after this turn's "
                f"other plays, {cost} for the held spells)"
            )
    return ""


# The game plan tells us to chip (#228): "Chip with Hazard now", "chip their life with Iroh's
# mode 1 each turn". Match f5169733 T8-T12: GamePlan T11 said "Chip with Hazard now" and the
# hold kept Tectonic Hazard (and both Iroh's Demonstrations) off the menu three turns running —
# the frozen rationale never faced the plan.
_CHIP_WORD_RE = re.compile(r"\bchip(?:s|ping|ped)?\b", re.IGNORECASE)
_CHIP_TEMPORAL_RE = re.compile(r"\b(?:now|this turn|each turn|every turn|main phase|main)\b", re.IGNORECASE)
_CONDITIONAL_LINE_RE = re.compile(r"^(?:if|when|unless|switch|otherwise)\b", re.IGNORECASE)
_NAME_TOKEN_RE = re.compile(r"[a-z]{4,}")


def plan_chip_release(plan_text: str | None, held: list[tuple[Any, dict]]) -> tuple[list[str], str]:
    """((the held cards' names the game plan's chip directive releases), why), or ([], '').

    A chip line naming a held card releases it. When no chip line names a held
    card but an unconditional one demands chipping now / this turn / each turn,
    every held damage spell is that chip. Recomputed against the CURRENT plan
    each window: a hold never outlives the plan that justified it (#228).
    """
    text = str(plan_text or "")
    if not text or not held:
        return [], ""
    chip_lines = [line.strip() for line in text.splitlines() if _CHIP_WORD_RE.search(line)]
    if not chip_lines:
        return [], ""
    named: list[str] = []
    for line in chip_lines:
        low = line.lower()
        for _option, card in held:
            name = str(card.get("name") or "")
            if name and (
                name.lower() in low or any(tok in low for tok in _NAME_TOKEN_RE.findall(name.lower()))
            ):
                named.append(name)
    if not named:
        imperative = [line for line in chip_lines if not _CONDITIONAL_LINE_RE.search(line)]
        if any(_CHIP_TEMPORAL_RE.search(line) for line in imperative):
            named = list(dict.fromkeys(str(card.get("name") or "") for _option, card in held))
    named = [name for name in dict.fromkeys(named) if name]
    if not named:
        return [], ""
    return named, f"the game plan says chip ({chip_lines[0][:120]})"


def line_gain_is_face_only(result: Any, face: int) -> bool:
    """Whether the searched best line beats holding (its best line that casts nothing now) only by
    ``face`` damage to their face: same outcome and timing, our life no higher at any step, and
    their final life at most ``face`` lower (ledger #136: Tectonic Hazard's "[LINE best]" at T1).
    True when holding is the best line itself: a cast rated "~best" then gains nothing over it
    (the held-removal floor, ledger #147, rates holding Hazard above its 1 face damage at T1)."""
    try:
        best = getattr(result, "best", None)
        hold = (getattr(result, "first_action", None) or {}).get(("nocast",))
        if best is None or hold is None or face <= 0:
            return False
        if hold is best:
            return True
        if (hold.cls, hold.timing) != (best.cls, best.timing):
            return False
        if any(ours > held for ours, held in zip(best.lives(), hold.lives(), strict=False)):
            return False

        def opp(line: Any) -> int | None:
            return next(
                (s.opp_life_after for s in reversed(line.steps) if s.opp_life_after is not None), None
            )

        best_opp, hold_opp = opp(best), opp(hold)
        return best_opp is not None and hold_opp is not None and hold_opp - best_opp <= face
    except Exception:  # the strategic layer never blocks a decision
        return False


def sweeper_hold(card: dict, state: dict) -> bool:
    """A sorcery that damages each of their creatures and kills nothing now (ledger #136).

    2026-10-19 13:18:48 (e917434f T1): Tectonic Hazard ("1 damage to each
    opponent and each creature they control") went face into an empty board;
    the line search tied it with Burst Lightning at the face. Held, it killed
    Frantic Scapegoat on T5 and pumped Slickshot Show-Off. The caller checks
    the pumps and the reach (:func:`hold_overrides`'s first two reasons).
    """
    from arenamcp.decision_memory import burn_kills_nothing

    if instant_speed(card) or "creature" in _types(card) or not is_sweeper(card):
        return False
    return burn_kills_nothing(str(card.get("oracle_text") or ""), state, castable=card)


# --- #139: our own spell on the stack -------------------------------------------------------------


@dataclass(frozen=True)
class LineWait:
    """The best line's sorcery-speed casts that wait for our own spell on the stack to resolve."""

    pending: tuple[str, ...]
    casts: tuple[str, ...]
    need: int


def own_stack_line_waits(state: dict, result: Any = None) -> LineWait | None:
    """With only our own spell on the stack in our main phase: the best line's next casts, when all
    of them are sorcery-speed (they wait for it to resolve), and the mana they need; else None.

    2026-10-19 13:28:25 (d9a33232 T4, ledger #139): Boltwave on the stack, the
    line "Hired Claw; then Mountain + Shock (on Jace) + Slickshot Show-Off";
    no option was tagged (tags need an empty stack) and Shock went at a
    3-loyalty Jace, spending the mana the line kept for Hired Claw.
    """
    stack = [entry for entry in state.get("stack") or [] if isinstance(entry, dict)]
    if not stack or not stack_is_ours(state) or not _our_turn(state):
        return None
    if _window(state) not in ("main1", "main2"):
        return None
    if result is None:
        try:
            from arenamcp.board_assessment import assess

            result = getattr(assess(state), "line_search", None)
        except Exception as error:  # the strategic layer never blocks a decision
            logger.debug("own-stack line: no assessment (%s)", error)
            return None
    if result is None or getattr(result, "truncated", True) or getattr(result, "dead_now", False):
        return None
    best = result.best
    step = best.steps[0] if best is not None and best.steps else None
    if step is None or not step.casts:
        return None
    hand = [c for c in state.get("hand") or [] if isinstance(c, dict)]
    used: set[int] = set()
    need = 0
    for name in step.casts:
        card = next((c for c in hand if c.get("name") == name and id(c) not in used), None)
        if card is None or instant_speed(card):
            return None  # an instant in the line's first step may go now; an unknown card says nothing
        used.add(id(card))
        need += _cost(card)
    pending = tuple(str(entry.get("name") or "spell") for entry in stack)
    return LineWait(pending, tuple(step.casts), need)


def own_stack_holds(decision: Any, state: dict, wait: LineWait) -> list[tuple[Any, dict]]:
    """Instant casts that would leave ``wait``'s plays unpaid (not one of them, too dear for the rest)."""
    from arenamcp.decision_memory import mana_available

    mana = mana_available(state)
    return [
        (option, card)
        for option, card in _casts(decision, state)
        if instant_speed(card)
        and str(card.get("name") or "") not in wait.casts
        and mana - _cost(card) < wait.need
    ]


# --- #140: our upkeep and draw step ---------------------------------------------------------------


def upkeep_holds(decision: Any, state: dict) -> list[tuple[Any, dict]]:
    """Damage-only instants on the menu in our upkeep or draw step with nothing of theirs on the stack.

    2026-10-19 13:28:47 (d9a33232 T6, ledger #140): Shock in our upkeep (at a
    Jace with 2 loyalty), with two Mountains untapped; in main 1 the Mountain
    came down, Slickshot Show-Off was cast and Hired Claw was stranded for
    want of the mana Shock took, and Slickshot's +2/+0 from Shock was lost.
    Cast in main 1 it does the same, after the land drop and our creatures.
    """
    if not _our_turn(state) or _window(state) not in ("upkeep", "draw") or not stack_is_ours(state):
        return []
    return [
        (option, card)
        for option, card in _casts(decision, state)
        if instant_speed(card) and damage_only(card, state)
    ]


_COMBAT_STEPS = (
    "begincombat",
    "declareattack",
    "declareblock",
    "firststrikedamage",
    "combatdamage",
    "endcombat",
)


def combat_kills(card: dict, state: dict) -> list[str]:
    """Their creatures that matter in this combat of ours which ``card``'s damage may kill now.

    Before blocks (beginning of combat, declare attackers) an untapped creature
    of theirs that could block; once blocks are declared, a creature blocking
    us (combat damage counts toward it, ``decision_memory._damage_may_kill``).
    Its ward is paid within our mana.
    """
    from arenamcp.decision_memory import _damage_may_kill, _reachable, burn_reach, mana_available

    burn = burn_reach(str(card.get("oracle_text") or ""), card, state)
    if burn is None or not burn.creatures:
        return []
    before_blocks = _window(state) in ("begincombat", "declareattack")
    mana = mana_available(state)
    found = []
    for creature in _theirs(state):
        if "creature" not in _types(creature):
            continue
        matters = not creature.get("is_tapped") if before_blocks else bool(creature.get("is_blocking"))
        if matters and any(
            _damage_may_kill(creature, damage) for damage in _reachable(burn, creature, state, mana)
        ):
            found.append(str(creature.get("name") or creature.get("instance_id")))
    return found


def combat_holds(decision: Any, state: dict) -> list[tuple[Any, dict]]:
    """Damage-only instants on the menu in our own combat that kill nothing that matters in it.

    2026-10-19 13:59:03 (633f56bd T7, ledger #140): Lightning Strike, held at
    13:58:33 for their end step, went to the face in our declare-blockers step
    (tagged "-7 vs best"; the line kept it for Leonin Vanguard). In combat it
    goes now only to kill a blocker (or a creature that could block, before
    blocks) or when it is lethal (the caller's check); nothing of theirs is on
    the stack.
    """
    if not _our_turn(state) or _window(state) not in _COMBAT_STEPS or not stack_is_ours(state):
        return []
    return [
        (option, card)
        for option, card in _casts(decision, state)
        if instant_speed(card) and damage_only(card, state) and not combat_kills(card, state)
    ]


def combat_face_reach(state: dict, holds: list[tuple[Any, dict]]) -> int:
    """Face damage this combat at most: our attacking creatures, the held burn, and its cast pumps on them."""
    attacking = [c for c in _ours(state) if "creature" in _types(c) and c.get("is_attacking")]
    pumps = sum(_pump_bonus(c) for c in attacking) * len(holds)
    return sum(_power(c) for c in attacking) + pumps + sum(face_damage(card) for _option, card in holds)


# --- #137: the release at their end step ----------------------------------------------------------


@dataclass(frozen=True)
class Release:
    """One held spell to cast now, and where."""

    option_id: str
    name: str
    target: str  # "opponent", or the name of the creature / planeswalker it kills
    target_id: int
    damage: int
    why: str


@dataclass
class EndStepPlan:
    pick: Release | None
    kept: list[tuple[str, str, str]] = field(default_factory=list)  # (option id, card, why)
    face_total: int = 0


def _kills_now(burn: Any, victim: dict, state: dict, mana: int) -> bool:
    """The spell's unkicked damage, its ward paid, kills this creature or planeswalker now."""
    from arenamcp.combat_strategy import loyalty
    from arenamcp.decision_memory import ward_tax
    from arenamcp.play_safety import lethal_damage

    plain_cost, plain = burn.variants[0]
    if plain_cost + ward_tax(victim, state) > mana:
        return False
    types = _types(victim)
    needs = []
    if "creature" in types and burn.creatures:
        needs.append(lethal_damage(victim))
    if "planeswalker" in types and burn.walkers:
        needs.append(loyalty(victim))
    return any(need is not None and plain >= need for need in needs)


def _most_face(options: list[tuple[Any, dict, int]], mana: int) -> list[tuple[Any, dict, int]]:
    """The spells that deal the most face damage together within ``mana`` (fewest mana on a tie)."""
    best: tuple[tuple[int, int], list] = ((0, 0), [])
    for size in range(1, len(options) + 1):
        for subset in combinations(options, size):
            spent = sum(_cost(card) for _option, card, _damage in subset)
            if spent > mana:
                continue
            key = (sum(damage for _o, _c, damage in subset), -spent)
            if key > best[0]:
                best = (key, list(subset))
    return best[1]


def end_step_release(decision: Any, state: dict) -> EndStepPlan | None:
    """At the opponent's end step (or cleanup) with an empty stack: our burn to cast now, or None.

    The mana untaps unused at our untap step, so held burn goes now, one cast
    per window (Arena gives another window after it resolves):

    1. a spell the searched best line for our next turn aims at a creature or
       planeswalker it kills now, at that target (the same kill, with free mana);
    2. else the face damage the mana pays for: the subset dealing the most,
       its most expensive spell first.

    A spell stays in hand when the best line needs it on our next turn (aimed
    at something it does not kill alone now, or in a line that wins then), or
    when it can kill a creature or planeswalker the line does not aim it at.
    Their face damage that is lethal now overrides every keep. A cast pump on
    our turn is no reason to keep it: the line search does not value the pump
    (ledger #94), and a spell kept for one would wait at every end step while
    the pumped creature lives. Casts need Arena's autotap (payable); X spells
    and creatures never go. None outside this window.
    """
    if getattr(decision, "request_type", "") != "ActionsAvailable":
        return None
    local = _local_seat(state)
    if local is None or _our_turn(state) or (state.get("turn") or {}).get("active_player") is None:
        return None
    if _window(state) not in ("end", "cleanup") or state.get("stack"):
        return None
    life = their_life(state)
    if life is None:
        return None
    from arenamcp.decision_memory import burn_kills_nothing, burn_reach, mana_available

    candidates = []
    for option, card in _casts(decision, state, confirmed=True):
        if _x_spell(card) or "creature" in _types(card) or not instant_speed(card):
            continue
        oracle = str(card.get("oracle_text") or "")
        burn = burn_reach(oracle, card, state, check_kicker=False)
        face = face_damage(card)
        if face or burn is not None:
            candidates.append((option, card, str(card.get("name") or option.label), face, burn))
    if not candidates:
        return None
    mana = mana_available(state)
    lethal = _most_face([(o, c, f) for o, c, _n, f, _b in candidates if f], mana)
    if lethal and sum(f for _o, _c, f in lethal) >= life:
        option, card, damage = max(lethal, key=lambda item: (_cost(item[1]), item[2]))
        name = str(card.get("name") or option.label)
        why = f"{sum(f for _o, _c, f in lethal)} face damage now is lethal (they are at {life})"
        return EndStepPlan(Release(option.option_id, name, "opponent", 0, damage, why), [], damage)

    step, best = None, None
    try:
        from arenamcp.board_assessment import assess
        from arenamcp.line_search_moves import WIN

        result = getattr(assess(state), "line_search", None)
        if result is not None and not result.truncated and not result.dead_now:
            best = result.best
            step = best.steps[0] if best is not None and best.steps else None
            win_next = best is not None and best.cls == WIN and best.win_at == 1
        else:
            win_next = False
    except Exception as error:  # the strategic layer never blocks a decision
        logger.debug("end-step release: no line search (%s)", error)
        win_next = False
    casts = list(step.casts) if step is not None else []
    targets = dict(step.targets) if step is not None else {}
    removal: list[tuple[int, Release]] = []
    face_options: list[tuple[Any, dict, int]] = []
    kept: list[tuple[str, str, str]] = []
    for option, card, name, face, burn in candidates:
        aimed = targets.get(name) if name in casts else None
        if aimed and aimed != "opponent":
            victims = [c for c in _theirs(state) if c.get("name") == aimed]
            killed = [v for v in victims if burn is not None and _kills_now(burn, v, state, mana)]
            if killed:
                victim = max(killed, key=lambda v: (_power(v), -int(v.get("instance_id") or 0)))
                removal.append(
                    (
                        _power(victim),
                        Release(
                            option.option_id,
                            name,
                            aimed,
                            int(victim.get("instance_id") or 0),
                            burn.variants[0][1],
                            f"the searched line kills {aimed} with it on our next turn; it dies to it now, "
                            "with mana that untaps unused otherwise",
                        ),
                    )
                )
            else:
                kept.append(
                    (option.option_id, name, f"the searched line aims it at {aimed} on our next turn")
                )
            continue
        if win_next and name in casts:
            kept.append((option.option_id, name, "the searched line wins on our next turn with it"))
            continue
        if burn is not None and not burn_kills_nothing(
            str(card.get("oracle_text") or ""), state, castable=card
        ):
            kept.append(
                (
                    option.option_id,
                    name,
                    "it can kill a creature or planeswalker of theirs now, which the line does not aim it at",
                )
            )
            continue
        if not face:
            continue  # removal-only burn with nothing to kill: nothing to cast it at
        face_options.append((option, card, face))
    if removal:
        _power_of, pick = max(removal, key=lambda item: item[0])
        return EndStepPlan(pick, kept)
    chosen = _most_face(face_options, mana)
    if not chosen:
        return EndStepPlan(None, kept)
    option, card, damage = max(chosen, key=lambda item: (_cost(item[1]), item[2]))
    total = sum(f for _o, _c, f in chosen)
    name = str(card.get("name") or option.label)
    why = (
        f"burn held for this window goes at their face ({total} damage this end step, they are at {life}); "
        "the mana untaps unused otherwise"
    )
    return EndStepPlan(Release(option.option_id, name, "opponent", 0, damage, why), kept, total)


# --- each hold's fate -----------------------------------------------------------------------------


@dataclass
class _Held:
    name: str
    turn: int
    why: str


class HoldFates:
    """What became of each held burn spell, logged as 'hold-fate: <card> (held T<n>) -> <fate>'.

    Fates: "released at end step at face", "used as removal on <X>" (at the
    end step or later), "cast at face on T<n>", "kept at their end step: <why>"
    (the release chose to keep it), and "stranded" (our next turn began with it
    still in hand, unreleased). One record per card name; a new game drops the
    old records as stranded.
    """

    def __init__(self) -> None:
        self._held: dict[str, _Held] = {}
        self._pending: tuple[str, bool, str] | None = None  # (card, released, window) awaiting its target
        self._turn: int | None = None

    @staticmethod
    def _turn_of(state: dict) -> int:
        try:
            return int((state.get("turn") or {}).get("turn_number") or 0)
        except (TypeError, ValueError):
            return 0

    def _resolve(self, name: str, fate: str) -> None:
        held = self._held.pop(name, None)
        if held is not None:
            logger.info("hold-fate: %s (held T%d: %s) -> %s", name, held.turn, held.why, fate)

    def tick(self, state: dict) -> None:
        """A new game, or our turn after the one we held on: what is still held was stranded."""
        turn = self._turn_of(state)
        if self._turn is not None and turn and turn < self._turn:
            for name in list(self._held):
                self._resolve(name, "stranded (the game ended with it in hand)")
            self._pending = None
        self._turn = turn or self._turn
        if not _our_turn(state):
            return
        for name, held in list(self._held.items()):
            if turn > held.turn + 1:
                self._resolve(name, f"stranded (our turn {turn} began with it unreleased)")

    def held(self, state: dict, names: list[str], why: str) -> None:
        turn = self._turn_of(state)
        for name in names:
            known = self._held.get(name)
            if known is None or known.turn != turn:
                self._held[name] = _Held(name, turn, why)

    def kept(self, name: str, why: str) -> None:
        self._resolve(name, f"kept at their end step: {why}")

    def observe(self, decision: Any, state: dict, chosen: list[str], *, released: str = "") -> None:
        """Follow a decision: a held card cast now, and the target its target step chose."""
        try:
            self._observe(decision, state, chosen, released)
        except Exception as error:  # the log never blocks a decision
            logger.debug("hold fates skipped: %s", error)

    def _observe(self, decision: Any, state: dict, chosen: list[str], released: str) -> None:
        self.tick(state)
        request = str(getattr(decision, "request_type", "") or "")
        turn = self._turn_of(state)
        if request == "SelectTargets" and self._pending is not None:
            name, release, window = self._pending
            if str(getattr(decision, "source_label", "") or "").strip().casefold() == name.casefold():
                self._pending = None
                self._resolve(name, self._target_fate(state, chosen, release, window, turn))
                return
        if self._pending is not None:
            name, release, window = self._pending
            self._pending = None
            self._resolve(name, f"cast on T{turn} ({window})")
        if request != "ActionsAvailable" or len(chosen or []) != 1:
            return
        option = decision.find(chosen[0])
        meta = (option.meta if option is not None else None) or {}
        if meta.get("actionType") != "ActionType_Cast":
            return
        name = str(_option_card(option, state).get("name") or "")
        if name in self._held:
            window = ("their " if not _our_turn(state) else "our ") + (_window(state) or "turn")
            self._pending = (name, name == released, window)

    @staticmethod
    def _target_fate(state: dict, chosen: list[str], release: bool, window: str, turn: int) -> str:
        seats = {p.get("seat_id") for p in state.get("players") or [] if isinstance(p, dict)}
        local = _local_seat(state)
        by_id = {c.get("instance_id"): c for c in state.get("battlefield") or []}
        for option_id in chosen or []:
            if not str(option_id).startswith("tgt:"):
                continue
            try:
                target = int(str(option_id)[4:])
            except ValueError:
                continue
            if target in seats and target != local:
                return "released at end step at face" if release else f"cast at face on T{turn} ({window})"
            card = by_id.get(target)
            if card is not None and _controller(card) != local:
                where = "at end step" if release else f"on T{turn} ({window})"
                return f"used as removal on {card.get('name') or target} {where}"
        return f"cast on T{turn} ({window}) at {', '.join(map(str, chosen or [])) or 'no target'}"


__all__ = [
    "EndStepPlan",
    "HoldFates",
    "LineWait",
    "Release",
    "cast_pumped_attackers",
    "combat_face_reach",
    "combat_holds",
    "combat_kills",
    "damage_only",
    "end_step_release",
    "face_damage",
    "hold_overrides",
    "instant_speed",
    "is_sweeper",
    "line_gain_is_face_only",
    "own_stack_holds",
    "own_stack_line_waits",
    "planned_mana",
    "stack_is_ours",
    "sweeper_hold",
    "their_life",
    "two_turn_reach",
    "upkeep_holds",
]
