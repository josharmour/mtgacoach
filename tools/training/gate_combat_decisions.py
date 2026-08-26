#!/usr/bin/env python3
"""Fail-closed COMBAT-DECISION gate (attack / block SUBSETS, not menu picks).

Why this file exists
--------------------
``gate_play_decisions.py`` gates one answer shape: a numbered ``Legal:`` menu
answered with ``{"pick": N}``. Combat does not fit it. Production's
``declare_attackers`` / ``declare_blockers`` actions carry an explicit creature
list, so the answer is a **subset** of the attack pool (or a **mapping** of
blockers onto attackers), and a single index cannot express it — see
``build_combat_decisions.py``'s "REPRESENTATION" section for why option (a),
"render K candidate subsets as a menu", is silently unexecutable at serve time.

Everything downstream of that changes: legality is "does every name I emitted
exist on the board", accuracy is set equality rather than index equality, and
the reflex policies a gate has to floor against are the *degenerate answers* of
each family (swing with everyone / swing with nobody / never block) rather than
"always pick the first row".

Corpus — real 17lands games, split before anything was trained on it
--------------------------------------------------------------------
``tools/training/data/combat_gate_{test,dev,train}.jsonl`` +
``*_permuted.jsonl``, built by ``tools/training/split_combat_gate.py`` from
``combat_decisions.jsonl`` (96,131 decisions, 5,468 drafts, seed 20260726):

    train 82,425 | dev 6,854 | test 6,852   (+ permuted twins for dev/test)

Leakage unit is the DRAFT — a strict superset of the game, because
``meta.record_key``'s ``game_no`` is the literal string ``1`` on all 96,121
17lands records and individual games are therefore not addressable. The split
manifest's leak check is CLEAN: all six pairwise group intersections are empty
on both the group key and the raw draft id, ``sha256(user)`` intersections are
0/0/0, and dev/test carry no internal duplicate prompt.

Composition, measured (this is what the floors below are calibrated against)
----------------------------------------------------------------------------
    kind          attack 79.0%   block 21.0%
    attack class  no_attack 30.0% | all_in 30.2% | proper_subset 39.9%
    block  class  no_block  68.7% | partial   27.5% | block_all      3.8%
    solver agrees with the human on 38.0% of decisions
    ranks         diamond 71.5% | mythic 28.5%

**68.7% of every block decision in this corpus is "no block".** A model that
simply never blocks scores ~0.70 on the block slice and looks competent. That
single number is the reason this file exists in the shape it does: the 2026-07-24
play-decision run returned PASS for a LoRA that had learned a *land* reflex, and
the fix was to promote the reflex comparison from an advisory field to a hard
floor. The equivalent reflexes here are named, realized and gated.

Measured reflex floors on ``combat_gate_test.jsonl`` (n=6,852, seed 20260726)
-----------------------------------------------------------------------------
    attack slice (n=5,440)      block slice (n=1,412)
      always_all_in       0.3048  always_no_block               0.6983
      always_no_attack    0.2893  uniform_random_subset (exp.)  0.1805
      uniform_random(exp) 0.1768  always_chump_biggest_attacker 0.0992
      always_attack_biggest 0.1693 always_block_all             0.0326

    best combined reflex (always_all_in on attacks + always_no_block on
    blocks) = **0.3859 overall** — that is the G6 floor.

    on the two classes the degenerate answers cannot reach:
      attack:proper_subset (n=2,208)  best reflex always_attack_biggest 0.4171
      block:partial_block  (n=  380)  best reflex always_chump_…       0.3684

Those numbers are why "overall accuracy" is worthless on its own here. A
synthetic policy that plays every attack perfectly and simply never blocks
scores **0.9378 overall** on this corpus — a number that would sail through any
gate built on an overall floor. It is caught by G7 and G10 and by nothing else;
``tests/test_gate_combat_decisions.py`` pins that.

One more thing to know before reading any accuracy: **the decision space is
small on most records.** 52.5% of attack decisions offer a 2-creature pool
(4 possible subsets) and 27.3% offer 3 (8 subsets); blocks are 53.7% / 27.3%
at 2 / 3 blockers. That is why blind guessing already scores 0.177. Exact-set
accuracy can only be so discriminating on a corpus this shallow — the wide
boards where combat is genuinely hard are the 8.4% with 5+ creatures. Treat a
strong headline number with that in mind, and prefer the ``pool_size``
breakdown in ``corpus_composition`` over the single figure.

Copy-equivalence: one honest limitation
---------------------------------------
The attack menu renders ``Attack with: NAME (P/T)``; the block menu renders
``Block with: NAME`` with **no P/T**. Attack copy-classes are therefore
``(name, power, toughness)`` while blocker copy-classes are ``(name,)``. 18.2%
of block decisions in the test split (257/1,412) contain two identically-named
blockers, so this matters in principle — but every record in dev/test is a
*reconstructed* 17lands position with no live P/T at all (the 10 GRE/replay
records, the only ones that carry live state, were pinned to train by the
splitter), so within this corpus two identically-named blockers really are the
same printed creature. Revisit this if GRE-sourced records ever enter a gate
split.

Scoring
-------
1. **LEGALITY (hard, G2).** Every name in ``attacker_names`` must be a row of
   the prompt's attack menu; every key of ``blocker_assignments`` must be a row
   of the blocker menu and every value must be a creature listed under
   ``ATTACKING YOU:``. The declared ``action_type`` must match the decision
   family. Illegal or unparseable output is scored **wrong**, and the illegal
   rate is reported on its own (``illegal_rate``) so a name-mangling model is
   distinguishable from a strategically wrong one. Zero tolerance is not
   pedantry: ``action_planner._is_legal_combat_declaration`` validates
   ``plan_names.issubset(legal_names)`` and production *refuses* the action, so
   an unrecognised name is a turn the autopilot cannot take.

2. **EXACT-SET accuracy (the gate criterion, G4-G12).** Order-insensitive and
   **copy-permutation-equivalent**: two identically-named, identically-statted
   creatures are the same answer. The equivalence is not re-implemented here —
   ``Emitter._state_key`` / ``_attack_agrees`` / ``_block_agrees`` are imported
   from ``build_combat_decisions`` and called on reconstructed decision objects,
   so the gate agrees with the corpus builder by construction rather than by
   inspection.

3. **PARTIAL credit (Jaccard).** Multiset Jaccard over chosen copy-classes
   (attack) or over (blocker, attacker) copy-class pairs (block). Reported
   beside exact accuracy, never gated: a model can score 0.9 Jaccard while
   getting every decision *slightly* wrong, and "slightly wrong" in combat is
   how you lose a creature for nothing.

4. **SLICES.** attack vs block; the three attack classes; the three block
   classes; solver-agrees vs solver-disagrees; diamond vs mythic. Every slice
   gets a Wilson 95% CI, and the gated ones get hard sample floors.

If two identically-named blockers ever *did* differ in live P/T, the gate would
treat them as interchangeable. That is the only defensible reading — the model
answers from the menu, and the menu does not distinguish them — but it can
credit a technically different block. Attackers named as *values* in
``blocker_assignments`` do carry P/T (the ``ATTACKING YOU:`` block renders it),
so they keep the full ``(name, power, toughness)`` key.

Gates — all hard, all fail-closed
---------------------------------
  G1  schema validity        >= 99%  (``validators.validate_action_schema_json``)
                             and the same floor on declaration-parse rate — a
                             response that yields no executable declaration is
                             not a wrong answer, it is no answer
  G2  legality               == 100% of parsed declarations; ANY name that is
                             not on the board is a hard fail. ``illegal_rate``
                             is reported separately
  G3  permutation invariance |acc(test) - acc(test_permuted)| <= 0.05. The twin
                             is the same decision with the menu rows shuffled
                             and the SAME label (the answer is a name set, so a
                             reordering cannot change it). A gap means the model
                             memorised menu positions. ``set_agreement`` is
                             reported beside it: a model that reads the board
                             answers the same set either way even when it is
                             wrong, so a collapsed agreement separates a
                             memoriser from a genuine close-call flip
  G4  slice coverage         every gated slice must meet its sample floor;
                             a shortfall is BLOCKED, never a smaller gate
  G5  non-inferiority        paired-bootstrap 95% CI of the exact-set accuracy
                             delta vs the declared baseline must not fall below
                             -0.02 (``gate_stage0.paired_bootstrap_ci``, imported)
  G6  trivial floor OVERALL  exact-set accuracy must EXCEED the best combined
                             reflex — max over attack reflexes x max over block
                             reflexes, weighted by slice size. Beating "the best
                             single named policy" would be too easy: a model is
                             free to run one reflex per family, so the floor is
                             the best pair
  G7  trivial floor BLOCK    block-slice accuracy must EXCEED the best block
                             reflex. ``always_no_block`` is 0.687 of this slice
                             and is the specifically load-bearing one — a model
                             that never blocks must not clear this gate
  G8  trivial floor ATTACK   attack-slice accuracy must EXCEED the best attack
                             reflex (``always_no_attack`` / ``always_all_in`` /
                             ``always_attack_biggest`` / random-subset)
  G9  proper-subset floor    accuracy on ``attack:proper_subset`` must EXCEED
                             the best reflex measured on that same slice. This
                             is the class the two degenerate answers cannot
                             touch, i.e. the only place "knows how to attack"
                             can show up
  G10 partial-block floor    the same, on ``block:partial_block``

                             Only these two classes get a reflex floor, and the
                             reason is in ``slice_reflex_floors``: on
                             ``attack:no_attack`` the best reflex scores 1.0 by
                             definition (that IS the reflex), and the same holds
                             for ``all_in``, ``no_block`` and ``block_all``.
                             Gating a degenerate class against its own reflex
                             would make the gate unpassable, so those classes
                             are measured and sample-floored (G4) but not
                             floored on accuracy. Do not "finish the set".
  G11 rank-slice floors      diamond and mythic each above the best combined
                             reflex measured on that rank
  G12 non-degenerate move    paired-bootstrap 95% CI lower bound of the accuracy
                             delta vs baseline on
                             ``attack:proper_subset + block:partial_block`` must
                             exceed 0 — the combat analogue of the play gate's
                             G7. Overall movement is not enough: a run that
                             improves only on "swing with everything" has not
                             learned combat

Reported, explicitly NOT gated
------------------------------
  * **Solver agreement.** ``solver_agrees`` marks the 38.0% of decisions where
    ``combat_solver.optimal_attacks/optimal_blocks`` reached the same answer as
    the human. Accuracy is reported on both sides of that line because it
    answers a real question — is the model following humans or following the
    solver? — but it is not a pass condition in either direction. Diamond/mythic
    humans are not optimal, the solver is not omniscient (no tricks, no hidden
    information), and gating either way would encode a claim about which is
    right that this corpus cannot support.
  * **Jaccard.** Diagnostic only (see 3 above).
  * **``block:block_all``.** 3.3% of test blocks, n=46 — a Wilson 95%
    half-width of 13.9pp at p=0.5. Measured and reported, declared non-gated,
    with the number stated so the exclusion is auditable rather than quiet.
    Every other gated slice clears its floor: the smallest is
    ``block:partial_block`` at n=380 (half-width 5.0pp).

ANY missing input, sample shortfall, unparseable config, coverage hole, backend
error row, or exception => verdict BLOCKED, exit 2, nothing registered. CLI
thresholds are clamped so they can only be made STRICTER (``_clamp_thresholds``);
there is no bypass flag of any kind.

Usage
-----
    # score every reflex policy on the corpus (do this first — it is the floor)
    python -m tools.training.gate_combat_decisions calibrate \\
        --corpus tools/training/data/combat_gate_test.jsonl \\
        --out tools/training/data/gate_combat_decisions_calibration_test.json

    # get responses (any runner that reads {id, system, user})
    python -m tools.eval.run \\
        --prompts tools/training/data/combat_gate_test.jsonl \\
        --responses tools/eval/data/cd_candidate.jsonl \\
        --backend online:deepseek-v4-flash

    # gate
    python -m tools.training.gate_combat_decisions gate \\
        --corpus tools/training/data/combat_gate_test.jsonl \\
        --permuted-corpus tools/training/data/combat_gate_test_permuted.jsonl \\
        --responses tools/eval/data/cd_candidate.jsonl \\
        --permuted-responses tools/eval/data/cd_candidate_permuted.jsonl \\
        --baseline-responses tools/eval/data/cd_baseline.jsonl \\
        --backend-label <candidate> --baseline-label <baseline> \\
        --report tools/training/data/gate_combat_report.json

    # prove the harness with synthetic policies (no model, no GPU)
    python -m tools.training.gate_combat_decisions dryrun \\
        --corpus tools/training/data/combat_gate_test.jsonl \\
        --permuted-corpus tools/training/data/combat_gate_test_permuted.jsonl \\
        --policy no_block --policy all_in --policy illegal --policy memoriser
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import random
import re
import subprocess
import sys
import time  # noqa: F401  (timestamps in reports / simulated rows)
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
for _p in (str(REPO), str(SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Copy-permutation equivalence is IMPORTED, never re-implemented: the gate must
# agree with the corpus builder by construction. ``_state_key`` /
# ``_attack_agrees`` / ``_block_agrees`` are pure functions of the decision
# object they are handed, so a bare Emitter instance is a legitimate host for
# them (no backend, no card DB, no args).
from tools.training.build_combat_decisions import (  # noqa: E402
    AttackDecision,
    BlockDecision,
    Emitter,
)
from tools.training.gate_stage0 import paired_bootstrap_ci  # noqa: E402

# Menu / label parsing is imported from the splitter for the same reason: one
# definition of "what is a menu row" across build, split and gate.
from tools.training.split_combat_gate import menu_rows, row_name  # noqa: E402
from tools.training.validators import validate_action_schema_json  # noqa: E402

logger = logging.getLogger("tools.training.gate_combat_decisions")

_EMITTER = Emitter.__new__(Emitter)  # host for the imported equivalence methods


# --------------------------------------------------------------------------
# Constants — thresholds may only be tightened at the CLI (_clamp_thresholds)
# --------------------------------------------------------------------------

DEFAULT_DATA_DIR = REPO / "tools" / "training" / "data"
DEFAULT_CORPUS = DEFAULT_DATA_DIR / "combat_gate_test.jsonl"
DEFAULT_PERMUTED_CORPUS = DEFAULT_DATA_DIR / "combat_gate_test_permuted.jsonl"
DRYRUN_STEM = "gate_combat_decisions_dryrun"

# Sample floors. Calibrated against the real test split (n=6,852: attack 5,440,
# block 1,412; attack classes 1,574 / 1,658 / 2,208; block classes 986 / 380 /
# 46). Every gated slice clears its floor with room; ``block:block_all`` does
# not and is declared non-gated below rather than silently averaged away.
MIN_SAMPLES = 1000
MIN_ATTACK_SAMPLES = 400
MIN_BLOCK_SAMPLES = 200
MIN_GATED_SLICE_SAMPLES = 200
MIN_RANK_SLICE_SAMPLES = 200

MIN_SCHEMA_RATE = 0.99  # G1
MAX_ILLEGAL_VIOLATIONS = 0  # G2 — production refuses an unrecognised name
MAX_PERMUTATION_ACC_GAP = 0.05  # G3
MAX_ACCURACY_REGRESSION = 0.02  # G5
MIN_TRIVIAL_POLICY_MARGIN = 0.0  # G6/G7/G8/G9/G10/G11 — strictly greater
MIN_NONDEGENERATE_DELTA_CI_LOWER = 0.0  # G12

DEFAULT_SEED = 20260726
DEFAULT_BOOTSTRAPS = 10000

# ``block:block_all`` is 0.79% of the corpus (758/96,131) and n=46 in the test
# split — Wilson 95% half-width 13.9pp at p=0.5, i.e. the slice cannot
# distinguish a good model from a bad one. Measured, reported, not gated.
NON_GATED_ANSWER_CLASSES = frozenset({"block:block_all"})

ATTACK_REFLEXES = (
    "always_no_attack",
    "always_all_in",
    "always_attack_biggest",
    "uniform_random_subset_expectation",
)
BLOCK_REFLEXES = (
    "always_no_block",
    "always_block_all",
    "always_chump_biggest_attacker",
    "uniform_random_subset_expectation",
)

# The three classes the degenerate answers cannot reach. G9/G10/G12 live here.
NON_DEGENERATE_CLASSES = frozenset({"attack:proper_subset", "block:partial_block"})

ATTACK_ROW_PREFIX = "Attack with: "
BLOCK_ROW_PREFIX = "Block with: "
ATTACKERS_HEADER = "ATTACKING YOU:"

# ``  Thawbringer 4/2 - When this creature enters…`` — P/T is appended by
# ``render_block_user`` only when both values resolved, and the oracle tail is
# joined with " - ".
_ATTACKER_PT_RE = re.compile(r"^(?P<name>.+?) (?P<p>-?[\d*+]+)/(?P<t>-?[\d*+]+)$")
# ``Attack with: NAME (2/3)`` / ``… (0/4) [0 POWER — attacking deals 0 damage]``.
# Anchored at the end AFTER the bracketed annotation is stripped, mirroring
# ``split_combat_gate.row_name``'s own two-step strip — an unanchored search
# would take a "(1/1)" out of the middle of a card name.
_ROW_ANNOTATION_RE = re.compile(r"\s*\[[^\[\]]*\]\s*$")
_ROW_PT_RE = re.compile(r"\((?P<p>-?[\d*+]+)/(?P<t>-?[\d*+]+)\)\s*$")


# --------------------------------------------------------------------------
# Small IO / stats helpers (same shapes as gate_play_decisions)
# --------------------------------------------------------------------------


def _read_jsonl(path: Path, *, skip_malformed: bool = False) -> Iterator[dict]:
    malformed = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                if not skip_malformed:
                    raise
                # LLM responses can embed raw control chars (e.g. \x01) that make a
                # single row unparseable. For RESPONSE files (the model's output) a
                # malformed row is skippable — it's the model's fault, not a missing
                # prompt. Callers pass skip_malformed=True only for response reading;
                # corpus/prompt files stay strict (a malformed prompt is a real error).
                malformed += 1
    if malformed:
        print(f"[gate_combat_decisions] _read_jsonl({path}): skipped {malformed} malformed "
              f"response row(s) (raw control char / bad JSON); fail-closed on genuine "
              f"errors is preserved.", file=sys.stderr)


def _write_jsonl(path: Path, rows: Iterable[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
    return n


def wilson_ci(successes: int, n: int, z: float = 1.959963984540054) -> list[float] | None:
    """Binomial 95% CI (Wilson score, no scipy). Same estimator as the play gate."""
    if n <= 0:
        return None
    p = successes / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def _multinomial(total: int, counts: Iterable[int]) -> int:
    out = 1
    rem = total
    for c in counts:
        out *= math.comb(rem, c)
        rem -= c
    return out


def _int_pt(value: str | None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Prompt parsing -> scoreable decision
# --------------------------------------------------------------------------


def parse_menu(user: str) -> tuple[list[tuple[str, int | None, int | None]], str]:
    """Creature rows of the numbered menu as (display_name, power, toughness).

    Uses ``split_combat_gate.menu_rows`` / ``row_name`` so "what is a menu row"
    has exactly one definition across build, split and gate. The trailing
    ``Done`` row is dropped: it is a production rendering artefact, never part
    of a declaration (the system prompt says so outright).
    """
    _prefix, rows, _done, _suffix = menu_rows(user)
    if not rows:
        raise ValueError("menu has no creature rows")
    kinds = {ATTACK_ROW_PREFIX if r.startswith(ATTACK_ROW_PREFIX) else BLOCK_ROW_PREFIX for r in rows}
    if len(kinds) != 1:
        raise ValueError(f"menu mixes attack and block rows: {rows[:3]}")
    kind = "attack" if kinds == {ATTACK_ROW_PREFIX} else "block"
    out: list[tuple[str, int | None, int | None]] = []
    for text in rows:
        name = row_name(text)
        m = _ROW_PT_RE.search(_ROW_ANNOTATION_RE.sub("", text))
        p = _int_pt(m.group("p")) if m else None
        t = _int_pt(m.group("t")) if m else None
        out.append((name, p, t))
    return out, kind


def parse_attackers(user: str) -> list[tuple[str, int | None, int | None]]:
    """The ``ATTACKING YOU:`` roster — the legal *values* of a block mapping.

    ``render_block_user`` emits ``  {name}{ P/T}{ - oracle}``. The oracle tail is
    split off first (it is the only place " - " appears in a rendered row; card
    names use an em dash, not " - "), then the P/T suffix. Any row that does not
    parse makes the record unscoreable, which is a corpus bug, not a model bug —
    ``load_corpus`` fails closed on it rather than gating on a partial roster.
    """
    if ATTACKERS_HEADER not in user:
        raise ValueError("block prompt has no ATTACKING YOU: section")
    body = user.split(ATTACKERS_HEADER, 1)[1]
    out: list[tuple[str, int | None, int | None]] = []
    for line in body.split("\n")[1:]:
        if not line.strip():
            break
        if not line.startswith("  "):
            break
        head = line[2:].split(" - ", 1)[0].rstrip()
        m = _ATTACKER_PT_RE.match(head)
        if m:
            out.append((m.group("name"), _int_pt(m.group("p")), _int_pt(m.group("t"))))
        else:
            out.append((head, None, None))
    if not out:
        raise ValueError("ATTACKING YOU: section is empty")
    return out


@dataclass
class ScoreRecord:
    """One scoreable combat decision. Prompt text is discarded after parsing.

    The corpus is 87MB per split; holding ``user`` for 6,852 identity records
    plus 6,852 twins is gigabytes for no benefit, so everything the gate needs
    is extracted here and the text is dropped.
    """

    rid: str
    kind: str  # "attack" | "block"
    dec: Any  # AttackDecision | BlockDecision (host for the imported equivalence)
    pool_names: list[str]  # menu order: attack pool, or blocker pool
    pool_power: list[int | None]
    iid_by_name: dict[str, int]
    attacker_names: list[str] = field(default_factory=list)
    attacker_power: list[int | None] = field(default_factory=list)
    attacker_iid_by_name: dict[str, int] = field(default_factory=dict)
    gold_ids: list[int] = field(default_factory=list)  # attack
    gold_map: dict[int, int] = field(default_factory=dict)  # block
    answer_class: str = ""
    meta: dict = field(default_factory=dict)

    # ---- copy-class helpers (all routed through the imported _state_key) ----
    def _pool_gid(self) -> dict[int, Any]:
        rows = self.dec.pool if self.kind == "attack" else self.dec.blockers
        return {i: g for (_n, g, i) in rows}

    def pool_keys(self, iids: Iterable[int]) -> Counter:
        gid = self._pool_gid()
        return Counter(_EMITTER._state_key(self.dec, gid, i) for i in iids)

    def pair_keys(self, mapping: dict[int, int]) -> Counter:
        b_gid = {i: g for (_n, g, i) in self.dec.blockers}
        a_gid = {i: g for (_n, g, i) in self.dec.attackers}
        return Counter(
            (
                _EMITTER._state_key(self.dec, b_gid, int(b)),
                _EMITTER._state_key(self.dec, a_gid, int(a)),
            )
            for b, a in mapping.items()
        )

    @property
    def gold_declaration(self) -> tuple[str, Any]:
        if self.kind == "attack":
            names = [n for n in self.pool_names if self.iid_by_name[n] in set(self.gold_ids)]
            return ("attack", names)
        by_iid = {v: k for k, v in self.iid_by_name.items()}
        atk_by_iid = {v: k for k, v in self.attacker_iid_by_name.items()}
        return ("block", {by_iid[b]: atk_by_iid[a] for b, a in self.gold_map.items()})


def derive_answer_class(kind: str, pool_n: int, chosen_n: int) -> str:
    """Answer class from the parsed prompt + label, not from a meta flag.

    Re-derived so the gate is self-consistent; ``load_corpus`` cross-checks it
    against ``meta.answer_class`` (written by ``split_combat_gate.answer_class``)
    and fails closed on a mismatch, which would mean the prompt and the metadata
    disagree about what the decision was.
    """
    if kind == "attack":
        if chosen_n == 0:
            return "attack:no_attack"
        if pool_n and chosen_n >= pool_n:
            return "attack:all_in"
        return "attack:proper_subset"
    if chosen_n == 0:
        return "block:no_block"
    if pool_n and chosen_n >= pool_n:
        return "block:block_all"
    return "block:partial_block"


def build_score_record(rec: dict) -> ScoreRecord:
    """Corpus record -> ScoreRecord. Raises on anything ambiguous (fail closed)."""
    rid = rec["id"]
    meta = rec.get("meta") or {}
    rows, kind = parse_menu(rec["user"])
    declared_kind = "attack" if rec.get("kind") == "combat_attack" else "block"
    if kind != declared_kind:
        raise ValueError(f"{rid}: menu says {kind!r} but record kind is {rec.get('kind')!r}")

    names = [n for (n, _p, _t) in rows]
    if len(set(names)) != len(names):
        raise ValueError(f"{rid}: menu has duplicate row names — disambiguation failed")
    iid_by_name = {n: i for i, n in enumerate(names)}

    gold_kind, gold_answer, reason = extract_declaration(rec.get("response") or "")
    if gold_kind != kind:
        raise ValueError(f"{rid}: gold label is not a {kind} declaration ({reason})")

    if kind == "attack":
        dec = AttackDecision(source="gate", key=rid, turn=int(meta.get("turn") or 0), on_play=None)
        # grp proxy = the display name with its "#N" copy suffix stripped by the
        # menu renderer's own convention; live = the rendered (P/T). Together
        # that is exactly the (grpId, live state) pair _state_key compares.
        dec.pool = [(n, _base_name(n), iid_by_name[n]) for n in names]
        dec.live = {iid_by_name[n]: (p, t, False) for (n, p, t) in rows}
        missing = [n for n in gold_answer if n not in iid_by_name]
        if missing:
            raise ValueError(f"{rid}: gold names not in menu: {missing}")
        gold_ids = sorted({iid_by_name[n] for n in gold_answer})
        dec.chosen_instance_ids = list(gold_ids)
        cls = derive_answer_class("attack", len(names), len(gold_ids))
        sr = ScoreRecord(
            rid=rid,
            kind="attack",
            dec=dec,
            pool_names=names,
            pool_power=[p for (_n, p, _t) in rows],
            iid_by_name=iid_by_name,
            gold_ids=gold_ids,
            answer_class=cls,
        )
    else:
        atk_rows = parse_attackers(rec["user"])
        atk_names = [n for (n, _p, _t) in atk_rows]
        if len(set(atk_names)) != len(atk_names):
            raise ValueError(f"{rid}: ATTACKING YOU: has duplicate names")
        atk_iid = {n: 1000 + i for i, n in enumerate(atk_names)}
        dec = BlockDecision(source="gate", key=rid, turn=int(meta.get("turn") or 0), on_play=None)
        dec.blockers = [(n, _base_name(n), iid_by_name[n]) for n in names]
        dec.attackers = [(n, _base_name(n), atk_iid[n]) for n in atk_names]
        # Only attackers get live P/T: the block menu renders none for blockers,
        # so blocker copy-classes are name-level by construction (see docstring).
        dec.live = {atk_iid[n]: (p, t, False) for (n, p, t) in atk_rows}
        bad_b = [b for b in gold_answer if b not in iid_by_name]
        bad_a = [a for a in gold_answer.values() if a not in atk_iid]
        if bad_b or bad_a:
            raise ValueError(f"{rid}: gold names off the board (blockers={bad_b} attackers={bad_a})")
        gold_map = {iid_by_name[b]: atk_iid[a] for b, a in gold_answer.items()}
        dec.assignments = dict(gold_map)
        cls = derive_answer_class("block", len(names), len(gold_map))
        sr = ScoreRecord(
            rid=rid,
            kind="block",
            dec=dec,
            pool_names=names,
            pool_power=[p for (_n, p, _t) in rows],
            iid_by_name=iid_by_name,
            attacker_names=atk_names,
            attacker_power=[p for (_n, p, _t) in atk_rows],
            attacker_iid_by_name=atk_iid,
            gold_map=gold_map,
            answer_class=cls,
        )

    recorded = meta.get("answer_class")
    if recorded and f"{sr.kind}:{recorded}" != sr.answer_class:
        raise ValueError(
            f"{rid}: derived answer class {sr.answer_class!r} disagrees with "
            f"meta.answer_class {recorded!r} — prompt and metadata describe different decisions"
        )
    sr.meta = {
        "answer_class": sr.answer_class,
        "kind": sr.kind,
        "rank": meta.get("rank"),
        "expansion": meta.get("expansion"),
        "solver_agrees": meta.get("solver_agrees"),
        "menu_size": meta.get("menu_size"),
        "turn": meta.get("turn"),
        "split": meta.get("split"),
        "variant": meta.get("variant"),
        "twin_id": meta.get("twin_id"),
        "game_id": meta.get("game_id"),
        "pool_size": len(sr.pool_names),
        "n_attackers": len(sr.attacker_names),
        "source": rec.get("source"),
    }
    return sr


def _base_name(display: str) -> str:
    """``Grizzly Bears #2`` -> ``Grizzly Bears`` (the copy-class identity).

    ``build_combat_decisions.disambiguate`` appends ``#N`` only to names that
    occur more than once, so stripping it is exactly undoing that step.
    """
    return re.sub(r"\s+#\d+$", "", display)


def load_corpus(path: Path, *, limit: int | None = None) -> list[ScoreRecord]:
    """Parse a corpus file into ScoreRecords. Any unparseable record raises."""
    out: list[ScoreRecord] = []
    for i, rec in enumerate(_read_jsonl(path)):
        if limit is not None and i >= limit:
            break
        out.append(build_score_record(rec))
    ids = Counter(r.rid for r in out)
    dupes = [k for k, v in ids.items() if v > 1]
    if dupes:
        raise ValueError(
            f"{path}: {len(dupes)} duplicate record ids (e.g. {dupes[:3]}) — a response file "
            "keyed by prompt id would silently score one and drop the rest"
        )
    return out


# --------------------------------------------------------------------------
# Response parsing
# --------------------------------------------------------------------------


def extract_declaration(response: str) -> tuple[str | None, Any, str]:
    """``{"actions":[{"action_type":"declare_attackers","attacker_names":[…]}]}``.

    Mirrors ``ActionPlanner._parse_response``'s tolerance (code fence, trailing
    commas) and nothing more. Returns ``("attack", [names])`` /
    ``("block", {blocker: attacker})`` / ``(None, None, reason)``.

    A ``declare_attackers`` action with no ``attacker_names`` key is *not*
    read as "attack with nobody": "no attack" is a real, common answer
    (30.0% of attacks) and inferring it from a missing field would credit a
    malformed response with the single most likely label.
    """
    s = (response or "").strip()
    if not s:
        return None, None, "empty response"
    body = s
    fence = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", s, re.DOTALL)
    if fence:
        body = fence.group(1).strip()
    body = re.sub(r",\s*([\]}])", r"\1", body)
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        return None, None, f"json: {e}"
    if not isinstance(data, dict):
        return None, None, "root is not an object"
    actions = data.get("actions")
    if not isinstance(actions, list) or not actions or not isinstance(actions[0], dict):
        return None, None, "missing actions[0]"
    act = actions[0]
    atype = act.get("action_type")
    if atype == "declare_attackers":
        names = act.get("attacker_names")
        if not isinstance(names, list) or any(not isinstance(n, str) for n in names):
            return None, None, "attacker_names missing or not a list of strings"
        return "attack", list(names), ""
    if atype == "declare_blockers":
        mapping = act.get("blocker_assignments")
        if not isinstance(mapping, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in mapping.items()
        ):
            return None, None, "blocker_assignments missing or not a str->str map"
        return "block", dict(mapping), ""
    return None, None, f"action_type={atype!r} is not a combat declaration"


# --------------------------------------------------------------------------
# Scoring one answer
# --------------------------------------------------------------------------


def score_declaration(rec: ScoreRecord, kind: str | None, answer: Any) -> dict:
    """Legality + exact-set + Jaccard for one (record, declaration) pair.

    ``exact`` is decided by the IMPORTED ``_attack_agrees`` / ``_block_agrees``,
    so a copy permutation (Bear #1 where Bear #2 was recorded) is the same
    answer here and in the corpus builder.
    """
    if kind is None:
        return {"parsed": False, "legal": False, "exact": False, "jaccard": 0.0, "reason": "unparsed"}
    if kind != rec.kind:
        return {
            "parsed": True,
            "legal": False,
            "exact": False,
            "jaccard": 0.0,
            "reason": f"declared {kind} on a {rec.kind} decision",
        }

    if rec.kind == "attack":
        names = list(dict.fromkeys(answer))  # a repeated name is one creature
        unknown = [n for n in names if n not in rec.iid_by_name]
        if unknown:
            return {
                "parsed": True,
                "legal": False,
                "exact": False,
                "jaccard": 0.0,
                "reason": f"not in attack pool: {unknown[:3]}",
                "duplicate_names": len(answer) != len(names),
            }
        pred_ids = [rec.iid_by_name[n] for n in names]
        exact = bool(_EMITTER._attack_agrees(rec.dec, pred_ids))
        pred_keys = rec.pool_keys(pred_ids)
        jac = _jaccard(pred_keys, rec.pool_keys(rec.gold_ids))
        return {
            "parsed": True,
            "legal": True,
            "exact": exact,
            "jaccard": jac,
            "reason": "",
            "answer_key": _answer_fingerprint(pred_keys),
            "pred_class": derive_answer_class("attack", len(rec.pool_names), len(pred_ids)),
            "duplicate_names": len(answer) != len(names),
        }

    mapping: dict[str, str] = dict(answer)
    unknown_b = [b for b in mapping if b not in rec.iid_by_name]
    unknown_a = [a for a in mapping.values() if a not in rec.attacker_iid_by_name]
    if unknown_b or unknown_a:
        return {
            "parsed": True,
            "legal": False,
            "exact": False,
            "jaccard": 0.0,
            "reason": f"off-board blockers={unknown_b[:3]} attackers={unknown_a[:3]}",
        }
    pred_map = {rec.iid_by_name[b]: rec.attacker_iid_by_name[a] for b, a in mapping.items()}
    exact = bool(_EMITTER._block_agrees(rec.dec, pred_map))
    pred_keys = rec.pair_keys(pred_map)
    jac = _jaccard(pred_keys, rec.pair_keys(rec.gold_map))
    return {
        "parsed": True,
        "legal": True,
        "exact": exact,
        "jaccard": jac,
        "reason": "",
        "answer_key": _answer_fingerprint(pred_keys),
        "pred_class": derive_answer_class("block", len(rec.pool_names), len(pred_map)),
    }


def _jaccard(pred: Counter, gold: Counter) -> float:
    """Multiset Jaccard. Two empty answers agree completely (both declined)."""
    if not pred and not gold:
        return 1.0
    inter = sum((pred & gold).values())
    union = sum((pred | gold).values())
    return round(inter / union, 4) if union else 1.0


def _answer_fingerprint(keys: Counter) -> str:
    """Stable id of an answer's COPY-CLASS multiset.

    Copy classes are menu-order independent, so the identity record and its
    permuted twin produce the same fingerprint for the same strategic answer.
    That is what makes ``permutation_invariance.set_agreement`` meaningful: it
    asks whether the model gave the SAME answer, not whether it was right twice.
    """
    payload = "|".join(sorted(f"{k!r}x{v}" for k, v in keys.items()))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# Trivial policies — the load-bearing floors
# --------------------------------------------------------------------------


def _biggest(names: list[str], powers: list[int | None]) -> str | None:
    """Highest power, ties broken by menu order. Unknown power sorts last."""
    if not names:
        return None
    best_i = max(range(len(names)), key=lambda i: (powers[i] if powers[i] is not None else -99, -i))
    return names[best_i]


# Which family each named reflex belongs to. A reflex evaluated on the OTHER
# family answers "commit nothing" — the definition that makes each named policy
# a single well-defined policy over the whole corpus (see reference_policies).
_ATTACK_FAMILY = frozenset(
    {"no_attack", "always_no_attack", "all_in", "always_all_in", "biggest", "always_attack_biggest"}
)
_BLOCK_FAMILY = frozenset(
    {
        "no_block",
        "always_no_block",
        "block_all",
        "always_block_all",
        "chump",
        "always_chump_biggest_attacker",
    }
)


def policy_declaration(policy: str, rec: ScoreRecord, rng: random.Random) -> tuple[str, Any]:
    """The answer a named reflex gives on this record.

    Every policy is defined from the PROMPT alone (menu order, rendered P/T) —
    none of them may consult the label, or the floor would be a cheat.

    ``always_attack_biggest``: the single highest-power row, ties broken by menu
    order. ``always_block_all``: every blocker blocks, spread round-robin over
    the attackers in listed order. ``always_chump_biggest_attacker``: exactly
    ONE blocker — the first in menu order, because the block menu renders no
    P/T and "my worst creature" is not expressible from the prompt — put in
    front of the highest-power attacker. That is deliberately a *partial* block
    so it is a real floor on ``block:partial_block`` rather than a duplicate of
    ``always_block_all``.
    """
    if rec.kind == "attack":
        if policy == "gold":
            return rec.gold_declaration
        if policy == "random":
            return ("attack", [n for n in rec.pool_names if rng.random() < 0.5])
        if policy in _BLOCK_FAMILY or policy in ("no_attack", "always_no_attack"):
            return ("attack", [])
        if policy in ("all_in", "always_all_in"):
            return ("attack", list(rec.pool_names))
        if policy in ("biggest", "always_attack_biggest"):
            b = _biggest(rec.pool_names, rec.pool_power)
            return ("attack", [b] if b else [])
        raise ValueError(f"unknown policy {policy!r}")

    if policy == "gold":
        return rec.gold_declaration
    if policy == "random":
        out: dict[str, str] = {}
        for n in rec.pool_names:
            if not rec.attacker_names:
                continue
            choice = rng.randrange(len(rec.attacker_names) + 1)
            if choice < len(rec.attacker_names):
                out[n] = rec.attacker_names[choice]
        return ("block", out)
    if policy in _ATTACK_FAMILY or policy in ("no_block", "always_no_block"):
        return ("block", {})
    if policy in ("block_all", "always_block_all"):
        if not rec.attacker_names:
            return ("block", {})
        return (
            "block",
            {n: rec.attacker_names[i % len(rec.attacker_names)] for i, n in enumerate(rec.pool_names)},
        )
    if policy in ("chump", "always_chump_biggest_attacker"):
        target = _biggest(rec.attacker_names, rec.attacker_power)
        if target is None or not rec.pool_names:
            return ("block", {})
        return ("block", {rec.pool_names[0]: target})
    raise ValueError(f"unknown policy {policy!r}")


def random_expectation(rec: ScoreRecord) -> float:
    """P(a uniformly random legal declaration is copy-equivalent to the label).

    Exact, not sampled. Attack: ``prod C(m_k, g_k) / 2^n`` over copy classes.
    Block: for each blocker class, the multinomial over which target class each
    copy takes, times ``a_t`` choices of identity attacker inside each target
    class, over ``(A+1)^B``.
    """
    if rec.kind == "attack":
        gid = {i: g for (_n, g, i) in rec.dec.pool}
        classes = Counter(_EMITTER._state_key(rec.dec, gid, i) for (_n, _g, i) in rec.dec.pool)
        chosen = Counter(_EMITTER._state_key(rec.dec, gid, i) for i in rec.gold_ids)
        equiv = 1
        for key, m in classes.items():
            equiv *= math.comb(m, chosen.get(key, 0))
        return equiv / (2 ** len(rec.dec.pool)) if rec.dec.pool else 1.0

    b_gid = {i: g for (_n, g, i) in rec.dec.blockers}
    a_gid = {i: g for (_n, g, i) in rec.dec.attackers}
    blocker_class = {i: _EMITTER._state_key(rec.dec, b_gid, i) for (_n, _g, i) in rec.dec.blockers}
    attacker_class = {i: _EMITTER._state_key(rec.dec, a_gid, i) for (_n, _g, i) in rec.dec.attackers}
    class_sizes = Counter(blocker_class.values())
    attacker_sizes = Counter(attacker_class.values())

    per_class: dict[Any, Counter] = defaultdict(Counter)
    for iid, bcls in blocker_class.items():
        tgt = rec.gold_map.get(iid)
        per_class[bcls][attacker_class[tgt] if tgt is not None else None] += 1

    equiv = 1
    for bcls, m in class_sizes.items():
        counts = list(per_class[bcls].values())
        counts.append(m - sum(counts))  # remainder must be "no block"
        if counts[-1] < 0:
            return 0.0
        equiv *= _multinomial(m, counts)
    for tcls, n_used in Counter(attacker_class[t] for t in rec.gold_map.values() if t is not None).items():
        equiv *= attacker_sizes[tcls] ** n_used

    total = (len(rec.dec.attackers) + 1) ** len(rec.dec.blockers)
    return equiv / total if total else 1.0


def reference_policies(corpus: list[ScoreRecord], *, seed: int = DEFAULT_SEED) -> dict:
    """Realized accuracy of every named reflex, overall and per family.

    ``overall`` extends a single-family reflex to the other family with that
    family's do-nothing answer, so each named policy is a single well-defined
    policy over the whole corpus. That makes ``always_no_attack`` and
    ``always_no_block`` numerically identical overall — they extend to the same
    "commit nothing" policy — which is stated rather than hidden. The number
    that matters for G6 is ``best_combined``: the best ATTACK reflex paired with
    the best BLOCK reflex, because nothing stops a model from running one reflex
    per family.
    """
    attack = [r for r in corpus if r.kind == "attack"]
    block = [r for r in corpus if r.kind == "block"]
    if not corpus:
        return {}

    def _acc(records: list[ScoreRecord], policy: str) -> float | None:
        if not records:
            return None
        hits = 0
        for r in records:
            rng = random.Random(f"{seed}:{policy}:{r.rid}")
            kind, answer = policy_declaration(policy, r, rng)
            if score_declaration(r, kind, answer)["exact"]:
                hits += 1
        return round(hits / len(records), 4)

    def _expectation(records: list[ScoreRecord]) -> float | None:
        if not records:
            return None
        return round(sum(random_expectation(r) for r in records) / len(records), 4)

    attack_slice = {p: _acc(attack, p) for p in ATTACK_REFLEXES if "random" not in p}
    attack_slice["uniform_random_subset_expectation"] = _expectation(attack)
    block_slice = {p: _acc(block, p) for p in BLOCK_REFLEXES if "random" not in p}
    block_slice["uniform_random_subset_expectation"] = _expectation(block)

    n, n_a, n_b = len(corpus), len(attack), len(block)

    def _overall(policy: str) -> float | None:
        a = attack_slice.get(policy)
        b = block_slice.get(policy)
        # A reflex of one family scores its own slice and "commit nothing" on
        # the other; do-nothing is itself a named reflex, so this composes.
        a = a if a is not None else attack_slice.get("always_no_attack")
        b = b if b is not None else block_slice.get("always_no_block")
        if n == 0:
            return None
        return round(((a or 0.0) * n_a + (b or 0.0) * n_b) / n, 4)

    named = sorted(set(ATTACK_REFLEXES) | set(BLOCK_REFLEXES))
    overall = {p: _overall(p) for p in named}

    best_a_name, best_a = _argmax(attack_slice)
    best_b_name, best_b = _argmax(block_slice)
    best_combined = round(((best_a or 0.0) * n_a + (best_b or 0.0) * n_b) / n, 4) if n else None
    return {
        "n": n,
        "n_attack": n_a,
        "n_block": n_b,
        "attack_slice": attack_slice,
        "block_slice": block_slice,
        "overall": overall,
        "best_attack_reflex": {"policy": best_a_name, "accuracy": best_a},
        "best_block_reflex": {"policy": best_b_name, "accuracy": best_b},
        "best_combined": {
            "attack_policy": best_a_name,
            "block_policy": best_b_name,
            "accuracy": best_combined,
        },
        "note": (
            "always_no_attack and always_no_block extend to the SAME 'commit nothing' "
            "policy overall; the per-slice numbers are the informative ones. G6 floors on "
            "best_combined (best attack reflex x best block reflex), which no single named "
            "policy can beat by construction."
        ),
    }


def _argmax(scores: dict[str, float | None]) -> tuple[str | None, float | None]:
    best_name: str | None = None
    best_val: float | None = None
    for name, val in scores.items():
        if val is None:
            continue
        if best_val is None or val > best_val:
            best_name, best_val = name, float(val)
    return best_name, best_val


def slice_reference_policies(corpus: list[ScoreRecord], predicate, *, seed: int = DEFAULT_SEED) -> dict:
    """Best reflex measured ON A SLICE (G9/G10/G11 floors).

    A reflex that is useless overall can still be strong inside one class —
    ``always_attack_biggest`` nails every single-creature attack — so the
    per-slice floor has to be recomputed on the slice, not inherited.
    """
    sub = [r for r in corpus if predicate(r)]
    if not sub:
        return {"n": 0, "policies": {}, "best": {"policy": None, "accuracy": None}}
    policies = sorted(set(ATTACK_REFLEXES) | set(BLOCK_REFLEXES))
    scores: dict[str, float | None] = {}
    for p in policies:
        if "random" in p:
            scores[p] = round(sum(random_expectation(r) for r in sub) / len(sub), 4)
            continue
        hits = 0
        for r in sub:
            rng = random.Random(f"{seed}:{p}:{r.rid}")
            kind, answer = policy_declaration(p, r, rng)
            if score_declaration(r, kind, answer)["exact"]:
                hits += 1
        scores[p] = round(hits / len(sub), 4)
    name, val = _argmax(scores)
    return {"n": len(sub), "policies": scores, "best": {"policy": name, "accuracy": val}}


# --------------------------------------------------------------------------
# Corpus composition
# --------------------------------------------------------------------------


def corpus_composition(corpus: list[ScoreRecord]) -> dict:
    """Read this before any accuracy number."""
    n = len(corpus)
    if not n:
        return {"n": 0}
    metas = [r.meta for r in corpus]
    classes = Counter(r.answer_class for r in corpus)
    kinds = Counter(r.kind for r in corpus)
    block_n = kinds.get("block", 0)
    attack_n = kinds.get("attack", 0)
    pool_sizes = sorted(len(r.pool_names) for r in corpus)
    dup_blockers = sum(
        1
        for r in corpus
        if r.kind == "block" and len({_base_name(x) for x in r.pool_names}) != len(r.pool_names)
    )
    return {
        "n": n,
        "by_kind": dict(kinds),
        "by_answer_class": dict(sorted(classes.items())),
        "answer_class_share": {k: round(v / n, 4) for k, v in sorted(classes.items())},
        "within_attack_share": {
            k.split(":", 1)[1]: round(v / attack_n, 4)
            for k, v in sorted(classes.items())
            if k.startswith("attack:") and attack_n
        },
        "within_block_share": {
            k.split(":", 1)[1]: round(v / block_n, 4)
            for k, v in sorted(classes.items())
            if k.startswith("block:") and block_n
        },
        "by_rank": dict(Counter(str(m.get("rank")) for m in metas)),
        "by_expansion": dict(Counter(str(m.get("expansion")) for m in metas)),
        "by_solver_agrees": dict(Counter(str(m.get("solver_agrees")) for m in metas)),
        "games": len({m.get("game_id") for m in metas if m.get("game_id")}),
        "pool_size": {
            "min": pool_sizes[0],
            "median": pool_sizes[len(pool_sizes) // 2],
            "max": pool_sizes[-1],
            "mean": round(sum(pool_sizes) / n, 2),
        },
        "blocks_with_duplicate_named_blockers": dup_blockers,
        "blocks_with_duplicate_named_blockers_share": round(dup_blockers / block_n, 4) if block_n else 0.0,
        "non_gated_answer_classes": sorted(NON_GATED_ANSWER_CLASSES),
    }


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


def _slice_stats(outcomes: list[dict]) -> dict:
    n = len(outcomes)
    exact = sum(1 for o in outcomes if o["exact"])
    illegal = sum(1 for o in outcomes if o["parsed"] and not o["legal"])
    unparsed = sum(1 for o in outcomes if not o["parsed"])
    return {
        "n": n,
        "exact": exact,
        "accuracy": round(exact / n, 4) if n else None,
        "accuracy_95ci": wilson_ci(exact, n),
        "jaccard_mean": round(sum(o["jaccard"] for o in outcomes) / n, 4) if n else None,
        "illegal": illegal,
        "illegal_rate": round(illegal / n, 4) if n else None,
        "unparsed": unparsed,
    }


def evaluate(corpus: list[ScoreRecord], responses_path: Path, backend_label: str) -> dict:
    """Score one (corpus, responses, backend) triple. No thresholds here."""
    by_id = {r.rid: r for r in corpus}
    seen: set[str] = set()
    duplicate_ids = 0
    outcomes: list[dict] = []
    per_id: dict[str, dict] = {}
    n = errors = schema_ok = 0
    illegal_examples: list[dict] = []
    parse_failure_reasons: Counter = Counter()
    decoding_params: list[dict] = []

    for row in _read_jsonl(responses_path, skip_malformed=True):
        if backend_label and row.get("backend") != backend_label:
            continue
        pid = row.get("prompt_id")
        rec = by_id.get(pid)
        if rec is None:
            continue
        if pid in seen:
            duplicate_ids += 1
            continue
        seen.add(pid)
        n += 1
        if row.get("decoding_params"):
            decoding_params.append(row["decoding_params"])
        if row.get("error"):
            errors += 1
            continue
        response = row.get("response") or ""
        ok_schema, _ = validate_action_schema_json(response)
        if ok_schema:
            schema_ok += 1
        kind, answer, reason = extract_declaration(response)
        if kind is None:
            parse_failure_reasons[reason.split(":")[0][:60]] += 1
        scored = score_declaration(rec, kind, answer)
        if scored["parsed"] and not scored["legal"] and len(illegal_examples) < 10:
            illegal_examples.append({"id": pid, "reason": scored["reason"]})
        outcome = {
            "id": pid,
            "kind": rec.kind,
            "answer_class": rec.answer_class,
            "parsed": scored["parsed"],
            "legal": scored["legal"],
            "exact": scored["exact"],
            "jaccard": scored["jaccard"],
            "schema_ok": ok_schema,
            "answer_key": scored.get("answer_key"),
            "pred_class": scored.get("pred_class"),
            "rank": rec.meta.get("rank"),
            "expansion": rec.meta.get("expansion"),
            "solver_agrees": rec.meta.get("solver_agrees"),
            "twin_id": rec.meta.get("twin_id"),
        }
        outcomes.append(outcome)
        per_id[pid] = outcome

    parsed = [o for o in outcomes if o["parsed"]]
    illegal = [o for o in parsed if not o["legal"]]
    by_kind = defaultdict(list)
    by_class = defaultdict(list)
    by_rank = defaultdict(list)
    by_solver = defaultdict(list)
    for o in outcomes:
        by_kind[o["kind"]].append(o)
        by_class[o["answer_class"]].append(o)
        by_rank[str(o["rank"])].append(o)
        by_solver[str(o["solver_agrees"])].append(o)
    non_degenerate = [o for o in outcomes if o["answer_class"] in NON_DEGENERATE_CLASSES]

    return {
        "backend": backend_label,
        "corpus_n": len(corpus),
        "responses_matched": n,
        "duplicate_response_rows": duplicate_ids,
        "missing_responses": len(corpus) - n,
        "errors": errors,
        # G1
        "schema_valid_rate": round(schema_ok / n, 4) if n else 0.0,
        "declaration_parse_rate": round(len(parsed) / n, 4) if n else 0.0,
        "parse_failure_reasons": dict(parse_failure_reasons.most_common(10)),
        # G2 — reported separately from accuracy, on purpose
        "illegal_declarations": len(illegal),
        "illegal_rate": round(len(illegal) / len(parsed), 4) if parsed else 0.0,
        "illegal_rate_of_all_responses": round(len(illegal) / n, 4) if n else 0.0,
        "illegal_examples": illegal_examples,
        # G4
        "overall": _slice_stats(outcomes),
        "by_kind": {k: _slice_stats(v) for k, v in sorted(by_kind.items())},
        "by_answer_class": {k: _slice_stats(v) for k, v in sorted(by_class.items())},
        "by_rank": {k: _slice_stats(v) for k, v in sorted(by_rank.items())},
        "by_solver_agreement": {k: _slice_stats(v) for k, v in sorted(by_solver.items())},
        "non_degenerate": _slice_stats(non_degenerate),
        "predicted_answer_class_histogram": dict(
            Counter(o["pred_class"] for o in outcomes if o["pred_class"])
        ),
        "per_id": per_id,
        "recorded_decoding_params": decoding_params[:5] if decoding_params else [{"temperature": 0.0}],
    }


def _paired_outcomes(candidate: dict, baseline: dict, predicate=None) -> list[dict]:
    """Candidate/baseline pairs for the same prompt id.

    Shape matches ``gate_stage0.paired_bootstrap_ci``; its balanced-accuracy arm
    is mulligan-specific (``truth`` in {keep, mull}) and comes back null here by
    construction, so only the raw delta CI is used.
    """
    pairs: list[dict] = []
    for pid, cand in candidate.get("per_id", {}).items():
        base = baseline.get("per_id", {}).get(pid)
        if base is None:
            continue
        if predicate is not None and not predicate(cand):
            continue
        pairs.append(
            {
                "prompt_id": pid,
                "truth": cand["answer_class"],
                "cand_correct": cand["exact"],
                "base_correct": base["exact"],
            }
        )
    return pairs


def permutation_metrics(identity: dict, permuted: dict, permuted_corpus: list[ScoreRecord]) -> dict:
    """G3 — same decision, shuffled menu, identical label.

    Unlike the play gate there is no gold index to move: the label is a set of
    names, so a correct answer is *invariant* by construction and any gap is
    position reading. ``set_agreement`` is the companion diagnostic — a model
    that answers the same set both ways (right or wrong) is reading the board.
    """
    twin_of = {r.rid: r.meta.get("twin_id") for r in permuted_corpus}
    pairs = []
    for perm_id, perm_outcome in permuted.get("per_id", {}).items():
        base_id = twin_of.get(perm_id)
        base_outcome = identity.get("per_id", {}).get(base_id)
        if base_outcome is None:
            continue
        pairs.append((base_outcome, perm_outcome))
    if not pairs:
        return {"paired_n": 0}
    n = len(pairs)
    id_correct = sum(1 for b, _ in pairs if b["exact"])
    perm_correct = sum(1 for _, p in pairs if p["exact"])
    # Agreement requires BOTH sides to have produced a legal declaration: two
    # unparseable or illegal answers are not evidence that the model read the
    # board the same way twice.
    agree = sum(
        1 for b, p in pairs if b.get("answer_key") is not None and b.get("answer_key") == p.get("answer_key")
    )
    both_right = sum(1 for b, p in pairs if b["exact"] and p["exact"])
    return {
        "paired_n": n,
        "identity_accuracy": round(id_correct / n, 4),
        "identity_accuracy_95ci": wilson_ci(id_correct, n),
        "permuted_accuracy": round(perm_correct / n, 4),
        "permuted_accuracy_95ci": wilson_ci(perm_correct, n),
        "accuracy_gap": round((id_correct - perm_correct) / n, 4),
        "abs_accuracy_gap": round(abs(id_correct - perm_correct) / n, 4),
        "set_agreement": round(agree / n, 4),
        "both_correct_rate": round(both_right / n, 4),
    }


# --------------------------------------------------------------------------
# Subcommand: calibrate
# --------------------------------------------------------------------------


def cmd_calibrate(args: argparse.Namespace) -> int:
    if not args.corpus.exists():
        logger.error(f"corpus missing: {args.corpus}")
        return 2
    corpus = load_corpus(args.corpus, limit=args.limit)
    out = {
        "corpus": str(args.corpus),
        "composition": corpus_composition(corpus),
        "reference_policies": reference_policies(corpus, seed=args.seed),
        "slice_floors": _slice_floors(corpus, seed=args.seed),
        "git_sha": _git_sha(),
        "ts": time.time(),
    }
    text = json.dumps(out, indent=2, ensure_ascii=False)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        logger.info(f"calibration -> {args.out}")
    else:
        print(text)
    return 0


def _slice_floors(corpus: list[ScoreRecord], *, seed: int) -> dict:
    """Per-slice reflex floors used by G9, G10 and G11."""
    floors = {
        "attack:proper_subset": slice_reference_policies(
            corpus, lambda r: r.answer_class == "attack:proper_subset", seed=seed
        ),
        "block:partial_block": slice_reference_policies(
            corpus, lambda r: r.answer_class == "block:partial_block", seed=seed
        ),
        "non_degenerate": slice_reference_policies(
            corpus, lambda r: r.answer_class in NON_DEGENERATE_CLASSES, seed=seed
        ),
    }
    for rank in ("diamond", "mythic"):
        floors[f"rank:{rank}"] = slice_reference_policies(
            corpus, lambda r, _r=rank: r.meta.get("rank") == _r, seed=seed
        )
    for cls in sorted({r.answer_class for r in corpus}):
        if cls in floors:
            continue
        floors[cls] = slice_reference_policies(corpus, lambda r, _c=cls: r.answer_class == _c, seed=seed)
    return floors


# --------------------------------------------------------------------------
# Subcommand: simulate (harness proof — no model, no GPU)
# --------------------------------------------------------------------------

ATTACK_POLICIES = ("gold", "no_attack", "all_in", "biggest", "random", "illegal", "memorise")
BLOCK_POLICIES = ("gold", "no_block", "block_all", "chump", "random", "illegal", "memorise")

# Ready-made candidate shapes: the four failure modes the gate must catch, plus
# the oracle that proves it can still say PASS.
DRYRUN_POLICIES = {
    "oracle": ("gold", "gold"),
    # A model that plays combat perfectly but NEVER blocks. Overall accuracy
    # looks strong; G7/G10 are the only things standing between it and a PASS.
    "no_block": ("gold", "no_block"),
    # The mirror: perfect blocks, but every attack is an all-in.
    "all_in": ("all_in", "gold"),
    # Names creatures that are not on the board — production would refuse it.
    "illegal": ("illegal", "illegal"),
    # Answers with the creatures sitting at the positions that were correct in
    # the identity corpus. Perfect on identity, near-chance on the twin.
    "memoriser": ("memorise", "memorise"),
    # The reflex baseline: commit nothing, ever.
    "do_nothing": ("no_attack", "no_block"),
}

ILLEGAL_NAME = "Phantom Nishoba (not on this board)"


def simulate_response(
    rec: ScoreRecord,
    attack_policy: str,
    block_policy: str,
    rng: random.Random,
    identity_gold: dict[str, list[int]] | None = None,
) -> str:
    policy = attack_policy if rec.kind == "attack" else block_policy
    if policy == "broken_schema":
        return "I would swing with the bear, it is clearly the strongest attack here."
    if policy == "illegal":
        if rec.kind == "attack":
            return json.dumps(
                {"actions": [{"action_type": "declare_attackers", "attacker_names": [ILLEGAL_NAME]}]}
            )
        target = rec.attacker_names[0] if rec.attacker_names else ILLEGAL_NAME
        return json.dumps(
            {"actions": [{"action_type": "declare_blockers", "blocker_assignments": {ILLEGAL_NAME: target}}]}
        )
    if policy == "memorise":
        # The identity record's gold POSITIONS, replayed against whatever now
        # sits at those positions. On the identity corpus that is the label; on
        # the permuted twin (menu rows shuffled, ATTACKING YOU: untouched) those
        # positions hold different creatures, so the answer is a different set.
        positions = (identity_gold or {}).get(rec.meta.get("twin_id") or rec.rid)
        if positions is None:
            positions = _gold_positions(rec)
        if rec.kind == "attack":
            names = [
                rec.pool_names[i] for i in positions if isinstance(i, int) and 0 <= i < len(rec.pool_names)
            ]
            return json.dumps({"actions": [{"action_type": "declare_attackers", "attacker_names": names}]})
        mapping = {}
        for pair in positions:
            b_i, a_i = pair
            if 0 <= b_i < len(rec.pool_names) and 0 <= a_i < len(rec.attacker_names):
                mapping[rec.pool_names[b_i]] = rec.attacker_names[a_i]
        return json.dumps({"actions": [{"action_type": "declare_blockers", "blocker_assignments": mapping}]})

    kind, answer = policy_declaration(policy, rec, rng)
    if kind == "attack":
        return json.dumps({"actions": [{"action_type": "declare_attackers", "attacker_names": list(answer)}]})
    return json.dumps({"actions": [{"action_type": "declare_blockers", "blocker_assignments": dict(answer)}]})


def _gold_positions(rec: ScoreRecord) -> list:
    """Label as MENU POSITIONS — attack: row indexes; block: (row, attacker) pairs.

    Instance ids in a ScoreRecord are the menu index (blockers/attack pool) and
    ``1000 + index`` (attackers), so this is a pure re-indexing, not a lookup.
    """
    if rec.kind == "attack":
        return sorted(rec.gold_ids)
    return sorted([b, a - 1000] for b, a in rec.gold_map.items())


def simulate_rows(
    corpus: list[ScoreRecord],
    attack_policy: str,
    block_policy: str,
    *,
    label: str,
    seed: int,
    identity_gold: dict[str, list[int]] | None = None,
) -> list[dict]:
    rows = []
    for rec in corpus:
        rng = random.Random(f"{seed}:{attack_policy}:{block_policy}:{rec.rid}")
        response = simulate_response(rec, attack_policy, block_policy, rng, identity_gold)
        rows.append(
            {
                "prompt_id": rec.rid,
                "backend": label,
                "model": f"synthetic-{label}",
                "response": response,
                "error": None,
                "latency_ms": 0.0,
                "response_chars": len(response),
                "temperature": 0.0,
                "decoding_params": {
                    "temperature": 0.0,
                    "attack_policy": attack_policy,
                    "block_policy": block_policy,
                },
                "ts": time.time(),
            }
        )
    return rows


def cmd_simulate(args: argparse.Namespace) -> int:
    if not args.corpus.exists():
        logger.error(f"corpus missing: {args.corpus}")
        return 2
    corpus = load_corpus(args.corpus, limit=args.limit)
    identity_gold = None
    if args.identity_corpus and args.identity_corpus.exists():
        identity_gold = {
            r.rid: _gold_positions(r) for r in load_corpus(args.identity_corpus, limit=args.limit)
        }
    rows = simulate_rows(
        corpus,
        args.attack_policy,
        args.block_policy,
        label=args.backend_label or f"synthetic:{args.attack_policy}+{args.block_policy}",
        seed=args.seed,
        identity_gold=identity_gold,
    )
    n = _write_jsonl(args.out, rows)
    logger.info(f"simulated attack={args.attack_policy} block={args.block_policy} n={n} -> {args.out}")
    return 0


# --------------------------------------------------------------------------
# Subcommand: gate
# --------------------------------------------------------------------------


def _clamp_thresholds(args: argparse.Namespace) -> list[str]:
    """CLI thresholds may only be made STRICTER than the module constants.

    "No bypass flag of any kind" operationally: a flag that could loosen a hard
    gate is a bypass with extra steps, so a loosening value is ignored and the
    attempted override is recorded in the report.
    """
    notes: list[str] = []
    clamps = (
        ("min_samples", MIN_SAMPLES, max),
        ("min_attack_samples", MIN_ATTACK_SAMPLES, max),
        ("min_block_samples", MIN_BLOCK_SAMPLES, max),
        ("min_slice_samples", MIN_GATED_SLICE_SAMPLES, max),
        ("min_rank_slice_samples", MIN_RANK_SLICE_SAMPLES, max),
        ("min_schema_rate", MIN_SCHEMA_RATE, max),
        ("max_permutation_gap", MAX_PERMUTATION_ACC_GAP, min),
        ("max_accuracy_regression", MAX_ACCURACY_REGRESSION, min),
        ("min_trivial_margin", MIN_TRIVIAL_POLICY_MARGIN, max),
        ("min_nondegenerate_delta_ci_lower", MIN_NONDEGENERATE_DELTA_CI_LOWER, max),
    )
    for name, floor, op in clamps:
        given = getattr(args, name)
        clamped = op(given, floor)
        if clamped != given:
            notes.append(f"--{name.replace('_', '-')}={given} ignored (would loosen); using {clamped}")
            setattr(args, name, clamped)
    return notes


def _floor_check(
    label: str,
    gate_id: str,
    observed: float | None,
    floor: dict,
    margin: float,
    failures: list[str],
    note: str = "",
) -> dict:
    """One 'must EXCEED the best reflex' criterion. Records it either way."""
    policy = floor.get("best", {}).get("policy")
    value = floor.get("best", {}).get("accuracy")
    record = {
        "slice": label,
        "candidate_accuracy": observed,
        "reflex_policy": policy,
        "reflex_accuracy": value,
        "margin_required": margin,
        "margin_observed": round(observed - value, 4)
        if (observed is not None and value is not None)
        else None,
        "policies": floor.get("policies", {}),
        "n": floor.get("n", 0),
    }
    if observed is None or value is None:
        failures.append(f"{gate_id} {label}: no reflex baseline computable — fail closed")
        record["verdict"] = "BLOCKED"
        return record
    if observed - value <= margin:
        failures.append(
            f"{gate_id} trivial-policy floor on {label}: candidate {observed:.4f} does not beat "
            f"reflex {policy!r} at {value:.4f} (margin {observed - value:+.4f} <= {margin})"
            + (f" — {note}" if note else "")
        )
        record["verdict"] = "FAIL"
    else:
        record["verdict"] = "PASS"
    return record


def cmd_gate(args: argparse.Namespace) -> int:  # noqa: C901 — one linear criteria list
    clamp_notes = _clamp_thresholds(args)
    for note in clamp_notes:
        logger.warning(note)
    verdict = "BLOCKED"
    failures: list[str] = []
    advisories: list[str] = []
    candidate: dict = {}
    baseline: dict = {}
    permuted: dict = {}
    perm_metrics: dict = {}
    bootstrap: dict = {}
    nondeg_bootstrap: dict = {}
    composition: dict = {}
    references: dict = {}
    floors: dict = {}
    criteria: list[dict] = []

    try:
        required = {
            "corpus": args.corpus,
            "permuted-corpus": args.permuted_corpus,
            "responses": args.responses,
            "permuted-responses": args.permuted_responses,
            "baseline-responses": args.baseline_responses,
        }
        for name, path in required.items():
            if path is None or not Path(path).exists():
                failures.append(f"missing required input --{name}: {path}")

        corpus: list[ScoreRecord] = []
        permuted_corpus: list[ScoreRecord] = []
        if not failures:
            corpus = args.preloaded_corpus or load_corpus(args.corpus)
            permuted_corpus = args.preloaded_permuted or load_corpus(args.permuted_corpus)
            if not corpus:
                failures.append("gate corpus is empty — fail closed")

        if not failures:
            composition = corpus_composition(corpus)
            references = args.preloaded_references or reference_policies(corpus, seed=args.seed)
            floors = args.preloaded_floors or _slice_floors(corpus, seed=args.seed)

            candidate = evaluate(corpus, args.responses, args.backend_label)
            permuted = evaluate(permuted_corpus, args.permuted_responses, args.backend_label)
            baseline = evaluate(corpus, args.baseline_responses, args.baseline_label)
            perm_metrics = permutation_metrics(candidate, permuted, permuted_corpus)

            # ---- coverage / sample sufficiency (fail closed) ----------------
            for label, metrics, size in (
                ("candidate", candidate, len(corpus)),
                ("permuted", permuted, len(permuted_corpus)),
                ("baseline", baseline, len(corpus)),
            ):
                if metrics["responses_matched"] != size:
                    failures.append(
                        f"{label} responses cover {metrics['responses_matched']}/{size} corpus "
                        "records — fail closed (no partial gate)"
                    )
                if metrics["errors"]:
                    failures.append(
                        f"{label} responses contain {metrics['errors']} backend errors — fail closed"
                    )
                if metrics["duplicate_response_rows"]:
                    failures.append(
                        f"{label} responses contain {metrics['duplicate_response_rows']} duplicate prompt_ids"
                    )

            # ---- G4 slice coverage -----------------------------------------
            if candidate["responses_matched"] < args.min_samples:
                failures.append(
                    f"G4 insufficient samples: {candidate['responses_matched']} < {args.min_samples}"
                )
            atk_n = candidate["by_kind"].get("attack", {}).get("n", 0)
            blk_n = candidate["by_kind"].get("block", {}).get("n", 0)
            if atk_n < args.min_attack_samples:
                failures.append(f"G4 insufficient attack samples: {atk_n} < {args.min_attack_samples}")
            if blk_n < args.min_block_samples:
                failures.append(f"G4 insufficient block samples: {blk_n} < {args.min_block_samples}")
            gated_classes = sorted(set(composition.get("by_answer_class", {})) - NON_GATED_ANSWER_CLASSES)
            for cls in gated_classes:
                got = candidate["by_answer_class"].get(cls, {}).get("n", 0)
                if got < args.min_slice_samples:
                    failures.append(
                        f"G4 gated slice {cls} has {got} < {args.min_slice_samples} samples — "
                        "fail closed (a slice too small to measure is not a slice to gate on)"
                    )
            for rank in ("diamond", "mythic"):
                got = candidate["by_rank"].get(rank, {}).get("n", 0)
                if got < args.min_rank_slice_samples:
                    failures.append(f"G4 rank slice {rank} has {got} < {args.min_rank_slice_samples} samples")
            for cls in sorted(NON_GATED_ANSWER_CLASSES & set(candidate["by_answer_class"])):
                stats = candidate["by_answer_class"][cls]
                advisories.append(
                    f"{cls} measured but NOT gated: n={stats['n']}, accuracy={stats['accuracy']}, "
                    f"95% CI {stats['accuracy_95ci']} — too small to distinguish policies"
                )

            # ---- G1 schema / parse ------------------------------------------
            if candidate["schema_valid_rate"] < args.min_schema_rate:
                failures.append(
                    f"G1 schema_valid_rate {candidate['schema_valid_rate']:.4f} < {args.min_schema_rate}"
                )
            if candidate["declaration_parse_rate"] < args.min_schema_rate:
                failures.append(
                    f"G1 declaration_parse_rate {candidate['declaration_parse_rate']:.4f} < "
                    f"{args.min_schema_rate} — responses that yield no executable declaration "
                    f"(reasons: {candidate['parse_failure_reasons']})"
                )

            # ---- G2 legality (hard) -----------------------------------------
            if candidate["illegal_declarations"] > MAX_ILLEGAL_VIOLATIONS:
                failures.append(
                    f"G2 legality: {candidate['illegal_declarations']} declaration(s) name a "
                    f"creature that is not on the board (illegal_rate "
                    f"{candidate['illegal_rate']:.4f}) — production refuses these. "
                    f"examples={candidate['illegal_examples'][:3]}"
                )
            if permuted["illegal_declarations"] > MAX_ILLEGAL_VIOLATIONS:
                failures.append(
                    f"G2 legality (permuted twin): {permuted['illegal_declarations']} illegal "
                    f"declaration(s) — hard fail"
                )

            # ---- G3 permutation invariance ----------------------------------
            if perm_metrics.get("paired_n", 0) < args.min_samples:
                failures.append(
                    f"G3 permutation twin paired on {perm_metrics.get('paired_n', 0)} decisions "
                    f"< {args.min_samples} — fail closed"
                )
            elif perm_metrics["abs_accuracy_gap"] > args.max_permutation_gap:
                failures.append(
                    f"G3 permutation invariance: |identity {perm_metrics['identity_accuracy']:.4f} - "
                    f"permuted {perm_metrics['permuted_accuracy']:.4f}| = "
                    f"{perm_metrics['abs_accuracy_gap']:.4f} > {args.max_permutation_gap}. The label "
                    "is a NAME SET and cannot change under a menu reorder, so a gap is menu-order "
                    f"memorisation (set_agreement {perm_metrics['set_agreement']:.4f})"
                )

            # ---- G5 non-inferiority vs baseline ------------------------------
            pairs = _paired_outcomes(candidate, baseline)
            if len(pairs) < args.min_samples:
                failures.append(
                    f"G5 paired bootstrap has {len(pairs)} paired decisions < {args.min_samples} "
                    "— fail closed"
                )
            else:
                bootstrap = paired_bootstrap_ci(pairs, n_bootstraps=args.bootstraps, seed=args.seed)
                bootstrap["paired_n"] = len(pairs)
                delta = candidate["overall"]["accuracy"] - baseline["overall"]["accuracy"]
                bootstrap["point_accuracy_delta"] = round(delta, 4)
                raw_ci = bootstrap.get("raw_accuracy_delta_95ci")
                if not raw_ci:
                    failures.append("G5 paired bootstrap produced no CI — fail closed")
                elif raw_ci[0] < -args.max_accuracy_regression:
                    failures.append(
                        f"G5 non-inferiority: accuracy delta 95% CI lower bound {raw_ci[0]:+.4f} < "
                        f"-{args.max_accuracy_regression} (point delta {delta:+.4f}) vs baseline "
                        f"{args.baseline_label!r}"
                    )

            # ---- G6 trivial floor OVERALL ------------------------------------
            best_combined = (references.get("best_combined") or {}).get("accuracy")
            cand_acc = candidate["overall"]["accuracy"]
            g6 = {
                "slice": "overall",
                "candidate_accuracy": cand_acc,
                "reflex_policy": (
                    f"{(references.get('best_combined') or {}).get('attack_policy')} + "
                    f"{(references.get('best_combined') or {}).get('block_policy')}"
                ),
                "reflex_accuracy": best_combined,
                "margin_required": args.min_trivial_margin,
                "margin_observed": round(cand_acc - best_combined, 4)
                if (cand_acc is not None and best_combined is not None)
                else None,
                "policies": references.get("overall", {}),
                "n": candidate["overall"]["n"],
            }
            if cand_acc is None or best_combined is None:
                failures.append("G6 trivial-policy floor: no reference baseline — fail closed")
                g6["verdict"] = "BLOCKED"
            elif cand_acc - best_combined <= args.min_trivial_margin:
                failures.append(
                    f"G6 trivial-policy floor: candidate accuracy {cand_acc:.4f} does not beat the "
                    f"best combined reflex ({g6['reflex_policy']}) at {best_combined:.4f} "
                    f"(margin {cand_acc - best_combined:+.4f} <= {args.min_trivial_margin}) — a model "
                    "that cannot beat a policy with no knowledge of the board has learned nothing"
                )
                g6["verdict"] = "FAIL"
            else:
                g6["verdict"] = "PASS"
            criteria.append({"id": "G6", **g6})

            # ---- G7 BLOCK-slice floor (the no-block reflex) -------------------
            block_floor = {
                "n": blk_n,
                "policies": references.get("block_slice", {}),
                "best": references.get("best_block_reflex", {}),
            }
            criteria.append(
                {
                    "id": "G7",
                    **_floor_check(
                        "block",
                        "G7",
                        candidate["by_kind"].get("block", {}).get("accuracy"),
                        block_floor,
                        args.min_trivial_margin,
                        failures,
                        note=(
                            "always_no_block is "
                            f"{references.get('block_slice', {}).get('always_no_block')} of this "
                            "slice — a model that never blocks must not clear this gate"
                        ),
                    ),
                }
            )

            # ---- G8 ATTACK-slice floor ---------------------------------------
            attack_floor = {
                "n": atk_n,
                "policies": references.get("attack_slice", {}),
                "best": references.get("best_attack_reflex", {}),
            }
            criteria.append(
                {
                    "id": "G8",
                    **_floor_check(
                        "attack",
                        "G8",
                        candidate["by_kind"].get("attack", {}).get("accuracy"),
                        attack_floor,
                        args.min_trivial_margin,
                        failures,
                    ),
                }
            )

            # ---- G9 / G10 non-degenerate class floors -------------------------
            for gate_id, cls in (("G9", "attack:proper_subset"), ("G10", "block:partial_block")):
                criteria.append(
                    {
                        "id": gate_id,
                        **_floor_check(
                            cls,
                            gate_id,
                            candidate["by_answer_class"].get(cls, {}).get("accuracy"),
                            floors.get(cls, {}),
                            args.min_trivial_margin,
                            failures,
                            note="the class the degenerate answers cannot reach",
                        ),
                    }
                )

            # ---- G11 rank slices ----------------------------------------------
            for rank in ("diamond", "mythic"):
                criteria.append(
                    {
                        "id": "G11",
                        **_floor_check(
                            f"rank:{rank}",
                            "G11",
                            candidate["by_rank"].get(rank, {}).get("accuracy"),
                            floors.get(f"rank:{rank}", {}),
                            args.min_trivial_margin,
                            failures,
                        ),
                    }
                )

            # ---- G12 movement on the non-degenerate slice ----------------------
            nondeg_pairs = _paired_outcomes(
                candidate, baseline, lambda o: o["answer_class"] in NON_DEGENERATE_CLASSES
            )
            nondeg_delta = None
            if (
                candidate["non_degenerate"]["accuracy"] is not None
                and baseline["non_degenerate"]["accuracy"] is not None
            ):
                nondeg_delta = round(
                    candidate["non_degenerate"]["accuracy"] - baseline["non_degenerate"]["accuracy"], 4
                )
            if len(nondeg_pairs) < args.min_slice_samples:
                failures.append(
                    f"G12 non-degenerate movement: only {len(nondeg_pairs)} paired decisions in "
                    f"attack:proper_subset + block:partial_block < {args.min_slice_samples} — fail closed"
                )
            else:
                nondeg_bootstrap = paired_bootstrap_ci(
                    nondeg_pairs, n_bootstraps=args.bootstraps, seed=args.seed
                )
                nondeg_bootstrap["paired_n"] = len(nondeg_pairs)
                nondeg_bootstrap["point_accuracy_delta"] = nondeg_delta
                ci = nondeg_bootstrap.get("raw_accuracy_delta_95ci")
                if not ci:
                    failures.append("G12 non-degenerate movement: no CI — fail closed")
                elif ci[0] <= args.min_nondegenerate_delta_ci_lower:
                    failures.append(
                        f"G12 non-degenerate movement: accuracy delta 95% CI [{ci[0]:+.4f}, "
                        f"{ci[1]:+.4f}] does not exclude {args.min_nondegenerate_delta_ci_lower} "
                        f"(point delta {nondeg_delta:+.4f} on n={len(nondeg_pairs)}) vs baseline "
                        f"{args.baseline_label!r} — no demonstrated improvement on the decisions "
                        "that are not 'everyone' or 'nobody'"
                    )

            # ---- solver diagnostic (reported, NEVER gated) ---------------------
            agree = candidate["by_solver_agreement"].get("True", {})
            disagree = candidate["by_solver_agreement"].get("False", {})
            if agree.get("accuracy") is not None and disagree.get("accuracy") is not None:
                advisories.append(
                    f"solver diagnostic (NOT gated): accuracy {agree['accuracy']:.4f} on the "
                    f"{agree['n']} decisions where the solver agreed with the human vs "
                    f"{disagree['accuracy']:.4f} on the {disagree['n']} where it did not "
                    f"(delta {agree['accuracy'] - disagree['accuracy']:+.4f}). A large positive "
                    "delta means the model is following the solver's kind of reasoning; a small "
                    "one means it is following humans. Neither is a pass condition."
                )

            # ---- optional EXTRA floor (tightens only) ---------------------------
            if args.min_accuracy is not None and cand_acc is not None and cand_acc < args.min_accuracy:
                failures.append(f"accuracy {cand_acc:.4f} < --min-accuracy {args.min_accuracy}")

        if not failures:
            verdict = "PASS"
    except Exception as e:  # any evaluation error blocks promotion
        logger.exception("gate exception")
        failures.append(f"gate exception: {type(e).__name__}: {e}")
        verdict = "BLOCKED"

    def _strip(metrics: dict) -> dict:
        return {k: v for k, v in metrics.items() if k != "per_id"}

    report = {
        "gate": "combat_decisions",
        "verdict": verdict,
        "failures": failures,
        "advisories": advisories,
        "criteria": criteria,
        "corpus_composition": composition,
        "reference_policies": references,
        "slice_reflex_floors": floors,
        "candidate": _strip(candidate),
        "candidate_permuted": _strip(permuted),
        "baseline": _strip(baseline),
        "permutation_invariance": perm_metrics,
        "paired_bootstrap": bootstrap,
        "paired_bootstrap_non_degenerate": nondeg_bootstrap,
        "thresholds": {
            "min_samples": args.min_samples,
            "min_attack_samples": args.min_attack_samples,
            "min_block_samples": args.min_block_samples,
            "min_slice_samples": args.min_slice_samples,
            "min_rank_slice_samples": args.min_rank_slice_samples,
            "min_schema_rate": args.min_schema_rate,
            "max_illegal_violations": MAX_ILLEGAL_VIOLATIONS,
            "max_permutation_gap": args.max_permutation_gap,
            "max_accuracy_regression": args.max_accuracy_regression,
            "min_trivial_margin": args.min_trivial_margin,
            "min_nondegenerate_delta_ci_lower": args.min_nondegenerate_delta_ci_lower,
            "min_accuracy": args.min_accuracy,
            "non_gated_answer_classes": sorted(NON_GATED_ANSWER_CLASSES),
            "clamped_cli_overrides": clamp_notes,
        },
        "inputs": {
            "corpus": str(args.corpus),
            "permuted_corpus": str(args.permuted_corpus),
            "responses": str(args.responses),
            "permuted_responses": str(args.permuted_responses),
            "baseline_responses": str(args.baseline_responses),
            "backend_label": args.backend_label,
            "baseline_label": args.baseline_label,
        },
        "honesty_note": (
            "The answer here is a SUBSET, so the reflexes to beat are the degenerate answers: "
            f"{(composition.get('within_block_share') or {}).get('no_block', 0):.1%} of this "
            "corpus's block decisions are 'no block' and "
            f"{(composition.get('within_attack_share') or {}).get('no_attack', 0):.1%} / "
            f"{(composition.get('within_attack_share') or {}).get('all_in', 0):.1%} of attacks are "
            "'nobody' / 'everybody'. G6-G11 floor the candidate at the best realized reflex overall, "
            "per family, on the two classes reflexes cannot reach, and per rank; G12 requires the "
            "non-degenerate classes themselves to move against the baseline. Jaccard is diagnostic "
            "only — a high Jaccard with a low exact accuracy means the model is losing one creature "
            "per combat, which is how Limited games are lost. Solver agreement is reported and NOT "
            "gated: humans at diamond/mythic are not optimal and the solver sees no tricks, so "
            "gating either direction would encode a claim this corpus cannot support."
        ),
        "git_sha": _git_sha(),
        "ts": time.time(),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    logger.info(f"gate verdict: {verdict}; report -> {args.report}")
    for msg in failures:
        logger.error(f"  gate failure: {msg}")

    if args.register:
        # There is no promotion path in this file. The flag exists so a caller
        # that expects one gets an explicit refusal instead of a silent no-op.
        logger.error(
            "Refusing to register: this gate never registers or promotes a model. "
            "Use the model registry explicitly after reading the report."
        )
        return 2

    return 0 if verdict == "PASS" else 2


# --------------------------------------------------------------------------
# Subcommand: dryrun (simulate + gate, corpora loaded once)
# --------------------------------------------------------------------------


def cmd_dryrun(args: argparse.Namespace) -> int:
    if not args.corpus.exists() or not args.permuted_corpus.exists():
        logger.error("dryrun needs both --corpus and --permuted-corpus")
        return 2
    corpus = load_corpus(args.corpus, limit=args.limit)
    permuted = load_corpus(args.permuted_corpus, limit=args.limit)
    identity_gold = {r.rid: _gold_positions(r) for r in corpus}
    references = reference_policies(corpus, seed=args.seed)
    floors = _slice_floors(corpus, seed=args.seed)

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    # Synthetic response files are deterministic in (seed, policy) and run to
    # ~3MB each; the committed fixture is the REPORT, so the intermediates are
    # removed unless the caller asks to inspect them.
    scratch: list[Path] = []
    base_a, base_b = DRYRUN_POLICIES[args.baseline_policy]
    base_rows = simulate_rows(corpus, base_a, base_b, label="baseline", seed=args.seed)
    base_path = out_dir / f"{DRYRUN_STEM}_responses_baseline.jsonl"
    _write_jsonl(base_path, base_rows)
    scratch.append(base_path)

    verdicts: dict[str, dict] = {}
    for policy in args.policy:
        atk, blk = DRYRUN_POLICIES[policy]
        cand_path = out_dir / f"{DRYRUN_STEM}_responses_{policy}.jsonl"
        perm_path = out_dir / f"{DRYRUN_STEM}_responses_{policy}_permuted.jsonl"
        _write_jsonl(
            cand_path,
            simulate_rows(corpus, atk, blk, label="candidate", seed=args.seed, identity_gold=identity_gold),
        )
        _write_jsonl(
            perm_path,
            simulate_rows(permuted, atk, blk, label="candidate", seed=args.seed, identity_gold=identity_gold),
        )
        scratch.extend([cand_path, perm_path])
        report_path = out_dir / f"{DRYRUN_STEM}_{policy}.json"
        gate_args = argparse.Namespace(
            corpus=args.corpus,
            permuted_corpus=args.permuted_corpus,
            responses=cand_path,
            permuted_responses=perm_path,
            baseline_responses=base_path,
            backend_label="candidate",
            baseline_label="baseline",
            report=report_path,
            min_samples=MIN_SAMPLES,
            min_attack_samples=MIN_ATTACK_SAMPLES,
            min_block_samples=MIN_BLOCK_SAMPLES,
            min_slice_samples=MIN_GATED_SLICE_SAMPLES,
            min_rank_slice_samples=MIN_RANK_SLICE_SAMPLES,
            min_schema_rate=MIN_SCHEMA_RATE,
            max_permutation_gap=MAX_PERMUTATION_ACC_GAP,
            max_accuracy_regression=MAX_ACCURACY_REGRESSION,
            min_trivial_margin=MIN_TRIVIAL_POLICY_MARGIN,
            min_nondegenerate_delta_ci_lower=MIN_NONDEGENERATE_DELTA_CI_LOWER,
            min_accuracy=None,
            bootstraps=args.bootstraps,
            seed=args.seed,
            register=False,
            preloaded_corpus=corpus,
            preloaded_permuted=permuted,
            preloaded_references=references,
            preloaded_floors=floors,
        )
        code = cmd_gate(gate_args)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        verdicts[policy] = {
            "verdict": report["verdict"],
            "exit_code": code,
            "failures": report["failures"],
            "accuracy": report["candidate"].get("overall", {}).get("accuracy"),
            "report": str(report_path),
        }
        logger.info(f"dryrun {policy}: {report['verdict']} ({len(report['failures'])} failures)")

    if not args.keep_responses:
        for path in scratch:
            path.unlink(missing_ok=True)

    summary = {
        "corpus": str(args.corpus),
        "baseline_policy": args.baseline_policy,
        "reference_policies": references,
        "slice_reflex_floors": {k: v.get("best") for k, v in sorted(floors.items()) if v.get("n")},
        "verdicts": verdicts,
    }
    summary_path = out_dir / f"{DRYRUN_STEM}_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v["verdict"] for k, v in verdicts.items()}, indent=2))
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("calibrate", help="score every reflex policy on a corpus (run this first)")
    c.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    c.add_argument("--out", type=Path, default=None)
    c.add_argument("--limit", type=int, default=None)
    c.add_argument("--seed", type=int, default=DEFAULT_SEED)
    c.set_defaults(func=cmd_calibrate)

    s = sub.add_parser("simulate", help="write synthetic-policy responses (harness proof, no model)")
    s.add_argument("--corpus", type=Path, required=True)
    s.add_argument(
        "--identity-corpus",
        type=Path,
        default=None,
        help="identity corpus, so the 'memorise' policy can answer with the identity gold positions",
    )
    s.add_argument("--attack-policy", choices=ATTACK_POLICIES, default="gold")
    s.add_argument("--block-policy", choices=BLOCK_POLICIES, default="gold")
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--backend-label", default="")
    s.add_argument("--limit", type=int, default=None)
    s.add_argument("--seed", type=int, default=DEFAULT_SEED)
    s.set_defaults(func=cmd_simulate)

    d = sub.add_parser("dryrun", help="simulate + gate a set of synthetic policies end to end")
    d.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    d.add_argument("--permuted-corpus", type=Path, default=DEFAULT_PERMUTED_CORPUS)
    d.add_argument("--policy", action="append", choices=sorted(DRYRUN_POLICIES), default=None)
    d.add_argument("--baseline-policy", choices=sorted(DRYRUN_POLICIES), default="do_nothing")
    d.add_argument("--out-dir", type=Path, default=DEFAULT_DATA_DIR)
    d.add_argument(
        "--keep-responses",
        action="store_true",
        help="keep the synthetic response files (~3MB each); by default only the reports survive",
    )
    d.add_argument("--limit", type=int, default=None)
    d.add_argument("--bootstraps", type=int, default=2000)
    d.add_argument("--seed", type=int, default=DEFAULT_SEED)
    d.set_defaults(func=cmd_dryrun)

    g = sub.add_parser("gate", help="score a candidate; PASS (0) or BLOCKED (2)")
    g.add_argument("--corpus", type=Path, required=True)
    g.add_argument("--permuted-corpus", type=Path, required=True)
    g.add_argument("--responses", type=Path, required=True)
    g.add_argument("--permuted-responses", type=Path, required=True)
    g.add_argument("--baseline-responses", type=Path, required=True)
    g.add_argument("--backend-label", required=True)
    g.add_argument("--baseline-label", required=True)
    g.add_argument("--report", type=Path, required=True)
    # Every threshold below is clamped so it can only be made STRICTER — see
    # _clamp_thresholds. There is no loosening flag.
    g.add_argument("--min-samples", type=int, default=MIN_SAMPLES)
    g.add_argument("--min-attack-samples", type=int, default=MIN_ATTACK_SAMPLES)
    g.add_argument("--min-block-samples", type=int, default=MIN_BLOCK_SAMPLES)
    g.add_argument("--min-slice-samples", type=int, default=MIN_GATED_SLICE_SAMPLES)
    g.add_argument("--min-rank-slice-samples", type=int, default=MIN_RANK_SLICE_SAMPLES)
    g.add_argument("--min-schema-rate", type=float, default=MIN_SCHEMA_RATE)
    g.add_argument("--max-permutation-gap", type=float, default=MAX_PERMUTATION_ACC_GAP)
    g.add_argument("--max-accuracy-regression", type=float, default=MAX_ACCURACY_REGRESSION)
    g.add_argument(
        "--min-trivial-margin",
        type=float,
        default=MIN_TRIVIAL_POLICY_MARGIN,
        help="G6-G11: how far the candidate must exceed the best reflex (may only be raised)",
    )
    g.add_argument(
        "--min-nondegenerate-delta-ci-lower",
        type=float,
        default=MIN_NONDEGENERATE_DELTA_CI_LOWER,
        help="G12: floor for the non-degenerate paired-bootstrap delta CI lower bound (may only be raised)",
    )
    g.add_argument(
        "--min-accuracy",
        type=float,
        default=None,
        help="optional EXTRA floor on exact-set accuracy (tightens the gate; there is no loosening flag)",
    )
    g.add_argument("--bootstraps", type=int, default=DEFAULT_BOOTSTRAPS)
    g.add_argument("--seed", type=int, default=DEFAULT_SEED)
    g.add_argument(
        "--register",
        action="store_true",
        help="accepted and always REFUSED — this gate never registers or promotes anything",
    )
    g.set_defaults(
        func=cmd_gate,
        preloaded_corpus=None,
        preloaded_permuted=None,
        preloaded_references=None,
        preloaded_floors=None,
    )

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s | %(message)s",
        stream=sys.stderr,
    )
    if getattr(args, "command", None) == "dryrun" and not args.policy:
        args.policy = ["oracle", "no_block", "all_in", "illegal", "memoriser"]
    try:
        return args.func(args)
    except Exception as e:  # fail closed at the top level too
        logger.exception("fatal error")
        print(f"BLOCKED: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
