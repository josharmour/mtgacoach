"""Build the COMBAT DECISION corpus — attack and block SUBSETS, not menu picks.

One record = "here is the board; which creatures attack?" or "which of your
creatures block, and in front of what?"

WHY THIS EXISTS (read before changing anything)
-----------------------------------------------
The 31B LoRA scored 13/63 on strategic decisions — identical to the untuned
base — because its corpus was 69% land drops. The strategic-cast corpus fixed
the *land* reflex but still trains only one decision family: pick a spell from
a menu. Attacks and blocks, the decisions that actually decide Limited games,
were trained on ZERO examples, because they do not fit a pick-one-from-menu
shape: the answer is a SUBSET of creatures.

They were not merely absent — they were actively DISCARDED. ``tools/eval/replay/
strategic_groundtruth.py::build_attack_decision`` drops ``autoDeclare`` and every
multi-creature declaration as ``multi_answer_ambiguous`` so the answer collapses
onto a single menu index. On the owner's 104-replay corpus that is 595 raw
DeclareAttackersReq → **54** kept decisions, and every one of the 54 has a
subset of size 0 or 1. Training on those re-teaches a reflex ("swing with the
one guy / don't swing"), which is the exact failure we are trying to undo.
This module keeps the multi-creature answers instead of dropping them.

REPRESENTATION — WHY (c), THE PRODUCTION ``declare_attackers`` ACTION
--------------------------------------------------------------------
Three options were on the table. The deciding constraint is that whatever the
model emits must be executable by the autopilot *unchanged*.

(a) K generated candidate subsets rendered as a menu, answered ``{"pick": N}``.
    REJECTED — it is silently unexecutable. ``{"pick": N}`` resolves against
    ``ActionPlanner._last_menu`` (action_planner.py ~1756), and at a real
    DeclareAttackers window that menu is built by
    ``RulesEngine._get_decision_actions`` (rules_engine.py ~707-781) as ONE ROW
    PER CREATURE — ``"Attack with: Grizzly Bears (2/2)"`` … plus
    ``"Done (confirm attackers)"``. ``_legal_action_to_action`` turns row N into
    ``DECLARE_ATTACKERS(attacker_names=[<that one creature>])``. So a model
    trained to answer ``{"pick": 2}`` meaning "the all-in candidate" would, at
    serve time, submit an attack with exactly one creature and log nothing
    unusual. Making (a) work would require changing the production menu
    renderer, which is shared by every other decision family.

(b) Per-creature binary attack/no-attack decisions. REJECTED — production opens
    ONE decision window per combat (``decision_context.type ==
    "declare_attackers"``) and expects one action to answer it. There is no
    per-creature request to answer, and N sequential LLM calls per combat does
    not fit the real-time budget.

(c) The production ``declare_attackers`` / ``declare_blockers`` action types
    with an explicit creature list. CHOSEN. This is already the contract:
      * ``ACTION_SCHEMA`` (action_planner.py ~176-195) carries
        ``attacker_names: [...]`` and ``blocker_assignments: {blocker: attacker}``.
      * ``AUTOPILOT_SYSTEM_PROMPT`` states it outright: "EXCEPTION:
        declare_attackers/declare_blockers carry the full set in one action —
        do NOT add a 'done' click."
      * ``_is_legal_combat_declaration`` (action_planner.py ~2648) validates
        ``plan_names.issubset(legal_names)`` — a multi-name set is legal by
        construction, and there is no code path that would accept a
        candidate-subset index.
      * ``autopilot_bridge._try_bridge_declare_attackers`` (~567-649) resolves
        each name to an instance id, toggles each over the GRE bridge, then
        finalizes; an empty list becomes the "confirm no attackers" click. The
        full subset is directly executable.

WHERE THE PAPER'S CANDIDATE-GENERATION IDEA ACTUALLY LANDS (ref [6])
--------------------------------------------------------------------
Dulac-Arnold's answer to a large discrete action space is *generate candidates,
then score* — and production already does exactly that, on the PROMPT side, not
the answer side: ``coach.py`` injects ``"Computed optimal attack: …"`` /
``"Computed optimal blocks: …"`` from ``combat_solver`` and the system prompt
tells the model the line is "the exhaustive answer". So candidate generation
belongs in the context, and the answer stays the executable subset.

That creates one trap this builder handles explicitly. If we render the solver
line and the human label disagrees with it, we would be training the model to
contradict its own system prompt. ``--solver-line`` therefore defaults to
``off`` (with a prompt addendum saying so, mirroring ``build_strategic_casts``'s
CANDIDATE-CAST addendum), and every record still carries the solver's pick and
an agreement flag in ``meta`` so a consumer can filter, weight, or build a DPO
pair from it. ``--solver-line on`` renders it always; ``agree`` renders it only
when it matches the human.

SOURCES AND WHAT EACH ONE CANNOT SUPPLY
---------------------------------------
1. GRE logs — ``--gre-logs`` (manasight Player.log corpus) and ``--replays-dir``
   (the owner's ``.rply`` files). FULL FIDELITY: real instance ids, the real
   qualified-attacker pool, real blocker→attacker pairings, real tapped state.
   The pool comes from the FIRST request: ``declareAttackersReq.attackers``
   equals ``qualifiedAttackers`` in 338/339 manasight episodes (and 307/307
   replay episodes per strategic_groundtruth). The label is NOT a union of
   responses: every toggle — select AND deselect — arrives as a
   ``declareAttackersResp.selectedAttackers`` entry naming the creature (a
   deselect merely omits ``selectedDamageRecipient``), so a union keeps every
   creature the player clicked on and then clicked off. The label is the
   DECLARED state of the LAST re-issued request
   (``attackers[].selectedDamageRecipient != null`` — the client's own
   ``DeclaredAttackers`` definition in DeclareAttackerRequest.cs) plus the
   final toggle/commit paired with that request; ``autoDeclare`` as the
   final act means the whole qualified pool. Block labels replay toggles in
   order and a retraction (empty ``selectedAttackerInstanceIds``) REMOVES
   the assignment. The block-side attacker roster comes from snapshot
   ``gameObjects.attackState`` — ``blockers[].attackerInstanceIds`` is the
   per-blocker LEGAL-target list and silently omits attackers nobody can
   block (a flier over a ground board).
2. 17lands ``replay_data`` — ``--replay-csv``. VOLUME: ``user_turn_N_creatures_attacked``
   is the attacker set for every turn of every game. ``oppo_turn_N_creatures_blocking``
   is the USER's blocker set during the opponent's turn N, and
   ``oppo_turn_N_creatures_attacked`` is what they were blocking.
   WHAT IT CANNOT SUPPLY:
     * No blocker→attacker pairing. With exactly one attacker every block is
       forced onto it and a complete ``blocker_assignments`` is recoverable;
       with two or more attackers it is NOT, and the whole window is dropped
       — blocked AND unblocked alike, because keeping only the no-block half
       taught P(block | 2+ attackers) = 0, a conditional reflex the marginal
       answer-mix audit cannot see. ``--block-pairing drop`` is a
       compatibility alias with the same behavior (it formerly fell through
       to a fabricated every-blocker→first-attacker mapping).
     * No targets, no instance ids, no tapped state, no stack, no instant-speed
       combat tricks, no first-strike/damage-order sub-decisions.
     * No within-combat ordering, so "attacked then cast a pump spell" collapses.
     * Multiple copies of one card are indistinguishable; names are
       disambiguated ``#1/#2`` the same way ``RulesEngine._disambiguate_names``
       does, and the label picks copies positionally.
3. ``--replay-groundtruth`` reads the DERIVED
   ``tools/eval/data/replay_strategic_groundtruth.jsonl``. Every attack record in
   it has a subset of size ≤ 1 by construction (see above), so it is OFF by
   default; turning it on re-injects the single-attacker bias.

DATA / LICENCE
--------------
17Lands ``replay_data`` is CC BY 4.0 and derived datasets must credit "17Lands".
manasight-corpus is MIT OR Apache-2.0 and requires the upstream notice plus a
statement that the files were changed. Both attributions are written onto every
record and into the stats sidecar.

CPU only. No GPU, no network.

Usage
-----
    # audit the 17lands funnel without writing a corpus
    python -m tools.training.build_combat_decisions \
        --replay-csv tools/eval/data/17lands/replay_data_public.EOE.PremierDraft.csv.gz \
        --max-rows-per-file 4000 --audit-only

    # full build across every source
    python -m tools.training.build_combat_decisions \
        --replay-csv tools/eval/data/17lands/replay_data_public.EOE.PremierDraft.csv.gz \
        --replay-csv tools/eval/data/17lands/replay_data_public.OTJ.PremierDraft.csv.gz \
        --gre-logs ~/.arenamcp/corpora/manasight-corpus-main/corpus \
        --max-turn 12 --min-pool 2 \
        --out tools/training/data/combat_decisions.jsonl \
        --stats-out tools/training/data/combat_decisions_stats.json
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import re
import sys
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("build_combat_decisions")

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Reconstruction + rendering are IMPORTED, never re-implemented. This module
# owns only the combat-specific parts.
from tools.training.build_play_decisions import (  # noqa: E402
    CardDetail,
    _board_lines,
    _mana_line,
    _num,
    predecessor_prefix,
)
from tools.training.synthesize_menu import CardResolver, iter_rows  # noqa: E402

from arenamcp.action_planner import AUTOPILOT_SYSTEM_PROMPT  # noqa: E402
from arenamcp.combat_solver import optimal_attacks, optimal_blocks  # noqa: E402

# Production's attackState parser — the block-side attacker roster must come
# from the same enum production trusts, not from per-blocker legal-target
# lists (which omit attackers nobody can block).
from arenamcp.gamestate_transforms import _parse_attack_state  # noqa: E402

SOURCE_17LANDS = "17lands-replay_data"
SOURCE_GRE_LOG = "manasight-player-log"
SOURCE_RPLY = "mtga-rply"
SOURCE_DERIVED = "replay-strategic-groundtruth"
DATASET = "combat_decisions"

ATTRIBUTION_17LANDS = "17Lands (CC BY 4.0)"
ATTRIBUTION_MANASIGHT = (
    "Derived from manasight-corpus (https://github.com/manasight/manasight-corpus), "
    "sanitized MTG Arena Player.log captures. Dual-licensed MIT OR Apache-2.0. "
    "Redistribution must carry the upstream copyright notice and license text; "
    "Apache-2.0 additionally requires stating that the files were changed — they "
    "were: MTGA GRE combat episodes were re-serialized into the mtgacoach "
    "combat-decision record schema by tools/training/build_combat_decisions.py."
)
ATTRIBUTION_OWNER = "mtgacoach owner .rply captures (private)"

# Production trigger strings, verbatim from
# ``ActionPlanner._build_action_prompt.trigger_descriptions``.
TRIGGER_ATTACK = "TRIGGER: Declare attackers phase. Choose which creatures attack."
TRIGGER_BLOCK = "TRIGGER: Opponent is attacking. Assign blockers."

DONE_ATTACK_ROW = "Done (confirm attackers)"
DONE_BLOCK_ROW = "Done (confirm blockers)"


COMBAT_ADDENDUM_COMMON = """
COMBAT-DECLARATION MODE (read this, it OVERRIDES the "pick by number" rule
above for this prompt):
- This decision is a SUBSET, not a menu pick. Do NOT answer with {"pick": N} —
  a single menu row can only ever name one creature, and the correct answer is
  frequently two or more creatures, or none at all.
- Answer with exactly one structured action and nothing else:
"""

ATTACK_ADDENDUM = (
    COMBAT_ADDENDUM_COMMON
    + """    {"actions":[{"action_type":"declare_attackers","attacker_names":["A","B"]}]}
  Use the creature names EXACTLY as they appear in the numbered list, including
  any "#1"/"#2" disambiguator, and WITHOUT the "(P/T)" annotation.
- To decline combat entirely, answer with an empty list:
    {"actions":[{"action_type":"declare_attackers","attacker_names":[]}]}
  Never emit a separate "Done" action — the declaration carries the whole set.
- Attacking with everything and attacking with nobody are both real answers.
  Hold back the blockers you need; swing with the ones that trade up or get
  through. No reasoning field, no prose, no markdown.
"""
)

BLOCK_ADDENDUM = (
    COMBAT_ADDENDUM_COMMON
    + """    {"actions":[{"action_type":"declare_blockers","blocker_assignments":{"YourCreature":"TheirAttacker"}}]}
  Keys are YOUR blockers, values are the attacker each one blocks. Use names
  EXACTLY as listed, including any "#1"/"#2" disambiguator.
- To take the damage instead, answer with an empty mapping:
    {"actions":[{"action_type":"declare_blockers","blocker_assignments":{}}]}
  Never emit a separate "Done" action — the declaration carries every block.
- No reasoning field, no prose, no markdown.
"""
)

NO_SOLVER_ADDENDUM = """
- There is NO "Computed optimal attack:" / "Computed optimal blocks:" line in
  this prompt. This is a reconstructed historical position and the solver line
  was deliberately withheld; the deference rule in the system prompt does not
  apply here. Work the combat math out yourself from the board shown.
