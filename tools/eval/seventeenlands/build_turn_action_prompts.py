"""Build a turn-action prompt corpus from 17lands replay data.

For each winning diamond+ game in the replay CSV, sample three turns
(early ~T2, mid ~T(num_turns/2), late ~T(num_turns-1)) and reconstruct
the state at the start of each. The coach is asked which action *categories*
to take that turn; the actually-played categories (derivable from the per-
turn count columns) are the ground truth.

Action categories:
    PLAY_LAND       -> user_turn_N_lands_played > 0
    CAST_CREATURE   -> user_turn_N_creatures_cast > 0
    CAST_SPELL      -> user_turn_N_non_creatures_cast > 0
                       OR user_turn_N_user_instants_sorceries_cast > 0
    ATTACK          -> user_turn_N_creatures_attacked > 0
    ACTIVATE        -> user_turn_N_user_abilities > 0
    PASS            -> none of the above (drew, did nothing, ended turn)

Usage:
    python -m tools.eval.seventeenlands.build_turn_action_prompts \\
        --csv tools/eval/data/17lands/replay_data_public.EOE.PremierDraft.csv.gz \\
        --out tools/eval/data/turn_action_prompts.jsonl \\
        --n 200
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tools.eval.seventeenlands.loader import (  # noqa: E402
    RANK_ORDER,
    iter_rows,
    parse_bool,
    parse_grpid_list,
    rank_tier,
)

from arenamcp.card_db import get_card_database  # noqa: E402

logger = logging.getLogger("eval.17l.turn_action")


SYSTEM_PROMPT = (
    "You are a Magic: The Gathering Arena coach. Given the state at the "
    "start of the user's turn, recommend which action categories the user "
    "should take this turn.\n\n"
    "You have priority on YOUR OWN Main Phase 1. High-rank Limited players "
    "take 2-4 actions on most main-phase turns — typically PLAY_LAND + at "
    "least one CAST + ATTACK if creatures are available. **PASS-only turns "
    "are very rare** (under 5% at diamond+ rank).\n\n"
    "Reply on the FIRST LINE with comma-separated tags from this exact set, "
    "in any order:\n"
    "  PLAY_LAND, CAST_CREATURE, CAST_SPELL, ATTACK, ACTIVATE, PASS\n\n"
    "Use PASS only when you genuinely have nothing to do this turn (no land "
    "in hand AND no castable spell AND no creature that can attack AND no "
    "ability worth activating). Combine multiple tags freely (e.g. "
    "'PLAY_LAND, CAST_CREATURE, ATTACK'). Then on subsequent lines give one "
    "short sentence of reasoning. Default to action — pass should require a "
    "specific justification."
)


# ---------------------------------------------------------------------------
# Column projection — pick only the per-turn fields we actually need.
# ---------------------------------------------------------------------------

_STATIC_COLS = (
    "expansion",
    "event_type",
    "rank",
    "opp_rank",
    "on_play",
    "won",
    "num_mulligans",
    "num_turns",
    "opening_hand",
    "main_colors",
    "splash_colors",
    "opp_colors",
    "draft_id",
    "match_number",
    "game_number",
)

# Per-turn suffixes (after `(user|oppo)_turn_N_`) that we project from the CSV.
_TURN_SUFFIXES = frozenset(
    {
        # End-of-turn state snapshot
        "eot_user_life",
        "eot_oppo_life",
        "eot_user_lands_in_play",
        "eot_oppo_lands_in_play",
        "eot_user_creatures_in_play",
        "eot_oppo_creatures_in_play",
        "eot_user_non_creatures_in_play",
        "eot_oppo_non_creatures_in_play",
        "eot_user_cards_in_hand",
        "eot_oppo_cards_in_hand",
        # Action counts
        "lands_played",
        "creatures_cast",
        "non_creatures_cast",
        "user_instants_sorceries_cast",
        "oppo_instants_sorceries_cast",
        "user_abilities",
        "oppo_abilities",
        "creatures_attacked",
        "creatures_blocked",
        "user_combat_damage_taken",
        "oppo_combat_damage_taken",
        # Card-name fields
        "cards_drawn",
        "cards_drawn_or_tutored",
        "cards_tutored",
        "cards_discarded",
    }
)

_TURN_RE = re.compile(r"^(user|oppo)_turn_(\d+)_(.+)$")


def _needed_columns(header: list[str], max_turn: int) -> list[str]:
    cols = [c for c in _STATIC_COLS if c in header]
    for c in header:
        m = _TURN_RE.match(c)
        if not m:
            continue
        if int(m.group(2)) > max_turn:
            continue
        if m.group(3) in _TURN_SUFFIXES:
            cols.append(c)
    return cols


def _peek_header(csv_gz: Path) -> list[str]:
    """Read just the CSV header for column allowlisting."""
    import csv as _csv
    import gzip

    with gzip.open(csv_gz, "rt", encoding="utf-8", newline="") as f:
        return next(_csv.reader(f))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_int(v) -> int:
    """Parse a numeric scalar field (e.g. '20.0', '20', ''). Returns 0 on miss."""
    if v is None:
        return 0
    s = str(v).strip()
    if not s:
        return 0
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except ValueError:
            return 0


def _count_list(v) -> int:
    """Count items in a pipe-separated list field (17lands per-turn lists).

    Most per-turn columns (lands_played, creatures_cast, eot_user_lands_in_play,
    eot_user_cards_in_hand, ...) are pipe-separated grpids/instance-ids, NOT
    counts. Returns the number of non-empty items.
    """
    if v is None:
        return 0
    s = str(v).strip()
    if not s:
        return 0
    return sum(1 for tok in s.split("|") if tok.strip())


def _sample_turns(num_turns: int) -> list[int]:
    """Pick early / mid / late turns. Always 1-3 turns, deduped."""
    if num_turns <= 0:
        return []
    if num_turns < 3:
        return list(range(1, num_turns + 1))
    early = 2
    mid = max(early + 1, num_turns // 2)
    late = max(mid + 1, num_turns - 1)
    seen: set[int] = set()
    out: list[int] = []
    for t in (early, mid, late):
        t = max(1, min(num_turns, t))
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


# Per-process memo so we don't re-hit the SQLite MTGA DB for every prompt's
# 7+N drawn cards. A set has only ~600 distinct grpids.
_RESOLVE_CACHE: dict[int, tuple[str, str]] = {}


def _resolve_grpid(grpid: int, cdb) -> tuple[str, str]:
    """Return (name, kind) for a grpid; kind in {land, creature, spell}."""
    cached = _RESOLVE_CACHE.get(grpid)
    if cached is not None:
        return cached
    info = cdb.get_card_by_arena_id(grpid) if cdb else None
    if info is None:
        cached = (f"Unknown({grpid})", "spell")
    else:
        name = getattr(info, "name", None) or f"Unknown({grpid})"
        type_line = (getattr(info, "type_line", "") or "").lower()
        if "land" in type_line:
            kind = "land"
        elif "creature" in type_line:
            kind = "creature"
        else:
            kind = "spell"
        cached = (name, kind)
    _RESOLVE_CACHE[grpid] = cached
    return cached


def _resolve_names(grpids: list[int], cdb) -> list[str]:
    return [_resolve_grpid(g, cdb)[0] for g in grpids]


def _classify(grpid: int, cdb) -> str:
    """Classify a card grpid as 'land', 'creature', or 'spell'."""
    return _resolve_grpid(grpid, cdb)[1]


def _resolve_with_types(grpids: list[int], cdb) -> list[tuple[int, str, str]]:
    """Return [(grpid, name, kind)] for each grpid; kind is land/creature/spell."""
    out: list[tuple[int, str, str]] = []
    for g in grpids:
        name, kind = _resolve_grpid(g, cdb)
        out.append((g, name, kind))
    return out


def _action_set(row: dict, n: int) -> set[str]:
    """Extract the actually-played category set for user turn N.

    Per-turn fields are pipe-separated grpid/instance-id lists. Use
    ``_count_list`` not ``_to_int`` (those columns are not numeric scalars).
    """
    p = f"user_turn_{n}_"
    s: set[str] = set()
    if _count_list(row.get(p + "lands_played")) > 0:
        s.add("PLAY_LAND")
    if _count_list(row.get(p + "creatures_cast")) > 0:
        s.add("CAST_CREATURE")
    if _count_list(row.get(p + "non_creatures_cast")) > 0:
        s.add("CAST_SPELL")
    if _count_list(row.get(p + "user_instants_sorceries_cast")) > 0:
        s.add("CAST_SPELL")
    if _count_list(row.get(p + "creatures_attacked")) > 0:
        s.add("ATTACK")
    if _count_list(row.get(p + "user_abilities")) > 0:
        s.add("ACTIVATE")
    if not s:
        s.add("PASS")
    return s


def _format_prompt(row: dict, n: int, cdb) -> str:
    on_play = parse_bool(row.get("on_play", "")) is True
    main_colors = row.get("main_colors") or "(unknown)"
    splash = row.get("splash_colors") or ""
    opp_colors = row.get("opp_colors") or "(unknown)"

    # State at start of turn N: use end-of-prior-turn snapshots.
    # Per-turn list fields (lands_in_play, creatures_in_play, cards_in_hand,
    # ...) are pipe-separated grpid/instance-id lists, not scalars.
    prev = max(1, n - 1)
    user_p = f"user_turn_{prev}_"
    if n == 1:
        user_life = 20
        opp_life = 20
        user_lands = 0
        opp_lands = 0
        user_creatures = 0
        opp_creatures = 0
        opp_hand = 7
        # Hand at start of turn 1 = the opening hand (post mulligan).
        hand_grpids = list(parse_grpid_list(row.get("opening_hand", "")))
    else:
        user_life = _to_int(row.get(user_p + "eot_user_life")) or 20
        opp_life = _to_int(row.get(user_p + "eot_oppo_life")) or 20
        user_lands = _count_list(row.get(user_p + "eot_user_lands_in_play"))
        opp_lands = _count_list(row.get(user_p + "eot_oppo_lands_in_play"))
        user_creatures = _count_list(row.get(user_p + "eot_user_creatures_in_play"))
        opp_creatures = _count_list(row.get(user_p + "eot_oppo_creatures_in_play"))
        opp_hand = _count_list(row.get(user_p + "eot_oppo_cards_in_hand"))
        # End-of-prior-user-turn cards-in-hand IS the start-of-turn-N hand
        # (before the draw step). Add this turn's cards_drawn/tutored to get
        # the effective hand on Main Phase 1.
        hand_grpids = list(parse_grpid_list(row.get(user_p + "eot_user_cards_in_hand", "")))
        hand_grpids.extend(parse_grpid_list(row.get(f"user_turn_{n}_cards_drawn", "")))
        hand_grpids.extend(parse_grpid_list(row.get(f"user_turn_{n}_cards_tutored", "")))

    # Render the hand by category. Names for unknown grpids fall through to
    # 'Unknown(N)' — that's still useful signal (count + colors).
    typed = _resolve_with_types(hand_grpids, cdb)
    hand_lands = [name for _, name, k in typed if k == "land"]
    hand_creatures = [name for _, name, k in typed if k == "creature"]
    hand_spells = [name for _, name, k in typed if k == "spell"]
    user_hand = len(hand_grpids)

    def _hand_section(label: str, names: list[str]) -> str:
        if not names:
            return f"  {label} (0): (none)"
        return f"  {label} ({len(names)}): {', '.join(names)}"

    hand_block = "\n".join(
        [
            _hand_section("LAND", hand_lands),
            _hand_section("CREATURE", hand_creatures),
            _hand_section("OTHER SPELL", hand_spells),
        ]
    )

    # Opp's prior turn summary.
    if n == 1 and on_play:
        opp_summary = "Opponent has not acted yet."
    else:
        opp_turn_n = n - 1 if on_play else n
        op = f"oppo_turn_{opp_turn_n}_"
        opp_lands_played = _count_list(row.get(op + "lands_played"))
        opp_creatures_cast = _count_list(row.get(op + "creatures_cast"))
        opp_spells_cast = _count_list(row.get(op + "non_creatures_cast")) + _count_list(
            row.get(op + "oppo_instants_sorceries_cast")
        )
        opp_attacks = _count_list(row.get(op + "creatures_attacked"))
        damage_we_took = _to_int(row.get(op + "user_combat_damage_taken"))
        # opp's cards_drawn_or_tutored is a SCALAR count (we don't know what
        # the opponent drew), unlike the user's pipe-separated grpid lists.
        opp_drew_count = _to_int(row.get(op + "cards_drawn_or_tutored"))
        opp_summary = (
            f"On opponent's last turn (T{opp_turn_n}): "
            f"played {opp_lands_played} land(s), "
            f"cast {opp_creatures_cast} creature(s), "
            f"cast {opp_spells_cast} non-creature spell(s), "
            f"attacked with {opp_attacks} creature(s), "
            f"dealt {damage_we_took} combat damage to you, "
            f"drew {opp_drew_count} card(s)."
        )

    play_str = "play" if on_play else "draw"
    deck_colors = main_colors + (f" (splash {splash})" if splash else "")

    prompt = (
        f"It is the START of your turn {n}, Main Phase 1. You have priority "
        f"and must decide which action(s) to take. You are on the {play_str}.\n"
        f"\n"
        f"Your deck colors: {deck_colors}.  Opponent's deck colors: {opp_colors}.\n"
        f"\n"
        f"BOARD STATE at start of your turn {n}:\n"
        f"  You — life {user_life}, lands in play {user_lands}, creatures in play {user_creatures}, "
        f"hand size {user_hand}\n"
        f"  Opp — life {opp_life}, lands in play {opp_lands}, creatures in play {opp_creatures}, "
        f"hand size {opp_hand}\n"
        f"\n"
        f"OPPONENT'S LAST TURN: {opp_summary}\n"
        f"\n"
        f"YOUR HAND at start of turn {n} (after this turn's draw):\n"
        f"{hand_block}\n"
        f"\n"
        f"With those resources, what action(s) should you take this turn? "
        f"Output the tags per the system-prompt format."
    )
    return prompt


# ---------------------------------------------------------------------------
# Main build
# ---------------------------------------------------------------------------


def build(
    csv_gz: Path,
    out_path: Path,
    n_samples: int,
    min_rank: str,
    max_rows_scan: Optional[int],
    seed: int,
    max_turn: int,
) -> None:
    rng = random.Random(seed)
    cdb = get_card_database()

    header = _peek_header(csv_gz)
    cols = _needed_columns(header, max_turn)
    logger.info("projecting %d / %d columns (max_turn=%d)", len(cols), len(header), max_turn)

    rows_seen = 0
    rows_eligible = 0
    eligible: list[tuple[dict, list[int]]] = []  # (row, sampled_turns)

    for row in iter_rows(
        csv_gz,
        cols,
        limit=max_rows_scan,
        where={"event_type": "PremierDraft"},
    ):
        rows_seen += 1
        if parse_bool(row.get("won", "")) is not True:
            continue
        if not rank_tier(row.get("rank", ""), min_tier=min_rank):
            continue
        nt = _to_int(row.get("num_turns"))
        if nt < 5:
            continue
        sampled = [t for t in _sample_turns(nt) if t <= max_turn]
        if not sampled:
            continue
        eligible.append((row, sampled))
        rows_eligible += 1

    logger.info("rows_seen=%d eligible=%d", rows_seen, rows_eligible)
    if not eligible:
        logger.error("no eligible rows; relax filters")
        sys.exit(2)

    # We sample N decisions (a "decision" = one sampled turn from one row).
    # Up to 3 decisions per row. Prefer diversity by drawing rows uniformly,
    # then taking up to 3 of their sampled turns until we hit n_samples.
    rng.shuffle(eligible)
    picked: list[tuple[dict, int]] = []
    for row, turns in eligible:
        for t in turns:
            picked.append((row, t))
            if len(picked) >= n_samples:
                break
        if len(picked) >= n_samples:
            break

    logger.info("picked %d decisions across %d games", len(picked), len({id(r) for r, _ in picked}))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for i, (row, n) in enumerate(picked):
            actions = sorted(_action_set(row, n))
            user_text = _format_prompt(row, n, cdb)
            record = {
                "id": f"17l-turn-{i:04d}-t{n}",
                "system": SYSTEM_PROMPT,
                "user": user_text,
                "max_tokens": 200,
                "temperature": 0.0,
                "meta": {
                    "source": "17lands",
                    "kind": "turn_action",
                    "turn": n,
                    "actually_did": actions,
                    "rank": row.get("rank", ""),
                    "draft_id": row.get("draft_id", ""),
                    "match_number": row.get("match_number", ""),
                    "game_number": row.get("game_number", ""),
                    "num_turns": _to_int(row.get("num_turns")),
                    "on_play": parse_bool(row.get("on_play", "")),
                },
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"wrote {len(picked)} prompts to {out_path}")
    # Surface the action-set distribution so we can catch trivial baselines
    # like "PASS dominates the dataset".
    from collections import Counter

    set_counts: Counter = Counter(tuple(sorted(_action_set(r, n))) for r, n in picked)
    print("\nActual-action-set distribution (top 10):")
    for tags, count in set_counts.most_common(10):
        pct = count / len(picked) * 100
        print(f"  {','.join(tags):40} {count:4d} ({pct:.1f}%)")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--n", dest="n_samples", type=int, default=200)
    parser.add_argument("--min-rank", default="diamond", choices=RANK_ORDER)
    parser.add_argument("--max-rows-scan", type=int, default=None)
    parser.add_argument(
        "--max-turn", type=int, default=15, help="Cap turn columns we project from CSV (default 15)"
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    build(
        csv_gz=args.csv,
        out_path=args.out,
        n_samples=args.n_samples,
        min_rank=args.min_rank,
        max_rows_scan=args.max_rows_scan,
        seed=args.seed,
        max_turn=args.max_turn,
    )


if __name__ == "__main__":
    main()
