"""Transport-free mirror-run protocol validator. Synthetic tests are not games.

Reads explicit protocol JSON and paired sanitized records, never controls Arena,
queries a model or writes bundles. Output is aggregate-only, without identities.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import re
from pathlib import Path


class InvalidRun(ValueError):
    pass


def require(ok):
    if not ok:
        raise InvalidRun("invalid or conflicting run evidence")


def digest(x, length=64):
    return (
        type(x) is str and re.fullmatch("[0-9a-f]{" + str(length) + "}", x) is not None
    )


def integer(x):
    return type(x) is int and 0 <= x <= 10**9


def protocol(p):
    require(
        type(p) is dict
        and set(p)
        == {"version", "run_id", "deck_sha256", "format", "games", "sides", "machines"}
    )
    require(type(p["version"]) is int and p["version"] == 1)
    require(digest(p["run_id"]) and digest(p["deck_sha256"]))
    require(
        p["format"]
        in (
            "Standard",
            "Historic",
            "Brawl",
            "Timeless",
            "Alchemy",
            "Explorer",
            "Limited",
        )
    )
    require(integer(p["games"]) and 2 <= p["games"] <= 100000 and p["games"] % 2 == 0)
    require(
        type(p["machines"]) is list
        and len(p["machines"]) == 2
        and all(digest(x) for x in p["machines"])
        and p["machines"][0] != p["machines"][1]
    )
    require(type(p["sides"]) is dict and set(p["sides"]) == {"A", "B"})
    for side in p["sides"].values():
        require(type(side) is dict and set(side) == {"model", "code_revision"})
        require(
            type(side["model"]) is str
            and re.fullmatch("[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}", side["model"])
            is not None
        )
        require(digest(side["code_revision"], 40))
    return p


METRICS = {
    "decisions",
    "decision_seconds",
    "timeouts",
    "safe_defaults",
    "model_calls",
    "input_tokens",
    "output_tokens",
}
FIELDS = {
    "game_id",
    "run_id",
    "game",
    "side",
    "machine",
    "deck_sha256",
    "format",
    "model",
    "code_revision",
    "winner",
    "on_play",
    "metrics",
}


def record(p, r):
    require(type(r) is dict and set(r) == FIELDS)
    require(digest(r["game_id"]))
    require(
        r["run_id"] == p["run_id"]
        and r["deck_sha256"] == p["deck_sha256"]
        and r["format"] == p["format"]
    )
    require(integer(r["game"]) and 1 <= r["game"] <= p["games"])
    require(type(r["side"]) is str and r["side"] in ("A", "B"))
    expected = p["sides"][r["side"]]
    require(
        r["model"] == expected["model"]
        and r["code_revision"] == expected["code_revision"]
    )
    swap = r["game"] > p["games"] // 2
    machine_index = (r["side"] == "B") ^ swap
    require(r["machine"] == p["machines"][machine_index])
    require(type(r["winner"]) is str and r["winner"] in ("A", "B", "draw", "unknown"))
    require(type(r["on_play"]) is str and r["on_play"] in ("A", "B", "unknown"))
    metrics = r["metrics"]
    require(type(metrics) is dict and set(metrics) == METRICS)
    for k, v in metrics.items():
        if k == "decision_seconds":
            require(type(v) in (int, float) and 0 <= v <= 10**9 and math.isfinite(v))
        else:
            require(integer(v))
    require(
        metrics["timeouts"] <= metrics["decisions"]
        and metrics["safe_defaults"] <= metrics["decisions"]
    )
    require(metrics["decisions"] > 0 or metrics["decision_seconds"] == 0)


def wilson(wins, n):
    if not n:
        return None
    z = 1.959963984540054
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    radius = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [max(0, center - radius), min(1, center + radius)]


def analyze(p, records):
    protocol(p)
    require(type(records) is list)
    paired = {}
    duplicates = 0
    game_ids = {}
    sequences = {}
    for r in records:
        record(p, r)
        key = (r["game"], r["side"])
        require(r["game_id"] not in game_ids or game_ids[r["game_id"]] == r["game"])
        require(r["game"] not in sequences or sequences[r["game"]] == r["game_id"])
        game_ids[r["game_id"]] = r["game"]
        sequences[r["game"]] = r["game_id"]
        if key in paired:
            require(paired[key] == r)
            duplicates += 1
        paired[key] = r
    outcomes = collections.Counter()
    wins = collections.Counter()
    onplay = collections.Counter()
    ondraw = collections.Counter()
    unknownplay = collections.Counter()
    totals = {s: collections.Counter() for s in ("A", "B")}
    complete = 0
    missing = 0
    for game in range(1, p["games"] + 1):
        a = paired.get((game, "A"))
        b = paired.get((game, "B"))
        if a is None or b is None:
            missing += 1
            continue
        require(a["winner"] == b["winner"] and a["on_play"] == b["on_play"])
        complete += 1
        winner = a["winner"]
        outcomes[winner] += 1
        for s, r in (("A", a), ("B", b)):
            totals[s].update(r["metrics"])
        if winner in ("A", "B"):
            wins[winner] += 1
            if a["on_play"] == "unknown":
                unknownplay[winner] += 1
            elif winner == a["on_play"]:
                onplay[winner] += 1
            else:
                ondraw[winner] += 1
    decisive = outcomes["A"] + outcomes["B"]
    result = {
        "scheduled_games": p["games"],
        "paired_games": complete,
        "missing_or_unpaired_games": missing,
        "duplicate_records_ignored": duplicates,
        "draws": outcomes["draw"],
        "unknown_outcomes": outcomes["unknown"],
        "decisive_games": decisive,
        "sides": {},
        "interval_basis": "95% Wilson interval on decisive games only; draws/unknown excluded; not causal significance",
        "acceptance": "source-only; live twenty-game gate not assessed; V2/P1 dependencies unchanged",
    }
    for side in ("A", "B"):
        t = totals[side]
        result["sides"][side] = {
            "wins": wins[side],
            "decisive_win_rate": wins[side] / decisive if decisive else None,
            "win_rate_95ci": wilson(wins[side], decisive),
            "wins_on_play": onplay[side],
            "wins_on_draw": ondraw[side],
            "wins_unknown_play": unknownplay[side],
            "seconds_per_decision": t["decision_seconds"] / t["decisions"]
            if t["decisions"]
            else None,
            "timeouts": t["timeouts"],
            "safe_defaults": t["safe_defaults"],
            "model_calls_per_paired_game": t["model_calls"] / complete
            if complete
            else None,
            "input_tokens_per_paired_game": t["input_tokens"] / complete
            if complete
            else None,
            "output_tokens_per_paired_game": t["output_tokens"] / complete
            if complete
            else None,
        }
    return result


def table(result):
    lines = [
        f"Paired games: {result['paired_games']}/{result['scheduled_games']}; missing/unpaired: {result['missing_or_unpaired_games']}",
        f"Draws: {result['draws']}; unknown: {result['unknown_outcomes']}",
    ]
    for side, metrics in result["sides"].items():
        lines.append("Side " + side)
        lines.extend(f"  {key}: {value}" for key, value in metrics.items())
    return "\n".join(lines + [result["interval_basis"], result["acceptance"]])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        required=True,
        type=Path,
        help="explicit protocol JSON path; no discovery or transport",
    )
    parser.add_argument(
        "--records", required=True, type=Path, help="sanitized paired-record JSON array"
    )
    parser.add_argument("--format", choices=("json", "table"), default="table")
    args = parser.parse_args(argv)
    try:
        result = analyze(
            json.loads(args.run.read_text()), json.loads(args.records.read_text())
        )
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        parser.exit(2, "invalid head-to-head evidence\n")
    print(
        json.dumps(result, sort_keys=True) if args.format == "json" else table(result)
    )


if __name__ == "__main__":
    main()
