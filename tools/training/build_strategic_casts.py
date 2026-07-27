"""Build the STRATEGIC CAST corpus from 17lands `replay_data`.

One record = "here is the board; which SPELL do you cast?" — asked of a
diamond-or-mythic Limited player, on any turn of the game, with the land drop
already resolved and removed from the menu.

WHY THIS EXISTS (read before changing anything)
-----------------------------------------------
The previous corpus (``build_play_decisions.py``) was 95.7% land drops because
it labelled the FIRST action of the turn and the first action of an early turn
is almost always the land. A model trained on it learned a land-drop reflex and
scored 13/63 — identical to the untuned base. Land drops are excluded here by
construction, not by filtering: the land played on the turn is removed from the
hand, added to the mana, and ``Play Land`` entries are never emitted. The
measured land-drop share of the answers is therefore exactly 0.

An earlier analysis wrongly claimed 17lands could only reconstruct turn 1. It
carries per-turn columns for turns 1..30 (``user_turn_N_creatures_cast``,
``_non_creatures_cast``, ``_cards_drawn``, ``eot_user_cards_in_hand``,
``eot_user_lands_in_play``, ...), so every turn of every game is reconstructable.
This builder uses all of them and MEASURES where the reconstruction stops being
trustworthy instead of assuming a cap.

THE DECISION POINT
------------------
For user turn N::

    hand   = eot user hand of the PRECEDING turn        (the opponent's turn N-1
             on the play / N on the draw — so cards discarded or drawn during
             the opponent's turn are accounted for)
             + user_turn_N_cards_drawn + user_turn_N_cards_tutored
             − the land played on turn N (one copy)
    mana   = eot user lands of the preceding turn + the land played on turn N
             (all untapped at the untap step; a land that entered tapped is
             detected from oracle text and excluded from the untapped count)
    board  = eot user creatures / non-creatures of the preceding turn
    menu   = {Cast c for every nonland card c in hand} + {Pass}
    answer = a spell actually cast on turn N

CONDITIONS CARRIED OVER FROM THE FIDELITY SPIKE (measured, not opinions)
------------------------------------------------------------------------
1. NO AFFORDABILITY FILTER. MTGA's own active menu is not mana-gated — 47% of
   real Cast rows cost more than the enumerated available mana. Applying an
   affordability filter raised end-to-end poison from 5.4% to 21%. We pass
   ``policy="nofilter"`` and never make it a tunable.
2. DROP EVERY ROW WHOSE RECORDED PICK IS ABSENT FROM THE RECONSTRUCTED HAND.
   That is the entire remaining poison term and it is decidable at build time.
   Counted per turn as ``cast_poison_by_turn`` — this is the curve that decides
   the turn cap.
3. THE MENU IS "Candidate:", NEVER "Legal:". It covers ~62% of a real MTGA menu
   (no mana abilities, no floating mana, no activated abilities, no instant-speed
   windows). The system prompt says so explicitly.
4. Ability activations are not emitted (measured Activate pick recall was 0%).

MULTI-CAST TURNS
----------------
17lands records no within-turn ordering, so a turn with 2+ casts has no ground
truth for "which came first". Four policies are implemented and ``--policy-audit``
reports the volume of each on the same sample:

    drop   — emit nothing for multi-cast turns. Zero ordering assumptions, but
             biases the corpus toward mana-poor turns (you can only cast one
             spell), which is the opposite of a strategic corpus.
    first  — emit one record, answer = the first cast listed. The column order
             is creatures-then-noncreatures, i.e. arbitrary; the label is an
             arbitrary member of the demonstrated set.
    all    — one record per cast, all sharing the START-OF-TURN state. Every
             label is a spell the player really cast, but k identical prompts
             carry k different answers (contradictory targets on one input).
    chain  — one record per cast, sequenced most-expensive-first, with the state
             advanced between records (earlier casts leave the hand, creatures
             join the board, mana is spent). No duplicate prompts, no
             contradictory labels; the cost is one heuristic — the assumed
             descending-mana-value order.

DATA / LICENCE
--------------
Source: 17Lands public ``replay_data`` dumps, CC BY 4.0. Derived datasets must
credit "17Lands"; the attribution is written into every record and into the
stats sidecar.

CPU only. No GPU, no network (uses the already-cached Scryfall bulk file).

Usage
-----
    python -m tools.training.build_strategic_casts \
        --replay-csv tools/eval/data/17lands/replay_data_public.EOE.PremierDraft.csv.gz \
        --max-rows-per-file 40000 --policy-audit --audit-only \
        --stats-out tools/training/data/strategic_casts_audit.json

    python -m tools.training.build_strategic_casts \
        --replay-csv ...EOE... --replay-csv ...OTJ... \
        --multi-cast-policy chain --max-turn 12 \
        --out tools/training/data/strategic_casts.jsonl \
        --stats-out tools/training/data/strategic_casts_stats.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("build_strategic_casts")

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Reconstruction is IMPORTED, never re-implemented (synthesize_menu owns the
# menu model; build_play_decisions owns the rendering helpers and the
# predecessor-turn indexing that was verified against the data).
from tools.training.build_play_decisions import (  # noqa: E402
    ATTRIBUTION,
    WUBRG,
    CardDetail,
    _board_lines,
    _hand_lines,
    _num,
    order_menu,
    predecessor_prefix,
)
from tools.training.synthesize_menu import (  # noqa: E402
    RANK_ORDER,
    CardResolver,
    RowView,
    iter_rows,
    parse_mana_cost,
    synth_from_zones,
)

from arenamcp.action_planner import AUTOPILOT_SYSTEM_PROMPT  # noqa: E402

SOURCE_TAG = "17lands-replay_data"
DATASET = "strategic_casts"

MULTI_CAST_POLICIES = ("drop", "first", "all", "chain")

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

CAST_MENU_ADDENDUM = """
CANDIDATE-CAST MODE (reconstructed historical position — read this, it OVERRIDES
the menu rules above for this prompt):
- The numbered menu below is headed "Candidate:", not "Legal:". It is a
  RECONSTRUCTED, DELIBERATELY INCOMPLETE set of candidate actions built from a
  recorded game history — it is NOT MTGA's legal-action list.
