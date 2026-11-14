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


# ---------------------------------------------------------------------------
# T1 — time lost on the clock (plan section T1)
#
# Fixture shaped like the real logged case named in the plan/report:
# turn 11 of match 9e858d62 on 2026-11-08 (~22:12 local), where consecutive
# no-op pass windows each paid a full model call while the active-player
# rope ran out.
# ---------------------------------------------------------------------------


def _t1_metrics(ms):
    return _metrics_line("decision.typed", 45000, None, 500, ms)


# Real logged window-change form (match 9e858d62 turn 11): the churn hops all
# carry this identical signature between consecutive passes.
_T1_SIG = ("11", "1", "2", "Phase_Main1", "-")


def _t1_clearing(sig=_T1_SIG, ts="2026-11-08 22:12:00.000"):
    return (
        f"{ts} | INFO | arenamcp.gamestate | Clearing stale decision "
        "'Choose Action' (window change to "
        f"turn={sig[0]} active={sig[1]} priority={sig[2]} "
        f"phase={sig[3]} step={sig[4]})"
    )


def _t1_pass(ts):
    return f"{ts} | INFO | arenamcp.gre_bridge | GRE bridge submitted pass"


T1_BUNDLE = {
    "match_id": "9e858d62",
    "started_at": 1794204615,
    "result": "loss",
    "settings_subset": {"autopilot_enabled": True},
    "final_state": {"local_seat_id": 1},
    "coach_log": {
        "lines": [
            # the no-op window opens (real logged form)
            _t1_clearing(ts="2026-11-08 22:11:58.000"),
            # no-op window #2 pays a full model call just to pass again
            ("2026-11-08 22:12:01.100 | INFO | Autopilot planning: trigger=post-cast priority"),
            ("2026-11-08 22:12:02.000 | INFO | post-cast priority: new pass request surfaced"),
            _t1_metrics(4500),
            _t1_pass("2026-11-08 22:12:07.000"),
            # the same window is re-presented (identical signature) ...
            _t1_clearing(ts="2026-11-08 22:12:07.100"),
            # no-op window #3 pays another full model call -> counted as waste
            ("2026-11-08 22:12:09.000 | INFO | post-cast priority: new pass request surfaced"),
            _t1_metrics(5200),
            _t1_pass("2026-11-08 22:12:15.000"),
            _t1_clearing(ts="2026-11-08 22:12:15.100"),
            # hop-limit yield; the trigger path picks the follow up 0.2 s
            # later -> measured handoff delay (the model call that then
            # answers it is legitimate decision time, not waiting)
            (
                "2026-11-08 22:12:20.000 | INFO | post-cast priority: "
                "hop limit (3) reached; leaving pass to the watchdog"
            ),
            ("2026-11-08 22:12:20.200 | INFO | arenamcp.coach_triggers | Triggering decision: pass"),
            _t1_pass("2026-11-08 22:12:26.000"),
            # planning cancelled by a request change -> measured waste
            ("2026-11-08 22:12:30.000 | INFO | Autopilot planning: trigger=declare attackers"),
            ("2026-11-08 22:12:33.000 | INFO | request changed during planning; discarding partial plan"),
            # surfaced request that never gets an answer inside the window
            ("2026-11-08 22:13:50.000 | INFO | post-cast priority: new block request surfaced"),
        ]
    },
    # GRE player_log event anchoring turn start + active-player rope length
    "player_log": {
        "events": [
            {
                "header_ts": "11/8/2026 10:12:00 PM",
                "json": '{"turnInfo": {"turnNumber": 11, "activePlayer": 1}, '
                '"timerStateMessage": {"seatId": 1, "timers": '
                '[{"type": "TimerType_ActivePlayer", "durationSec": 90, "behavior": "TimerBehavior_TakeControl", "running": true}, '
                '{"type": "TimerType_NonActivePlayer", "durationSec": 30, "behavior": "TimerBehavior_Timeout", "running": true}]}}',
            }
        ]
    },
}


