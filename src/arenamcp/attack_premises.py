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
  :func:`arenamcp.combat_solver.optimal_blocks` searches their best block
  with our single attacker, with :func:`arenamcp.combat_solver._can_block`
  deciding blocking legality. Flying/reach legality, deathtouch,
  indestructible and trample therefore come from the same solver the line
  search uses; this module performs no arithmetic of its own.
- Missing power/toughness on the attacker or ANY candidate blocker means
  no definitive verdict: the clause degrades to who-can-block facts and
  never claims a guaranteed outcome.
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
from arenamcp.combat_solver import _can_block, optimal_blocks
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


def _best_block_verdict(attacker: dict, blockers: list[dict], victim: str, life: int):
    """Evaluate their best block with the production combat solver.

    Returns ``(clause, short_outcome, target_unharmed, damage_through)``;
    ``("", "", None, None)`` when nothing definitive can be stated (no
    blockers, missing power/toughness on the attacker or any candidate, or a
    search that found no plan). Blocking legality (flying/reach/menace),
    deathtouch, indestructible and trample all come from
    :func:`combat_solver.optimal_blocks` / :func:`combat_solver._can_block`
    - no arithmetic here."""
    if not blockers:
        return "", "", None, None
    attacker_body = _body(attacker, _PROBE_TURN, {})
    if attacker_body is None:
        return "", "", None, None
    power = attacker_body["power"]
    toughness = attacker_body["toughness"]
    desc = f"the {power}/{toughness} attacker"
    legal = []
    for card in blockers:
        body = _body(card, _PROBE_TURN, {})
        if body is None:
            return "", "", None, None  # missing stats: decline definitive arithmetic
        if _can_block(attacker_body, body):
            legal.append(body)
    if not legal:
        reason = (
            "only a creature with flying or reach can block a flyer"
            if has_combat_keyword(attacker, "flying")
            else ""
        )
        clause = f"none of their untapped creatures can legally block {desc}"
        if reason:
            clause += f" ({reason})"
        clause += f"; all {power} damage goes to {victim} this combat."
        outcome = (
            f"nothing among their untapped creatures can legally block "
            f"{attacker_body['name']}; all {power} damage goes to {victim}"
        )
        return clause, outcome, False, power
    if has_combat_keyword(attacker, "menace") and len(legal) == 1:
        clause = (
            f"only two or more creatures can block the menacing {desc}; "
            f"all {power} damage goes to {victim} this combat."
        )
        outcome = (
            f"their single legal blocker cannot stop {attacker_body['name']}; "
            f"all {power} damage goes to {victim}"
        )
        return clause, outcome, False, power
    plan = optimal_blocks([attacker_body], legal, max(1, int(life)), caller="attack_premises")
    if plan is None:
        return "", "", None, None  # no searched plan: decline definitive arithmetic
    through = int(plan.damage_through or 0)
    dead_attacker = attacker_body["instance_id"] in (plan.attackers_killed_ids or frozenset())
    assigned = [
        body for body in legal if plan.assignments.get(body["instance_id"]) == attacker_body["instance_id"]
    ]
    if not assigned:
        clause = (
            f"their best block leaves the attack unblocked; all {power} damage goes to {victim} this combat."
        )
        outcome = (
            f"their best block leaves {attacker_body['name']} unblocked; all {power} damage goes to {victim}"
        )
        return clause, outcome, False, through
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
    return clause, outcome, through == 0, through


def best_block_clause(attacker: dict, blockers: list[dict], target: str = "", life: int = 20) -> str:
    """What their best block does to one attacker and the thing it attacks.

    Solver-backed single-attacker evaluation with their untapped creatures as

    the defender's pool. Returns "" when nothing definitive can be said."""
    clause, _, _, _ = _best_block_verdict(attacker, blockers, target or "the attack's target", life)
    return clause


def best_block_outcome(attacker: dict, blockers: list[dict], target: str = "", life: int = 20) -> str:
    """The same verdict as one short clause for commitment reconciliation."""
    _, outcome, _, _ = _best_block_verdict(attacker, blockers, target or "the attack's target", life)
    return outcome


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
    verdict_clause = ""
    unharmed: bool | None = None
    through: int | None = None
    clauses: list[str] = []
    if untapped:
        verdict_clause, _, unharmed, through = (
            _best_block_verdict(solo, untapped, target or "the attack's target", defender_life(state))
            if solo
            else ("", "", None, None)
        )
        clause = f"Their untapped {_describe(untapped)} can still block this attack now, alone or together"
        clause += f" - {verdict_clause}" if verdict_clause else "."
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
    if kill_claimed and unharmed:
        contradicted = True
    elif kill_claimed and through is not None:
        # Below known planeswalker loyalty also contradicts a kill claim even
        # when some damage gets through.
        target_card = _target_card(state, target)
        target_loyalty = loyalty(target_card) if target_card is not None else None
        if target_loyalty is not None and through < target_loyalty:
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
