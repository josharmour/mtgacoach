#!/usr/bin/env python3
"""Baseline metrics for the Fable November upgrades (plan milestone M1).

Replays the match bundles that the coach writes under ``~/.arenamcp/match_bundles``
and reproduces the baseline numbers documented in section 1 of
``fable-november-upgrades.md`` (repo root):

* matches / wins / losses, split by ``settings_subset.autopilot_enabled``
* LLM inference per call class (calls, input tokens, cached input tokens,
  thinking tokens, seconds — per match and per call), from the
  ``[PROXY] Request metrics: {...}`` lines in ``coach_log``
* decision answers: total, ``pass`` answers, answers resolved by code without
  a model call, and menus that offered a single option
* line search: decisions that ran one, decisions with an unevaluated option,
  chosen options that were unevaluated, and the most frequent unevaluated cards
* game plans formed, and plans whose ``path`` was corrected
  (``GamePlan validation ... path: corrected ...``)
* mulligan-guard keeps grouped by number of lands kept, with win rate
* decision prompt size (``typed-decision: big prompt (N chars)``): median / p90

Usage::

    python tools/upgrade_metrics.py                       # ~/.arenamcp/match_bundles
    python tools/upgrade_metrics.py --dir DIR --since 2026-11-09
    python tools/upgrade_metrics.py --json                # machine-readable

A "match" is a unique ``match_id``; a bundle is one game of that match.
Decisions with no ``chosen_options`` (e.g. superseded by an executed action)
are not counted as answers.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

DEFAULT_BUNDLES_DIR = os.path.expanduser("~/.arenamcp/match_bundles")

REQUEST_METRICS_RE = re.compile(r"Request metrics:?\s*(\{.*\})\s*$")
KEEP_LINE_RE = re.compile(r"Mulligan guard: KEEP")
LANDS_RE = re.compile(r"(\d+) lands?")
BIG_PROMPT_RE = re.compile(r"typed-decision: big prompt \((\d+) chars\)")
PLAN_FORMED_RE = re.compile(r"Game plan reform \(")
# Real logged form: "GamePlan validation (turn 12): path: plan claim corrected: ..."
PATH_CORRECTED_RE = re.compile(r"GamePlan validation.*\bpath:.*corrected")


def parse_request_metrics(line):
    """Return the metrics dict from a ``[PROXY] Request metrics:`` log line.

    The payload is a Python repr (single quotes), not JSON, so it is parsed
    with ``ast.literal_eval``. Returns None when the line does not carry a
    parsable payload.
    """
    m = REQUEST_METRICS_RE.search(line)
    if not m:
        return None
    try:
        payload = ast.literal_eval(m.group(1))
    except (ValueError, SyntaxError):
        return None
    return payload if isinstance(payload, dict) else None


def parse_keep_lands(line):
    """Return the number of lands stated in a mulligan-guard KEEP line.

    Lines that justify a keep qualitatively (e.g. "Mountains cast Laelia ...")
    state no count and yield None; those games are excluded from the per-land
    win-rate table.
    """
    if not KEEP_LINE_RE.search(line):
        return None
    m = LANDS_RE.search(line)
    return int(m.group(1)) if m else None


def unmodelled_card_name(value):
    """Reduce an unevaluated-option description to the card name.

    Real logged values look like ``"Laelia, the Blade Reforged (its triggered
    ability)"``; the parenthetical qualifier is stripped.
    """
    text = str(value).strip()
    if text.startswith("T: "):
        text = text[3:]
    return text.split(" (")[0].strip()


def load_bundle(path):
    """Load one bundle file; transparently handles gzip and plain JSON."""
    with open(path, "rb") as fh:
        magic = fh.read(2)
    opener = gzip.open if magic == b"\x1f\x8b" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def iter_bundle_paths(directory):
    """Sorted bundle paths (*.json and *.json.gz) under ``directory``."""
    names = sorted(
        n for n in os.listdir(directory)
        if n.endswith(".json") or n.endswith(".json.gz")
    )
    return [os.path.join(directory, n) for n in names]


def _bundle_day(bundle):
    """UTC calendar date (YYYY-MM-DD) of the bundle's ``started_at``."""
    started = bundle.get("started_at")
    if isinstance(started, (int, float)):
        return datetime.fromtimestamp(started, tz=timezone.utc).strftime("%Y-%m-%d")
    return str(started or "")[:10]


