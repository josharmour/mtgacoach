#!/usr/bin/env python3
"""Baseline metrics for the Fable November upgrades (plan milestone M1).

Replays the match bundles that the coach writes under ``~/.arenamcp/match_bundles``
and reproduces the baseline numbers documented in section 1 of
``fable-november-upgrades.md`` (repo root):

* matches / wins / losses, split by ``settings_subset.autopilot_enabled``
* LLM inference per call class (calls, input tokens, cached input tokens,
  thinking tokens, seconds — per match and per call), from the
  ``[PROXY] Request metrics: {...}`` lines in ``coach_log``
* decision answers: total, ``pass`` answers, answers resolved by code without
  a model call, and menus that offered a single option
* line search: decisions that ran one, decisions with an unevaluated option,
  chosen options that were unevaluated, and the most frequent unevaluated cards
* game plans formed, and plans whose ``path`` was corrected
  (``GamePlan validation ... path: corrected ...``)
* mulligan-guard keeps grouped by number of lands kept, with win rate
* decision prompt size (``typed-decision: big prompt (N chars)``): median / p90

Usage::

    python tools/upgrade_metrics.py                       # ~/.arenamcp/match_bundles
    python tools/upgrade_metrics.py --dir DIR --since 2026-11-09
    python tools/upgrade_metrics.py --json                # machine-readable

A "match" is a unique ``match_id``; a bundle is one game of that match.
Decisions with no ``chosen_options`` (e.g. superseded by an executed action)
are not counted as answers.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

DEFAULT_BUNDLES_DIR = os.path.expanduser("~/.arenamcp/match_bundles")

REQUEST_METRICS_RE = re.compile(r"Request metrics:?\s*(\{.*\})\s*$")
KEEP_LINE_RE = re.compile(r"Mulligan guard: KEEP")
LANDS_RE = re.compile(r"(\d+) lands?")
BIG_PROMPT_RE = re.compile(r"typed-decision: big prompt \((\d+) chars\)")
PLAN_FORMED_RE = re.compile(r"Game plan reform \(")
# Real logged form: "GamePlan validation (turn 12): path: plan claim corrected: ..."
PATH_CORRECTED_RE = re.compile(r"GamePlan validation.*\bpath:.*corrected")

# --- T1: time lost on the Arena clock (plan section "T1") -------------------
#
# Measures, per event type, the seconds between Arena asking and the coach
# answering, and how much of Arena's turn timer was left at the answer.
# Four waste sources are quantified because plan section T1 names them as
# likely candidates:
#   * surfaced priority requests answered slowly (ask -> answer)
#   * watchdog waits after a hop-limit yield
#   * planning thrown away when the request changed mid-plan
#   * candidate full model calls spent only to conclude "pass again" on an
#     unchanged no-op priority window (equal window-change signatures mark
#     these as candidate waste, not proven-redundant — see the caveat the
#     output prints beside the figure; the waste that drained the clock in
#     match 9e858d62 turn 11 on 2026-11-08)

COACH_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})")
GRE_HEADER_TS_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4}) (\d{1,2}):(\d{2}):(\d{2}) (AM|PM)")
SURFACED_REQUEST_RE = re.compile(r"post-cast priority: new (\w+) request surfaced")
HOP_LIMIT_RE = re.compile(r"post-cast priority: hop limit \(\d+\) reached; leaving \w+ to the watchdog")
PLAN_TRIGGER_RE = re.compile(r"Autopilot planning: trigger=")
REQUEST_CHANGED_RE = re.compile(r"request changed during planning")
ANY_SUBMIT_RE = re.compile(r"mac bridge submit_\w+:|GRE bridge submitted ")
PASS_SUBMIT_RE = re.compile(r"mac bridge submit_pass:|GRE bridge submitted pass\b")
# Rope timers are deadline timers (decision_clock._DEADLINE_BEHAVIORS); the
# side map mirrors decision_clock._ROPE_TYPES.
_DEADLINE_BEHAVIORS = frozenset({"TimerBehavior_TakeControl", "TimerBehavior_Timeout"})
_ROPE_SIDES = {"TimerType_ActivePlayer": "active", "TimerType_NonActivePlayer": "nonactive"}

_ANSWER_SEARCH_LINES = 80  # surface -> answer lookahead (log lines)
_ANSWER_WINDOW_S = 120.0  # ...and max seconds between ask and answer
_HOP_WAIT_WINDOW_S = 90.0  # hop-limit yield -> watchdog answer window
_SUBMIT_DEDUPE_S = 0.5  # mac + GRE bridges log one submission twice

# "Clearing stale decision 'Priority' (window change to turn=9 active=1
# priority=2 phase=Phase_Main1 step=-)" — the game-window identity the
# redundant-pass detector keys on (real logged form, match 9e858d62).
WINDOW_CHANGE_RE = re.compile(
    r"window change to turn=(-?\d+) active=(-?\d+) priority=(-?\d+) "
    r"phase=(\S+) step=([^\s)]+)"
)


def parse_request_metrics(line):
    """Return the metrics dict from a ``[PROXY] Request metrics:`` log line.

    The payload is a Python repr (single quotes), not JSON, so it is parsed
    with ``ast.literal_eval``. Returns None when the line does not carry a
    parsable payload.
    """
    m = REQUEST_METRICS_RE.search(line)
    if not m:
        return None
    try:
        payload = ast.literal_eval(m.group(1))
    except (ValueError, SyntaxError):
        return None
    return payload if isinstance(payload, dict) else None


def parse_keep_lands(line):
    """Return the number of lands stated in a mulligan-guard KEEP line.

    Lines that justify a keep qualitatively (e.g. "Mountains cast Laelia ...")
    state no count and yield None; those games are excluded from the per-land
    win-rate table.
    """
    if not KEEP_LINE_RE.search(line):
        return None
    m = LANDS_RE.search(line)
    return int(m.group(1)) if m else None


def unmodelled_card_name(value):
    """Reduce an unevaluated-option description to the card name.

    Real logged values look like ``"Laelia, the Blade Reforged (its triggered
    ability)"``; the parenthetical qualifier is stripped.
    """
    text = str(value).strip()
    if text.startswith("T: "):
        text = text[3:]
    return text.split(" (")[0].strip()


def load_bundle(path):
    """Load one bundle file; transparently handles gzip and plain JSON."""
    with open(path, "rb") as fh:
        magic = fh.read(2)
    opener = gzip.open if magic == b"\x1f\x8b" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def iter_bundle_paths(directory):
    """Sorted bundle paths (*.json and *.json.gz) under ``directory``."""
    names = sorted(n for n in os.listdir(directory) if n.endswith(".json") or n.endswith(".json.gz"))
    return [os.path.join(directory, n) for n in names]


def _bundle_day(bundle):
    """UTC calendar date (YYYY-MM-DD) of the bundle's ``started_at``."""
    started = bundle.get("started_at")
    if isinstance(started, (int, float)):
        return datetime.fromtimestamp(started, tz=timezone.utc).strftime("%Y-%m-%d")
    return str(started or "")[:10]


