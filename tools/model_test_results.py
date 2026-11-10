"""Results of autopilot matches grouped by the model that played them.

Interim tool for the outside-model test sessions (fable-november-upgrades.md,
"Model testing"). It reads the coach's match bundles and attributes each match
to the model that answered most of its decision calls, taken from the
``[PROXY] Request metrics`` lines the bundle carries.

    python tools/model_test_results.py                    # every bundle
    python tools/model_test_results.py --since 2026-11-10
    python tools/model_test_results.py --deck "Minsc"     # deck name contains
    python tools/model_test_results.py --json

A match with calls to more than one model is listed under "mixed" and left out
of the per-model rows: switch models between matches, not during one.
"""

from __future__ import annotations

import argparse
import collections
import glob
import gzip
import json
import math
import os
import re
import statistics

BUNDLES = os.path.expanduser("~/.arenamcp/match_bundles")
_METRICS = re.compile(r"\[PROXY\] Request metrics: (\{.*\})")
_FIELD = {
    "model": re.compile(r"'served_model': '([^']*)'"),
    "alias": re.compile(r"'model': '([^']*)'"),
    "class": re.compile(r"'call_class': '([^']*)'"),
    "ms": re.compile(r"'total_ms': ([\d.]+)"),
    "status": re.compile(r"'status': '([^']*)'"),
    "in": re.compile(r"'input_tokens': (\d+)"),
    "think": re.compile(r"'reasoning_tokens': (\d+)"),
}
_EVENTS = {
    "fallbacks": "typed-decision fallback",
    "first_token_timeouts": "FirstTokenTimeout",
    "watchdog_defaults": "[WATCHDOG] safe default sent",
    "manual_required": "MANUAL REQUIRED: ",
    "no_coaching": "No coaching available",
}


def _get(name: str, line: str) -> str | None:
    found = _FIELD[name].search(line)
    return found.group(1) if found else None


def read_match(path: str) -> dict | None:
    """One bundle as a flat record, or None when it cannot be read or has no result."""
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
            bundle = json.load(handle)
    except (OSError, ValueError):
        return None
    result = bundle.get("result")
    if result not in ("win", "loss"):
        return None
    lines = (bundle.get("coach_log") or {}).get("lines") or []
    models: collections.Counter[str] = collections.Counter()
    seconds: list[float] = []
    record = {name: 0 for name in _EVENTS}
    record.update(calls=0, not_ok=0, input_tokens=0, thinking_tokens=0)
    for line in lines:
        for name, needle in _EVENTS.items():
            if needle in line:
                record[name] += 1
        if "Request metrics" not in line:
            continue
        call_class = _get("class", line) or ""
        if not call_class.startswith("decision"):
            continue
        # The alias the coach asked for names the test model ("or-..."); the
        # served name is what the gateway reports for the local model.
        alias = _get("alias", line) or ""
        models[alias if alias.startswith("or-") else (_get("model", line) or alias or "unknown")] += 1
        record["calls"] += 1
        if (_get("status", line) or "ok") != "ok":
            record["not_ok"] += 1
        if ms := _get("ms", line):
            seconds.append(float(ms) / 1000)
        record["input_tokens"] += int(_get("in", line) or 0)
        record["thinking_tokens"] += int(_get("think", line) or 0)
    if not models:
        return None
    top, top_count = models.most_common(1)[0]
    deck = ((bundle.get("packet") or {}).get("deck_strategy") or "").split("\n", 1)[0]
    deck = re.sub(r"^DECK PLAYBOOK v\d+:\s*", "", deck)[:60] or "unknown deck"
    settings = bundle.get("settings_subset") or {}
    record.update(
        match=str(bundle.get("match_id") or "")[:8],
        when=str(bundle.get("created_at") or "")[:16],
        result=result,
        model=top if top_count >= 0.9 * sum(models.values()) else "mixed",
        deck=deck,
        format=(bundle.get("final_state") or {}).get("format_name") or "",
        autopilot=bool(settings.get("autopilot_enabled")),
        seconds=seconds,
    )
    return record


def interval(wins: int, games: int) -> tuple[float, float]:
    """Wilson 95% interval for a win rate, as percentages."""
    if not games:
        return 0.0, 0.0
    z = 1.96
    p = wins / games
    centre = (p + z * z / (2 * games)) / (1 + z * z / games)
    half = z * math.sqrt(p * (1 - p) / games + z * z / (4 * games * games)) / (1 + z * z / games)
    return 100 * (centre - half), 100 * (centre + half)


def summarise(matches: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for match in matches:
        groups[(match["model"], match["deck"])].append(match)
    rows = []
    for (model, deck), items in sorted(groups.items()):
        games = len(items)
        wins = sum(item["result"] == "win" for item in items)
        low, high = interval(wins, games)
        seconds = sorted(second for item in items for second in item["seconds"])
        calls = sum(item["calls"] for item in items)
        rows.append(
            {
                "model": model,
                "deck": deck,
                "games": games,
                "wins": wins,
                "losses": games - wins,
                "win_rate": round(100 * wins / games, 1),
                "interval_95": [round(low), round(high)],
                "decision_median_s": round(statistics.median(seconds), 1) if seconds else None,
                "decision_p95_s": round(seconds[int(len(seconds) * 0.95)], 1) if seconds else None,
                "calls_per_game": round(calls / games, 1),
                "not_ok_pct": round(100 * sum(item["not_ok"] for item in items) / max(1, calls), 1),
                "thinking_per_call": round(sum(item["thinking_tokens"] for item in items) / max(1, calls)),
                **{name: round(sum(item[name] for item in items) / games, 2) for name in _EVENTS},
            }
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dir", default=BUNDLES)
    parser.add_argument("--since", default="", help="YYYY-MM-DD or YYYY-MM-DDTHH:MM (bundle local time)")
    parser.add_argument("--deck", default="", help="only decks whose name contains this text")
    parser.add_argument("--all", action="store_true", help="include hand-played games (autopilot off)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    matches = []
    for path in glob.glob(os.path.join(args.dir, "*.json.gz")):
        match = read_match(path)
        if match is None or match["when"] < args.since:
            continue
        if args.deck.lower() not in match["deck"].lower():
            continue
        if not args.all and not match["autopilot"]:
            continue
        matches.append(match)
    rows = summarise(matches)
    if args.json:
        print(json.dumps(rows, indent=1))
        return
    print(f"{len(matches)} autopilot matches" + (f" since {args.since}" if args.since else ""))
    header = (
        f"{'model':22s} {'deck':30s} {'W-L':>8s} {'win%':>5s} {'95% range':>10s} "
        f"{'med s':>5s} {'p95 s':>5s} {'calls':>5s} {'fail%':>5s} {'think':>5s} "
        f"{'fallb':>5s} {'tmo':>4s} {'wdog':>4s} {'manual':>6s}"
    )
    print(header)
    for row in rows:
        low, high = row["interval_95"]
        print(
            f"{row['model'][:22]:22s} {row['deck'][:30]:30s} {row['wins']:3d}-{row['losses']:<4d} "
            f"{row['win_rate']:5.1f} {f'{low}-{high}':>10s} {row['decision_median_s'] or 0:5.1f} "
            f"{row['decision_p95_s'] or 0:5.1f} {row['calls_per_game']:5.1f} {row['not_ok_pct']:5.1f} "
            f"{row['thinking_per_call']:5d} {row['fallbacks']:5.2f} {row['first_token_timeouts']:4.2f} "
            f"{row['watchdog_defaults']:4.2f} {row['manual_required']:6.2f}"
        )


if __name__ == "__main__":
    main()