def test_t1_surfaced_requests_ask_to_answer_and_rope_left():
    t1 = um.summarize_bundle(T1_BUNDLE)["t1"]
    # answers land ~5 s after each surface; rope-left is anchored on the
    # GRE turn start (22:12:00) minus elapsed time from the active rope (90 s)
    assert t1["events"] == [
        {"event_type": "pass", "ask_to_ans_s": pytest.approx(5.0), "rope_left_s": pytest.approx(83.0)},
        {"event_type": "pass", "ask_to_ans_s": pytest.approx(6.0), "rope_left_s": pytest.approx(75.0)},
    ]
    # the block request near end-of-log never got an answer
    assert t1["unanswered_by_type"] == {"block": 1}


def test_t1_redundant_pass_cycles_named_case():
    t1 = um.summarize_bundle(T1_BUNDLE)["t1"]
    # second consecutive pass window burned a full model call (5.2 s) only to
    # conclude pass again; the third submit had no model call between ->
    # correctly not counted as waste
    assert t1["redundant_pass_cycles_s"] == [pytest.approx(5.2)]


def test_t1_redundant_pass_needs_unchanged_window():
    """A pass pair spanning a real window change is not redundant (review 2026-11-11).

    The old detector counted any two adjacent passes within 30 s; the
    logged window signature is what says the window is unchanged.
    """
    lines = [
        _t1_clearing(ts="2026-11-08 22:12:00.000"),
        _t1_metrics(4000),
        _t1_pass("2026-11-08 22:12:07.000"),
        # the game moved on: a different turn/phase/step between the passes
        _t1_clearing(sig=("12", "1", "1", "Phase_Combat", "-"), ts="2026-11-08 22:12:08.000"),
        _t1_metrics(5000),
        _t1_pass("2026-11-08 22:12:15.000"),
    ]
    assert um.redundant_pass_cycles(lines) == []


def test_t1_redundant_pass_counts_only_decision_class_seconds():
    """Background plan seconds inside the interval are not pass-cycle waste."""
    lines = [
        _t1_clearing(ts="2026-11-08 22:12:00.000"),
        _t1_metrics(2000),
        _t1_pass("2026-11-08 22:12:07.000"),
        _t1_clearing(ts="2026-11-08 22:12:07.100"),
        # a background plan call ran in the same interval — not attributable
        _metrics_line("background.game_plan", 11000, None, 300, 9000),
        _t1_metrics(2500),
        _t1_pass("2026-11-08 22:12:20.000"),
    ]
    assert um.redundant_pass_cycles(lines) == [pytest.approx(2.5)]


def test_t1_bridge_double_logged_submission_counts_once():
    """The mac and GRE bridges log one submission twice ~3 ms apart."""
    lines = [
        _t1_clearing(ts="2026-11-08 22:12:00.000"),
        _t1_metrics(4000),
        "2026-11-08 22:12:07.000 | INFO | arenamcp.mac_bridge_adapter | "
        "mac bridge submit_pass: {'ok': True, 'submitted_type': 'Pass'}",
        _t1_pass("2026-11-08 22:12:07.003"),
        _t1_clearing(ts="2026-11-08 22:12:07.100"),
        _t1_metrics(5000),
        "2026-11-08 22:12:15.000 | INFO | arenamcp.mac_bridge_adapter | "
        "mac bridge submit_pass: {'ok': True, 'submitted_type': 'Pass'}",
        _t1_pass("2026-11-08 22:12:15.002"),
    ]
    # one cycle (5.0 s), not two half-cycles split by the double log
    assert um.redundant_pass_cycles(lines) == [pytest.approx(5.0)]


def test_t1_hop_limit_watchdog_wait():
    t1 = um.summarize_bundle(T1_BUNDLE)["t1"]
    # yield -> the trigger path picks the follow-up up (0.2 s), not
    # yield -> submit: the answering model call is decision time, not wait
    assert t1["hop_waits_s"] == [pytest.approx(0.2)]


def test_t1_cancelled_planning_waste():
    t1 = um.summarize_bundle(T1_BUNDLE)["t1"]
    assert t1["cancellation_marker_families"] == ["other"]


def test_t1_final_clock_snapshot_records_local_seat_and_timers():
    t1 = um.summarize_bundle(T1_BUNDLE)["t1"]
    assert t1["local_seat"] == 1
    assert t1["final_timers_by_seat"] == {
        1: {"active": pytest.approx(90.0), "nonactive": pytest.approx(30.0)}
    }


