"""Correct attack reasoning that rests on a false tapped-enemy premise.

2026-11-12 (Gar vs Minsc, T8): the plan attacked our hasty 4/4 Boo at their
3-loyalty Minsc saying "their big Boo is tapped so crackback is limited."
Three things are wrong with that premise, generically:

- their untapped creatures can still block THIS attack, alone or together,
  so a planeswalker kill is not guaranteed until the best block is
  evaluated (their untapped Curator 3/3 + Halfling 2/3 double block kills
  the 4/4 and - even with trample - leaves the walker standing);
- a tapped creature untaps during its controller's untap step, so being
  tapped now says nothing about their NEXT combat's attackers. That threat
  stands even when their whole board is tapped today;
- when the attacker was committed as a blocker earlier this turn (or held
  back by a guard), spending it on attack reverses that commitment and the
  reasoning must resolve that conflict honestly - state the reversal and
  its validated consequence, never an instruction after the choice.

The correction is narration-only: the pick stands, the reasoning gains the
board facts, and the trace records the premise sentence. Legal tactical
attacks are never overridden, holding is never claimed to win, and no card
or match identity is consulted.

When the premise sentence asserts killing or destroying the target and the
validated verdict says the target takes no damage this combat (or takes
less damage than its known loyalty), the sentence is REPLACED - an
appended contradiction after "Kill their Minsc" is not a justified
revision. The replacement keeps the attack intent ("Attack their Minsc
engine") and follows it with the validated verdict, so the revision says
what changed and why it still stands on the board facts.

Limits (deliberately bounded):
- The best-block evaluation reuses the production combat engine -
  :func:`arenamcp.board_assessment._body` builds each body and
  :func:`arenamcp.combat_solver._can_block` decides blocking legality.
  Against a life total the search is :func:`combat_solver.optimal_blocks`;
  against a PLANESWALKER victim every legal block assignment is resolved by
  :func:`combat_solver._resolve_attacker_damage` and the least damage through
  wins - a life-optimal search happily leaves a walker unblocked, so it can
  never validate or refute a walker kill claim (review finding on 2317892).
  Flying/reach legality, deathtouch, indestructible and trample therefore all
  come from the same solver the line search uses; this module performs no
  arithmetic of its own.
- Missing power/toughness on the attacker or ANY candidate blocker means
  no definitive verdict: the clause degrades to availability facts and never
  claims a guaranteed outcome.
- Who-can-block claims are DERIVED FROM THE LEGAL SET: when no untapped
  creature can legally block (e.g. ground creatures facing a flyer, or a
  menacing attacker with fewer than two legal blockers), the text says so
  instead of asserting they "can still block"; a MIXED pool names only its
  legal members as able to block (review finding on d7fc70f); when legality
  was not evaluated (multi-attacker plans or missing stats) only neutral
  availability ("are still untapped this combat") is stated.
- Unblocked damage is production resolution, not raw power: double strike
  doubles the damage that gets through (review finding on d7fc70f).
- Whole-assignment restrictions are enforced on top of per-blocker legality:
  menace requires TWO blockers, so singleton assignments are never scored
  against a menacing attacker (review finding on 167cdf0). Restrictions the
  production engine does not model (e.g. "can't be blocked by more than one
  creature") decline definitive arithmetic entirely (safe unknown).
- Enumeration of counter-block assignments is capped; past the cap an honest
  unknown replaces any "their best block" claim (a found saving block still
  proves one exists - a subset minimum bounds the true best from above).
- Defender life for the block search comes from the snapshot via the
  engine's convention (:func:`arenamcp.board_assessment._life`, 20 when
  unknown).
- Static grants (e.g. anthem pumps) are not modelled: bodies are built
  with empty rules, matching what the snapshot's printed P/T shows.
- Only single-attacker declarations get arithmetic; multi-attacker plans
  get the qualitative current-blockers / next-combat facts.
- Contradiction replacement fires only when a kill/destroy claim meets a
  validated no-damage (or below-known-loyalty) verdict; otherwise the
  facts append after the reasoning unchanged.
- Whether a tapped creature actually untaps is assumed unless an effect
  keeps it tapped; the wording says "unless an effect keeps it tapped".
- Blocker commitments are read from this turn's decision memory only:
  guard hold-back notes and the model's own recorded reasoning that names
  the creature with a blocking/save/reserve intent. Outage fallbacks
  (``model_unavailable``) are history, not commitments. The reconciliation
  note states the reversal plus its validated consequence - it never
  instructs and never claims that holding wins.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from arenamcp.board_assessment import _body, _life
from arenamcp.combat_keywords import has_combat_keyword
from arenamcp.combat_solver import _can_block, _resolve_attacker_damage, optimal_blocks
from arenamcp.combat_strategy import loyalty

# Bodies for evaluation are built far past any summoning-sickness window:
# sickness only gates attacking, and this module consumes blocking legality
# and combat arithmetic only.
_PROBE_TURN = 10**6

# A sentence that ties one of THEIR creatures being tapped to a crackback /
# blocking-safety conclusion. Both halves must be in the same sentence so a
# passing mention of a tapped permanent of ours never triggers it.
_PREMISE = re.compile(
    r"(?:\btheir\b|\benemy\b|\bopponent(?:'s)?\b|\bopposing\b)[^.!?]{0,80}\btapped\b"
    r"|\btapped\b[^.!?]{0,80}(?:\btheir\b|\benemy\b|\bopponent(?:'s)?\b|\bopposing\b)",
    re.IGNORECASE,
)
_CONSEQUENCE = re.compile(
    r"crack[ -]?back|retaliat\w*|attack\w*\s+back|hits?\s+back|swings?\s+back"
    r"|can(?:'|no)t\s+block|won'?t\s+(?:be\s+able\s+to\s+)?block|no\s+blocks?\b",
    re.IGNORECASE,
)

# Kill/destroy claims in the premise sentence. Only verbs with a swap are
# listed: every match must be replaceable by its attack-intent wording.
_KILL_CLAIM = re.compile(
    r"\b(kills?|killing|destroy(?:s|ing)?|finish(?:es)?\s+off|wipe(?:s|d)?\s+out)\b",
    re.IGNORECASE,
)
_KILL_SWAP = {
    "kill": "attack",
    "kills": "attacks",
    "killing": "attacking",
    "destroy": "attack",
    "destroys": "attacks",
    "destroying": "attacking",
    "finish off": "attack",
    "finishes off": "attacks",
    "wipe out": "attack",
    "wipes out": "attacks",
    "wiped out": "attacked",
}

# Sentences this module adds. Their presence means the reasoning was already
# corrected on a previous pass - never append or replace twice.
_CORRECTED_MARKERS = (
    "can still block this attack now",
    "cannot legally block this attack now",
    "are still untapped this combat",
    "Tapped now is not tapped next combat",
    "reverses this turn's hold-back",
    "reverses that commitment",
)

_HELD_NOTE = re.compile(r"attack held by guard: (.+?) not attacking")

# A recorded reasoning that names a creature AND states a blocking/save/reserve
# intent for it is a commitment spending that creature on attack reverses.
_COMMITMENT = re.compile(
    r"\bblocks?\w*|chump\w*|reserv\w*|sav\w*|keeps?\s+(?:\w+\s+){0,2}back"
    r"|holds?\s+(?:\w+\s+){0,2}back|hold\s+back|absorb\w*",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CrackbackCorrection:
    """What to do with a reasoning that rests on the tapped-enemy premise.

    ``mode="replace"``: ``sentence`` asserted killing/destroying the target
    and the validated verdict contradicts it - swap ``sentence`` for ``text``
    (the attack intent kept, followed by the validated verdict).
    ``mode="append"``: ``text`` carries board facts that follow from the
    reasoning; append them after it.
    """

    sentence: str = ""
    text: str = ""
    mode: str = ""


def already_corrected(reasoning: str) -> bool:
    """Whether ``reasoning`` already carries this module's corrections."""
    text = str(reasoning or "")
    return any(marker in text for marker in _CORRECTED_MARKERS)


