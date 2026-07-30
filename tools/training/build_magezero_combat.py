#!/usr/bin/env python3
"""MageZero decisions JSONL → combat-gate-shaped attackers/blockers records.

Consumes the shared WP-3 decisions schema (decisions JSONL) and emits
training records whose answer shape matches what
tools/training/gate_combat_decisions.py expects:
  attack:  {"actions":[{"action_type":"declare_attackers","attacker_names":["A","B"]}]}
  block:   {"actions":[{"action_type":"declare_blockers","blocker_assignments":{"Y":"X"}}]}

Only decision_kind in (attackers, blockers) rows are consumed; all others
are skipped and counted.

Solver lines: OFF (the equivalent of --solver-line off).  Every record carries
NO_SOLVER_ADDENDUM and no mcts_counts leak into the prompt.

Class-histogram guard
---------------------
The report now prints per-class shares *with percentages* so a build skew
-- the original 71.8 % no_block that went undetected — is visible at a
glance.  If any single class exceeds ``--max-class-share`` (default 0.50)
the build ABORTS with the histogram, unless ``--allow-skewed`` is passed.
Modeled on the solver-line fail-closed guard in build_combat_decisions.py.

Rebalance mode
--------------
``--balance downsample`` deterministically (seed=7) downsamples the
majority class toward the threshold, reporting what was dropped.
No upsampling or duplication.

Usage
-----
    python3 tools/training/build_magezero_combat.py \
        --in <decisions.jsonl> --out <combat_records.jsonl> [--report]

    # guard will abort on the real corpus; pass --allow-skewed to force:
    python3 tools/training/build_magezero_combat.py \
        --in <decisions.jsonl> --out <combat_records.jsonl> --report --allow-skewed

    # downsample the majority class toward 0.50:
    python3 tools/training/build_magezero_combat.py \
        --in <decisions.jsonl> --out <combat_records.jsonl> --report \
        --balance downsample --allow-skewed
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

logger = logging.getLogger("tools.training.build_magezero_combat")

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"

for _p in (str(SRC), str(REPO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── Import AUTOPILOT_SYSTEM_PROMPT via importlib (avoids PySide6 cascade) ──


def _load_autopilot_system_prompt() -> str:
    import importlib.util
    import types

    if "arenamcp" not in sys.modules:
        pkg = types.ModuleType("arenamcp")
        pkg.__path__ = [str(SRC / "arenamcp")]
        pkg.__file__ = str(SRC / "arenamcp" / "__init__.py")
        pkg.__package__ = "arenamcp"
        sys.modules["arenamcp"] = pkg
    bh_path = SRC / "arenamcp" / "backend_health.py"
    if "arenamcp.backend_health" not in sys.modules:
        spec_h = importlib.util.spec_from_file_location("arenamcp.backend_health", str(bh_path))
        assert spec_h is not None, f"cannot find backend_health.py at {bh_path}"
        mod_h = importlib.util.module_from_spec(spec_h)
        mod_h.__package__ = "arenamcp"
        sys.modules["arenamcp.backend_health"] = mod_h
        spec_h.loader.exec_module(mod_h)
    ap_path = SRC / "arenamcp" / "action_planner.py"
    if "arenamcp.action_planner" not in sys.modules:
        spec_a = importlib.util.spec_from_file_location("arenamcp.action_planner", str(ap_path))
        assert spec_a is not None, f"cannot find action_planner.py at {ap_path}"
        mod_a = importlib.util.module_from_spec(spec_a)
        mod_a.__package__ = "arenamcp"
        sys.modules["arenamcp.action_planner"] = mod_a
        spec_a.loader.exec_module(mod_a)
    return sys.modules["arenamcp.action_planner"].AUTOPILOT_SYSTEM_PROMPT


AUTOPILOT_SYSTEM_PROMPT = _load_autopilot_system_prompt()

# ── Constant-policy classes (guard-eligible) ──────────────────────────────

CONSTANT_POLICY_CLASSES: frozenset[str] = frozenset(
    {
        "no_block",
        "block_all",
        "all_in",
        "no_attack",
    }
)
"""Classes that are CONSTANT POLICIES — the model can answer without reading
the board (always block nothing, always block everything, attack with all,
attack with none).  Their share IS the score a do-nothing model achieves.
``proper_subset`` and ``partial_block`` are NOT single answers — the model
must still choose WHICH creatures — so high share there is harmless."""

# ── Import from the combat corpus builder (never re-implement) ─────────────

from tools.training.build_combat_decisions import (  # noqa: E402
    ATTACK_ADDENDUM,
    BLOCK_ADDENDUM,
    NO_SOLVER_ADDENDUM,
    RECONSTRUCTED_ADDENDUM,
    TRIGGER_ATTACK,
    TRIGGER_BLOCK,
    DONE_ATTACK_ROW,
    DONE_BLOCK_ROW,
)

# ── Card-detection helpers (MZ only has card names) ───────────────────────

# Best-effort land detection — only needed to exclude lands from the
# attack/block pool (MZ data has no grp_ids or type lines).
_BASIC_LANDS = frozenset(
    {
        "Plains",
        "Island",
        "Swamp",
        "Mountain",
        "Forest",
        "Snow-Covered Plains",
        "Snow-Covered Island",
        "Snow-Covered Swamp",
        "Snow-Covered Mountain",
        "Snow-Covered Forest",
        "Wastes",
    }
)
_LAND_SUFFIXES = (
    "Land",
    "lands",
    "Verge",
    "Heath",
    "Foothills",
    "Delta",
    "Strand",
    "Mire",
    "Catacombs",
    "Meadow",
    "Pool",
    "Grove",
    "Garden",
    "Tomb",
    "Node",
    "Spire",
    "Cavern",
    "Passage",
    "Factory",
    "Opal",
    "Citadel",
    "Sanctum",
    "Archive",
    "Crossroads",
    "Hideout",
    "Shrine",
    "Village",
    "Mine",
    "Foundry",
    "Den",
    "Expanse",
    "Reach",
    "Canopy",
    "Vista",
    "Basin",
    "Fortress",
    "Cave",
    "Crater",
    "Bridge",
    "Vale",
    "Confluence",
    "Borough",
    "Hub",
    "Borderpost",
    "Column",
)
_LAND_NAME_CONTAINS = ("Cavern of", "Field of", "Urza's", "Mishra's", "Corrupted", "Darksteel")


def _is_land(name: str) -> bool:
    if name in _BASIC_LANDS:
        return True
    if any(name.endswith(s) for s in _LAND_SUFFIXES):
        return True
    if any(name.startswith(p) for p in _LAND_NAME_CONTAINS):
        return True
    return False


# ── Combat-suffix helpers ──────────────────────────────────────────────────

_COMBAT_SUFFIXES = (",attacking", ",blocking")


def _strip_combat_suffix(name: str) -> tuple[str, str | None]:
    """Return (clean_name, state) where state in (attacking, blocking, None)."""
    for suffix in _COMBAT_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)], suffix[1:]  # remove leading comma
    return name, None


def _is_attacking(name: str) -> bool:
    return name.endswith(",attacking")


def _is_blocking(name: str) -> bool:
    return name.endswith(",blocking")


def _known_combat_state(name: str) -> bool:
    return _is_attacking(name) or _is_blocking(name)


# ── Name disambiguation (matches build_combat_decisions.disambiguate) ──────


def _disambiguate(names: list[str]) -> list[str]:
    """Byte-identical to build_combat_decisions.disambiguate."""
    counts = Counter(names)
    seen: dict[str, int] = {}
    out: list[str] = []
    for name in names:
        if counts[name] > 1:
            seen[name] = seen.get(name, 0) + 1
            out.append(f"{name} #{seen[name]}")
        else:
            out.append(name)
    return out


# ── Response formatters (thin wrappers matching build_combat_decisions) ────


def _attack_response(names: list[str]) -> str:
    return json.dumps(
        {"actions": [{"action_type": "declare_attackers", "attacker_names": names}]},
        ensure_ascii=False,
    )


def _block_response(assignments: dict[str, str]) -> str:
    return json.dumps(
        {"actions": [{"action_type": "declare_blockers", "blocker_assignments": assignments}]},
        ensure_ascii=False,
    )


# ── System prompt builder (matches build_combat_decisions.build_system) ────


def _build_system(kind: str) -> str:
    addendum = ATTACK_ADDENDUM if kind == "attack" else BLOCK_ADDENDUM
    addendum += NO_SOLVER_ADDENDUM
    addendum += RECONSTRUCTED_ADDENDUM
    return AUTOPILOT_SYSTEM_PROMPT + "\n" + addendum


# ── User prompt builders ───────────────────────────────────────────────────


def _user_prompt_attack(
    row: dict,
    pool_names: list[str],
    declared_names: list[str],
) -> str:
    """Build a user prompt for an attack decision from MZ data."""
    declared_set = set(declared_names)
    menu = [f"  {i + 1}. Attack with: {n}" for i, n in enumerate(pool_names)]
    menu.append(f"  {len(menu) + 1}. {DONE_ATTACK_ROW}")
    life = row.get("active_life", 20)
    opp_life = row.get("opp_life", 20)
    turn = row.get("turn", 0)
    lines = [
        TRIGGER_ATTACK,
        "",
        "=== GAME ===",
        "Legal: (pick by number)",
        *menu,
        f"T{turn} YOUR | Combat | Pri:You",
        "Pending decision: Declare Attackers",
        f"Life: You={life} Opp={opp_life}",
        "",
        "YOUR BOARD:",
    ]
    self_lines = _board_lines(row, "battlefield_self")
    lines.extend(self_lines or ["  (empty)"])
    lines.append("")
    lines.append("OPP BOARD:")
    opp_lines = _board_lines(row, "battlefield_opp")
    lines.extend(opp_lines or ["  (empty)"])
    return "\n".join(lines)


def _user_prompt_block(
    row: dict,
    blocker_names: list[str],
    attacker_names: list[str],
    declared_assignments: dict[str, str],
) -> str:
    """Build a user prompt for a block decision from MZ data."""
    menu = [f"  {i + 1}. Block with: {n}" for i, n in enumerate(blocker_names)]
    menu.append(f"  {len(menu) + 1}. {DONE_BLOCK_ROW}")
    life = row.get("active_life", 20)
    opp_life = row.get("opp_life", 20)
    turn = row.get("turn", 0)
    lines = [
        TRIGGER_BLOCK,
        "",
        "=== GAME ===",
        "Legal: (pick by number)",
        *menu,
        f"T{turn} OPP | Combat | Pri:You",
        "Pending decision: Declare Blockers",
        f"Life: You={life} Opp={opp_life}",
        "",
        "ATTACKING YOU:",
    ]
    for name in attacker_names:
        lines.append(f"  {name}")
    lines.append("")
    lines.append("YOUR BOARD:")
    self_lines = _board_lines(row, "battlefield_self")
    lines.extend(self_lines or ["  (empty)"])
    lines.append("")
    lines.append("OPP BOARD:")
    opp_lines = _board_lines(row, "battlefield_opp")
    lines.extend(opp_lines or ["  (empty)"])
    return "\n".join(lines)


def _board_lines(row: dict, side: str) -> list[str]:
    """Build board lines from MZ battlefield data (names sorted, duplicates grouped)."""
    seen: list[str] = []
    counts: Counter = Counter()
    for p in row.get(side, []):
        name, _state = _strip_combat_suffix(p["name"])
        counts[name] += 1
    out: list[str] = []
    for name in sorted(counts):
        n = counts[name]
        line = f"  {name}"
        if n > 1:
            line += f" x{n}"
            out.append(line)
        else:
            out.append(line)
    return out


# ── Record construction ────────────────────────────────────────────────────


def _rid(key: str) -> str:
    return f"mzc-{hashlib.sha1(key.encode()).hexdigest()[:16]}"


def _classify_attack(declared: list[str], pool: list[str]) -> str:
    if not declared:
        return "no_attack"
    if len(declared) == len(pool):
        return "all_in"
    return "proper_subset"


def _classify_block(declared: dict[str, str], pool_n: int) -> str:
    if not declared:
        return "no_block"
    if len(declared) == pool_n:
        return "block_all"
    return "partial_block"


def _match_declared_to_pool(
    pool_raw: list[str],
    pool_disambiguated: list[str],
    declared_raw: list[str],
) -> list[str]:
    """Positionally match declared names to disambiguated pool names.

    ``pool_raw`` is the undeduplicated name list; ``pool_disambiguated`` is
    ``_disambiguate(pool_raw)``.  ``declared_raw`` is a SUBSET of the pool
    (with duplicates).  Return the disambiguated names of the declared entries
    in order, consuming pool copies left-to-right.
    """
    used: list[bool] = [False] * len(pool_raw)
    out: list[str] = []
    for d in declared_raw:
        for i, (raw_name, is_used) in enumerate(zip(pool_raw, used)):
            if not is_used and raw_name == d:
                used[i] = True
                out.append(pool_disambiguated[i])
                break
    return out


def build_attack_record(row: dict) -> tuple[dict | None, str]:
    """Single attackers row → training record, or (None, reason)."""
    # Extract creatures on self, strip combat suffixes
    self_raw: list[tuple[str, str | None]] = []
    for p in row.get("battlefield_self", []):
        name, state = _strip_combat_suffix(p["name"])
        if _is_land(name):
            continue
        if state and state != "attacking":
            continue  # skip blocking markers during attackers phase
        # Tapped creatures that are NOT attacking are summoning-sick
        # or were tapped by a prior spell — cannot attack.
        # Attacking creatures ARE tapped (declaring taps them), so allow.
        if p.get("tapped", False) and state != "attacking":
            continue
        self_raw.append((name, state))

    if not self_raw:
        return None, "attack_no_creatures"

    # Attack pool: all creatures (regardless of attacking state)
    pool = _disambiguate([n for n, _ in self_raw])

    # Declared attackers: creatures marked ,attacking
    declared_raw = [n for n, s in self_raw if s == "attacking"]
    if not declared_raw:
        # No attackers declared at this decision point — the MCTS chose
        # a spell/ability before any attack declaration.  Drop: this is
        # a priority action during combat, not a combat decision.
        return None, "attack_no_declared"

    declared = _match_declared_to_pool([n for n, _ in self_raw], pool, declared_raw)

    user = _user_prompt_attack(row, pool, declared)
    rec = {
        "id": _rid(row.get("game_id", "") + ":atk:" + str(row.get("turn", 0))),
        "system": _build_system("attack"),
        "user": user,
        "response": _attack_response(declared),
        "source": "magezero_combat",
        "attribution": "MageZero self-play (XMage-based AlphaZero engine)",
        "kind": "combat_attack",
        "meta": {
            "game_id": row.get("game_id", ""),
            "turn": row.get("turn", 0),
            "phase": row.get("phase", ""),
            "decision_kind": "attackers",
            "pool_size": len(pool),
            "chosen_size": len(declared),
            "chosen_names": declared,
            "answer_shape": "declare_attackers/attacker_names",
            "is_all_in": len(declared) == len(pool) > 0,
            "is_no_attack": not declared,
            "is_proper_subset": 0 < len(declared) < len(pool),
            "attack_class": _classify_attack(declared, pool),
            "solver_line_rendered": False,
            "session": row.get("session", ""),
            "outcome": row.get("outcome", "unknown"),
            "actor": row.get("actor", ""),
            "reconstructed_from": "magezero",
            "mcts_chosen": row.get("chosen", ""),
        },
    }
    # Prove no solver line and no mcts_counts in the prompt
    assert "Computed optimal" not in rec["user"], "solver line leaked into prompt"
    assert "count" not in rec["user"].lower() or "discard" not in rec["user"].lower(), (
        "mcts_counts may have leaked"
    )
    return rec, ""


def build_block_record(row: dict) -> tuple[dict | None, str]:
    """Single blockers row → training record, or (None, reason)."""
    # Extract self creatures with ,blocking suffix (PlayerA's blockers)
    self_blockers_raw: list[str] = []
    for p in row.get("battlefield_self", []):
        name, state = _strip_combat_suffix(p["name"])
        if state == "blocking":
            self_blockers_raw.append(name)

    # Extract opp creatures with ,attacking suffix (opponent's attackers)
    opp_attackers: list[str] = []
    for p in row.get("battlefield_opp", []):
        name, state = _strip_combat_suffix(p["name"])
        if state == "attacking":
            opp_attackers.append(name)

    # If neither side has clear markers, drop as ambiguous
    if not self_blockers_raw and not opp_attackers:
        return None, "block_no_markers"

    # Build blocker pool: all non-land self creatures that aren't marked attacking
    # (include ,blocking creatures even if tapped — blockers cannot normally be
    # tapped, but the MZ data's tapped flag may reflect a prior combat sub-phase)
    blocker_pool_raw: list[str] = []
    for p in row.get("battlefield_self", []):
        name, state = _strip_combat_suffix(p["name"])
        if _is_land(name):
            continue
        if state == "attacking":
            continue  # these creatures already attacked, can't block
        if p.get("tapped", False) and state != "blocking":
            continue  # non-blocking tapped creatures can't block
        blocker_pool_raw.append(name)
    blocker_pool = _disambiguate(blocker_pool_raw)

    # Build attacker roster from opp battlefield
    attacker_roster_raw: list[str] = []
    for p in row.get("battlefield_opp", []):
        name, _state = _strip_combat_suffix(p["name"])
        if _is_land(name):
            continue
        attacker_roster_raw.append(name)
    attacker_roster = _disambiguate(attacker_roster_raw)

    # Handle the reverse pattern: self has ,attacking and opp has ,blocking
    # This is the most common pattern (69/199 rows).  In XMage's log, these
    # markers indicate creatures that were declared during a PREVIOUS
    # declare_attackers/blockers sub-phase of the SAME turn.  The actual
    # attackers for THIS blockers decision are the opp creatures with
    # ,attacking — but in this pattern, no opp creature has ,attacking.
    # We cannot determine the blocking assignment from ambiguous markers.
    self_attacking = any(_is_attacking(p["name"]) for p in row.get("battlefield_self", []))
    opp_blocking = any(_is_blocking(p["name"]) for p in row.get("battlefield_opp", []))
    if self_attacking and opp_blocking:
        return None, "block_reversed_markers_ambiguous"

    if not self_blockers_raw:
        # No blockers declared — opponent's attackers are known
        if not opp_attackers:
            return None, "block_no_attackers_either"
        user = _user_prompt_block(row, blocker_pool, opp_attackers, {})
        rec = {
            "id": _rid(row.get("game_id", "") + ":blk:" + str(row.get("turn", 0))),
            "system": _build_system("block"),
            "user": user,
            "response": _block_response({}),
            "source": "magezero_combat",
            "attribution": "MageZero self-play (XMage-based AlphaZero engine)",
            "kind": "combat_block",
            "meta": {
                "game_id": row.get("game_id", ""),
                "turn": row.get("turn", 0),
                "phase": row.get("phase", ""),
                "decision_kind": "blockers",
                "blocker_pool_size": len(blocker_pool),
                "n_attackers": len(opp_attackers),
                "n_blocks": 0,
                "answer_shape": "declare_blockers/blocker_assignments",
                "is_no_block": True,
                "is_partial_block": False,
                "block_class": "no_block",
                "solver_line_rendered": False,
                "session": row.get("session", ""),
                "outcome": row.get("outcome", "unknown"),
                "actor": row.get("actor", ""),
                "reconstructed_from": "magezero",
                "mcts_chosen": row.get("chosen", ""),
            },
        }
        assert "Computed optimal" not in rec["user"]
        return rec, ""

    # Blockers declared. Map: if exactly 1 attacker, all blockers map to it.
    if len(opp_attackers) != 1:
        return None, f"block_{len(opp_attackers)}_attackers_cannot_pair"

    # Match declared blockers against the pool for correct disambiguation
    declared_blockers = _match_declared_to_pool(blocker_pool_raw, blocker_pool, self_blockers_raw)
    target_name = opp_attackers[0]
    assignments = {b: target_name for b in declared_blockers}

    user = _user_prompt_block(row, blocker_pool, opp_attackers, assignments)
    rec = {
        "id": _rid(row.get("game_id", "") + ":blk:" + str(row.get("turn", 0))),
        "system": _build_system("block"),
        "user": user,
        "response": _block_response(assignments),
        "source": "magezero_combat",
        "attribution": "MageZero self-play (XMage-based AlphaZero engine)",
        "kind": "combat_block",
        "meta": {
            "game_id": row.get("game_id", ""),
            "turn": row.get("turn", 0),
            "phase": row.get("phase", ""),
            "decision_kind": "blockers",
            "blocker_pool_size": len(blocker_pool),
            "n_attackers": len(opp_attackers),
            "n_blocks": len(assignments),
            "answer_shape": "declare_blockers/blocker_assignments",
            "is_no_block": False,
            "is_partial_block": len(assignments) < len(blocker_pool),
            "block_class": _classify_block(assignments, len(blocker_pool)),
            "mapping_source": "forced_single_attacker",
            "solver_line_rendered": False,
            "session": row.get("session", ""),
            "outcome": row.get("outcome", "unknown"),
            "actor": row.get("actor", ""),
            "reconstructed_from": "magezero",
            "mcts_chosen": row.get("chosen", ""),
        },
    }
    assert "Computed optimal" not in rec["user"]
    return rec, ""


# ── IO and CLI ─────────────────────────────────────────────────────────────

from typing import Callable


def _class_histogram(
    records: list[dict], field: str, predicate: Callable[[dict], bool]
) -> list[tuple[str, int, float]]:
    """Return sorted [(class, count, share)] for matching records.

    ``predicate`` selects the subset (attack vs block).  Sorted by share
    descending so the dominant class appears first — exactly what the
    fail-closed guard needs to catch a 71.8 % skew.
    """
    subset = [r for r in records if predicate(r)]
    total = len(subset)
    if total == 0:
        return []
    counts = Counter(r["meta"].get(field, "?") for r in subset)
    items = [(cls, n, round(n / total, 4)) for cls, n in counts.items()]
    items.sort(key=lambda x: (-x[2], x[0]))
    return items


def _check_class_share(
    hist: list[tuple[str, int, float]],
    max_share: float,
    label: str,
    guard_classes: frozenset[str] | None = None,
) -> None:
    """Raise SystemExit if a guard-eligible class in *hist* exceeds *max_share*.

    Only classes in *guard_classes* trigger the fail-closed guard — these are
    CONSTANT POLICIES (no_block, block_all, all_in, no_attack) whose share IS
    the score a do-nothing model achieves.  Classes like proper_subset and
    partial_block are reported but never abort the build.
    """
    if not hist:
        return
    if guard_classes is None:
        guard_classes = CONSTANT_POLICY_CLASSES

    # Filter to guard-eligible classes, find the one with the highest share
    eligible = [(c, n, s) for c, n, s in hist if c in guard_classes]
    if not eligible:
        return

    culprit_cls, culprit_n, culprit_share = max(eligible, key=lambda x: x[2])
    if culprit_share <= max_share:
        return

    total = sum(h[1] for h in hist)
    lines = [
        f"\nFAIL-CLOSED: {label} class '{culprit_cls}' is "
        f"{culprit_share:.1%} ({culprit_n}/{total}) — "
        f"exceeds --max-class-share={max_share:.0%}",
        f"  Guard-eligible classes: {sorted(guard_classes)}",
        "  Full histogram:",
    ]
    for c, nn, s in hist:
        marker = " *" if c in guard_classes else ""
        lines.append(f"    {c}: {nn:>4d} ({s:.1%}){marker}")
    lines.append("  Pass --allow-skewed to build anyway, or use --balance downsample.")
    raise SystemExit("\n".join(lines))


def _downsample(
    records: list[dict],
    attack_hist: list[tuple[str, int, float]],
    block_hist: list[tuple[str, int, float]],
    threshold: float,
) -> list[dict]:
    """Deterministically (seed=7) downsample majority classes to *threshold*.

    Only the block side is typically the problem, but we run the same
    check on both.  Returns a NEW list; original *records* is unchanged.
    """
    import random

    rng = random.Random(7)
    kept: list[dict] = []

    # Process attack and block subsets independently
    for hist, kind_val, meta_class_field in [
        (attack_hist, "combat_attack", "attack_class"),
        (block_hist, "combat_block", "block_class"),
    ]:
        subset = [r for r in records if r.get("kind") == kind_val]
        if not hist or len(subset) == 0:
            kept.extend(subset)
            continue

        total = sum(h[1] for h in hist)
        max_class, max_count, max_share = hist[0]

        if max_share <= threshold:
            kept.extend(subset)
            print(
                f"  [{kind_val}] no class exceeds {threshold:.0%}, keeping all {len(subset)}",
                file=sys.stderr,
            )
            continue

        # Target count for the majority class
        other_total = total - max_count
        target_max = int(other_total * threshold / (1 - threshold))
        # Clamp: never go below the next-largest class or below 1
        next_largest = hist[1][1] if len(hist) > 1 else 0
        target_max = max(target_max, next_largest, 1)

        to_drop = max_count - target_max
        if to_drop <= 0:
            kept.extend(subset)
            continue

        majority_recs = [r for r in subset if r["meta"].get(meta_class_field, "") == max_class]
        non_majority = [r for r in subset if r["meta"].get(meta_class_field, "") != max_class]

        rng.shuffle(majority_recs)
        keep_count = len(majority_recs) - to_drop
        keep_majority = majority_recs[:keep_count]

        print(
            f"  [{kind_val}] downsampled '{max_class}': "
            f"{len(majority_recs)} -> {keep_count} "
            f"(dropped {to_drop})",
            file=sys.stderr,
        )
        kept.extend(non_majority)
        kept.extend(keep_majority)

    return kept


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build combat-gate-shaped records from MageZero decisions JSONL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--in", dest="input", type=Path, required=True, help="Path to decisions JSONL (MageZero schema)"
    )
    parser.add_argument(
        "--out", dest="output", type=Path, required=True, help="Path to write training records JSONL"
    )
    parser.add_argument("--report", action="store_true", help="Print build report to stderr on completion")
    parser.add_argument(
        "--max-class-share",
        type=float,
        default=0.50,
        help="Maximum allowed share of any single class (default: 0.50).  Build aborts if exceeded.",
    )
    parser.add_argument("--allow-skewed", action="store_true", help="Skip the fail-closed class-share guard")
    parser.add_argument(
        "--guard-classes",
        type=str,
        default=None,
        help="Comma-separated class names the guard checks (default: no_block,block_all,all_in,no_attack)",
    )
    parser.add_argument(
        "--balance",
        choices=("downsample",),
        default=None,
        help="Downsample the majority class toward the threshold (deterministic, seed=7)",
    )
    args = parser.parse_args(argv)

    t0 = time.time()

    if not args.input.exists():
        logger.error(f"input not found: {args.input}")
        return 2

    raw = _read_jsonl(args.input)
    read_count = len(raw)
    logger.info(f"read {read_count} decisions from {args.input}")

    records: list[dict] = []
    drops: Counter = Counter()
    skip_counts: Counter = Counter()

    for row in raw:
        kind = row.get("decision_kind", "")
        if kind not in ("attackers", "blockers"):
            skip_counts[kind] += 1
            drops[f"skip_kind_{kind}"] += 1
            continue

        if kind == "attackers":
            rec, reason = build_attack_record(row)
        else:
            rec, reason = build_block_record(row)

        if rec is None:
            drops[reason] += 1
            continue
        records.append(rec)

    if not records:
        logger.error("no usable records — fail closed")
        return 2

    # ── Class-histogram guard -------------------------------------------------

    attack_hist = _class_histogram(
        records,
        "attack_class",
        lambda r: r.get("kind") == "combat_attack",
    )
    block_hist = _class_histogram(
        records,
        "block_class",
        lambda r: r.get("kind") == "combat_block",
    )

    if not args.allow_skewed:
        guard_classes = (
            frozenset(args.guard_classes.split(",")) if args.guard_classes else CONSTANT_POLICY_CLASSES
        )
        _check_class_share(attack_hist, args.max_class_share, "attack", guard_classes=guard_classes)
        _check_class_share(block_hist, args.max_class_share, "block", guard_classes=guard_classes)

    # ── Rebalance -------------------------------------------------------------

    if args.balance == "downsample":
        records = _downsample(records, attack_hist, block_hist, args.max_class_share)
        # Recompute histograms after downsample for accurate report
        attack_hist = _class_histogram(
            records,
            "attack_class",
            lambda r: r.get("kind") == "combat_attack",
        )
        block_hist = _class_histogram(
            records,
            "block_class",
            lambda r: r.get("kind") == "combat_block",
        )

    elapsed = time.time() - t0
    written = _write_jsonl(args.output, records)
    logger.info(f"wrote {written} records -> {args.output}")

    if args.report:
        _print_report(
            records,
            drops,
            skip_counts,
            read_count,
            args.input,
            args.output,
            elapsed,
            attack_hist=attack_hist,
            block_hist=block_hist,
        )

    return 0


def _print_report(
    records: list[dict],
    drops: Counter,
    skip_counts: Counter,
    read_count: int,
    input_path: Path,
    output_path: Path,
    elapsed: float,
    attack_hist: list[tuple[str, int, float]] | None = None,
    block_hist: list[tuple[str, int, float]] | None = None,
) -> None:
    attack_recs = [r for r in records if r.get("kind") == "combat_attack"]
    block_recs = [r for r in records if r.get("kind") == "combat_block"]
    any_mcts = sum(1 for r in records if isinstance(r.get("meta"), dict) and r["meta"].get("mcts_chosen"))

    print(f"\n=== BUILD REPORT ===", file=sys.stderr)
    print(f"  Input:      {input_path} ({read_count} rows)", file=sys.stderr)
    print(f"  Output:     {output_path} ({len(records)} records)", file=sys.stderr)
    print(f"  Elapsed:    {elapsed:.2f}s", file=sys.stderr)
    print(f"  Skipped kinds: {dict(skip_counts)}", file=sys.stderr)
    print(f"  Drops:      {dict(drops)}", file=sys.stderr)
    print(f"  Total drops: {sum(drops.values())}", file=sys.stderr)
    print(f"  Attack recs: {len(attack_recs)}", file=sys.stderr)
    print(f"  Block  recs: {len(block_recs)}", file=sys.stderr)
    print(f"  Records with mcts_chosen: {any_mcts}", file=sys.stderr)

    # Verify no solver lines or mcts leaks in any prompt
    leak_solver = sum(1 for r in records if "Computed optimal" in r.get("user", ""))
    print(f"  Solver line leaks: {leak_solver} (should be 0)", file=sys.stderr)

    if attack_hist:
        print(f"\n  Attack class histogram:", file=sys.stderr)
        for cls, n, share in attack_hist:
            print(f"    {cls}: {n:>4d} ({share:.1%})", file=sys.stderr)

    if block_hist:
        print(f"\n  Block class histogram:", file=sys.stderr)
        for cls, n, share in block_hist:
            print(f"    {cls}: {n:>4d} ({share:.1%})", file=sys.stderr)

    # Show one example of each
    if attack_recs:
        print(f"\n  Sample attack response:", file=sys.stderr)
        sample = attack_recs[0]
        print(f"    id: {sample['id']}", file=sys.stderr)
        print(f"    response: {sample['response']}", file=sys.stderr)
        print(
            f"    meta: {json.dumps({k: sample['meta'][k] for k in ('pool_size', 'chosen_size', 'chosen_names', 'attack_class')})}",
            file=sys.stderr,
        )
    if block_recs:
        print(f"\n  Sample block response:", file=sys.stderr)
        sample = block_recs[0]
        print(f"    id: {sample['id']}", file=sys.stderr)
        print(f"    response: {sample['response']}", file=sys.stderr)
        print(
            f"    meta: {json.dumps({k: sample['meta'][k] for k in ('blocker_pool_size', 'n_attackers', 'n_blocks', 'block_class')})}",
            file=sys.stderr,
        )

    print(f"  {'=' * 40}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