# Real logged GRE payload forms (match bundles under
# /home/joshu/fable-nov-evidence/arenamcp/match_bundles): turnInfo carries
# turnNumber/activePlayer; each timerStateMessage carries its owning seatId.

_TURN11_EVENT = {
    "header_ts": "11/8/2026 10:12:00 PM",
    "json": '{"turnInfo": {"turnNumber": 11, "activePlayer": 1}, '
    '"timerStateMessage": {"seatId": 1, "timers": ['
    '{"type": "TimerType_ActivePlayer", "durationSec": 90, "behavior": "TimerBehavior_TakeControl", "running": true}, '
    '{"type": "TimerType_NonActivePlayer", "durationSec": 30, "behavior": "TimerBehavior_Timeout", "running": true}]}}',
}

_TURN11_SEAT2_EVENT = {
    "header_ts": "11/8/2026 10:12:00 PM",
    "json": '{"timerStateMessage": {"seatId": 2, "timers": ['
    '{"type": "TimerType_ActivePlayer", "durationSec": 90, "behavior": "TimerBehavior_TakeControl", "running": true}, '
    '{"type": "TimerType_NonActivePlayer", "durationSec": 30, "behavior": "TimerBehavior_Timeout", "running": true}]}}',
}

_TURN12_EVENT = {
    "header_ts": "11/8/2026 10:14:00 PM",
    "json": '{"turnInfo": {"turnNumber": 12, "activePlayer": 2}, '
    '"timerStateMessage": {"seatId": 2, "timers": ['
    '{"type": "TimerType_ActivePlayer", "durationSec": 75, "behavior": "TimerBehavior_TakeControl", "running": true}, '
    '{"type": "TimerType_NonActivePlayer", "durationSec": 25, "behavior": "TimerBehavior_Timeout", "running": true}]}}',
}


def _gre_epoch(header_ts):
    return um.gre_event_epoch({"header_ts": header_ts})


def test_t1_rope_left_uses_clock_state_known_at_each_event():
    """Historical rope-left never moves when later turns are appended.

    Verifier temporal repro (2026-11-11 round-3 rejection): a GRE event at
    11/8/2026 10:12 PM (turn 11, activePlayer 1, active rope 90 s) measured
    rope-left 85.0 s at +5 s; appending a later 10:14 PM turn-12 event with
    activePlayer 2 dragged the SAME historical timestamp to 25.0 s because
    the old timeline kept only the match-final active seat and rope lengths.
    """
    ask = _gre_epoch("11/8/2026 10:12:05 PM")
    clock = um.gre_clock_timeline([_TURN11_EVENT])
    assert um.rope_seconds_left(clock, ask, local_seat=1) == pytest.approx(85.0)

    clock = um.gre_clock_timeline([_TURN11_EVENT, _TURN11_SEAT2_EVENT, _TURN12_EVENT])
    # the historical value is unchanged by the appended future turn
    assert um.rope_seconds_left(clock, ask, local_seat=1) == pytest.approx(85.0)
    # the future turn reads its own state as of then
    later = _gre_epoch("11/8/2026 10:14:05 PM")
    assert um.rope_seconds_left(clock, later, local_seat=2) == pytest.approx(70.0)
    # Seat 1 has no new report: its earlier explicitly running 30s timer
    # has expired after 125s. A turn change alone does not reset that timer.
    assert um.rope_seconds_left(clock, later, local_seat=1) == pytest.approx(0.0)


def test_t1_rope_side_tracks_active_transitions_for_local_seat_2():
    """Local seat 2's rope side follows each turn's active seat as of then."""
    clock = um.gre_clock_timeline([_TURN11_EVENT, _TURN11_SEAT2_EVENT, _TURN12_EVENT])
    during_turn11 = _gre_epoch("11/8/2026 10:12:05 PM")
    during_turn12 = _gre_epoch("11/8/2026 10:14:05 PM")
    # turn 11: seat 1 active -> local seat 2 is on its nonactive rope (30 s)
    assert um.rope_seconds_left(clock, during_turn11, local_seat=2) == pytest.approx(25.0)
    # turn 12: seat 2 active -> local seat 2 is on its active rope (75 s)
    assert um.rope_seconds_left(clock, during_turn12, local_seat=2) == pytest.approx(70.0)


