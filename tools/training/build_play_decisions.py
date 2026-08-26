"""Build the production-shaped PLAY-DECISION dataset from 17lands `replay_data`.

One record = one real decision made by a diamond-or-mythic Limited player at the
first priority window of their own precombat main phase, rendered as a numbered
CANDIDATE menu, answered with production's `{"actions":[{"pick": N}]}` envelope.

WHAT THIS IS NOT
----------------
The menu here is NOT MTGA's legal-action list. 17lands `replay_data` is
turn-granular and records no menu at all; the menu is *reconstructed* by
``tools.training.synthesize_menu`` (imported, never re-implemented) from the
recorded hand/board/lands columns. The fidelity spike (docs/rl-training-status.md
§19.1) measured that reconstruction against 665 REAL MTGA main-1 menus pulled
from `.rply` replays and found:

    end-to-end pick recall  94.59%   (poison 5.41%, all self-detectable)
    menu recall             0.622    (mana abilities / float mana / activated
                                      abilities are 41% of a real menu and are
                                      structurally unrecoverable here)
    exact menu match         8.6%

So the reconstruction is a good *pick* source and a bad *menu* source. Every
design choice below follows from that, and from the five conditions §19.1
declared non-negotiable:

1. NO AFFORDABILITY FILTER. MTGA's active menu is not mana-gated (47.0% of real
   Cast rows cost more than the menu's own enumerated mana). Filtering pushes
   end-to-end poison from 5.41% to 20.99%. We pass ``policy="nofilter"``.
2. DROP EVERY ROW WHOSE RECORDED PICK IS ABSENT FROM THE RECONSTRUCTED HAND.
   That single check is the entire remaining poison term and it is decidable at
   build time. Implemented in ``_make_record``; counted in the stats as
   ``dropped_pick_not_in_hand`` (measured on a 27,000-game diamond+ scan: 3.9%
   of CAST picks, 0.45% of land drops — consistent with §19.1's 5.41%).
3. THE MENU IS LABELLED "Candidate:", NEVER "Legal:". It covers ~62% of a real
   menu. The system prompt carries an explicit addendum saying so, and the stats
   sidecar records the divergence. This dataset must be gated on its own terms —
   it is NOT prompt-identical to production (contrast the `.rply` corpus, §17.2).
4. SCOPE: first priority window of the player's own precombat main phase,
   player-turn <= 5 (pick recall 96.8-100% through turn 5, 71-92% after).
5. NO ABILITY ACTIVATIONS ARE EMITTED — measured Activate pick recall was 0%.

HONEST LIMITS baked into the output
-----------------------------------
* THE DATASET IS DOMINATED BY THE LAND DROP. §19.1 expected ~67% (measured on
  real main-1 menus across ALL turns); capping at player-turn 5 makes it worse,
  because a land drop is available on nearly every early turn. Measured here:
  **95.7% land drops, 4.3% spell casts.** Only the cast subset is unambiguously
  a strategic decision; ``--emit cast`` / ``--cast-out`` isolate it, and
  ``--nontrivial-out`` adds land drops where 2+ distinct lands were available.
  Do not quote the raw record count as if it were all strategy.
* 17lands records no within-turn ordering. "Which action came first" is a
  HEURISTIC: land-drop-first. Turns with >=2 casts and no land drop are
  genuinely ambiguous and are dropped by default (``--multi-cast-policy``).
  A consequence: 13,328 of 14,249 casts observed on labelled turns (94%) are
  discarded because they were not the turn's FIRST action.
* Ground truth is "what a diamond/mythic player actually clicked", not "what was
  optimal". Imitation is capped by the demonstrator, as always.
* Deliberate inaction is indistinguishable from having no play, so Pass rows are
  excluded by default (``--include-pass`` to opt in).

DATA / LICENCE
--------------
Source: 17Lands public `replay_data` dumps, CC BY 4.0. Derived datasets must
credit "17Lands". The attribution is written into every record (``attribution``)
and into the stats sidecar.

CPU only. No GPU, no network (uses the already-cached Scryfall bulk file).

Usage
-----
    python -m tools.training.build_play_decisions \
        --replay-csv tools/eval/data/17lands/replay_data_public.EOE.PremierDraft.csv.gz \
        --replay-csv tools/eval/data/17lands/replay_data_public.TDM.PremierDraft.csv.gz \
        --max-rows-per-file 3500 \
        --out tools/training/data/play_decisions.jsonl \
        --nontrivial-out tools/training/data/play_decisions_nontrivial.jsonl \
        --stats-out tools/training/data/play_decisions_stats.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("build_play_decisions")

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.training.synthesize_menu import (  # noqa: E402
    RANK_ORDER,
    Card,
    CardResolver,
    RowView,
    iter_rows,
    parse_mana_cost,
    synth_from_zones,
)

# The production system prompt is IMPORTED, never retyped. If production edits
# it this dataset changes with it -- and tests/test_build_play_decisions.py
# asserts the imported prefix, so a silent divergence fails the suite.
from arenamcp.action_planner import AUTOPILOT_SYSTEM_PROMPT  # noqa: E402

ATTRIBUTION = "17Lands (CC BY 4.0)"
SOURCE_TAG = "17lands-replay_data"

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

# Condition 3. Production says "ONLY pick actions from the 'Legal:' menu" and
# "TRUST [OK] tags"; neither is true of a reconstructed menu, so the addendum
# states the divergence rather than letting the model assume a completeness and
# an affordability signal that are not there.
CANDIDATE_MENU_ADDENDUM = """
CANDIDATE-MENU MODE (reconstructed historical position — read this, it OVERRIDES
the menu rules above for this prompt):
- The numbered menu below is headed "Candidate:", not "Legal:". It is a
  RECONSTRUCTED, DELIBERATELY INCOMPLETE set of candidate actions built from a
  recorded game history — it is NOT MTGA's legal-action list.
