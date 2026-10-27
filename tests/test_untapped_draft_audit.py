"""Protect the evidence boundaries of the offline Companion comparison."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from scripts.audit_untapped_drafts import build_report, dynamic_comparison, match_coach, parse_log, summarize


def test_dynamic_ties_are_agreements_and_static_does_not_override():
    result = dynamic_comparison(
        ["a", "b", "c"],
        {
            "a": {"dynamicScore": 40, "staticScore": 5},
            "b": {"dynamicScore": 40, "staticScore": 1},
            "c": {"dynamicScore": 20, "staticScore": 100},
        },
        "b",
    )
    assert result["top_ids"] == ["a", "b"]
    assert result["agrees_with_top"] is True
    assert result["rank"] == 1
    assert result["gap"] == 0


def test_missing_dynamic_is_not_zero_or_substituted_static():
    result = dynamic_comparison(["a", "b"], {"a": {"dynamicScore": 30}, "b": {"staticScore": 100}}, "b")
    assert result["complete_coverage"] is False
    assert result["chosen_dynamic_score"] is None
    assert result["gap"] is None
    assert result["agrees_with_top"] is None


def test_summary_excludes_partial_coverage_and_forced_choices():
    rows = [
        {"comparison": dynamic_comparison(["a"], {"a": {"dynamicScore": 30}}, "a")},
        {"comparison": dynamic_comparison(["a", "b"], {"a": {"dynamicScore": 30}}, "a")},
        {
            "comparison": dynamic_comparison(
                ["a", "b"], {"a": {"dynamicScore": 30}, "b": {"dynamicScore": 10}}, "b"
            )
        },
    ]
    summary = summarize(rows, "comparison")
    assert summary["comparable_with_complete_scores"] == 2
    assert summary["nonforced_choices"] == 1
    assert summary["top_agreements"] == 0
    assert summary["mean_raw_gap"] == 20


def test_attribution_requires_unique_coordinate_time_and_offered_name():
    start = datetime(2026, 10, 25, 22, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    row = {"PackNumber": 1, "PickNumber": 1, "DraftPack": ["1", "2"], "PickedCard": "2"}
    candidate = {"timestamp": start, "pack": 1, "pick": 1, "names": ["A"]}
    match, _ = match_coach([candidate], row, {"1": "A", "2": "B"}, start, start + timedelta(hours=1))
    assert match == candidate  # Actual B does not get silently attributed to the coach.
    later = {**candidate, "timestamp": start + timedelta(minutes=1), "names": ["B"]}
    match, reason = match_coach(
        [candidate, later], row, {"1": "A", "2": "B"}, start, start + timedelta(hours=1)
    )
    assert match is None
    assert reason == "ambiguous_log_matches"
    match, _ = match_coach([candidate], row, {"1": "Other", "2": "B"}, start, start + timedelta(hours=1))
    assert match is None


def test_parse_log_only_exports_draft_evidence(tmp_path):
    log = tmp_path / "standalone.log"
    log.write_text(
        "2026-10-25 22:00:00 | INFO | other | account_secret=do-not-export\n"
        "2026-10-25 22:00:01 | INFO | arenamcp.draftstate | PremierDraft started: CubeDraft_Test (set: , picks_per_pack: 1)\n"
        "2026-10-25 22:00:02 | INFO | arenamcp.draft_event | Draft autoplay: P1p1: took Mox Emerald (model)\n"
        "2026-10-25 22:00:02 | INFO | arenamcp.draft_event | Draft pick P1p1 ['Mox Emerald']: ['Fast mana']\n"
        "2026-10-25 22:00:03 | INFO | arenamcp.draft_event | Draft pick P1p2 not confirmed: autoplay was switched off\n"
    )
    picks, events = parse_log(log, ZoneInfo("America/Los_Angeles"))
    assert len(picks) == 1
    assert picks[0]["names"] == ["Mox Emerald"]
    assert picks[0]["decision_source"] == "model"
    assert events[0]["event"] == "CubeDraft_Test"
    assert "do-not-export" not in repr((picks, events))


def test_sealed_record_does_not_inherit_nearby_draft_event():
    tz = ZoneInfo("America/Los_Angeles")
    start = datetime(2026, 10, 25, 22, 0, tzinfo=tz)
    report = build_report(
        {"sealed": {"startTime": start.timestamp() * 1000, "limitedType": "sealed"}},
        [],
        [{"timestamp": start - timedelta(minutes=1), "event": "QuickDraft_Test", "line": 1}],
        {},
        tz,
        set(),
    )
    assert report["drafts"][0]["event"] is None


def test_pick_source_and_model_cannot_leak_across_event_or_delay(tmp_path):
    log = tmp_path / "standalone.log"
    log.write_text(
        "2026-10-25 22:00:00 | INFO | Draft autoplay: P1p1: took A (model)\n"
        "2026-10-25 22:00:00 | INFO | {'served_model': 'old-model', 'call_class': 'draft.pick'}\n"
        "2026-10-25 22:00:01 | INFO | PremierDraft started: CubeDraft_New (set: )\n"
        "2026-10-25 22:00:02 | INFO | Draft pick P1p1 ['A']: ['Reason']\n"
        "2026-10-25 22:00:03 | INFO | Draft autoplay: P1p2: took B (ranking)\n"
        "2026-10-25 22:00:13 | INFO | Draft pick P1p2 ['B']: ['Reason']\n"
    )
    picks, events = parse_log(log, ZoneInfo("America/Los_Angeles"))
    assert all(p["decision_source"] is None for p in picks)
    assert all(p["served_model_context"] is None for p in picks)
    assert events[0]["source"] == str(log)
