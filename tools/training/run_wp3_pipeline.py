#!/usr/bin/env python3
"""WP-3 end-to-end pipeline: MageZero logs → production-shaped gemma training records.

Chains the four WP-3 merged modules:

  1. parse_magezero_log  — XMage text logs → decisions JSONL
  2. magezero_filters     — drop_single_option → outcome_filter(won_only) → dedupe →
                            pass_rate_tripwire → split_by_game
  3. build_magezero_bridge — decisions JSONL row → training record via the
                             production gate_play_decisions.build_user_message
  4. Leak scan            — reject corpus containing training-set artifacts

Usage
-----
    python3 tools/training/run_wp3_pipeline.py \
        --log /home/joshu/mz_train_smoke.log \
        --log /home/joshu/mz_logs/*.log

Output
------
    tools/training/data/wp3/decisions.jsonl     — unfiltered decisions (intermediate)
    tools/training/data/wp3/filtered.jsonl      — post-filter decisions (intermediate)
    tools/training/data/wp3/train.jsonl          — training records (split)
    tools/training/data/wp3/val.jsonl            — validation records (split)
    tools/training/data/wp3/test.jsonl           — test records (split)
    tools/training/data/wp3/manifest.json        — corpus manifest (committed)
    tools/training/data/wp3/REPORT.md            — pipeline report (committed)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

# ── Ensure repo src is on sys.path for module imports ──────────────────────
REPO = Path(__file__).resolve().parent.parent.parent
SRC = REPO / "src"
for p in [str(SRC), str(REPO)]:
    if p not in sys.path:
        sys.path.insert(0, p)

# ── WP-3 module imports ───────────────────────────────────────────────────
from tools.training import build_magezero_bridge as BRIDGE
from tools.training import magezero_combat_micro as COMBAT
from tools.training import magezero_filters as FILTERS
from tools.training import oracle_coverage as ORACLE
from tools.training import parse_magezero_log as PARSER

# ---------------------------------------------------------------------------
# Output paths
# ---------------------------------------------------------------------------

DEFAULT_OUTDIR = REPO / "tools" / "training" / "data" / "wp3"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(8192 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "unknown"


# ---------------------------------------------------------------------------
# Stage 1: Parse
# ---------------------------------------------------------------------------


def stage_parse(
    log_paths: list[Path],
    primary_deck: str | None = None,
    decks_dir: str | None = None,
) -> list[dict]:
    """Parse log files, returning combined decisions list.

    ``primary_deck``/``decks_dir`` feed the parser's fail-closed deck-signature
    guardrail (parse_magezero_log.resolve_primary_deck): with neither set,
    behavior on legacy logs (no ``_<Primary>_vs_<Opponent>`` filename) is
    unchanged; deck-named logs are checked against their .dck automatically.
    A DeckSignatureError aborts the pipeline — never builds an unchecked corpus.
    """
    all_decisions: list[dict] = []
    per_log_counts: dict[str, int] = {}

    for lp in log_paths:
        print(f"  Parsing {lp} ...", end=" ", flush=True)
        decisions, sessions = PARSER.parse_log(str(lp), primary_deck=primary_deck, decks_dir=decks_dir)
        all_decisions.extend(decisions)
        per_log_counts[str(lp)] = len(decisions)
        checked = PARSER.LAST_PARSE_STATS.get("deck_signature_checked_rows", 0)
        sig = f", deck-signature OK on {checked} rows" if checked else ""
        print(f"{len(decisions)} decisions, {len(sessions)} sessions{sig}")

    return all_decisions


# ---------------------------------------------------------------------------
# Stage 2: Filter
# ---------------------------------------------------------------------------


def stage_filter(
    rows: list[dict],
    *,
    balance: str | None = None,
    max_pass_frac: float = 0.40,
    seed: int = 7,
) -> tuple[list[dict], dict[str, int]]:
    """Apply filters in order: drop_single_option → outcome_filter → dedupe.

    With ``balance="downsample-pass"``, surplus PRIORITY passes are then dropped
    to meet ``max_pass_frac`` before the tripwire runs. Combat declines are never
    dropped — see magezero_filters.downsample_passes for why.

    Returns (filtered, filter_counts).
    """
    counts: dict[str, int] = {}

    rows, n = FILTERS.drop_single_option(rows)
    counts["drop_single_option"] = n
    print(f"  drop_single_option: {n} dropped ({len(rows)} kept)")

    rows, n = FILTERS.outcome_filter(rows, "won_only")
    counts["outcome_filter"] = n
    print(f"  outcome_filter (won_only): {n} dropped ({len(rows)} kept)")

    rows, n = FILTERS.dedupe(rows)
    counts["dedupe"] = n
    print(f"  dedupe: {n} dropped ({len(rows)} kept)")

    # Pass-rate visibility per decision_kind, before any balancing, so the raw
    # corpus shape is on the record. The literal-"Pass" gate misses binary
    # CHOOSE_USE declines ("false"), which are also do-nothing decisions.
    for kind, st in FILTERS.pass_rate_report(rows).items():
        print(
            f"    pass rate {kind:<12s} rows={int(st['rows']):6d}  "
            f"literal={st['literal_frac'] * 100:5.1f}%  pass-like={st['pass_like_frac'] * 100:5.1f}%"
        )

    if balance == "downsample-pass":
        rows, bstats = FILTERS.downsample_passes(rows, max_frac=max_pass_frac, seed=seed)
        counts["downsample_pass"] = bstats["dropped"]
        print(
            f"  downsample_pass: {bstats['dropped']} dropped ({len(rows)} kept; "
            f"{bstats['eligible']} priority passes eligible)"
        )
        if bstats.get("shortfall"):
            print(
                f"  ** {bstats['shortfall']} more drops needed than eligible priority "
                f"passes; the tripwire will reject this corpus **"
            )

    return rows, counts


# ---------------------------------------------------------------------------
# Stage 3: Tripwire
# ---------------------------------------------------------------------------


def stage_tripwire(rows: list[dict], max_frac: float = 0.40) -> float:
    """Check pass rate. Returns the pass fraction (exits on violation)."""
    total = len(rows)
    if total == 0:
        return 0.0
    n_pass = sum(1 for r in rows if r.get("chosen") == "Pass")
    frac = n_pass / total
    print(f"  Pass rate: {n_pass}/{total} = {frac:.4f} ({frac * 100:.1f}%)")
    FILTERS.pass_rate_tripwire(rows, max_frac)
    return frac


# ---------------------------------------------------------------------------
# Stage 4: Count attackers/blockers
# ---------------------------------------------------------------------------


def count_attackers_blockers(splits: dict[str, list[dict]]) -> dict[str, int]:
    """Count how many rows per split are attackers/blockers."""
    counts: dict[str, Counter] = {}
    for split_name, rows in splits.items():
        c: Counter = Counter()
        for r in rows:
            kind = r.get("decision_kind", "priority")
            if kind in ("attackers", "blockers"):
                c[kind] += 1
        counts[split_name] = c
    return counts


# ---------------------------------------------------------------------------
# Stage 5: Render (via build_magezero_bridge.build_record)
# ---------------------------------------------------------------------------


def stage_render(
    splits: dict[str, list[dict]],
    split_filter_counts: dict[str, Counter],
    attackers_blockers_counts: dict[str, int],
    include_combat: bool = False,
) -> dict[str, list[dict]]:
    """Render rows in each split via the kind-appropriate builder.

    * ``priority`` rows -> build_magezero_bridge.build_record (unchanged).
    * ``attack_commit`` / ``block_assign`` rows -> magezero_combat_micro.
      build_combat_record, only when ``include_combat`` (they cannot appear
      otherwise; if one does with the flag off, it is counted, not rendered).
    * legacy ``attackers``/``blockers`` rows are skipped and counted per
      split, exactly as before (#453: those labels were fabricated upstream).

    Returns {split_name: [training_records]}.
    """
    result: dict[str, list[dict]] = {}
    for split_name, rows in splits.items():
        records: list[dict] = []
        drops: Counter = Counter()
        for row in rows:
            # Pre-count attackers/blockers that bypass drop_single_option
            kind = row.get("decision_kind", "priority")
            if kind in ("attackers", "blockers"):
                drops[f"dk_{kind}"] += 1
                continue
            if kind in ("attack_commit", "block_assign"):
                if not include_combat:
                    drops[f"dk_{kind}_flag_off"] += 1
                    continue
                record, reason = COMBAT.build_combat_record(row)
            else:
                record, reason = BRIDGE.build_record(row)
            if record is None:
                drops[reason] += 1
                continue
            records.append(record)
        result[split_name] = records
        split_filter_counts[split_name] = drops
        n_ab = sum(drops.get(f"dk_{k}", 0) for k in ("attackers", "blockers"))
        print(
            f"  split '{split_name}': {len(rows)} input rows → "
            f"{len(records)} records, "
            f"{drops.total()} drops "
            f"({n_ab} attackers/blockers, "
            f"{drops.total() - n_ab} other)"
        )
    return result


# ---------------------------------------------------------------------------
# Stage 5b: Oracle-text coverage (fail-closed metric)
# ---------------------------------------------------------------------------


def stage_oracle_coverage(
    rendered: dict[str, list[dict]],
    min_coverage: float | None,
) -> dict[str, Any]:
    """Measure oracle-text coverage over every rendered prompt.

    The transfer medium for card generalization is TEXT (a bare card name
    teaches name->action; oracle text teaches role->action), so the fraction
    of card-name mentions carrying oracle text is a first-class corpus
    metric. ``unresolved`` names (no LOCAL oracle source) count AGAINST
    coverage — a card we cannot ground is a gap, not an excuse.

    With ``min_coverage`` set, a corpus below the floor aborts the pipeline
    (exit 43). Default is off so existing invocations are unchanged.
    """
    lookup = ORACLE.build_lookup()
    stats = ORACLE.new_stats()
    for records in rendered.values():
        ORACLE.audit_records(records, lookup, stats)
    summary = ORACLE.summarize(stats)
    summary["mention_accounting"] = {
        k: BRIDGE.ORACLE_STATS.get(k, 0)
        for k in ("oracle_attached", "oracle_no_text", "oracle_unresolved", "no_targets_stripped")
    }

    print(f"  records audited: {summary['records_audited']}")
    for ctx, row in summary["contexts"].items():
        cov = row["coverage"]
        cov_s = f"{cov * 100:5.1f}%" if cov is not None else "  n/a"
        print(
            f"  {ctx:<11s} coverage={cov_s}  covered={row['covered']} "
            f"uncovered={row['uncovered']} unresolved={row['unresolved']} "
            f"keyword_only={row['keyword_only']} no_text={row['no_text']}"
        )
    overall = summary["overall_coverage"]
    print(f"  overall oracle_text_coverage: "
          f"{overall if overall is not None else 'n/a (no mentions)'}")
    if summary["unparsed_lines"]:
        print(f"  unparsed lines (counted, fail-closed): {summary['unparsed_lines']}")

    if min_coverage is not None:
        if overall is None:
            print(
                f"\nORACLE COVERAGE FLOOR FAILED: no card mentions found to measure "
                f"(floor {min_coverage}). Corpus REJECTED.",
                file=sys.stderr,
            )
            raise SystemExit(43)
        if overall < min_coverage:
            print(
                f"\nORACLE COVERAGE FLOOR FAILED: {overall:.4f} < {min_coverage} "
                f"(--min-oracle-coverage). Corpus REJECTED.",
                file=sys.stderr,
            )
            raise SystemExit(43)
        print(f"  floor check: {overall:.4f} >= {min_coverage} PASS")
    return summary


# ---------------------------------------------------------------------------
# Stage 6: Leak scan
# ---------------------------------------------------------------------------


def stage_leak_scan(
    rendered: dict[str, list[dict]],
    raw_decisions: list[dict],
) -> None:
    """Scan every rendered prompt for training-set artifacts.

    Aborts (SystemExit) on ANY hit — the corpus is not safe for training.

    Checks:
      1. No "score:" substring in any prompt text — this is a definitive
         marker of MCTS raw data leaking into the prompt.
      2. No "count:" substring in any prompt text — same.
      3. No integer that appears in the MCTS counts (≥10) appears as a
         bare integer token in the rendered prompt, after excluding
         values that also appear as life totals (10-20 are common in
         "Life: You=N Opp=N").

    Why not check every possible MCTS integer against every action name?
    The production prompt builder (gate_play_decisions.build_user_message)
    receives only game_state and menu — it has no access to mcts_counts.
    MCTS data can only leak through a bug in the formatter, and such a
    bug would surface via "score:" / "count:" markers. The integer-check
    here is a secondary defense against subtle leaks.
    """
    # Collect all distinct MCTS integer values ≥ 10
    mcts_values: set[int] = set()
    for d in raw_decisions:
        mcts = d.get("mcts_counts", {})
        for count in mcts.values():
            if isinstance(count, (int, float)) and count >= 10:
                mcts_values.add(int(count))

    # Values 10-20 are common in life totals ("Life: You=13 Opp=20") —
    # only flag values > 30 that are unlikely to be legitimate game state
    mcts_values_high = {v for v in mcts_values if v > 30}

    errors: list[str] = []

    re_bare_int = re.compile(r"\b(\d{2,})\b")
    # The rendered menu is NUMBERED ("  31. Pass") and that numbering is the
    # answer mechanism, not a leak — the model must see it to emit {"pick": N}.
    # Scanning it for bare integers made the scan reject the whole corpus the
    # moment a menu grew past the smallest MCTS count (reproduced: 31 options
    # with an MCTS count of 31). Strip menu lines before the integer scan; the
    # word-level checks above still cover the whole prompt.
    re_menu_line = re.compile(r"^\s*\d+\.\s", re.MULTILINE)

    def _strip_menu_lines(text: str) -> str:
        return "\n".join("" if re_menu_line.match(line) else line for line in text.splitlines())

    for split_name, records in rendered.items():
        for i, rec in enumerate(records):
            user = rec.get("user", "")
            system = rec.get("system", "")

            # 1. "score:" leak
            if "score:" in user or "score:" in system:
                errors.append(f"[{split_name} record {i}] 'score:' found in prompt")

            # 2. "count:" leak
            if "count:" in user or "count:" in system:
                errors.append(f"[{split_name} record {i}] 'count:' found in prompt")

            # 3. MCTS integer ≥30 leak: find all bare integers in the prompt
            # and check if any are in our high MCTS set. Menu numbering is
            # excluded (see _strip_menu_lines) because it is the input
            # mechanism, not model-invisible search data.
            combined = _strip_menu_lines(user) + " " + _strip_menu_lines(system)
            for match in re_bare_int.finditer(combined):
                val = int(match.group(1))
                if val in mcts_values_high:
                    # Check context: is this a life total (preceded by =)?
                    pos = match.start()
                    prev_char = combined[pos - 1] if pos > 0 else " "
                    # Menu numbers like "1. " start at position 0 or after \n
                    # Life totals: "You=NN" or "Opp=NN"
                    # Turn: "TNN"
                    # We flag if not preceded by =, :, >, <, or T
                    if prev_char not in ("=", ":", ">", "<"):
                        errors.append(
                            f"[{split_name} record {i}] MCTS count {val} "
                            f"found as bare integer in prompt (preceded by "
                            f"'{prev_char}')"
                        )

    if errors:
        for e in errors:
            print(f"  LEAK: {e}", file=sys.stderr)
        print(
            f"\nLEAK SCAN FAILED: {len(errors)} violation(s) found. Corpus REJECTED.\n",
            file=sys.stderr,
        )
        raise SystemExit(42)
    else:
        print("  Leak scan passed: 0 violations.")


# ---------------------------------------------------------------------------
# Stage 7: Write output
# ---------------------------------------------------------------------------


def stage_write(
    rendered: dict[str, list[dict]],
    outdir: Path,
    filter_counts: dict[str, int],
    splits: dict[str, list[dict]],
    raw_count: int,
    pass_rate: float,
    attackers_blockers_total: int,
    elapsed: float,
    combat_info: dict[str, Any] | None = None,
    oracle_summary: dict[str, Any] | None = None,
) -> dict:
    """Write per-split JSONL files and manifest. Returns manifest dict."""
    outdir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "version": 2,
        "pipeline": "run_wp3_pipeline.py",
        "git_sha": _git_sha(),
        "built_ts": time.time(),
        "elapsed_seconds": round(elapsed, 2),
        "input_raw_decisions": raw_count,
        "pass_rate": round(pass_rate, 6),
        "attackers_blockers_excluded_total": attackers_blockers_total,
        "filter_counts": dict(filter_counts),
        "splits": {},
    }

    if oracle_summary is not None:
        manifest["oracle_text_coverage"] = oracle_summary

    if combat_info is not None:
        hist = combat_info.get("histograms_filtered", {})
        manifest["combat"] = {
            "included": True,
            "accounting": combat_info.get("accounting", {}),
            "filter_counts": combat_info.get("filter_counts", {}),
            "histograms_raw": combat_info.get("histograms_raw", {}),
            "histograms_filtered": hist,
            "floor_breach": bool(hist.get("floor_breach", False)),
        }
        # Rendered combat record counts per split (post-render truth).
        by_split: dict[str, dict[str, int]] = {}
        for split_name, records in rendered.items():
            c: Counter = Counter(
                rec.get("kind", "?")
                for rec in records
                if rec.get("kind") in ("attack_commit", "block_assign")
            )
            by_split[split_name] = dict(c)
        manifest["combat"]["rendered_by_split"] = by_split
        manifest["combat"]["rendered_attack"] = sum(
            v.get("attack_commit", 0) for v in by_split.values()
        )
        manifest["combat"]["rendered_block"] = sum(
            v.get("block_assign", 0) for v in by_split.values()
        )

    for split_name in sorted(rendered.keys()):
        records = rendered[split_name]
        split_path = outdir / f"{split_name}.jsonl"
        with open(split_path, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        sha = _sha256_file(split_path)

        # Count outcome, decision_kind, sessions
        by_outcome: Counter = Counter()
        by_kind: Counter = Counter()
        by_session: Counter = Counter()
        menu_sizes: list[int] = []
        for rec in records:
            meta = rec.get("meta", {})
            by_outcome[meta.get("outcome", "?")] += 1
            by_kind[meta.get("decision_kind", "?")] += 1
            by_session[meta.get("session", "?")] += 1
            menu_sizes.append(meta.get("menu_size", 0))

        manifest["splits"][split_name] = {
            "records": len(records),
            "file": split_path.name,
            "sha256": sha,
            "by_outcome": dict(by_outcome),
            "by_decision_kind": dict(by_kind),
            "by_session": dict(by_session),
            "menu_size": {
                "min": min(menu_sizes) if menu_sizes else 0,
                "median": sorted(menu_sizes)[len(menu_sizes) // 2] if menu_sizes else 0,
                "max": max(menu_sizes) if menu_sizes else 0,
                "mean": round(sum(menu_sizes) / len(menu_sizes), 2) if menu_sizes else 0,
            },
        }

    manifest_path = outdir / "manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")

    print(f"  Manifest written to {manifest_path}")
    return manifest


# ---------------------------------------------------------------------------
# Stage 8: REPORT.md
# ---------------------------------------------------------------------------


def stage_report(
    manifest: dict,
    filter_counts: dict[str, int],
    rendered: dict[str, list[dict]],
    raw_decisions: list[dict],
    filtered_decisions: list[dict],
    pass_rate: float,
    attackers_blockers_total: int,
    outdir: Path,
    log_paths: list[Path],
) -> str:
    """Write REPORT.md to the output directory. Returns the path."""
    lines: list[str] = []
    lines.append("# WP-3 Pipeline Report — wp3-integration")
    lines.append("")
    lines.append(f"Built: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Git SHA: {manifest.get('git_sha', 'unknown')}")
    lines.append(f"Elapsed: {manifest.get('elapsed_seconds', 0)}s")
    lines.append("")
    lines.append("## Input")
    lines.append("")
    for lp in log_paths:
        lines.append(f"- `{lp}`")
    lines.append(f"- Raw decisions parsed: {len(raw_decisions)}")
    lines.append("")
    lines.append("## Per-Stage Drop Counts")
    lines.append("")
    lines.append("| Stage | In | Out | Drop |")
    lines.append("|-------|----|-----|------|")
    total_in = len(raw_decisions)
    n1 = filter_counts.get("drop_single_option", 0)
    after_1 = total_in - n1
    lines.append(f"| parse → drop_single_option | {total_in} | {after_1} | {n1} |")
    n2 = filter_counts.get("outcome_filter", 0)
    after_2 = after_1 - n2
    lines.append(f"| drop_single_option → outcome_filter | {after_1} | {after_2} | {n2} |")
    n3 = filter_counts.get("dedupe", 0)
    after_3 = after_2 - n3
    lines.append(f"| outcome_filter → dedupe | {after_2} | {after_3} | {n3} |")
    lines.append("")
    lines.append("## Pass Rate")
    lines.append("")
    lines.append(f"Pass fraction (post-filter): {pass_rate:.4f} ({pass_rate * 100:.1f}%)")
    lines.append(f"Pass rate tripwire: {pass_rate <= 0.40} ({'PASS' if pass_rate <= 0.40 else 'FAIL'})")
    lines.append("")
    lines.append("## Attackers/Blockers Excluded")
    lines.append("")
    lines.append(f"Total attackers/blockers rows: {attackers_blockers_total}")
    for split_name, records in rendered.items():
        ab_in_split = sum(
            1 for rec in records if rec.get("meta", {}).get("decision_kind") in ("attackers", "blockers")
        )
        # Actually attackers/blockers were excluded before rendering, so this
        # should be 0 for all splits.
        pass
    lines.append("(Counted and excluded before split rendering — no attackers/blockers appear in any split.)")
    lines.append("")
    combat = manifest.get("combat")
    if combat:
        lines.append("## Combat Micro-Decisions (--include-combat)")
        lines.append("")
        lines.append(
            f"Rendered: {combat.get('rendered_attack', 0)} attack_commit + "
            f"{combat.get('rendered_block', 0)} block_assign"
        )
        hist = combat.get("histograms_filtered", {})
        lines.append("")
        lines.append("Post-filter class histograms (pre-registered floors:")
        lines.append(
            f"no_block {COMBAT.NO_BLOCK_FLOOR:.0%}, all-in {COMBAT.ALL_IN_FLOOR:.1%}):"
        )
        lines.append("")
        lines.append(f"- attack records: {hist.get('attack_records', 0)} — {hist.get('attack_hist', {})}")
        lines.append(
            f"- attack chains: {hist.get('attack_chains', 0)} — {hist.get('attack_chain_hist', {})}"
            f" (all-in share {hist.get('all_in_share', 0.0):.1%})"
        )
        lines.append(
            f"- block records: {hist.get('block_records', 0)} — {hist.get('block_hist', {})}"
            f" (no-block share {hist.get('no_block_share', 0.0):.1%})"
        )
        if combat.get("floor_breach"):
            lines.append("")
            lines.append("**TRIVIAL-POLICY FLOOR BREACHED — not rebalanced; gate decision is the owner's.**")
        lines.append("")
        lines.append("Drop/accounting ledger (scanner):")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(combat.get("accounting", {}), indent=2, sort_keys=True))
        lines.append("```")
        lines.append("")
    oracle = manifest.get("oracle_text_coverage")
    if oracle:
        lines.append("## Oracle-Text Coverage")
        lines.append("")
        lines.append(
            "Fraction of card-name mentions whose oracle text appears in the same "
            "prompt (denominator excludes cards with keyword-only or no oracle "
            "text; `unresolved` = no LOCAL oracle source, counted AGAINST coverage)."
        )
        lines.append("")
        lines.append("| Context | Coverage | Covered | Uncovered | Unresolved | Keyword-only | No-text |")
        lines.append("|---------|----------|---------|-----------|------------|--------------|---------|")
        for ctx, row in oracle.get("contexts", {}).items():
            cov = row.get("coverage")
            cov_s = f"{cov * 100:.1f}%" if cov is not None else "n/a"
            lines.append(
                f"| {ctx} | {cov_s} | {row['covered']} | {row['uncovered']} | "
                f"{row['unresolved']} | {row['keyword_only']} | {row['no_text']} |"
            )
        oc = oracle.get("overall_coverage")
        lines.append("")
        lines.append(f"**Overall: {oc * 100:.1f}%**" if oc is not None else "**Overall: n/a**")
        if oracle.get("unparsed_lines"):
            lines.append("")
            lines.append(f"Unparsed lines (counted, fail-closed): `{oracle['unparsed_lines']}`")
        if oracle.get("examples"):
            lines.append("")
            lines.append("Example gaps per context:")
            for ctx, ex in oracle["examples"].items():
                for cls, names in ex.items():
                    lines.append(f"- {ctx}/{cls}: {', '.join(names)}")
        lines.append("")
    lines.append("## Split Sizes")
    lines.append("")
    lines.append("| Split | Records |")
    lines.append("|-------|---------|")
    for split_name in sorted(rendered.keys()):
        lines.append(f"| {split_name} | {len(rendered[split_name])} |")
    total = sum(len(v) for v in rendered.values())
    lines.append(f"| **Total** | **{total}** |")
    lines.append("")
    lines.append("## Sample Records (first 2 per split)")
    lines.append("")
    for split_name in sorted(rendered.keys()):
        records = rendered[split_name]
        lines.append(f"### Split: {split_name} ({len(records)} records)")
        lines.append("")
        for idx, rec in enumerate(records[:2]):
            lines.append(f"**Record {idx + 1}**")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(rec, indent=2, ensure_ascii=False))
            lines.append("```")
            lines.append("")
    lines.append("## Manifest")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False))
    lines.append("```")
    lines.append("")

    report = "\n".join(lines)
    report_path = outdir / "REPORT.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"  Report written to {report_path}")
    return str(report_path)


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------


def stage_combat(
    log_paths: list[Path],
    raw_decisions: list[dict],
    outdir: Path,
) -> tuple[list[dict], dict[str, Any]]:
    """Scan the same logs for combat micro-decisions (--include-combat only).

    Outcomes and sessions are JOINED from the priority parse by game_id —
    game numbering is identical by construction (same die-roll boundaries,
    same per-thread counters), so a combat row inherits the calibrated
    outcome of its game. A game with no priority row keeps the scanner's
    life-inferred outcome (usually "unknown", which the outcome filter then
    drops and counts).
    """
    combat_rows: list[dict] = []
    total_acct: Counter = Counter()
    for lp in log_paths:
        print(f"  Combat-scanning {lp} ...", end=" ", flush=True)
        rows, acct = COMBAT.parse_combat_log(str(lp))
        COMBAT.verify_accounting(acct)
        combat_rows.extend(rows)
        total_acct.update(acct)
        print(f"{len(rows)} combat rows")

    outcome_map: dict[str, str] = {}
    session_map: dict[str, str] = {}
    for d in raw_decisions:
        gid = d.get("game_id", "")
        if gid:
            outcome_map[gid] = d.get("outcome", "unknown")
            session_map[gid] = d.get("session", "")
    joined = 0
    for r in combat_rows:
        gid = r.get("game_id", "")
        if gid in outcome_map:
            r["outcome"] = outcome_map[gid]
            r["session"] = session_map[gid]
            joined += 1
        else:
            total_acct["note_outcome_join_miss"] += 1
    print(
        f"  outcome join: {joined}/{len(combat_rows)} rows joined to the "
        f"priority parse ({total_acct.get('note_outcome_join_miss', 0)} misses "
        f"keep the scanner's life-inferred outcome)"
    )

    combat_path = outdir / "combat_decisions.jsonl"
    with open(combat_path, "w", encoding="utf-8") as f:
        for r in combat_rows:
            f.write(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n")
    print(f"  Wrote {len(combat_rows)} combat decisions -> {combat_path}")

    info: dict[str, Any] = {"accounting": dict(total_acct)}
    return combat_rows, info


def run_pipeline(
    log_paths: list[Path],
    outdir: Path,
    max_pass_frac: float = 0.40,
    seed: int = 7,
    balance: str | None = None,
    include_combat: bool = False,
    primary_deck: str | None = None,
    decks_dir: str | None = None,
    min_oracle_coverage: float | None = None,
) -> dict[str, Any]:
    """Run the full WP-3 pipeline.

    Returns the manifest dict.
    """
    t0 = time.time()
    outdir.mkdir(parents=True, exist_ok=True)
    BRIDGE.ORACLE_STATS.clear()  # per-run mention accounting

    # ── Stage 1: Parse ────────────────────────────────────────────────────
    print("Stage 1/5 — Parse logs")
    raw_decisions = stage_parse(log_paths, primary_deck=primary_deck, decks_dir=decks_dir)
    raw_count = len(raw_decisions)

    # Save intermediate: decisions
    decisions_path = outdir / "decisions.jsonl"
    with open(decisions_path, "w", encoding="utf-8") as f:
        for d in raw_decisions:
            f.write(json.dumps(d, sort_keys=True, ensure_ascii=False) + "\n")
    print(f"  Wrote {raw_count} decisions -> {decisions_path}")

    # ── Stage 1b: Combat micro-decision scan (--include-combat only) ──────
    combat_rows: list[dict] = []
    combat_info: dict[str, Any] | None = None
    if include_combat:
        print("Stage 1b — Combat micro-decision scan (magezero_combat_micro)")
        combat_rows, combat_info = stage_combat(log_paths, raw_decisions, outdir)

    # ── Stage 2: Filter ────────────────────────────────────────────────────
    print("Stage 2/5 — Filter decisions")
    filtered, filter_counts = stage_filter(
        raw_decisions, balance=balance, max_pass_frac=max_pass_frac, seed=seed
    )
    filtered_count = len(filtered)

    # Save intermediate: filtered
    filtered_path = outdir / "filtered.jsonl"
    with open(filtered_path, "w", encoding="utf-8") as f:
        for d in filtered:
            f.write(json.dumps(d, sort_keys=True, ensure_ascii=False) + "\n")
    print(f"  Wrote {filtered_count} filtered -> {filtered_path}")

    # ── Stage 3: Tripwire ──────────────────────────────────────────────────
    # The pass-rate tripwire is computed on the PRIORITY corpus only, exactly
    # as pre-registered. Combat rows are filtered separately below and never
    # dilute the pass fraction — a 56%-pass priority corpus must still trip
    # even when thousands of combat rows are added.
    print("Stage 3/5 — Pass rate tripwire")
    pass_rate = stage_tripwire(filtered, max_pass_frac)

    # ── Stage 2b: Filter combat rows (separately, counted separately) ─────
    combat_filtered: list[dict] = []
    if include_combat:
        print("Stage 2b — Filter combat rows")
        combat_filtered, cn1 = FILTERS.drop_single_option(combat_rows)
        print(f"  drop_single_option: {cn1} dropped ({len(combat_filtered)} kept)")
        combat_filtered, cn2 = FILTERS.outcome_filter(combat_filtered, "won_only")
        print(f"  outcome_filter (won_only): {cn2} dropped ({len(combat_filtered)} kept)")
        combat_filtered, cn3 = FILTERS.dedupe(combat_filtered)
        print(f"  dedupe: {cn3} dropped ({len(combat_filtered)} kept)")
        assert combat_info is not None
        combat_info["filter_counts"] = {
            "drop_single_option": cn1,
            "outcome_filter": cn2,
            "dedupe": cn3,
        }
        # Class histograms + pre-registered trivial-policy floors, on both the
        # raw scan and the post-filter corpus (the one that trains).
        combat_info["histograms_raw"] = COMBAT.combat_histograms(combat_rows)
        combat_info["histograms_filtered"] = COMBAT.combat_histograms(combat_filtered)
        print("  Combat histograms (raw scan):")
        COMBAT.print_floor_check(combat_info["histograms_raw"], out=sys.stdout)
        print("  Combat histograms (post-filter):")
        COMBAT.print_floor_check(combat_info["histograms_filtered"], out=sys.stdout)

    # ── Stage 4: Split ─────────────────────────────────────────────────────
    # Split on the UNION so that all rows of a game — priority and combat —
    # land in the same split (game-level leak safety across kinds).
    print("Stage 4/5 — Split by game")
    split_input = filtered + combat_filtered if include_combat else filtered
    splits = FILTERS.split_by_game(split_input, seed=seed)
    for name, group in splits.items():
        unique_games = len(set(FILTERS._game_id(r) for r in group))
        print(f"  split '{name}': {len(group)} rows ({unique_games} games)")

    # Count attackers/blockers before rendering
    attackers_blockers_total = sum(1 for d in filtered if d.get("decision_kind") in ("attackers", "blockers"))
    print(f"  Attackers/blockers rows (pre-render): {attackers_blockers_total}")

    # ── Stage 5: Render ────────────────────────────────────────────────────
    print("Stage 5/5 — Render training records via build_magezero_bridge")
    split_drops: dict[str, Counter] = {}
    rendered = stage_render(splits, split_drops, {}, include_combat=include_combat)

    # Surface the per-reason drop tally. It was computed into split_drops and
    # then never read, so the PR body's claim about WHY rows were dropped could
    # not have come from this code. An unexplained drop count is how a silently
    # broken adapter looks exactly like a working one.
    render_drops: dict[str, dict[str, int]] = {
        split: dict(counter) for split, counter in split_drops.items() if counter
    }
    total_dropped = sum(sum(c.values()) for c in split_drops.values())
    if total_dropped:
        print(f"  render drops: {total_dropped} row(s) not rendered, by reason:")
        agg: Counter = Counter()
        for counter in split_drops.values():
            agg.update(counter)
        for reason, n in agg.most_common():
            print(f"    {reason}: {n}")

    # ── Leak scan ──────────────────────────────────────────────────────────
    # Combat rows' MCTS counts join the scan set so a combat count leaking
    # into ANY prompt (priority or combat) is caught.
    print("Leak scan — checking rendered prompts for training-set artifacts...")
    stage_leak_scan(rendered, raw_decisions + combat_rows)

    # ── Oracle-text coverage ───────────────────────────────────────────────
    print("Oracle-text coverage — measuring card-mention text coverage...")
    oracle_summary = stage_oracle_coverage(rendered, min_oracle_coverage)

    # ── Write output ────────────────────────────────────────────────────────
    elapsed = time.time() - t0
    manifest = stage_write(
        rendered,
        outdir,
        filter_counts,
        splits,
        raw_count,
        pass_rate,
        attackers_blockers_total,
        elapsed,
        combat_info=combat_info,
        oracle_summary=oracle_summary,
    )

    # ── Report ──────────────────────────────────────────────────────────────
    stage_report(
        manifest,
        filter_counts,
        rendered,
        raw_decisions,
        filtered,
        pass_rate,
        attackers_blockers_total,
        outdir,
        log_paths,
    )

    # Summary
    total_records = sum(len(v) for v in rendered.values())
    print("\n=== PIPELINE COMPLETE ===")
    print(f"  Input decisions:   {raw_count}")
    print(f"  Post-filter:       {filtered_count}")
    print(f"  Training records:  {total_records}")
    print(f"  Attackers/blockers excluded: {attackers_blockers_total}")
    print(f"  Pass rate:         {pass_rate:.4f} ({pass_rate * 100:.1f}%)")
    _oc = oracle_summary.get("overall_coverage")
    print(f"  Oracle coverage:   {_oc:.4f}" if _oc is not None else "  Oracle coverage:   n/a")
    if include_combat and combat_info is not None:
        print(
            f"  Combat rows:       {len(combat_rows)} scanned, "
            f"{len(combat_filtered)} post-filter, "
            f"{manifest.get('combat', {}).get('rendered_attack', 0)} attack + "
            f"{manifest.get('combat', {}).get('rendered_block', 0)} block rendered"
        )
        if manifest.get("combat", {}).get("floor_breach"):
            print("  ** COMBAT TRIVIAL-POLICY FLOOR BREACHED — see manifest **")
    print(f"  Elapsed:           {elapsed:.1f}s")
    print(f"  Output:            {outdir}")

    return manifest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="WP-3 end-to-end pipeline: MageZero logs → training records",
    )
    parser.add_argument(
        "--log",
        dest="log_paths",
        type=str,
        action="append",
        required=True,
        help="Path to MageZero XMage log file (can be specified multiple times or as glob)",
    )
    parser.add_argument(
        "--outdir",
        type=str,
        default=str(DEFAULT_OUTDIR),
        help="Output directory (default: tools/training/data/wp3/)",
    )
    parser.add_argument(
        "--max-pass-frac",
        type=float,
        default=0.40,
        help="Maximum allowed Pass fraction (default: 0.40). Trips pipeline if exceeded.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=7,
        help="PRNG seed for deterministic splitting (default: 7).",
    )
    parser.add_argument(
        "--balance",
        choices=("downsample-pass",),
        default=None,
        help="Drop surplus PRIORITY passes to meet --max-pass-frac before the "
        "tripwire (see magezero_filters.downsample_passes). Combat declines are "
        "never dropped. Off by default so the raw corpus shape is not altered.",
    )
    parser.add_argument(
        "--include-combat",
        action="store_true",
        default=False,
        help="Also extract combat micro-decisions (attack_commit/block_assign) "
        "via magezero_combat_micro and render them into the same splits. "
        "OFF by default: without this flag the pipeline output is unchanged.",
    )
    parser.add_argument(
        "--primary-deck",
        default=None,
        help="Primary (MCTS) deck name for the parser's fail-closed hand "
        "deck-signature guardrail (e.g. HighNoonControl). Default: inferred "
        "from a _<Primary>_vs_<Opponent>.log filename; legacy logs run "
        "unchecked exactly as before.",
    )
    parser.add_argument(
        "--decks-dir",
        default=None,
        help="XMage deck directory holding the .dck lists "
        "(default: parse_magezero_log.DEFAULT_DECKS_DIR)",
    )
    parser.add_argument(
        "--min-oracle-coverage",
        type=float,
        default=None,
        metavar="FRAC",
        help="Fail-closed floor on overall oracle-text coverage (0..1). When "
        "set, a corpus whose fraction of card mentions with attached oracle "
        "text falls below FRAC aborts the pipeline (exit 43). Coverage is "
        "always measured and reported; the floor is OFF by default.",
    )
    return parser


def expand_globs(paths: list[str]) -> tuple[list[Path], list[str]]:
    """Expand globs and deduplicate, preserving order.

    Returns ``(resolved_paths, unmatched_patterns)``. Unmatched patterns are
    RETURNED rather than swallowed: a typo'd --log used to be discarded here, and
    the pipeline then printed "=== PIPELINE COMPLETE ===" with 0 decisions and
    wrote empty split files. A run that silently produces an empty corpus is
    indistinguishable from a run that succeeded, which is the worst possible
    failure for a training pipeline.
    """
    import glob as _glob

    seen: set[str] = set()
    result: list[Path] = []
    unmatched: list[str] = []
    for p in paths:
        expanded = os.path.expanduser(p)
        matches = sorted(_glob.glob(expanded, recursive=False))
        if matches:
            for match in matches:
                resolved = os.path.realpath(match)
                if resolved not in seen:
                    seen.add(resolved)
                    result.append(Path(resolved))
            continue
        resolved = os.path.realpath(expanded)
        if os.path.exists(resolved):
            if resolved not in seen:
                seen.add(resolved)
                result.append(Path(resolved))
        else:
            unmatched.append(p)
    return result, unmatched


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    # Expand globs in log paths
    log_paths, unmatched = expand_globs(args.log_paths)

    if unmatched:
        for pat in unmatched:
            print(f"ERROR: --log matched nothing: {pat}", file=sys.stderr)
        print(
            "\nRefusing to run: every --log must resolve to at least one file. "
            "Silently skipping a typo'd path yields an empty corpus that looks "
            "like a successful run.",
            file=sys.stderr,
        )
        return 2
    if not log_paths:
        print("ERROR: no input logs resolved.", file=sys.stderr)
        return 2

    print(f"WP-3 Pipeline — {len(log_paths)} log file(s)")
    for lp in log_paths:
        size_mb = os.path.getsize(lp) / (1024 * 1024)
        print(f"  {lp} ({size_mb:.0f} MB)")
    print()

    outdir = Path(args.outdir).resolve()

    try:
        run_pipeline(
            log_paths=log_paths,
            outdir=outdir,
            max_pass_frac=args.max_pass_frac,
            seed=args.seed,
            balance=args.balance,
            include_combat=args.include_combat,
            primary_deck=args.primary_deck,
            decks_dir=args.decks_dir,
            min_oracle_coverage=args.min_oracle_coverage,
        )
    except PARSER.DeckSignatureError as e:
        print(f"\nPipeline aborted (fail closed): {e}", file=sys.stderr)
        return 43
    except SystemExit as e:
        if e.code == 42:
            print("\nPipeline aborted: pass rate tripwire or leak scan failed.", file=sys.stderr)
            return 42
        if e.code == 43:
            print("\nPipeline aborted: oracle-text coverage below --min-oracle-coverage.", file=sys.stderr)
            return 43
        raise

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
