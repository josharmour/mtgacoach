"""Aggregate replay-eval responses into per-backend + per-replay metrics.

Reads the JSONL produced by `replay/run.py` and produces:
  - per-backend headline: total decisions, % matched, parse rate, mean latency
  - per-decision-kind breakdown (eg. ActionsAvailable subtypes split by
    whether the player Cast / Played / Activated / Passed)
  - per-replay distribution: match rate per replay (the "match-arc" view)
  - JSON summary suitable for upload to the admin dashboard
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional


def _read_jsonl(path: Path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


_CARD_DB = None
_CMC_CACHE: dict[int, float] = {}


def _cmc_for(grp_id: Optional[int]) -> Optional[float]:
    """Resolve a grpId to converted mana cost. None on lookup failure.

    Memoized to amortize the card-DB cost across thousands of contested
    checks. Card DB lazy-loads on first use.
    """
    if grp_id is None:
        return None
    gid = int(grp_id)
    if gid in _CMC_CACHE:
        return _CMC_CACHE[gid]
    global _CARD_DB
    if _CARD_DB is None:
        try:
            from arenamcp.card_db import get_card_database  # type: ignore

            _CARD_DB = get_card_database()
        except Exception:
            _CARD_DB = False  # sentinel: never try again
    if _CARD_DB is False:
        return None
    info = _CARD_DB.get_card_by_arena_id(gid)
    cmc = float(getattr(info, "cmc", 0) or 0) if info else None
    _CMC_CACHE[gid] = cmc
    return cmc


def _is_contested(rec: dict) -> bool:
    """True when this decision had a real choice — not a forced/trivial move.

    Disaggregates "matches the boring majority play" from "matches when there
    were real options on the table." Definitions per kind:

    - ActionsAvailable: contested when the player faced a real sequencing
      choice — multiple castable spells competing for similar mana, or
      they actively chose to Pass while a play was available. The mana
      filter prunes "1-drop alongside a 5-drop board wipe" cases where
      one card dominates the call. Cards within ±1 CMC of the chosen
      play are treated as competing alternatives.
    - DeclareAttackers: contested when the player held back at least one
      qualified attacker (i.e., qualified_ids > chosen). All-out attacks
      are the "boring majority" we're filtering out.
    - DeclareBlockers: contested when there's >=1 attacker AND >=1 possible
      blocker — the agonizing trade math case. Trivial chumps with no
      blockers and trivial open-fields with no attackers are excluded.
    - Mulligan: every mulligan is a real choice; treat all as contested.
    """
    kind = rec.get("kind")
    if kind == "Mulligan":
        return True
    if kind == "DeclareAttackers":
        qualified = rec.get("qualified_ids") or []
        chosen = (rec.get("ground_truth") or {}).get("instance_ids") or []
        return len(qualified) >= 2 and len(qualified) > len(chosen)
    if kind == "DeclareBlockers":
        attackers = rec.get("attacker_ids") or []
        blockers = rec.get("blocker_ids") or []
        return len(attackers) >= 1 and len(blockers) >= 1
    if kind == "ActionsAvailable":
        actions = rec.get("actions") or []
        card_types = {"ActionType_Cast", "ActionType_Play", "ActionType_PlayMDFC", "ActionType_CastOmen"}
        card_actions = [a for a in actions if (a.get("action_type") or a.get("actionType")) in card_types]
        chosen_type = (rec.get("ground_truth") or {}).get("action_type")
        gt = rec.get("ground_truth") or {}
        chosen_grp_ids = gt.get("grp_ids") or []
        chosen_grp_id = chosen_grp_ids[0] if chosen_grp_ids else None

        if chosen_type in card_types:
            # Player picked a card play; contested only if at least one
            # other card action is competing for the same mana band
            # (within ±1 CMC of the chosen card). This filters out the
            # "1-drop trash alongside a 5-drop wipe" case where the call
            # isn't really between two plays.
            if len(card_actions) < 2:
                return False
            chosen_cmc = _cmc_for(chosen_grp_id)
            if chosen_cmc is None:
                # Card DB miss — fall back to the count-only rule so we
                # don't silently zero contested counts on DB outages.
                return len(card_actions) >= 2
            for a in card_actions:
                gid = a.get("grp_id") or a.get("grpId")
                if gid is None or int(gid) == int(chosen_grp_id or 0):
                    continue
                other_cmc = _cmc_for(gid)
                if other_cmc is None:
                    continue
                if abs(other_cmc - chosen_cmc) <= 1.0:
                    return True
            return False
        if chosen_type == "ActionType_Pass":
            # Player declined to play with a card on offer. The "real"
            # version of this is "held up mana for an instant or trick";
            # for v1 we still count any card-action-available pass as
            # contested since they're not common enough to subdivide.
            return len(card_actions) >= 1
        # Mana abilities, Activate, etc. are mechanical — not strategic.
        return False
    return False


def score(responses_path: Path, json_out: Path | None) -> None:
    def _new_kind_stats():
        return {
            "n": 0,
            "matched": 0,
            "jaccard_sum": 0.0,
            "n_contested": 0,
            "matched_contested": 0,
            "jaccard_sum_contested": 0.0,
        }

    def _new_be_stats():
        return {
            "n": 0,
            "errors": 0,
            "parsed": 0,
            "matched": 0,
            "jaccard_sum": 0.0,
            "n_contested": 0,
            "matched_contested": 0,
            "jaccard_sum_contested": 0.0,
            "latencies_ms": [],
            # Which seat each record was scored from. Records written before
            # per-replay seat detection landed carry no "local_seat" and may
            # have been scored from the OPPONENT's perspective.
            "by_seat": defaultdict(int),
            "seat_missing": 0,
            "by_kind": defaultdict(_new_kind_stats),
            "by_action_type": defaultdict(
                lambda: {"n": 0, "matched": 0, "jaccard_sum": 0.0, "n_contested": 0, "matched_contested": 0}
            ),
            "by_replay": defaultdict(lambda: {"n": 0, "matched": 0, "jaccard_sum": 0.0}),
        }

    by_backend: dict[str, dict] = defaultdict(_new_be_stats)

    def _was_parsed(rec: dict) -> bool:
        if rec.get("choice_number") is not None:
            return True
        if rec.get("coach_attacker_ids") is not None:
            return True
        if rec.get("coach_blocks") is not None:
            return True
        if rec.get("coach_keep") is not None:
            return True
        return False

    for r in _read_jsonl(responses_path):
        be = r.get("backend") or "?"
        s = by_backend[be]
        s["n"] += 1
        seat = r.get("local_seat")
        if seat is None:
            s["seat_missing"] += 1
        else:
            s["by_seat"][int(seat)] += 1
        if r.get("error"):
            s["errors"] += 1
            continue
        if _was_parsed(r):
            s["parsed"] += 1
        matched = bool(r.get("match"))
        contested = _is_contested(r)
        if matched:
            s["matched"] += 1
        jacc = r.get("jaccard")
        jacc_f = float(jacc) if isinstance(jacc, (int, float)) else None
        if jacc_f is not None:
            s["jaccard_sum"] += jacc_f
        if contested:
            s["n_contested"] += 1
            if matched:
                s["matched_contested"] += 1
            if jacc_f is not None:
                s["jaccard_sum_contested"] += jacc_f
        lat = r.get("latency_ms")
        if isinstance(lat, (int, float)):
            s["latencies_ms"].append(float(lat))

        kind = r.get("kind") or "?"
        sk = s["by_kind"][kind]
        sk["n"] += 1
        if matched:
            sk["matched"] += 1
        if jacc_f is not None:
            sk["jaccard_sum"] += jacc_f
        if contested:
            sk["n_contested"] += 1
            if matched:
                sk["matched_contested"] += 1
            if jacc_f is not None:
                sk["jaccard_sum_contested"] += jacc_f

        gt = r.get("ground_truth") or {}
        atype = gt.get("action_type") or "?"
        sa = s["by_action_type"][atype]
        sa["n"] += 1
        if matched:
            sa["matched"] += 1
        if jacc_f is not None:
            sa["jaccard_sum"] += jacc_f
        if contested:
            sa["n_contested"] += 1
            if matched:
                sa["matched_contested"] += 1

        replay = r.get("replay") or "?"
        sr = s["by_replay"][replay]
        sr["n"] += 1
        if matched:
            sr["matched"] += 1
        if jacc_f is not None:
            sr["jaccard_sum"] += jacc_f

    # ---- seat provenance (must come first: it invalidates everything below) ----
    total_missing = sum(s["seat_missing"] for s in by_backend.values())
    if total_missing:
        print()
        print("=" * 78)
        print(f"WARNING: {total_missing} record(s) carry no 'local_seat' field.")
        print("They were produced before per-replay seat detection existed, when")
        print("the local player was hardcoded to seat 2 — and 55 of the 104 corpus")
        print("replays record as seat 1. Any such record may have been scored from")
        print("the OPPONENT's hand and board. Re-run replay/run.py before trusting")
        print("the numbers below.")
        print("=" * 78)
    seat_line = []
    for be, s in sorted(by_backend.items()):
        parts = ", ".join(f"seat {k}: {v}" for k, v in sorted(s["by_seat"].items()))
        if s["seat_missing"]:
            parts = (parts + ", " if parts else "") + f"unknown: {s['seat_missing']}"
        seat_line.append(f"  {be}  {parts}")
    if seat_line:
        print("\nRecords by scored seat:")
        print("\n".join(seat_line))

    # ---- print headline ----
    print()
    cols = [
        ("backend", 30),
        ("n", 5),
        ("parse%", 7),
        ("match%", 8),
        ("contested%", 11),
        ("jaccard", 8),
        ("jacc_contested", 16),
        ("median_ms", 11),
    ]
    print(" ".join(f"{name:<{w}}" for name, w in cols))
    print("-" * sum(w + 1 for _, w in cols))
    for be, s in sorted(by_backend.items()):
        n = s["n"] or 1
        nc = s["n_contested"] or 1
        match_pct = s["matched"] / n * 100
        contested_pct = s["matched_contested"] / nc * 100 if s["n_contested"] else 0.0
        parse_pct = s["parsed"] / n * 100
        mean_jacc = s["jaccard_sum"] / n
        mean_jacc_c = s["jaccard_sum_contested"] / nc if s["n_contested"] else 0.0
        med_ms = statistics.median(s["latencies_ms"]) if s["latencies_ms"] else 0.0
        row = [
            be[:30],
            s["n"],
            f"{parse_pct:.0f}%",
            f"{match_pct:.1f}%",
            f"{contested_pct:.1f}% (n={s['n_contested']})",
            f"{mean_jacc:.3f}",
            f"{mean_jacc_c:.3f}",
            f"{med_ms:.0f}",
        ]
        print(" ".join(f"{str(v):<{w}}" for v, (_, w) in zip(row, cols, strict=True)))

    print("\nMatch% / mean Jaccard by decision KIND (all / contested-only):")
    for be, s in sorted(by_backend.items()):
        print(f"  {be}")
        for kind, sk in sorted(s["by_kind"].items(), key=lambda kv: -kv[1]["n"]):
            n = sk["n"] or 1
            nc = sk["n_contested"] or 1
            mp = sk["matched"] / n * 100
            mp_c = sk["matched_contested"] / nc * 100 if sk["n_contested"] else 0.0
            mj = sk["jaccard_sum"] / n
            mj_c = sk["jaccard_sum_contested"] / nc if sk["n_contested"] else 0.0
            print(
                f"    {kind:18s}  n={sk['n']:4d}  match={sk['matched']:4d}  "
                f"match%={mp:5.1f}%  jacc={mj:.3f}  "
                f"|  contested n={sk['n_contested']:4d}  "
                f"match%={mp_c:5.1f}%  jacc={mj_c:.3f}"
            )

    # ---- per action-type ----
    print("\nMatch% by player's actual ActionType:")
    for be, s in sorted(by_backend.items()):
        print(f"  {be}")
        for atype, sub in sorted(s["by_action_type"].items(), key=lambda kv: -kv[1]["n"]):
            pct = sub["matched"] / sub["n"] * 100 if sub["n"] else 0.0
            print(
                f"    {atype.replace('ActionType_', ''):20s}  n={sub['n']:4d}  match={sub['matched']:4d}  {pct:.1f}%"
            )

    # ---- per-replay distribution ----
    print("\nPer-replay match% distribution:")
    for be, s in sorted(by_backend.items()):
        replays = list(s["by_replay"].values())
        if not replays:
            continue
        pcts = [r["matched"] / r["n"] * 100 for r in replays if r["n"]]
        print(
            f"  {be}  replays={len(replays)}  "
            f"median={statistics.median(pcts):.1f}%  "
            f"mean={statistics.mean(pcts):.1f}%  "
            f"top10={statistics.mean(sorted(pcts, reverse=True)[: max(1, len(pcts) // 10)]):.1f}%"
        )

    # ---- JSON summary for upload ----
    if json_out:
        backends_payload = []
        for be, s in sorted(by_backend.items()):
            n = s["n"] or 0
            replay_pcts = []
            for replay_name, sub in s["by_replay"].items():
                if sub["n"]:
                    replay_pcts.append(
                        {
                            "replay": replay_name,
                            "n": sub["n"],
                            "matched": sub["matched"],
                            "match_rate": round(sub["matched"] / sub["n"], 4),
                        }
                    )
            nc = s["n_contested"] or 0
            backends_payload.append(
                {
                    "backend": be,
                    "n": s["n"],
                    "errors": s["errors"],
                    "parsed": s["parsed"],
                    "matched": s["matched"],
                    "match_rate": round(s["matched"] / n, 4) if n else None,
                    "parse_rate": round(s["parsed"] / n, 4) if n else None,
                    "mean_jaccard": round(s["jaccard_sum"] / n, 4) if n else None,
                    "n_contested": nc,
                    "matched_contested": s["matched_contested"],
                    "match_rate_contested": round(s["matched_contested"] / nc, 4) if nc else None,
                    "mean_jaccard_contested": round(s["jaccard_sum_contested"] / nc, 4) if nc else None,
                    "latency_ms_median": round(statistics.median(s["latencies_ms"]), 1)
                    if s["latencies_ms"]
                    else None,
                    "latency_ms_p90": round(statistics.quantiles(s["latencies_ms"], n=10)[-1], 1)
                    if len(s["latencies_ms"]) >= 10
                    else None,
                    "by_seat": {str(k): v for k, v in sorted(s["by_seat"].items())},
                    "seat_missing": s["seat_missing"],
                    "by_kind": {
                        kind: {
                            "n": sub["n"],
                            "matched": sub["matched"],
                            "match_rate": round(sub["matched"] / sub["n"], 4) if sub["n"] else None,
                            "mean_jaccard": round(sub["jaccard_sum"] / sub["n"], 4) if sub["n"] else None,
                            "n_contested": sub.get("n_contested", 0),
                            "matched_contested": sub.get("matched_contested", 0),
                            "match_rate_contested": (
                                round(sub["matched_contested"] / sub["n_contested"], 4)
                                if sub.get("n_contested")
                                else None
                            ),
                            "mean_jaccard_contested": (
                                round(sub["jaccard_sum_contested"] / sub["n_contested"], 4)
                                if sub.get("n_contested")
                                else None
                            ),
                        }
                        for kind, sub in s["by_kind"].items()
                    },
                    "by_action_type": {
                        atype: {
                            "n": sub["n"],
                            "matched": sub["matched"],
                            "match_rate": round(sub["matched"] / sub["n"], 4) if sub["n"] else None,
                            "mean_jaccard": round(sub["jaccard_sum"] / sub["n"], 4) if sub["n"] else None,
                            "n_contested": sub.get("n_contested", 0),
                            "matched_contested": sub.get("matched_contested", 0),
                            "match_rate_contested": (
                                round(sub["matched_contested"] / sub["n_contested"], 4)
                                if sub.get("n_contested")
                                else None
                            ),
                        }
                        for atype, sub in s["by_action_type"].items()
                    },
                    "per_replay": sorted(replay_pcts, key=lambda r: -r["match_rate"]),
                }
            )
        payload = {
            "target": "replay_match",
            "ts": time.time(),
            "n_decisions": sum(s["n"] for s in by_backend.values()),
            "n_replays": len({r for s in by_backend.values() for r in s["by_replay"]}),
            # Non-zero means part of the corpus predates per-replay seat
            # detection and may have been scored from the opponent's side.
            "n_seat_missing": total_missing,
            "backends": backends_payload,
        }
        with open(json_out, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"\nWrote {json_out}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--responses", required=True, type=Path)
    p.add_argument("--json", type=Path, help="Optional structured JSON summary for the admin dashboard")
    args = p.parse_args()
    score(args.responses, args.json)


if __name__ == "__main__":
    main()