def is_match_bundle(bundle):
    """True when the file is a match bundle, not e.g. a live_context.json
    pointer (which carries only paths/offsets and no coach_log or packet)."""
    return "coach_log" in bundle or "packet" in bundle


def coach_line_epoch(line):
    """Epoch seconds of a coach_log line's leading timestamp, or None."""
    m = COACH_TS_RE.match(line)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f").timestamp()


def gre_event_epoch(event):
    """Epoch seconds of a GRE player_log event's header timestamp, or None."""
    m = GRE_HEADER_TS_RE.match(str((event or {}).get("header_ts") or ""))
    if not m:
        return None
    month, day, year, hour, minute, second, ampm = m.groups()
    hour = int(hour) % 12 + (12 if ampm == "PM" else 0)
    return datetime(int(year), int(month), int(day), hour, int(minute), int(second)).timestamp()


def gre_event_epoch_precise(event):
    """Header-local clock coordinate with the payload's subsecond precision.

    Coach log timestamps and GRE headers are local wall time; payload epochs
    are UTC (eight hours apart in the retained example). Do not mix axes.
    Only the fractional second is applied, so timezone offsets cannot distort
    ask/answer joins. Absent payload precision retains the header coordinate.
    """
    from decimal import Decimal, InvalidOperation

    header = gre_event_epoch(event)
    if header is None:
        return None
    try:
        raw = str(json.loads(str((event or {}).get("json") or "{}")).get("timestamp") or "")
        if not raw.isdigit():
            return header
        stamp = Decimal(raw)
        scale = Decimal(10000000 if len(raw) >= 17 else 1000)
        if len(raw) < 12:
            return header
        return header + float((stamp % scale) / scale)
    except (ValueError, InvalidOperation):
        return header


def _timer_seconds(timer, key_ms, key_s):
    """Seconds from a timer field pair, preferring the millisecond key.

    Mirrors decision_clock._seconds so the metric reads timers exactly as
    the live coach does.
    """
    for key, scale in ((key_ms, 1000.0), (key_s, 1.0)):
        value = timer.get(key)
        if value is not None:
            try:
                return float(value) / scale
            except (TypeError, ValueError):
                continue
    return None


def _rope_report(timer, reported_at):
    """Frozen reading of one rope timer, mirroring decision_clock.seat_entry.

    Only deadline-behaviour rope timers count (TimerBehavior_TakeControl /
    TimerBehavior_Timeout on TimerType_ActivePlayer / NonActivePlayer).
    Returns {"duration_s", "elapsed_s", "running", "reported_at"}: the time
    left at ``reported_at`` is ``duration_s - elapsed_s``, decaying by wall
    time since only while ``running`` — a paused report freezes until the
    next one (review round 4: the previous revision derived remaining time
    from wall time since turn start, ignoring elapsedMs and paused state,
    so a paused 90 s rope with 30 s elapsed read 80 s ten seconds later
    instead of 60 s).
    """
    if timer.get("behavior") not in _DEADLINE_BEHAVIORS:
        return None
    side = _ROPE_SIDES.get(timer.get("type"))
    if side is None:
        return None
    duration = _timer_seconds(timer, "durationMs", "durationSec")
    if duration is None or duration <= 0:
        return None
    elapsed = _timer_seconds(timer, "elapsedMs", "elapsedSec") or 0.0
    return {
        "duration_s": duration,
        "elapsed_s": elapsed,
        "running": bool(timer.get("running")),
        "reported_at": reported_at,
    }


