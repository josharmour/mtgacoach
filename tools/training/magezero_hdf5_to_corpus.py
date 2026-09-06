#!/usr/bin/env python3
"""MageZero generation-5+ full-state self-play HDF5 -> frontier-distillation corpus.

Ranked-pipeline step #3: "MageZero games as the distillation corpus".  The
existing distillation track reads *XMage replay/decision logs*
(``parse_magezero_log.py`` -> decisions JSONL -> ``build_magezero_bridge.py`` /
``build_magezero_combat.py``).  As recorded in ``data/dsv4_labels_v1_manifest.json``
(known_limits), those replay logs carry ``no counters/auras/damage marked;
P/T is base printed only`` and *combat decisions are excluded for that reason*.

MageZero's generation-5+ self-play **game records** (HDF5 shards under
``data/<Deck>/ver<v>/training/session<sid>_<Deck>_vs_<Opp>.hdf5``, one per game)
DO carry the full state as the network sees it: sparse bags of feature indices
decoded by the engine's ``FeatureTable.txt`` vocabulary (life totals, mana,
counters [+1/+1, oil], P/T pumps, damage, zones, creature types, full oracle
text, combat/attack/block indicators, racing/sweeper signals).  This converter
is the convergence point of the two teacher tracks: it turns those full-state
game records into dsv4-shaped distillaion training instances.

Schema (matches ``data/dsv4_sft_v1.json``, byte-compatible ``system``):
    {"system": AUTOPILOT_SYSTEM_PROMPT,
     "user":   <full-state GAME block + numbered Legal action menu>,
     "response": json.dumps({"actions": [{"pick": N, "action_type": ...,
                                          "reasoning": ...}], "voice_advice": ...}),
     "meta":   {...provenance}}

CPU-only.  READ-ONLY on the fleet's game files: copy originals to a scratch
dir and point --h5 there (never open the live training dir for writing).

Usage
-----
    python3 magezero_hdf5_to_corpus.py \\
        --h5 /tmp/mzconv/samples/session148_UWTempo_vs_GBLegends.hdf5 \\
        --h5 /tmp/mzconv/samples/session150_UWTempo_vs_EVG_Goblins.hdf5 \\
        --feature-table /home/joshu/repos/magezero/.mz_tmp/hosts/blackwell/xmage/FeatureTable.txt \\
        --out /tmp/mzconv/mzcorpus_proof.jsonl --gen 5 --max-points 5

    # read ALL shards in a training dir (at scale, later):
        ... --dir /home/joshu/repos/magezero/data/UWTempo/ver2/training --gen 5
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"

# ---------------------------------------------------------------------------
# AUTOPILOT_SYSTEM_PROMPT bootstrap (byte-for-byte same as build_magezero_bridge)
# ---------------------------------------------------------------------------

def _load_autopilot_system_prompt() -> str:
    """Load AUTOPILOT_SYSTEM_PROMPT via importlib, bypassing __init__.py."""
    import types
    if "arenamcp" not in sys.modules:
        pkg = types.ModuleType("arenamcp")
        pkg.__path__ = [str(SRC / "arenamcp")]
        pkg.__file__ = str(SRC / "arenamcp" / "__init__.py")
        pkg.__package__ = "arenamcp"
        sys.modules["arenamcp"] = pkg
    bh = SRC / "arenamcp" / "backend_health.py"
    if "arenamcp.backend_health" not in sys.modules:
        spec = importlib.util.spec_from_file_location("arenamcp.backend_health", str(bh))
        assert spec is not None and spec.loader is not None, "backend_health"
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = "arenamcp"
        sys.modules["arenamcp.backend_health"] = mod
        spec.loader.exec_module(mod)
    ap = SRC / "arenamcp" / "action_planner.py"
    if "arenamcp.action_planner" not in sys.modules:
        spec = importlib.util.spec_from_file_location("arenamcp.action_planner", str(ap))
        assert spec is not None and spec.loader is not None, "action_planner"
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = "arenamcp"
        sys.modules["arenamcp.action_planner"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["arenamcp.action_planner"].AUTOPILOT_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# FeatureTable.txt parsing
# ---------------------------------------------------------------------------

_ENTRY = re.compile(r"\[([^\]\[]*?)#(\d+)\]")


def load_feature_table(path: str) -> dict[int, list[str]]:
    """index -> list of human-readable feature texts (drop leading hashed id)."""
    table: dict[int, list[str]] = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = re.match(r"^(\d+):\s*(.*)$", line.strip())
            if not m:
                continue
            i = int(m.group(1))
            texts: list[str] = []
            for body, _cnt in _ENTRY.findall(m.group(2)):
                if "/" in body:
                    body = body.split("/", 1)[1]
                body = body.strip()
                if body:
                    texts.append(body)
            if texts:
                table[i] = texts
    return table


# Action-like tokens: concrete executable thing the MCTS could / did do.
_RE_ACTION = re.compile(
    r"^(Cast |Play |Activate |Sacrifice |Declare |Attack|Block|Choose )", re.I
)
# Mastery/state signals the replay corpus cannot represent.
_RE_MASTERY = re.compile(
    r"(\+1/\+1 counter|oil counter|counter.*on \{this\}|LifeTotal@|"
    r"Power@|Toughness@|Damage@|combat damage|attacks|blocks|double strike|"
    r"first strike|toxic|deathtouch|reach|flying|sweeper|destroy all)",
    re.I,
)


def decode_state(indices: Iterable[int], table: dict[int, list[str]]) -> Counter:
    """Sparse feature bag -> dict of text -> occurrence count."""
    c: Counter = Counter()
    for fi in indices:
        fi = int(fi)
        for t in table.get(fi, ()):
            c[t] += 1
    return c


def action_tokens(c: Counter) -> list[str]:
    """Concrete executable actions present in the state, most-common first."""
    out = []
    for t, n in c.most_common():
        if _RE_ACTION.match(t) and t.strip():
            out.append(t)
    return out


def mastery_tokens(c: Counter) -> list[str]:
    """Full-state mastery signals (counters / P-T / damage / combat / racing)."""
    out = []
    for t, n in c.most_common():
        if _RE_MASTERY.search(t):
            out.append(t)
    return out


# ---------------------------------------------------------------------------
# HDF5 reading (h5py) -- CPU only
# ---------------------------------------------------------------------------

def read_game(path: str):
    import h5py, numpy as np  # local: converter stays importable without h5py
    f = h5py.File(path, "r")
    off = f["/offsets"][...].astype(np.int64, copy=False)
    idx = f["/indices"][...].astype(np.int64, copy=False)
    row = f["/row"][...].astype(np.float32, copy=False)
    n = int(off.shape[0] - 1)
    if n != int(row.shape[0]):
        raise ValueError(f"offsets rows {n} != row rows {row.shape[0]}")
    a = int(row.shape[1] - 4)
    # keep a closed view so h5py.File GC is safe
    return {"offsets": off, "indices": idx, "row": row, "N": n, "A": a, "path": path}


# ---------------------------------------------------------------------------
# Build a dsv4-shaped instance
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r"session(\d+)_(.+?)_vs_(.+)\.hdf5$")


def parse_game_name(filename: str) -> tuple[object, str, str]:
    m = _NAME_RE.search(filename)
    if not m:
        return None, Path(filename).stem, ""
    return int(m.group(1)), m.group(2), m.group(3)


def decision_to_instance(g: dict, k: int, table: dict[int, list[str]],
                         system: str, gen: int, deck: str, opp: str,
                         sid: object) -> dict[str, Any] | None:
    a, b = int(g["offsets"][k]), int(g["offsets"][k + 1])
    if b <= a:
        return None  # empty decision slot
    sv = g["indices"][a:b]
    row = g["row"][k]
    tail = row[g["A"]:g["A"] + 4]
    result_label = float(tail[0])
    state_score = float(tail[1])
    is_player = "A" if float(tail[2]) > 0.5 else "B"
    action_type = int(tail[3])
    policy = row[:g["A"]]
    policy_argmax = int(policy.argmax()) if g["A"] else -1

    c = decode_state(sv, table)
    acts = action_tokens(c)
    mastery = mastery_tokens(c)

    # Life total feature (best-effort)
    life = [t for t in c if t.startswith("LifeTotal@")]

    # ---- render USER (full-state GAME block + Legal menu) ------------------
    lines = [f"GRID: magezero gen{gen} | {deck} vs {opp} | decision #{k} | player {is_player}"]
    if life:
        lines.append("LIFE: " + ", ".join(life[:4]))
    if mastery:
        lines.append("MASTERY[counters/p-t/damage/combat]:")
        for t in mastery[:40]:
            lines.append(f"  - {t}")
    lines.append("STATE (decoded feature bag, top-40):")
    for t, n in c.most_common(40):
        lines.append(f"  {t}" + (f"  (x{n})" if n > 1 else ""))
    lines.append("Legal: (pick by number)")
    chosen = -1
    if acts:
        for i, t in enumerate(acts, 1):
            lines.append(f"  {i}. {t}")
        # choose the top action as the MCTS-inferred play (see report caveat)
        chosen = 1
    else:
        lines.append("  1. Pass")
        chosen = 1
    lines.append(
        f"value: result_label={result_label:+.2f} state_score={state_score:+.2f} "
        f"policy_argmax={policy_argmax} action_type={action_type}"
    )
    user = "\n".join(lines)

    # ---- render RESPONSE ----------------------------------------------------
    chosen_text = acts[0] if acts else "Pass"
    plan = {
        "actions": [{
            "pick": chosen,
            "action_type": "priority_action",
            "reasoning": (
                f"MageZero self-play (full-state) line at decision {k}: "
                f"MCTS-selected '{chosen_text}' "
                f"(state_score {state_score:+.2f}, result_label {result_label:+.2f})."
            ),
        }],
        "voice_advice": f"Play {chosen_text} per MageZero full-state search.",
    }
    response = json.dumps(plan)

    ident = f"session{sid}_{deck}_vs_{opp}:d{k}"
    meta = {
        "source": "magezero_hdf5",
        "game": g.get("path", ""),
        "game_id": f"session{sid}_{deck}_vs_{opp}",
        "gen": gen,
        "deck": deck,
        "opponent": opp,
        "decision_index": k,
        "is_player": is_player,
        "action_type": action_type,
        "result_label": round(result_label, 3),
        "state_score": round(state_score, 3),
        "policy_argmax": policy_argmax,
        "chosen_action": chosen_text,
        "num_state_features": int(b - a),
        "num_action_tokens": len(acts),
        "gen5_full_state": bool(mastery),
        "teacher": "magezero_selfplay",
        "id": ident,
        "sha": hashlib.sha256(
            (ident + "|" + json.dumps(plan, sort_keys=True)).encode()
        ).hexdigest()[:12],
    }
    return {"system": system, "user": user, "response": response, "meta": meta}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--h5", action="append", default=[], help="one or more HDF5 game files")
    ap.add_argument("--dir", default=None, help="dir of *.hdf5 shards to convert")
    ap.add_argument("--feature-table", required=True, help="magezero xmage FeatureTable.txt")
    ap.add_argument("--out", required=True, help="output .jsonl")
    ap.add_argument("--gen", type=int, default=5, help="generation label for provenance")
    ap.add_argument("--deck", default=None, help="overrides deck parsed from filename")
    ap.add_argument("--opponent", default=None)
    ap.add_argument("--max-points", type=int, default=None,
                    help="cap decision points per game (smoke test)")
    ap.add_argument("--stride", type=int, default=1,
                    help="only convert every Nth decision point (corpus thinning)")
    ap.add_argument("--print", action="store_true",
                    help="print the first converted entry to stdout")
    args = ap.parse_args()

    table = load_feature_table(args.feature_table)
    system = _load_autopilot_system_prompt()

    paths: list[str] = list(args.h5)
    if args.dir:
        paths += sorted(str(p) for p in Path(args.dir).glob("*.hdf5"))
    if not paths:
        print("no input files; use --h5 or --dir", file=sys.stderr)
        return 2

    n_entries = 0
    n_games = 0
    with open(args.out, "w", encoding="utf-8") as out:
        for p in paths:
            g = read_game(p)
            sid, deck, opp = parse_game_name(Path(p).name)
            deck = args.deck or deck
            opp = args.opponent or opp
            limit = args.max_points if args.max_points is not None else g["N"]
            first = None
            for k in range(0, min(limit, g["N"]), args.stride):
                rec = decision_to_instance(g, k, table, system, args.gen,
                                           deck, opp, sid)
                if rec is None:
                    continue
                out.write(json.dumps(rec) + "\n")
                n_entries += 1
                if first is None:
                    first = rec
            n_games += 1
            print(f"[{Path(p).name}] N={g['N']} A={g['A']} -> converted", file=sys.stderr)
            if args.print and first is not None:
                print(json.dumps(first, indent=2))
    print(f"OK: {n_games} game(s), {n_entries} entry/entries -> {args.out}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