def is_match_bundle(bundle):
    """True when the file is a match bundle, not e.g. a live_context.json
    pointer (which carries only paths/offsets and no coach_log or packet)."""
    return "coach_log" in bundle or "packet" in bundle


def summarize_bundle(bundle):
    """Compute this bundle's contribution to every metric.

    Pure function of the bundle dict — the tests exercise it directly.
    """
    settings = bundle.get("settings_subset") or {}
    final_state = bundle.get("final_state") or {}
    coach_lines = ((bundle.get("coach_log") or {}).get("lines")) or []

    out = {
        "match_id": bundle.get("match_id"),
        "day": _bundle_day(bundle),
        "autopilot": bool(settings.get("autopilot_enabled")),
        # Match-level result: bundle["result"] is the match outcome string
        # ("win"/"loss"); final_state.last_game_result is the last game's.
        "result": bundle.get("result") or final_state.get("last_game_result"),
        # decision stats
        "answers": 0,
        "pass_answers": 0,
        "code_answers": 0,
        "single_option_menus": 0,
        "decisions_with_line_search": 0,
        "decisions_with_unmodelled": 0,
        "chosen_unmodelled": 0,
        "unmodelled_cards": Counter(),
        # inference per call class: {class: {calls,input,cached,think,sec}}
        "call_classes": defaultdict(lambda: {"calls": 0, "input": 0,
                                             "cached": 0, "think": 0, "sec": 0.0}),
        # coach-log counters
        "plans_formed": 0,
        "path_corrected": 0,
        "big_prompts": [],
        # (lands_kept, result) pairs for parseable KEEP lines
        "keeps": [],
    }

    for line in coach_lines:
        metrics = parse_request_metrics(line)
        if metrics is not None:
            bucket = out["call_classes"][metrics.get("call_class")]
            bucket["calls"] += 1
            bucket["input"] += metrics.get("input_tokens") or 0
            bucket["cached"] += metrics.get("cached_input_tokens") or 0
            bucket["think"] += metrics.get("reasoning_tokens") or 0
            bucket["sec"] += (metrics.get("total_ms") or 0) / 1000.0
        if PLAN_FORMED_RE.search(line):
            out["plans_formed"] += 1
        if PATH_CORRECTED_RE.search(line):
            out["path_corrected"] += 1
        m = BIG_PROMPT_RE.search(line)
        if m:
            out["big_prompts"].append(int(m.group(1)))
        lands = parse_keep_lands(line)
        if lands is not None:
            out["keeps"].append((lands, out["result"]))

    for decision in (bundle.get("packet") or {}).get("decisions") or []:
        pending = decision.get("pending_decision") or {}
        options = pending.get("options") or []
        chosen = decision.get("chosen_options") or []
        trace = decision.get("trace") or {}

        if not chosen:
            continue  # no answer recorded (e.g. superseded by an executed action)
        out["answers"] += 1
        if len(options) == 1:
            out["single_option_menus"] += 1
        if chosen == ["pass"]:
            out["pass_answers"] += 1
        if "llm_usage" not in trace:
            out["code_answers"] += 1

        lines = trace.get("lines")
        if isinstance(lines, dict) and lines:
            out["decisions_with_line_search"] += 1
            unmodelled = lines.get("unmodelled") or {}
            if unmodelled:
                out["decisions_with_unmodelled"] += 1
                chosen_set = set(chosen)
                for key, value in unmodelled.items():
                    out["unmodelled_cards"][unmodelled_card_name(value)] += 1
                    if key in chosen_set:
                        out["chosen_unmodelled"] += 1

    return out