- What is missing from it: mana abilities, floating mana, and activated
  abilities of permanents (together ~41% of a real MTGA menu), plus every
  instant-speed window, target choice, and attacker/blocker declaration. Only
  cards playable from hand at the first priority window of your own precombat
  main phase are listed, plus Pass.
- There are NO "[OK]" affordability tags, because affordability was
  deliberately NOT computed: MTGA's own menu is not mana-gated either. Use the
  "Mana:" line to judge what you can actually pay for; a menu entry you cannot
  afford is still listed.
- Answer with the number of the best entry: {"actions":[{"pick": N}]}. Exactly
  one action. No reasoning field, no prose.
"""

SYSTEM_PROMPT = AUTOPILOT_SYSTEM_PROMPT + "\n" + CANDIDATE_MENU_ADDENDUM

# The exact production trigger string for this decision point (action_planner
# trigger_descriptions["new_turn"]).
TRIGGER_LINE = "TRIGGER: Your turn started (Main Phase 1). Plan your plays."

WUBRG = ("W", "U", "B", "R", "G")


# ---------------------------------------------------------------------------
# Card detail (power/toughness/oracle) -- reuses the resolver's loaded Scryfall
# blobs so no second bulk file is parsed.
# ---------------------------------------------------------------------------


class CardDetail:
    """Extra Scryfall fields the distilled index intentionally drops."""

    def __init__(self, resolver: CardResolver):
        self._raw: dict = getattr(resolver, "_scry_raw", {}) or {}
        self._by_name: dict = getattr(resolver, "_scry_by_name", {}) or {}

    def raw(self, card: Card) -> dict:
        got = self._raw.get(card.grp_id)
        if got is None and card.name:
            got = self._by_name.get(card.name.lower())
        return got or {}

    def pt(self, card: Card) -> str:
        raw = self.raw(card)
        p, t = raw.get("power"), raw.get("toughness")
        if p is None or t is None:
            faces = raw.get("card_faces") or []
            for f in faces:
                if f.get("power") is not None:
                    p, t = f.get("power"), f.get("toughness")
                    break
        return f"{p}/{t}" if p is not None and t is not None else ""

    def oracle(self, card: Card, limit: int) -> str:
        if limit <= 0:
            return ""
        raw = self.raw(card)
        text = raw.get("oracle_text") or ""
        if not text:
            faces = raw.get("card_faces") or []
            text = " // ".join(f.get("oracle_text") or "" for f in faces).strip(" /")
        text = " ".join(text.split())
        if len(text) > limit:
            text = text[: limit - 1].rstrip() + "…"
        return text


# ---------------------------------------------------------------------------
# Zone reconstruction
# ---------------------------------------------------------------------------


@dataclass
class Position:
    """Everything needed to render one decision point."""

    turn: int
    on_play: bool
    hand: list[int] = field(default_factory=list)
    lands: list[int] = field(default_factory=list)
    creatures: list[int] = field(default_factory=list)
    noncreatures: list[int] = field(default_factory=list)
    opp_lands: list[int] = field(default_factory=list)
    opp_creatures: list[int] = field(default_factory=list)
    opp_noncreatures: list[int] = field(default_factory=list)
    life: Optional[float] = None
    opp_life: Optional[float] = None
    opp_hand_count: Optional[float] = None
    lands_played: list[int] = field(default_factory=list)
    casts: list[int] = field(default_factory=list)
    basis: str = "oppo"


def _num(value: str) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def predecessor_prefix(turn: int, on_play: bool, basis: str) -> Optional[str]:
    """Column prefix of the turn that immediately precedes user turn ``turn``.

    Verified against the data: on the play the order is user_1, oppo_1, user_2,
    oppo_2, ...; on the draw it is oppo_1, user_1, oppo_2, user_2, ...  So the
    turn immediately before user turn N is oppo turn N-1 (on the play) or oppo
    turn N (on the draw).

    ``basis="user"`` reproduces the spike's simpler model (previous OWN turn),
    which cannot see anything that happened during the opponent's turn.
    """
    if basis == "user":
        return f"user_turn_{turn - 1}_" if turn > 1 else None
    opp_turn = turn - 1 if on_play else turn
    return f"oppo_turn_{opp_turn}_" if opp_turn >= 1 else None


def build_position(rv: RowView, row: list[str], turn: int, on_play: bool, basis: str) -> Optional[Position]:
    """Reconstruct the board/hand at the top of the user's main phase 1 on turn N."""
    cur = f"user_turn_{turn}_"
    lands_played = rv.ids(row, cur + "lands_played")
    creatures_cast = rv.ids(row, cur + "creatures_cast")
    non_creatures_cast = rv.ids(row, cur + "non_creatures_cast")
    if not (
        lands_played or creatures_cast or non_creatures_cast or rv.get(row, cur + "eot_user_cards_in_hand")
    ):
        return None  # turn never happened

    pre = predecessor_prefix(turn, on_play, basis)
    if pre is None:
        base_hand = rv.ids(row, "opening_hand")
    else:
        base_hand = rv.ids(row, pre + "eot_user_cards_in_hand")
        if not base_hand and turn == 1:
            base_hand = rv.ids(row, "opening_hand")

    # Cards tutored arrive mid-turn; including them mirrors synthesize_menu's
    # choice (excluding them guarantees a false "played card not in hand"
    # whenever a tutor resolves and the fetched card is played the same turn).
    hand = base_hand + rv.ids(row, cur + "cards_drawn") + rv.ids(row, cur + "cards_tutored")

    pos = Position(
        turn=turn,
        on_play=on_play,
        hand=hand,
        lands_played=lands_played,
        casts=creatures_cast + non_creatures_cast,
        basis=basis,
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
    return pos


# ---------------------------------------------------------------------------
# Pick selection
# ---------------------------------------------------------------------------


@dataclass
class PickChoice:
    pick: Optional[tuple[str, Optional[int]]]
    kind: str  # land_drop | cast | pass
    drop_reason: str = ""
    n_casts: int = 0


def choose_pick(pos: Position, multi_cast_policy: str, include_pass: bool) -> PickChoice:
    """Pick the FIRST action of the turn. 17lands records no ordering, so this
    is an explicit heuristic, not ground truth:

      * a land drop, when one happened, is taken as the first action -- real
        main-1 menus put the land drop at 67% of first picks (§19.1) and playing
        the land before spending mana is the overwhelmingly normal line;
      * otherwise a single recorded cast is unambiguous;
      * two or more casts with no land drop is genuinely ambiguous -> dropped.
    """
    if pos.lands_played:
        return PickChoice(("Play", pos.lands_played[0]), "land_drop", n_casts=len(pos.casts))
    if len(pos.casts) == 1:
        return PickChoice(("Cast", pos.casts[0]), "cast", n_casts=1)
    if len(pos.casts) >= 2:
        if multi_cast_policy == "first":
            return PickChoice(("Cast", pos.casts[0]), "cast", n_casts=len(pos.casts))
        return PickChoice(None, "cast", drop_reason="ambiguous_multi_cast", n_casts=len(pos.casts))
    if include_pass:
        return PickChoice(("Pass", None), "pass")
    return PickChoice(None, "pass", drop_reason="no_action_recorded")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def menu_entry_text(resolver: CardResolver, entry: tuple[str, Optional[int]]) -> str:
    kind, gid = entry
    if kind == "Pass":
        return "Pass"
    card = resolver.get(int(gid))
    name = card.name or f"Unknown card (grpId {gid})"
    if kind == "Play":
        return f"Play Land: {name}" if card.is_land else f"Play Land: {name} (MDFC land face)"
    return f"Cast {name}"


def _mana_line(resolver: CardResolver, lands: list[int]) -> str:
    """Mana from lands in play. All lands untap at the start of the turn, so
    this is exact for the top of main 1 -- which is precisely why the position
    is taken there and nowhere else."""
    if not lands:
        return "Mana: 0"
    sources: list[str] = []
    for gid in lands:
        produced = [c for c in (resolver.get(gid).produced_mana or ()) if c in (*WUBRG, "C")]
        sources.append("/".join(produced) if produced else "?")
    display = " ".join(f"{{{s}}}" if "/" in s else s for s in sources)
    return f"Mana: {len(lands)} (sources: {display})"


def _board_lines(resolver: CardResolver, detail: CardDetail, gids: list[int], oracle_chars: int) -> list[str]:
    out: list[str] = []
    counts = Counter(gids)
    for gid in dict.fromkeys(gids):
        card = resolver.get(gid)
        name = card.name or f"grpId {gid}"
        bits = [name]
        n = counts[gid]
        if n > 1:
            bits.append(f"x{n}")
        pt = detail.pt(card)
        if pt:
            bits.append(pt)
        if card.type_line:
            bits.append(f"({card.type_line})")
        text = detail.oracle(card, oracle_chars)
        if text:
            bits.append(f"- {text}")
        out.append("  " + " ".join(bits))
    return out


def _hand_lines(resolver: CardResolver, detail: CardDetail, hand: list[int], oracle_chars: int) -> list[str]:
    out: list[str] = []
    for gid in sorted(hand, key=lambda g: (resolver.get(g).name or "", g)):
        card = resolver.get(gid)
        name = card.name or f"grpId {gid}"
        bits = [name]
        if card.mana_cost:
            bits.append(card.mana_cost)
        pt = detail.pt(card)
        if pt:
            bits.append(pt)
        if card.type_line:
            bits.append(f"({card.type_line})")
        text = detail.oracle(card, oracle_chars)
        if text:
            bits.append(f"- {text}")
        out.append("  " + " ".join(bits))
    return out


def render_user_message(
    resolver: CardResolver,
    detail: CardDetail,
    pos: Position,
    menu: list[tuple[str, Optional[int]]],
    oracle_chars: int,
) -> str:
    """Mirror production's `_format_game_context(for_planner=True)` layout with
    "Legal:" replaced by "Candidate:" (condition 3). This is a documented
    approximation of the production context, not a byte-identical copy: the
    17lands source has no instance ids, no tapped state, no stack and no
    within-turn ordering, so a real GameState cannot be rebuilt from it."""
    lines: list[str] = ["=== GAME ==="]
    menu_lines = "\n".join(f"  {i + 1}. {menu_entry_text(resolver, e)}" for i, e in enumerate(menu))
    lines.append("Candidate: (pick by number — RECONSTRUCTED, NOT a complete legal-action list)")
    lines.append(menu_lines)
    lines.append(f"T{pos.turn} YOUR | Main1 | Pri:You")
    lines.append("Timing: ALL SPELLS")
    you = f"{pos.life:.0f}" if pos.life is not None else "20"
    opp = f"{pos.opp_life:.0f}" if pos.opp_life is not None else "20"
    lines.append(f"Life: You={you} Opp={opp}")
    lines.append(_mana_line(resolver, pos.lands))
    # Production's exact two strings (coach._format_game_context): the land drop
    # is always unused at this decision point, so only these two cases arise.
    lines.append(
        "Land: AVAILABLE"
        if any(k == "Play" for k, _ in menu)
        else "Land: not playable in this window (no Play Land action)"
    )
    lines.append(f"On the {'play' if pos.on_play else 'draw'}.")
    if pos.opp_hand_count is not None:
        lines.append(f"Opp hand: {pos.opp_hand_count:.0f} cards")

    lines.append("")
    lines.append("YOUR BOARD:")
    own = pos.creatures + pos.noncreatures + pos.lands
    lines.extend(_board_lines(resolver, detail, own, oracle_chars) or ["  (empty)"])

    lines.append("")
    lines.append("OPP BOARD:")
    opp_board = pos.opp_creatures + pos.opp_noncreatures + pos.opp_lands
    lines.extend(_board_lines(resolver, detail, opp_board, oracle_chars) or ["  (empty)"])

    lines.append("")
    lines.append("HAND:")
    lines.extend(_hand_lines(resolver, detail, pos.hand, oracle_chars) or ["  (empty)"])

    return TRIGGER_LINE + "\n\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Menu ordering
# ---------------------------------------------------------------------------


def order_menu(
    resolver: CardResolver,
    menu_set: set[tuple[str, Optional[int]]],
    seed: int,
    mode: str,
) -> list[tuple[str, Optional[int]]]:
    """Deterministic menu order.

    Default is a seeded shuffle. A canonical (lands-first) order would put the
    land drop at index 1 in ~2/3 of records, handing the model a positional
    shortcut -- exactly the failure mode §17.1 praised the real corpus for not
    having. The seed is derived from the record key and is stored on the record
    so a permuted twin can be regenerated for the permutation-invariance gate.
    """

    def sort_key(e: tuple[str, Optional[int]]) -> tuple:
        kind, gid = e
        if kind == "Pass":
            return (2, 0, "", 0)
        card = resolver.get(int(gid))
        cost = parse_mana_cost(card.mana_cost or "")
        return (0 if kind == "Play" else 1, cost.total if cost.parse_ok else 99, card.name or "", int(gid))

    entries = sorted(menu_set, key=sort_key)
    if mode == "canonical":
        return entries
    rng = random.Random(seed)
    rng.shuffle(entries)
    return entries


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


@dataclass
class BuildStats:
    games_read: int = 0
    turn_slots_seen: int = 0
    turn_slots_no_data: int = 0
    candidate_decisions: int = 0
    emitted: int = 0  # survived every drop filter (the true population)
    written: int = 0  # actually written to --out (see --emit)
    dropped: Counter = field(default_factory=Counter)
    dropped_by_kind: Counter = field(default_factory=Counter)
    labelled_by_kind: Counter = field(default_factory=Counter)
    casts_observed: int = 0
    kinds: Counter = field(default_factory=Counter)
    ranks: Counter = field(default_factory=Counter)
    expansions: Counter = field(default_factory=Counter)
    by_turn: Counter = field(default_factory=Counter)
    landdrop_by_turn: Counter = field(default_factory=Counter)
    menu_sizes: list[int] = field(default_factory=list)
    pick_indices: Counter = field(default_factory=Counter)
    nontrivial: int = 0
    landdrop_with_choice: int = 0
    prompt_chars: list[int] = field(default_factory=list)


def is_nontrivial(kind: str, menu: list[tuple[str, Optional[int]]]) -> bool:
    """Does this record carry a real choice?

    Deliberately strict, because the alternative is flattering the numbers:

      * a cast pick is always a real decision (which spell, and whether to);
      * a land drop is a real decision ONLY when two or more DISTINCT lands
        were available -- "which land do I play" is a genuine (if small)
        Limited decision. Two copies of the same basic is not a choice, and
        "play your only land" is not a decision at all.

    Everything else is a forced move. Note that even a "non-trivial" land drop
    is a much weaker training signal than a cast: see the honest accounting in
    the module docstring.
    """
    if kind == "cast":
        return True
    distinct_lands = {gid for kind_, gid in menu if kind_ == "Play"}
    return len(distinct_lands) >= 2


def build(
    resolver: CardResolver,
    detail: CardDetail,
    csv_paths: list[Path],
    *,
    min_rank: str,
    max_rows_per_file: int,
    min_turn: int,
    max_turn: int,
    multi_cast_policy: str,
    include_pass: bool,
    menu_order: str,
    oracle_chars: int,
    state_basis: str,
    out: Path,
    nontrivial_out: Optional[Path],
    cast_out: Optional[Path],
    max_records: int,
    emit: str = "all",
) -> BuildStats:
    st = BuildStats()
    out.parent.mkdir(parents=True, exist_ok=True)
    fh_nt = nontrivial_out.open("w", encoding="utf-8") if nontrivial_out else None
    fh_cast = cast_out.open("w", encoding="utf-8") if cast_out else None
    try:
        with out.open("w", encoding="utf-8") as fh:
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
                        pos = build_position(rv, row, turn, on_play, basis=state_basis)
                        if pos is None:
                            st.turn_slots_no_data += 1
                            continue
                        st.candidate_decisions += 1
                        rec = _make_record(
                            resolver,
                            detail,
                            pos,
                            st,
                            game_key=game_key,
                            rank=rank,
                            expansion=expansion,
                            won=rv.get(row, "won"),
                            wr_bucket=rv.get(row, "user_game_win_rate_bucket"),
                            multi_cast_policy=multi_cast_policy,
                            include_pass=include_pass,
                            menu_order=menu_order,
                            oracle_chars=oracle_chars,
                        )
                        if rec is None:
                            continue
                        line = json.dumps(rec, ensure_ascii=False)
                        st.emitted += 1
                        keep = (
                            emit == "all"
                            or (emit == "nontrivial" and rec["meta"]["nontrivial"])
                            or (emit == "cast" and rec["meta"]["pick_kind"] == "cast")
                        )
                        if keep:
                            fh.write(line + "\n")
                            st.written += 1
                        if fh_nt is not None and rec["meta"]["nontrivial"]:
                            fh_nt.write(line + "\n")
                        if fh_cast is not None and rec["meta"]["pick_kind"] == "cast":
                            fh_cast.write(line + "\n")
                        if max_records and st.written >= max_records:
                            logger.info("hit --max-records=%d", max_records)
                            return st
    finally:
        if fh_nt is not None:
            fh_nt.close()
        if fh_cast is not None:
            fh_cast.close()
    return st


def _make_record(
    resolver: CardResolver,
    detail: CardDetail,
    pos: Position,
    st: BuildStats,
    *,
    game_key: str,
    rank: str,
    expansion: str,
    won: str,
    wr_bucket: str,
    multi_cast_policy: str,
    include_pass: bool,
    menu_order: str,
    oracle_chars: int,
) -> Optional[dict]:
    st.casts_observed += len(pos.casts)
    choice = choose_pick(pos, multi_cast_policy, include_pass)
    if choice.pick is None:
        st.dropped[choice.drop_reason] += 1
        st.dropped_by_kind[f"{choice.drop_reason}:{choice.kind}"] += 1
        return None
    st.labelled_by_kind[choice.kind] += 1

    hand_set = set(pos.hand)
    # ---- CONDITION 2: the recorded pick must exist in the reconstructed hand.
    # This is the whole remaining poison term (§19.1: 5.41% end-to-end poison,
    # self-detectable, dropping it takes poison to ~0).
    if choice.pick[1] is not None and choice.pick[1] not in hand_set:
        st.dropped["pick_not_in_hand"] += 1
        st.dropped_by_kind[f"pick_not_in_hand:{choice.kind}"] += 1
        return None

    # ---- CONDITION 1: no affordability filter. "nofilter" is not a tunable.
    menu_set = synth_from_zones(resolver, pos.hand, pos.lands, policy="nofilter", allow_play=True)
    if choice.pick not in menu_set:
        # Same class of defect as condition 2 but caused by the menu model
        # (unresolved card, uncostable card, land face not recognised).
        st.dropped["pick_not_in_menu"] += 1
        st.dropped_by_kind[f"pick_not_in_menu:{choice.kind}"] += 1
        return None

    # Unnamed entries would render as "Unknown card (grpId ...)" and teach the
    # model to pick from garbage. Cheap to detect, so drop the whole decision.
    if any(gid is not None and not resolver.get(int(gid)).name for _, gid in menu_set):
        st.dropped["unresolved_menu_entry"] += 1
        return None
    if any(not resolver.get(g).name for g in hand_set):
        st.dropped["unresolved_hand_card"] += 1
        return None

    if len(menu_set) < 2:
        st.dropped["degenerate_menu"] += 1
        return None

    record_key = f"{game_key}|t{pos.turn}"
    seed = int(hashlib.blake2b(record_key.encode(), digest_size=8).hexdigest(), 16) % (2**31)
    menu = order_menu(resolver, menu_set, seed, menu_order)
    pick_index = menu.index(choice.pick) + 1

    user = render_user_message(resolver, detail, pos, menu, oracle_chars)
    response = json.dumps({"actions": [{"pick": pick_index}]}, ensure_ascii=False)

    nontrivial = is_nontrivial(choice.kind, menu)
    st.kinds[choice.kind] += 1
    st.by_turn[pos.turn] += 1
    st.menu_sizes.append(len(menu))
    st.pick_indices[pick_index] += 1
    st.prompt_chars.append(len(user))
    if choice.kind == "land_drop":
        st.landdrop_by_turn[pos.turn] += 1
        if nontrivial:
            st.landdrop_with_choice += 1
    if nontrivial:
        st.nontrivial += 1

    return {
        "system": SYSTEM_PROMPT,
        "user": user,
        "response": response,
        "source": SOURCE_TAG,
        "attribution": ATTRIBUTION,
        "kind": "play_decision",
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
            "pick_kind": choice.kind,
            "pick_action": [choice.pick[0], choice.pick[1]],
            "menu_size": len(menu),
            "menu_seed": seed,
            "menu_order": menu_order,
            "nontrivial": nontrivial,
            "casts_recorded_this_turn": choice.n_casts,
            "menu_is_candidate_not_legal": True,
        },
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def summarize(st: BuildStats, args: argparse.Namespace, elapsed: float) -> dict:
    def pct(n: int, d: int) -> float:
        return round(100.0 * n / d, 2) if d else 0.0

    considered = st.candidate_decisions
    total_dropped = sum(st.dropped.values())
    landdrops = st.kinds.get("land_drop", 0)
    return {
        "dataset": "play_decisions",
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_sec": round(elapsed, 1),
        "attribution": ATTRIBUTION,
        "source": SOURCE_TAG,
        "source_files": [str(p) for p in args.replay_csv],
        "scope": {
            "decision_point": "first priority window of the player's own precombat main phase",
            "player_turn_range": [args.min_turn, args.max_turn],
            "min_rank": args.min_rank,
            "affordability_filter": "none (condition 1)",
            "ability_activations_emitted": False,
            "pass_rows_included": bool(args.include_pass),
            "multi_cast_policy": args.multi_cast_policy,
            "menu_order": args.menu_order,
            "state_basis": args.state_basis,
        },
        "divergence_from_production": {
            "menu_header": "Candidate: (not Legal:)",
            "menu_is_complete_legal_action_list": False,
            "measured_menu_recall_vs_real_mtga": 0.622,
            "measured_exact_menu_match_pct": 8.6,
            "missing_action_classes": [
                "Activate_Mana",
                "FloatMana",
                "Activate",
                "instant-speed windows",
                "targets",
                "attacker/blocker pairings",
            ],
            "affordability_tags_present": False,
            "measured_end_to_end_pick_recall_before_drop_pct": 94.59,
            "note": "§19.1 of docs/rl-training-status.md. Must be gated on its own terms; NOT prompt-identical to production.",
        },
        "counts": {
            "games_read": st.games_read,
            "turn_slots_seen": st.turn_slots_seen,
            "turn_slots_with_no_data": st.turn_slots_no_data,
            "decisions_considered": considered,
            "dropped_total": total_dropped,
            "dropped_total_pct": pct(total_dropped, considered),
            "dropped_by_reason": dict(st.dropped.most_common()),
            "dropped_pick_not_in_hand": st.dropped.get("pick_not_in_hand", 0),
            "dropped_pick_not_in_hand_pct_of_considered": pct(
                st.dropped.get("pick_not_in_hand", 0), considered
            ),
            "dropped_pick_not_in_hand_pct_of_labelled": pct(
                st.dropped.get("pick_not_in_hand", 0), sum(st.labelled_by_kind.values())
            ),
            # The headline 0.1%-ish figure is dominated by land drops, which are
            # trivially self-consistent. The condition-2 rate that matters is the
            # one on CAST picks -- that is where §19.1 measured the 5.41% poison.
            "condition2_drop_rate_by_pick_kind_pct": {
                kind: pct(st.dropped_by_kind.get(f"pick_not_in_hand:{kind}", 0), n)
                for kind, n in sorted(st.labelled_by_kind.items())
            },
            "dropped_by_reason_and_pick_kind": dict(st.dropped_by_kind.most_common()),
            "recorded_casts_seen_in_labelled_turns": st.casts_observed,
            "recorded_casts_not_labelled": st.casts_observed - st.kinds.get("cast", 0),
            "recorded_casts_not_labelled_note": (
                "casts that happened on a labelled turn but were not the FIRST action; "
                "17lands records no within-turn ordering, and condition 4 restricts the "
                "scope to the first priority window, so they are unusable here"
            ),
            "emitted": st.emitted,
            "written_to_out": st.written,
            "emit_filter": args.emit,
        },
        "composition": {
            "READ_THIS_FIRST": (
                "Within the mandated scope (first priority window of own precombat main, "
                "player-turn <= 5) the land drop is the first action of almost every turn, so "
                "this dataset is dominated by land drops. `cast_records` is the only subset "
                "that is unambiguously a spell decision; `nontrivial_records` adds land drops "
                "where 2+ DISTINCT lands were available. Do not quote `emitted` as a count of "
                "strategic decisions."
            ),
            "pick_kind": dict(st.kinds.most_common()),
            "land_drop_share_pct": pct(landdrops, st.emitted),
            "trivial_forced_land_drop_records": landdrops - st.landdrop_with_choice,
            "trivial_forced_land_drop_share_pct": pct(landdrops - st.landdrop_with_choice, st.emitted),
            "nontrivial_records": st.nontrivial,
            "nontrivial_share_pct": pct(st.nontrivial, st.emitted),
            "cast_records": st.kinds.get("cast", 0),
            "cast_share_pct": pct(st.kinds.get("cast", 0), st.emitted),
            "land_drop_with_real_choice": st.landdrop_with_choice,
            "rank_distribution_games": dict(st.ranks.most_common()),
            "expansion_distribution_games": dict(st.expansions.most_common()),
            "records_by_turn": {str(k): v for k, v in sorted(st.by_turn.items())},
            "land_drop_share_by_turn_pct": {
                str(t): pct(st.landdrop_by_turn.get(t, 0), n) for t, n in sorted(st.by_turn.items())
            },
        },
        "menu": {
            "mean_menu_size": round(statistics.fmean(st.menu_sizes), 2) if st.menu_sizes else 0,
            "median_menu_size": statistics.median(st.menu_sizes) if st.menu_sizes else 0,
            "min_menu_size": min(st.menu_sizes) if st.menu_sizes else 0,
            "max_menu_size": max(st.menu_sizes) if st.menu_sizes else 0,
            "pick_index_distribution": dict(sorted(st.pick_indices.items())),
            "modal_pick_index_share_pct": pct(
                max(st.pick_indices.values()) if st.pick_indices else 0, st.emitted
            ),
        },
        "prompt": {
            "system_prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
            "autopilot_system_prompt_sha256": hashlib.sha256(AUTOPILOT_SYSTEM_PROMPT.encode()).hexdigest(),
            "mean_user_chars": round(statistics.fmean(st.prompt_chars), 1) if st.prompt_chars else 0,
            "median_user_chars": statistics.median(st.prompt_chars) if st.prompt_chars else 0,
            "max_user_chars": max(st.prompt_chars) if st.prompt_chars else 0,
        },
    }


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--replay-csv",
        type=Path,
        action="append",
        required=True,
        help="17lands replay_data csv(.gz); repeatable",
    )
    ap.add_argument(
        "--min-rank",
        default="diamond",
        choices=[*RANK_ORDER, ""],
        help="keep games at this rank or above (default: diamond -> diamond+mythic)",
    )
    ap.add_argument("--max-rows-per-file", type=int, default=3500, help="max qualifying GAMES read per csv")
    ap.add_argument("--min-turn", type=int, default=1)
    ap.add_argument(
        "--max-turn", type=int, default=5, help="condition 4: pick recall degrades after player-turn 5"
    )
    ap.add_argument(
        "--multi-cast-policy",
        choices=("drop", "first"),
        default="drop",
        help="turns with >=2 casts and no land drop have no recoverable ordering",
    )
    ap.add_argument(
        "--include-pass",
        action="store_true",
        help="emit Pass labels for turns with no recorded action (off by default: "
        "deliberate inaction is indistinguishable from having no play)",
    )
    ap.add_argument("--menu-order", choices=("shuffled", "canonical"), default="shuffled")
    ap.add_argument(
        "--state-basis",
        choices=("oppo", "user"),
        default="oppo",
        help="'oppo' rebuilds the position from the intervening OPPONENT turn's "
        "end-of-turn snapshot (fresher: sees removal, discards and instants "
        "from that turn). 'user' reproduces synthesize_menu's simpler "
        "previous-own-turn basis, for comparison.",
    )
    ap.add_argument(
        "--oracle-chars",
        type=int,
        default=180,
        help="max oracle-text chars per card (0 disables oracle text)",
    )
    ap.add_argument(
        "--emit",
        choices=("all", "nontrivial", "cast"),
        default="all",
        help="which records go to --out. 'all' is the honest, representative "
        "distribution (dominated by land drops); 'cast' collects the "
        "strategic subset from a deep scan",
    )
    ap.add_argument("--max-records", type=int, default=0, help="0 = unlimited (counts --out rows)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument(
        "--nontrivial-out",
        type=Path,
        default=None,
        help="also write the non-trivial (real-decision) subset here",
    )
    ap.add_argument(
        "--cast-out",
        type=Path,
        default=None,
        help="also write the spell-decision-only subset here — the only subset "
        "that is unambiguously strategic",
    )
    ap.add_argument(
        "--json-out", type=Path, default=None, help="also write a JSON list (the shape train.py consumes)"
    )
    ap.add_argument("--stats-out", type=Path, default=None)
    ap.add_argument("--verbose", action="store_true")
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s"
    )
    t0 = time.time()
    resolver = CardResolver()
    detail = CardDetail(resolver)

    st = build(
        resolver,
        detail,
        args.replay_csv,
        min_rank=args.min_rank,
        max_rows_per_file=args.max_rows_per_file,
        min_turn=args.min_turn,
        max_turn=args.max_turn,
        multi_cast_policy=args.multi_cast_policy,
        include_pass=args.include_pass,
        menu_order=args.menu_order,
        oracle_chars=args.oracle_chars,
        state_basis=args.state_basis,
        out=args.out,
        nontrivial_out=args.nontrivial_out,
        cast_out=args.cast_out,
        max_records=args.max_records,
        emit=args.emit,
    )
    resolver.save()

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open() as fh, args.json_out.open("w") as jf:
            json.dump([json.loads(line) for line in fh], jf, ensure_ascii=False)

    stats = summarize(st, args, time.time() - t0)
    text = json.dumps(stats, indent=2)
    print(text)
    if args.stats_out:
        args.stats_out.parent.mkdir(parents=True, exist_ok=True)
        args.stats_out.write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