def _sentences(reasoning: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", str(reasoning or "")) if s.strip()]


def _opponent_seat(state: dict) -> int | None:
    local = state.get("local_seat_id")
    if local is None:
        for player in state.get("players") or []:
            if player.get("is_local"):
                local = player.get("seat_id")
                break
    if local is None:
        return None
    for player in state.get("players") or []:
        seat = player.get("seat_id")
        if seat is not None and seat != local:
            return seat
    return None


def _their_creatures(state: dict) -> list[dict]:
    seat = _opponent_seat(state)
    if seat is None:
        return []
    creatures = []
    for card in state.get("battlefield") or []:
        if card.get("controller_seat_id") != seat:
            continue
        if "Creature" not in str(card.get("type_line") or ""):
            continue
        creatures.append(card)
    return creatures


def their_untapped_creatures(state: dict) -> list[dict]:
    """Their creatures that can still block this attack now."""
    return [card for card in _their_creatures(state) if not card.get("is_tapped")]


def defender_life(state: dict) -> int:
    """The defender's life for the block search, under the engine's convention."""
    return _life(state, _opponent_seat(state))


def _describe(cards: list[dict]) -> str:
    parts = []
    for card in cards[:3]:
        name = str(card.get("name") or "a creature")
        power = card.get("power")
        toughness = card.get("toughness")
        stats = f" {power}/{toughness}" if power is not None and toughness is not None else ""
        parts.append(f"{name}{stats}")
    return ", ".join(parts)


def _swap_kill_match(match: re.Match) -> str:
    phrase = match.group(0)
    replacement = _KILL_SWAP.get(phrase.casefold())
    if replacement is None:
        return phrase
    return replacement.capitalize() if phrase[:1].isupper() else replacement


@dataclass(frozen=True)
class BlockVerdict:
    """What their best block does to one attacker and its victim.

    ``clause``/``outcome`` are empty when nothing definitive can be stated;
    ``no_legal`` marks a board where no untapped creature can legally block;
    ``unharmed``/``through`` carry the validated damage verdict."""

    clause: str = ""
    outcome: str = ""
    unharmed: bool | None = None
    through: int | None = None
    no_legal: bool = False


# Whole-assignment restrictions the production engine does not model (it only
# knows menace's "two or more blockers" rule). When an attacker carries one,
# per-blocker legality plus a subset enumeration could score illegal
# assignments, so definitive arithmetic declines instead (safe unknown).
_UNMODELLED_MULTI_BLOCK = re.compile(
    r"can'?t be blocked by (?:(?:more|no more) than one|two or more|at least two)",
    re.IGNORECASE,
)


def _legal_blocker_bodies(attacker_body: dict, blockers: list[dict]) -> list[dict] | None:
    """The candidate blockers that can legally block ``attacker_body``.

    Solver-built bodies (:func:`board_assessment._body`,
    :func:`combat_solver._can_block`). Returns ``None`` when ANY candidate's
    power/toughness is unknown - legality was then not evaluated for the pool
    and callers must stay neutral rather than claim who can block (review
    finding on d7fc70f)."""
    legal = []
    for card in blockers:
        body = _body(card, _PROBE_TURN, {})
        if body is None:
            return None
        if _can_block(attacker_body, body):
            legal.append(body)
    return legal


_MAX_COUNTER_BLOCK_ASSIGNMENTS = 512


def _counter_block_search(attacker_body: dict, legal: list[dict], min_blockers: int = 1):
    """The least damage ``attacker_body`` forces through any legal block.

    Every candidate assignment - each permitted-size combination of legal
    blockers - is resolved by the production engine,
    :func:`combat_solver._resolve_attacker_damage`, so trample spill order,
    deathtouch and double strike are exactly what the line search plays with;
    no arithmetic lives here. ``min_blockers`` carries whole-assignment
    restrictions (menace needs TWO blockers - a singleton assignment is not a
    legal block against a menacing attacker, review finding on 167cdf0).
    Returns ``(least_through, names, attacker_died, complete)``;
    ``complete`` is False when the enumeration hit the assignment cap - the
    minimum found then bounds the true best from above only, so a found
    saving block still proves one exists but no "their best block"
    superlative may be claimed.
    """
    from itertools import combinations

    if min_blockers > len(legal):
        return None, "", False, True
    least_through: int | None = None
    least_names = ""
    least_attacker_died = False
    evaluated = 0
    complete = True
    for size in range(min_blockers, len(legal) + 1):
        for combo in combinations(legal, size):
            evaluated += 1
            if evaluated > _MAX_COUNTER_BLOCK_ASSIGNMENTS:
                complete = False
                break
            out = _resolve_attacker_damage(attacker_body, list(combo))
            through = int(out.damage_through or 0)
            if least_through is None or through < least_through:
                least_through = through
                least_names = ", ".join(str(body["name"]) for body in combo)
                least_attacker_died = bool(out.attacker_died)
        if not complete:
            break
    return least_through, least_names, least_attacker_died, complete


def _planeswalker_block_verdict(
    attacker_body: dict,
    legal: list[dict],
    victim: str,
    pw_loyalty: int,
    min_blockers: int = 1,
) -> BlockVerdict:
    """A kill-claim-appropriate verdict against a planeswalker victim.

    The life-optimal search answers what a defender concedes to protect life -
    it will happily leave a planeswalker unblocked, so it can neither validate
    nor refute a walker kill claim (review finding on 2317892). What a kill
    claim needs is whether any legal block holds the attacker's damage below
    the walker's loyalty: every legal assignment is resolved by the production
    engine and the least damage through wins. ``min_blockers`` carries
    whole-assignment restrictions (menace)."""
    power = attacker_body["power"]
    desc = f"the {power}/{attacker_body['toughness']} attacker"
    through, names, attacker_died, complete = _counter_block_search(
        attacker_body, legal, min_blockers=min_blockers
    )
    if through is None:
        return BlockVerdict()
    fate = f"kills {desc}" if attacker_died else f"{desc} survives"
    if through == 0:
        qualifier = "their best block" if complete else "blocking"
        clause = f"{qualifier} ({names}) {fate} and {victim} takes no damage this combat."
        outcome = f"{qualifier} ({names}) leaves {victim} unharmed"
        return BlockVerdict(clause=clause, outcome=outcome, unharmed=True, through=0)
    if through < pw_loyalty:
        # A saving block exists: the subset minimum bounds the true best from above.
        qualifier = "their best block" if complete else "blocking"
        clause = (
            f"{qualifier} ({names}) {fate} and only {through} damage gets through "
            f"to {victim}, below its {pw_loyalty} loyalty."
        )
        outcome = (
            f"{qualifier} ({names}) holds the damage to {through}, below {victim}'s {pw_loyalty} loyalty"
        )
        return BlockVerdict(clause=clause, outcome=outcome, unharmed=False, through=through)
    if not complete:
        return BlockVerdict()  # cannot prove "best": honest unknown
    clause = (
        f"even their best legal block ({names}) lets {through} damage through to "
        f"{victim}, removing its {pw_loyalty} loyalty."
    )
    outcome = f"even their best legal block lets {through} damage through to {victim}"
    return BlockVerdict(clause=clause, outcome=outcome, unharmed=False, through=through)


def _best_block_verdict(
    attacker: dict,
    blockers: list[dict],
    victim: str,
    life: int,
    target_card: dict | None = None,
) -> BlockVerdict:
    """Evaluate their best block with the production combat solver.

    Returns a :class:`BlockVerdict`; an empty clause when nothing definitive
    can be stated (no blockers, missing power/toughness on the attacker or any
    candidate, or a search that found no plan). Blocking legality
    (flying/reach/menace), deathtouch, indestructible and trample all come
    from :func:`combat_solver.optimal_blocks` /
    :func:`combat_solver._resolve_attacker_damage` /
    :func:`combat_solver._can_block` - no arithmetic here. When
    ``target_card`` is a planeswalker with known loyalty the verdict switches
    to the target-appropriate counter-block evaluation; the life-optimal
    search cannot speak to walker survival."""
    if not blockers:
        return BlockVerdict()
    attacker_body = _body(attacker, _PROBE_TURN, {})
    if attacker_body is None:
        return BlockVerdict()
    power = attacker_body["power"]
    toughness = attacker_body["toughness"]
    desc = f"the {power}/{toughness} attacker"
    legal = _legal_blocker_bodies(attacker_body, blockers)
    if legal is None:
        return BlockVerdict()  # missing stats: decline definitive arithmetic
    # Production unblocked resolution (double strike doubles the through
    # damage, combat-hit bonuses apply): never narrate raw power as the
    # unblocked damage (review finding on d7fc70f).
    unblocked_through = int(_resolve_attacker_damage(attacker_body, []).damage_through or 0)
    if not legal:
        reason = (
            "only a creature with flying or reach can block a flyer"
            if has_combat_keyword(attacker, "flying")
            else ""
        )
        clause = f"none of their untapped creatures can legally block {desc}"
        if reason:
            clause += f" ({reason})"
        clause += f"; all {unblocked_through} damage goes to {victim} this combat."
        outcome = (
            f"nothing among their untapped creatures can legally block "
            f"{attacker_body['name']}; all {unblocked_through} damage goes to {victim}"
        )
        return BlockVerdict(
            clause=clause, outcome=outcome, unharmed=False, through=unblocked_through, no_legal=True
        )
    if has_combat_keyword(attacker, "menace") and len(legal) < 2:
        # Whole-assignment restriction: a menacing attacker cannot be legally
        # blocked by a single creature (review finding on 167cdf0).
        clause = (
            f"only two or more creatures can block the menacing "
            f"{power}/{toughness} attacker; all {unblocked_through} damage goes to {victim} this combat."
        )
        outcome = (
            f"their single legal blocker cannot stop {attacker_body['name']}; "
            f"all {unblocked_through} damage goes to {victim}"
        )
        return BlockVerdict(
            clause=clause, outcome=outcome, unharmed=False, through=unblocked_through, no_legal=True
        )
    if _UNMODELLED_MULTI_BLOCK.search(str(attacker.get("oracle_text") or "")):
        return BlockVerdict()  # unmodellable whole-assignment restriction: safe unknown
    pw_loyalty = None
    if target_card is not None and "Planeswalker" in str(target_card.get("type_line") or ""):
        pw_loyalty = loyalty(target_card)
    if pw_loyalty is not None:
        return _planeswalker_block_verdict(
            attacker_body,
            legal,
            victim,
            int(pw_loyalty),
            min_blockers=2 if has_combat_keyword(attacker, "menace") else 1,
        )
    plan = optimal_blocks([attacker_body], legal, max(1, int(life)), caller="attack_premises")
    if plan is None:
        return BlockVerdict()  # no searched plan: decline definitive arithmetic
    through = int(plan.damage_through or 0)
    dead_attacker = attacker_body["instance_id"] in (plan.attackers_killed_ids or frozenset())
    assigned = [
        body for body in legal if plan.assignments.get(body["instance_id"]) == attacker_body["instance_id"]
    ]
    if not assigned:
        clause = f"their best block leaves the attack unblocked; all {through} damage goes to {victim} this combat."
        outcome = f"their best block leaves {attacker_body['name']} unblocked; all {through} damage goes to {victim}"
        return BlockVerdict(clause=clause, outcome=outcome, unharmed=False, through=through)
    used_names = ", ".join(str(body["name"]) for body in assigned)
    kill_part = (
        f"their best block ({used_names}) kills {desc}"
        if dead_attacker
        else f"their best block ({used_names}) cannot kill {desc}"
    )
    damage_part = (
        f"{through} damage gets through to {victim}"
        if through > 0
        else f"{victim} takes no damage this combat"
    )
    clause = f"{kill_part} and {damage_part}."
    outcome = (
        f"{'their best block kills' if dead_attacker else 'their best block cannot kill'} "
        f"{attacker_body['name']} and {damage_part}"
    )
    return BlockVerdict(clause=clause, outcome=outcome, unharmed=through == 0, through=through)


def best_block_clause(
    attacker: dict,
    blockers: list[dict],
    target: str = "",
    life: int = 20,
    target_card: dict | None = None,
) -> str:
    """What their best block does to one attacker and the thing it attacks.

    Solver-backed single-attacker evaluation with their untapped creatures as
    the defender's pool. Returns "" when nothing definitive can be said."""
    return _best_block_verdict(
        attacker, blockers, target or "the attack's target", life, target_card=target_card
    ).clause


def best_block_outcome(
    attacker: dict,
    blockers: list[dict],
    target: str = "",
    life: int = 20,
    target_card: dict | None = None,
) -> str:
    """The same verdict as one short clause for commitment reconciliation."""
    return _best_block_verdict(
        attacker, blockers, target or "the attack's target", life, target_card=target_card
    ).outcome


def target_permanent(state: dict, target: str):
    """Their permanent named by ``target``, when its name matches (public)."""
    return _target_card(state, target)


def validated_attack_outcome(state: dict, attackers: list[dict] | None, target: str = "") -> str:
    """The solver-validated best-block verdict for this attack, as one clause.

    Empty when the attack is not a single fully-statted attacker or nothing
    definitive can be stated. Planner hooks pass it to
    :func:`held_blocker_reconciliation` so a commitment reversal states its
    consequence on this board instead of asserting a bare reversal."""
    solo = attackers[0] if attackers and len(attackers) == 1 else None
    if solo is None:
        return ""
    untapped = [card for card in _their_creatures(state) if not card.get("is_tapped")]
    if not untapped:
        return ""
    return _best_block_verdict(
        solo,
        untapped,
        target or "the attack's target",
        defender_life(state),
        target_card=_target_card(state, target),
    ).outcome


def _target_card(state: dict, target: str):
    """Their permanent named by ``target``, when its name matches."""
    wanted = str(target or "").casefold().strip()
    if not wanted:
        return None
    seat = _opponent_seat(state)
    for card in state.get("battlefield") or []:
        if seat is not None and card.get("controller_seat_id") != seat:
            continue
        name = str(card.get("name") or "").casefold().strip()
        if name and (wanted in name or name in wanted):
            return card
    return None


def tapped_crackback_correction(
    state: dict,
    reasoning: str,
    attackers: list[dict] | None = None,
    target: str = "",
) -> CrackbackCorrection:
    """The board facts an attack's "tapped enemy limits crackback" premise ignores.

    Returns an empty :class:`CrackbackCorrection` when the reasoning does not

    make that premise or was already corrected. Otherwise independent clauses:

    - current blockers (their untapped creatures): who can block this attack

    now, and - single attacker with full stats - what their best block does;

    - next combat (their tapped creatures): they untap before their next attack.

    Either clause alone fires - an all-tapped enemy board still threatens next

    combat, and an all-untapped one still blocks this attack.

    When a kill/destroy claim meets a validated verdict that leaves the target

    unharmed (or below its known loyalty), mode is ``replace``: the premise

    sentence becomes its attack intent followed by the validated verdict.

    Otherwise mode is ``append``."""
    premise_sentence = next(
        (s for s in _sentences(str(reasoning or "")) if _PREMISE.search(s) and _CONSEQUENCE.search(s)),
        "",
    )
    if not premise_sentence or already_corrected(reasoning):
        return CrackbackCorrection()
    theirs = _their_creatures(state)
    tapped = [card for card in theirs if card.get("is_tapped")]
    untapped = [card for card in theirs if not card.get("is_tapped")]
    solo = attackers[0] if attackers and len(attackers) == 1 else None
    verdict = BlockVerdict()
    clauses: list[str] = []
    if untapped:
        if solo:
            verdict = _best_block_verdict(
                solo,
                untapped,
                target or "the attack's target",
                defender_life(state),
                target_card=_target_card(state, target),
            )
        if verdict.no_legal:
            # Derive the availability claim from the legal set (review finding
            # on 167cdf0): "can still block this attack now" would contradict
            # the computed no-legal-block verdict that follows it.
            clause = f"Their untapped {_describe(untapped)} are still untapped this combat - {verdict.clause}"
        else:
            legal = None
            if solo:
                solo_body = _body(solo, _PROBE_TURN, {})
                if solo_body is not None:
                    legal = _legal_blocker_bodies(solo_body, untapped)
            if legal:
                # Name ONLY the legal members of the pool (review finding on
                # d7fc70f): a mixed pool never credits its illegal members.
                clause = (
                    f"Their untapped {_describe(legal)} can still block this attack now, alone or together"
                )
                clause += f" - {verdict.clause}" if verdict.clause else "."
            else:
                # Legality was not evaluated (multi-attacker plan or missing
                # stats): neutral availability only - never claim they can
                # block (review finding on d7fc70f).
                clause = f"Their untapped {_describe(untapped)} are still untapped this combat."
        clauses.append(clause)
    if tapped:
        clauses.append(
            "Tapped now is not tapped next combat: unless an effect keeps it tapped,"
            f" their tapped {_describe(tapped)} untap before their next attack."
        )
    joined_clauses = " ".join(clauses)
    if not joined_clauses:
        return CrackbackCorrection()
    kill_claimed = bool(_KILL_CLAIM.search(premise_sentence))
    contradicted = False
    if kill_claimed and verdict.unharmed:
        contradicted = True
    elif kill_claimed and verdict.through is not None:
        # Below known planeswalker loyalty also contradicts a kill claim even
        # when some damage gets through.
        target_card = _target_card(state, target)
        target_loyalty = loyalty(target_card) if target_card is not None else None
        if target_loyalty is not None and verdict.through < target_loyalty:
            contradicted = True
    if contradicted:
        head_sentence = premise_sentence.split(";")[0].strip().rstrip(".")
        swapped_head_sentence = _KILL_CLAIM.sub(_swap_kill_match, head_sentence, count=1)
        return CrackbackCorrection(
            sentence=premise_sentence,
            text=f"{swapped_head_sentence}; {joined_clauses}",
            mode="replace",
        )
    return CrackbackCorrection(sentence=premise_sentence, text=joined_clauses, mode="append")


def apply_tapped_crackback(
    state: dict,
    reasoning: str,
    attackers: list[dict] | None = None,
    target: str = "",
) -> tuple[str, CrackbackCorrection]:
    """Return ``(updated_reasoning, finding)`` with the correction applied."""
    finding_result = tapped_crackback_correction(
        state, str(reasoning or ""), attackers=attackers, target=target
    )
    base = str(reasoning or "")
    if not finding_result.text:
        return base.rstrip(), finding_result
    if finding_result.mode == "replace":
        return base.replace(finding_result.sentence, finding_result.text), finding_result
    return f"{base.rstrip()} {finding_result.text}".strip(), finding_result


def _commitment_snippet(reasoning: str, name: str) -> str:
    """The first sentence of ``reasoning`` that names ``name``, trimmed."""
    for sentence in _sentences(str(reasoning or "")):
        if re.search(rf"\b{re.escape(name.casefold())}\b", sentence.casefold()):
            return " ".join(sentence.split())[:120]
    return " ".join(str(reasoning).split())[:120]


def held_blocker_reconciliation(records: list[Any], attacker_names: list[str], outcome: str = "") -> str:
    """State what an attack's commitment reversal costs, on this board.

    Two commitment sources from this turn's decision memory:

    - guard hold-back notes ("attack held by guard: NAME not attacking");
    - the model's own recorded reasoning that names the creature with a
      blocking/save/reserve intent (outage fallbacks excluded).

    Each note states the reversal as fact and - when ``outcome`` carries a
    validated verdict - the consequence on this board. It resolves the
    conflict honestly instead of instructing after the choice, and never
    claims that holding wins."""
    wanted = [str(name).strip() for name in attacker_names or [] if str(name).strip()]
    if not wanted:
        return ""
    held_by_guard: set[str] = set()
    committed: dict[str, str] = {}
    for record in records or []:
        labels = [str(label) for label in getattr(record, "chosen_labels", []) or []]
        joined_labels = ", ".join(labels)
        for label in labels:
            match = _HELD_NOTE.match(label)
            if match:
                held_by_guard.update(name.strip() for name in match.group(1).split(","))
        if getattr(record, "model_unavailable", False):
            continue  # an outage fallback is history,not a commitment
        reasoning = str(getattr(record, "reasoning", "") or "")
        text = f"{joined_labels} {reasoning}".casefold()
        for name in wanted:
            if name in committed:
                continue
            if not re.search(rf"\b{re.escape(name.casefold())}\b", text):
                continue
            if not _COMMITMENT.search(text):
                continue
            snippet = _commitment_snippet(reasoning, name) or joined_labels[:120]
            committed[name] = snippet
    consequence = f" - on this board, {outcome}" if outcome else ""
    notes = []
    spent_guard = sorted(held_by_guard & set(wanted))
    if spent_guard:
        notes.append(f"Attacking with {', '.join(spent_guard)} reverses this turn's hold-back{consequence}.")
    for name in wanted:
        if name in committed:
            notes.append(
                f'Earlier this turn you committed {name} as a blocker ("{committed[name]}"); '
                f"attacking with it reverses that commitment{consequence}."
            )
    return " ".join(notes)
