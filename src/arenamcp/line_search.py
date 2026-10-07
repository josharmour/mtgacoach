"""Bounded, deterministic multi-turn line search over a ``BoardModel``.

The board assessment used to project one greedy line (``_schedule`` plus
``_project``) that never modelled our attacks, the opponent's life, which land
to play or a modal card's mode. This module compares candidate lines instead.
A line is our turn T (land, casts and modes, attack), their attack, our turn
T+1, their attack, a greedy T+2 and their third attack. T is expanded
exhaustively until the soft deadline (one child per action key first), T+1
with a beam (``groups`` T steps, the top ``k_next`` cast sets, ``width``
continuations kept), T+2 greedily. Today's greedy line (no attacks) always runs
as the pinned baseline: another line replaces it only when it is better in
outcome class or death/win timing, or by at least ``LINE_TOL`` in value.

The opponent (``line_search_moves.ParanoidOpponent``) is paranoid but
uninventive: if ANY of its attack policies kills us the line is dead there (a
lethal attack is never filtered out); otherwise it takes the non-suicidal one
worst for us. It deploys nothing, draws nothing and casts no tricks (v1); an
unparsed or 'other' mode of a modal card is worth 0; no unknown card is ever
invented. ``proxy_outcome`` replays a line with +2/+0 on their biggest attacker
as a robustness check.

Combat goes through ``board_assessment._combat`` (the combat solver's best
blocks), so flying/reach, menace, trample, deathtouch and "can't be blocked"
are the solver's, and so are its known gaps: lifelink is ignored, first strike
counts on both sides, double-strike blockers and marked damage are not
modelled.

Scores are (class, timing, V): WIN 2 (sooner is better), ALIVE 1, DEAD 0
(later is better), then V = u(life) - 0.6 opp life + material + 0.3 hand + a
race term on the top leaves. The constants are hand-tuned (no learned model).

Pure and deterministic: no I/O, no LLM, nothing logged above DEBUG; it reads
only planner-snapshot facts. The soft limit is a deterministic work budget
(``budget``: combat solves plus node expansions, ``line_search_moves.WORK_BUDGET``):
past it the remaining T steps are finished by greedy rollout (``bounded``),
so the same board always gives the same lines. Only the hard cap is a clock
(``time.perf_counter`` before every node expansion): past ``hard_ms`` the best
line so far is returned, the baseline at worst (``truncated``: callers keep
today's greedy facts).
"""

from __future__ import annotations

import dataclasses
import logging
import re
import time
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

from arenamcp.board_assessment import (
    TurnProjection,
    _body,
    _budget_turns,
    _cast_text,
    _controller,
    _enters_tapped,
    _int,
    _is_creature,
    _kills,
    _line_search_enabled,
    _name,
    _reach_or_flying,
    _schedule,
    _simulate_attacks,
    _source_card,
    _text,
    _threats,
    _types,
    face_damage,
    life_loss,
    search_hints,
)
from arenamcp.board_model import BoardModel, build_board_model
from arenamcp.line_search_moves import (
    ALIVE,
    DEAD,
    LABELS,
    NOTHING,
    WIN,
    Deadline,
    HandSpell,
    Moves,
    Node,
    OpponentModel,
    ParanoidOpponent,
    Step,
    Variant,
    bullets,
    classify,
    hand_info,
    ids_of,
    make_tokens,
    mode_label,
    token_specs,
    unmodelled_effect,
)
from arenamcp.mulligan_policy import _land_colors, _pip_matching

logger = logging.getLogger(__name__)

# The baseline (or its T step) stays the pick within this value band.
LINE_TOL = 1.5
# Attack vs hold posture needs this value gap (same class and timing).
POSTURE_MARGIN = 3.0
_RACE_HORIZON = 4
_RACE = 15.0
# compare_modes: instance ids of the tokens a forced mode makes before T.
_ROOT_TOKEN_IDS = 800_000_000

# ('land', (colors, enters_tapped)), ('cast', iid, mode), ('cycle', iid),
# ('nocast',), ('attack', frozenset(ids)), ('noattack',), ('block', variant).
# Cast and cycle ids are the lowest instance id of that card name in hand; a
# cast carries both its mode key and ('cast', iid, None).
ActionKey = tuple


@dataclass(frozen=True)
class Line:
    """Our next turns, their attacks between them, and the outcome."""

    steps: tuple[Step, ...]
    cls: int
    timing: int
    v: float
    dead_at: int | None = None  # their attack that kills us, counted like dead_in
    dead_turn: int | None = None
    win_at: int | None = None  # our attack that kills them (1 = T)
    win_turn: int | None = None
    now_life: int | None = None  # our life after their attack already under way
    block: str = ""  # root block variant when their attackers were declared
    baseline: bool = False
    complete: bool = True  # False: finished by greedy rollout past the soft deadline
    aliases: frozenset = frozenset()  # keys of other T steps reaching the same board
    _leaf: Any = field(default=None, repr=False, compare=False)

    @property
    def score(self) -> tuple[int, int, float]:
        return (self.cls, self.timing, self.v)

    @property
    def outcome(self) -> str:
        return {WIN: "win", ALIVE: "alive", DEAD: "dead"}[self.cls]

    @property
    def first_sig(self) -> tuple:
        """The root block variant and T step: lines with the same one start alike."""
        return (self.block, self.steps[0].sig if self.steps else None)

    @property
    def first_actions(self) -> frozenset:
        keys = set(self.steps[0].keys if self.steps else ()) | set(self.aliases)
        if self.block:
            keys.add(("block", self.block))
        return frozenset(keys)

    def lives(self) -> list[int]:
        """Our life after each of their attacks in this line, the one under way first."""
        lives = [self.now_life] if self.now_life is not None else []
        return lives + [s.life_after for s in self.steps if s.life_after is not None]

    def outcome_text(self) -> str:
        """'lethal on T15' / 'dead on T15' / 'survives (life 7, 5, 8; opponent at 17)'."""
        if self.cls == WIN:
            return f"lethal on T{self.win_turn}"
        if self.cls == DEAD:
            return f"dead on T{self.dead_turn}"
        lives = ", ".join(str(life) for life in self.lives())
        opp = next((s.opp_life_after for s in reversed(self.steps) if s.opp_life_after is not None), None)
        return f"survives (life {lives}" + (f"; opponent at {opp})" if opp is not None else ")")

    def summary(self, max_chars: int = 110) -> str:
        """'<T step>; then <T+1 step> — <outcome>', short enough for narration."""
        outcome = self.outcome_text()
        if not self.steps:
            head = "no blocks" if self.block == "none" else "their attack now"
        else:
            head = self.steps[0].text()
            if len(self.steps) > 1 and (self.steps[1].casts or self.steps[1].attack or self.steps[1].cycles):
                head += f"; then {self.steps[1].text()}"
        room = max_chars - len(outcome) - 3
        if len(head) > room:
            head = head[: max(0, room - 1)].rstrip(" ,;+") + "…"
        return f"{head} — {outcome}"

    def as_payload(self) -> dict[str, Any]:
        return {
            "summary": self.summary(),
            "outcome": self.outcome,
            "dead_at": self.dead_at,
            "win_at": self.win_at,
            "now_life": self.now_life,
            "value": round(float(self.v), 2),
            "steps": [
                {
                    "turn": s.turn,
                    "label": s.label,
                    "land": s.land,
                    "casts": list(s.casts),
                    "modes": dict(s.modes),
                    "cycles": list(s.cycles),
                    "attack": list(s.attack),
                    "held": list(s.held),
                    "life_after": s.life_after,
                    "opp_life_after": s.opp_life_after,
                }
                for s in self.steps
            ],
        }