def _merge(dst, src):
    """Fold one bundle summary into a running total."""
    for key in ("answers", "pass_answers", "code_answers", "single_option_menus",
                "decisions_with_line_search", "decisions_with_unmodelled",
                "chosen_unmodelled", "plans_formed", "path_corrected"):
        dst[key] += src[key]
    dst["unmodelled_cards"].update(src["unmodelled_cards"])
    dst["big_prompts"].extend(src["big_prompts"])
    dst["keeps"].extend(src["keeps"])
    for cls, bucket in src["call_classes"].items():
        agg = dst["call_classes"][cls]
        for field in ("calls", "input", "cached", "think"):
            agg[field] += bucket[field]
        agg["sec"] += bucket["sec"]
    dst["games"] += 1


def aggregate(summaries):
    """Roll per-bundle summaries up into the report structure."""
    matches = {}   # match_id -> folded totals (a match may span several games)

    for summary in summaries:
        fold = matches.setdefault(summary["match_id"], {
            "games": 0,
            "answers": 0, "pass_answers": 0, "code_answers": 0,
            "single_option_menus": 0,
            "decisions_with_line_search": 0, "decisions_with_unmodelled": 0,
            "chosen_unmodelled": 0,
            "unmodelled_cards": Counter(),
            "call_classes": defaultdict(lambda: {"calls": 0, "input": 0,
                                                 "cached": 0, "think": 0, "sec": 0.0}),
            "plans_formed": 0, "path_corrected": 0,
            "big_prompts": [], "keeps": [],
            "_latest": None,
        })
        _merge(fold, summary)
        fold["_latest"] = summary  # last game carries the match result

    n_matches = len(matches)

    # Wins/losses are match-level: count them once per match, from the last
    # game's summary (a best-of-three counted per game would inflate both).
    wl_by_autopilot = {"on": {"matches": 0, "wins": 0, "losses": 0},
                       "off": {"matches": 0, "wins": 0, "losses": 0}}
    for fold in matches.values():
        latest = fold["_latest"]
        side = wl_by_autopilot["on" if latest["autopilot"] else "off"]
        side["matches"] += 1
        if latest["result"] == "win":
            side["wins"] += 1
        elif latest["result"] == "loss":
            side["losses"] += 1

    per_class = {}
    totals = {"calls": 0, "input": 0, "cached": 0, "think": 0, "sec": 0.0}
    for match_fold in matches.values():
        for cls, bucket in match_fold["call_classes"].items():
            agg = per_class.setdefault(cls, {"calls": 0, "input": 0,
                                             "cached": 0, "think": 0, "sec": 0.0})
            for field in ("calls", "input", "cached", "think"):
                agg[field] += bucket[field]
            agg["sec"] += bucket["sec"]
    for agg in per_class.values():
        for field in totals:
            totals[field] += agg[field]
    for agg in list(per_class.values()) + [totals]:
        agg["calls_per_match"] = agg["calls"] / n_matches if n_matches else 0.0
        agg["input_per_call"] = agg["input"] / agg["calls"] if agg["calls"] else 0.0
        agg["cached_per_call"] = agg["cached"] / agg["calls"] if agg["calls"] else 0.0
        agg["think_per_call"] = agg["think"] / agg["calls"] if agg["calls"] else 0.0
        agg["sec_per_call"] = agg["sec"] / agg["calls"] if agg["calls"] else 0.0

    answers_total = sum(fold["answers"] for fold in matches.values())
    pass_answers = sum(fold["pass_answers"] for fold in matches.values())
    code_answers = sum(fold["code_answers"] for fold in matches.values())
    single_option_menus = sum(fold["single_option_menus"] for fold in matches.values())

    line_decisions = sum(fold["decisions_with_line_search"] for fold in matches.values())
    unmodelled_decisions = sum(fold["decisions_with_unmodelled"]
                               for fold in matches.values())
    chosen_unmodelled = sum(fold["chosen_unmodelled"] for fold in matches.values())
    card_counter = Counter()
    for fold in matches.values():
        card_counter.update(fold["unmodelled_cards"])

    plans_formed = sum(fold["plans_formed"] for fold in matches.values())
    path_corrected = sum(fold["path_corrected"] for fold in matches.values())

    keeps_by_lands = defaultdict(lambda: {"games": 0, "wins": 0})
    for fold in matches.values():
        for lands, result in fold["keeps"]:
            row = keeps_by_lands[lands]
            row["games"] += 1
            if result == "win":
                row["wins"] += 1

    all_prompts = sorted(p for fold in matches.values() for p in fold["big_prompts"])

    def pct(num, den):
        return (100.0 * num / den) if den else None

    def percentile(sorted_values, q):
        if not sorted_values:
            return None
        idx = min(int(len(sorted_values) * q), len(sorted_values) - 1)
        return sorted_values[idx]

    return {
        "matches": n_matches,
        "days_covered": sorted({s["day"] for s in summaries}),
        "autopilot": {
            key: {
                "matches": side["matches"],
                "wins": side["wins"],
                "losses": side["losses"],
                "win_rate_pct": pct(side["wins"],
                                    side["wins"] + side["losses"]),
            }
            for key, side in wl_by_autopilot.items()
        },
        "inference_per_class": per_class,
        "inference_totals": totals,
        "input_tokens_per_match": totals["input"] / n_matches if n_matches else 0.0,
        "answers": {
            "total": answers_total,
            "pass": pass_answers,
            "pass_pct": pct(pass_answers, answers_total),
            "code_answered_no_model_call": code_answers,
            "code_answered_pct": pct(code_answers, answers_total),
            "single_option_menus": single_option_menus,
            "per_match": answers_total / n_matches if n_matches else 0.0,
        },
        "line_search": {
            "decisions_with_line_search": line_decisions,
            "with_unevaluated_option": unmodelled_decisions,
            "with_unevaluated_pct": pct(unmodelled_decisions, line_decisions),
            "chosen_was_unevaluated": chosen_unmodelled,
            "top_unevaluated_cards": card_counter.most_common(10),
        },
        "plans_formed": plans_formed,
        "plans_path_corrected": path_corrected,
        "keeps_by_lands": {
            lands: {
                "games": row["games"],
                "wins": row["wins"],
                "win_rate_pct": pct(row["wins"], row["games"]),
            }
            for lands, row in sorted(keeps_by_lands.items())
        },
        "prompt_size_median_chars":
            percentile(all_prompts, 0.5),
        "prompt_size_p90_chars":
            percentile(all_prompts, 0.9),
    }


