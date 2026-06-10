"""Deterministic post-match review: only evidenced findings, no LLM claims.

The log fixtures replicate real lines from match 9d7d486b (2026-06-10),
the match whose prose analysis invented a concede recommendation — the
motivating case for this module.
"""

from datetime import datetime

from arenamcp.match_review import (
    Finding,
    build_issue,
    detect_advice_repetition,
    detect_manual_required,
    detect_matcher_dead_ends,
    detect_platform_noise,
    detect_rejected_decisions,
    detect_unresolved_cards,
    detect_validator_dropped_legal,
    detect_win_prob_misses,
    read_log_slice,
    run_match_review,
    should_file_issue,
)

LOG = """\
2026-06-10 12:36:33 | WARNING  | arenamcp.gre_action_matcher | Could not match CAST_SPELL 'normally' among 0 Cast actions
2026-06-10 12:36:35 | WARNING  | arenamcp.gre_action_matcher | Could not match CAST_SPELL 'normally' among 0 Cast actions
2026-06-10 12:37:27 | INFO     | arenamcp.autopilot | [AUTOPILOT] MANUAL REQUIRED: Planner produced no safe action [No bridge request pending (pending_decision='Choose Casting Option')]
2026-06-10 12:41:09 | INFO     | arenamcp.gamestate | Captured Decision: Select Items (1 items, type=select_n, options=['Card#191049', 'Card#194019', 'Ureni of the Unwritten'])
2026-06-10 12:45:10 | INFO     | arenamcp.gamestate | Captured Decision: Pay Costs (source: Card#194020, mana: 1xGeneric, 1xG, autotap=True)
2026-06-10 12:45:34 | WARNING  | arenamcp.action_planner | Dropping illegal planner action: click_button (Auto-pay) bridge_request='PayCostsReq' decision='pay_costs' not in ['Pay costs for Card#194020', 'Auto-pay']
2026-06-10 12:47:50 | INFO     | arenamcp.coach | [WIN-PROB] WIN: 15%
2026-06-10 12:49:33 | INFO     | arenamcp.autopilot | [AUTOPILOT] MANUAL REQUIRED: Bridge couldn't handle declare_blockers (?) — take this action manually. [Bridge gap: DeclareBlockers (type=declare_blockers)]
2026-06-10 12:39:05 | ERROR    | arenamcp.screen_capture | All screenshot methods failed: cannot identify image file '/tmp/a.png'
2026-06-10 12:41:37 | ERROR    | arenamcp.screen_capture | All screenshot methods failed: cannot identify image file '/tmp/b.png'
"""


def test_unresolved_cards_counts_and_severity():
    f = detect_unresolved_cards(LOG, [])
    assert len(f) == 1
    assert f[0].category == "card_db"
    assert f[0].severity == "high"  # 3 distinct ids
    assert any("191049" in e for e in f[0].evidence)


def test_manual_required_grouped_by_reason():
    f = detect_manual_required(LOG)
    assert len(f) == 2  # casting-option + declare_blockers
    assert all(x.severity == "high" for x in f)


def test_validator_dropped_legal_catches_autopay_bug():
    f = detect_validator_dropped_legal(LOG)
    assert len(f) == 1
    assert "Auto-pay" in f[0].title


def test_matcher_dead_end_requires_repeats():
    f = detect_matcher_dead_ends(LOG)
    assert len(f) == 1 and "'normally'" in f[0].title
    single = "2026-01-01 00:00:00 | W | x | Could not match CAST_SPELL 'Bolt' among 0 Cast actions"
    assert detect_matcher_dead_ends(single) == []


def test_win_prob_miss_on_low_estimate_win(tmp_path, monkeypatch):
    import arenamcp.match_review as mr
    monkeypatch.setattr(mr, "CALIBRATION_LOG", tmp_path / "cal.jsonl")
    f = detect_win_prob_misses(LOG, "win")
    assert len(f) == 1 and "15%" in f[0].title
    assert (tmp_path / "cal.jsonl").exists()
    # 15% then a loss is NOT a miss
    assert detect_win_prob_misses(LOG, "loss") == []


def test_platform_noise_needs_two_hits():
    assert len(detect_platform_noise(LOG)) == 1
    assert detect_platform_noise(LOG.splitlines()[8]) == []


def test_rejected_decisions_from_packet():
    packet = {"decisions": [
        {"pending_decision": {"request_type": "SelectTargets", "request_id": [1, 2]},
         "chosen_options": ["tgt:5"], "outcome": "REJECTED"},
        {"pending_decision": {"request_type": "Mulligan"},
         "chosen_options": ["mull:keep"], "outcome": "ADVANCED"},
    ]}
    f = detect_rejected_decisions(packet)
    assert len(f) == 1 and "REJECTED" in f[0].title


def test_advice_repetition_threshold():
    hist = [{"advice": "Searching for a Forest to trigger Landfall."}] * 4
    assert len(detect_advice_repetition(hist)) == 1
    assert detect_advice_repetition(hist[:3]) == []


def test_run_match_review_end_to_end(tmp_path, monkeypatch):
    import arenamcp.match_review as mr
    monkeypatch.setattr(mr, "CALIBRATION_LOG", tmp_path / "cal.jsonl")
    findings = run_match_review(
        advice_history=[{"advice": "Play Forest."}],
        match_result="win",
        log_slice=LOG,
        packet=None,
    )
    cats = {f.category for f in findings}
    assert {"card_db", "autopilot", "planner", "advice"} <= cats
    assert should_file_issue(findings)
    title, body = build_issue("9d7d486b-x", "win", findings, version="2.4.0")
    assert title.startswith("[match-review] win 9d7d486b")
    assert "Auto-pay" in body and "191049" in body


def test_low_only_findings_do_not_file():
    assert not should_file_issue([Finding("advice", "t", "d", severity="low")])
    assert should_file_issue([Finding("advice", "t", "d", severity="medium")])


def test_read_log_slice_respects_since(tmp_path):
    p = tmp_path / "log.txt"
    p.write_text(
        "2026-06-10 11:00:00 | old line\n"
        "2026-06-10 12:00:00 | new line\n"
        "continuation without timestamp\n",
        encoding="utf-8",
    )
    s = read_log_slice(p, datetime(2026, 6, 10, 11, 30))
    assert "old line" not in s
    assert "new line" in s and "continuation" in s