def _unmodelled_mark(line: Line, unmodelled: Collection[str]) -> str:
    """' (X not modelled)' for the casts of ``line`` named in ``unmodelled``, else ''."""
    names = sorted({name for step in line.steps for name in step.casts if name in unmodelled})
    return f" ({', '.join(names)} not modelled)" if names else ""


def _order(line: Line) -> tuple:
    """Sort key, higher is better: the score, then a stable text tiebreak."""
    return (line.cls, line.timing, line.v, tuple(-ord(c) for c in line.summary()))


@dataclass
class LineSearchResult:
    """The search's lines and the facts the strategic layer derives from them."""

    best: Line
    baseline: Line
    lines: list[Line]  # top 5 with distinct T steps, best first
    first_action: dict  # ActionKey -> best line whose T step has it
    now_life: int | None
    dead_now: bool
    dead_in: int | None  # the best line's dead_at (their attacks until we die)
    dead_in_greedy: int | None  # the baseline's, through the same transition
    all_dead_at_first: bool  # every T step dies to their very next attack
    exact_first_attack: bool  # root complete, every first-attack block search exhaustive
    only_survivor: bool  # exactly one T step survives (exact root) while others die
    only_first_attack_survivor: bool
    posture: str  # lethal / attack / hold / either
    posture_reason: str
    nodes: int
    combats: int
    elapsed_ms: float
    bounded: bool
    truncated: bool
    model: BoardModel = field(repr=False, compare=False)
    _search: Any = field(default=None, repr=False, compare=False)
    # The greedy line dies but the best line survives, and it is not the only survivor.
    greedy_dies: bool = False

    def stats(self) -> dict[str, Any]:
        return {
            "nodes": self.nodes,
            "combats": self.combats,
            "ms": round(self.elapsed_ms, 1),
            "bounded": self.bounded,
            "truncated": self.truncated,
        }

    def prompt_line(
        self, max_chars: int = 320, *, unmodelled: Collection[str] = (), pending: Collection[str] = ()
    ) -> str:
        """One 'LINES ...' line for a per-decision prompt.

        ``unmodelled`` (card names): a line casting one of them is marked
        "(X not modelled)", its outcome undervalues that cast. ``pending`` (our
        stack objects, ``board_assessment._our_pending``): the lines read the
        board before they resolve, which the line says.
        """
        head = "LINES (2-turn search, greedy 3rd; their new cards/tricks not modelled): "
        parts = [f"best {self.best.summary()}{_unmodelled_mark(self.best, unmodelled)}"]
        parts += [
            f"alt {line.summary(90)}{_unmodelled_mark(line, unmodelled)}"
            for line in self.lines
            if line.first_sig != self.best.first_sig
        ][:2]
        if self.baseline.first_sig != self.best.first_sig:
            parts.append(f"greedy {self.baseline.outcome_text()}")
        tail = f" — before our pending {', '.join(pending)} resolves" if pending else ""
        text = head + " | ".join(parts) + tail
        while len(text) > max_chars and len(parts) > 1:
            parts.pop()
            text = head + " | ".join(parts) + tail
        return text if len(text) <= max_chars else text[: max_chars - 1] + "…"

    def to_projections(self) -> list[TurnProjection]:
        """The best line as the assessment's lookahead rows (T, T+1, T+2)."""
        steps = list(self.best.steps)
        if self._search is not None and self.best._leaf is not None and len(steps) < len(LABELS):
            steps += self._search.fill_steps(self.best._leaf, len(steps), len(LABELS) - len(steps))
        names = {f.name for f in dataclasses.fields(TurnProjection)}
        rows = []
        for index, step in enumerate(steps[: len(LABELS)]):
            values = {
                "label": step.label,
                "turn": step.turn,
                "mana": step.mana,
                "colors": step.colors,
                "land": step.land,
                "casts": list(step.casts),
                "castable": list(step.castable),
                "life_after": step.life_after,
                "opponent_creatures_after": step.their_creatures_after,
                "source_colors": list(step.source_colors),
                # Defaulted fields the assessment adds for the line search.
                "attack": list(step.attack),
                "opp_life_after": step.opp_life_after,
                "modes": dict(step.modes),
                "held": ", ".join(step.held),
                "posture": self.posture if index == 0 else "",
            }
            rows.append(TurnProjection(**{k: v for k, v in values.items() if k in names}))
        return rows


@dataclass
class PlanEvaluation:
    line: Line | None
    budgets: list[dict]  # [{turn, label, mana, colors, source_colors}] for the planned turns
    issues: list[str]
    # Planned casts whose effect (or noted mode) the search does not model, as
    # "T: Elemental Uprising (its tokens)" / "T: Archive Arbiter (choose: destroy …): mode
    # not modelled": the replayed line undervalues them, so it is no verdict on the plan.
    unmodelled: list[str] = field(default_factory=list)


@dataclass
class ModeComparison:
    source: str
    lines: dict[str, Line]  # option_id -> best line with that mode
    results: dict[str, LineSearchResult]
    modes: dict[str, str]  # option_id -> 'gain 4 life' / removal text / 'other'
    contingent: list[str]  # what an unmodelled 'other' mode might answer

    @property
    def complete(self) -> bool:
        """Every mode got a line and no search hit its hard deadline."""
        return len(self.lines) == len(self.modes) and not any(r.truncated for r in self.results.values())


@dataclass(eq=False)
class _Group:
    """One T step (after any root block variant) and what the search learned past it."""

    order: int
    node: Node  # after the T step and their attack
    keys: frozenset
    text: str
    attacks: bool  # the T step attacks
    aliases: set = field(default_factory=set)
    leaves: list = field(default_factory=list)
    line: Line | None = None