def test_t1_rope_left_never_guesses_the_local_seat():
    """Without a known local seat no side can be picked; return None."""
    clock = um.gre_clock_timeline([_TURN11_EVENT])
    ask = _gre_epoch("11/8/2026 10:12:05 PM")
    assert um.rope_seconds_left(clock, ask) is None


def test_t1_aggregate_and_render():
    report = um.aggregate([um.summarize_bundle(T1_BUNDLE)])
    t1 = report["t1"]

    by_type = t1["by_event_type"]["pass"]
    assert by_type["answered"] == 2 and by_type["unanswered"] == 0
    assert by_type["ask_to_ans"]["median_s"] == pytest.approx(6.0)
    assert by_type["ask_to_ans"]["total_s"] == pytest.approx(11.0)
    assert by_type["rope_left_at_answer"]["min_s"] == pytest.approx(75.0)

    assert t1["hop_limit_watchdog_waits"]["total_s"] == pytest.approx(0.2)
    assert t1["cancellation_markers"]["marker_count"] == 1
    assert t1["cancellation_markers"]["attributable_duration_s"] is None
    assert t1["redundant_pass_cycles"]["total_s"] == pytest.approx(5.2)

    pm = t1["per_match"]["9e858d62"]
    assert pm["surfaced_events"] == 2
    assert pm["events_by_type"]["pass"]["min_rope_left_s"] == pytest.approx(75.0)

    text = um.render_report(report)
    assert "Time on the Arena clock (T1):" in text
    assert "Candidate redundant model calls on unchanged pass windows" in text
    assert "caveat: equal window-change signatures mark candidate waste only" in text
    assert (
        t1["redundant_pass_cycles"]["interpretation"]
        == "candidate waste intervals identified from equal window-change "
        "signatures; signature equality does not prove the stack, menu or "
        "material state was unchanged, and a static rerun of old bundles "
        "cannot demonstrate live savings"
    )


def test_clock_recovered_real_00651f77_reports():
    fixture = json.loads((pathlib.Path(__file__).parent / "fixtures/t1_clock_00651f77.json").read_text())
    events = fixture["events"]
    clock = um.gre_clock_timeline(events)
    for header, expected in [
        ("11/10/2026 8:28:50 AM", 93.169),
        ("11/10/2026 8:29:28 AM", 72.081),
        ("11/10/2026 8:29:37 AM", 98.714),
    ]:
        event = next(e for e in events if e["header_ts"] == header)
        assert um.rope_seconds_left(clock, um.gre_event_epoch_precise(event), 2) == pytest.approx(
            expected, abs=0.001
        )


def test_clock_paused_elapsed_and_later_same_second():
    event = {
        "header_ts": "11/10/2026 8:28:50 AM",
        "json": json.dumps(
            {
                "timestamp": "1794328130255",
                "turnInfo": {"turnNumber": 1, "activePlayer": 2},
                "timerStateMessage": {
                    "seatId": 2,
                    "timers": [
                        {
                            "type": "TimerType_ActivePlayer",
                            "behavior": "TimerBehavior_TakeControl",
                            "durationSec": 90,
                            "elapsedMs": 30000,
                            "running": False,
                        }
                    ],
                },
            }
        ),
    }
    epoch = um.gre_event_epoch_precise(event)
    assert um.rope_seconds_left(um.gre_clock_timeline([event]), epoch + 10, 2) == pytest.approx(60)
    later = json.loads(json.dumps(event))
    data = json.loads(later["json"])
    data["timestamp"] = "1794328130800"
    data["timerStateMessage"]["timers"][0].update(elapsedMs=40000, running=True)
    later["json"] = json.dumps(data)
    clock = um.gre_clock_timeline([event, later])
    assert um.rope_seconds_left(clock, epoch, 2) == pytest.approx(60)
    assert um.rope_seconds_left(clock, um.gre_event_epoch_precise(later) + 1, 2) == pytest.approx(49)


