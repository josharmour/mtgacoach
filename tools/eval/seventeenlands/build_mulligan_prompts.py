"""Build a mulligan-eval prompt corpus from 17lands replay data.

Strategy:

  1. Stream the 17lands CSV, keeping only games at >= ``--min-rank`` (default
     diamond) so the "actually played" decisions are higher-quality ground
     truth.
  2. For each row, extract the candidate-1 hand (the hand the user was first
     offered before any mulligans). The decision is "kept" if num_mulligans
     == 0 else "mulled".
  3. Bucket rows by (num_lands_in_hand, on_play, color_count). Compute, per
     bucket, the empirical win-rate of keeping vs mulling that hand profile.
     The "correct" answer for a bucket = the higher-WR option, provided the
     bucket has enough samples to be trustworthy (--min-bucket-n).
  4. Sample N games stratified across buckets. For each, build a coach
     mulligan prompt using card names (resolved from grpids) and embed the
     bucket's WR pair + the actually-played decision in the meta block.

The resulting prompts.jsonl is compatible with ``tools.eval.run`` — it has
the standard ``id``, ``system``, ``user`` fields and an extra ``meta``
block consumed by ``score_mulligan.py``.

Usage:
    python -m tools.eval.seventeenlands.build_mulligan_prompts \\
        --csv tools/eval/data/17lands/replay_data_public.EOE.PremierDraft.csv.gz \\
        --out tools/eval/data/mulligan_prompts.jsonl \\
        --n 200
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from collections import defaultdict
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

logger = logging.getLogger("eval.17l.mulligan")


# Columns we project out of the giant CSV
NEEDED_COLS = (
    "expansion",
    "event_type",
    "rank",
    "on_play",
    "num_mulligans",
    "main_colors",
    "splash_colors",
    "won",
    "num_turns",
    "candidate_hand_1",
    "draft_id",
    "match_number",
    "game_number",
)


SYSTEM_PROMPT = (
    "You are a Magic: The Gathering Arena coach. The user is being offered "
    "a 7-card opening hand and must choose to KEEP or MULLIGAN. Reply with "
    "exactly one word on the first line — KEEP or MULLIGAN — followed by "
    "one short sentence of reasoning. Do not invent cards that aren't in the "
    "hand. Consider land count, curve, and the deck's color requirements."
)


def _color_count(main_colors: str, splash: str) -> int:
    s = "".join(c for c in (main_colors or "") + (splash or "") if c.isalpha())
    return len(set(s.upper()))


def _is_land(card_info) -> bool:
    type_line = (getattr(card_info, "type_line", "") or "").lower()
    return "land" in type_line


# Per-process memo so the pass-1 scan doesn't hit the SQLite-on-/mnt/c MTGA DB
# 21M times. A set has only ~600 distinct grpids; caching collapses those down
# to one lookup each.
_RESOLVE_CACHE: dict[int, tuple[str, bool]] = {}


def _resolve_hand(grpids: list[int], cdb) -> list[tuple[int, str, bool]]:
    """Look up grpids -> (grpid, name, is_land). Unresolved grpids stay as
    ``Unknown(<grpid>)``."""
    out: list[tuple[int, str, bool]] = []
    for g in grpids:
        cached = _RESOLVE_CACHE.get(g)
        if cached is None:
            info = cdb.get_card_by_arena_id(g) if cdb else None
            if info is None:
                cached = (f"Unknown({g})", False)
            else:
                name = getattr(info, "name", None) or f"Unknown({g})"
                cached = (name, _is_land(info))
            _RESOLVE_CACHE[g] = cached
        out.append((g, cached[0], cached[1]))
    return out


def _bucket_key(num_lands: int, on_play: bool, color_count: int) -> tuple:
    # Cap land count buckets at 7 (full hand of lands is rare anyway)
    return (min(num_lands, 7), bool(on_play), color_count)


def _format_prompt(
    hand_resolved: list[tuple[int, str, bool]],
    on_play: bool,
    main_colors: str,
    splash: str,
) -> str:
    play_str = "play" if on_play else "draw"
    card_lines = ", ".join(name for _, name, _ in hand_resolved)
    colors = main_colors or "(unknown)"
    if splash:
        colors = f"{colors} (splash {splash})"
    return (
        f"On the {play_str}. Opening hand (7 cards): {card_lines}.\n"
        f"Deck colors: {colors}.\n"
        f"Mulligan options: KEEP, MULLIGAN to 6.\n"
    )


def _summarize_buckets(bucket_stats: dict, min_bucket_n: int) -> dict[tuple, dict]:
    """Compute per-bucket {keep_n, keep_wins, mull_n, mull_wins, correct}."""
    summary: dict[tuple, dict] = {}
    for key, s in bucket_stats.items():
        keep_n = s["keep_n"]
        mull_n = s["mull_n"]
        keep_wr = s["keep_wins"] / keep_n if keep_n else None
        mull_wr = s["mull_wins"] / mull_n if mull_n else None
        # Need both arms to assert a "correct" option, and need enough samples
        # in each arm to be trustworthy.
        correct: Optional[str] = None
        if keep_wr is not None and mull_wr is not None and keep_n >= min_bucket_n and mull_n >= min_bucket_n:
            if keep_wr > mull_wr:
                correct = "keep"
            elif mull_wr > keep_wr:
                correct = "mull"
            # Tie -> leave correct=None (don't score)
        summary[key] = {
            "keep_n": keep_n,
            "mull_n": mull_n,
            "keep_wr": round(keep_wr, 4) if keep_wr is not None else None,
            "mull_wr": round(mull_wr, 4) if mull_wr is not None else None,
            "correct": correct,
        }
    return summary


def _bucket_label(key: tuple) -> str:
    nl, op, cc = key
    return f"lands={nl} {'play' if op else 'draw'} colors={cc}"


def build(
    csv_gz: Path,
    out_path: Path,
    n_samples: int,
    min_rank: str,
    min_bucket_n: int,
    max_rows_scan: Optional[int],
    seed: int,
) -> None:
    rng = random.Random(seed)

    cdb = get_card_database()

    # Pass 1: scan the dataset, accumulate per-bucket stats and a pool of
    # eligible rows (rank-filtered, candidate_hand_1 well-formed). We keep
    # rows lean — only the fields needed to materialize prompts later.
    logger.info("pass 1: scanning dataset for bucket stats + eligible rows")

    bucket_stats: dict[tuple, dict] = defaultdict(
        lambda: {"keep_n": 0, "keep_wins": 0, "mull_n": 0, "mull_wins": 0}
    )
    eligible: list[dict] = []

    rows_seen = 0
    for row in iter_rows(
        csv_gz,
        NEEDED_COLS,
        limit=max_rows_scan,
        where={"event_type": "PremierDraft"},
    ):
        rows_seen += 1
        if not rank_tier(row.get("rank", ""), min_tier=min_rank):
            continue
        on_play = parse_bool(row.get("on_play", ""))
        won = parse_bool(row.get("won", ""))
        if on_play is None or won is None:
            continue
        hand = parse_grpid_list(row.get("candidate_hand_1", ""))
        if len(hand) != 7:
            continue

        try:
            num_mulligans = int(row.get("num_mulligans", "0") or "0")
        except ValueError:
            continue
        kept = num_mulligans == 0

        # Bucket on land count, on_play, color count. Resolving 7 grpids per
        # row is cheap — the card_db is in-process.
        hand_resolved = _resolve_hand(hand, cdb)
        num_lands = sum(1 for _, _, is_land in hand_resolved if is_land)
        cc = _color_count(row.get("main_colors", ""), row.get("splash_colors", ""))
        key = _bucket_key(num_lands, on_play, cc)

        b = bucket_stats[key]
        if kept:
            b["keep_n"] += 1
            if won:
                b["keep_wins"] += 1
        else:
            b["mull_n"] += 1
            if won:
                b["mull_wins"] += 1

        eligible.append(
            {
                "key": key,
                "hand": hand,
                "hand_resolved": [(g, n) for g, n, _ in hand_resolved],
                "on_play": on_play,
                "main_colors": row.get("main_colors", ""),
                "splash_colors": row.get("splash_colors", ""),
                "kept": kept,
                "won": won,
                "rank": row.get("rank", ""),
                "draft_id": row.get("draft_id", ""),
                "match_number": row.get("match_number", ""),
                "game_number": row.get("game_number", ""),
                "num_lands": num_lands,
            }
        )

    summary = _summarize_buckets(bucket_stats, min_bucket_n)
    logger.info(
        "pass 1 done: rows_seen=%d eligible=%d buckets=%d (with min_n=%d on both arms)",
        rows_seen,
        len(eligible),
        len(summary),
        min_bucket_n,
    )

    # Filter eligible rows to ones whose bucket has a determinable "correct"
    # answer. We can't score the others.
    scorable = [r for r in eligible if summary[r["key"]]["correct"]]
    logger.info("scorable rows (bucket has correct option): %d", len(scorable))

    if not scorable:
        logger.error(
            "no scorable rows — try lowering --min-bucket-n or scanning more rows. Bucket summary so far:"
        )
        for key in sorted(bucket_stats):
            s = summary[key]
            logger.error(
                "  %s: keep n=%d wr=%s, mull n=%d wr=%s, correct=%s",
                _bucket_label(key),
                s["keep_n"],
                s["keep_wr"],
                s["mull_n"],
                s["mull_wr"],
                s["correct"],
            )
        sys.exit(2)

    # Stratified sampling: try to spread sampled rows across buckets so we
    # don't get N samples from one over-represented bucket. Group by key,
    # sample proportionally.
    by_key: dict[tuple, list[dict]] = defaultdict(list)
    for r in scorable:
        by_key[r["key"]].append(r)

    # Round-robin pick across buckets until we hit N or run out.
    keys = list(by_key.keys())
    rng.shuffle(keys)
    for k in keys:
        rng.shuffle(by_key[k])
    picked: list[dict] = []
    while len(picked) < n_samples and any(by_key[k] for k in keys):
        for k in keys:
            if not by_key[k]:
                continue
            picked.append(by_key[k].pop())
            if len(picked) >= n_samples:
                break

    logger.info("picked %d prompts (requested %d)", len(picked), n_samples)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for i, r in enumerate(picked):
            key = r["key"]
            b = summary[key]
            user_text = _format_prompt(
                [(g, n, False) for g, n in r["hand_resolved"]],
                r["on_play"],
                r["main_colors"],
                r["splash_colors"],
            )
            record = {
                "id": f"17l-mull-{i:04d}",
                "system": SYSTEM_PROMPT,
                "user": user_text,
                "max_tokens": 200,
                "temperature": 0.0,
                "meta": {
                    "source": "17lands",
                    "kind": "mulligan",
                    "bucket": {
                        "num_lands_in_hand": key[0],
                        "on_play": key[1],
                        "color_count": key[2],
                    },
                    "bucket_stats": b,
                    "actually_played": "keep" if r["kept"] else "mull",
                    "actually_won": r["won"],
                    "rank": r["rank"],
                    "draft_id": r["draft_id"],
                    "match_number": r["match_number"],
                    "game_number": r["game_number"],
                },
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"wrote {len(picked)} prompts to {out_path}")
    print()
    print("bucket coverage in the corpus:")
    counts: dict[tuple, int] = defaultdict(int)
    for r in picked:
        counts[r["key"]] += 1
    for key in sorted(counts):
        s = summary[key]
        print(
            f"  {_bucket_label(key):30}  n={counts[key]:>3}  "
            f"keep_wr={s['keep_wr']}  mull_wr={s['mull_wr']}  "
            f"correct={s['correct']}"
        )


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--csv", required=True, type=Path, help="Path to a 17lands replay_data_public.*.csv.gz file"
    )
    parser.add_argument("--out", required=True, type=Path, help="Output prompts.jsonl path")
    parser.add_argument(
        "--n",
        dest="n_samples",
        type=int,
        default=200,
        help="Number of mulligan prompts to sample (default 200)",
    )
    parser.add_argument(
        "--min-rank",
        default="diamond",
        choices=RANK_ORDER,
        help="Minimum player rank to include (default diamond)",
    )
    parser.add_argument(
        "--min-bucket-n", type=int, default=20, help="Per-bucket arm minimum to call a 'correct' option"
    )
    parser.add_argument(
        "--max-rows-scan", type=int, default=None, help="Cap on rows scanned in pass 1 (default: all)"
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
        min_bucket_n=args.min_bucket_n,
        max_rows_scan=args.max_rows_scan,
        seed=args.seed,
    )


if __name__ == "__main__":
    main()