class _Search(Moves):
    def __init__(self, model: BoardModel, *, groups: int, width: int, k_next: int, **kwargs: Any) -> None:
        super().__init__(model, **kwargs)
        self.groups, self.width, self.k_next = max(1, groups), max(1, width), max(0, k_next)
        self._race_memo: dict = {}

    def baseline(self, root: Node) -> Node:
        """Today's greedy line (``_budget_turns`` + ``_schedule``, no attacks) through this transition."""
        model = self.model
        budgets = _budget_turns(
            model.our_turn, model.turn, list(model.sources_now), list(model.sources_all),
            list(model.hand_lands), model.land_drop_now and not model.t_instant_only,
        )  # fmt: skip
        # In the search's own (name) order, so the pick never depends on the hand's order.
        copies = [self.spells[i].spell for i in self.hand0]
        schedule = _schedule(
            copies, budgets, survival=self.survival, theirs=list(model.theirs), ours=list(model.ours),
            instant_only_first=self.our_turn and model.t_instant_only,
        )  # fmt: skip
        index_of = {id(self.spells[i].spell): i for i in self.hand0}
        node = root
        for k, (budget, casts) in enumerate(zip(budgets, schedule, strict=False)):
            if node.terminal:
                break
            land = next((x for x in node.lands if x.name == budget.get("land")), None)
            if None in self.land_options(node):
                land = None
            choice = []
            life = node.our_life
            taken: set = set()  # as _project: each removal takes the best target the earlier ones left
            for spell in casts:
                hs = self.spells[index_of[id(spell)]]
                var = hs.plain
                if var is None or hs.index not in node.hand or var.loss >= life:
                    continue
                if k == 0 and self.our_turn and model.t_instant_only and not var.instant:
                    continue
                target = None
                if var.reach is not None:
                    killable = [
                        b
                        for b in node.theirs
                        if b["instance_id"] not in taken and self.kills(var.reach, b, node.ours)
                    ]
                    if killable:
                        target = max(killable, key=lambda b: (b["power"], b["toughness"]))
                        taken.add(target["instance_id"])
                    elif var.body is None:
                        continue  # nothing left to remove: kept in hand (the search holds it too)
                life -= var.loss
                choice.append((var, target))
            # Cast order as the search enumerates it (hand order): the same T step has the same sig.
            choice.sort(key=lambda item: node.hand.index(item[0].spell))
            mid = self.begin(node, land, tuple(choice))
            options, _possible = self.attack_choices(mid)
            node = self.finish(mid, options[0] if options[0][0] == "declared" else ("none", ()), greedy=True)
        return node

    def race(self, node: Node) -> float:
        """R: +15/our clock when it is no slower than theirs, else -15/their clock (horizon 4)."""
        key = (ids_of(node.ours), ids_of(node.theirs), node.their_tapped, node.our_life, node.opp_life)
        found = self._race_memo.get(key)
        if found is None:
            open_blockers = [b for b in node.theirs if b["instance_id"] not in node.their_tapped]
            ours, _ = _simulate_attacks(
                list(node.ours), list(node.theirs), node.opp_life,
                first_attackers=None, first_blockers=open_blockers, horizon=_RACE_HORIZON,
            )  # fmt: skip
            theirs, _ = _simulate_attacks(
                list(node.theirs), list(node.ours), node.our_life,
                first_attackers=None, first_blockers=None, horizon=_RACE_HORIZON,
            )  # fmt: skip
            if ours is not None and (theirs is None or ours <= theirs):
                found = _RACE / ours
            else:
                found = -_RACE / theirs if theirs is not None else 0.0
            self._race_memo[key] = found
        return found

    def line(self, node: Node, *, race: float = 0.0, baseline=False, aliases=frozenset()) -> Line:
        cls, timing, v = self.score(node)
        return Line(
            steps=tuple(node.history),
            cls=cls,
            timing=timing,
            v=float(v + race) if cls == ALIVE else float(v),
            dead_at=node.dead_at,
            dead_turn=node.dead_turn,
            win_at=node.win_at,
            win_turn=node.win_turn,
            now_life=node.now_life,
            block=node.block,
            baseline=baseline,
            complete=not node.rolled,
            aliases=frozenset(aliases),
            _leaf=node,
        )

    # -- driver -----------------------------------------------------------------------------

    def run(self) -> LineSearchResult:
        self.free = True  # their attack under way and the baseline always finish
        roots = self.roots()
        if roots[0][1].dead_at is not None:
            return self._dead_now(roots[0][1])
        base_leaf = self.baseline(roots[0][1])
        self.free = False
        groups: list[_Group] = []
        root_complete = False
        try:
            groups, root_complete = self._root_groups(roots)
            chosen, extra = self._pick(groups, base_leaf)
            self._expand(chosen, extra)
        except Deadline:
            self.truncated = True
        result = self._result(groups, base_leaf, root_complete)
        self.free = True  # later calls (to_projections) must never raise Deadline
        return result

    def _root_groups(self, roots) -> tuple[list[_Group], bool]:
        """Every T step after each root: one per action key first, then by spell value."""
        pairs = []
        for block, node in roots:
            extra = {("block", block)} if block else set()
            if node.terminal:
                pairs.append((-1e9, block, node, None, (), frozenset(extra)))
                continue
            instant_only = self.our_turn and self.model.t_instant_only
            for land in self.land_options(node):
                _base, sources = self.sources(node, land)
                for casts in self.cast_sets(node, sources, instant_only=instant_only):
                    keys = frozenset(self.play_keys(land, casts) | extra)
                    pairs.append((self.choice_value(casts, node.theirs), block, node, land, casts, keys))
        pairs.sort(key=lambda pair: (-round(pair[0], 6), self._pair_text(pair)))
        covered: set = set()
        first, rest = [], []
        for pair in pairs:  # coverage: the best pair for each key not yet seen goes first
            (first if not pair[5] <= covered else rest).append(pair)
            covered |= pair[5]
        groups: list[_Group] = []
        by_sig: dict = {}
        for order, pair in enumerate(first + rest):
            if order and self.past_soft():
                self.bounded = True
                return groups, False
            _value, _block, node, land, casts, keys = pair
            if node.terminal:
                children = [(keys, False, node)]
            else:
                mid = self.begin(node, land, casts)
                children = [
                    (
                        keys | {("attack", frozenset(ids)) if ids else ("noattack",)},
                        bool(ids),
                        self.finish(mid, (n, ids)),
                    )
                    for n, ids in self.attack_choices(mid)[0]
                ]
            for child_keys, attacks, child in children:
                sig = self._sig(child)
                if sig in by_sig:  # a transposition: same board, other T step
                    by_sig[sig].aliases |= child_keys
                    continue
                text = f"{self._pair_text(pair)}|{child.history[0].attack if child.history else ''}"
                by_sig[sig] = _Group(len(groups), child, frozenset(child_keys), text, attacks)
                groups.append(by_sig[sig])
        return groups, True

    def _pair_text(self, pair) -> str:
        _value, block, _node, land, casts, _keys = pair
        return f"{block}|{land.name if land else ''}|{self.choice_text(casts)}"

    @staticmethod
    def _sig(node: Node) -> tuple:
        return (
            node.k, node.our_life, node.opp_life, ids_of(node.ours), ids_of(node.theirs), node.our_tapped,
            node.their_tapped, node.entered, node.hand, tuple(x.iid for x in node.lands),
            tuple(sorted(s.name for s in node.played)), len(node.rocks), ids_of(node.returning),
            node.dead_at, node.win_at, node.overkill, node.block,
        )  # fmt: skip

    def _rank(self, group: _Group) -> tuple:
        cls, timing, v = self.score(group.node)
        return (-cls, -timing, -round(v, 6), group.text, group.order)

    def _pick(self, groups: list[_Group], base_leaf: Node) -> tuple[list[_Group], list[_Group]]:
        """Up to ``groups`` T steps: the baseline's, the best per action key, then by 1-ply score."""
        ordered: list[_Group] = []
        if base_leaf.history:
            sig = (base_leaf.block, base_leaf.history[0].sig)
            ordered += [g for g in groups if g.node.history and (g.node.block, g.node.history[0].sig) == sig][
                :1
            ]
        # The solver's own blocks first: the facts come from them (other block variants are advisory).
        main = [g for g in groups if g.node.block in ("", "best")]
        keyed: list[_Group] = []
        for subset in (main, [g for g in groups if g.node.block not in ("", "best")]):
            best_per_key: dict = {}
            for group in subset:
                for key in group.keys | group.aliases:
                    if key not in best_per_key or self._rank(group) < self._rank(best_per_key[key]):
                        best_per_key[key] = group
            keyed += sorted({id(g): g for g in best_per_key.values()}.values(), key=self._rank)
        for group in keyed + sorted(main, key=self._rank) + sorted(groups, key=self._rank):
            if group not in ordered:
                ordered.append(group)
        chosen = ordered[: self.groups]
        return chosen, [g for g in keyed if g not in chosen]  # keys past the cap: greedy rollout

    def _expand(self, chosen: list[_Group], extra: list[_Group]) -> None:
        for group in chosen:
            if group.node.terminal:
                group.leaves = [group.node]
            elif self.past_soft():
                self.bounded = True
                group.leaves = [dataclasses.replace(self.rollout(group.node, cheap=True), rolled=True)]
            else:
                kids = self._next(group.node)
                kids.sort(key=lambda item: (tuple(-x for x in self.score(item[1])), item[0]))
                group.leaves = [
                    self.rollout(kid, cheap=self.past_soft()) for _text, kid in kids[: self.width]
                ]
        for group in extra:
            leaf = group.node if group.node.terminal else self.rollout(group.node, cheap=self.past_soft())
            group.leaves = [dataclasses.replace(leaf, rolled=leaf is not group.node)]

    def _next(self, node: Node) -> list[tuple[str, Node]]:
        """T+1: land classes x (the top ``k_next`` cast sets + none) x attacks."""
        kids = []
        for land in self.land_options(node):
            _base, sources = self.sources(node, land)
            sets = self.cast_sets(node, sources, instant_only=False)
            for casts in [c for c in sets if c][: self.k_next] + [()]:
                mid = self.begin(node, land, casts)
                for attack in self.attack_choices(mid)[0]:
                    text = f"{land.name if land else ''}|{self.choice_text(casts)}|{attack[1]}"
                    kids.append((text, self.finish(mid, attack)))
        return kids

    def _result(self, groups: list[_Group], base_leaf: Node, root_complete: bool) -> LineSearchResult:
        expanded = [g for g in groups if g.leaves]
        alive = sorted(
            (
                leaf
                for leaf in [leaf for g in expanded for leaf in g.leaves] + [base_leaf]
                if not leaf.terminal
            ),
            key=lambda leaf: tuple(-x for x in self.score(leaf)),
        )
        races: dict[int, float] = {}
        if not self.truncated and not self.past_soft():
            # The race term needs two horizon-4 clocks per leaf: top leaves only.
            for leaf in alive[: 2 * self.width] + ([] if base_leaf.terminal else [base_leaf]):
                if self.past_soft():
                    self.bounded = True
                    break
                races[id(leaf)] = self.race(leaf)
        floor = min(races.values()) if races else 0.0  # leaves without R get the worst one seen
        baseline = self.line(base_leaf, race=races.get(id(base_leaf), floor), baseline=True)
        for group in expanded:
            group.line = max(
                (
                    self.line(leaf, race=races.get(id(leaf), floor), aliases=group.aliases)
                    for leaf in group.leaves
                ),
                key=_order,
            )
        ranked = sorted((g.line for g in expanded), key=_order, reverse=True)
        # Block variants are advisory: the facts come from the solver's own blocks.
        main = [g for g in groups if g.node.block in ("", "best")]
        best = self._pick_best([line for line in ranked if line.block in ("", "best")], baseline)
        lines, seen = [best], {best.first_sig}
        for line in ranked + [baseline]:
            if len(lines) < 5 and line.first_sig not in seen:
                seen.add(line.first_sig)
                lines.append(line)
        first_action: dict = {}
        for line in [baseline] + ranked:
            for key in line.first_actions:
                if key not in first_action or line.score > first_action[key].score:
                    first_action[key] = line
        exact_root = root_complete and not self.truncated
        # A T step's node has seen their first attack after T (or died to the one under way).
        dead_first = [g for g in main if g.node.dead_at is not None]
        survive_first = {self._content(g) for g in main if g.node.dead_at is None}
        # Each T step's best line, or its 1-ply node when the beam never expanded it.
        lives = {id(g): (g.line.cls != DEAD) if g.leaves else g.node.dead_at is None for g in main}
        survivors = {self._content(g) for g in main if lives[id(g)]}
        some_die = not all(lives.values())
        only = best.cls != DEAD and exact_root and some_die and len(survivors) == 1
        posture, reason = self._posture([g for g in main if g.leaves], best)
        return LineSearchResult(
            best=best,
            baseline=baseline,
            lines=lines,
            first_action=first_action,
            now_life=best.now_life,
            dead_now=False,
            dead_in=best.dead_at,
            dead_in_greedy=baseline.dead_at,
            all_dead_at_first=exact_root and bool(main) and all(g.node.dead_at == 1 for g in main),
            exact_first_attack=exact_root and self.exact_first,
            only_survivor=only,
            greedy_dies=best.cls != DEAD and baseline.cls == DEAD and not only,
            only_first_attack_survivor=exact_root and len(survive_first) == 1 and bool(dead_first),
            posture=posture,
            posture_reason=reason,
            nodes=self.nodes,
            combats=self.combats,
            elapsed_ms=(time.perf_counter() - self.started) * 1000,
            bounded=self.bounded,
            truncated=self.truncated,
            model=self.model,
            _search=self,
        )

    @staticmethod
    def _pick_best(ranked: list[Line], baseline: Line) -> Line:
        """The top line, unless it edges out the baseline's T step (then the baseline) by < LINE_TOL.

        The greedy line is pinned: another T step must beat the best line
        starting with the baseline's T step in outcome class or timing, or by
        LINE_TOL in value; that line in turn must beat the baseline the same
        way. Close calls (G1_T8: Theorix Metamage or Tetsuko first) keep
        today's pick.
        """

        def close(a: Line, b: Line) -> bool:
            return (a.cls, a.timing) == (b.cls, b.timing) and a.v - b.v < LINE_TOL

        top = ranked[0] if ranked else baseline
        anchor = max(
            [baseline] + [line for line in ranked if line.first_sig == baseline.first_sig], key=_order
        )
        if top.first_sig != anchor.first_sig and close(top, anchor):
            top = anchor
        return (
            baseline
            if (top.cls, top.timing) < (baseline.cls, baseline.timing) or close(top, baseline)
            else top
        )

    @staticmethod
    def _content(group: _Group) -> tuple:
        """A T step's plays without its land drop or attack: lines that differ only in those count
        as one (which land and whether to attack are the posture's and the lookahead's facts)."""
        return tuple(
            sorted(repr(k) for k in group.keys | group.aliases if k[0] not in ("land", "attack", "noattack"))
        )

    def _posture(self, groups: list[_Group], best: Line) -> tuple[str, str]:
        if self.lethal_now or (best.cls == WIN and best.win_at == 1):
            who = ", ".join(best.steps[0].attack) if best.steps else ""
            return "lethal", f"attack{f' with {who}' if who else ''} for lethal"
        attacking = [g.line for g in groups if g.attacks]
        holding = [g.line for g in groups if not g.attacks]
        if not attacking or not holding:
            return "either", "" if attacking else "no attack available"
        top, hold = max(attacking, key=_order), max(holding, key=_order)
        if (top.cls, top.timing) != (hold.cls, hold.timing):
            posture = "attack" if (top.cls, top.timing) > (hold.cls, hold.timing) else "hold"
        elif abs(top.v - hold.v) >= POSTURE_MARGIN:
            posture = "attack" if top.v > hold.v else "hold"
        else:
            return "either", "attacking and holding end alike"
        return (
            posture,
            f"attack with {', '.join(top.steps[0].attack)}: {top.outcome_text()}; hold: {hold.outcome_text()}",
        )

    def _dead_now(self, node: Node) -> LineSearchResult:
        line = self.line(node, baseline=True)
        return LineSearchResult(
            best=line,
            baseline=line,
            lines=[],
            first_action={},
            now_life=node.now_life,
            dead_now=True,
            dead_in=1,
            dead_in_greedy=1,
            all_dead_at_first=True,
            exact_first_attack=True,
            only_survivor=False,
            only_first_attack_survivor=False,
            posture="either",
            posture_reason="their attack under way is lethal",
            nodes=self.nodes,
            combats=self.combats,
            elapsed_ms=(time.perf_counter() - self.started) * 1000,
            bounded=False,
            truncated=False,
            model=self.model,
            _search=self,
        )


