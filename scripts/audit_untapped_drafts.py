#!/usr/bin/env python3
"""Offline, read-only comparison of saved Companion scores and coach pick logs.

The resulting gaps measure agreement with Untapped, not card quality or win rate.
Only explicit dynamic scores are compared; static scores are never substituted.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

PICK = re.compile(r"Draft pick P(\d+)p(\d+) (\[.*\]): (\[.*\])$")
TOOK = re.compile(r"Draft autoplay: P(\d+)p(\d+): took .* \(([^()]*)\)$")
EVENT = re.compile(r"(?:Premier|Quick|Trad)Draft started: (\S+)")
STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)")


def parse_log(path: Path, tz: ZoneInfo) -> tuple[list[dict], list[dict]]:
    """Whitelist draft evidence; never copy arbitrary log lines into the report."""
    picks, events = [], []
    mode_by_key: dict[tuple[int, int], tuple[datetime, int, str]] = {}
    model = None
    model_at = None
    with path.open(errors="replace") as stream:
        for line_no, line in enumerate(stream, 1):
            if not any(
                x in line
                for x in ("Draft pick P", "Draft started:", "Draft autoplay: P", "'call_class': 'draft.pick'")
            ):
                continue
            stamp = STAMP.match(line)
            if not stamp:
                continue
            timestamp = datetime.fromisoformat(stamp[1]).replace(tzinfo=tz)
            event = EVENT.search(line)
            if event:
                events.append(
                    {"timestamp": timestamp, "event": event[1], "line": line_no, "source": str(path)}
                )
                mode_by_key.clear()
                model = model_at = None
            if "'call_class': 'draft.pick'" in line:
                match = re.search(r"'served_model': '([^']+)'", line)
                model = match[1] if match else None
                model_at = timestamp
            took = TOOK.search(line)
            if took:
                mode_by_key[(int(took[1]), int(took[2]))] = (timestamp, line_no, took[3])
            match = PICK.search(line.rstrip())
            if not match:
                continue
            try:
                names, reasons = ast.literal_eval(match[3]), ast.literal_eval(match[4])
            except (ValueError, SyntaxError):
                continue
            if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                continue
            key = (int(match[1]), int(match[2]))
            prior_mode = mode_by_key.pop(key, None)
            paired_mode = (
                prior_mode[2]
                if prior_mode
                and 0 <= (timestamp - prior_mode[0]).total_seconds() <= 2
                and line_no - prior_mode[1] <= 5
                else None
            )
            picks.append(
                {
                    "timestamp": timestamp,
                    "pack": key[0],
                    "pick": key[1],
                    "names": names,
                    "reasons": reasons,
                    "line": line_no,
                    "source": str(path),
                    "decision_source": paired_mode,
                    "served_model_context": model
                    if paired_mode == "model"
                    and model_at
                    and 0 <= (timestamp - model_at).total_seconds() <= 30
                    else None,
                }
            )
    return picks, events


def dynamic_comparison(pack: list[str], scores: dict, chosen: str | None) -> dict:
    """Tie-aware comparison, disclosing incomplete score coverage."""
    available = {}
    for card in pack:
        value = scores.get(card, {}).get("dynamicScore")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            available[card] = float(value)
    result = {
        "scored_cards": len(available),
        "pack_cards": len(set(pack)),
        "complete_coverage": len(available) == len(set(pack)),
        "chosen_dynamic_score": available.get(chosen),
    }
    if not available:
        return {
            **result,
            "top_ids": [],
            "top_score": None,
            "rank": None,
            "gap": None,
            "agrees_with_top": None,
        }
    best = max(available.values())
    value = available.get(chosen)
    return {
        **result,
        "top_ids": [c for c, s in available.items() if s == best],
        "top_score": best,
        "rank": None if value is None else 1 + sum(s > value for s in available.values()),
        "gap": None if value is None else round(best - value, 6),
        "agrees_with_top": None if value is None else value == best,
    }


def match_coach(
    candidates: list[dict], row: dict, names: dict[str, str], start: datetime, end: datetime
) -> tuple[dict | None, str]:
    """Require unique coordinate/time/name-in-pack support; don't infer actual=coach."""
    offered = {names.get(str(c), str(c)) for c in row.get("DraftPack", [])}
    matches = [
        c
        for c in candidates
        if c["pack"] == row["PackNumber"]
        and c["pick"] == row["PickNumber"]
        and start - timedelta(seconds=60) <= c["timestamp"] < end
        and set(c["names"]).issubset(offered)
    ]
    unique = {(c["timestamp"], tuple(c["names"])): c for c in matches}
    if len(unique) != 1:
        return None, "ambiguous_log_matches" if unique else "no_matching_pick_log"
    return next(iter(unique.values())), "unique_time_coordinate_and_offered_name_match"


def summarize(rows: list[dict], field: str) -> dict:
    comparisons = [
        r[field]
        for r in rows
        if r.get(field) and r[field]["complete_coverage"] and r[field]["gap"] is not None
    ]
    # A singleton pack is forced and must not inflate agreement.
    choices = [r for r in comparisons if r["pack_cards"] > 1]
    gaps = [r["gap"] for r in choices]
    return {
        "comparable_with_complete_scores": len(comparisons),
        "nonforced_choices": len(choices),
        "top_agreements": sum(r["agrees_with_top"] for r in choices),
        "top_agreement_rate": round(mean(r["agrees_with_top"] for r in choices), 4) if choices else None,
        "mean_raw_gap": round(mean(gaps), 3) if gaps else None,
        "median_raw_gap": median(gaps) if gaps else None,
        "max_raw_gap": max(gaps) if gaps else None,
    }