def render_report(agg):
    """Human-readable report mirroring the baseline tables in plan section 1."""
    out = []
    ap = agg["autopilot"]
    out.append(f"Matches: {agg['matches']} "
               f"(days covered: {', '.join(agg['days_covered']) or 'n/a'})")
    for key in ("on", "off"):
        side = ap[key]
        wr = side["win_rate_pct"]
        wr_s = f"{wr:.0f}%" if wr is not None else "-"
        out.append(f"  autopilot {key}: {side['matches']} matches "
                   f"({side['wins']}W/{side['losses']}L, win rate {wr_s})")

    out.append("")
    out.append("Inference per call class:")
    out.append(f"{'call_class':<28}{'calls/m':>9}{'in_tok/call':>13}"
               f"{'cached/call':>13}{'think/call':>12}{'s/call':>8}")
    order = sorted(agg["inference_per_class"].items(),
                   key=lambda kv: -kv[1]["calls"]) + [("<ALL>", agg["inference_totals"])]
    for cls, row in order:
        out.append(f"{cls:<28}{row['calls_per_match']:>9.1f}"
                   f"{row['input_per_call']:>13.0f}{row['cached_per_call']:>13.0f}"
                   f"{row['think_per_call']:>12.1f}{row['sec_per_call']:>8.2f}")
    out.append(f"Input tokens per match: {agg['input_tokens_per_match']:.0f}")

    ans = agg["answers"]
    out.append("")
    out.append(f"Decision answers: {ans['total']} ({ans['per_match']:.1f}/match)")
    pass_pct = f"{ans['pass_pct']:.1f}%" if ans['pass_pct'] is not None else "-"
    code_pct = f"{ans['code_answered_pct']:.1f}%" if ans['code_answered_pct'] is not None else "-"
    out.append(f"  pass answers: {ans['pass']} ({pass_pct})")
    out.append(f"  answered by code without a model call: "
               f"{ans['code_answered_no_model_call']} ({code_pct})")
    out.append(f"  single-option menus: {ans['single_option_menus']}")

    ls = agg["line_search"]
    uneval_pct = (f"{ls['with_unevaluated_pct']:.1f}%"
                  if ls['with_unevaluated_pct'] is not None else "-")
    out.append("")
    out.append(f"Line search: {ls['decisions_with_line_search']} decisions ran one; "
               f"{ls['with_unevaluated_option']} had an unevaluated option ({uneval_pct}); "
               f"a chosen option was unevaluated {ls['chosen_was_unevaluated']} times")
    out.append("Most frequent unevaluated cards:")
    for name, count in ls["top_unevaluated_cards"]:
        out.append(f"  {count:>4}x {name}")

    pc_pct = (f"{100.0 * agg['plans_path_corrected'] / agg['plans_formed']:.1f}%"
              if agg['plans_formed'] else "-")
    out.append("")
    out.append(f"Plans formed: {agg['plans_formed']}; "
               f"path corrected: {agg['plans_path_corrected']} ({pc_pct})")

    out.append("")
    out.append("Mulligan-guard keeps by lands kept:")
    out.append(f"{'lands':>6}{'games':>7}{'wins':>6}{'win rate':>10}")
    for lands, row in agg["keeps_by_lands"].items():
        wr = row['win_rate_pct']
        wr_s = f"{wr:.0f}%" if wr is not None else "-"
        out.append(f"{lands:>6}{row['games']:>7}{row['wins']:>6}{wr_s:>10}")

    median = agg["prompt_size_median_chars"]
    p90 = agg["prompt_size_p90_chars"]
    out.append("")
    if median is not None:
        out.append(f"Decision prompt size (typed-decision big prompt lines): "
                   f"median {median} chars, p90 {p90} chars")
    else:
        out.append("Decision prompt size: no 'typed-decision: big prompt' lines found")
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Reproduce the Fable November baseline metrics from match bundles.")
    parser.add_argument("--dir", "--bundles", dest="bundles_dir",
                        default=DEFAULT_BUNDLES_DIR,
                        help=f"match_bundles directory (default: {DEFAULT_BUNDLES_DIR})")
    parser.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                        help="only count bundles started on/after this UTC date")
    parser.add_argument("--json", action="store_true",
                        help="emit the aggregated metrics as JSON instead of text")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.bundles_dir):
        parser.error(f"bundles directory not found: {args.bundles_dir}")

    summaries = []
    for path in iter_bundle_paths(args.bundles_dir):
        bundle = load_bundle(path)
        if not is_match_bundle(bundle):
            continue
        summary = summarize_bundle(bundle)
        if args.since and summary["day"] < args.since:
            continue
        summaries.append(summary)

    if not summaries:
        print(f"No bundles found in {args.bundles_dir}"
              + (f" on/after {args.since}" if args.since else ""), file=sys.stderr)
        return 1

    agg = aggregate(summaries)
    if args.json:
        def _plain(obj):
            if isinstance(obj, Counter):
                return dict(obj)
            raise TypeError(repr(type(obj)))
        print(json.dumps(agg, indent=2, default=_plain))
    else:
        print(render_report(agg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