# --- public API ----------------------------------------------------------------------


def search_lines(
    model: BoardModel,
    *,
    survival: bool,
    lethal_now: bool,
    soft_ms: float | None = None,
    hard_ms: float = 350.0,
    groups: int = 16,
    width: int = 2,
    k_next: int = 4,
    opponent: OpponentModel | None = None,
    budget: int | None = None,
) -> LineSearchResult:
    """Search our lines over T, T+1 (and a greedy T+2) against the paranoid opponent.

    ``budget`` (work units, default ``WORK_BUDGET``) is the deterministic soft
    limit; ``soft_ms`` is its legacy form (120 ms = the default budget).
    """
    search = _Search(
        model, survival=survival, lethal_now=lethal_now, soft_ms=soft_ms, hard_ms=hard_ms, groups=groups,
        width=width, k_next=k_next, opponent=opponent, budget=budget,
    )  # fmt: skip
    result = search.run()
    logger.debug(
        "line search: %s (nodes=%d, combats=%d, %.1f ms%s%s)", result.best.summary(), result.nodes,
        result.combats, result.elapsed_ms, ", bounded" if result.bounded else "",
        ", truncated" if result.truncated else "",
    )  # fmt: skip
    return result


def _hand_key(card: dict, state: dict) -> int:
    ids = [
        _int(c.get("instance_id")) or 0
        for c in state.get("hand") or []
        if isinstance(c, dict) and c.get("name") == card.get("name")
    ]
    return min(ids) if ids else _int(card.get("instance_id")) or 0