"""

RECONSTRUCTED_ADDENDUM = """
- RECONSTRUCTED POSITION: this board was rebuilt from a recorded game. There
  is no stack, no instance ids and no instant-speed window. Creatures marked
  [tapped], [cannot attack: defender] or [cannot attack: summoning sick] are
  real board context that cannot be declared this combat. Assume every
  creature listed as a legal attacker/blocker really is one, and judge only
  the combat in front of you.
"""


# ---------------------------------------------------------------------------
# Card facts
# ---------------------------------------------------------------------------


class CombatFacts:
    """Memoised keyword / P-T lookups used by both the filters and the solver."""

    _KEYWORDS = (
        "haste",
        "defender",
        "vigilance",
        "flying",
        "reach",
        "menace",
        "deathtouch",
        "trample",
        "first strike",
        "double strike",
        "lifelink",
        "indestructible",
    )

    _REMINDER_RE = re.compile(r"\([^)]*\)")

    def __init__(self, resolver: CardResolver, detail: CardDetail):
        self.resolver = resolver
        self.detail = detail
        self._oracle: dict[int, str] = {}
        self._kw: dict[int, frozenset[str]] = {}
        self._pt: dict[int, tuple[Optional[int], Optional[int]]] = {}

    def oracle(self, gid: int) -> str:
        got = self._oracle.get(gid)
        if got is None:
            got = (self.detail.oracle(self.resolver.get(gid), 4000) or "").lower()
            self._oracle[gid] = got
        return got

    def keywords(self, gid: int) -> frozenset[str]:
        """Static keyword abilities on the FRONT face, parsed as line-initial
        comma/semicolon token lists with reminder text stripped.

        Bare substring search over the merged-both-faces oracle made
        '{T}: Target creature gains haste' a haste creature, 'create a 1/1
        token with flying' a flier, and let back-face keywords leak onto the
        battlefield face — poisoning the pool filters, the evasion map and
        the solver.
        """
        got = self._kw.get(gid)
        if got is None:
            raw = self.detail.raw(self.resolver.get(gid))
            text = raw.get("oracle_text")
            if text is None:
                faces = raw.get("card_faces") or []
                text = (faces[0].get("oracle_text") or "") if faces else ""
            text = self._REMINDER_RE.sub("", text or "")
            toks: set[str] = set()
            for ln in text.split("\n"):
                for part in re.split(r"[,;]", ln):
                    tok = part.strip().strip(".").lower()
                    if tok in self._KEYWORDS:
                        toks.add(tok)
            got = frozenset(toks)
            self._kw[gid] = got
        return got

    def has(self, gid: int, keyword: str) -> bool:
        return keyword in self.keywords(gid)

    def name(self, gid: int) -> str:
        return self.resolver.get(gid).name or f"grpId {gid}"

    def pt(self, gid: int) -> tuple[Optional[int], Optional[int]]:
        got = self._pt.get(gid)
        if got is None:
            raw = self.detail.pt(self.resolver.get(gid))
            p = t = None
            if raw and "/" in raw:
                a, b = raw.split("/", 1)
                p = _int_or_none(a)
                t = _int_or_none(b)
            got = (p, t)
            self._pt[gid] = got
        return got

    def is_creature(self, gid: int) -> bool:
        return "creature" in (self.resolver.get(gid).type_line or "").lower()

    def solver_card(
        self,
        gid: int,
        instance_id: int,
        name: Optional[str] = None,
        live: Optional[tuple[Optional[int], Optional[int], bool]] = None,
    ) -> Optional[dict]:
        """A ``combat_solver``-shaped dict, or None when P/T is unresolvable.

        Live GRE power/toughness (which already fold in +1/+1 counters,
        auras and equipment) win over printed Scryfall P/T; printed values
        are the fallback for 17lands, which has no live state. A variable
        printed P/T (``*/*``) with no live value used to collapse to 0/0 —
        the solver read a Tarmogoyf as a harmless 0/0 — so it now returns
        None and the caller skips the solver (counted) instead of guessing.

        ``oracle_text`` is the canonical front-face keyword list, NOT raw
        oracle text: ``combat_solver._has`` is a substring test, so raw text
        re-introduced the 'gains haste'/token/back-face false positives this
        class's ``keywords()`` exists to kill.
        """
        p = live[0] if live else None
        t = live[1] if live else None
        if p is None or t is None:
            pp, pt_ = self.pt(gid)
            p = p if p is not None else pp
            t = t if t is not None else pt_
        if p is None or t is None:
            return None
        return {
            "name": name or self.name(gid),
            "instance_id": int(instance_id),
            "grp_id": int(gid),
            "power": int(p),
            "toughness": int(t),
            "oracle_text": ", ".join(sorted(self.keywords(gid))),
        }


def _int_or_none(s: str) -> Optional[int]:
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return None


def disambiguate(names: list[str]) -> list[str]:
    """Byte-identical to ``RulesEngine._disambiguate_names`` — duplicates get
    ``#1``/``#2`` suffixes in list order. Reimplemented rather than imported so
    the corpus does not depend on importing the whole rules engine, and pinned
    by ``tests`` against the production helper."""
    counts = Counter(names)
    seen: dict[str, int] = {}
    out: list[str] = []
    for name in names:
        if counts[name] > 1:
            seen[name] = seen.get(name, 0) + 1
            out.append(f"{name} #{seen[name]}")
        else:
            out.append(name)
    return out


def attack_row(display_name: str, p: Optional[int], t: Optional[int], oracle: str) -> str:
    """Reproduce ``RulesEngine._get_decision_actions``'s attack row, including the
    0-power warning it appends, so the training menu is the production menu."""
    suffix = ""
    if p is not None and t is not None:
        suffix = f" ({p}/{t})"
        if p == 0:
            attack_trigger = ("whenever" in oracle and "attack" in oracle) or "exert" in oracle
            if not attack_trigger:
                suffix += " [0 POWER — attacking deals 0 damage]"
    return f"Attack with: {display_name}{suffix}"


# ---------------------------------------------------------------------------
# Decision containers
# ---------------------------------------------------------------------------


@dataclass
class AttackDecision:
    """One declare-attackers window, source-independent."""

    source: str
    key: str
    turn: int
    on_play: Optional[bool]
    # (display_name, grp_id, instance_id) in menu order
    pool: list[tuple[str, int, int]] = field(default_factory=list)
    chosen_instance_ids: list[int] = field(default_factory=list)
    life: Optional[int] = None
    opp_life: Optional[int] = None
    opp_hand: Optional[int] = None
    own_board: list[int] = field(default_factory=list)
    own_lands: list[int] = field(default_factory=list)
    opp_board: list[int] = field(default_factory=list)
    opp_creatures: list[tuple[str, int, int]] = field(default_factory=list)
    # Untapped opp creatures only — production (coach.py) blocks with
    # ``not is_tapped``; passing tapped creatures modeled phantom blockers.
    # None means the source has no tapped info (17lands) and all
    # opp_creatures are used.
    opp_blockers: Optional[list[tuple[str, int, int]]] = None
    # Untapped creatures NOT in the candidate pool — production's
    # ``your_remaining_blockers``. The old ``pool minus human-chosen`` leaked
    # the label into the solver and double-counted held-back creatures as
    # crackback defenders (optimal_attacks re-derives held_back per subset).
    remaining_blockers: list[tuple[str, int, int]] = field(default_factory=list)
    # instance_id -> (live power, live toughness, is_tapped) from the GRE
    # snapshot; empty for reconstructed (17lands) sources.
    live: dict[int, tuple[Optional[int], Optional[int], bool]] = field(default_factory=dict)
    # GRE sources render creatures with live state; (grp_id, power,
    # toughness, tapped) per instance. None -> printed-P/T _board_lines.
    own_board_live: Optional[list[tuple[int, Optional[int], Optional[int], bool]]] = None
    opp_board_live: Optional[list[tuple[int, Optional[int], Optional[int], bool]]] = None
    # (grp_id, annotation) context creatures excluded from the menu but on
    # the real battlefield — defenders, summoning-sick, tapped. Hiding them
    # taught all-ins against an apparently undefended home board.
    own_board_extra: list[tuple[int, str]] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


@dataclass
class BlockDecision:
    """One declare-blockers window, source-independent."""

    source: str
    key: str
    turn: int
    on_play: Optional[bool]
    blockers: list[tuple[str, int, int]] = field(default_factory=list)
    attackers: list[tuple[str, int, int]] = field(default_factory=list)
    # blocker instance id -> attacker instance id
    assignments: dict[int, int] = field(default_factory=dict)
    # blocker instance id -> attacker instance ids it may legally block
    allowed: dict[int, set[int]] = field(default_factory=dict)
    life: Optional[int] = None
    opp_life: Optional[int] = None
    opp_hand: Optional[int] = None
    own_board: list[int] = field(default_factory=list)
    own_lands: list[int] = field(default_factory=list)
    opp_board: list[int] = field(default_factory=list)
    # See AttackDecision — live GRE state and board-context annotations.
    live: dict[int, tuple[Optional[int], Optional[int], bool]] = field(default_factory=dict)
    own_board_live: Optional[list[tuple[int, Optional[int], Optional[int], bool]]] = None
    opp_board_live: Optional[list[tuple[int, Optional[int], Optional[int], bool]]] = None
    own_board_extra: list[tuple[int, str]] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Solver bridge (paper ref [6]: generate candidates, score them)
# ---------------------------------------------------------------------------


def _solver_cards(
    facts: CombatFacts,
    entries: list[tuple[str, int, int]],
    live: dict[int, tuple[Optional[int], Optional[int], bool]],
) -> tuple[Optional[list[dict]], bool]:
    """(cards, any_printed_fallback), cards=None when any P/T is unresolvable.

    None means the position cannot be scored honestly (variable ``*/*``
    printed P/T with no live value) — the caller must skip the solver and
    count the reason, never feed 0/0.
    """
    out: list[dict] = []
    printed = False
    for name, gid, iid in entries:
        lp = live.get(iid)
        if lp is None or lp[0] is None or lp[1] is None:
            printed = True
        card = facts.solver_card(gid, iid, name, live=lp)
        if card is None:
            return None, printed
        out.append(card)
    return out, printed


def solve_attack(facts: CombatFacts, dec: AttackDecision, stats: Counter) -> Optional[dict]:
    """Run ``combat_solver.optimal_attacks`` over the reconstructed position.

    Returns ``{"ids", "names", "explanation", "score"}`` or None. Never raises —
    a solver failure must not drop a real decision from the corpus.
    """
    try:
        cands, p1 = _solver_cards(facts, dec.pool, dec.live)
        opp_all, p2 = _solver_cards(facts, dec.opp_creatures, dec.live)
        opp_blk_entries = dec.opp_blockers if dec.opp_blockers is not None else dec.opp_creatures
        opp_blk, p3 = _solver_cards(facts, opp_blk_entries, dec.live)
        # Production parity (coach.py your_remaining_blockers): the untapped
        # creatures OUTSIDE the candidate pool. The old ``pool minus the
        # human's pick`` leaked the label into the "independent" solver and
        # double-counted every held creature as a crackback defender.
        held, p4 = _solver_cards(facts, dec.remaining_blockers, dec.live)
        if cands is None or opp_all is None or opp_blk is None or held is None:
            stats["solver_skip_variable_pt"] += 1
            return None
        if p1 or p2 or p3 or p4:
            # 17lands has no live values; printed Scryfall P/T is the
            # counted fallback, not silent truth.
            stats["solver_pt_printed_fallback"] += 1
        plan = optimal_attacks(
            cands,
            opp_blk,
            int(dec.opp_life if dec.opp_life is not None else 20),
            int(dec.life if dec.life is not None else 20),
            opp_all,  # worst case: everything the opponent has untaps and swings back
            held,
        )
        if plan is None:
            return None
        return {
            "ids": sorted(int(x) for x in plan.attacker_ids),
            "names": list(plan.attacker_names),
            "explanation": plan.explanation,
            "score": round(float(plan.score), 3),
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug("attack solver failed on %s: %s", dec.key, exc)
        return None


def solve_block(facts: CombatFacts, dec: BlockDecision, stats: Counter) -> Optional[dict]:
    try:
        attackers, p1 = _solver_cards(facts, dec.attackers, dec.live)
        blockers, p2 = _solver_cards(facts, dec.blockers, dec.live)
        if attackers is None or blockers is None:
            stats["solver_skip_variable_pt"] += 1
            return None
        if p1 or p2:
            stats["solver_pt_printed_fallback"] += 1
        allowed = {k: set(v) for k, v in dec.allowed.items()} or None
        plan = optimal_blocks(
            attackers,
            blockers,
            int(dec.life if dec.life is not None else 20),
            blocker_allowed_attackers=allowed,
        )
        if plan is None:
            return None
        return {
            "assignments": {int(k): int(v) for k, v in plan.assignments.items()},
            "explanation": plan.explanation,
            "score": round(float(plan.score), 3),
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug("block solver failed on %s: %s", dec.key, exc)
        return None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _life_line(dec: Any) -> str:
    you = f"{dec.life:.0f}" if dec.life is not None else "20"
    opp = f"{dec.opp_life:.0f}" if dec.opp_life is not None else "20"
    return f"Life: You={you} Opp={opp}"


def _resolved_pt(
    facts: CombatFacts, gid: int, live: dict[int, tuple[Optional[int], Optional[int], bool]], iid: int
) -> tuple[Optional[int], Optional[int]]:
    """Live GRE P/T when the source carries it, else printed. Rendering the
    printed 2/2 for a counter-buffed 4/4 taught attacks that read as
    suicides."""
    lp = live.get(iid)
    p = lp[0] if lp else None
    t = lp[1] if lp else None
    if p is None or t is None:
        pp, pt_ = facts.pt(gid)
        p = p if p is not None else pp
        t = t if t is not None else pt_
    return p, t


def _live_board_lines(
    facts: CombatFacts,
    entries: list[tuple[int, Optional[int], Optional[int], bool]],
    oracle_chars: int,
) -> list[str]:
    """``_board_lines`` with LIVE GRE state substituted for printed P/T.

    Groups identical (grpId, state) copies the way ``_board_lines`` groups
    grpIds; a [tapped] marker replaces the fiction that every rendered
    creature was ready to fight.
    """
    out: list[str] = []
    groups = Counter(entries)
    for key in dict.fromkeys(entries):
        gid, p, t, tapped = key
        card = facts.resolver.get(gid)
        bits = [card.name or f"grpId {gid}"]
        n = groups[key]
        if n > 1:
            bits.append(f"x{n}")
        if p is not None and t is not None:
            bits.append(f"{p}/{t}")
        else:
            pt = facts.detail.pt(card)
            if pt:
                bits.append(pt)
        if tapped:
            bits.append("[tapped]")
        if card.type_line:
            bits.append(f"({card.type_line})")
        text = facts.detail.oracle(card, oracle_chars)
        if text:
            bits.append(f"- {text}")
        out.append("  " + " ".join(bits))
    return out


def _own_board_block(facts: CombatFacts, dec: Any, oracle_chars: int) -> list[str]:
    """YOUR BOARD lines: live lines (GRE) or printed lines plus the annotated
    context creatures (defenders/summoning-sick/tapped) the menu excludes.
    Omitting them rendered a defended board as an empty one."""
    lines: list[str] = []
    if dec.own_board_live is not None:
        lines.extend(_live_board_lines(facts, dec.own_board_live, oracle_chars))
    lines.extend(_board_lines(facts.resolver, facts.detail, dec.own_board, oracle_chars))
    extra_groups = Counter(dec.own_board_extra)
    for gid, note in dict.fromkeys(dec.own_board_extra):
        for ln in _board_lines(facts.resolver, facts.detail, [gid] * extra_groups[(gid, note)], oracle_chars):
            lines.append(f"{ln} {note}")
    return lines


def _opp_board_block(facts: CombatFacts, dec: Any, oracle_chars: int) -> list[str]:
    lines: list[str] = []
    if dec.opp_board_live is not None:
        lines.extend(_live_board_lines(facts, dec.opp_board_live, oracle_chars))
    lines.extend(_board_lines(facts.resolver, facts.detail, dec.opp_board, oracle_chars))
    return lines


def render_attack_user(
    facts: CombatFacts, dec: AttackDecision, oracle_chars: int, solver_line: Optional[str]
) -> tuple[str, list[str]]:
    # Menu rows use live P/T when the source carries it — production's menu
    # is built from live game state, so a printed-P/T row would diverge from
    # what the model sees at serve time.
    menu = [
        attack_row(name, *_resolved_pt(facts, gid, dec.live, iid), facts.oracle(gid))
        for (name, gid, iid) in dec.pool
    ] + [DONE_ATTACK_ROW]
    lines = ["=== GAME ===", "Legal: (pick by number)"]
    lines.extend(f"  {i + 1}. {row}" for i, row in enumerate(menu))
    lines.append(f"T{dec.turn} YOUR | Combat | Pri:You")
    lines.append("Pending decision: Declare Attackers")
    lines.append(_life_line(dec))
    lines.append(_mana_line(facts.resolver, dec.own_lands))
    if dec.on_play is not None:
        lines.append(f"On the {'play' if dec.on_play else 'draw'}.")
    if dec.opp_hand is not None:
        lines.append(f"Opp hand: {dec.opp_hand:.0f} cards")
    if solver_line:
        lines.append(solver_line)
    lines.append("")
    lines.append("YOUR BOARD:")
    lines.extend(_own_board_block(facts, dec, oracle_chars) or ["  (empty)"])
    lines.append("")
    lines.append("OPP BOARD:")
    lines.extend(_opp_board_block(facts, dec, oracle_chars) or ["  (empty)"])
    return TRIGGER_ATTACK + "\n\n" + "\n".join(lines), menu


def render_block_user(
    facts: CombatFacts, dec: BlockDecision, oracle_chars: int, solver_line: Optional[str]
) -> tuple[str, list[str]]:
    menu = [f"Block with: {name}" for (name, _g, _i) in dec.blockers] + [DONE_BLOCK_ROW]
    lines = ["=== GAME ===", "Legal: (pick by number)"]
    lines.extend(f"  {i + 1}. {row}" for i, row in enumerate(menu))
    lines.append(f"T{dec.turn} OPP | Combat | Pri:You")
    lines.append("Pending decision: Declare Blockers")
    lines.append(_life_line(dec))
    lines.append(_mana_line(facts.resolver, dec.own_lands))
    if dec.on_play is not None:
        lines.append(f"On the {'play' if dec.on_play else 'draw'}.")
    if dec.opp_hand is not None:
        lines.append(f"Opp hand: {dec.opp_hand:.0f} cards")
    lines.append("")
    lines.append("ATTACKING YOU:")
    for name, gid, iid in dec.attackers:
        # Live P/T when available — an aura'd/countered attacker rendered at
        # printed size understates the attack the human actually faced.
        p, t = _resolved_pt(facts, gid, dec.live, iid)
        pt = f" {p}/{t}" if p is not None and t is not None else ""
        text = facts.detail.oracle(facts.resolver.get(gid), oracle_chars)
        lines.append(f"  {name}{pt}" + (f" - {text}" if text else ""))
    if solver_line:
        lines.append(solver_line)
    lines.append("")
    lines.append("YOUR BOARD:")
    lines.extend(_own_board_block(facts, dec, oracle_chars) or ["  (empty)"])
    lines.append("")
    lines.append("OPP BOARD:")
    lines.extend(_opp_board_block(facts, dec, oracle_chars) or ["  (empty)"])
    return TRIGGER_BLOCK + "\n\n" + "\n".join(lines), menu


def attack_response(names: list[str]) -> str:
    return json.dumps(
        {"actions": [{"action_type": "declare_attackers", "attacker_names": names}]},
        ensure_ascii=False,
    )


def block_response(assignments: dict[str, str]) -> str:
    return json.dumps(
        {"actions": [{"action_type": "declare_blockers", "blocker_assignments": assignments}]},
        ensure_ascii=False,
    )


def build_system(kind: str, reconstructed: bool, solver_line_present: bool) -> str:
    addendum = ATTACK_ADDENDUM if kind == "attack" else BLOCK_ADDENDUM
    if not solver_line_present:
        addendum += NO_SOLVER_ADDENDUM
    if reconstructed:
        addendum += RECONSTRUCTED_ADDENDUM
    return AUTOPILOT_SYSTEM_PROMPT + "\n" + addendum


def _rid(prefix: str, key: str) -> str:
    return f"{prefix}-{hashlib.sha1(key.encode()).hexdigest()[:16]}"


def _canonical_attack_explanation(solved: dict, human_names: list[str]) -> str:
    """Solver explanation with the HUMAN's copy names in the attacker list.

    ``_attack_agrees`` compares (grpId, live state) multisets, so agreement
    can be satisfied by copy-equivalence — the solver picks Bear #1 where the
    human picked Bear #2. Rendering the solver's own names would then put
    "Computed optimal attack: attack with Bear #1" above a response declaring
    Bear #2 — a record that trains the model to contradict the very line the
    system prompt tells it to defer to. The line only renders when agreement
    holds (see ``_solver_line``), so substituting the human's names is exact:
    the subsets are state-identical and the tail ("N through, crackback M
    (lose material ..., kill ...)") is name-free.
    """
    _head, sep, tail = str(solved["explanation"]).partition("; ")
    return f"attack with {', '.join(human_names) or 'nobody'}" + sep + tail


def _canonical_block_explanation(dec: BlockDecision, solved: dict) -> str:
    """Solver explanation with the HUMAN's copy names throughout.

    Same copy-equivalence hazard as ``_canonical_attack_explanation``, but the
    block line names creatures in two places: the "X blocks Y" description and
    the tail's "kills ... / loses ..." lists. The description is rebuilt from
    the human's assignments (blocker-pool order, the same order the response
    dict is built in); tail names are remapped through the copy bijection
    implied by pairing equivalence-equal assignment entries, replaced in one
    alternation pass sorted longest-first so "Bear #1" never clobbers part of
    "Bear #10" and name swaps cannot cascade. The tail is dropped instead of
    remapped when the two plans differ in gang STRUCTURE (the pair-multiset
    agreement test cannot see whether a double-block was split across
    identical attacker copies, and the tail's kill/loss claims come from the
    solver's structure) or when a remap conflict appears (one solver copy
    needing two different human names): the numbers lost are garnish, a
    name or claim the response contradicts is a contradiction. This also
    normalizes the greedy fallback's "greedy: X -> atk#<instance id>" form,
    which leaked raw instance ids into prompts.
    """
    b_name = {i: n for (n, _g, i) in dec.blockers}
    a_name = {i: n for (n, _g, i) in dec.attackers}
    b_gid = {i: g for (_n, g, i) in dec.blockers}
    a_gid = {i: g for (_n, g, i) in dec.attackers}

    def key(gids: dict, iid: int) -> tuple:
        # Mirrors Emitter._state_key — the identity agreement was judged on.
        return (gids.get(int(iid)), dec.live.get(int(iid)))

    desc = (
        ", ".join(
            f"{b_name[b]} blocks {a_name[dec.assignments[b]]}"
            for (_n, _g, b) in dec.blockers
            if b in dec.assignments
        )
        or "no blocks"
    )

    # Gang structure must match before the tail's kill/loss claims can be
    # renamed: per attacker copy, the multiset of blocker states ganged onto
    # it. Pair-multiset agreement holds for "split two chumps across twin
    # Wolves" vs "double-block one Wolf", but the outcomes (and the tail)
    # differ — repr-sorted because state keys mix ints, None and tuples.
    def gang_struct(items: Iterable[tuple[int, int]]) -> Counter:
        per_atk: dict[int, list[str]] = {}
        for b, a in items:
            per_atk.setdefault(int(a), []).append(repr(key(b_gid, b)))
        return Counter((repr(key(a_gid, a)), tuple(sorted(bs))) for a, bs in per_atk.items())

    conflict = gang_struct(dec.assignments.items()) != gang_struct(
        (int(b), int(a)) for b, a in solved["assignments"].items()
    )

    # Pair each solver assignment with an equivalence-equal human assignment;
    # copies inside a bucket are state-identical, so the sorted zip is an
    # arbitrary-but-deterministic bijection.
    buckets: dict[tuple, list[tuple[int, int]]] = {}
    for b, a in dec.assignments.items():
        buckets.setdefault((key(b_gid, b), key(a_gid, a)), []).append((int(b), int(a)))
    for pairs in buckets.values():
        pairs.sort(key=lambda ba: (b_name.get(ba[0], ""), a_name.get(ba[1], "")))
    renames: dict[str, str] = {}
    solver_items = (
        []  # structure mismatch already drops the tail; renames are moot
        if conflict
        else sorted(
            ((int(b), int(a)) for b, a in solved["assignments"].items()),
            key=lambda ba: (b_name.get(ba[0], ""), a_name.get(ba[1], "")),
        )
    )
    for sb, sa in solver_items:
        bucket = buckets.get((key(b_gid, sb), key(a_gid, sa)))
        if not bucket:
            conflict = True  # unreachable when agreement held; never guess
            break
        hb, ha = bucket.pop(0)
        for src, dst in ((b_name.get(sb), b_name.get(hb)), (a_name.get(sa), a_name.get(ha))):
            if src is None or dst is None or src == dst:
                continue
            if renames.setdefault(src, dst) != dst:
                conflict = True
                break
        if conflict:
            break

    m = re.search(r" \(([^()]*)\)$", str(solved["explanation"]))
    tail = m.group(1) if m and not conflict else ""
    if tail and renames:
        rx = re.compile("|".join(re.escape(s) for s in sorted(renames, key=len, reverse=True)))
        tail = rx.sub(lambda mm: renames[mm.group(0)], tail)
    return f"{desc} ({tail})" if tail else desc


# ---------------------------------------------------------------------------
# Record emission
# ---------------------------------------------------------------------------


class Emitter:
    def __init__(self, facts: CombatFacts, args: argparse.Namespace):
        self.facts = facts
        self.args = args
        self.stats: Counter[str] = Counter()
        self.pool_hist: Counter[int] = Counter()
        self.chosen_hist: Counter[int] = Counter()
        self.block_pool_hist: Counter[int] = Counter()
        self.block_chosen_hist: Counter[int] = Counter()
        self.by_source: Counter[str] = Counter()
        self.solver_agree: Counter[str] = Counter()
        self.answer_mix: Counter[str] = Counter()

    def _solver_line(self, kind: str, agrees: Optional[bool], explanation: Optional[str]) -> Optional[str]:
        mode = self.args.solver_line
        if mode == "off" or not explanation:
            return None
        # Never render a solver line that contradicts the record's response:
        # the system prompt's deference rule demands exact compliance (or a
        # stated reason, which the combat addenda forbid), so a disagreeing
        # line + a bare contradicting label trains the model that the
        # deference rule is noise. Under "on" the caller DROPS disagreeing
        # records (counted); under "agree" they keep their label, lose the
        # line, and NO_SOLVER_ADDENDUM applies.
        if mode in ("on", "agree") and not agrees:
            return None
        label = "Computed optimal attack" if kind == "attack" else "Computed optimal blocks"
        return f"{label}: {explanation}"

    def _state_key(self, dec: Any, gid_by_iid: dict[int, Optional[int]], iid: int):
        """(grpId, live state) — the identity that matters for agreement.

        Instance-id-exact comparison flagged the solver's mirror assignment
        among identical copies (Bears #1->A/#2->B vs #1->B/#2->A) as
        disagreement, deflating agreement stats exactly on duplicate-common
        boards."""
        return (gid_by_iid.get(int(iid)), dec.live.get(int(iid)))

    def _attack_agrees(self, dec: AttackDecision, solver_ids: list[int]) -> bool:
        gid_by_iid: dict[int, Optional[int]] = {i: g for (_n, g, i) in dec.pool}
        return Counter(self._state_key(dec, gid_by_iid, i) for i in solver_ids) == Counter(
            self._state_key(dec, gid_by_iid, i) for i in dec.chosen_instance_ids
        )

    def _block_agrees(self, dec: BlockDecision, solver_assignments: dict) -> bool:
        b_gid: dict[int, Optional[int]] = {i: g for (_n, g, i) in dec.blockers}
        a_gid: dict[int, Optional[int]] = {i: g for (_n, g, i) in dec.attackers}
        human = Counter(
            (self._state_key(dec, b_gid, b), self._state_key(dec, a_gid, a))
            for b, a in dec.assignments.items()
        )
        solver = Counter(
            (self._state_key(dec, b_gid, int(b)), self._state_key(dec, a_gid, int(a)))
            for b, a in solver_assignments.items()
        )
        return human == solver

    def emit_attack(self, dec: AttackDecision) -> Optional[dict]:
        pool_n = len(dec.pool)
        chosen = [e for e in dec.pool if e[2] in set(dec.chosen_instance_ids)]
        self.pool_hist[min(pool_n, 8)] += 1
        self.chosen_hist[min(len(chosen), 8)] += 1
        if pool_n < self.args.min_pool:
            self.stats["drop_pool_too_small"] += 1
            return None
        if len(chosen) != len(set(dec.chosen_instance_ids)):
            # A declared attacker that is not in the qualified pool means the
            # reconstruction is wrong for this window; guessing is worse.
            self.stats["drop_chosen_not_in_pool"] += 1
            return None

        names = [e[0] for e in chosen]
        solved = solve_attack(self.facts, dec, self.stats)
        agrees: Optional[bool] = None
        if solved is not None:
            agrees = self._attack_agrees(dec, solved["ids"])
            self.solver_agree["attack_agree" if agrees else "attack_disagree"] += 1
        if self.args.solver_line == "on" and agrees is False:
            # "on" promises a corpus whose every record defers to a rendered
            # solver line; emitting the record without the line would break
            # that promise, and emitting it WITH the line trains
            # contradiction of the system prompt. Drop, counted.
            self.stats["drop_attack_solver_line_conflict"] += 1
            return None
        # When agreement is by copy-equivalence the solver's explanation may
        # name a different copy than the response does — canonicalize onto
        # the human's names before the line can render.
        explanation: Optional[str] = None
        if solved is not None:
            explanation = _canonical_attack_explanation(solved, names) if agrees else solved["explanation"]
        line = self._solver_line("attack", agrees, explanation)

        user, menu = render_attack_user(self.facts, dec, self.args.oracle_chars, line)
        rec = {
            "id": _rid("cd", dec.key),
            "system": build_system("attack", bool(dec.meta.get("reconstructed", True)), bool(line)),
            "user": user,
            "response": attack_response(names),
            "source": dec.source,
            "attribution": _attribution_for(dec.source),
            "kind": "combat_attack",
            "meta": {
                **dec.meta,
                "record_key": dec.key,
                "turn": dec.turn,
                "on_play": dec.on_play,
                "pool_size": pool_n,
                "chosen_size": len(chosen),
                "chosen_names": names,
                "menu_size": len(menu),
                "answer_shape": "declare_attackers/attacker_names",
                "is_all_in": len(chosen) == pool_n and pool_n > 0,
                "is_no_attack": not chosen,
                "is_proper_subset": 0 < len(chosen) < pool_n,
                "solver_ids": solved["ids"] if solved else None,
                "solver_agrees": agrees,
                "solver_line_rendered": bool(line),
            },
        }
        self.stats["emit_attack"] += 1
        if not chosen:
            self.answer_mix["attack_no_attack"] += 1
        elif len(chosen) == pool_n:
            self.answer_mix["attack_all_in"] += 1
        else:
            self.answer_mix["attack_proper_subset"] += 1
        self.by_source[f"{dec.source}:attack"] += 1
        return rec

    def emit_block(self, dec: BlockDecision) -> Optional[dict]:
        pool_n = len(dec.blockers)
        self.block_pool_hist[min(pool_n, 8)] += 1
        self.block_chosen_hist[min(len(dec.assignments), 8)] += 1
        if pool_n < self.args.min_pool:
            self.stats["drop_block_pool_too_small"] += 1
            return None
        if not dec.attackers:
            self.stats["drop_block_no_attackers"] += 1
            return None
        by_iid = {i: n for (n, _g, i) in dec.blockers}
        atk_by_iid = {i: n for (n, _g, i) in dec.attackers}
        if any(b not in by_iid or a not in atk_by_iid for b, a in dec.assignments.items()):
            self.stats["drop_block_assignment_off_board"] += 1
            return None

        solved = solve_block(self.facts, dec, self.stats)
        agrees: Optional[bool] = None
        if solved is not None:
            agrees = self._block_agrees(dec, solved["assignments"])
            self.solver_agree["block_agree" if agrees else "block_disagree"] += 1
        if self.args.solver_line == "on" and agrees is False:
            # Same contract as emit_attack: "on" must never emit a record
            # whose label contradicts a rendered (or promised) solver line.
            self.stats["drop_block_solver_line_conflict"] += 1
            return None
        # Same canonicalization as emit_attack: agreement-by-equivalence must
        # not render solver copy names the response contradicts.
        explanation: Optional[str] = None
        if solved is not None:
            explanation = _canonical_block_explanation(dec, solved) if agrees else solved["explanation"]
        line = self._solver_line("block", agrees, explanation)

        user, menu = render_block_user(self.facts, dec, self.args.oracle_chars, line)
        assignments = {by_iid[b]: atk_by_iid[a] for b, a in dec.assignments.items()}
        rec = {
            "id": _rid("cd", dec.key),
            "system": build_system("block", bool(dec.meta.get("reconstructed", True)), bool(line)),
            "user": user,
            "response": block_response(assignments),
            "source": dec.source,
            "attribution": _attribution_for(dec.source),
            "kind": "combat_block",
            "meta": {
                **dec.meta,
                "record_key": dec.key,
                "turn": dec.turn,
                "on_play": dec.on_play,
                "blocker_pool_size": pool_n,
                "n_attackers": len(dec.attackers),
                "n_blocks": len(dec.assignments),
                "menu_size": len(menu),
                "answer_shape": "declare_blockers/blocker_assignments",
                "is_no_block": not dec.assignments,
                "solver_assignments": solved["assignments"] if solved else None,
                "solver_agrees": agrees,
                "solver_line_rendered": bool(line),
            },
        }
        self.stats["emit_block"] += 1
        if not dec.assignments:
            self.answer_mix["block_none"] += 1
        elif len(dec.assignments) == pool_n:
            self.answer_mix["block_all"] += 1
        else:
            self.answer_mix["block_partial"] += 1
        self.by_source[f"{dec.source}:block"] += 1
        return rec


def _attribution_for(source: str) -> str:
    if source == SOURCE_17LANDS:
        return ATTRIBUTION_17LANDS
    if source == SOURCE_GRE_LOG:
        return ATTRIBUTION_MANASIGHT
    return ATTRIBUTION_OWNER


# ---------------------------------------------------------------------------
# Source 1: 17lands replay_data
# ---------------------------------------------------------------------------


def _tag(facts: CombatFacts, gids: list[int], base: int = 10_000) -> list[tuple[str, int, int]]:
    """Turn a grpId multiset into (display_name, grp_id, synthetic_instance_id).

    17lands has no instance ids, so copies get synthetic ones. Display names are
    disambiguated exactly as production does, so ``"Bear #2"`` in the menu is a
    name the autopilot's legality check would accept.

    ``base`` must differ per list within one decision — the solver keys
    assignment maps by instance id, so a pool creature and an opp creature
    sharing synthetic id 10_000 would silently collide.
    """
    names = disambiguate([facts.name(g) for g in gids])
    return [(n, g, base + i) for i, (n, g) in enumerate(zip(names, gids, strict=True))]


def _check_17lands_columns(rv, path: Path, cols: tuple[str, ...]) -> None:
    """Loud failure on 17lands schema drift.

    ``RowView.get`` returns "" for a missing column, so a renamed
    ``creatures_attacked``/``creatures_blocking`` column would silently
    mass-label every row "never attack"/"never block" — the 69%-land-drop
    incident with a quieter cause. One regex pass over the header per file.
    """
    for pat in cols:
        rx = re.compile(pat)
        if not any(rx.match(c) for c in rv.idx):
            raise SystemExit(
                f"{path.name}: no column matching {pat!r} — the 17lands replay_data "
                "schema changed; refusing to build a corpus of fabricated labels"
            )


def _turn_played(rv, row, prefix: str) -> bool:
    """Positive evidence the half-turn at ``prefix`` was actually played.

    Zero-fill-safe by construction: for turns after the game ended, 17lands
    writes "" to card-LIST columns and "0.0"/"0" to NUMERIC columns — on BOTH
    probed sets (2026-07-26, 300 games each of EOE and OTJ: 3,600/3,600
    post-game life reads were "0.0", never blank; every post-game list column
    was ""). So only a nonempty list column or a strictly positive numeric
    counts as play. The mandatory draw step makes the drawn-cards column
    near-universal on played turns; the eot hand/board lists catch the rest
    (0 interior turns across all 600 probed games were missed by this
    disjunction — no gaps).
    """
    for col in (
        "lands_played",
        "creatures_cast",
        "non_creatures_cast",
        "creatures_attacked",
        "cards_discarded",
        "eot_user_cards_in_hand",
        "eot_user_lands_in_play",
        "eot_oppo_lands_in_play",
    ):
        if rv.get(row, prefix + col):
            return True
    if prefix.startswith("user_"):
        # user turns carry a card-id LIST; oppo turns only a count.
        if rv.get(row, prefix + "cards_drawn"):
            return True
        spent = _num(rv.get(row, prefix + "user_mana_spent"))
    else:
        drawn = _num(rv.get(row, prefix + "cards_drawn_or_tutored"))
        if drawn is not None and drawn > 0:
            return True
        spent = _num(rv.get(row, prefix + "oppo_mana_spent"))
    return spent is not None and spent > 0


def iter_17lands_attacks(
    facts: CombatFacts, path: Path, args: argparse.Namespace, stats: Counter
) -> Iterable[AttackDecision]:
    set_code = path.name.split(".")[1] if "." in path.name else path.stem
    checked_cols = False
    for rv, row in iter_rows(path, args.min_rank, args.max_rows_per_file):
        if not checked_cols:
            _check_17lands_columns(
                rv,
                path,
                (
                    r"^user_turn_\d+_creatures_attacked$",
                    r"^user_turn_\d+_eot_user_life$",
                    # The no-attack evidence guard reads these; their silent
                    # disappearance would mass-drop every no-attack label.
                    r"^num_turns$",
                    # record_key uniqueness depends on match_number: without
                    # it every match of a draft run collapses to one id and
                    # 46% of records become unaddressable (fixed 2026-07-26).
                    r"^match_number$",
                    r"^oppo_turn_\d+_cards_drawn_or_tutored$",
                ),
            )
            checked_cols = True
        stats["attack_rows"] += 1
        on_play = rv.get(row, "on_play") in ("True", "true", "1")
        draft_id = rv.get(row, "draft_id") or rv.get(row, "game_id") or ""
        # ``game_number`` alone does NOT identify a game: 17lands publishes one
        # row per MATCH here, so it reads "1" on every row (120,000/120,000
        # probed on EOE). Keying on it collapsed every match of a draft run into
        # one key — 44,463 of 96,131 records shared an id, and any harness that
        # does ``{rec["id"]: rec}`` (tools/eval/run.py,
        # gate_play_decisions.evaluate) silently scored one and dropped the
        # rest. ``match_number`` is the real discriminator:
        # (draft_id, match_number, game_number) was unique in 120,000/120,000.
        # Field ORDER is unchanged so downstream ``split("|")`` parsers still
        # work — only this field's content is now "<match>.<game>".
        game_no = f"{rv.get(row, 'match_number') or '0'}.{rv.get(row, 'game_number') or '1'}"
        max_turn = min(rv.max_user_turn(), args.max_turn)
        for turn in range(1, max_turn + 1):
            cur = f"user_turn_{turn}_"
            attacked = rv.ids(row, cur + "creatures_attacked")
            # ``attacked`` belongs in the turn-existence disjunction: a
            # hellbent turn that only swings (empty eot hand, no land, no
            # casts) is a real turn — the previous gate silently deleted
            # exactly the late-game alpha strikes this corpus exists to add.
            if not (
                rv.ids(row, cur + "lands_played")
                or rv.ids(row, cur + "creatures_cast")
                or rv.ids(row, cur + "non_creatures_cast")
                or rv.get(row, cur + "eot_user_cards_in_hand")
                or attacked
            ):
                continue
            pre = predecessor_prefix(turn, on_play, "oppo")
            start_creatures = rv.ids(row, pre + "eot_user_creatures_in_play") if pre else []
            start_nondef = [g for g in start_creatures if not facts.has(g, "defender")]
            defenders = [g for g in start_creatures if facts.has(g, "defender")]
            fresh = rv.ids(row, cur + "creatures_cast")
            ac = Counter(attacked)
            # A creature cast this turn can only attack with haste — but
            # 17lands has no within-turn ordering, so a haste creature cast
            # AFTER combat was never an option. A hasty cast is provably
            # precombat only when the attack count for its grpId exceeds
            # what the start-of-turn board could supply; any other hasty
            # cast is ambiguous and the window is dropped, never guessed
            # into a menu the player may never have seen.
            start_count = Counter(start_nondef)
            hasty_pool: list[int] = []
            hasty_ambiguous = False
            for g, n in Counter(
                g for g in fresh if facts.has(g, "haste") and not facts.has(g, "defender")
            ).items():
                provable = min(n, max(0, ac[g] - start_count[g]))
                if provable < n:
                    hasty_ambiguous = True
                hasty_pool.extend([g] * provable)
            pool_gids = start_nondef + hasty_pool
            if not pool_gids and not attacked:
                continue
            stats["attack_windows"] += 1
            if hasty_ambiguous:
                stats["attack_drop_hasty_ambiguous"] += 1
                continue
            if not attacked:
                # A no-attack label needs positive evidence the turn actually
                # reached combat. Row-existence is not it: a game that ended
                # during this turn (lethal burn in main 1, a concession)
                # would fabricate a "declined to attack" decision the player
                # never faced. The old test — successor-turn eot life parsing
                # as non-numeric — was VACUOUS: 17lands zero-fills numeric
                # columns past the game's end with "0.0", never blank, on
                # BOTH probed sets (2026-07-26, 300 games each of EOE and
                # OTJ: 3,600/3,600 post-game life reads were "0.0"), and a
                # real death at exactly 0 life would be indistinguishable
                # from the fill even if blanks existed. The trustworthy
                # signal is game-level ``num_turns``: it equals the final
                # turn-PAIR index — max(last played user turn, last played
                # oppo turn) — in 300/300 probed games on each set. Pair
                # order by parity: on the play the user's half of pair N
                # precedes the opponent's; on the draw it follows.
                #   * turn < num_turns: a later pair was played, so this
                #     turn's combat came and went — keep.
                #   * turn == num_turns on the play: the game ended inside
                #     this pair — during our half (combat possibly never
                #     reached: the fabrication this guard drops) or during
                #     the opponent's (ours completed). The successor
                #     half-turn's own activity separates the two.
                #   * turn == num_turns on the draw: our half is the pair's
                #     final one, so the game ended mid-user-turn — drop.
                #     Real final-turn no-attacks are dropped too — counted,
                #     and indistinguishable from the fabrications by
                #     construction.
                # The successor of user turn N is oppo turn N on the play and
                # oppo turn N+1 on the draw — the mirror of
                # predecessor_prefix's verified ordering; its activity test
                # (_turn_played) is zero-fill-safe and doubles as the
                # fallback when num_turns fails to parse (0/600 probed).
                nt = _num(rv.get(row, "num_turns"))
                succ = f"oppo_turn_{turn}_" if on_play else f"oppo_turn_{turn + 1}_"
                if not ((nt is not None and turn < int(nt)) or _turn_played(rv, row, succ)):
                    stats["attack_drop_no_combat_evidence"] += 1
                    continue
            pc = Counter(pool_gids)
            if any(ac[g] > pc[g] for g in ac):
                # The recorded attacker is not in the reconstructed pool: the
                # position is wrong (a haste/flash creature we could not detect,
                # a token, a reanimation). This is the entire poison term and it
                # is decidable at build time — drop, never guess.
                stats["poison_attacker_not_in_pool"] += 1
                stats[f"attack_poison_turn_{turn}"] += 1
                continue
            stats["attack_clean"] += 1
            pool = _tag(facts, pool_gids)
            chosen_ids = _positional_match(pool, attacked)
            # Fresh casts that are not provably-precombat haste cannot attack
            # but ARE battlefield context by the crackback turn — summoning
            # sickness does not stop blocking. (Cast timing is ambiguous for
            # them; they exist by the opponent's turn either way.)
            sick = list((Counter(fresh) - Counter(hasty_pool)).elements())
            remaining = _tag(facts, defenders + sick, base=20_000)
            opp_creature_gids = rv.ids(row, pre + "eot_oppo_creatures_in_play") if pre else []
            # 17lands has no tapped info for the opponent either, so every
            # opp creature doubles as blocker and next-turn attacker — the
            # printed-P/T fallback of the same class, counted via
            # solver_pt_printed_fallback.
            opp_creatures = _tag(facts, opp_creature_gids, base=30_000)
            yield AttackDecision(
                source=SOURCE_17LANDS,
                key=f"{draft_id}|{game_no}|{set_code}|t{turn}|atk",
                # Prompts use MTGA's GLOBAL turn counter (the GRE sources and
                # production both do); a per-player "T6" showing 11 lands
                # taught the model two different games under one turn number.
                turn=(2 * turn - 1) if on_play else 2 * turn,
                on_play=on_play,
                pool=pool,
                chosen_instance_ids=chosen_ids,
                life=_num(rv.get(row, pre + "eot_user_life")) if pre else None,
                opp_life=_num(rv.get(row, pre + "eot_oppo_life")) if pre else None,
                opp_hand=_num(rv.get(row, pre + "eot_oppo_cards_in_hand")) if pre else None,
                own_board=pool_gids + (rv.ids(row, pre + "eot_user_non_creatures_in_play") if pre else []),
                # The land played THIS turn is on the battlefield at declare
                # attackers; the prior-eot list understated mana by one on
                # nearly every attack record.
                own_lands=(rv.ids(row, pre + "eot_user_lands_in_play") if pre else [])
                + rv.ids(row, cur + "lands_played"),
                opp_board=opp_creature_gids
                + (rv.ids(row, pre + "eot_oppo_non_creatures_in_play") if pre else [])
                + (rv.ids(row, pre + "eot_oppo_lands_in_play") if pre else []),
                opp_creatures=opp_creatures,
                opp_blockers=None,
                remaining_blockers=remaining,
                own_board_extra=[(g, "[cannot attack: defender]") for g in defenders]
                + [(g, "[cannot attack: summoning sick]") for g in sick],
                meta={
                    "expansion": set_code,
                    "rank": rv.get(row, "rank"),
                    "won": rv.get(row, "won"),
                    "reconstructed": True,
                    "has_pairing": None,
                    "user_turn": turn,
                },
            )


def iter_17lands_blocks(
    facts: CombatFacts, path: Path, args: argparse.Namespace, stats: Counter
) -> Iterable[BlockDecision]:
    """Blocks during the OPPONENT's turn.

    ``oppo_turn_N_creatures_blocking`` is the USER's blocker set (the columns are
    relative to the turn's active player), and ``oppo_turn_N_creatures_attacked``
    is what they were blocking.

    Pairing is NOT recorded. With exactly one attacker every block is forced onto
    it and a complete ``blocker_assignments`` is recoverable; with two or more
    attackers the mapping is unknowable from this source and the WHOLE window is
    dropped — blocked and unblocked alike. Dropping only the blocked half (the
    old ``forced-only``) made P(block | 2+ attackers) = 0 in the emitted slice;
    the old ``drop`` fell through to a fabricated everyone→attackers[0] mapping.
    """
    set_code = path.name.split(".")[1] if "." in path.name else path.stem
    checked_cols = False
    for rv, row in iter_rows(path, args.min_rank, args.max_rows_per_file):
        if not checked_cols:
            _check_17lands_columns(
                rv,
                path,
                (
                    r"^oppo_turn_\d+_creatures_attacked$",
                    r"^oppo_turn_\d+_creatures_blocking$",
                    r"^oppo_turn_\d+_eot_user_life$",
                    # The no-block evidence guard reads these; their silent
                    # disappearance would mass-drop every no-block label.
                    r"^num_turns$",
                    # record_key uniqueness depends on match_number: without
                    # it every match of a draft run collapses to one id and
                    # 46% of records become unaddressable (fixed 2026-07-26).
                    r"^match_number$",
                    r"^oppo_turn_\d+_user_combat_damage_taken$",
                    r"^user_turn_\d+_cards_drawn$",
                ),
            )
            checked_cols = True
        stats["block_rows"] += 1
        on_play = rv.get(row, "on_play") in ("True", "true", "1")
        draft_id = rv.get(row, "draft_id") or rv.get(row, "game_id") or ""
        # ``game_number`` alone does NOT identify a game: 17lands publishes one
        # row per MATCH here, so it reads "1" on every row (120,000/120,000
        # probed on EOE). Keying on it collapsed every match of a draft run into
        # one key — 44,463 of 96,131 records shared an id, and any harness that
        # does ``{rec["id"]: rec}`` (tools/eval/run.py,
        # gate_play_decisions.evaluate) silently scored one and dropped the
        # rest. ``match_number`` is the real discriminator:
        # (draft_id, match_number, game_number) was unique in 120,000/120,000.
        # Field ORDER is unchanged so downstream ``split("|")`` parsers still
        # work — only this field's content is now "<match>.<game>".
        game_no = f"{rv.get(row, 'match_number') or '0'}.{rv.get(row, 'game_number') or '1'}"
        max_turn = min(rv.max_user_turn(), args.max_turn)
        for turn in range(1, max_turn + 1):
            cur = f"oppo_turn_{turn}_"
            attackers_gids = rv.ids(row, cur + "creatures_attacked")
            if not attackers_gids:
                continue
            stats["block_windows"] += 1
            # The turn immediately before oppo turn N is user turn N (on the
            # play) or user turn N-1 (on the draw) — the mirror of
            # predecessor_prefix's verified ordering.
            user_turn = turn if on_play else turn - 1
            if user_turn < 1:
                stats["block_drop_no_predecessor"] += 1
                continue
            pre = f"user_turn_{user_turn}_"
            own_creatures = rv.ids(row, pre + "eot_user_creatures_in_play")
            # Creatures that attacked on our preceding turn are still tapped
            # during the opponent's turn unless they have vigilance, so they
            # cannot block. 17lands records no tapped state; this is the one
            # place it is derivable — but ONLY when no copy of that grpId
            # left the battlefield during that turn: eot_user_creatures_in_play
            # excludes attackers that DIED, while creatures_attacked still
            # counts them, so a bare per-grpId subtraction let a dead twin
            # consume the untapped status of a copy that stayed home. The
            # multiset check below proves survival (eot count == start count
            # + casts); anything else is ambiguous — drop, never guess.
            attacked_prev = [
                g for g in rv.ids(row, pre + "creatures_attacked") if not facts.has(g, "vigilance")
            ]
            prev_pre = predecessor_prefix(user_turn, on_play, "oppo")
            start_prev = rv.ids(row, prev_pre + "eot_user_creatures_in_play") if prev_pre else []
            cast_prev = rv.ids(row, pre + "creatures_cast")
            sc = Counter(start_prev) + Counter(cast_prev)
            ec, tc = Counter(own_creatures), Counter(attacked_prev)
            tapped_ambiguous = False
            for g, a_n in tc.items():
                if ec[g] == 0:
                    continue  # no surviving copies -> nothing to mark tapped
                if prev_pre is None or ec[g] != sc[g] or a_n > ec[g]:
                    tapped_ambiguous = True
                    break
            if tapped_ambiguous:
                stats["block_drop_tapped_ambiguous"] += 1
                continue
            tapped = Counter({g: n for g, n in tc.items() if ec[g] > 0})
            pool_gids: list[int] = []
            tapped_board: list[int] = []
            for g in own_creatures:
                if tapped[g] > 0:
                    tapped[g] -= 1
                    tapped_board.append(g)
                    continue
                pool_gids.append(g)
            blocked_with = rv.ids(row, cur + "creatures_blocking")
            if not pool_gids and not blocked_with:
                # Counted so the audit funnel reconciles: windows must equal
                # clean + poison + drops, or the sidecar lies.
                stats["block_skip_no_pool_no_blocks"] += 1
                continue
            if not blocked_with:
                # Same evidence rule as the attack side — and the same
                # vacuousness fix (see iter_17lands_attacks: successor life
                # columns are "0.0"-filled past the game's end on both
                # probed sets, so the old non-numeric test never fired). A
                # no-block label needs proof the game continued past this
                # combat, or that combat damage actually resolved. Oppo turn
                # N is the pair's SECOND half on the play and FIRST half on
                # the draw; its successor is user turn N+1 / user turn N
                # respectively.
                #   * turn < num_turns: a later pair was played — keep.
                #   * successor half-turn played: on the draw that is user
                #     turn N (same pair) — the real final-pair refinement;
                #     on the play it is user turn N+1, redundant with the
                #     num_turns test but robust to a failed parse.
                #   * user_combat_damage_taken > 0 THIS oppo turn: the
                #     combat damage step follows declare-blockers, so
                #     recorded damage proves the empty declaration really
                #     resolved — "declined to block and took it" is a real
                #     decision, not a fabrication (94.7% of EOE and 95.5%
                #     of OTJ probed attacked-and-unblocked turns show
                #     damage > 0; unplayed turns are "0.0"-filled). A
                #     concession AT the declare-blockers window shows 0.
                # What still drops: final-pair concessions at the window,
                # and the ~5% of real final-turn no-blocks where nothing
                # connected (fog effects, 0-power attacks) — conservative,
                # counted.
                nt = _num(rv.get(row, "num_turns"))
                succ = f"user_turn_{turn + 1}_" if on_play else f"user_turn_{turn}_"
                dmg = _num(rv.get(row, cur + "user_combat_damage_taken"))
                if not (
                    (nt is not None and turn < int(nt))
                    or _turn_played(rv, row, succ)
                    or (dmg is not None and dmg > 0)
                ):
                    stats["block_drop_no_combat_evidence"] += 1
                    continue
            pc, bc = Counter(pool_gids), Counter(blocked_with)
            if any(bc[g] > pc[g] for g in bc):
                stats["poison_blocker_not_in_pool"] += 1
                stats[f"block_poison_turn_{turn}"] += 1
                continue
            if len(attackers_gids) > 1:
                # Pairing is unknowable with 2+ attackers under EITHER flag
                # value: "forced-only" used to keep the no-block half (teaching
                # P(block | 2+ attackers) = 0) and "drop" used to fabricate
                # everyone→attackers[0]. Both now drop the window whole.
                stats["drop_pairing_unknowable"] += 1
                continue
            stats["block_clean"] += 1
            pool = _tag(facts, pool_gids)
            attackers = _tag(facts, attackers_gids, base=40_000)
            chosen_ids = _positional_match(pool, blocked_with)
            # Only reachable with exactly ONE attacker, so the mapping really
            # is forced — never a guess.
            assignments = {b: attackers[0][2] for b in chosen_ids} if attackers else {}
            allowed = _keyword_allowed(facts, pool, attackers)
            yield BlockDecision(
                source=SOURCE_17LANDS,
                key=f"{draft_id}|{game_no}|{set_code}|ot{turn}|blk",
                # Global turn counter, mirroring the GRE sources/production.
                turn=2 * turn if on_play else 2 * turn - 1,
                on_play=on_play,
                blockers=pool,
                attackers=attackers,
                assignments=assignments,
                allowed=allowed,
                life=_num(rv.get(row, pre + "eot_user_life")),
                opp_life=_num(rv.get(row, pre + "eot_oppo_life")),
                opp_hand=_num(rv.get(row, pre + "eot_oppo_cards_in_hand")),
                own_board=pool_gids + rv.ids(row, pre + "eot_user_non_creatures_in_play"),
                own_lands=rv.ids(row, pre + "eot_user_lands_in_play"),
                opp_board=attackers_gids
                + rv.ids(row, pre + "eot_oppo_non_creatures_in_play")
                + rv.ids(row, pre + "eot_oppo_lands_in_play"),
                # Tapped creatures cannot block but they ARE on the board the
                # human saw — hiding them made chosen no-blocks look forced.
                own_board_extra=[(g, "[tapped]") for g in tapped_board],
                meta={
                    "expansion": set_code,
                    "rank": rv.get(row, "rank"),
                    "won": rv.get(row, "won"),
                    "reconstructed": True,
                    "has_pairing": True,
                    "pairing_source": "forced_single_attacker",
                    "oppo_turn": turn,
                },
            )


def _positional_match(pool: list[tuple[str, int, int]], gids: list[int]) -> list[int]:
    """Map a grpId multiset onto pool entries, consuming copies left to right."""
    remaining = Counter(gids)
    out: list[int] = []
    for _name, gid, iid in pool:
        if remaining[gid] > 0:
            remaining[gid] -= 1
            out.append(iid)
    return out


def _keyword_allowed(
    facts: CombatFacts, blockers: list[tuple[str, int, int]], attackers: list[tuple[str, int, int]]
) -> dict[int, set[int]]:
    """Keyword-level evasion filter (flying/reach) for reconstructed positions.

    The GRE sources carry the authoritative ``attackerInstanceIds`` list; this
    only stands in for it when the source cannot supply one.
    """
    out: dict[int, set[int]] = {}
    for _bn, bg, bi in blockers:
        ok: set[int] = set()
        for _an, ag, ai in attackers:
            if facts.has(ag, "flying") and not (facts.has(bg, "flying") or facts.has(bg, "reach")):
                continue
            ok.add(ai)
        out[bi] = ok
    return out


# ---------------------------------------------------------------------------
# Source 2 & 3: GRE message streams (manasight Player.log, owner .rply)
# ---------------------------------------------------------------------------


def _episode_bounds(msgs: list, start: int, msg_type: str, seat: int) -> tuple[list[int], int]:
    """Indices of every consecutive request of ``msg_type`` addressed to ``seat``.

    MTGA re-issues DeclareAttackersReq/DeclareBlockersReq after each toggle, so
    one combat is several requests. The episode ends at the next IN request of a
    different type.
    """
    ep: list[int] = []
    j = start
    while j < len(msgs):
        m = msgs[j]
        if m.direction == "IN" and m.is_request and m.msg_type == msg_type:
            addressed = [int(x) for x in (m.payload.get("systemSeatIds") or [])]
            # A request with NO seat ids cannot be attributed; assuming
            # "ours" is how opponent-perspective poisoning starts. It ends
            # the episode and the main loop counts it when reached.
            if seat not in addressed:
                break
            ep.append(j)
            j += 1
            continue
        if m.direction == "IN" and m.is_request:
            break
        j += 1
    return ep, j


def iter_gre_combat(
    facts: CombatFacts,
    messages: list,
    seat: int,
    source: str,
    file_key: str,
    args: argparse.Namespace,
    stats: Counter,
) -> Iterable[Any]:
    """Walk one match's GRE stream and yield AttackDecision / BlockDecision."""
    from tools.eval.replay.menu_groundtruth import _iter_state_messages
    from tools.eval.replay.state import GameStateSnapshot, _apply_state_message
    from tools.training.ingest_manasight import pair_requests_ordered

    pairs = pair_requests_ordered(messages)
    snap = GameStateSnapshot(local_seat_id=seat)
    opp = 1 if seat == 2 else 2
    i = 0
    while i < len(messages):
        m = messages[i]
        if m.direction == "IN":
            for gsm in _iter_state_messages(m):
                _apply_state_message(snap, gsm)
        if not (m.direction == "IN" and m.is_request):
            i += 1
            continue
        addressed = [int(x) for x in (m.payload.get("systemSeatIds") or [])]
        if seat not in addressed:
            # Empty systemSeatIds used to be treated as "addressed to us" —
            # the one remaining place the acting player was assumed instead
            # of resolved. Count it so the assumption fails loud.
            if not addressed and m.msg_type in (
                "GREMessageType_DeclareAttackersReq",
                "GREMessageType_DeclareBlockersReq",
            ):
                stats["gre_seat_unaddressed_request"] += 1
            i += 1
            continue

        if m.msg_type == "GREMessageType_DeclareAttackersReq":
            ep, nxt = _episode_bounds(messages, i, m.msg_type, seat)
            dec = _gre_attack(facts, messages, ep, pairs, snap, seat, opp, source, file_key, stats)
            # Advance the snapshot over the episode we just consumed.
            for k in range(i, nxt):
                if messages[k].direction == "IN":
                    for gsm in _iter_state_messages(messages[k]):
                        _apply_state_message(snap, gsm)
            i = nxt
            if dec is not None:
                yield dec
            continue

        if m.msg_type == "GREMessageType_DeclareBlockersReq":
            ep, nxt = _episode_bounds(messages, i, m.msg_type, seat)
            dec = _gre_block(facts, messages, ep, pairs, snap, seat, opp, source, file_key, stats)
            for k in range(i, nxt):
                if messages[k].direction == "IN":
                    for gsm in _iter_state_messages(messages[k]):
                        _apply_state_message(snap, gsm)
            i = nxt
            if dec is not None:
                yield dec
            continue

        i += 1


def _obj_name(snap, iid: Optional[int]) -> tuple[Optional[int], str]:
    from tools.eval.replay.menu_groundtruth import card_facts

    if iid is None:
        return None, "?"
    obj = snap.game_objects.get(int(iid)) or {}
    grp = obj.get("grpId")
    return (int(grp) if grp is not None else None), (card_facts(grp).get("name") or f"#{iid}")


def _live_fields(obj: dict) -> tuple[Optional[int], Optional[int], bool]:
    """(power, toughness, is_tapped) from a raw GRE game object.

    These are the CURRENT values — counters, auras and equipment folded in —
    which the printed Scryfall lookup can never see. GRE JSON serializes
    power/toughness either bare or as ``{"value": N}`` (production
    gamestate.py handles both), and ``isTapped`` is omitted when false.
    """

    def val(x: Any) -> Optional[int]:
        if isinstance(x, dict):
            x = x.get("value")
        try:
            return int(x)
        except (TypeError, ValueError):
            return None

    return val(obj.get("power")), val(obj.get("toughness")), bool(obj.get("isTapped"))


def _live_creature_objs(snap, seat: int) -> list[tuple[int, int, Optional[int], Optional[int], bool]]:
    """(instance_id, grp_id, power, toughness, tapped) per creature on
    ``seat``'s battlefield, from the raw snapshot objects."""
    from tools.eval.replay.menu_groundtruth import card_facts

    out: list[tuple[int, int, Optional[int], Optional[int], bool]] = []
    for obj in snap.battlefield(seat):
        iid, grp = obj.get("instanceId"), obj.get("grpId")
        if iid is None or grp is None:
            continue
        tl = (card_facts(grp).get("type_line") or "").lower()
        if "creature" not in tl:
            continue
        p, t, tapped = _live_fields(obj)
        out.append((int(iid), int(grp), p, t, tapped))
    return out


def _board_gids(snap, seat: int) -> tuple[list[int], list[int], list[int]]:
    """(creature grpIds, land grpIds, other grpIds) on ``seat``'s battlefield."""
    from tools.eval.replay.menu_groundtruth import card_facts

    creatures: list[int] = []
    lands: list[int] = []
    other: list[int] = []
    for obj in snap.battlefield(seat):
        grp = obj.get("grpId")
        if grp is None:
            continue
        tl = (card_facts(grp).get("type_line") or "").lower()
        if "creature" in tl:
            creatures.append(int(grp))
        elif "land" in tl:
            lands.append(int(grp))
        else:
            other.append(int(grp))
    return creatures, lands, other


def _gre_attack(
    facts, messages, ep, pairs, snap, seat, opp, source, file_key, stats
) -> Optional[AttackDecision]:
    stats["gre_attack_episodes"] += 1
    first = messages[ep[0]].payload.get("declareAttackersReq") or {}
    qualified = first.get("qualifiedAttackers") or first.get("attackers") or []
    pool_entries: list[tuple[str, int, int]] = []
    raw_names: list[str] = []
    raw: list[tuple[int, int]] = []
    for a in qualified:
        iid = a.get("attackerInstanceId")
        if iid is None:
            continue
        grp, name = _obj_name(snap, iid)
        if grp is None:
            continue
        raw_names.append(name)
        raw.append((int(grp), int(iid)))
    for disp, (grp, iid) in zip(disambiguate(raw_names), raw, strict=True):
        pool_entries.append((disp, grp, iid))
    if not pool_entries:
        stats["gre_attack_no_pool"] += 1
        return None

    # --- Label: the LAST re-issued request's DECLARED state, then the final
    # toggle/commit paired with that request. Every toggle — select AND
    # deselect — arrives as a declareAttackersResp.selectedAttackers entry
    # naming the creature (the deselect clears selectedDamageRecipient, per
    # DeclareAttackersWorkflow.UndeclareAttacker), so the old UNION kept every
    # creature the player clicked on and then clicked off — a declaration
    # that never happened. The client's own DeclaredAttackers is exactly
    # "attackers with a non-null recipient" (DeclareAttackerRequest.cs).
    last_req = messages[ep[-1]].payload.get("declareAttackersReq") or {}
    chosen: set[int] = set()
    for a in last_req.get("attackers") or []:
        iid = a.get("attackerInstanceId")
        if iid is not None and a.get("selectedDamageRecipient"):
            chosen.add(int(iid))
    auto_any = False
    auto_final = False
    answered = False
    for j in ep:
        resp = pairs.get(j)
        if resp is None:
            continue
        if resp.msg_type == "ClientMessageType_SubmitAttackersReq":
            answered = True
            continue
        body = resp.payload.get("declareAttackersResp") or {}
        if body.get("autoDeclare"):
            auto_any = True
        if body.get("selectedAttackers") or body.get("autoDeclare"):
            answered = True
        if j != ep[-1]:
            # This toggle's effect is already reflected in the NEXT re-issued
            # request's attackers[] state; replaying it here would re-apply
            # stale selections over later deselects.
            continue
        if body.get("autoDeclare"):
            # autoDeclare == "attack with every qualified creature". As the
            # FINAL act nothing after it can deselect; an auto-then-deselect
            # sequence instead shows up in the re-issued request state above.
            chosen = {iid for (_n, _g, iid) in pool_entries}
            auto_final = True
        for a in body.get("selectedAttackers") or []:
            iid = a.get("attackerInstanceId")
            if iid is None:
                continue
            # Recipient present == selected; recipient absent == deselect.
            if a.get("selectedDamageRecipient"):
                chosen.add(int(iid))
            else:
                chosen.discard(int(iid))
    if auto_final:
        stats["gre_attack_auto_declare"] += 1
    if not chosen and not answered:
        stats["gre_attack_no_answer"] += 1
        return None

    # Live per-instance state (counters/auras folded into power/toughness,
    # real tapped flags) for the solver, agreement keys and the prompt.
    live: dict[int, tuple[Optional[int], Optional[int], bool]] = {}
    own_objs = _live_creature_objs(snap, seat)
    opp_objs = _live_creature_objs(snap, opp)
    for iid, _g, p, t, tapped in own_objs + opp_objs:
        live[iid] = (p, t, tapped)

    # Production parity (coach.py): your_remaining_blockers = the UNTAPPED
    # creatures NOT in the candidate pool; opp_blockers = the opponent's
    # UNTAPPED creatures; every opp creature untaps to attack next turn.
    pool_iids = {iid for (_n, _g, iid) in pool_entries}
    rem_raw = [(g, i) for (i, g, _p, _t, tapped) in own_objs if i not in pool_iids and not tapped]
    rem_names = disambiguate([_obj_name(snap, i)[1] for (_g, i) in rem_raw])
    remaining = [(n, g, i) for n, (g, i) in zip(rem_names, rem_raw, strict=True)]
    opp_names = disambiguate([_obj_name(snap, i)[1] for (i, _g, _p, _t, _tp) in opp_objs])
    opp_creature_entries = [(n, g, i) for n, (i, g, _p, _t, _tp) in zip(opp_names, opp_objs, strict=True)]
    opp_blockers = [
        e for e, (_i, _g, _p, _t, tapped) in zip(opp_creature_entries, opp_objs, strict=True) if not tapped
    ]

    _own_creatures, own_lands, own_other = _board_gids(snap, seat)
    _opp_creatures, opp_lands, opp_other = _board_gids(snap, opp)
    return AttackDecision(
        source=source,
        key=f"{file_key}|msg{messages[ep[0]].msg_id}|atk",
        turn=int(snap.turn_number or 0),
        on_play=None,
        pool=pool_entries,
        chosen_instance_ids=sorted(chosen),
        life=snap.life(seat),
        opp_life=snap.life(opp),
        opp_hand=snap.hand_size(opp),
        own_board=own_other,
        own_lands=own_lands,
        opp_board=opp_other + opp_lands,
        opp_creatures=opp_creature_entries,
        opp_blockers=opp_blockers,
        remaining_blockers=remaining,
        live=live,
        # Creatures render from live state; non-creatures via _board_lines.
        own_board_live=[(g, p, t, tapped) for (_i, g, p, t, tapped) in own_objs],
        opp_board_live=[(g, p, t, tapped) for (_i, g, p, t, tapped) in opp_objs],
        meta={
            "reconstructed": False,
            "requests_in_episode": len(ep),
            "auto_declare": auto_any,
            "has_pairing": None,
            "label_derivation": "last_request_state+final_delta",
        },
    )


def _gre_block(
    facts, messages, ep, pairs, snap, seat, opp, source, file_key, stats
) -> Optional[BlockDecision]:
    stats["gre_block_episodes"] += 1
    first = messages[ep[0]].payload.get("declareBlockersReq") or {}
    cands = first.get("blockers") or []
    raw: list[tuple[int, int]] = []
    raw_names: list[str] = []
    allowed: dict[int, set[int]] = {}
    legal_target_ids: list[int] = []
    for b in cands:
        bid = b.get("blockerInstanceId")
        if bid is None:
            continue
        grp, name = _obj_name(snap, bid)
        if grp is None:
            continue
        raw.append((int(grp), int(bid)))
        raw_names.append(name)
        ids = {int(x) for x in (b.get("attackerInstanceIds") or [])}
        allowed[int(bid)] = ids
        for x in ids:
            if x not in legal_target_ids:
                legal_target_ids.append(x)
    blockers = [(d, g, i) for d, (g, i) in zip(disambiguate(raw_names), raw, strict=True)]
    if not blockers:
        stats["gre_block_no_pool"] += 1
        return None

    # --- Attacker roster: snapshot gameObjects.attackState, NOT the union of
    # per-blocker attackerInstanceIds. That field is each blocker's LEGAL
    # targets, so an attacker no creature may block (flier over a ground
    # board — the most common evasion case in Limited) vanished from the
    # prompt, the solver and the damage math entirely. attackState is the
    # same enum production trusts (gamestate.py issue #420).
    attacker_ids: list[int] = []
    for obj in snap.battlefield(opp):
        iid = obj.get("instanceId")
        if iid is not None and _parse_attack_state(obj.get("attackState")):
            attacker_ids.append(int(iid))
    if not attacker_ids:
        # The snapshot lacks combat state, so the true attack is unknowable
        # (the legal-target union is provably incomplete for evasive
        # attackers) — drop, never guess.
        stats["gre_block_no_attackstate_roster"] += 1
        return None
    for x in legal_target_ids:
        if x not in attacker_ids:
            # A legal-target id IS an attacker by proto semantics; its
            # absence from the snapshot roster means snapshot drift. Keeping
            # it is data, not a guess — but count the drift.
            attacker_ids.append(x)
            stats["gre_block_roster_extra_from_legal"] += 1

    atk_raw: list[tuple[int, int]] = []
    atk_names: list[str] = []
    for aid in attacker_ids:
        grp, name = _obj_name(snap, aid)
        if grp is None:
            # A roster attacker we cannot resolve to a card would silently
            # shrink the attack — the exact understated-combat poisoning the
            # roster exists to prevent. Drop the window instead.
            stats["gre_block_attacker_unresolvable"] += 1
            return None
        atk_raw.append((int(grp), int(aid)))
        atk_names.append(name)
    attackers = [(d, g, i) for d, (g, i) in zip(disambiguate(atk_names), atk_raw, strict=True)]
    if not attackers:
        stats["gre_block_no_pool"] += 1
        return None

    assignments: dict[int, int] = {}
    answered = False
    for j in ep:
        resp = pairs.get(j)
        if resp is None:
            continue
        if resp.msg_type == "ClientMessageType_SubmitBlockersReq":
            answered = True
            continue
        body = resp.payload.get("declareBlockersResp") or {}
        sbs = body.get("selectedBlockers") or []
        if sbs:
            answered = True
        for sb in sbs:
            bid = sb.get("blockerInstanceId")
            if bid is None:
                continue
            sel = sb.get("selectedAttackerInstanceIds") or []
            if sel:
                # maxAttackers is 1 in every observed row; a multi-block
                # blocker would need the schema to carry a list, which
                # production's blocker_assignments cannot express.
                assignments[int(bid)] = int(sel[0])
            else:
                # Retraction: the un-toggle path clears the blocker's
                # SelectedAttackerInstanceIds and re-sends it. Skipping the
                # empty entry (the old ``and sel`` guard) kept the stale
                # pairing and labeled a block the player took back —
                # final state wins.
                assignments.pop(int(bid), None)
    if not assignments and not answered:
        stats["gre_block_no_answer"] += 1
        return None

    live: dict[int, tuple[Optional[int], Optional[int], bool]] = {}
    own_objs = _live_creature_objs(snap, seat)
    opp_objs = _live_creature_objs(snap, opp)
    for iid, _g, p, t, tapped in own_objs + opp_objs:
        live[iid] = (p, t, tapped)

    _own_creatures, own_lands, own_other = _board_gids(snap, seat)
    _opp_creatures, opp_lands, opp_other = _board_gids(snap, opp)
    return BlockDecision(
        source=source,
        key=f"{file_key}|msg{messages[ep[0]].msg_id}|blk",
        turn=int(snap.turn_number or 0),
        on_play=None,
        blockers=blockers,
        attackers=attackers,
        assignments=assignments,
        allowed=allowed,
        life=snap.life(seat),
        opp_life=snap.life(opp),
        opp_hand=snap.hand_size(opp),
        own_board=own_other,
        own_lands=own_lands,
        opp_board=opp_other + opp_lands,
        live=live,
        own_board_live=[(g, p, t, tapped) for (_i, g, p, t, tapped) in own_objs],
        opp_board_live=[(g, p, t, tapped) for (_i, g, p, t, tapped) in opp_objs],
        meta={
            "reconstructed": False,
            "requests_in_episode": len(ep),
            "has_pairing": True,
            "pairing_source": "declareBlockersResp.selectedAttackerInstanceIds",
            "attacker_roster_source": "gameObjects.attackState",
        },
    )


def iter_gre_logs(facts: CombatFacts, root: Path, args, stats: Counter) -> Iterable[Any]:
    from tools.training.ingest_manasight import detect_local_seat_player_log, parse_player_log

    files = sorted(root.glob("*.log.gz")) + sorted(root.glob("*.log"))
    stats["gre_log_files"] = len(files)
    for fp in files:
        try:
            matches, _ranks = parse_player_log(fp)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not parse %s: %s", fp.name, exc)
            stats["gre_log_parse_errors"] += 1
            continue
        for mi, lm in enumerate(matches):
            seat, _method = detect_local_seat_player_log(lm.messages)
            if seat is None:
                stats["gre_seat_undetermined"] += 1
                continue
            key = f"{fp.name}|m{mi}|{lm.match_id}"
            yield from iter_gre_combat(facts, lm.messages, seat, SOURCE_GRE_LOG, key, args, stats)


def iter_rply(facts: CombatFacts, root: Path, args, stats: Counter) -> Iterable[Any]:
    """The owner's ``.rply`` captures.

    NOT EXERCISED on the machine this was written on — no ``.rply`` file exists
    under ``/Users`` or ``/Volumes``, and the server has none either. The episode
    grouping and the response parsing are the SAME code paths the manasight
    source exercises (both produce ``ReplayMessage`` lists), so the risk is
    confined to ``parse_replay_path`` and seat detection.
    """
    from tools.eval.replay.reader import parse_replay_path
    from tools.eval.replay.state import resolve_local_seat

    files = sorted(root.glob("*.rply"))
    stats["rply_files"] = len(files)
    for fp in files:
        try:
            _meta, messages = parse_replay_path(fp)
            seat = resolve_local_seat(messages)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not parse %s: %s", fp.name, exc)
            stats["rply_parse_errors"] += 1
            continue
        yield from iter_gre_combat(facts, messages, seat, SOURCE_RPLY, fp.name, args, stats)


# ---------------------------------------------------------------------------
# Source 4: the derived replay_strategic_groundtruth.jsonl (bias warning)
# ---------------------------------------------------------------------------


def iter_derived(facts: CombatFacts, path: Path, args, stats: Counter) -> Iterable[Any]:
    """Read attack/block rows out of the already-extracted strategic groundtruth.

    Every attack row here has ``|chosen| <= 1`` because
    ``build_attack_decision`` drops autoDeclare and multi-creature declarations,
    and every block row has at most one (blocker, attacker) pair. Off by default
    for exactly that reason.
    """
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            kind = rec.get("decision_kind")
            if kind not in ("attack_declaration", "block_assignment"):
                continue
            state = rec.get("state") or {}
            ctx = rec.get("decision_context") or {}
            own = [int(c["grp_id"]) for c in state.get("creatures_in_play") or [] if c.get("grp_id")]
            opp = [int(g) for g in state.get("opp_creatures_grp_ids") or []]
            lands = [int(g) for g in state.get("lands_in_play_grp_ids") or []]
            life = (state.get("life") or {}).get("you")
            opp_life = (state.get("life") or {}).get("opp")
            key = f"{rec.get('replay_file')}|{rec.get('decision_uid')}"
            if kind == "attack_declaration":
                pool = _tag(facts, own)
                sel = ctx.get("selected_attacker_instance_id")
                # Instance ids in the derived file do not survive the grpId
                # retagging, so match by the picked menu entry's grp id.
                pick = rec.get("real_pick") or {}
                pick_grp = pick.get("grp_id")
                chosen = _positional_match(pool, [int(pick_grp)] if pick_grp else [])
                stats["derived_attack"] += 1
                yield AttackDecision(
                    source=SOURCE_DERIVED,
                    key=key,
                    turn=int(rec.get("turn_number") or 0),
                    on_play=None,
                    pool=pool,
                    chosen_instance_ids=chosen,
                    life=life,
                    opp_life=opp_life,
                    opp_hand=state.get("opp_hand_size"),
                    own_board=own,
                    own_lands=lands,
                    opp_board=opp,
                    opp_creatures=_tag(facts, opp),
                    meta={
                        "reconstructed": False,
                        "shape_capped_to_single_attacker": True,
                        "selected_instance_id": sel,
                    },
                )
            else:
                stats["derived_block"] += 1
                # The derived rows carry only the picked (blocker, attacker)
                # equivalence key, not the full assignment map, so they cannot
                # express a multi-block answer either.
                continue


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _percent(n: int, d: int) -> float:
    return round(100.0 * n / d, 2) if d else 0.0


def run(args: argparse.Namespace) -> int:
    if args.solver_line != "off" and not args.allow_leaky_solver_line:
        raise SystemExit(
            "--solver-line "
            f"{args.solver_line!r} renders the gold answer into the prompt on "
            "every kept record where the solver matches the human label — a "
            "label leak. The combat_v1 LoRA trained on exactly this and was "
            "BLOCKED for hint-copying (2026-07-28 gate). Training corpora "
            "must use --solver-line off (the solver's pick stays in meta for "
            "DPO). If this corpus is for eval/debugging only, pass "
            "--allow-leaky-solver-line."
        )
    t0 = time.time()
    resolver = CardResolver()
    facts = CombatFacts(resolver, CardDetail(resolver))
    emitter = Emitter(facts, args)
    stats: Counter[str] = Counter()

    out_fh = None
    if not args.audit_only:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        opener = gzip.open if args.out.suffix == ".gz" else open
        out_fh = opener(args.out, "wt", encoding="utf-8")  # type: ignore[operator]

    n_written = 0
    seen_ids: set[str] = set()
    try:
        streams: list[Iterable[Any]] = []
        for csv_path in args.replay_csv or []:
            if args.decision in ("attack", "both"):
                streams.append(iter_17lands_attacks(facts, csv_path, args, stats))
            if args.decision in ("block", "both"):
                streams.append(iter_17lands_blocks(facts, csv_path, args, stats))
        if args.gre_logs:
            streams.append(iter_gre_logs(facts, args.gre_logs, args, stats))
        if args.replays_dir:
            streams.append(iter_rply(facts, args.replays_dir, args, stats))
        if args.replay_groundtruth:
            streams.append(iter_derived(facts, args.replay_groundtruth, args, stats))

        for stream in streams:
            for dec in stream:
                if isinstance(dec, AttackDecision):
                    if args.decision == "block":
                        continue
                    rec = emitter.emit_attack(dec)
                elif isinstance(dec, BlockDecision):
                    if args.decision == "attack":
                        continue
                    rec = emitter.emit_block(dec)
                else:
                    continue
                if rec is None:
                    continue
                n_written += 1
                # Record ids must be unique. Every downstream harness keys by
                # id (``by_id = {rec["id"]: rec}`` in tools/eval/run.py and
                # gate_play_decisions.evaluate), so a duplicate is not a
                # cosmetic blemish — it SILENTLY DROPS the record from
                # scoring. A build that collided on 46% of records shipped
                # once; this refuses rather than letting it happen again.
                if rec["id"] in seen_ids:
                    raise SystemExit(
                        f"DUPLICATE record id {rec['id']} "
                        f"(record_key={rec['meta'].get('record_key')!r}) — refusing to "
                        "write a corpus whose records cannot be addressed individually. "
                        "The key needs a discriminator that separates these two decisions."
                    )
                seen_ids.add(rec["id"])
                if out_fh is not None:
                    out_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    finally:
        if out_fh is not None:
            out_fh.close()

    summary = {
        "READ_THIS_FIRST": (
            "The answer is a SUBSET, emitted as production's declare_attackers / "
            "declare_blockers action with an explicit creature list — NOT a "
            "{'pick': N} menu index. A pick at a real DeclareAttackers window "
            "resolves to ONE creature (rules_engine renders one row per "
            "creature), so a pick-shaped combat corpus is unexecutable. Quote "
            "attack_answer_mix, not the record total: a corpus that is mostly "
            "all-in or mostly no-attack teaches a reflex the same way the "
            "69%-land-drop corpus did."
        ),
        "records_written": n_written,
        "elapsed_sec": round(time.time() - t0, 1),
        "config": {
            "decision": args.decision,
            "min_pool": args.min_pool,
            "max_turn": args.max_turn,
            "min_rank": args.min_rank,
            "max_rows_per_file": args.max_rows_per_file,
            "solver_line": args.solver_line,
            "block_pairing": args.block_pairing,
            "oracle_chars": args.oracle_chars,
            "replay_csv": [str(p) for p in (args.replay_csv or [])],
            "gre_logs": str(args.gre_logs) if args.gre_logs else None,
            "replays_dir": str(args.replays_dir) if args.replays_dir else None,
            "replay_groundtruth": str(args.replay_groundtruth) if args.replay_groundtruth else None,
        },
        "funnel": dict(sorted(stats.items())),
        "emitter": dict(sorted(emitter.stats.items())),
        "by_source": dict(sorted(emitter.by_source.items())),
        "attack_pool_size_hist": dict(sorted(emitter.pool_hist.items())),
        "attack_chosen_size_hist": dict(sorted(emitter.chosen_hist.items())),
        "block_pool_size_hist": dict(sorted(emitter.block_pool_hist.items())),
        "block_chosen_size_hist": dict(sorted(emitter.block_chosen_hist.items())),
        "solver_agreement": dict(sorted(emitter.solver_agree.items())),
        "attribution": {
            "17lands": ATTRIBUTION_17LANDS,
            "manasight": ATTRIBUTION_MANASIGHT,
            "owner_replays": ATTRIBUTION_OWNER,
        },
        "known_gaps": [
            "17lands supplies NO blocker->attacker pairing; only single-attacker "
            "combats are emitted at all — every multi-attacker block window is "
            "dropped (blocked and unblocked alike, so the surviving records do "
            "not encode P(block | 2+ attackers) = 0), counted as "
            "drop_pairing_unknowable.",
            "17lands supplies no targets, no instance ids, no tapped state (except "
            "the derivable 'attacked last turn without vigilance' case, and only "
            "when multiset arithmetic proves no same-grpId copy died that turn), "
            "no stack and no instant-speed combat tricks.",
            "Multi-block (two blockers on one attacker) is representable; a single "
            "blocker blocking two attackers is NOT — production's "
            "blocker_assignments maps one blocker to one attacker.",
            "Damage-assignment order after a multi-block is a separate GRE request "
            "and is not covered by this corpus.",
        ],
    }
    n_atk = emitter.stats["emit_attack"]
    n_blk = emitter.stats["emit_block"]
    if n_atk:
        summary["attack_answer_mix"] = {
            "n": n_atk,
            "no_attack": emitter.answer_mix["attack_no_attack"],
            "no_attack_pct": _percent(emitter.answer_mix["attack_no_attack"], n_atk),
            "all_in": emitter.answer_mix["attack_all_in"],
            "all_in_pct": _percent(emitter.answer_mix["attack_all_in"], n_atk),
            "proper_subset": emitter.answer_mix["attack_proper_subset"],
            "proper_subset_pct": _percent(emitter.answer_mix["attack_proper_subset"], n_atk),
            "solver_agreement_pct": _percent(
                emitter.solver_agree["attack_agree"],
                emitter.solver_agree["attack_agree"] + emitter.solver_agree["attack_disagree"],
            ),
        }
    if n_blk:
        summary["block_answer_mix"] = {
            "n": n_blk,
            "no_block": emitter.answer_mix["block_none"],
            "no_block_pct": _percent(emitter.answer_mix["block_none"], n_blk),
            "block_all": emitter.answer_mix["block_all"],
            "partial": emitter.answer_mix["block_partial"],
            "solver_agreement_pct": _percent(
                emitter.solver_agree["block_agree"],
                emitter.solver_agree["block_agree"] + emitter.solver_agree["block_disagree"],
            ),
        }
    if args.stats_out:
        args.stats_out.parent.mkdir(parents=True, exist_ok=True)
        args.stats_out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--replay-csv", type=Path, action="append", help="17lands replay_data csv(.gz)")
    p.add_argument("--gre-logs", type=Path, help="directory of manasight Player.log(.gz) captures")
    p.add_argument("--replays-dir", type=Path, help="directory of MTGA .rply captures")
    p.add_argument(
        "--replay-groundtruth",
        type=Path,
        help="derived replay_strategic_groundtruth.jsonl (single-attacker shape — off by default)",
    )
    p.add_argument("--decision", choices=("attack", "block", "both"), default="both")
    p.add_argument("--min-pool", type=int, default=2, help="minimum candidate pool size (1 = keep forced)")
    p.add_argument("--max-turn", type=int, default=12)
    p.add_argument("--min-rank", default="diamond")
    p.add_argument("--max-rows-per-file", type=int, default=200_000)
    p.add_argument("--oracle-chars", type=int, default=160)
    p.add_argument(
        "--allow-leaky-solver-line",
        action="store_true",
        help="required to use --solver-line on/agree. Both modes print the "
        "solver's line only on records where it MATCHES the human label — "
        "i.e. the gold answer appears verbatim in the prompt. The 2026-07-28 "
        "combat gate proved a LoRA trained on such a corpus learns to copy "
        "the hint (1.0000 on hinted records, 0/289 on unhinted blocks "
        "requiring a block; verdict BLOCKED). Eval/debug corpora only.",
    )
    p.add_argument(
        "--solver-line",
        choices=("off", "on", "agree"),
        default="off",
        help="render combat_solver's 'Computed optimal ...' line. off: never. "
        "on: render it and DROP records whose human label disagrees with it "
        "(counted as drop_*_solver_line_conflict) — a rendered line the label "
        "contradicts would train the model to ignore the system prompt's "
        "deference rule with the reasoning escape hatch forbidden. agree: "
        "render only when it matches the human label; disagreeing records "
        "keep their label but get no line.",
    )
    p.add_argument(
        "--block-pairing",
        choices=("forced-only", "drop"),
        default="forced-only",
        help="17lands blocker->attacker pairing policy. Both values now emit "
        "only single-attacker combats (the pairing is forced, never guessed) "
        "and drop every multi-attacker window — blocked AND unblocked alike, "
        "because keeping the no-block half taught P(block | 2+ attackers)=0. "
        "'drop' is a compatibility alias of 'forced-only'; it formerly "
        "fabricated every-blocker->first-attacker pairings.",
    )
    p.add_argument("--out", type=Path, default=Path("tools/training/data/combat_decisions.jsonl"))
    p.add_argument("--stats-out", type=Path, default=None)
    p.add_argument("--audit-only", action="store_true", help="measure the funnel, write nothing")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if not any((args.replay_csv, args.gre_logs, args.replays_dir, args.replay_groundtruth)):
        build_parser().error("at least one source is required")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