- THIS DECISION IS THE SPELL CAST ONLY. Your land drop for the turn is already
  resolved and no "Play Land" entry will ever appear. Do not ask for one.
- What else is missing: mana abilities, floating mana, activated abilities of
  permanents, every instant-speed window, target choices and attack/block
  declarations. Only cards castable from your hand are listed, plus Pass.
- There are NO "[OK]" affordability tags, because affordability was
  deliberately NOT computed: MTGA's own menu is not mana-gated either. Use the
  "Mana:" line to judge what you can pay for; an entry you cannot afford is
  still listed.
- Answer with the number of the best entry: {"actions":[{"pick": N}]}. Exactly
  one action. No reasoning field, no prose.
"""

SYSTEM_PROMPT = AUTOPILOT_SYSTEM_PROMPT + "\n" + CAST_MENU_ADDENDUM

# Production trigger strings, verbatim from action_planner.trigger_descriptions.
TRIGGER_AFTER_LAND = "TRIGGER: Land played. What's the next play?"
TRIGGER_NO_LAND = "TRIGGER: Your turn started (Main Phase 1). Plan your plays."

_TAPPED_MARKERS = ("enters tapped", "enters the battlefield tapped")


# ---------------------------------------------------------------------------
# Card helpers
# ---------------------------------------------------------------------------


class CardFacts:
    """Cheap memoised card facts layered on the shared resolver."""

    def __init__(self, resolver: CardResolver, detail: CardDetail):
        self.resolver = resolver
        self.detail = detail
        self._mv: dict[int, int] = {}
        self._tapped: dict[int, bool] = {}

    def mana_value(self, gid: int) -> int:
        got = self._mv.get(gid)
        if got is not None:
            return got
        card = self.resolver.get(gid)
        best = -1
        for cost in card.castable_costs:
            for piece in cost.split("//") if "//" in cost else [cost]:
                parsed = parse_mana_cost(piece)
                if parsed.parse_ok:
                    best = max(best, parsed.total)
        if best < 0:
            best = 0
        self._mv[gid] = best
        return best

    def enters_tapped(self, gid: int) -> bool:
        got = self._tapped.get(gid)
        if got is not None:
            return got
        card = self.resolver.get(gid)
        text = (self.detail.oracle(card, 4000) or "").lower()
        val = any(m in text for m in _TAPPED_MARKERS)
        self._tapped[gid] = val
        return val


def _remove_one(cards: list[int], gid: int) -> list[int]:
    """Remove a single copy of ``gid``; a no-op if it is not present."""
    out = list(cards)
    try:
        out.remove(gid)
    except ValueError:
        pass
    return out


# ---------------------------------------------------------------------------
# Position reconstruction
# ---------------------------------------------------------------------------


@dataclass
class CastPosition:
    """The board at the point the player casts on user turn N."""

    turn: int
    on_play: bool
    hand: list[int] = field(default_factory=list)  # land drop already removed
    lands: list[int] = field(default_factory=list)  # incl. the land played this turn
    land_played: Optional[int] = None
    land_entered_tapped: bool = False
    creatures: list[int] = field(default_factory=list)
    noncreatures: list[int] = field(default_factory=list)
    opp_lands: list[int] = field(default_factory=list)
    opp_creatures: list[int] = field(default_factory=list)
    opp_noncreatures: list[int] = field(default_factory=list)
    life: Optional[float] = None
    opp_life: Optional[float] = None
    opp_hand_count: Optional[float] = None
    creature_casts: list[int] = field(default_factory=list)
    noncreature_casts: list[int] = field(default_factory=list)

    @property
    def casts(self) -> list[int]:
        return self.creature_casts + self.noncreature_casts


def build_cast_position(
    facts: CardFacts,
    rv: RowView,
    row: list[str],
    turn: int,
    on_play: bool,
    basis: str,
) -> Optional[CastPosition]:
    cur = f"user_turn_{turn}_"
    lands_played = rv.ids(row, cur + "lands_played")
    creature_casts = rv.ids(row, cur + "creatures_cast")
    noncreature_casts = rv.ids(row, cur + "non_creatures_cast")
    if not (
        lands_played or creature_casts or noncreature_casts or rv.get(row, cur + "eot_user_cards_in_hand")
    ):
        return None  # turn never happened

    pre = predecessor_prefix(turn, on_play, basis)
    if pre is None:
        base_hand = rv.ids(row, "opening_hand")
    else:
        base_hand = rv.ids(row, pre + "eot_user_cards_in_hand")
        if not base_hand and turn == 1:
            base_hand = rv.ids(row, "opening_hand")

    hand = base_hand + rv.ids(row, cur + "cards_drawn") + rv.ids(row, cur + "cards_tutored")

    pos = CastPosition(
        turn=turn,
        on_play=on_play,
        creature_casts=creature_casts,
        noncreature_casts=noncreature_casts,
    )
    if pre is not None:
        pos.lands = rv.ids(row, pre + "eot_user_lands_in_play")
        pos.creatures = rv.ids(row, pre + "eot_user_creatures_in_play")
        pos.noncreatures = rv.ids(row, pre + "eot_user_non_creatures_in_play")
        pos.opp_lands = rv.ids(row, pre + "eot_oppo_lands_in_play")
        pos.opp_creatures = rv.ids(row, pre + "eot_oppo_creatures_in_play")
        pos.opp_noncreatures = rv.ids(row, pre + "eot_oppo_non_creatures_in_play")
        pos.life = _num(rv.get(row, pre + "eot_user_life"))
        pos.opp_life = _num(rv.get(row, pre + "eot_oppo_life"))
        pos.opp_hand_count = _num(rv.get(row, pre + "eot_oppo_cards_in_hand"))

    # The land drop is RESOLVED before this decision: it leaves the hand and
    # joins the mana. 17lands records at most one land per turn.
    if lands_played:
        pos.land_played = lands_played[0]
        hand = _remove_one(hand, pos.land_played)
        pos.lands = pos.lands + [pos.land_played]
        pos.land_entered_tapped = facts.enters_tapped(pos.land_played)
    pos.hand = hand
    return pos


# ---------------------------------------------------------------------------
# Multi-cast policy
# ---------------------------------------------------------------------------


@dataclass
class Step:
    seq: int  # 0-based index within the turn
    n_casts: int
    pick: int
    hand: list[int]
    creatures: list[int]
    noncreatures: list[int]
    mana_spent: int
    already_cast: list[int]


def steps_for_turn(facts: CardFacts, pos: CastPosition, policy: str) -> list[Step]:
    casts = pos.casts
    if not casts:
        return []
    n = len(casts)
    if policy == "drop":
        if n != 1:
            return []
        order = casts
    elif policy == "first":
        order = casts[:1]
    elif policy == "all":
        order = casts
    elif policy == "chain":
        # Most expensive first: when a Limited player casts several spells in a
        # turn the big one is normally the mana-defining play and the cheap ones
        # fill in around it. This is the ONLY ordering assumption in the file.
        order = sorted(casts, key=lambda g: (-facts.mana_value(g), g))
    else:  # pragma: no cover - argparse constrains this
        raise ValueError(policy)

    creature_set = set(pos.creature_casts)
    out: list[Step] = []
    if policy != "chain":
        for i, gid in enumerate(order):
            out.append(
                Step(
                    seq=i,
                    n_casts=n,
                    pick=gid,
                    hand=list(pos.hand),
                    creatures=list(pos.creatures),
                    noncreatures=list(pos.noncreatures),
                    mana_spent=0,
                    already_cast=[],
                )
            )
        return out

    hand = list(pos.hand)
    creatures = list(pos.creatures)
    noncreatures = list(pos.noncreatures)
    spent = 0
    already: list[int] = []
    for i, gid in enumerate(order):
        out.append(
            Step(
                seq=i,
                n_casts=n,
                pick=gid,
                hand=list(hand),
                creatures=list(creatures),
                noncreatures=list(noncreatures),
                mana_spent=spent,
                already_cast=list(already),
            )
        )
        hand = _remove_one(hand, gid)
        if gid in creature_set:
            creatures = creatures + [gid]
        else:
            noncreatures = noncreatures + [gid]
        spent += facts.mana_value(gid)
        already = already + [gid]
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def menu_entry_text(resolver: CardResolver, entry: tuple[str, Optional[int]]) -> str:
    kind, gid = entry
    if kind == "Pass":
        return "Pass"
    card = resolver.get(int(gid))
    return f"Cast {card.name or f'Unknown card (grpId {gid})'}"


def mana_line(facts: CardFacts, pos: CastPosition, spent: int) -> str:
    """Mana available for the cast decision.

    Every land untaps at the start of the turn, so the count is exact except for
    a land that entered tapped THIS turn (detected from oracle text) and for
    mana already committed to earlier casts on the same turn.
    """
    if not pos.lands:
        return "Mana: 0"
    sources: list[str] = []
    for gid in pos.lands:
        produced = [c for c in (facts.resolver.get(gid).produced_mana or ()) if c in (*WUBRG, "C")]
        sources.append("/".join(produced) if produced else "?")
    display = " ".join(f"{{{s}}}" if "/" in s else s for s in sources)
    avail = len(pos.lands) - (1 if pos.land_entered_tapped else 0) - spent
    avail = max(avail, 0)
    line = f"Mana: {avail} (lands: {len(pos.lands)}, sources: {display})"
    notes = []
    if pos.land_entered_tapped:
        notes.append("land played this turn entered TAPPED")
    if spent:
        notes.append(f"{spent} already spent this turn")
    if notes:
        line += " [" + "; ".join(notes) + "]"
    return line


def render_user_message(
    facts: CardFacts,
    pos: CastPosition,
    step: Step,
    menu: list[tuple[str, Optional[int]]],
    oracle_chars: int,
) -> str:
    resolver, detail = facts.resolver, facts.detail
    lines: list[str] = ["=== GAME ==="]
    lines.append("Candidate: (pick by number — RECONSTRUCTED CASTS ONLY, not a complete legal-action list)")
    lines.extend(f"  {i + 1}. {menu_entry_text(resolver, e)}" for i, e in enumerate(menu))
    lines.append(f"T{pos.turn} YOUR | Main1 | Pri:You")
    lines.append("Timing: ALL SPELLS")
    you = f"{pos.life:.0f}" if pos.life is not None else "20"
    opp = f"{pos.opp_life:.0f}" if pos.opp_life is not None else "20"
    lines.append(f"Life: You={you} Opp={opp}")
    lines.append(mana_line(facts, pos, step.mana_spent))
    if pos.land_played is not None:
        name = resolver.get(pos.land_played).name or f"grpId {pos.land_played}"
        lines.append(f"Land: ALREADY PLAYED this turn ({name}) — no land action in this decision")
    else:
        lines.append("Land: no land played this turn — land actions are excluded from this decision")
    if step.already_cast:
        cast_names = ", ".join(
            resolver.get(g).name or f"grpId {g}" for g in step.already_cast
        )
        lines.append(f"Already cast this turn: {cast_names}")
    lines.append(f"On the {'play' if pos.on_play else 'draw'}.")
    if pos.opp_hand_count is not None:
        lines.append(f"Opp hand: {pos.opp_hand_count:.0f} cards")

    lines.append("")
    lines.append("YOUR BOARD:")
    own = step.creatures + step.noncreatures + pos.lands
    lines.extend(_board_lines(resolver, detail, own, oracle_chars) or ["  (empty)"])

    lines.append("")
    lines.append("OPP BOARD:")
    opp_board = pos.opp_creatures + pos.opp_noncreatures + pos.opp_lands
    lines.extend(_board_lines(resolver, detail, opp_board, oracle_chars) or ["  (empty)"])

    lines.append("")
    lines.append("HAND:")
    lines.extend(_hand_lines(resolver, detail, step.hand, oracle_chars) or ["  (empty)"])

    trigger = TRIGGER_AFTER_LAND if pos.land_played is not None else TRIGGER_NO_LAND
    return trigger + "\n\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@dataclass
class BuildStats:
    games_read: int = 0
    turn_slots_seen: int = 0
    turn_slots_no_data: int = 0
    turns_with_data: int = 0
    turns_no_cast: int = 0
    turns_with_cast: int = 0
    turns_multi_cast: int = 0
    casts_observed: int = 0
    emitted: int = 0

    dropped: Counter = field(default_factory=Counter)
    ranks: Counter = field(default_factory=Counter)
    expansions: Counter = field(default_factory=Counter)

    # per-turn curves (policy independent)
    turns_with_cast_by_turn: Counter = field(default_factory=Counter)
    casts_by_turn: Counter = field(default_factory=Counter)
    cast_poison_by_turn: Counter = field(default_factory=Counter)
    land_drops_by_turn: Counter = field(default_factory=Counter)

    # per-turn curves (chosen policy, post-filter)
    emitted_by_turn: Counter = field(default_factory=Counter)
    menu_sizes: list[int] = field(default_factory=list)
    pick_indices: Counter = field(default_factory=Counter)
    prompt_chars: list[int] = field(default_factory=list)
    answer_cards: Counter = field(default_factory=Counter)
    answer_kind: Counter = field(default_factory=Counter)
    seq_index: Counter = field(default_factory=Counter)
    n_casts_hist: Counter = field(default_factory=Counter)
    answer_mv: Counter = field(default_factory=Counter)
    expected_index1: float = 0.0
    answer_cards_by_expansion: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Record construction
# ---------------------------------------------------------------------------


def make_records(
    facts: CardFacts,
    pos: CastPosition,
    *,
    policy: str,
    min_menu_casts: int,
    include_pass: bool,
    oracle_chars: int,
    menu_order: str,
    game_key: str,
    rank: str,
    expansion: str,
    won: str,
    wr_bucket: str,
    st: Optional[BuildStats] = None,
    render: bool = True,
) -> tuple[list[dict], Counter]:
    """Turn one reconstructed position into 0..k records. Returns (recs, drops)."""
    drops: Counter = Counter()
    resolver = facts.resolver
    out: list[dict] = []
    creature_set = set(pos.creature_casts)

    for step in steps_for_turn(facts, pos, policy):
        hand = step.hand
        # ---- CONDITION 2: the recorded pick must be in the reconstructed hand.
        if step.pick not in hand:
            drops["pick_not_in_hand"] += 1
            continue
        # ---- CONDITION 1: no affordability filter, and no Play Land entries.
        menu_set = synth_from_zones(resolver, hand, pos.lands, policy="nofilter", allow_play=False)
        if not include_pass:
            # Every record in this corpus is a turn the player DID cast, so Pass
            # would be a systematically-wrong entry in 100% of menus. Leaving it
            # in teaches "never pass" — the same class of reflex as the land-drop
            # reflex this corpus exists to avoid. It is dropped, and the corpus
            # is honestly scoped as "which spell", not "whether to act".
            menu_set = {e for e in menu_set if e[0] != "Pass"}
        if ("Cast", step.pick) not in menu_set:
            # Pick is a land / uncostable / unresolved card; same defect class.
            drops["pick_not_in_menu"] += 1
            continue
        if any(gid is not None and not resolver.get(int(gid)).name for _, gid in menu_set):
            drops["unresolved_menu_entry"] += 1
            continue
        if any(not resolver.get(g).name for g in set(hand)):
            drops["unresolved_hand_card"] += 1
            continue
        n_cast_entries = sum(1 for k, _ in menu_set if k == "Cast")
        if n_cast_entries < min_menu_casts:
            drops["menu_below_min_casts"] += 1
            continue

        record_key = f"{game_key}|t{pos.turn}|s{step.seq}"
        seed = int(hashlib.blake2b(record_key.encode(), digest_size=8).hexdigest(), 16) % (2**31)
        menu = order_menu(resolver, menu_set, seed, menu_order)
        pick_index = menu.index(("Cast", step.pick)) + 1

        user = render_user_message(facts, pos, step, menu, oracle_chars) if render else ""
        rec = {
            "id": f"sc-{hashlib.blake2b(record_key.encode(), digest_size=8).hexdigest()}",
            "system": SYSTEM_PROMPT,
            "user": user,
            "response": json.dumps({"actions": [{"pick": pick_index}]}, ensure_ascii=False),
            "source": SOURCE_TAG,
            "attribution": ATTRIBUTION,
            "kind": DATASET,
            "meta": {
                "game_key": game_key,
                "record_key": record_key,
                "expansion": expansion,
                "rank": rank,
                "won": won,
                "user_game_win_rate_bucket": wr_bucket,
                "turn": pos.turn,
                "on_play": pos.on_play,
                "pick_index": pick_index,
                "pick_kind": "cast",
                "pick_action": ["Cast", step.pick],
                "pick_card": resolver.get(step.pick).name,
                "pick_is_creature": step.pick in creature_set,
                "pick_mana_value": facts.mana_value(step.pick),
                "menu_size": len(menu),
                "menu_cast_entries": n_cast_entries,
                "menu_seed": seed,
                "menu_order": menu_order,
                "land_drop_resolved": pos.land_played is not None,
                "casts_recorded_this_turn": step.n_casts,
                "cast_seq_index": step.seq,
                "multi_cast_policy": policy,
                "menu_is_candidate_not_legal": True,
                "menu_excludes_land_actions": True,
            },
        }
        out.append(rec)
        if st is not None:
            st.emitted_by_turn[pos.turn] += 1
            st.menu_sizes.append(len(menu))
            st.pick_indices[pick_index] += 1
            # If the shuffle is doing its job the answer is uniform over menu
            # positions, so P(pick==1) must equal E[1/menu_size]. Any excess is
            # a positional shortcut the model could learn instead of the game.
            st.expected_index1 += 1.0 / len(menu)
            st.prompt_chars.append(len(user))
            st.answer_cards[rec["meta"]["pick_card"] or f"grpId {step.pick}"] += 1
            st.answer_kind["creature" if rec["meta"]["pick_is_creature"] else "noncreature"] += 1
            st.seq_index[step.seq] += 1
            st.n_casts_hist[step.n_casts] += 1
            st.answer_mv[rec["meta"]["pick_mana_value"]] += 1
            st.answer_cards_by_expansion.setdefault(expansion, Counter())[
                rec["meta"]["pick_card"] or f"grpId {step.pick}"
            ] += 1
    return out, drops


# ---------------------------------------------------------------------------
# Build driver
# ---------------------------------------------------------------------------


def build(
    facts: CardFacts,
    csv_paths: list[Path],
    *,
    min_rank: str,
    max_rows_per_file: int,
    min_turn: int,
    max_turn: int,
    policy: str,
    min_menu_casts: int,
    include_pass: bool,
    oracle_chars: int,
    menu_order: str,
    state_basis: str,
    out: Optional[Path],
    max_records: int,
    policy_audit: bool,
) -> tuple[BuildStats, dict[str, Counter]]:
    st = BuildStats()
    audit: dict[str, Counter] = {p: Counter() for p in MULTI_CAST_POLICIES} if policy_audit else {}
    fh = None
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        fh = out.open("w")
    try:
        for path in csv_paths:
            logger.info("reading %s", path)
            for rv, row in iter_rows(path, min_rank, max_rows_per_file):
                st.games_read += 1
                rank = rv.get(row, "rank")
                expansion = rv.get(row, "expansion")
                st.ranks[rank] += 1
                st.expansions[expansion] += 1
                on_play = rv.get(row, "on_play").strip().lower() in ("true", "1", "t")
                game_key = "|".join(
                    (
                        rv.get(row, "draft_id"),
                        rv.get(row, "match_number"),
                        rv.get(row, "game_number"),
                        expansion,
                    )
                )
                top = min(max_turn, rv.max_user_turn())
                for turn in range(min_turn, top + 1):
                    st.turn_slots_seen += 1
                    pos = build_cast_position(facts, rv, row, turn, on_play, basis=state_basis)
                    if pos is None:
                        st.turn_slots_no_data += 1
                        continue
                    st.turns_with_data += 1
                    if pos.land_played is not None:
                        st.land_drops_by_turn[turn] += 1
                    casts = pos.casts
                    if not casts:
                        st.turns_no_cast += 1
                        continue
                    st.turns_with_cast += 1
                    st.turns_with_cast_by_turn[turn] += 1
                    st.casts_observed += len(casts)
                    st.casts_by_turn[turn] += len(casts)
                    if len(casts) > 1:
                        st.turns_multi_cast += 1

                    # Policy-independent poison curve: how many of the casts the
                    # player really made are missing from the reconstructed hand?
                    have = Counter(pos.hand)
                    want = Counter(casts)
                    missing = sum(max(0, c - have.get(g, 0)) for g, c in want.items())
                    if missing:
                        st.cast_poison_by_turn[turn] += missing

                    if policy_audit:
                        for p in MULTI_CAST_POLICIES:
                            recs, drops = make_records(
                                facts,
                                pos,
                                policy=p,
                                min_menu_casts=min_menu_casts,
                                include_pass=include_pass,
                                oracle_chars=oracle_chars,
                                menu_order=menu_order,
                                game_key=game_key,
                                rank=rank,
                                expansion=expansion,
                                won="",
                                wr_bucket="",
                                render=False,
                            )
                            audit[p]["emitted"] += len(recs)
                            audit[p]["turns_emitting"] += 1 if recs else 0
                            audit[p].update(drops)
                            for r in recs:
                                audit[p][f"turn_{r['meta']['turn']}"] += 1

                    recs, drops = make_records(
                        facts,
                        pos,
                        policy=policy,
                        min_menu_casts=min_menu_casts,
                        include_pass=include_pass,
                        oracle_chars=oracle_chars,
                        menu_order=menu_order,
                        game_key=game_key,
                        rank=rank,
                        expansion=expansion,
                        won=rv.get(row, "won"),
                        wr_bucket=rv.get(row, "user_game_win_rate_bucket"),
                        st=st,
                        render=fh is not None,
                    )
                    st.dropped.update(drops)
                    for rec in recs:
                        st.emitted += 1
                        if fh is not None:
                            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    if max_records and st.emitted >= max_records:
                        logger.info("hit --max-records=%d", max_records)
                        return st, audit
    finally:
        if fh is not None:
            fh.close()
    return st, audit


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 3) if d else 0.0


def summarize(
    st: BuildStats, audit: dict[str, Counter], args: argparse.Namespace, elapsed: float
) -> dict:
    per_turn = []
    for t in sorted(set(st.turns_with_cast_by_turn) | set(st.emitted_by_turn)):
        casts = st.casts_by_turn.get(t, 0)
        poison = st.cast_poison_by_turn.get(t, 0)
        per_turn.append(
            {
                "turn": t,
                "turns_with_a_cast": st.turns_with_cast_by_turn.get(t, 0),
                "casts_observed": casts,
                "casts_missing_from_reconstructed_hand": poison,
                "poison_pct": _pct(poison, casts),
                "land_drops": st.land_drops_by_turn.get(t, 0),
                "records_emitted": st.emitted_by_turn.get(t, 0),
            }
        )

    total = sum(st.answer_cards.values())
    top = st.answer_cards.most_common(20)
    hhi = sum((c / total) ** 2 for c in st.answer_cards.values()) if total else 0.0
    entropy = -sum((c / total) * math.log2(c / total) for c in st.answer_cards.values()) if total else 0.0

    out = {
        "dataset": DATASET,
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_sec": round(elapsed, 1),
        "attribution": ATTRIBUTION,
        "source": SOURCE_TAG,
        "source_files": [str(p) for p in args.replay_csv],
        "scope": {
            "decision_point": "spell cast on the player's own precombat main phase, land drop already resolved",
            "player_turn_range": [args.min_turn, args.max_turn],
            "min_rank": args.min_rank,
            "multi_cast_policy": args.multi_cast_policy,
            "min_menu_cast_entries": args.min_menu_casts,
            "state_basis": args.state_basis,
            "affordability_filter": "NONE (condition 1)",
            "menu_label": "Candidate (not Legal) — ~62% of a real MTGA menu",
            "land_actions_in_menu": False,
            "pass_in_menu": bool(args.include_pass),
            "pass_note": "every record is a turn the player DID cast, so Pass can never be the "
            "answer; it is excluded so the corpus cannot teach an 'always act' reflex",
        },
        "funnel": {
            "games_read": st.games_read,
            "turn_slots_seen": st.turn_slots_seen,
            "turn_slots_never_played": st.turn_slots_no_data,
            "turns_with_data": st.turns_with_data,
            "turns_with_no_cast_dropped": st.turns_no_cast,
            "turns_with_at_least_one_cast": st.turns_with_cast,
            "turns_with_multiple_casts": st.turns_multi_cast,
            "casts_observed": st.casts_observed,
            "dropped": dict(st.dropped.most_common()),
            "records_emitted": st.emitted,
        },
        "funnel_pct": {
            "turns_with_data_that_had_a_cast": _pct(st.turns_with_cast, st.turns_with_data),
            "multi_cast_share_of_cast_turns": _pct(st.turns_multi_cast, st.turns_with_cast),
            "poison_drop_pct_of_casts": _pct(st.dropped.get("pick_not_in_hand", 0), st.casts_observed),
            "emitted_per_game": round(st.emitted / st.games_read, 3) if st.games_read else 0.0,
        },
        "rank_mix": {k: {"games": v, "pct": _pct(v, st.games_read)} for k, v in st.ranks.most_common()},
        "expansion_mix": dict(st.expansions.most_common()),
        "per_turn": per_turn,
        "answers": {
            "land_drop_share_pct": 0.0,
            "land_drop_share_note": "structural: Play Land entries are never emitted and the land played "
            "on the turn is removed from the hand before the menu is built",
            "creature_vs_noncreature": dict(st.answer_kind),
            "answer_mana_value_histogram": dict(sorted(st.answer_mv.items())),
            "distinct_answer_cards": len(st.answer_cards),
            "distinct_answer_cards_by_expansion": {
                k: len(v) for k, v in st.answer_cards_by_expansion.items()
            },
            "answer_entropy_bits": round(entropy, 2),
            "answer_max_entropy_bits": round(math.log2(len(st.answer_cards)), 2) if st.answer_cards else 0,
            "answer_hhi": round(hhi, 5),
            "top20_share_pct": _pct(sum(c for _, c in top), total),
            "top20_answers": top,
        },
        "menu": {
            "mean_menu_size": round(statistics.mean(st.menu_sizes), 2) if st.menu_sizes else 0,
            "median_menu_size": statistics.median(st.menu_sizes) if st.menu_sizes else 0,
            "mean_cast_entries": round(
                statistics.mean(st.menu_sizes) - (1 if args.include_pass else 0), 2
            )
            if st.menu_sizes
            else 0,
            "pick_index_distribution": dict(sorted(st.pick_indices.items())),
            "pick_index_1_observed_pct": _pct(st.pick_indices.get(1, 0), st.emitted),
            "pick_index_1_expected_pct_if_uniform": round(
                100.0 * st.expected_index1 / st.emitted, 3
            )
            if st.emitted
            else 0.0,
            "mean_prompt_chars": round(statistics.mean(st.prompt_chars), 1) if st.prompt_chars else 0,
        },
        "multi_cast": {
            "casts_per_turn_histogram": dict(sorted(st.n_casts_hist.items())),
            "cast_seq_index_histogram": dict(sorted(st.seq_index.items())),
        },
    }
    if audit:
        out["multi_cast_policy_audit"] = {
            p: {
                "records": c.get("emitted", 0),
                "turns_emitting": c.get("turns_emitting", 0),
                "dropped": {k: v for k, v in c.items() if not k.startswith("turn_") and k not in
                            ("emitted", "turns_emitting")},
            }
            for p, c in audit.items()
        }
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--replay-csv", type=Path, action="append", required=True)
    ap.add_argument("--min-rank", default="diamond", choices=[*RANK_ORDER, ""])
    ap.add_argument("--max-rows-per-file", type=int, default=20000)
    ap.add_argument("--min-turn", type=int, default=1)
    ap.add_argument("--max-turn", type=int, default=30, help="30 = no cap; the poison curve decides")
    ap.add_argument("--multi-cast-policy", default="chain", choices=list(MULTI_CAST_POLICIES))
    ap.add_argument(
        "--min-menu-casts",
        type=int,
        default=2,
        help="drop decisions offering fewer than N castable candidates (default 2)",
    )
    ap.add_argument(
        "--include-pass",
        action="store_true",
        help="keep the Pass entry. OFF by default: every record is a turn the player DID "
        "cast, so Pass would be wrong in 100% of menus and teach an 'always act' reflex",
    )
    ap.add_argument("--menu-order", default="shuffled", choices=("shuffled", "canonical"))
    ap.add_argument("--oracle-chars", type=int, default=160)
    ap.add_argument("--state-basis", default="oppo", choices=("oppo", "user"))
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--stats-out", type=Path, default=None)
    ap.add_argument("--max-records", type=int, default=0)
    ap.add_argument("--policy-audit", action="store_true", help="count every multi-cast policy in one pass")
    ap.add_argument("--audit-only", action="store_true", help="measure only; write no records")
    ap.add_argument("--verbose", action="store_true")
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    t0 = time.time()
    resolver = CardResolver()
    facts = CardFacts(resolver, CardDetail(resolver))

    st, audit = build(
        facts,
        args.replay_csv,
        min_rank=args.min_rank,
        max_rows_per_file=args.max_rows_per_file,
        min_turn=args.min_turn,
        max_turn=args.max_turn,
        policy=args.multi_cast_policy,
        min_menu_casts=args.min_menu_casts,
        include_pass=args.include_pass,
        oracle_chars=args.oracle_chars,
        menu_order=args.menu_order,
        state_basis=args.state_basis,
        out=None if args.audit_only else args.out,
        max_records=args.max_records,
        policy_audit=args.policy_audit,
    )
    resolver.save()
    stats = summarize(st, audit, args, time.time() - t0)
    text = json.dumps(stats, indent=2)
    print(text)
    if args.stats_out:
        args.stats_out.parent.mkdir(parents=True, exist_ok=True)
        args.stats_out.write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