def action_key(option: Any, state: dict) -> ActionKey | None:
    """The search's key for one ActionsAvailable option; None for pass and the unmodelled."""
    meta = getattr(option, "meta", None) or {}
    action = str(meta.get("actionType") or "").removeprefix("ActionType_").lower()
    if getattr(option, "option_id", "") == "pass" or action == "pass":
        return None
    source, zone = _source_card(state, meta)
    if not source:
        return None
    if action in ("play", "playland"):
        return ("land", (frozenset(_land_colors(source)), _enters_tapped(source)))
    if zone != "hand":
        return None
    if action == "cast":
        return ("cast", _hand_key(source, state), None)
    label = str(getattr(option, "label", "") or "").lower()
    if action == "activate" and "cycling" in label and hand_info(source).landcycling is not None:
        return ("cycle", _hand_key(source, state))
    return None


def _sibling(result: LineSearchResult, **kwargs: Any) -> _Search:
    """A fresh search on ``result``'s board with the same settings, free of deadlines."""
    base = result._search
    search = _Search(
        result.model, survival=base.survival, lethal_now=base.lethal_now, budget=10**9, hard_ms=1e9,
        groups=base.groups, width=base.width, k_next=base.k_next, opponent=base.opponent,
        root_effect=base.root_effect, **kwargs,
    )  # fmt: skip
    search.free = True
    return search


def _root(search: _Search, block: str = "") -> Node:
    roots = dict(search.roots())
    return roots.get(block) or roots.get("best") or next(iter(roots.values()))


def proxy_outcome(result: LineSearchResult | None, line: Line | None) -> Line | None:
    """``line``'s own choices replayed with +2/+0 on their biggest attacker each attack.

    The boost applies only while their hand may hold a trick (hand >= 1 or
    unknown); later turns the line never reached play greedily. None when no
    replay is possible.
    """
    if result is None or line is None or result._search is None:
        return None
    try:
        search = _sibling(result, proxy=True)
        node = _root(search, line.block)
        for step in line.steps:
            if node.terminal:
                break
            land, casts, ids = step.plays
            land = next((x for x in node.lands if land is not None and x.iid == land.iid), None)
            playable = []
            for var, target in casts:
                if var.spell not in node.hand:
                    continue
                if target is not None:
                    same = [b for b in node.theirs if b["instance_id"] == target["instance_id"]]
                    target = (
                        same or search.targets(var.reach, node.theirs, node.their_tapped, node.ours) or [None]
                    )[0]
                    if target is None and var.body is None:
                        continue  # a creature still enters when its trigger finds no target
                playable.append((var, target))
            mid = search.begin(node, land, tuple(playable))
            allowed = {i for _n, option in search.attack_choices(mid)[0] for i in option}
            node = search.finish(mid, ("replay", tuple(i for i in ids if i in allowed)))
        return search.line(search.rollout(node))
    except Exception:  # never let the robustness check break a decision
        logger.debug("proxy replay failed", exc_info=True)
        return None


def _plan_index(raw: dict, position: int, first_turn: int) -> int:
    label = str(raw.get("label") or raw.get("turn") or "").replace(" ", "").upper()
    if label in LABELS:
        return LABELS.index(label)
    turn = _int(raw.get("turn"))
    if turn is not None and turn >= first_turn and (turn - first_turn) % 2 == 0:
        return (turn - first_turn) // 2
    return position