def build_report(
    drafts: dict,
    coach: list[dict],
    events: list[dict],
    names: dict[str, str],
    tz: ZoneInfo,
    holdouts: set[str],
) -> dict:
    entries = sorted(drafts.items(), key=lambda item: item[1]["startTime"])
    results = []
    for index, (draft_id, draft) in enumerate(entries):
        start = datetime.fromtimestamp(draft["startTime"] / 1000, tz)
        end = (
            min(
                start + timedelta(hours=6),
                datetime.fromtimestamp(entries[index + 1][1]["startTime"] / 1000, tz),
            )
            if index + 1 < len(entries)
            else start + timedelta(hours=6)
        )
        prior_events = [
            e
            for e in events
            if start - timedelta(minutes=10) <= e["timestamp"] <= start + timedelta(seconds=5)
        ]
        event = prior_events[-1] if prior_events and draft.get("limitedType") == "draft" else None
        rows = []
        for source_row in draft.get("picks", []):
            pack = [str(c) for c in source_row.get("DraftPack", [])]
            actual = source_row.get("PickedCard")
            actual = str(actual) if actual is not None else None
            scores = source_row.get("PackScores", {})
            matched, basis = match_coach(coach, source_row, names, start, end)
            coach_id = None
            if matched and len(matched["names"]) == 1:
                ids = [c for c in pack if names.get(c) == matched["names"][0]]
                if len(ids) == 1:
                    coach_id = ids[0]
            if matched:
                matched = {**matched, "timestamp": matched["timestamp"].isoformat()}
            row = {
                "pack": source_row["PackNumber"],
                "pick": source_row["PickNumber"],
                "pool_before_ids": [str(c) for c in source_row.get("PickedCards", [])],
                "offered": [
                    {
                        "id": c,
                        "name": names.get(c, c),
                        "dynamic_score": scores.get(c, {}).get("dynamicScore"),
                        "static_score": scores.get(c, {}).get("staticScore"),
                    }
                    for c in pack
                ],
                "actual_pick_id": actual,
                "actual_pick_name": names.get(actual) if actual else None,
                "actual_comparison": dynamic_comparison(pack, scores, actual),
                "coach_evidence": matched,
                "coach_match_basis": basis,
                "coach_pick_id": coach_id,
                "coach_matches_actual": coach_id == actual
                if coach_id is not None and actual is not None
                else None,
                "coach_comparison": dynamic_comparison(pack, scores, coach_id) if coach_id else None,
            }
            rows.append(row)
        results.append(
            {
                "draft_id": draft_id,
                "limited_type": draft.get("limitedType"),
                "start_time": start.isoformat(),
                "event": event["event"] if event else None,
                "event_log_line": event["line"] if event else None,
                "event_log_source": event["source"] if event else None,
                "event_match_basis": "nearby_draft_start_log" if event else None,
                "partition": "reserved_replay_check" if draft_id in holdouts else "audit",
                "pick_rows": len(rows),
                "actual_picks_recorded": sum(r["actual_pick_id"] is not None for r in rows),
                "coach_picks_attributed": sum(r["coach_pick_id"] is not None for r in rows),
                "actual_summary": summarize(rows, "actual_comparison"),
                "coach_summary": summarize(rows, "coach_comparison"),
                "picks": rows,
            }
        )
    return {
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "limitations": [
            "Scores are raw Untapped dynamic units, not probabilities or measured draft strength.",
            "No static score substitutes; aggregate comparisons require complete pack dynamic coverage and exclude forced singleton choices.",
            "Coach attribution requires a unique log time/pack/pick/name-in-offered match. Logs lack full offered pack IDs, so this is correlated evidence rather than an Arena draft-ID join.",
            "Actual picks without matching logs are unattributed. Historical code revisions are unknown; served-model context is recorded when available.",
            "Reserved replay check drafts were selected before this disagreement inspection, but may have informed prior work. They are not unbiased holdouts. No new per-card tuning, simulator, live model rerun, or superiority claim is made.",
            "Conditional comparisons use the historical pool. Alternative earlier picks would change subsequent pool and scores.",
            "These are historical decisions, not reruns of the changed coach. Recommendation matching is bounded to six hours after the Companion draft start and ends sooner if another saved draft starts.",
        ],
        "holdout_ids": sorted(holdouts),
        "drafts": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drafts", type=Path, required=True)
    parser.add_argument("--log", type=Path, action="append", required=True)
    parser.add_argument("--card-db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timezone", default="America/Los_Angeles")
    parser.add_argument("--holdout", action="append", default=[])
    args = parser.parse_args()
    from arenamcp.mtgadb import MTGADatabase

    raw = args.drafts.read_bytes()
    drafts = json.loads(raw)
    tz = ZoneInfo(args.timezone)
    coach, events = [], []
    for log in args.log:
        p, e = parse_log(log, tz)
        coach.extend(p)
        events.extend(e)
    events.sort(key=lambda e: e["timestamp"])
    ids = {str(c) for d in drafts.values() for p in d.get("picks", []) for c in p.get("DraftPack", [])}
    db = MTGADatabase(args.card_db)
    names = {c: card.name for c in ids if (card := db.get_card(int(c)))}
    report = build_report(drafts, coach, events, names, tz, set(args.holdout))
    report["sources"] = {
        "companion": str(args.drafts),
        "companion_snapshot_sha256": hashlib.sha256(raw).hexdigest(),
        "coach_logs": [str(p) for p in args.log],
        "card_database": str(args.card_db),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "draft_records": len(report["drafts"]),
                "coach_matches": sum(d["coach_picks_attributed"] for d in report["drafts"]),
            }
        )
    )


if __name__ == "__main__":
    main()
