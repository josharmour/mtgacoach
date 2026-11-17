"""Aggregate-only offline model comparison for chunk H3 (B2/B3).

Splits B3 labelled cases into code-settled vs model-required cohorts,
compares GLM baseline, GLM variants (e.g. thinking=low), and smaller
candidates on identical corpus/labels without emitting case IDs or prompts.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import statistics
from pathlib import Path


def _report_module():
    try:
        from tools import benchmark_report

        return benchmark_report
    except ImportError:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "benchmark_report", Path(__file__).resolve().parent / "benchmark_report.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


def _parse_named_report(spec: str) -> tuple[str, Path]:
    if "=" not in spec:
        raise ValueError(f"expected NAME=PATH, got {spec!r}")
    name, raw_path = spec.split("=", 1)
    name = name.strip()
    if not name or not raw_path.strip():
        raise ValueError(f"expected NAME=PATH, got {spec!r}")
    return name, Path(raw_path.strip())


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(max(math.ceil(q * len(sorted_vals)) - 1, 0), len(sorted_vals) - 1)
    return sorted_vals[idx]


def _model_metrics_row(
    *,
    name: str,
    role: str,
    report: dict,
    glm_by_id: dict[str, dict],
    code_settled_ids: set[str],
) -> dict:
    rows = report["results"]
    by_id = {r["case_id"]: r for r in rows}
    total = len(rows)
    statuses = collections.Counter(r["status"] for r in rows)
    scores = collections.Counter(r["score"] for r in rows)
    secs = sorted(float(r["seconds"]) for r in rows)
    total_s = sum(secs)

    overall_only_glm = 0
    overall_only_cand = 0
    overall_both = 0
    overall_same_sel = 0
    req_only_glm = 0
    req_only_cand = 0
    req_both = 0
    req_acceptable = 0
    req_blunder = 0
    model_req_ids = [cid for cid in by_id if cid not in code_settled_ids]
    for cid, row in by_id.items():
        glm_row = glm_by_id[cid]
        cand_acc = row["score"] == "acceptable"
        glm_acc = glm_row["score"] == "acceptable"
        if glm_acc and not cand_acc:
            overall_only_glm += 1
        elif cand_acc and not glm_acc:
            overall_only_cand += 1
        elif cand_acc and glm_acc:
            overall_both += 1
        if (
            row["status"] == "answered"
            and glm_row["status"] == "answered"
            and row["selection_sha256"] == glm_row["selection_sha256"]
        ):
            overall_same_sel += 1
        if cid not in code_settled_ids:
            if row["score"] == "acceptable":
                req_acceptable += 1
            elif row["score"] == "blunder":
                req_blunder += 1
            if glm_acc and not cand_acc:
                req_only_glm += 1
            elif cand_acc and not glm_acc:
                req_only_cand += 1
            elif cand_acc and glm_acc:
                req_both += 1

    glm_scores = collections.Counter(r["score"] for r in glm_by_id.values())
    req_count = len(model_req_ids)
    return {
        "name": name,
        "role": role,
        "configuration_sha256": report["configuration_sha256"],
        "facts_sha256": report["facts_sha256"],
        "cases": total,
        "answered": statuses["answered"],
        "withheld": statuses["withheld"],
        "invalid": statuses["invalid"],
        "error": statuses["error"],
        "acceptable": scores["acceptable"],
        "acceptable_rate": (scores["acceptable"] / total) if total else 0.0,
        "blunder": scores["blunder"],
        "blunder_rate": (scores["blunder"] / total) if total else 0.0,
        "unlabelled_choice": scores["unlabelled_choice"],
        "not_scored": scores["not_scored"],
        "total_seconds": round(total_s, 3),
        "mean_seconds": round(statistics.fmean(secs), 4) if secs else 0.0,
        "median_seconds": round(statistics.median(secs), 4) if secs else 0.0,
        "p95_seconds": round(_percentile(secs, 0.95), 4) if secs else 0.0,
        "vs_glm": {
            "overall": {
                "acceptable_delta": scores["acceptable"] - glm_scores["acceptable"],
                "blunder_delta": scores["blunder"] - glm_scores["blunder"],
                "only_glm_acceptable": overall_only_glm,
                "only_candidate_acceptable": overall_only_cand,
                "both_acceptable": overall_both,
                "same_selection_count": overall_same_sel,
            },
            "model_required": {
                "cases": req_count,
                "acceptable": req_acceptable,
                "acceptable_rate": (req_acceptable / req_count) if req_count else 0.0,
                "blunder": req_blunder,
                "blunder_rate": (req_blunder / req_count) if req_count else 0.0,
                "only_glm_acceptable": req_only_glm,
                "only_candidate_acceptable": req_only_cand,
                "both_acceptable": req_both,
            },
        },
    }


def compare_models(
    *,
    code_report: dict,
    glm_report: dict,
    candidates: list[tuple[str, dict]],
    glm_variants: list[tuple[str, dict]] | None = None,
    glm_name: str = "glm_current",
) -> dict:
    """Compare code-only, GLM, GLM variants, and smaller candidate v1 reports."""
    br = _report_module()
    glm_variants = list(glm_variants or [])

    br.validate(code_report)
    if code_report["model_layer"] != "not_run":
        raise ValueError("code_report must have model_layer == 'not_run'")

    all_model_reports = [(glm_name, "glm", glm_report)]
    all_model_reports.extend((name, "glm_variant", rep) for name, rep in glm_variants)
    all_model_reports.extend((name, "candidate", rep) for name, rep in candidates)

    seen_names: set[str] = set()
    ref_corpus = code_report["corpus_sha256"]
    ref_labels = code_report["labels_sha256"]
    code_by_id = {r["case_id"]: r for r in code_report["results"]}
    ref_ids = set(code_by_id)
    if not ref_ids:
        raise ValueError("code_report contains zero cases")

    for name, _, report in all_model_reports:
        if name in seen_names:
            raise ValueError(f"duplicate model name: {name!r}")
        seen_names.add(name)
        br.validate(report)
        if report["model_layer"] != "run":
            raise ValueError(f"report {name!r} must have model_layer == 'run'")
        if report["corpus_sha256"] != ref_corpus:
            raise ValueError(f"corpus_sha256 mismatch for {name!r}")
        if report["labels_sha256"] != ref_labels:
            raise ValueError(f"labels_sha256 mismatch for {name!r}")
        cand_ids = {r["case_id"] for r in report["results"]}
        if cand_ids != ref_ids:
            raise ValueError(f"case_id set mismatch for {name!r}")

    code_settled_ids = {cid for cid, row in code_by_id.items() if row["status"] == "answered"}
    code_scores = collections.Counter(code_by_id[cid]["score"] for cid in code_settled_ids)
    model_req_ids = [cid for cid in code_by_id if cid not in code_settled_ids]
    glm_by_id = {r["case_id"]: r for r in glm_report["results"]}
    cand_maps = [{r["case_id"]: r for r in rep["results"]} for _, rep in candidates]

    any_cand_acc = 0
    all_cand_acc = 0
    only_glm_acc = 0
    cand_beats_glm = 0
    neither_acc = 0
    glm_acc_on_req = 0

    for cid in model_req_ids:
        glm_ok = glm_by_id[cid]["score"] == "acceptable"
        if glm_ok:
            glm_acc_on_req += 1
        cand_ok_list = [cmap[cid]["score"] == "acceptable" for cmap in cand_maps]
        any_ok = any(cand_ok_list)
        all_ok = bool(cand_ok_list) and all(cand_ok_list)
        if any_ok:
            any_cand_acc += 1
        if all_ok:
            all_cand_acc += 1
        if glm_ok and not any_ok:
            only_glm_acc += 1
        if any_ok and not glm_ok:
            cand_beats_glm += 1
        if not glm_ok and not any_ok:
            neither_acc += 1

    total = len(code_by_id)
    req_n = len(model_req_ids)
    model_rows = [
        _model_metrics_row(
            name=name,
            role=role,
            report=report,
            glm_by_id=glm_by_id,
            code_settled_ids=code_settled_ids,
        )
        for name, role, report in all_model_reports
    ]
    return {
        "corpus_sha256": ref_corpus,
        "labels_sha256": ref_labels,
        "cases": total,
        "split_by_who_answers": {
            "settled_by_code": {
                "cases": len(code_settled_ids),
                "share_of_all": len(code_settled_ids) / total,
                "acceptable": code_scores["acceptable"],
                "blunder": code_scores["blunder"],
                "unlabelled_choice": code_scores["unlabelled_choice"],
                "not_scored": code_scores["not_scored"],
            },
            "model_required_cases": req_n,
            "glm_acceptable": glm_acc_on_req,
            "any_candidate_acceptable": any_cand_acc,
            "all_candidates_acceptable": all_cand_acc,
            "only_glm_acceptable": only_glm_acc,
            "only_glm_share_of_all": only_glm_acc / total,
            "only_glm_share_of_model_required": (only_glm_acc / req_n) if req_n else 0.0,
            "candidate_beats_glm_acceptable": cand_beats_glm,
            "neither_acceptable": neither_acc,
        },
        "models": model_rows,
        "acceptance": "not_assessed",
    }


def render_table(comparison: dict) -> str:
    split = comparison["split_by_who_answers"]
    settled = split["settled_by_code"]
    lines = [
        f"H3 Offline Model Comparison ({comparison['cases']} cases; acceptance: {comparison['acceptance']})",
        f"  corpus_sha256: {comparison['corpus_sha256'][:16]}  labels_sha256: {comparison['labels_sha256'][:16]}",
        (
            f"  settled_by_code: {settled['cases']}/{comparison['cases']} "
            f"({settled['acceptable']} acceptable, {settled['blunder']} blunder); "
            f"model_required: {split['model_required_cases']}"
        ),
        (
            f"  model_required split: glm={split['glm_acceptable']}, "
            f"any_candidate={split['any_candidate_acceptable']}, "
            f"all_candidates={split['all_candidates_acceptable']}, "
            f"only_glm={split['only_glm_acceptable']} "
            f"({split['only_glm_share_of_all']:.1%} all / {split['only_glm_share_of_model_required']:.1%} model-req), "
            f"candidate_beats_glm={split['candidate_beats_glm_acceptable']}, "
            f"neither={split['neither_acceptable']}"
        ),
        "",
        (
            f"{'model':<28}{'role':<13}{'acc':>7}{'acc%':>8}{'blund':>7}{'blnd%':>8}"
            f"{'err':>6}{'med s':>8}{'p95 s':>8}{'onlyGLM':>9}{'onlyCnd':>9}"
        ),
    ]
    for row in comparison["models"]:
        mreq = row["vs_glm"]["model_required"]
        lines.append(
            f"{row['name']:<28}{row['role']:<13}{row['acceptable']:>7}"
            f"{row['acceptable_rate'] * 100:>7.1f}%{row['blunder']:>7}{row['blunder_rate'] * 100:>7.1f}%"
            f"{row['error']:>6}{row['median_seconds']:>8.2f}{row['p95_seconds']:>8.2f}"
            f"{mreq['only_glm_acceptable']:>9}{mreq['only_candidate_acceptable']:>9}"
        )
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-report", type=Path, required=True)
    parser.add_argument("--glm-report", type=Path, required=True)
    parser.add_argument("--glm-name", default="glm_current")
    parser.add_argument(
        "--glm-variant",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="additional GLM configuration report (e.g. glm_thinking_low=path.json)",
    )
    parser.add_argument(
        "--candidate",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="smaller candidate model report",
    )
    parser.add_argument("--format", choices=("table", "json"), default="table")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        code_rep = json.loads(args.code_report.read_text(encoding="utf-8"))
        glm_rep = json.loads(args.glm_report.read_text(encoding="utf-8"))
        variants = []
        for item in args.glm_variant:
            name, path = _parse_named_report(item)
            variants.append((name, json.loads(path.read_text(encoding="utf-8"))))
        cands = []
        for item in args.candidate:
            name, path = _parse_named_report(item)
            cands.append((name, json.loads(path.read_text(encoding="utf-8"))))
        result = compare_models(
            code_report=code_rep,
            glm_report=glm_rep,
            candidates=cands,
            glm_variants=variants,
            glm_name=args.glm_name,
        )
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.exit(2, f"invalid h3 comparison inputs: {error}\n")

    rendered = json.dumps(result, indent=2, sort_keys=True) if args.format == "json" else render_table(result)
    if args.out is not None:
        args.out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