def _walk_payload_nodes(node):
    """Every dict in a parsed GRE event payload, in document order."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_payload_nodes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_payload_nodes(value)


def gre_clock_timeline(events):
    """Chronological clock snapshots from GRE player_log events.

    Each snapshot freezes what the log knew AT that event, so a rope-left
    value computed for a past timestamp never moves when later events are
    appended (2026-11-11 review round 3), and it reads Arena's own timer
    reports instead of deriving time from wall offsets (review round 4):

        [{"epoch": sec, "turn": int|None, "active_seat": int|None,
          "ropes": {seat: {"active": report|None, "nonactive": report|None}}}]

    Every message in an event payload is walked, so GameStateMessage /
    QueuedGameStateMessage timer updates count alongside TimerStateMessage
    ones: rope timers are owned via ``players[]`` ``systemSeatNumber``/
    ``timerIds`` pairs (unknown ownership is not guessed from timer numbering), and ``turn``/``active_seat`` come from whichever ``turnInfo``
    the payload carries. A rope report keeps the exact
    ``durationSec``/``elapsedMs``/``running`` the log stated; see
    _rope_report for how remaining time is read from it.
    """
    snaps = []
    cur_turn = None
    active_seat = None
    timer_seats: dict = {}
    ropes: dict = {}
    prev_epoch = None

    def owner_of(timer_id):
        if timer_id in timer_seats:
            return timer_seats[timer_id]
        return None

    for event in events or []:
        epoch = gre_event_epoch_precise(event)
        payload = str((event or {}).get("json") or "")
        if epoch is None or not payload:
            continue
        # Keep document order for occasional out-of-order header timestamps;
        # payload fractional precision distinguishes events within one second.
        if prev_epoch is not None and epoch < prev_epoch:
            epoch = prev_epoch
        prev_epoch = epoch
        try:
            root = json.loads(payload)
        except ValueError:
            continue
        nodes = list(_walk_payload_nodes(root))
        changed = False
        # Ownership first: a GameStateMessage may list its players after its
        # timers, so learn every timerId -> seat pair in this event before
        # attributing any of its timer reports.
        for node in nodes:
            for player in node.get("players") or []:
                if not isinstance(player, dict):
                    continue
                owner = player.get("systemSeatNumber")
                if owner is None:
                    continue
                for timer_id in player.get("timerIds") or []:
                    try:
                        timer_seats[int(timer_id)] = int(owner)
                    except (TypeError, ValueError):
                        continue
        for node in nodes:
            info = node.get("turnInfo")
            if isinstance(info, dict):
                try:
                    turn = int(info["turnNumber"])
                except (KeyError, TypeError, ValueError):
                    turn = None
                if turn is not None and turn != cur_turn:
                    cur_turn = turn
                    changed = True
                try:
                    seen_seat = int(info["activePlayer"])
                except (KeyError, TypeError, ValueError):
                    seen_seat = None
                if seen_seat is not None and seen_seat != active_seat:
                    active_seat = seen_seat
                    changed = True
            timers = node.get("timers")
            if not isinstance(timers, list):
                continue
            seat_hint = node.get("seatId")
            for timer in timers:
                if not isinstance(timer, dict):
                    continue
                # Stamp using this event coordinate; a query at arrival has
                # zero decay, and later queries decay only running reports.
                report = _rope_report(timer, epoch)
                if report is None:
                    continue
                try:
                    timer_id = int(timer.get("timerId") or 0)
                except (TypeError, ValueError):
                    timer_id = 0
                try:
                    owner = int(seat_hint) if seat_hint is not None else owner_of(timer_id)
                except (TypeError, ValueError):
                    owner = 0
                if not owner:
                    continue
                slot = ropes.setdefault(owner, {"active": None, "nonactive": None})
                side = _ROPE_SIDES[timer["type"]]
                if slot[side] != report:
                    slot[side] = report
                    changed = True
        if changed:
            snaps.append(
                {
                    "epoch": epoch,
                    "turn": cur_turn,
                    "active_seat": active_seat,
                    "ropes": {seat: dict(sides) for seat, sides in ropes.items()},
                }
            )
    return snaps


def rope_seconds_left(clock, epoch, local_seat=None):
    """Seconds of the local player's Arena decision rope left at ``epoch``.

    Reads the rope report visible at ``epoch`` exactly the way the live
    coach's decision_clock.seat_entry consumes per-event timer broadcasts:
    time left at the report is duration minus elapsed, decaying by wall time
    since the report only while that report said ``running`` — a paused
    reading freezes until the next one (review round 4: wall-time-since-
    turn-start ignored elapsedMs, paused state and GameStateMessage updates).
    The side follows the active seat as of ``epoch``.

    Queries use header-local coordinates with payload fractional precision.
    A report is visible only at or before the query; later same-second events
    cannot influence earlier measurements. No claim of matching all corpus
    timer events is made without an explicit per-event comparison.
    """
    if epoch is None or local_seat is None:
        return None
    snap = None
    for candidate in clock or []:
        if candidate["epoch"] <= epoch:
            snap = candidate
        else:
            break
    if snap is None:
        return None
    if snap["active_seat"] is None:
        return None
    side = "active" if snap["active_seat"] == local_seat else "nonactive"
    report = ((snap.get("ropes") or {}).get(local_seat) or {}).get(side)
    if report is None:
        return None
    remaining = report["duration_s"] - report["elapsed_s"]
    if report["running"]:
        remaining -= max(epoch - report["reported_at"], 0.0)
    return max(remaining, 0.0)


def final_rope_seconds(clock):
    """Each seat's rope time left as of the timeline's last snapshot."""
    if not clock:
        return {}
    end = clock[-1]["epoch"]
    out: dict = {}
    for candidate in clock:
        for seat, sides in (candidate.get("ropes") or {}).items():
            for side, rep in sides.items():
                if rep is None:
                    continue
                remaining = rep["duration_s"] - rep["elapsed_s"]
                if rep["running"]:
                    remaining -= max(end - rep["reported_at"], 0.0)
                out.setdefault(seat, {})[side] = max(remaining, 0.0)
    return out


def successful_submit(line):
    """Only an explicit successful bridge result is an observed answer."""
    if "GRE bridge submitted " in line:
        return True
    match = re.search(r"mac bridge submit_\w+:\s*(.*)$", line)
    if not match:
        return False
    try:
        result = ast.literal_eval(match.group(1))
    except (ValueError, SyntaxError):
        return False
    return isinstance(result, dict) and result.get("ok") is True


def surfaced_request_events(coach_lines, clock, local_seat=None):
    """Ask->answer seconds and rope left for each surfaced priority request.

    Returns (events, unanswered_by_type) where each event is
    {"event_type", "ask_to_ans_s", "rope_left_s"}. Attribution is bounded
    log-marker matching, not proof of every Arena request identity: these
    markers carry no request IDs. Supersession, cancellation and failed or
    unproven mac submissions cannot establish an answer.
    """
    events = []
    unanswered = Counter()
    for i, line in enumerate(coach_lines):
        m = SURFACED_REQUEST_RE.search(line)
        if not m:
            continue
        ask = coach_line_epoch(line)
        if ask is None:
            continue
        answer_epoch = None
        for j in range(i + 1, min(len(coach_lines), i + _ANSWER_SEARCH_LINES)):
            later = coach_line_epoch(coach_lines[j])
            if later is not None and later - ask > _ANSWER_WINDOW_S:
                break
            # A new surfaced request or cancellation closes this ask. Never
            # borrow a later request's answer, including undated boundaries.
            if SURFACED_REQUEST_RE.search(coach_lines[j]) or REQUEST_CHANGED_RE.search(coach_lines[j]):
                break
            if successful_submit(coach_lines[j]):
                if later is not None and later < ask:
                    break
                answer_epoch = later  # may be None if the submit line is undated
                break
        if answer_epoch is None:
            unanswered[m.group(1)] += 1
            continue
        events.append(
            {
                "event_type": m.group(1),
                "ask_to_ans_s": round(answer_epoch - ask, 3),
                "rope_left_s": rope_seconds_left(clock, answer_epoch, local_seat),
            }
        )
    return events, unanswered


HANDOFF_PICKUP_RE = re.compile(r"Triggering decision:|Triggers detected:|\| TRIGGER: ")


def hop_limit_waits(coach_lines):
    """Seconds each hop-limit yield added before the follow-up was picked up.

    When the post-cast chain hits its hop limit it leaves the follow-up
    request to the watchdog; the clock cost of that handover is only the
    delay until something picks the request up again. The normal trigger
    path usually does so within tens of milliseconds (2026-11-10
    evidence: median 0.18 s, p90 0.26 s over 72 yields in standalone.log)
    — the model call that then ANSWERS the follow-up is legitimate
    decision time, not waiting, and counting it (as an earlier revision
    did, yield -> submit) inflated this class ~10x. Measures yield -> the
    next pickup marker (``Triggering decision:``, ``Triggers detected:``
    or ``TRIGGER:`` — different trigger kinds log different lines); a
    yield followed by no pickup inside the window (the window closed with
    no follow-up) adds nothing.
    """
    waits = []
    for i, line in enumerate(coach_lines):
        if not HOP_LIMIT_RE.search(line):
            continue
        hop = coach_line_epoch(line)
        if hop is None:
            continue
        for j in range(i + 1, min(len(coach_lines), i + _ANSWER_SEARCH_LINES)):
            later = coach_line_epoch(coach_lines[j])
            if later is not None and later - hop > _HOP_WAIT_WINDOW_S:
                break
            if HANDOFF_PICKUP_RE.search(coach_lines[j]):
                if later is not None and later >= hop:
                    waits.append(round(later - hop, 3))
                break
    return waits


def cancellation_marker_families(coach_lines):
    """Count log markers, not independent requests or attributable elapsed time.

    Layered notices lack stable request identity; nearest preceding start cannot
    establish causal pairing or additive waste, even after deduplication.
    """
    families = []
    for line in coach_lines:
        if not REQUEST_CHANGED_RE.search(line):
            continue
        if "typed-decision:" in line:
            family = "typed_decision"
        elif "Backend returned error sentinel" in line:
            family = "backend_error_sentinel"
        elif "Autopilot:" in line:
            family = "autopilot"
        elif "LLM path failed" in line:
            family = "planner_path_failure"
        else:
            family = "other"
        families.append(family)
    return families


def cancellation_summary(families):
    return {
        "marker_count": len(families),
        "families": dict(sorted(Counter(families).items())),
        "unique_requests": None,
        "attributable_duration_s": None,
        "causal_time_rank_eligible": False,
        "interpretation": "layered log markers; request identity and causal duration unproven",
    }


def redundant_pass_cycles(coach_lines):
    """Decision-model seconds likely burned re-answering one unchanged pass window.

    A cycle is two consecutive pass submissions between which every
    ``Clearing stale decision ... (window change to ...)`` line carries
    the same turn/active/priority/phase/step signature the first pass was
    answered under, and no non-pass submission intervened (match 9e858d62
    turn 11, 2026-11-08: ~10 such cycles in ~79 s, each paying a full
    decision call that concluded "pass again" while the rope ran out).
    Only decision-class model seconds count — a background plan call
    inside the same interval is not waste attributable to the unchanged
    window. Submissions the mac and GRE bridges log twice within
    _SUBMIT_DEDUPE_S count once.

    Caveat (2026-11-11 review): equality of the logged turn/active/
    priority/phase/step signature does not by itself prove the stack,
    menu or material state were unchanged — the log line is a summary,
    not the state. Each counted cycle is therefore a *candidate* waste
    interval identified from the log, not a proven-redundant one; the
    runtime reuse guard additionally checks the menu fingerprint and the
    full material-state digest before it re-answers anything.
    """
    wasted_s = []
    last_pass_sig = None  # window signature the last counted pass saw
    sigs_since_pass = set()  # signatures observed since that pass
    cur_sig = None  # most recent window-change signature
    decision_s = 0.0  # decision-class model seconds since last submit
    last_submit_epoch = None
    last_submit_was_pass = False
    for line in coach_lines:
        metrics = parse_request_metrics(line)
        if metrics is not None:
            if str(metrics.get("call_class") or "").startswith("decision"):
                decision_s += (metrics.get("total_ms") or 0) / 1000.0
            continue
        m = WINDOW_CHANGE_RE.search(line)
        if m:
            cur_sig = m.groups()
            sigs_since_pass.add(cur_sig)
            continue
        if not ANY_SUBMIT_RE.search(line):
            continue
        epoch = coach_line_epoch(line)
        if epoch is None:
            continue
        is_pass = bool(PASS_SUBMIT_RE.search(line))
        if (
            last_submit_epoch is not None
            and is_pass == last_submit_was_pass
            and 0.0 < epoch - last_submit_epoch <= _SUBMIT_DEDUPE_S
        ):
            last_submit_epoch = epoch  # same submission logged by both bridges
            continue
        if (
            is_pass
            and last_pass_sig is not None
            and cur_sig == last_pass_sig
            and sigs_since_pass <= {last_pass_sig}
            and decision_s > 0.0
        ):
            wasted_s.append(round(decision_s, 3))
        last_pass_sig = cur_sig if is_pass else None
        sigs_since_pass = set()
        decision_s = 0.0
        last_submit_epoch = epoch
        last_submit_was_pass = is_pass
    return wasted_s


def t1_bundle_metrics(bundle):
    """T1 clock measurements for one bundle (plan section T1, item 1)."""
    coach_lines = ((bundle.get("coach_log") or {}).get("lines")) or []
    final_state = bundle.get("final_state") or {}
    local_seat = final_state.get("local_seat_id")
    clock = gre_clock_timeline(((bundle.get("player_log") or {}).get("events")) or [])
    events, unanswered = surfaced_request_events(coach_lines, clock, local_seat)
    return {
        "events": events,
        "unanswered_by_type": dict(unanswered),
        "hop_waits_s": hop_limit_waits(coach_lines),
        "cancellation_marker_families": cancellation_marker_families(coach_lines),
        "redundant_pass_cycles_s": redundant_pass_cycles(coach_lines),
        "local_seat": local_seat,
        "final_timers_by_seat": final_rope_seconds(clock),
    }


def summarize_bundle(bundle):
    """Compute this bundle's contribution to every metric.

    Pure function of the bundle dict — the tests exercise it directly.
    """
    settings = bundle.get("settings_subset") or {}
    final_state = bundle.get("final_state") or {}
    coach_lines = ((bundle.get("coach_log") or {}).get("lines")) or []

    out = {
        "match_id": bundle.get("match_id"),
        "day": _bundle_day(bundle),
        "autopilot": bool(settings.get("autopilot_enabled")),
        # Match-level result: bundle["result"] is the match outcome string
        # ("win"/"loss"); final_state.last_game_result is the last game's.
        "result": bundle.get("result") or final_state.get("last_game_result"),
        # decision stats
        "answers": 0,
        "pass_answers": 0,
        "code_answers": 0,
        "single_option_menus": 0,
        "decisions_with_line_search": 0,
        "decisions_with_unmodelled": 0,
        "chosen_unmodelled": 0,
        "unmodelled_cards": Counter(),
        # inference per call class: {class: {calls,input,cached,think,sec}}
        "call_classes": defaultdict(lambda: {"calls": 0, "input": 0, "cached": 0, "think": 0, "sec": 0.0}),
        # coach-log counters
        "plans_formed": 0,
        "path_corrected": 0,
        "big_prompts": [],
        # (lands_kept, result) pairs for parseable KEEP lines
        "keeps": [],
        # T1 clock measurements (plan section T1)
        "t1": {
            "events": [],
            "unanswered_by_type": Counter(),
            "hop_waits_s": [],
            "cancellation_marker_families": [],
            "redundant_pass_cycles_s": [],
        },
    }

    for line in coach_lines:
        metrics = parse_request_metrics(line)
        if metrics is not None:
            bucket = out["call_classes"][metrics.get("call_class")]
            bucket["calls"] += 1
            bucket["input"] += metrics.get("input_tokens") or 0
            bucket["cached"] += metrics.get("cached_input_tokens") or 0
            bucket["think"] += metrics.get("reasoning_tokens") or 0
            bucket["sec"] += (metrics.get("total_ms") or 0) / 1000.0
        if PLAN_FORMED_RE.search(line):
            out["plans_formed"] += 1
        if PATH_CORRECTED_RE.search(line):
            out["path_corrected"] += 1
        m = BIG_PROMPT_RE.search(line)
        if m:
            out["big_prompts"].append(int(m.group(1)))
        lands = parse_keep_lands(line)
        if lands is not None:
            out["keeps"].append((lands, out["result"]))

    for decision in (bundle.get("packet") or {}).get("decisions") or []:
        pending = decision.get("pending_decision") or {}
        options = pending.get("options") or []
        chosen = decision.get("chosen_options") or []
        trace = decision.get("trace") or {}

        if not chosen:
            continue  # no answer recorded (e.g. superseded by an executed action)
        out["answers"] += 1
        if len(options) == 1:
            out["single_option_menus"] += 1
        if chosen == ["pass"]:
            out["pass_answers"] += 1
        if "llm_usage" not in trace:
            out["code_answers"] += 1

        lines = trace.get("lines")
        if isinstance(lines, dict) and lines:
            out["decisions_with_line_search"] += 1
            unmodelled = lines.get("unmodelled") or {}
            if unmodelled:
                out["decisions_with_unmodelled"] += 1
                chosen_set = set(chosen)
                for key, value in unmodelled.items():
                    out["unmodelled_cards"][unmodelled_card_name(value)] += 1
                    if key in chosen_set:
                        out["chosen_unmodelled"] += 1

    out["t1"] = t1_bundle_metrics(bundle)

    return out


def _merge(dst, src):
    """Fold one bundle summary into a running total."""
    for key in (
        "answers",
        "pass_answers",
        "code_answers",
        "single_option_menus",
        "decisions_with_line_search",
        "decisions_with_unmodelled",
        "chosen_unmodelled",
        "plans_formed",
        "path_corrected",
    ):
        dst[key] += src[key]
    dst["unmodelled_cards"].update(src["unmodelled_cards"])
    dst["big_prompts"].extend(src["big_prompts"])
    dst["keeps"].extend(src["keeps"])
    for cls, bucket in src["call_classes"].items():
        agg = dst["call_classes"][cls]
        for field in ("calls", "input", "cached", "think"):
            agg[field] += bucket[field]
        agg["sec"] += bucket["sec"]
    t1_dst, t1_src = dst["t1"], src["t1"]
    t1_dst["events"].extend(t1_src["events"])
    t1_dst["unanswered_by_type"].update(t1_src["unanswered_by_type"])
    for key in ("hop_waits_s", "cancellation_marker_families", "redundant_pass_cycles_s"):
        t1_dst[key].extend(t1_src[key])
    dst["games"] += 1


def _stat_block(values):
    """n / median / p90 / total for a list of second-values."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return {"n": 0, "median_s": None, "p90_s": None, "total_s": 0.0}
    return {
        "n": len(vals),
        "median_s": round(vals[min(int(len(vals) * 0.5), len(vals) - 1)], 3),
        "p90_s": round(vals[min(int(len(vals) * 0.9), len(vals) - 1)], 3),
        "total_s": round(sum(vals), 3),
    }