def test_clock_unknown_timer_ownership_is_not_guessed():
    event = {
        "header_ts": "11/10/2026 8:28:50 AM",
        "json": json.dumps(
            {
                "turnInfo": {"activePlayer": 1},
                "timers": [
                    {
                        "timerId": 3,
                        "type": "TimerType_ActivePlayer",
                        "behavior": "TimerBehavior_TakeControl",
                        "durationSec": 90,
                        "running": True,
                    }
                ],
            }
        ),
    }
    assert um.rope_seconds_left(um.gre_clock_timeline([event]), um.gre_event_epoch(event), 1) is None


@pytest.mark.parametrize(
    "boundary",
    [
        "post-cast priority: new block request surfaced",
        "request changed during planning",
    ],
)
def test_t1_superseded_request_does_not_borrow_later_answer(boundary):
    lines = [
        "2026-11-08 22:00:00.000 post-cast priority: new action request surfaced",
        "2026-11-08 22:00:02.000 " + boundary,
        _t1_pass("2026-11-08 22:00:05.000"),
    ]
    events, unanswered = um.surfaced_request_events(lines, [])
    assert unanswered == {"action": 1}
    assert [event["ask_to_ans_s"] for event in events] == ([3.0] if "surfaced" in boundary else [])


@pytest.mark.parametrize("result", ["{'ok': False}", "{'ok': 1}", "{}", "broken", "{'ok': True} garbage"])
def test_t1_failed_or_unproven_submit_is_not_answer(result):
    lines = [
        "2026-11-08 22:00:00.000 post-cast priority: new action request surfaced",
        "2026-11-08 22:00:01.000 mac bridge submit_pass: " + result,
    ]
    assert um.surfaced_request_events(lines, []) == ([], {"action": 1})
    lines.append("2026-11-08 22:00:03.000 mac bridge submit_pass: {'ok': True}")
    events, unanswered = um.surfaced_request_events(lines, [])
    assert not unanswered
    assert [event["ask_to_ans_s"] for event in events] == [3.0]


def test_t1_unanswered_only_types_survive_global_aggregation():
    fold = {
        "t1": {
            "events": [],
            "unanswered_by_type": {"block": 2},
            "hop_waits_s": [],
            "cancellation_marker_families": [],
            "redundant_pass_cycles_s": [],
        }
    }
    output = um.aggregate_t1({"one": fold, "two": fold})
    assert output["by_event_type"]["block"]["unanswered"] == 4
    assert output["by_event_type"]["block"]["answered"] == 0
    assert output["per_match"]["one"]["unanswered_by_type"] == {"block": 2}


def test_cancellation_markers_never_infer_unique_calls_or_elapsed_cost():
    lines = [
        "2026-11-08 22:00:00.000 Planning actions for action window",
        "2026-11-08 22:00:03.000 typed-decision: request changed during planning",
        "2026-11-08 22:00:03.000 Backend returned error sentinel; no plan: request changed during planning",
        "2026-11-08 22:00:03.000 Autopilot: request changed during planning",
        "2026-11-08 22:00:03.000 plan_decision_options LLM path failed: request changed during planning",
        "no timestamp request changed during planning",
    ]
    families = um.cancellation_marker_families(lines)
    out = um.cancellation_summary(families)
    assert out["marker_count"] == 5
    assert all(value == 1 for value in out["families"].values())
    assert out["unique_requests"] is None
    assert out["attributable_duration_s"] is None
    assert out["causal_time_rank_eligible"] is False
    # Repeated logging is counted honestly, not deduplicated into guessed calls.
    assert um.cancellation_summary(families * 2)["marker_count"] == 10
    assert um.cancellation_summary([])["attributable_duration_s"] is None


def test_cancellation_report_does_not_advertise_unproven_waste():
    agg = um.aggregate([um.summarize_bundle(T1_BUNDLE)])
    text = um.render_report(agg)
    assert "log markers" in text
    assert "attributable duration unproven" in text
    assert "cancel marks" in text
    assert "wasting" not in text
    assert "cancelled_planning_calls" not in agg["t1"]
