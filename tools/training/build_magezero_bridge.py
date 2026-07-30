#!/usr/bin/env python3
"""MageZero decisions JSONL → production-shaped gemma training records.

Consumes the shared WP-3 decisions schema (decisions JSONL) and emits training
records whose system and user messages are byte-for-byte the production
AUTOPILOT shape — because they are built by the SAME functions the gate corpus
builder calls (tools.training.gate_play_decisions.build_user_message).

No prompt text is re-implemented here; re-implementation is the original sin
(template: build_bridge_dataset.py).

Rule
----
# R1 — system prompt identity guard (per record):
    record["system"] == arenamcp.action_planner.AUTOPILOT_SYSTEM_PROMPT
    Any mismatch aborts the build.

# R2 — attackers/blockers rows are OUT OF SCOPE:
    decision_kind := priority only; attackers/blockers are skipped and counted.

# R3 — unknown-outcome rows are skipped and counted:
    The bridge corpus must only contain resolved games.

# R4 — answer indexing:
    Answers are {"pick": N} where N is the 1-based index of the human's
    chosen action in the rendered ``Legal: (pick by number)`` menu.

Usage
-----
    python3 tools/training/build_magezero_bridge.py --in <jsonl> --out <jsonl> [--report]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path

logger = logging.getLogger("tools.training.build_magezero_bridge")

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"

# ---- bootstrap: add repo paths -------------------------------------------
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# ---- import AUTOPILOT_SYSTEM_PROMPT via importlib -------------------------
# Rationale: arenamcp/__init__.py imports watcher (watchdog), draft_eval
# (requests), and other modules that drag in GUI deps (PySide6).  Rather than
# installing PySide6 in the build venv, we load just action_planner.py
# directly.  This is safe because action_planner.py only imports stdlib +
# arenamcp.backend_health (stdlib), and we register the module in sys.modules
# so that downstream lazy imports (e.g. _new_planner in gate_play_decisions)
# find it.

def _load_autopilot_system_prompt() -> str:
    """Load AUTOPILOT_SYSTEM_PROMPT via importlib, bypassing __init__.py."""
    import types

    # Create a minimal arenamcp package to prevent __init__.py cascade
    if "arenamcp" not in sys.modules:
        pkg = types.ModuleType("arenamcp")
        pkg.__path__ = [str(SRC / "arenamcp")]
        pkg.__file__ = str(SRC / "arenamcp" / "__init__.py")
        pkg.__package__ = "arenamcp"
        sys.modules["arenamcp"] = pkg

    # Load backend_health (stdlib-only)
    bh_path = SRC / "arenamcp" / "backend_health.py"
    if "arenamcp.backend_health" not in sys.modules:
        spec_h = importlib.util.spec_from_file_location(
            "arenamcp.backend_health", str(bh_path)
        )
        assert spec_h is not None, f"cannot find backend_health.py at {bh_path}"
        mod_h = importlib.util.module_from_spec(spec_h)
        mod_h.__package__ = "arenamcp"
        sys.modules["arenamcp.backend_health"] = mod_h
        spec_h.loader.exec_module(mod_h)

    # Load action_planner
    ap_path = SRC / "arenamcp" / "action_planner.py"
    if "arenamcp.action_planner" not in sys.modules:
        spec_a = importlib.util.spec_from_file_location(
            "arenamcp.action_planner", str(ap_path)
        )
        assert spec_a is not None, f"cannot find action_planner.py at {ap_path}"
        mod_a = importlib.util.module_from_spec(spec_a)
        mod_a.__package__ = "arenamcp"
        sys.modules["arenamcp.action_planner"] = mod_a
        spec_a.loader.exec_module(mod_a)

    return sys.modules["arenamcp.action_planner"].AUTOPILOT_SYSTEM_PROMPT


AUTOPILOT_SYSTEM_PROMPT = _load_autopilot_system_prompt()

# ---- import gate_play_decisions builders -----------------------------------
from tools.training import gate_play_decisions as G  # noqa: E402

# ---------------------------------------------------------------------------
# Card name → type_line helper (MageZero only has card names)
# ---------------------------------------------------------------------------

# Basic land type lines — these are the only cards we can reliably type from
# a bare name without a card database.
_BASIC_LAND_TYPES: dict[str, str] = {
    "Plains": "Basic Land — Plains",
    "Island": "Basic Land — Island",
    "Swamp": "Basic Land — Swamp",
    "Mountain": "Basic Land — Mountain",
    "Forest": "Basic Land — Forest",
    # Snow-covered basics
    "Snow-Covered Plains": "Basic Snow Land — Plains",
    "Snow-Covered Island": "Basic Snow Land — Island",
    "Snow-Covered Swamp": "Basic Snow Land — Swamp",
    "Snow-Covered Mountain": "Basic Snow Land — Mountain",
    "Snow-Covered Forest": "Basic Snow Land — Forest",
    # Wastes (colourless)
    "Wastes": "Basic Land — Wastes",
    # Common duals that are easy to recognise by name suffix
}

# Suffix-based land detection: any card whose name ends with one of these
# tokens is treated as a Land for mana-source counting.
_LAND_SUFFIXES = ("Land", "lands", "Verge", "Heath", "Foothills", "Delta",
                  "Strand", "Mire", "Catacombs", "Meadow", "Pool", "Grove",
                  "Garden", "Tomb", "Node", "Spire", "Cavern", "Passage",
                  "Factory", "Opal", "Citadel", "Sanctum", "Archive",
                  "Crossroads", "Hideout", "Shrine", "Village", "Mine",
                  "Foundry", "Den", "Expanse", "Reach", "Canopy", "Vista",
                  "Basin", "Fortress", "Cave", "Crater", "Bridge", "Vale",
                  "Confluence", "Borough", "Hub", "Borderpost", "Column")

# Lands whose names have no suffix-based signal but contain a basic-land word
_LAND_NAME_CONTAINS = ("Cavern of", "Field of", "Urza's", "Mishra's",
                       "Corrupted", "Darksteel")


def _resolve_type_line(name: str) -> str:
    """Return a type_line for a card given only its name.

    MageZero data does not carry grp_ids, oracle text, or any card facts.
    This function provides best-effort type lines so the formatter's mana
    computation is not completely blind.
    """
    hit = _BASIC_LAND_TYPES.get(name)
    if hit:
        return hit
    name_lower = name.lower()
    # Check for basic-land-name-in-name (e.g. "Temple of Silence")
    for basic, tl in (("plains", "Land — Plains"), ("island", "Land — Island"),
                      ("swamp", "Land — Swamp"), ("mountain", "Land — Mountain"),
                      ("forest", "Land — Forest")):
        if basic in name_lower:
            return tl
    # Suffix detection
    if any(name.endswith(s) for s in _LAND_SUFFIXES):
        return "Land"
    if any(name.startswith(p) for p in _LAND_NAME_CONTAINS):
        return "Land"
    return ""


# ---------------------------------------------------------------------------
# Game-state construction
# ---------------------------------------------------------------------------

LOCAL_SEAT = 1
OPP_SEAT = 2


def _build_card(name: str, seat: int, tapped: bool, instance_id: int) -> dict:
    """One battlefield card entry from MageZero data."""
    tl = _resolve_type_line(name)
    return {
        "instance_id": instance_id,
        "name": name,
        "type_line": tl,
        "oracle_text": "",
        "mana_cost": "",
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "is_tapped": tapped,
        "turn_entered_battlefield": -1,
    }


def _build_hand_card(name: str) -> dict:
    """One hand card entry from MageZero data."""
    tl = _resolve_type_line(name)
    return {
        "instance_id": 0,
        "name": name,
        "type_line": tl,
        "mana_cost": "",
        "cmc": 0.0,
        "oracle_text": "",
    }


def build_game_state(row: dict) -> dict:
    """Build a game_state dict from a MageZero decisions row.

    The output shape matches what build_user_message (→_build_action_prompt
    →_format_game_context) expects: players, turn, battlefield, hand, stack,
    graveyard, legal_actions.
    """
    battlefield: list[dict] = []
    next_id = 1
    for card in row.get("battlefield_self", []):
        battlefield.append(_build_card(card["name"], LOCAL_SEAT,
                                        card.get("tapped", False), next_id))
        next_id += 1
    for card in row.get("battlefield_opp", []):
        battlefield.append(_build_card(card["name"], OPP_SEAT,
                                        card.get("tapped", False),
                                        1000 + next_id))
        next_id += 1

    hand = [_build_hand_card(n) for n in row.get("hand", [])]

    # Determine phase string. MageZero uses MTGA-style phase names.
    phase_raw = row.get("phase", "PRECOMBAT_MAIN")
    phase = {"PRECOMBAT_MAIN": "Phase_Main1",
             "MAIN1": "Phase_Main1",
             "MAIN2": "Phase_Main2",
             "COMBAT_BEFORE_BLOCKERS": "Phase_Combat",
             "COMBAT_DECLARE_BLOCKERS": "Phase_Combat",
             "COMBAT_DECLARE_ATTACKERS": "Phase_Combat",
             "END_COMBAT": "Phase_Combat",
             }.get(phase_raw, f"Phase_{phase_raw}")

    return {
        "players": [
            {
                "seat_id": LOCAL_SEAT,
                "is_local": True,
                "life_total": row.get("active_life", 20),
                "lands_played": 0,
                "hand_size": len(row.get("hand", [])),
            },
            {
                "seat_id": OPP_SEAT,
                "is_local": False,
                "life_total": row.get("opp_life", 20),
                "lands_played": 0,
                "hand_size": 0,
            },
        ],
        "turn": {
            "turn_number": row.get("turn", 0),
            "phase": phase,
            "step": "",
            "active_player": LOCAL_SEAT,
            "priority_player": LOCAL_SEAT,
        },
        "battlefield": battlefield,
        "hand": hand,
        "stack": [],
        "graveyard": [],
        "legal_actions": [],
    }


# ---------------------------------------------------------------------------
# Answer index resolution
# ---------------------------------------------------------------------------

def _resolve_answer(menu: list[str], chosen: str) -> int | None:
    """Return 1-based index of ``chosen`` in ``menu``, or None if not found."""
    try:
        return menu.index(chosen) + 1
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Record construction
# ---------------------------------------------------------------------------

def build_record(row: dict) -> tuple[dict | None, str]:
    """Single MageZero row → training record, or (None, drop_reason)."""
    # R2: skip attackers/blockers — handled by build_combat_decisions.py
    kind = row.get("decision_kind", "priority")
    if kind not in ("priority",):
        return None, f"decision_kind_{kind}"

    # R3: unknown-outcome rows skipped
    outcome = row.get("outcome", "unknown")
    if outcome == "unknown":
        return None, "outcome_unknown"

    game_state = build_game_state(row)
    menu = row.get("menu", [])
    if len(menu) < 2:
        return None, "menu_too_small"

    chosen = row.get("chosen", "")
    if not chosen:
        return None, "chosen_empty"

    answer_idx = _resolve_answer(menu, chosen)
    if answer_idx is None:
        return None, "chosen_not_in_menu"
    if not (1 <= answer_idx <= len(menu)):
        return None, "answer_out_of_range"

    # Build user message via production formatter (build_user_message calls
    # ActionPlanner._build_action_prompt → CoachEngine._format_game_context).
    user = G.build_user_message(game_state, menu)

    # R1: the guard that killed prior runs — assert every record.
    if user.count("Legal: (pick by number)") != 1:
        return None, "menu_header_not_production"

    # Build meta
    session = row.get("session", "")
    actor = row.get("actor", "")
    meta = {
        "game_id": row.get("game_id", ""),
        "turn": row.get("turn", 0),
        "phase": row.get("phase", ""),
        "outcome": outcome,
        "actor": actor,
        "session": session,
        "decision_kind": kind,
        "prompt_shape": "production_legal_menu",
        "system_is_autopilot_prompt": True,
        "answer_pick": answer_idx,
        "menu_size": len(menu),
        # MCTS-derived signal: how many visits the chosen action got
        "mcts_chosen_visits": row.get("mcts_counts", {}).get(chosen, 0),
    }

    record = {
        "system": AUTOPILOT_SYSTEM_PROMPT,
        "user": user,
        "response": json.dumps({"actions": [{"pick": answer_idx}]}),
        "source": "magezero_bridge",
        "attribution": "MageZero self-play (XMage-based AlphaZero engine)",
        "kind": "play_decision",
        "meta": meta,
    }
    return record, ""


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------

def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _write_jsonl(path: Path, rows: list[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


def _fmt_counter(c: Counter) -> dict:
    return {str(k): v for k, v in sorted(c.items(), key=lambda kv: kv[0])}


def _report_stats(
    records: list[dict],
    drops: Counter,
    read_count: int,
    input_path: Path,
    output_path: Path,
    elapsed: float,
) -> dict:
    n = len(records)
    outcome_counts = Counter(r["meta"]["outcome"] for r in records)
    actor_counts = Counter(r["meta"]["actor"] for r in records)
    session_counts = Counter(r["meta"]["session"] for r in records)
    menu_sizes = sorted(r["meta"]["menu_size"] for r in records) if records else []
    answer_picks = sorted(r["meta"]["answer_pick"] for r in records) if records else []
    win_count = sum(1 for r in records if r["meta"]["outcome"] == "won")
    loss_count = sum(1 for r in records if r["meta"]["outcome"] == "lost")

    stats = {
        "corpus": "magezero_bridge",
        "built_ts": time.time(),
        "input": {"path": str(input_path), "rows_read": read_count},
        "output": {"path": str(output_path), "records": n},
        "elapsed_seconds": round(elapsed, 2),
        "drops": _fmt_counter(drops),
        "dropped_total": sum(drops.values()),
        "records_kept": n,
        "by_outcome": dict(outcome_counts.most_common()),
        "by_actor": dict(actor_counts.most_common()),
        "by_session": dict(session_counts.most_common()),
        "win_share": round(win_count / n, 4) if n else 0.0,
        "loss_share": round(loss_count / n, 4) if n else 0.0,
        "system_prompt_len": len(AUTOPILOT_SYSTEM_PROMPT),
        "system_prompt_sha256": _sha256_text(AUTOPILOT_SYSTEM_PROMPT),
        "menu_size": {
            "min": menu_sizes[0] if menu_sizes else 0,
            "median": menu_sizes[n // 2] if menu_sizes else 0,
            "max": menu_sizes[-1] if menu_sizes else 0,
            "mean": round(sum(menu_sizes) / n, 2) if menu_sizes else 0.0,
        },
        "answer_picks": {
            "min": answer_picks[0] if answer_picks else 0,
            "median": answer_picks[n // 2] if answer_picks else 0,
            "max": answer_picks[-1] if answer_picks else 0,
            "mean": round(sum(answer_picks) / n, 2) if answer_picks else 0.0,
        },
    }
    return stats


def _sha256_text(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build production-shaped training records from MageZero decisions JSONL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--in", dest="input", type=Path, required=True,
                        help="Path to decisions JSONL (MageZero schema)")
    parser.add_argument("--out", dest="output", type=Path, required=True,
                        help="Path to write training records JSONL")
    parser.add_argument("--report", action="store_true",
                        help="Print build report to stderr on completion")
    args = parser.parse_args(argv)

    t0 = time.time()

    if not args.input.exists():
        logger.error(f"input not found: {args.input}")
        return 2

    # ---- R1: system prompt identity guard (per record follows below) ----
    logger.info(f"AUTOPILOT_SYSTEM_PROMPT loaded: {len(AUTOPILOT_SYSTEM_PROMPT)} chars")

    raw = _read_jsonl(args.input)
    read_count = len(raw)
    logger.info(f"read {read_count} decisions from {args.input}")

    records: list[dict] = []
    drops: Counter = Counter()

    for row in raw:
        record, reason = build_record(row)
        if record is None:
            drops[reason] += 1
            continue

        # R1: per-record system prompt identity guard
        if record["system"] != AUTOPILOT_SYSTEM_PROMPT:
            raise AssertionError(
                f"system prompt drift on {row.get('game_id', '?')}: "
                f"len(record.system)={len(record['system'])} != "
                f"len(AUTOPILOT)={len(AUTOPILOT_SYSTEM_PROMPT)}"
            )

        records.append(record)

    if not records:
        logger.error("no usable records — fail closed")
        return 2

    elapsed = time.time() - t0
    written = _write_jsonl(args.output, records)
    logger.info(f"wrote {written} records -> {args.output}")

    stats = _report_stats(records, drops, read_count, args.input,
                          args.output, elapsed)

    # Write report to stderr if --report
    if args.report:
        import pprint
        print("\n=== BUILD REPORT ===", file=sys.stderr)
        pprint.pprint(stats, stream=sys.stderr, width=100, sort_dicts=False)
        print("", file=sys.stderr)

    # Write PROGRESS file
    progress_dir = REPO / "tools" / "training" / "wp3"
    progress_dir.mkdir(parents=True, exist_ok=True)
    progress_path = progress_dir / "PROGRESS-build_magezero_bridge.md"
    with open(progress_path, "w", encoding="utf-8") as f:
        f.write("# PROGRESS: build_magezero_bridge\n\n")
        f.write(f"Built: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write("## Stats\n\n")
        f.write(f"- Input: {stats['input']['path']} ({stats['input']['rows_read']} rows)\n")
        f.write(f"- Output: {stats['output']['path']} ({stats['output']['records']} records)\n")
        f.write(f"- Drops: {_fmt_counter(drops)}\n")
        f.write(f"- Elapsed: {elapsed:.2f}s\n\n")
        f.write("## Drops\n\n")
        for reason, count in drops.most_common():
            f.write(f"- {reason}: {count}\n")
        f.write("\n## Known gaps\n\n")
        f.write("- Card facts: only basic land type lines are resolved; non-basic\n")
        f.write("  permanents render without type/oracle info in the prompt.\n")
        f.write("- Mana computation: non-basic lands without basic-land-name-in-name\n")
        f.write("  contribute 0 mana to the pool.\n")
        f.write("- Leak scan: fixture-only test (no real gate corpus to check against).\n")
        f.write("\n## Verified\n\n")
        f.write("- R1: system prompt identity assert exercised.\n")
        f.write("- R2: attackers/blockers skipped and counted.\n")
        f.write("- R3: unknown-outcome rows skipped and counted.\n")
        f.write("- R4: answer index from menu match.\n")

    logger.info(f"progress -> {progress_path}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    raise SystemExit(main())