def aggregate_t1(match_folds):
    """Aggregate the per-match T1 folds into the report's clock section."""
    by_type = defaultdict(lambda: {"answered": 0, "unanswered": 0, "ask_to_ans_s": [], "rope_left_s": []})
    hop, cancels, cycles = [], [], []
    per_match = {}
    for match_id, fold in match_folds.items():
        t1 = fold["t1"]
        pm = per_match.setdefault(
            match_id,
            {
                "surfaced_events": 0,
                "events_by_type": defaultdict(list),
                "unanswered_by_type": Counter(),
                "hop_waits_s": [],
                "cancellation_marker_families": [],
                "redundant_pass_cycles_s": [],
            },
        )
        for ev in t1["events"]:
            row = by_type[ev["event_type"]]
            row["answered"] += 1
            row["ask_to_ans_s"].append(ev["ask_to_ans_s"])
            if ev["rope_left_s"] is not None:
                row["rope_left_s"].append(ev["rope_left_s"])
            pm["surfaced_events"] += 1
            pm["events_by_type"][ev["event_type"]].append(ev)
        pm["unanswered_by_type"].update(t1["unanswered_by_type"])
        for event_type, count in t1["unanswered_by_type"].items():
            by_type[event_type]["unanswered"] += count
        for key, dst_list in (
            ("hop_waits_s", hop),
            ("cancellation_marker_families", cancels),
            ("redundant_pass_cycles_s", cycles),
        ):
            dst_list.extend(t1[key])
            pm[key].extend(t1[key])

    by_type_out = {}
    for etype, row in by_type.items():
        ropes = sorted(row["rope_left_s"])
        by_type_out[etype] = {
            "answered": row["answered"],
            "unanswered": row["unanswered"],
            "ask_to_ans": _stat_block(row["ask_to_ans_s"]),
            "rope_left_at_answer": (
                {
                    "n": len(ropes),
                    "median_s": round(ropes[min(int(len(ropes) * 0.5), len(ropes) - 1)], 3),
                    "min_s": round(ropes[0], 3),
                }
                if ropes
                else {"n": 0, "median_s": None, "min_s": None}
            ),
        }

    per_match_out = {}
    for match_id, pm in per_match.items():
        entry = {
            "surfaced_events": pm["surfaced_events"],
            "unanswered_by_type": dict(pm["unanswered_by_type"]),
            "events_by_type": {},
            "hop_waits": _stat_block(pm["hop_waits_s"]),
            "cancellation_markers": cancellation_summary(pm["cancellation_marker_families"]),
            "redundant_pass_cycles": _stat_block(pm["redundant_pass_cycles_s"]),
        }
        for etype, evs in pm["events_by_type"].items():
            ropes = [e["rope_left_s"] for e in evs if e["rope_left_s"] is not None]
            asks = sorted(e["ask_to_ans_s"] for e in evs)
            entry["events_by_type"][etype] = {
                "n": len(evs),
                "median_ask_to_ans_s": round(asks[min(int(len(asks) * 0.5), len(asks) - 1)], 3),
                "min_rope_left_s": round(min(ropes), 3) if ropes else None,
            }
        per_match_out[match_id] = entry

    cycles_block = _stat_block(cycles)
    cycles_block["interpretation"] = (
        "candidate waste intervals identified from equal window-change "
        "signatures; signature equality does not prove the stack, menu or "
        "material state was unchanged, and a static rerun of old bundles "
        "cannot demonstrate live savings"
    )
    return {
        "by_event_type": by_type_out,
        "hop_limit_watchdog_waits": _stat_block(hop),
        "cancellation_markers": cancellation_summary(cancels),
        "redundant_pass_cycles": cycles_block,
        "per_match": per_match_out,
    }


