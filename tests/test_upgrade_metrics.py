"""upgrade_metrics: reproduce the Fable November baseline metrics.

Two small synthetic bundles shaped like the real match bundles in
~/.arenamcp/match_bundles (see /home/joshu/fable-nov-evidence/arenamcp),
covering every metric in the M1 spec: W/L by autopilot, inference per call
class, decision answers (pass / code / single-option menus), planner line
searches with unevaluated options, plans formed and path-corrected, mulligan
keeps by land count, and prompt sizes.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

_TOOLS_DIR = pathlib.Path(__file__).resolve().parent.parent / "tools"
_SPEC = importlib.util.spec_from_file_location("upgrade_metrics", _TOOLS_DIR / "upgrade_metrics.py")
um = importlib.util.module_from_spec(_SPEC)
sys.modules["upgrade_metrics"] = um
_SPEC.loader.exec_module(um)


def _metrics_line(call_class, input_tokens, cached=None, reasoning=0, ms=0):
    return "[PROXY] Request metrics: " + repr(
        {
            "call_class": call_class,
            "input_tokens": input_tokens,
            "cached_input_tokens": cached,
            "reasoning_tokens": reasoning,
            "total_ms": ms,
        }
    )


def _decision(options, chosen, trace=None):
    return {
        "pending_decision": {"options": [{"option_id": o} for o in options]},
        "chosen_options": chosen,
        "trace": trace or {},
    }


BUNDLE_A = {
    "match_id": "m1",
    "started_at": 1794300000,  # 2026-11-10 UTC
    "result": "win",
    "settings_subset": {"autopilot_enabled": True},
    "coach_log": {
        "lines": [
            _metrics_line("decision.typed", 1000, None, 50, 2000),
            _metrics_line("background.game_plan", 2000, 500, 100, 4000),
            "Game plan reform (first plan): attack with everything.",
            "GamePlan validation (turn 5): path: plan claim corrected: "
            "trade the flyer, then swing for lethal.",
            "typed-decision: big prompt (12000 chars)",
            "typed-decision: big prompt (20000 chars)",
            "Mulligan guard: KEEP: 2 lands on the draw with cheap plays (Lumra scout into curve).",
            # hand-size line: must NOT count as a land count
            "Mulligan guard: KEEP: keeping 4: never mulligan to 3.",
            # qualitative keep: no land count -> excluded
            "Mulligan guard: KEEP: Mountains cast Laelia early.",
        ]
    },
    "packet": {
        "decisions": [
            # line search with an unevaluated option; the chosen one is unevaluated
            _decision(
                ["idx:0", "idx:1"],
                ["idx:0"],
                {
                    "lines": {
                        "summary": "LINES (2-turn search, greedy 3rd turn extension)",
                        "tags": {"attackers": ["idx:0"]},
                        "unmodelled": {
                            "idx:0": "Laelia, the Blade Reforged (its triggered ability)",
                            "idx:1": "Throes of Chaos (cascade)",
                        },
                    },
                    "llm_usage": {"input_tokens": 10},
                },
            ),
            # pass answer with a model call
            _decision(["idx:0", "idx:1", "idx:2"], ["pass"], {"llm_usage": {"input_tokens": 10}}),
            # single-option menu answered by code (no llm_usage)
            _decision(["idx:0"], ["idx:0"]),
        ]
    },
}

BUNDLE_B = {
    "match_id": "m2",
    "started_at": 1794386400,  # next day, 2026-11-11 UTC
    "result": "loss",
    "settings_subset": {"autopilot_enabled": False},
    "coach_log": {
        "lines": [
            _metrics_line("decision.typed", 3000, 750, 25, 1000),
        ]
    },
    "packet": {
        "decisions": [
            # unevaluated options exist but the CHOSEN one is evaluated ->
            # chosen_unmodelled must not increase
            _decision(
                ["idx:0", "idx:1"],
                ["idx:1"],
                {
                    "lines": {
                        "summary": "LINES (2-turn search)",
                        "tags": {},
                        "unmodelled": {"idx:0": "Chandra, Fire of Kaladesh"},
                    },
                    "llm_usage": {"input_tokens": 10},
                },
            ),
        ]
    },
}


@pytest.fixture()
def bundles_dir(tmp_path):
    d = tmp_path / "match_bundles"
    d.mkdir()
    (d / "aaa.json.gz").write_bytes(_gzip(BUNDLE_A))
    (d / "bbb.json.gz").write_bytes(_gzip(BUNDLE_B))
    # a live_context.json pointer must be ignored
    (d / "live_context.json").write_text(json.dumps({"match_id": None}))
    return d


def _gzip(obj):
    import gzip
    import io

    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as fh:
        fh.write(json.dumps(obj).encode())
    return buf.getvalue()


def test_parse_request_metrics_variants():
    assert um.parse_request_metrics(
        "[PROXY] Request metrics: {'call_class': 'decision.typed', 'input_tokens': 123}"
    ) == {"call_class": "decision.typed", "input_tokens": 123}
    assert um.parse_request_metrics("[PROXY] Request metrics: not a dict") is None
    assert um.parse_request_metrics("unrelated coach line") is None


def test_parse_keep_lands_variants():
    assert um.parse_keep_lands("[PROXY] Mulligan guard: KEEP: 2 lands on the draw.") == 2
    assert um.parse_keep_lands("[PROXY] Mulligan guard: KEEP: 3 lands and a curve.") == 3
    # hand size, not lands
    assert um.parse_keep_lands("[PROXY] Mulligan guard: KEEP: keeping 4: never mulligan to 3.") is None
    # qualitative keep without a land count
    assert um.parse_keep_lands("[PROXY] Mulligan guard: KEEP: Mountains cast Laelia early.") is None


def test_unmodelled_card_name():
    assert (
        um.unmodelled_card_name("Laelia, the Blade Reforged (its triggered ability)")
        == "Laelia, the Blade Reforged"
    )
    assert um.unmodelled_card_name("plain text") == "plain text"


def test_aggregate_all_nine_metrics(bundles_dir):
    summaries = [um.summarize_bundle(um.load_bundle(p)) for p in sorted(bundles_dir.glob("*.json.gz"))]
    report = um.aggregate(summaries)

    # 1. W/L by autopilot — match level, one row per match
    assert report["matches"] == 2
    on = report["autopilot"]["on"]
    off = report["autopilot"]["off"]
    assert (on["matches"], on["wins"], on["losses"]) == (1, 1, 0)
    assert (off["matches"], off["wins"], off["losses"]) == (1, 0, 1)

    # 2. inference per call class (+ cached tokens reported even when absent)
    inf = report["inference_per_class"]
    dec = inf["decision.typed"]
    assert dec["calls"] == 2 and dec["calls_per_match"] == pytest.approx(1.0)
    assert dec["input_per_call"] == pytest.approx(2000.0)
    assert dec["cached_per_call"] == pytest.approx(375.0)  # (0+750)/2
    assert dec["think_per_call"] == pytest.approx(37.5)
    assert dec["sec_per_call"] == pytest.approx(1.5)
    plan = inf["background.game_plan"]
    assert plan["calls"] == 1
    assert plan["cached_per_call"] == pytest.approx(500.0)
    tot = report["inference_totals"]
    assert tot["calls_per_match"] == pytest.approx(1.5)
    assert tot["input_per_call"] == pytest.approx(2000.0)

    # 3. decision answers
    ans = report["answers"]
    assert ans["total"] == 4  # 3 in A + 1 in B
    assert ans["pass"] == 1  # A d2
    assert ans["code_answered_no_model_call"] == 1  # A d3 (no llm_usage)
    assert ans["single_option_menus"] == 1  # A d3

    # 4. planner line searches with unevaluated options
    ls = report["line_search"]
    assert ls["decisions_with_line_search"] == 2  # A d1 + B d1
    assert ls["with_unevaluated_option"] == 2  # both have unmodelled
    assert ls["chosen_was_unevaluated"] == 1  # only A d1
    cards = ls["top_unevaluated_cards"]
    assert cards[0][0] == "Laelia, the Blade Reforged"  # most frequent first

    # 5. plans formed / path corrected
    assert report["plans_formed"] == 1
    assert report["plans_path_corrected"] == 1

    # 6. keeps by land count (hand-size and qualitative lines excluded)
    keeps = report["keeps_by_lands"]
    assert keeps[2]["games"] == 1 and keeps[2]["wins"] == 1
    assert 3 not in keeps  # no 3-land keep in the fixtures

    # 7. prompt sizes from the two big-prompt lines
    assert report["prompt_size_median_chars"] >= 12000
