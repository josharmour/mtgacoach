"""Side-by-side renderer + replayer for the coach.py prompt-variant ablation.

There are two ways to use this script:

(1) `--demo` — synthesize a representative game_state dict and render it
    through both the compressed and raw_json builders, printing sizes and
    excerpts. Useful for sanity-checking the implementation without a
    capture corpus.

(2) `--captures <path.jsonl>` — replay an existing capture file (produced
    by setting MTGACOACH_PROMPT_DUMP_PATH during a play session). For each
    captured record, we already have the rendered user message; we send it
    to the configured backend and record the response, latency, and the
    prompt_variant tag the capture was made with. Then a separate run with
    the *other* variant on a parallel capture lets you compare apples-to-
    apples on real game states.

The full quality comparison flow:

    # On Windows, with the desktop coach running
    $env:MTGACOACH_PROMPT_DUMP_PATH = "$HOME\.arenamcp\eval_compressed.jsonl"
    $env:MTGACOACH_PROMPT_VARIANT = "default"
    # ... play 5-10 matches ...

    $env:MTGACOACH_PROMPT_DUMP_PATH = "$HOME\.arenamcp\eval_rawjson.jsonl"
    $env:MTGACOACH_PROMPT_VARIANT = "raw_json"
    # ... play another 5-10 matches ...

    # Back in WSL:
    python -m tools.eval.coach_prompt_ablation \\
        --captures ~/.arenamcp/eval_compressed.jsonl ~/.arenamcp/eval_rawjson.jsonl \\
        --backend openai-compatible|http://localhost:8000/v1|gemma4:e2b \\
        --out tools/eval/data/coach_ablation_responses.jsonl

The captures are not directly comparable on a per-decision basis (they're
from different matches), but at >=30 captures per variant the aggregate
metrics (mean response length, parse rate, mean latency, judge score)
become statistically interpretable.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Iterable


def _read_jsonl(path: Path) -> Iterable[dict]:
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _synthetic_game_state() -> dict:
    """Toy game_state dict shaped like gamestate.py's MCP output.

    Not a faithful replica — just enough fields to exercise the formatter
    helpers (life, zones, mana, phase, legal moves). Real captures from a
    play session are the authoritative comparison input.
    """
    return {
        "match_id": "demo-match-id-12345678",
        "_match_number": 1,
        "turn": {"turn_number": 4, "phase": "Phase_Main1", "step": "",
                 "active_player": 1, "priority_player": 1},
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 17,
             "mana": {"R": 2, "G": 1}, "library_count": 47, "graveyard_count": 1},
            {"seat_id": 2, "is_local": False, "life_total": 14,
             "mana": {}, "library_count": 50, "graveyard_count": 2},
        ],
        "battlefield": [
            {"name": "Mountain", "controller": 1, "tapped": False, "instance_id": 101},
            {"name": "Mountain", "controller": 1, "tapped": True, "instance_id": 102},
            {"name": "Forest", "controller": 1, "tapped": False, "instance_id": 103},
            {"name": "Llanowar Elves", "controller": 1, "tapped": False, "power": 1, "toughness": 1, "instance_id": 104},
            {"name": "Mountain", "controller": 2, "tapped": False, "instance_id": 201},
            {"name": "Goblin Guide", "controller": 2, "tapped": True, "power": 2, "toughness": 2, "instance_id": 202},
        ],
        "hand": [
            {"name": "Lightning Strike", "controller": 1, "instance_id": 110},
            {"name": "Cut Down", "controller": 1, "instance_id": 111},
            {"name": "Charging Monstrosaur", "controller": 1, "instance_id": 112},
            {"name": "Forest", "controller": 1, "instance_id": 113},
        ],
        "graveyard": [{"name": "Shock", "controller": 1}],
        "stack": [],
        "exile": [],
        "command": [],
        "decision_context": {"type": "actions_available"},
        "valid_moves": ["Cast Lightning Strike", "Cast Cut Down", "Play Forest", "Pass priority"],
        # Internal noise the raw_json builder strips:
        "raw_gre_events": [{"some": "huge", "blob": "x" * 5000}],
        "legal_actions_raw": [{"actionType": "ActionType_Pass"}],
    }


def _render_demo() -> int:
    from arenamcp.coach import CoachEngine

    # Skip backend init — we only need the formatter methods.
    engine = CoachEngine.__new__(CoachEngine)
    engine._system_prompt = ""
    engine._word_tracker = None  # type: ignore  # not used by the formatters

    state = _synthetic_game_state()
    compressed = engine._format_game_context(state)
    rawjson = engine._format_game_context_raw_json(state)

    print(f">>> compressed (default): {len(compressed)} chars")
    print(compressed[:800])
    print(f"... [{len(compressed) - 800} chars omitted]" if len(compressed) > 800 else "")
    print()
    print(f">>> raw_json: {len(rawjson)} chars")
    print(rawjson[:800])
    print(f"... [{len(rawjson) - 800} chars omitted]" if len(rawjson) > 800 else "")
    print()
    ratio = len(rawjson) / len(compressed) if compressed else float("inf")
    print(f"raw_json is {ratio:.2f}x the size of compressed on this state")
    return 0


def _replay_captures(capture_paths: list[Path], backend_spec: str,
                     out_path: Path, license_key: str) -> int:
    """Replay captured prompts through a backend; emit responses + latency."""
    from tools.eval.run import BackendSpec

    spec = BackendSpec.parse(backend_spec, license_key=license_key)
    client = spec.build()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    by_variant: dict[str, list[float]] = {}
    by_variant_chars: dict[str, list[int]] = {}
    n_total = 0

    with open(out_path, "a", encoding="utf-8") as out_f:
        for cap_path in capture_paths:
            for rec in _read_jsonl(cap_path):
                variant = (rec.get("prompt_variant") or "default").lower()
                t0 = time.perf_counter()
                resp = client.complete(
                    system_prompt=rec.get("system") or "",
                    user_message=rec.get("user") or "",
                    max_tokens=rec.get("max_tokens") or 200,
                    temperature=0.0,
                )
                lat = (time.perf_counter() - t0) * 1000
                out = {
                    "ts": time.time(),
                    "capture_file": str(cap_path),
                    "prompt_variant": variant,
                    "backend": spec.label,
                    "model": rec.get("model"),
                    "user_chars": len(rec.get("user") or ""),
                    "response": resp,
                    "response_chars": len(resp or ""),
                    "latency_ms": round(lat, 1),
                }
                out_f.write(json.dumps(out, ensure_ascii=False) + "\n")
                out_f.flush()
                by_variant.setdefault(variant, []).append(lat)
                by_variant_chars.setdefault(variant, []).append(len(rec.get("user") or ""))
                n_total += 1

    print(f"\nReplayed {n_total} captures through {spec.label} -> {out_path}")
    print(f"\n{'variant':<14}{'n':>5}{'med_lat_ms':>12}{'med_user_chars':>16}")
    print("-" * 47)
    for variant in sorted(by_variant):
        lats = by_variant[variant]
        chars = by_variant_chars[variant]
        print(f"{variant:<14}{len(lats):>5}"
              f"{statistics.median(lats):>12.0f}"
              f"{statistics.median(chars):>16.0f}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    p_demo = sub.add_parser("demo", help="Render a synthetic game_state in both formats and print sizes")
    p_demo.set_defaults(_kind="demo")
    p_replay = sub.add_parser("replay", help="Replay captured prompts through a backend and record latency/output")
    p_replay.add_argument("--captures", nargs="+", type=Path, required=True)
    p_replay.add_argument("--backend", required=True,
                          help='Backend spec, e.g. "openai-compatible|http://localhost:8000/v1|gemma4:e2b"')
    p_replay.add_argument("--out", type=Path, required=True)
    p_replay.add_argument("--license-key", default="")
    p_replay.set_defaults(_kind="replay")
    args = p.parse_args()

    if args._kind == "demo":
        return _render_demo()
    return _replay_captures(args.captures, args.backend, args.out, args.license_key)


if __name__ == "__main__":
    sys.exit(main())