def aggregate(summaries):
    """Roll per-bundle summaries up into the report structure."""
    matches = {}  # match_id -> folded totals (a match may span several games)

    for summary in summaries:
        fold = matches.setdefault(
            summary["match_id"],
            {
                "games": 0,
                "answers": 0,
                "pass_answers": 0,
                "code_answers": 0,
                "single_option_menus": 0,
                "decisions_with_line_search": 0,
                "decisions_with_unmodelled": 0,
                "chosen_unmodelled": 0,
                "unmodelled_cards": Counter(),
                "call_classes": defaultdict(
                    lambda: {"calls": 0, "input": 0, "cached": 0, "think": 0, "sec": 0.0}
                ),
                "plans_formed": 0,
                "path_corrected": 0,
                "big_prompts": [],
                "keeps": [],
                "t1": {
                    "events": [],
                    "unanswered_by_type": Counter(),
                    "hop_waits_s": [],
                    "cancellation_marker_families": [],
                    "redundant_pass_cycles_s": [],
                },
                "_latest": None,
            },
        )
        _merge(fold, summary)
        fold["_latest"] = summary  # last game carries the match result

    n_matches = len(matches)

    # Wins/losses are match-level: count them once per match, from the last
    # game's summary (a best-of-three counted per game would inflate both).
    wl_by_autopilot = {
        "on": {"matches": 0, "wins": 0, "losses": 0},
        "off": {"matches": 0, "wins": 0, "losses": 0},
    }
    for fold in matches.values():
        latest = fold["_latest"]
        side = wl_by_autopilot["on" if latest["autopilot"] else "off"]
        side["matches"] += 1
        if latest["result"] == "win":
            side["wins"] += 1
        elif latest["result"] == "loss":
            side["losses"] += 1

    per_class = {}
    totals = {"calls": 0, "input": 0, "cached": 0, "think": 0, "sec": 0.0}
    for match_fold in matches.values():
        for cls, bucket in match_fold["call_classes"].items():
            agg = per_class.setdefault(cls, {"calls": 0, "input": 0, "cached": 0, "think": 0, "sec": 0.0})
            for field in ("calls", "input", "cached", "think"):
                agg[field] += bucket[field]
            agg["sec"] += bucket["sec"]
    for agg in per_class.values():
        for field in totals:
            totals[field] += agg[field]
    for agg in list(per_class.values()) + [totals]:
        agg["calls_per_match"] = agg["calls"] / n_matches if n_matches else 0.0
        agg["input_per_call"] = agg["input"] / agg["calls"] if agg["calls"] else 0.0
        agg["cached_per_call"] = agg["cached"] / agg["calls"] if agg["calls"] else 0.0
        agg["think_per_call"] = agg["think"] / agg["calls"] if agg["calls"] else 0.0
        agg["sec_per_call"] = agg["sec"] / agg["calls"] if agg["calls"] else 0.0

    answers_total = sum(fold["answers"] for fold in matches.values())
    pass_answers = sum(fold["pass_answers"] for fold in matches.values())
    code_answers = sum(fold["code_answers"] for fold in matches.values())
    single_option_menus = sum(fold["single_option_menus"] for fold in matches.values())

    line_decisions = sum(fold["decisions_with_line_search"] for fold in matches.values())
    unmodelled_decisions = sum(fold["decisions_with_unmodelled"] for fold in matches.values())
    chosen_unmodelled = sum(fold["chosen_unmodelled"] for fold in matches.values())
    card_counter = Counter()
    for fold in matches.values():
        card_counter.update(fold["unmodelled_cards"])

    plans_formed = sum(fold["plans_formed"] for fold in matches.values())
    path_corrected = sum(fold["path_corrected"] for fold in matches.values())

    keeps_by_lands = defaultdict(lambda: {"games": 0, "wins": 0})
    for fold in matches.values():
        for lands, result in fold["keeps"]:
            row = keeps_by_lands[lands]
            row["games"] += 1
            if result == "win":
                row["wins"] += 1

    all_prompts = sorted(p for fold in matches.values() for p in fold["big_prompts"])

    def pct(num, den):
        return (100.0 * num / den) if den else None

    def percentile(sorted_values, q):
        if not sorted_values:
            return None
        idx = min(int(len(sorted_values) * q), len(sorted_values) - 1)
        return sorted_values[idx]

    return {
        "matches": n_matches,
        "days_covered": sorted({s["day"] for s in summaries}),
        "autopilot": {
            key: {
                "matches": side["matches"],
                "wins": side["wins"],
                "losses": side["losses"],
                "win_rate_pct": pct(side["wins"], side["wins"] + side["losses"]),
            }
            for key, side in wl_by_autopilot.items()
        },
        "inference_per_class": per_class,
        "inference_totals": totals,
        "input_tokens_per_match": totals["input"] / n_matches if n_matches else 0.0,
        "answers": {
            "total": answers_total,
            "pass": pass_answers,
            "pass_pct": pct(pass_answers, answers_total),
            "code_answered_no_model_call": code_answers,
            "code_answered_pct": pct(code_answers, answers_total),
            "single_option_menus": single_option_menus,
            "per_match": answers_total / n_matches if n_matches else 0.0,
        },
        "line_search": {
            "decisions_with_line_search": line_decisions,
            "with_unevaluated_option": unmodelled_decisions,
            "with_unevaluated_pct": pct(unmodelled_decisions, line_decisions),
            "chosen_was_unevaluated": chosen_unmodelled,
            "top_unevaluated_cards": card_counter.most_common(10),
        },
        "plans_formed": plans_formed,
        "plans_path_corrected": path_corrected,
        "keeps_by_lands": {
            lands: {
                "games": row["games"],
                "wins": row["wins"],
                "win_rate_pct": pct(row["wins"], row["games"]),
            }
            for lands, row in sorted(keeps_by_lands.items())
        },
        "prompt_size_median_chars": percentile(all_prompts, 0.5),
        "prompt_size_p90_chars": percentile(all_prompts, 0.9),
        "t1": aggregate_t1(matches),
    }