def _plain(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def evaluate_plan(
    result: LineSearchResult | None, steps: list[dict], state: dict | None = None
) -> PlanEvaluation:
    """A game plan's turns replayed through the search: its line, each turn's mana and the issues.

    Names map to hand instances, first unused copy first. A cast may carry a
    note, as the plan's own text writes it: "Archive Arbiter (choose: gain 4
    life)", "Archive Arbiter (gain 4 life)" (CANDIDATE LINES), "Shock (on
    Cadet)", "Shock (choose: …, on opponent)"; or a step may map card names to
    modes in ``modes``. A noted mode is replayed (matched against the mode's
    label or its text); without one, the mode worth most on that board. A
    noted target is preferred when the cast can hit it. Issues: a card not in
    hand, a cast the line's own mana (its land drops and rocks) can't pay, an
    attacker that is summoning-sick. ``unmodelled`` lists the planned casts
    whose effect or noted mode the search can't value (``unmodelled_effect``).
    Turns the plan leaves out play greedily. ``state`` is accepted for
    symmetry; the result's board is used.
    """
    if result is None or result._search is None:
        return PlanEvaluation(None, [], [])
    try:
        return _evaluate_plan(result, steps)
    except Exception:  # never let plan checking break the plan
        logger.debug("plan evaluation failed", exc_info=True)
        return PlanEvaluation(None, [], [])


def _evaluate_plan(result: LineSearchResult, steps: list[dict]) -> PlanEvaluation:
    search = _sibling(result)
    planned: dict[int, dict] = {}
    for position, raw in enumerate(steps[: len(LABELS)]):
        if isinstance(raw, dict):
            planned.setdefault(_plan_index(raw, position, search.first_turn), raw)
    node = _root(search)
    budgets: list[dict] = []
    issues: list[str] = []
    unmodelled: list[str] = []
    while not node.terminal and node.k < len(LABELS):
        raw = planned.get(node.k)
        if raw is None:
            node = search.greedy_step(node)
            continue
        label = LABELS[node.k]
        land = None
        if _plain(raw.get("land")):
            land = next((x for x in node.lands if _plain(x.name) == _plain(raw.get("land"))), None)
            if land is None or None in search.land_options(node):
                issues.append(f"{label}: land {raw.get('land')} is not playable from hand")
                land = None
        _base, sources = search.sources(node, land)
        casts: list = []
        hand = list(node.hand)
        names = raw.get("cast") or []
        noted_modes = raw.get("modes") if isinstance(raw.get("modes"), dict) else {}
        for entry in [names] if isinstance(names, str) else names:
            name, mode, aim = _cast_entry(str(entry), {_plain(search.spells[i].name) for i in hand})
            index = next((i for i in hand if _plain(search.spells[i].name) == _plain(name)), None)
            if index is None:
                issues.append(f"{label}: {name} is not in hand")
                continue
            hs = search.spells[index]
            mode = mode or next((str(m) for k, m in noted_modes.items() if _plain(k) == _plain(hs.name)), "")
            var, mode_why = _noted_variant(search, hs, node, mode, aim)
            if var is None:
                issues.append(f"{label}: {name} is not modelled")
                # Not cast by the replay (an X spell, a cast that does nothing it values): the plan's
                # line leaves out what the card does, so it can't be judged on it.
                why = unmodelled_effect(hs.spell.card, result)
                if why:
                    unmodelled.append(f"{label}: {hs.name} ({why})")
                continue
            trial = casts + [(var, None)]
            if sum(v.loss for v, _ in trial) >= node.our_life:
                issues.append(f"{label}: {name} costs the life we have left")
                continue
            pips = tuple(p for v, _ in trial for p in v.pips)
            if sum(v.mana_value for v, _ in trial) > len(sources) or (
                pips and not _pip_matching(pips, sources)
            ):
                colors = "".join(sorted({c for s in sources for c in s.produces if c != "C"})) or "no colours"
                issues.append(f"{label}: {name} is unpayable with this line's {len(sources)} mana ({colors})")
                continue
            hand.remove(index)
            why = unmodelled_effect(hs.spell.card, result)
            if why:
                unmodelled.append(f"{label}: {hs.name} ({why})")
            elif mode_why:
                unmodelled.append(f"{label}: {hs.name} (choose: {mode}): {mode_why}")
            taken = {t["instance_id"] for _v, t in casts if t is not None}
            options = (
                search.targets(var.reach, node.theirs, node.their_tapped, node.ours)
                if var.reach is not None
                else []
            )
            options = [t for t in options if t["instance_id"] not in taken]
            named = [t for t in options if aim and _plain(t["name"]) == _plain(aim)]
            casts.append((var, (named or options or [None])[0]))
        mid = search.begin(node, land, tuple(casts))
        options, possible = search.attack_choices(mid)
        ids = _plan_attackers(search, mid, str(raw.get("attack") or ""), label, issues) if possible else ()
        allowed = {i for _n, option in options for i in option}
        budgets.append(
            {
                "turn": node.abs_turn,
                "label": label,
                "mana": len(mid.sources),
                "colors": "".join(sorted({c for s in mid.sources for c in s.produces if c != "C"})),
                "source_colors": ["".join(sorted(s.produces)) for s in mid.base_sources],
            }
        )
        node = search.finish(mid, ("plan", tuple(i for i in ids if i in allowed)))
    return PlanEvaluation(search.line(search.rollout(node)), budgets, issues, unmodelled)


_NOTE = re.compile(r"\s*\(([^()]*)\)\s*$")


def _cast_entry(entry: str, in_hand: set[str]) -> tuple[str, str, str]:
    """(card name, noted mode, noted target) of a plan cast entry such as "X (choose: gain 4 life)".

    The note is split off only when the entry is not itself a card in hand.
    "on Y" names the target ("on opponent" the face-damage variant); the rest
    of the note, without "choose:", is the mode (which may contain commas).
    """
    text = entry.strip()
    match = _NOTE.search(text)
    if _plain(text) in in_hand or match is None:
        return text, "", ""
    note = re.sub(r"^\s*choose:\s*", "", match.group(1), flags=re.I).strip()
    mode, aim = note, ""
    on = re.search(r"(?:^|,\s*)on (?P<aim>[^,]+)$", note)
    if on:
        mode, aim = note[: on.start()].strip(" ,"), on.group("aim").strip()
    return text[: match.start()].strip(), mode, aim


def _same_text(note: str, text: str) -> bool:
    """A noted mode and a mode's label or text agree (either is a prefix of the other)."""
    a, b = _plain(note), _plain(str(text).rstrip("…"))
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


def _noted_variant(
    search: _Search, hs: HandSpell, node: Node, mode: str, aim: str
) -> tuple[Variant | None, str]:
    """(the variant a plan's cast replays, why its noted mode can't be judged, or '').

    A noted mode (``_mode_index``) picks the variant of that mode (its
    face-damage variant for "on opponent"); a mode the search dropped (a
    noncreature's 'other' mode) replays as a cast that does nothing: 'mode
    not modelled'. A note naming no mode of a modal card replays
    ``_default_variant`` but is reported ('mode not recognised'): the plan
    may mean a mode the replay did not cast. Without a note, or on a card
    without modes: ``_default_variant``.
    """
    casts = [v for v in hs.variants if v.kind == "cast"]
    texts = bullets(str(hs.spell.card.get("oracle_text") or ""))
    if not mode or not casts or not texts:
        return _default_variant(search, hs, node, aim), ""
    index = _mode_index(hs, texts, mode)
    if index is None:
        return _default_variant(search, hs, node, aim), "mode not recognised"
    kind = classify(hs.name, texts[index])[0]
    matching = [v for v in casts if v.mode == index]
    if not matching and kind != "other":  # a duplicate of another mode's effect: that variant
        matching = [v for v in casts if v.mode is not None and classify(hs.name, texts[v.mode])[0] == kind]
    if matching:
        why = "mode not modelled" if kind == "other" else ""
        return _default_variant(search, hs, node, aim, matching), why
    # A mode the search dropped: it does nothing it can value (a creature keeps its body).
    blank = dataclasses.replace(
        casts[0], mode=index, mode_text=mode_label(kind, 0, texts[index], hs.name), reach=None, gain=0,
        bounce=False, rock=False, face=0, aim="", tokens=(),
    )  # fmt: skip
    return blank, "mode not modelled"


# Words a plan's mode note uses for a kind of mode ("gain life", "+3 life", "kill it", "make a token").
_MODE_WORDS = (
    ("lifegain", r"\b(?:gain|gains|life|lifegain|heal)\b"),
    ("removal", r"\b(?:destroy|exile|kill|removal|remove|damage|burn)\b"),
    ("bounce", r"\b(?:return|bounce)\b"),
    ("tokens", r"\b(?:create|token|tokens)\b"),
)


def _mode_index(hs: HandSpell, texts: list[str], mode: str) -> int | None:
    """The mode a plan's note names, or None.

    In order: the note and a mode's text or label agree (``_same_text``);
    "Mode 2" / "Mode 2: You gain 3 life." (the menu's labels); the one mode
    starting with the note's first word ("destroy Splinter Twin"); the one
    mode of the kind the note's words name ("gain life", "lifegain", "+3
    life").
    """

    def agrees(note: str) -> int | None:
        return next(
            (
                i
                for i, text in enumerate(texts)
                if _same_text(note, text) or _same_text(note, mode_label(*_kind(hs, text), text, hs.name))
            ),
            None,
        )

    found = agrees(mode)
    if found is not None:
        return found
    numbered = re.match(r"^\s*mode\s*(\d+)\b\s*[:.)-]?\s*(.*)$", mode, flags=re.I)
    if numbered:
        rest = numbered.group(2).strip()
        found = agrees(rest) if rest else None
        if found is not None:
            return found
        number = int(numbered.group(1)) - 1
        return number if 0 <= number < len(texts) else None
    words = _plain(mode).split()
    first = [i for i, text in enumerate(texts) if words and _plain(text).split()[:1] == words[:1]]
    if len(first) == 1:
        return first[0]
    kinds = {kind for kind, pattern in _MODE_WORDS if re.search(pattern, _plain(mode))}
    named = [i for i, text in enumerate(texts) if classify(hs.name, text)[0] in kinds]
    return named[0] if len(named) == 1 else None


def _kind(hs: HandSpell, text: str) -> tuple[str, int]:
    kind, gain, _reach, _bounce = classify(hs.name, text)
    return kind, gain


def _default_variant(
    search: _Search, hs: HandSpell, node: Node, aim: str = "", variants: list | None = None
) -> Variant | None:
    """A plan names cards, not modes: the variant worth most against the node's board.

    ``variants`` limits the choice (a noted mode's); ``aim`` 'opponent' prefers
    the face-damage variant, any other aim the variants that hit creatures.
    """
    casts = variants if variants is not None else [v for v in hs.variants if v.kind == "cast"]
    if aim:
        face = _plain(aim) in ("opponent", "the opponent", "them", "player", "face")
        preferred = [v for v in casts if (v.aim == "opponent") == face]
        casts = preferred or casts

    def worth(var: Variant) -> float:
        target = (
            (search.targets(var.reach, node.theirs, node.their_tapped, node.ours) or [None])[0]
            if var.reach
            else None
        )
        return search.item_value(var, target, node.theirs)

    return max(casts, key=worth) if casts else None


def _plan_attackers(search: _Search, mid: Any, text: str, label: str, issues: list[str]) -> tuple:
    plain = _plain(text)
    if not plain or plain in ("none", "no attack", "nobody", "no one", "hold") or plain.startswith("none "):
        return ()
    node = mid.node
    everyone = bool(re.search(r"\b(?:all|everything|everyone|all in)\b", plain))
    named = list(mid.ours) if everyone else [
        b for b in mid.ours if _plain(b["name"]) in plain or _plain(b["name"].split(",")[0]) in plain
    ]  # fmt: skip
    ids = []
    for body in named:
        if body["instance_id"] in mid.our_tapped or not body["_can_attack"]:
            continue
        if search.sick(body, node.k, node.abs_turn, mid.entered):
            if not everyone:
                cast_now = mid.entered.get(body["instance_id"]) == node.abs_turn
                why = "can't attack the turn it is cast" if cast_now else "is summoning-sick"
                issues.append(f"{label}: {body['name']} {why}")
            continue
        ids.append(body["instance_id"])
    return tuple(sorted(ids))


def _mode_source(state: dict, options: list) -> tuple[dict | None, dict | None, bool]:
    """(card whose modes these are, object holding the mode text, already on the battlefield).

    The instance id changes from hand to stack to battlefield: the option's
    sourceId names the stack object (a cast spell, or a triggered ability
    whose parent is on the battlefield); otherwise the card is found by its
    modes' text.
    """
    source_id = _int((options[0].meta or {}).get("sourceId"))
    battlefield = [c for c in state.get("battlefield") or [] if isinstance(c, dict)]
    for obj in state.get("stack") or []:
        if not isinstance(obj, dict) or source_id is None or _int(obj.get("instance_id")) != source_id:
            continue
        parent = _int(obj.get("parent_instance_id"))
        if parent and "ability" in str(obj.get("object_kind") or obj.get("type_line") or "").lower():
            host = next((c for c in battlefield if _int(c.get("instance_id")) == parent), None)
            return host or obj, obj, True  # a trigger: its source already entered (or left)
        return obj, obj, False
    labels = [_plain(re.sub(r"^mode \d+:\s*", "", str(o.label), flags=re.I)) for o in options]
    for zone, on_battlefield in (("stack", False), ("battlefield", True), ("hand", False)):
        for card in state.get(zone) or []:
            found = (
                {_plain(b) for b in bullets(str(card.get("oracle_text") or ""))}
                if isinstance(card, dict)
                else set()
            )
            if found and all(label in found for label in labels):
                return card, card, on_battlefield
    return None, None, False


def compare_modes(
    state: dict, decision: Any, *, soft_ms: float = 60.0, hard_ms: float = 200.0
) -> ModeComparison | None:
    """One forced-root search per mode of a modal CastingTimeOptions decision.

    Each mode is applied before T (a creature still on the stack enters with
    it), removal modes on each of the top two killable targets (and an 'any
    target' mode on the opponent too); the best line per mode is kept.
    ``contingent`` names what an unmodelled 'other' mode might do (hit their
    creatures or combat, answer an engine or anthem): such a verdict must
    never be applied. ``soft_ms``/``hard_ms`` bound the whole comparison, split
    evenly over its searches. None when ARENAMCP_LINE_SEARCH=0.
    """
    if not _line_search_enabled():
        return None
    try:
        return _compare_modes(state, decision, soft_ms, hard_ms)
    except Exception:  # never let the mode check break a decision
        logger.debug("mode comparison failed", exc_info=True)
        return None


def _compare_modes(state: dict, decision: Any, soft_ms: float, hard_ms: float) -> ModeComparison | None:
    if getattr(decision, "request_type", "") != "CastingTimeOptions":
        return None
    modal = [o for o in decision.options if (o.meta or {}).get("choiceKind") == "modal"]
    if not modal:
        return None
    modal = [o for o in modal if o.meta.get("childIndex") == modal[0].meta.get("childIndex")]
    source, holder, on_battlefield = _mode_source(state, modal)
    texts = bullets(str((holder or {}).get("oracle_text") or "")) or bullets(
        str((source or {}).get("oracle_text") or "")
    )
    if source is None or not texts:
        return None
    model = build_board_model({**state, "stack": [c for c in state.get("stack") or [] if c is not holder]})
    if model is None:
        return None
    # The same hints the assessment gives its search, from board clocks: no search runs here.
    survival, lethal_now = search_hints(model)
    body = _body(source, model.turn, model.our_rules) if not on_battlefield and _is_creature(source) else None
    name = _name(source)
    kinds: dict[str, str] = {}
    other: list[str] = []
    runs: list[tuple[str, dict]] = []
    for option in modal:
        label = _plain(re.sub(r"^mode \d+:\s*", "", str(option.label), flags=re.I))
        index = next(
            (i for i, b in enumerate(texts) if _plain(b) == label), _int(option.meta.get("optionIndex"))
        )
        if index is None or not 0 <= index < len(texts):
            continue
        text = texts[index]
        kind, gain, reach, bounce = classify(name, text)
        pseudo = {"name": name, "oracle_text": text, "type_line": ""}
        alt, each = face_damage(pseudo)
        kinds[option.option_id] = "other" if kind == "other" else mode_label(kind, gain, text, name)
        other += [text] if kind == "other" and not (alt or each) else []
        # A mode's creature tokens enter with it (an id range apart from the search's own tokens).
        tokens = make_tokens(
            token_specs(_cast_text(pseudo)) or [], model.turn, model.our_rules, _ROOT_TOKEN_IDS
        )
        effect = {
            "body": body,
            "gain": gain - life_loss(pseudo),
            "bounce": bounce,
            "face": each,
            "tokens": tokens,
        }
        targets: list = [None]
        if reach is not None:
            killable = sorted(
                (
                    b
                    for b in model.theirs
                    if _kills(reach, b, ours=model.ours, ward_mana=len(model.sources_now))
                ),
                key=lambda b: (-b["power"], -b["toughness"], b["instance_id"]),
            )
            targets = killable[:2] or [None]
        runs += [(option.option_id, {**effect, "target": target}) for target in targets]
        if alt:
            runs.append((option.option_id, {**effect, "target": None, "face": each + alt}))
    lines: dict[str, Line] = {}
    results: dict[str, LineSearchResult] = {}
    started = time.perf_counter()
    for position, (option_id, effect) in enumerate(runs):
        left = hard_ms - (time.perf_counter() - started) * 1000
        if left <= 0 and option_id in results:
            continue
        share = max(0.0, left) / (len(runs) - position)
        result = _Search(
            model, survival=survival, lethal_now=lethal_now, soft_ms=soft_ms / len(runs), hard_ms=share,
            groups=16, width=2, k_next=4, opponent=None, root_effect=effect,
        ).run()  # fmt: skip
        if option_id not in results or _order(result.best) > _order(results[option_id].best):
            results[option_id], lines[option_id] = result, result.best
    if not lines:
        return None
    return ModeComparison(name, lines, results, kinds, _contingent(state, model, other))


# An 'other' mode whose text touches their creatures or combat: the search can't value it.
_COMBAT_MODE = re.compile(
    r"\bcreatures? (?:your opponents control|target opponent controls|an opponent controls|you don't control)"
    r"|\b(?:each|all) (?:other )?creatures?\b|\bgets? [-−–]\d+/|\btap\b|\bdoesn't untap\b"
    r"|\bcan't (?:attack|block)\b|\bprevent\b|\bexile target\b|\bsacrifices?\b|\breturn target\b"
    r"|\bfights?\b|\bdamage to\b"
)
_ATTACHMENT_GRANT = re.compile(r"\b(?:enchanted|equipped) creature (?:gets|has)\b")


def _contingent(state: dict, model: BoardModel, other_modes: list[str]) -> list[str]:
    """What an unmodelled ('other') mode could do that the search can't see.

    The mode's own text hitting their creatures or combat (a -1/-1 sweep, a
    tap, "can't attack"); every engine and anthem of theirs (all of
    ``_threats``' noncreature reasons, not only its top five); their auras and
    equipment pumping a creature of theirs.
    """
    if not other_modes:
        return []
    notes = [
        f"'{mode}' affects their creatures or combat; not modelled"
        for mode in other_modes
        if _COMBAT_MODE.search(mode.lower())
    ]
    battlefield = [c for c in state.get("battlefield") or [] if isinstance(c, dict)]
    our_air = any(_reach_or_flying(b) for b in model.ours)
    threats = _threats(battlefield, model.opponent, list(model.theirs), our_air, len(model.ours), limit=None)
    for threat in threats:
        if "token/copy engine" in threat.why or "anthem" in threat.why:
            notes += [
                f"'{mode}' could answer {threat.name} ({threat.why}); not modelled" for mode in other_modes
            ]
    theirs = {b["instance_id"] for b in model.theirs}
    for card in battlefield:
        if _controller(card) != model.opponent or _is_creature(card):
            continue
        attached = _int(card.get("attached_to_id"))
        if (
            attached in theirs
            and ("aura" in _types(card) or "equipment" in _types(card))
            and _ATTACHMENT_GRANT.search(_text(card))
        ):
            notes += [
                f"'{mode}' could answer {_name(card)} (on their creature); not modelled"
                for mode in other_modes
            ]
    return notes


__all__ = [
    "ALIVE",
    "DEAD",
    "LINE_TOL",
    "NOTHING",
    "POSTURE_MARGIN",
    "WIN",
    "ActionKey",
    "Line",
    "LineSearchResult",
    "ModeComparison",
    "OpponentModel",
    "ParanoidOpponent",
    "PlanEvaluation",
    "Step",
    "action_key",
    "compare_modes",
    "evaluate_plan",
    "proxy_outcome",
    "search_lines",
    "unmodelled_effect",
]