def render_report(agg):
    """Human-readable report mirroring the baseline tables in plan section 1."""
    out = []
    ap = agg["autopilot"]
    out.append(f"Matches: {agg['matches']} (days covered: {', '.join(agg['days_covered']) or 'n/a'})")
    for key in ("on", "off"):
        side = ap[key]
        wr = side["win_rate_pct"]
        wr_s = f"{wr:.0f}%" if wr is not None else "-"
        out.append(
            f"  autopilot {key}: {side['matches']} matches "
            f"({side['wins']}W/{side['losses']}L, win rate {wr_s})"
        )

    out.append("")
    out.append("Inference per call class:")
    out.append(
        f"{'call_class':<28}{'calls/m':>9}{'in_tok/call':>13}"
        f"{'cached/call':>13}{'think/call':>12}{'s/call':>8}"
    )
    order = sorted(agg["inference_per_class"].items(), key=lambda kv: -kv[1]["calls"]) + [
        ("<ALL>", agg["inference_totals"])
    ]
    for cls, row in order:
        out.append(
            f"{cls:<28}{row['calls_per_match']:>9.1f}"
            f"{row['input_per_call']:>13.0f}{row['cached_per_call']:>13.0f}"
            f"{row['think_per_call']:>12.1f}{row['sec_per_call']:>8.2f}"
        )
    out.append(f"Input tokens per match: {agg['input_tokens_per_match']:.0f}")

    ans = agg["answers"]
    out.append("")
    out.append(f"Decision answers: {ans['total']} ({ans['per_match']:.1f}/match)")
    pass_pct = f"{ans['pass_pct']:.1f}%" if ans["pass_pct"] is not None else "-"
    code_pct = f"{ans['code_answered_pct']:.1f}%" if ans["code_answered_pct"] is not None else "-"
    out.append(f"  pass answers: {ans['pass']} ({pass_pct})")
    out.append(f"  answered by code without a model call: {ans['code_answered_no_model_call']} ({code_pct})")
    out.append(f"  single-option menus: {ans['single_option_menus']}")

    ls = agg["line_search"]
    uneval_pct = f"{ls['with_unevaluated_pct']:.1f}%" if ls["with_unevaluated_pct"] is not None else "-"
    out.append("")
    out.append(
        f"Line search: {ls['decisions_with_line_search']} decisions ran one; "
        f"{ls['with_unevaluated_option']} had an unevaluated option ({uneval_pct}); "
        f"a chosen option was unevaluated {ls['chosen_was_unevaluated']} times"
    )
    out.append("Most frequent unevaluated cards:")
    for name, count in ls["top_unevaluated_cards"]:
        out.append(f"  {count:>4}x {name}")

    pc_pct = (
        f"{100.0 * agg['plans_path_corrected'] / agg['plans_formed']:.1f}%" if agg["plans_formed"] else "-"
    )
    out.append("")
    out.append(
        f"Plans formed: {agg['plans_formed']}; path corrected: {agg['plans_path_corrected']} ({pc_pct})"
    )

    out.append("")
    out.append("Mulligan-guard keeps by lands kept:")
    out.append(f"{'lands':>6}{'games':>7}{'wins':>6}{'win rate':>10}")
    for lands, row in agg["keeps_by_lands"].items():
        wr = row["win_rate_pct"]
        wr_s = f"{wr:.0f}%" if wr is not None else "-"
        out.append(f"{lands:>6}{row['games']:>7}{row['wins']:>6}{wr_s:>10}")

    median = agg["prompt_size_median_chars"]
    p90 = agg["prompt_size_p90_chars"]
    out.append("")
    if median is not None:
        out.append(
            f"Decision prompt size (typed-decision big prompt lines): median {median} chars, p90 {p90} chars"
        )
    else:
        out.append("Decision prompt size: no 'typed-decision: big prompt' lines found")

    t1 = agg.get("t1") or {}

    def _secs(value):
        return f"{value:.2f}" if value is not None else "-"

    out.append("")
    out.append("Time on the Arena clock (T1):")
    out.append("Surfaced priority requests (Arena ask -> coach answer):")
    out.append(
        f"{'event_type':<22}{'answered':>9}{'med s':>8}{'p90 s':>8}"
        f"{'rope med s':>12}{'rope min s':>12}{'unans':>7}"
    )
    for etype, row in sorted(t1.get("by_event_type", {}).items()):
        ask = row["ask_to_ans"]
        rope = row["rope_left_at_answer"]
        out.append(
            f"{etype:<22}{row['answered']:>9}{_secs(ask['median_s']):>8}"
            f"{_secs(ask['p90_s']):>8}{_secs(rope['median_s']):>12}"
            f"{_secs(rope['min_s']):>12}{row['unanswered']:>7}"
        )
    hop = t1.get("hop_limit_watchdog_waits") or {}
    cancels = t1.get("cancellation_markers") or {}
    cycles = t1.get("redundant_pass_cycles") or {}
    out.append(
        f"Watchdog waits after hop-limit yield: {hop.get('n', 0)} events, "
        f"{hop.get('total_s', 0.0):.1f} s total "
        f"(median {_secs(hop.get('median_s'))} s)"
    )
    out.append(
        f"Request-change cancellation: {cancels.get('marker_count', 0)} log markers; "
        "unique requests and attributable duration unproven (excluded from causal-time ranking)"
    )
    out.append(
        "  marker families: "
        + ", ".join(f"{name}={count}" for name, count in sorted(cancels.get("families", {}).items()))
    )
    out.append(
        f"Candidate redundant model calls on unchanged pass windows: "
        f"{cycles.get('n', 0)} cycles / "
        f"{cycles.get('total_s', 0.0):.1f} s "
        f"(median {_secs(cycles.get('median_s'))} s)"
    )
    out.append(
        "  caveat: equal window-change signatures mark candidate waste only —"
        " they do not prove the stack/menu/material state was unchanged, and"
        " a static rerun cannot demonstrate live savings"
    )

    per_match = t1.get("per_match") or {}
    interesting = {
        mid: entry
        for mid, entry in per_match.items()
        if entry.get("surfaced_events")
        or entry["hop_waits"]["n"]
        or entry["cancellation_markers"]["marker_count"]
        or entry["redundant_pass_cycles"]["n"]
    }
    if interesting:
        out.append("")
        out.append(
            f"Per-match clock detail ({len(interesting)} of {len(per_match)} "
            f"matches had measurable clock events):"
        )
        out.append(
            f"{'match':<40}{'surf n/med s':>14}{'rope min s':>12}{'hop s':>8}{'cancel marks':>12}{'cyc n/s':>14}"
        )
        for mid, entry in sorted(interesting.items()):
            types = entry.get("events_by_type") or {}
            surf_n = sum(v["n"] for v in types.values())
            meds = sorted(
                v["median_ask_to_ans_s"] for v in types.values() if v["median_ask_to_ans_s"] is not None
            )
            surf_med = meds[len(meds) // 2] if meds else None
            rope_mins = [v["min_rope_left_s"] for v in types.values() if v["min_rope_left_s"] is not None]
            rope_min = min(rope_mins) if rope_mins else None
            cyc = entry["redundant_pass_cycles"]
            cyc_cell = f"{cyc['n']}/{cyc['total_s']:.1f}" if cyc["n"] else "-"
            surf_cell = f"{surf_n}/{surf_med:.2f}" if surf_n else "-"
            hop_cell = f"{entry['hop_waits']['total_s']:.1f}" if entry["hop_waits"]["n"] else "-"
            cancel_cell = str(entry["cancellation_markers"]["marker_count"])
            out.append(
                f"{mid:<40}{surf_cell:>14}{_secs(rope_min):>12}{hop_cell:>8}{cancel_cell:>12}{cyc_cell:>14}"
            )

    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Reproduce the Fable November baseline metrics from match bundles."
    )
    parser.add_argument(
        "--dir",
        "--bundles",
        dest="bundles_dir",
        default=DEFAULT_BUNDLES_DIR,
        help=f"match_bundles directory (default: {DEFAULT_BUNDLES_DIR})",
    )
    parser.add_argument(
        "--since",
        default=None,
        metavar="YYYY-MM-DD",
        help="only count bundles started on/after this UTC date",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the aggregated metrics as JSON instead of text"
    )
    args = parser.parse_args(argv)

    if not os.path.isdir(args.bundles_dir):
        parser.error(f"bundles directory not found: {args.bundles_dir}")

    summaries = []
    for path in iter_bundle_paths(args.bundles_dir):
        bundle = load_bundle(path)
        if not is_match_bundle(bundle):
            continue
        summary = summarize_bundle(bundle)
        if args.since and summary["day"] < args.since:
            continue
        summaries.append(summary)

    if not summaries:
        print(
            f"No bundles found in {args.bundles_dir}" + (f" on/after {args.since}" if args.since else ""),
            file=sys.stderr,
        )
        return 1

    agg = aggregate(summaries)
    if args.json:

        def _plain(obj):
            if isinstance(obj, Counter):
                return dict(obj)
            raise TypeError(repr(type(obj)))

        print(json.dumps(agg, indent=2, default=_plain))
    else:
        print(render_report(agg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
